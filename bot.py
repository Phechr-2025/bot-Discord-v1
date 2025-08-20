import os
import asyncio
import tempfile
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Literal

import discord
from discord.ext import commands
from discord import app_commands, Interaction
from discord.ui import View, Select, Button

import gdown
from aiohttp import web

# ---------- Healthcheck for Render ----------
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

# ---------- CONFIG ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ADMIN_ENV_IDS = {
    *[int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x]
}
DB_PATH = os.getenv("DB_PATH", "shopbot.db")
MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # ~24MB

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.message_content = False

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- UTILS ----------
def to_satang(thb: float) -> int:
    return int(round(thb * 100))

def fmt_thb(satang: int) -> str:
    return f"{satang/100:.2f} บาท"

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- DB ----------
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                discord_id INTEGER PRIMARY KEY
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('shop_open','1')")
        conn.commit()

def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
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
        conn.execute(
            "UPDATE users SET balance_cents = balance_cents + ? WHERE discord_id=?",
            (cents, discord_id),
        )
        conn.commit()

def list_items(active_only: bool = True) -> List[sqlite3.Row]:
    q = "SELECT * FROM items" + (" WHERE is_active=1" if active_only else "")
    with db() as conn:
        return list(conn.execute(q).fetchall())

def get_item(item_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

def upsert_item(
    name: str,
    price_cents: int,
    gdrive_url: str,
    filename: str = "video.mp4",
    item_id: Optional[int] = None,
) -> int:
    with db() as conn:
        cur = conn.cursor()
        if item_id is None:
            cur.execute(
                "INSERT INTO items (name, price_cents, gdrive_url, filename, is_active) VALUES (?,?,?,?,1)",
                (name, price_cents, gdrive_url, filename),
            )
            conn.commit()
            return cur.lastrowid
        else:
            cur.execute(
                "UPDATE items SET name=?, price_cents=?, gdrive_url=?, filename=? WHERE id=?",
                (name, price_cents, gdrive_url, filename, item_id),
            )
            conn.commit()
            return item_id

def delete_item(item_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.commit()
        return cur.rowcount > 0

def set_item_active(item_id: int, active: bool):
    with db() as conn:
        conn.execute("UPDATE items SET is_active=? WHERE id=?", (1 if active else 0, item_id))
        conn.commit()

def add_purchase(discord_id: int, item_id: int, price_cents: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO purchases (discord_id, item_id, price_cents, created_at) VALUES (?,?,?,?)",
            (discord_id, item_id, price_cents, now_utc_iso()),
        )
        conn.execute(
            "UPDATE users SET balance_cents = balance_cents - ? WHERE discord_id=?",
            (price_cents, discord_id),
        )
        conn.commit()

def get_my_purchases(discord_id: int, limit: int = 20) -> List[sqlite3.Row]:
    with db() as conn:
        return list(
            conn.execute(
                """
                SELECT p.id, p.created_at, p.price_cents, i.name
                FROM purchases p JOIN items i ON p.item_id=i.id
                WHERE p.discord_id=? ORDER BY p.id DESC LIMIT ?
                """,
                (discord_id, limit),
            ).fetchall()
        )

# ---------- Admin helpers ----------
def is_admin_user(user_id: int) -> bool:
    if user_id in ADMIN_ENV_IDS:
        return True
    with db() as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE discord_id=?", (user_id,)).fetchone()
        if row:
            return True
    return False

def is_admin(inter: Interaction) -> bool:
    guild_owner_ok = inter.guild is not None and inter.user.id == inter.guild.owner_id
    return guild_owner_ok or is_admin_user(inter.user.id)

def grant_admin(user_id: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO admins (discord_id) VALUES (?)", (user_id,))
        conn.commit()

def revoke_admin(user_id: int):
    with db() as conn:
        conn.execute("DELETE FROM admins WHERE discord_id=?", (user_id,))
        conn.commit()

# ---------- DOWNLOAD / DELIVERY ----------
async def download_drive_to_temp(url_or_id: str, filename_hint: str) -> Tuple[str, int]:
    def _download() -> Tuple[str, int]:
        tmpdir = tempfile.mkdtemp(prefix="shopclip_")
        out = os.path.join(
            tmpdir,
            filename_hint if filename_hint.endswith(".mp4") else f"{filename_hint}.mp4",
        )
        # ต้องแชร์ไฟล์เป็น Anyone with the link
        gdown.download(url_or_id, out, quiet=True, fuzzy=True)
        return out, os.path.getsize(out)

    return await asyncio.to_thread(_download)

async def deliver(
    *,
    user: discord.User,
    channel: Optional[discord.abc.Messageable],
    item_name: str,
    gdrive_url: str,
    filename: str,
    method: Literal["file", "link"],
):
    """ส่งคลิปตาม method และปลายทาง (DM หรือในห้อง)"""
    target = channel if channel is not None else await user.create_dm()

    if method == "link":
        await target.send(content=f"ลิงก์ดาวน์โหลดสำหรับ **{item_name}**:\\n{gdrive_url}")
        return

    # method == file
    path, size = await download_drive_to_temp(gdrive_url, filename or "video.mp4")
    if size <= MAX_UPLOAD_BYTES:
        await target.send(
            content=f"ส่งคลิป **{item_name}** ให้แล้วครับ 🎬",
            file=discord.File(path),
        )
    else:
        await target.send(
            content=(
                f"ไฟล์ **{item_name}** ขนาด {size/1024/1024:.1f}MB "
                f"เกินลิมิตอัปโหลดของ Discord ครับ 🙏\\nลิงก์ดาวน์โหลด: {gdrive_url}"
            )
        )

# ---------- UI ----------
class ShopSelect(Select):
    def __init__(self):
        rows = list_items(active_only=True)
        if not rows:
            super().__init__(placeholder="ยังไม่มีรายการจำหน่าย", options=[], disabled=True)
            return

        options = []
        for r in rows:
            options.append(
                discord.SelectOption(
                    label=r["name"][:100],
                    value=str(r["id"]),
                    description=f"ราคา {fmt_thb(r['price_cents'])}",
                )
            )
        super().__init__(placeholder="เลือกรายการทั้งหมด", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: Interaction):
        # ร้านเปิดไหม
        if get_setting("shop_open", "1") != "1":
            return await interaction.response.send_message("ตอนนี้ร้านปิดชั่วคราว ⛔ กรุณามาใหม่ภายหลัง", ephemeral=True)

        item_id = int(self.values[0])
        item = get_item(item_id)
        if not item or not item["is_active"]:
            return await interaction.response.send_message("รายการนี้ไม่พร้อมจำหน่ายแล้วครับ", ephemeral=True)

        price = item["price_cents"]
        bal = get_balance(interaction.user.id)
        if bal < price:
            need = price - bal
            return await interaction.response.send_message(
                f"ยอดเงินของคุณไม่พอสำหรับ **{item['name']}** (ต้องการ {fmt_thb(price)})\\n"
                f"ยอดคงเหลือของคุณ: {fmt_thb(bal)}\\nโปรดติดต่อแอดมินเพื่อเติมเงินอีก {fmt_thb(need)}",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"ยืนยันซื้อ **{item['name']}** ราคา {fmt_thb(price)} ?\\n"
            f"เลือกรูปแบบการส่งและปลายทางด้านล่าง👇",
            view=ConfirmBuyView(item_id=item_id),
            ephemeral=True,  # ยืนยันส่วนตัว กันสแปมหน้าห้อง
        )

class ConfirmBuyView(View):
    def __init__(self, item_id: int):
        super().__init__(timeout=120)
        self.item_id = item_id

    async def _handle(self, interaction: Interaction, method: Literal["file", "link"], dest: Literal["dm", "channel"]):
        item = get_item(self.item_id)
        if not item or not item["is_active"]:
            return await interaction.response.edit_message(content="รายการนี้ไม่พร้อมจำหน่ายแล้วครับ", view=None)

        # ร้านเปิดไหม
        if get_setting("shop_open", "1") != "1":
            return await interaction.response.edit_message(content="ตอนนี้ร้านปิดชั่วคราว ⛔", view=None)

        price = item["price_cents"]
        bal = get_balance(interaction.user.id)
        if bal < price:
            return await interaction.response.edit_message(content="ยอดเงินไม่พอ", view=None)

        await interaction.response.edit_message(content="กำลังเตรียมให้คุณ... ⏳", view=None)
        add_purchase(interaction.user.id, item["id"], price)

        channel = interaction.channel if dest == "channel" else None
        await deliver(
            user=interaction.user,
            channel=channel,
            item_name=item["name"],
            gdrive_url=item["gdrive_url"],
            filename=item["filename"] or "video.mp4",
            method=method,
        )

        where_txt = "ในห้องนี้" if dest == "channel" else "ทาง DM"
        how_txt = "เป็นไฟล์" if method == "file" else "เป็นลิงก์"
        await interaction.followup.send(
            f"ซื้อ **{item['name']}** เสร็จสิ้น ✅ | ราคา {fmt_thb(price)}\\n"
            f"ได้ทำการส่ง {how_txt} {where_txt} แล้วครับ 🎬\\n"
            f"ยอดคงเหลือ: {fmt_thb(get_balance(interaction.user.id))}",
            ephemeral=True,
        )

    # 4 ปุ่ม: (ไฟล์/ลิงก์) x (DM/ห้อง)
    @discord.ui.button(label="ส่งเป็นไฟล์ + DM", style=discord.ButtonStyle.success)
    async def file_dm(self, interaction: Interaction, button: Button):
        await self._handle(interaction, "file", "dm")

    @discord.ui.button(label="ส่งเป็นไฟล์ + ในห้องนี้", style=discord.ButtonStyle.success)
    async def file_chan(self, interaction: Interaction, button: Button):
        await self._handle(interaction, "file", "channel")

    @discord.ui.button(label="ส่งเป็นลิงก์ + DM", style=discord.ButtonStyle.primary)
    async def link_dm(self, interaction: Interaction, button: Button):
        await self._handle(interaction, "link", "dm")

    @discord.ui.button(label="ส่งเป็นลิงก์ + ในห้องนี้", style=discord.ButtonStyle.primary)
    async def link_chan(self, interaction: Interaction, button: Button):
        await self._handle(interaction, "link", "channel")

class MenuView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(ShopSelect())

    @discord.ui.button(emoji="🔄", label="รีเซ็ตเมนู", style=discord.ButtonStyle.secondary)
    async def reset_btn(self, interaction: Interaction, button: Button):
        await interaction.response.edit_message(content="เมนูรีเฟรชแล้ว ✅", view=MenuView())

    @discord.ui.button(emoji="💰", label="เช็คยอดเงิน", style=discord.ButtonStyle.secondary)
    async def balance_btn(self, interaction: Interaction, button: Button):
        await interaction.response.send_message(
            f"ยอดคงเหลือของคุณ: **{fmt_thb(get_balance(interaction.user.id))}**",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🧾", label="ประวัติการซื้อ", style=discord.ButtonStyle.secondary)
    async def history_btn(self, interaction: Interaction, button: Button):
        rows = get_my_purchases(interaction.user.id, limit=20)
        if not rows:
            return await interaction.response.send_message("ยังไม่มีประวัติการซื้อครับ", ephemeral=True)
        lines = [f"- {r['name']} | {fmt_thb(r['price_cents'])} | {r['created_at']}" for r in rows]
        await interaction.response.send_message("ประวัติการซื้อ 20 รายการล่าสุด:\\n" + "\\n".join(lines), ephemeral=True)

# ---------- Slash Commands: user ----------
@bot.tree.command(name="menu", description="เปิดเมนูร้าน (สาธารณะ)")
async def menu_cmd(interaction: Interaction):
    is_open = get_setting("shop_open", "1") == "1"
    title = "[ ร้านเปิดให้บริการ ]" if is_open else "[ ร้านปิดชั่วคราว ]"
    desc = "เลือกจากเมนูด้านล่างได้เลยครับ" if is_open else "ยังไม่เปิดขายในตอนนี้"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=MenuView(), ephemeral=False)

@bot.tree.command(name="menu_private", description="เปิดเมนูร้าน (เห็นคนเดียว)")
async def menu_private_cmd(interaction: Interaction):
    is_open = get_setting("shop_open", "1") == "1"
    title = "[ ร้านเปิดให้บริการ ]" if is_open else "[ ร้านปิดชั่วคราว ]"
    desc = "เลือกจากเมนูด้านล่างได้เลยครับ" if is_open else "ยังไม่เปิดขายในตอนนี้"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
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
    await interaction.response.send_message("ประวัติของคุณ:\\n" + "\\n".join(lines), ephemeral=True)

@bot.tree.command(name="ping", description="ทดสอบบอท")
async def ping_cmd(interaction: Interaction):
    await interaction.response.send_message("pong! ✅", ephemeral=True)

# ---------- Slash Commands: admin ----------
def require_admin(inter: Interaction) -> Optional[str]:
    return None if is_admin(inter) else "ต้องเป็นแอดมินเท่านั้น"

@bot.tree.command(name="admin_add_item", description="(แอดมิน) เพิ่มสินค้า")
@app_commands.describe(
    name="ชื่อที่จะแสดง",
    price_thb="ราคา (บาท)",
    gdrive_url="ลิงก์ Google Drive",
    filename="ชื่อไฟล์ .mp4",
)
async def admin_add_item(
    interaction: Interaction,
    name: str,
    price_thb: float,
    gdrive_url: str,
    filename: Optional[str] = "video.mp4",
):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    item_id = upsert_item(
        name=name,
        price_cents=to_satang(price_thb),
        gdrive_url=gdrive_url,
        filename=filename or "video.mp4",
    )
    await interaction.response.send_message(
        f"เพิ่มสินค้า #{item_id}: **{name}** ราคา {price_thb:.2f} บาท",
        ephemeral=True,
    )

@bot.tree.command(name="admin_edit_item", description="(แอดมิน) แก้ไขสินค้า")
@app_commands.describe(
    item_id="รหัสสินค้า",
    name="ชื่อใหม่",
    price_thb="ราคาใหม่ (บาท)",
    gdrive_url="ลิงก์ใหม่",
    filename="ไฟล์ .mp4",
)
async def admin_edit_item(
    interaction: Interaction,
    item_id: int,
    name: str,
    price_thb: float,
    gdrive_url: str,
    filename: Optional[str] = "video.mp4",
):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    if not get_item(item_id):
        return await interaction.response.send_message("ไม่พบสินค้า", ephemeral=True)
    upsert_item(
        name=name,
        price_cents=to_satang(price_thb),
        gdrive_url=gdrive_url,
        filename=filename or "video.mp4",
        item_id=item_id,
    )
    await interaction.response.send_message(f"แก้ไขสินค้า #{item_id} เรียบร้อย", ephemeral=True)

@bot.tree.command(name="admin_delete_item", description="(แอดมิน) ลบสินค้า")
@app_commands.describe(item_id="รหัสสินค้า")
async def admin_delete_item(interaction: Interaction, item_id: int):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    ok = delete_item(item_id)
    await interaction.response.send_message("ลบเรียบร้อย" if ok else "ไม่พบสินค้า", ephemeral=True)

@bot.tree.command(name="admin_toggle_item", description="(แอดมิน) เปิด/ปิด การขายสินค้า (รายชิ้น)")
@app_commands.describe(item_id="รหัสสินค้า", active="เปิดขายหรือไม่")
async def admin_toggle_item(interaction: Interaction, item_id: int, active: bool):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    if not get_item(item_id):
        return await interaction.response.send_message("ไม่พบสินค้า", ephemeral=True)
    set_item_active(item_id, active)
    await interaction.response.send_message(
        f"{'เปิด' if active else 'ปิด'}การขายสินค้ารหัส #{item_id} แล้ว",
        ephemeral=True,
    )

@bot.tree.command(name="admin_items", description="(แอดมิน) ดูรายการสินค้าทั้งหมด")
async def admin_items(interaction: Interaction):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)

    rows = list_items(active_only=False)
    if not rows:
        return await interaction.response.send_message("ยังไม่มีสินค้า", ephemeral=True)

    lines: List[str] = []
    for r in rows:
        status = "ON" if r["is_active"] else "OFF"
        item_id = r["id"]
        name = r["name"]
        price = fmt_thb(r["price_cents"])
        lines.append(f"#{item_id} | {status} | {name} | {price}")

    await interaction.response.send_message("\\n".join(lines), ephemeral=True)

@bot.tree.command(name="admin_add_balance", description="(แอดมิน) เติมเงินให้ผู้ใช้")
@app_commands.describe(user="เลือกผู้ใช้", amount_thb="จำนวนเงิน (บาท)")
async def admin_add_balance(interaction: Interaction, user: discord.User, amount_thb: float):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    add_balance(user.id, to_satang(amount_thb))
    await interaction.response.send_message(
        f"เติมเงินให้ {user.mention} จำนวน {amount_thb:.2f} บาท แล้ว",
        ephemeral=True,
    )

@bot.tree.command(name="admin_shop_toggle", description="(แอดมิน) เปิด/ปิดร้านทั้งระบบ")
@app_commands.describe(is_open="เปิดร้านหรือไม่")
async def admin_shop_toggle(interaction: Interaction, is_open: bool):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    set_setting("shop_open", "1" if is_open else "0")
    await interaction.response.send_message(
        "เปิดร้านแล้ว ✅" if is_open else "ปิดร้านแล้ว ⛔",
        ephemeral=True,
    )

@bot.tree.command(name="admin_grant", description="(เจ้าของกิลด์) เพิ่มสิทธิ์แอดมิน")
@app_commands.describe(user="ผู้ใช้ที่จะให้สิทธิ์")
async def admin_grant_cmd(interaction: Interaction, user: discord.User):
    if interaction.guild is None or interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะเจ้าของกิลด์", ephemeral=True)
    grant_admin(user.id)
    await interaction.response.send_message(f"ให้สิทธิ์แอดมินแก่ {user.mention} แล้ว", ephemeral=True)

@bot.tree.command(name="admin_revoke", description="(เจ้าของกิลด์) ยกเลิกสิทธิ์แอดมิน")
@app_commands.describe(user="ผู้ใช้ที่จะยกเลิกสิทธิ์")
async def admin_revoke_cmd(interaction: Interaction, user: discord.User):
    if interaction.guild is None or interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะเจ้าของกิลด์", ephemeral=True)
    revoke_admin(user.id)
    await interaction.response.send_message(f"ยกเลิกสิทธิ์แอดมินของ {user.mention} แล้ว", ephemeral=True)

# ---------- STARTUP ----------
@bot.event
async def on_ready():
    db_init()
    bot.loop.create_task(run_web_server())

    # sync รายกิลด์ (ใช้งานได้ทันที)
    try:
        for g in bot.guilds:
            await bot.tree.sync(guild=g)
            print(f"Synced commands to guild {g.name} ({g.id})")
    except Exception as e:
        print("Guild sync error:", e)

    # เผื่อไว้ sync global
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global commands")
    except Exception as e:
        print("Global sync error:", e)

    print(f"Logged in as {bot.user}")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("กรุณาตั้งค่า DISCORD_TOKEN ใน Environment Variables")
    bot.run(DISCORD_TOKEN)
