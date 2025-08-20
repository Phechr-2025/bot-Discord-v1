import os
import asyncio
import tempfile
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands, Interaction
from discord.ui import View, Select, Button

# ดาวน์โหลดจาก Google Drive
import gdown

# ---- health check server สำหรับ Render (ต้อง bind $PORT) ----
from aiohttp import web

async def _health(request):
    return web.Response(text="ok")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] listening on :{port}")

# -------------------- CONFIG --------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ADMIN_USER_IDS = {
    *[int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x]
}
DB_PATH = os.getenv("DB_PATH", "shopbot.db")
MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # ~24MB

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.message_content = False

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# -------------------- UTIL --------------------
def to_satang(thb: float) -> int:
    return int(round(thb * 100))

def fmt_thb(satang: int) -> str:
    return f"{satang/100:.2f} บาท"

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# -------------------- DB LAYER --------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                balance_cents INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                gdrive_url TEXT NOT NULL,
                filename TEXT DEFAULT 'video.mp4',
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(item_id) REFERENCES items(id)
            )
        """)
        conn.commit()

def ensure_user(discord_id: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO users (discord_id) VALUES (?)", (discord_id,))
        conn.commit()

def get_balance(discord_id: int) -> int:
    ensure_user(discord_id)
    with db() as conn:
        row = conn.execute("SELECT balance_cents FROM users WHERE discord_id=?", (discord_id,)).fetchone()
        return row["balance_cents"] if row else 0

def add_balance(discord_id: int, cents: int):
    ensure_user(discord_id)
    with db() as conn:
        conn.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE discord_id=?",
                     (cents, discord_id))
        conn.commit()

def list_items(active_only=True) -> List[sqlite3.Row]:
    q = "SELECT * FROM items" + (" WHERE is_active=1" if active_only else "")
    with db() as conn:
        return list(conn.execute(q).fetchall())

def get_item(item_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

def upsert_item(name: str, price_cents: int, gdrive_url: str, filename: str = "video.mp4",
                item_id: Optional[int] = None) -> int:
    with db() as conn:
        cur = conn.cursor()
        if item_id is None:
            cur.execute("""INSERT INTO items (name, price_cents, gdrive_url, filename, is_active)
                           VALUES (?,?,?,?,1)""",
                        (name, price_cents, gdrive_url, filename))
            conn.commit()
            return cur.lastrowid
        else:
            cur.execute("""UPDATE items SET name=?, price_cents=?, gdrive_url=?, filename=?
                           WHERE id=?""",
                        (name, price_cents, gdrive_url, filename, item_id))
            conn.commit()
            return item_id

def set_item_active(item_id: int, active: bool):
    with db() as conn:
        conn.execute("UPDATE items SET is_active=? WHERE id=?", (1 if active else 0, item_id))
        conn.commit()

def add_purchase(discord_id: int, item_id: int, price_cents: int):
    with db() as conn:
        conn.execute("""INSERT INTO purchases (discord_id, item_id, price_cents, created_at)
                        VALUES (?,?,?,?)""",
                     (discord_id, item_id, price_cents, now_utc_iso()))
        conn.execute("""UPDATE users SET balance_cents = balance_cents - ?
                        WHERE discord_id=?""", (price_cents, discord_id))
        conn.commit()

def get_my_purchases(discord_id: int, limit: int = 20) -> List[sqlite3.Row]:
    with db() as conn:
        return list(conn.execute("""
            SELECT p.id, p.created_at, p.price_cents, i.name
            FROM purchases p JOIN items i ON p.item_id=i.id
            WHERE p.discord_id=? ORDER BY p.id DESC LIMIT ?
        """, (discord_id, limit)).fetchall())

# -------------------- DOWNLOAD / DELIVERY --------------------
async def download_drive_to_temp(url_or_id: str, filename_hint: str) -> Tuple[str, int]:
    def _download() -> Tuple[str, int]:
        tmpdir = tempfile.mkdtemp(prefix="shopclip_")
        out = os.path.join(tmpdir, filename_hint if filename_hint.endswith(".mp4")
                           else f"{filename_hint}.mp4")
        # ต้องแชร์ไฟล์ Drive เป็น Anyone with the link
        gdown.download(url_or_id, out, quiet=True, fuzzy=True)
        return out, os.path.getsize(out)
    return await asyncio.to_thread(_download)

async def send_video_or_link(user: discord.User, file_path: str, size_bytes: int, fallback_url: str, item_name: str):
    try:
        dm = await user.create_dm()
        if size_bytes <= MAX_UPLOAD_BYTES:
            await dm.send(content=f"ส่งคลิป **{item_name}** ให้แล้วครับ 🎬",
                          file=discord.File(file_path))
        else:
            await dm.send(content=(f"ไฟล์ **{item_name}** ขนาด {size_bytes/1024/1024:.1f}MB "
                                   f"เกินลิมิตอัปโหลดของ Discord ครับ 🙏\nลิงก์ดาวน์โหลด: {fallback_url}"))
    except discord.Forbidden:
        pass

# -------------------- UI --------------------
class ShopSelect(Select):
    def __init__(self):
        rows = list_items(active_only=True)
        if not rows:
            super().__init__(placeholder="ยังไม่มีรายการจำหน่าย", options=[], disabled=True)
            return
        options = []
        for r in rows:
            options.append(discord.SelectOption(
                label=r["name"][:100],
                value=str(r["id"]),
                description=f"ราคา {fmt_thb(r['price_cents'])}"
            ))
        super().__init__(placeholder="เลือกรายการทั้งหมด", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: Interaction):
        item_id = int(self.values[0])
        item = get_item(item_id)
        if not item or not item["is_active"]:
            return await interaction.response.send_message("รายการนี้ไม่พร้อมจำหน่ายแล้วครับ", ephemeral=True)

        price = item["price_cents"]
        bal = get_balance(interaction.user.id)
        if bal < price:
            need = price - bal
            return await interaction.response.send_message(
                f"ยอดเงินของคุณไม่พอสำหรับ **{item['name']}** (ต้องการ {fmt_thb(price)})\n"
                f"ยอดคงเหลือของคุณ: {fmt_thb(bal)}\nโปรดติดต่อแอดมินเพื่อเติมเงินอีก {fmt_thb(need)}",
                ephemeral=True
            )
        await interaction.response.send_message(
            f"ยืนยันซื้อ **{item['name']}** ราคา {fmt_thb(price)} ?",
            view=ConfirmBuyView(item_id=item_id),
            ephemeral=True
        )

class ConfirmBuyView(View):
    def __init__(self, item_id: int):
        super().__init__(timeout=60)
        self.item_id = item_id

    @discord.ui.button(label="ยืนยันซื้อ", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: Interaction, button: Button):
        item = get_item(self.item_id)
        if not item or not item["is_active"]:
            return await interaction.response.edit_message(content="รายการนี้ไม่พร้อมจำหน่ายแล้วครับ", view=None)

        price = item["price_cents"]
        bal = get_balance(interaction.user.id)
        if bal < price:
            return await interaction.response.edit_message(
                content=f"ยอดเงินไม่พอ (ต้องการ {fmt_thb(price)} คงเหลือ {fmt_thb(bal)})", view=None
            )

        await interaction.response.edit_message(content="กำลังเตรียมไฟล์ให้คุณ... ⏳", view=None)
        # ตัดเงิน + บันทึก
        add_purchase(interaction.user.id, item["id"], price)

        try:
            path, size = await download_drive_to_temp(item["gdrive_url"], item["filename"] or "video.mp4")
        except Exception as e:
            add_balance(interaction.user.id, price)  # ย้อนเงิน
            return await interaction.followup.send(
                f"โหลดไฟล์ **{item['name']}** ไม่สำเร็จ: {e}\nคืนเงิน {fmt_thb(price)} ให้แล้ว",
                ephemeral=True
            )

        await send_video_or_link(interaction.user, path, size, item["gdrive_url"], item["name"])
        await interaction.followup.send(
            f"ซื้อ **{item['name']}** เสร็จสิ้น ✅\n"
            f"ราคา {fmt_thb(price)} | ยอดคงเหลือ: {fmt_thb(get_balance(interaction.user.id))}\n"
            f"ส่งคลิปให้ทาง DM แล้วครับ 🎬",
            ephemeral=True
        )

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: Button):
        await interaction.response.edit_message(content="ยกเลิกการซื้อแล้วครับ", view=None)

class MenuView(View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(ShopSelect())

    @discord.ui.button(emoji="💰", label="เช็คยอดเงิน", style=discord.ButtonStyle.primary)
    async def balance_btn(self, interaction: Interaction, button: Button):
        await interaction.response.send_message(
            f"ยอดคงเหลือของคุณ: **{fmt_thb(get_balance(interaction.user.id))}**",
            ephemeral=True
        )

    @discord.ui.button(emoji="🧾", label="ประวัติการซื้อ", style=discord.ButtonStyle.blurple)
    async def history_btn(self, interaction: Interaction, button: Button):
        rows = get_my_purchases(interaction.user.id, limit=20)
        if not rows:
            return await interaction.response.send_message("ยังไม่มีประวัติการซื้อครับ", ephemeral=True)
        lines = [f"- {r['name']} | {fmt_thb(r['price_cents'])} | {r['created_at']}" for r in rows]
        await interaction.response.send_message("ประวัติการซื้อ 20 รายการล่าสุด:\n" + "\n".join(lines), ephemeral=True)

# -------- Slash Commands (ผู้ใช้) --------
@bot.tree.command(name="menu", description="เปิดเมนูร้าน")
async def menu_cmd(interaction: Interaction):
    embed = discord.Embed(
        title="[ รายการทั้งหมด ]",
        description="เลือกจากเมนูด้านล่างได้เลยครับ",
        color=discord.Color.dark_embed()
    )
    await interaction.response.send_message(embed=embed, view=MenuView(), ephemeral=True)

@bot.tree.command(name="balance", description="เช็คยอดเงินของฉัน")
async def balance_cmd(interaction: Interaction):
    await interaction.response.send_message(
        f"ยอดคงเหลือของคุณ: **{fmt_thb(get_balance(interaction.user.id))}**", ephemeral=True
    )

@bot.tree.command(name="history", description="ดูประวัติการซื้อของฉัน")
async def history_cmd(interaction: Interaction):
    rows = get_my_purchases(interaction.user.id, limit=50)
    if not rows:
        return await interaction.response.send_message("ยังไม่มีประวัติการซื้อครับ", ephemeral=True)
    lines = [f"- {r['name']} | {fmt_thb(r['price_cents'])} | {r['created_at']}" for r in rows]
    await interaction.response.send_message("ประวัติของคุณ:\n" + "\n".join(lines), ephemeral=True)

# -------- Slash Commands (แอดมิน) --------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

@bot.tree.command(name="admin_add_item", description="(แอดมิน) เพิ่มสินค้า")
@app_commands.describe(name="ชื่อที่จะแสดง", price_thb="ราคา (บาท)", gdrive_url="ลิงก์ Google Drive", filename="ชื่อไฟล์ .mp4")
async def admin_add_item(interaction: Interaction, name: str, price_thb: float, gdrive_url: str, filename: Optional[str] = "video.mp4"):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("ต้องเป็นแอดมินเท่านั้น", ephemeral=True)
    item_id = upsert_item(name=name, price_cents=to_satang(price_thb), gdrive_url=gdrive_url, filename=filename or "video.mp4")
    await interaction.response.send_message(f"เพิ่มสินค้า #{item_id}: **{name}** ราคา {price_thb:.2f} บาท", ephemeral=True)

@bot.tree.command(name="admin_edit_item", description="(แอดมิน) แก้ไขสินค้า")
@app_commands.describe(item_id="รหัสสินค้า", name="ชื่อใหม่", price_thb="ราคาใหม่ (บาท)", gdrive_url="ลิงก์ใหม่", filename="ไฟล์ .mp4")
async def admin_edit_item(interaction: Interaction, item_id: int, name: str, price_thb: float, gdrive_url: str, filename: Optional[str] = "video.mp4"):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("ต้องเป็นแอดมินเท่านั้น", ephemeral=True)
    if not get_item(item_id):
        return await interaction.response.send_message("ไม่พบสินค้า", ephemeral=True)
    upsert_item(name=name, price_cents=to_satang(price_thb), gdrive_url=gdrive_url, filename=filename or "video.mp4", item_id=item_id)
    await interaction.response.send_message(f"แก้ไขสินค้า #{item_id} เรียบร้อย", ephemeral=True)

@bot.tree.command(name="admin_toggle_item", description="(แอดมิน) เปิด/ปิด การขายสินค้า")
@app_commands.describe(item_id="รหัสสินค้า", active="เปิดขายหรือไม่")
async def admin_toggle_item(interaction: Interaction, item_id: int, active: bool):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("ต้องเป็นแอดมินเท่านั้น", ephemeral=True)
    if not get_item(item_id):
        return await interaction.response.send_message("ไม่พบสินค้า", ephemeral=True)
    set_item_active(item_id, active)
    await interaction.response.send_message(f"{'เปิด' if active else 'ปิด'}การขายสินค้ารหัส #{item_id} แล้ว", ephemeral=True)

@bot.tree.command(name="admin_items", description="(แอดมิน) ดูรายการสินค้าทั้งหมด")
async def admin_items(interaction: Interaction):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("ต้องเป็นแอดมินเท่านั้น", ephemeral=True)
    rows = list_items(active_only=False)
    if not rows:
        return await interaction.response.send_message("ยังไม่มีสินค้า", ephemeral=True)
    lines = [f"#{r['id']} | {'ON' if r['is_active'] else 'OFF'} | {r['name']} | {fmt_thb(r['price_cents'])}" for r in rows]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="admin_add_balance", description="(แอดมิน) เติมเงินให้ผู้ใช้")
@app_commands.describe(user="เลือกผู้ใช้", amount_thb="จำนวนเงิน (บาท)")
async def admin_add_balance(interaction: Interaction, user: discord.User, amount_thb: float):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("ต้องเป็นแอดมินเท่านั้น", ephemeral=True)
    add_balance(user.id, to_satang(amount_thb))
    await interaction.response.send_message(f"เติมเงินให้ {user.mention} จำนวน {amount_thb:.2f} บาท แล้ว", ephemeral=True)

# -------------------- STARTUP --------------------
@bot.event
async def on_ready():
    # start web health server (Render)
    bot.loop.create_task(run_web_server())

    db_init()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user}")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("กรุณาตั้งค่า DISCORD_TOKEN ใน Environment Variables")
    bot.run(DISCORD_TOKEN)
