import os
import asyncio
import tempfile
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Literal

import re

import discord
from discord.ext import commands
from discord import app_commands, Interaction
from discord.ui import View, Select, Button, Modal, TextInput

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
INTENTS.members = True
INTENTS.message_content = False

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- UTILS ----------
def to_satang(thb: float) -> int:
    return int(round(thb * 100))

def fmt_thb(satang: int) -> str:
    return f"{satang/100:.2f} บาท"

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_user_id(text: str) -> Optional[int]:
    """
    รับได้ทั้งรูปแบบ: 1234567890, <@123>, <@!123>, mention ธรรมดา
    """
    if not text:
        return None
    s = text.strip()
    s = s.strip("<>@!#& ")
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        try:
            return int(digits)
        except Exception:
            return None
    return None

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
        # ประวัติการโอนเงิน
        c.execute("""
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # ตั้งค่าแยกต่อกิลด์ (channel ids)
        c.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            )
        """)
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

def get_guild_setting(guild_id: int, key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key)).fetchone()
        return row["value"] if row else default

def set_guild_setting(guild_id: int, key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, value),
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

# ---------- Transfers ----------
def transfer_balance(from_id: int, to_id: int, amount_cents: int) -> Tuple[bool, str]:
    if amount_cents <= 0:
        return False, "จำนวนเงินต้องมากกว่า 0"
    if from_id == to_id:
        return False, "ไม่สามารถโอนให้ตัวเองได้"

    ensure_user(from_id)
    ensure_user(to_id)
    with db() as conn:
        row = conn.execute("SELECT balance_cents FROM users WHERE discord_id=?", (from_id,)).fetchone()
        bal = row["balance_cents"] if row else 0
        if bal < amount_cents:
            return False, "ยอดเงินไม่พอ"

        conn.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE discord_id=?",
                     (amount_cents, from_id))
        conn.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE discord_id=?",
                     (amount_cents, to_id))
        conn.execute("INSERT INTO transfers (from_id, to_id, amount_cents, created_at) VALUES (?,?,?,?)",
                     (from_id, to_id, amount_cents, now_utc_iso()))
        conn.commit()
    return True, "โอนเงินสำเร็จ"

# ---------- Google Drive link normalize & download helpers ----------
_GDRIVE_ID_RE = re.compile(r'(?:/d/|id=)([A-Za-z0-9_-]{10,})')

def _clean_link(s: str) -> str:
    return s.strip().strip('<>').strip('"\'' )

def _gdrive_file_id(s: str) -> Optional[str]:
    s = _clean_link(s)
    m = _GDRIVE_ID_RE.search(s)
    if m:
        return m.group(1)
    if "/" not in s and len(s) >= 10:
        return s
    return None

def normalize_gdrive_for_download(url_or_id: str) -> str:
    s = _clean_link(url_or_id)
    fid = _gdrive_file_id(s)
    if fid:
        return f"https://drive.google.com/uc?id={fid}"
    return s

# ---------- DOWNLOAD / DELIVERY ----------
async def download_drive_to_temp(url_or_id: str, filename_hint: str) -> Tuple[str, int]:
    def _download() -> Tuple[str, int]:
        tmpdir = tempfile.mkdtemp(prefix="shopclip_")
        out = os.path.join(
            tmpdir,
            filename_hint if filename_hint.endswith(".mp4") else f"{filename_hint}.mp4",
        )
        url = normalize_gdrive_for_download(url_or_id)
        gdown.download(url, out, quiet=True, fuzzy=True)
        return out, os.path.getsize(out)

    # timeout 120s to avoid hanging
    return await asyncio.wait_for(asyncio.to_thread(_download), timeout=120)

async def deliver_file(
    *,
    user: discord.User,
    channel: Optional[discord.abc.Messageable],
    item_name: str,
    gdrive_url: str,
    filename: str,
) -> Tuple[bool, str]:
    """ส่งไฟล์ .mp4 เท่านั้น; คืน (success, error_message)"""
    target = channel if channel is not None else await user.create_dm()
    clean_link = normalize_gdrive_for_download(gdrive_url)

    try:
        path, size = await download_drive_to_temp(clean_link, filename or "video.mp4")
        if size <= MAX_UPLOAD_BYTES:
            await target.send(content=f"ส่งคลิป **{item_name}** ให้แล้วครับ 🎬", file=discord.File(path))
            return True, ""
        else:
            return False, f"ไฟล์ {item_name} ขนาด {size/1024/1024:.1f}MB เกินลิมิตอัปโหลดของ Discord"
    except asyncio.TimeoutError:
        return False, "การเตรียมไฟล์ใช้เวลานานเกินกำหนด"
    except Exception as e:
        return False, f"เกิดข้อผิดพลาดระหว่างดาวน์โหลด: {e}"

# ---------- Logging helpers ----------
async def log_to_channel_id(guild: Optional[discord.Guild], channel_id_str: str, text: str):
    if guild is None or not channel_id_str:
        return
    try:
        cid = int(channel_id_str)
    except Exception:
        return
    ch = guild.get_channel(cid) or bot.get_channel(cid)
    if ch is None:
        try:
            ch = await bot.fetch_channel(cid)
        except Exception:
            ch = None
    if ch:
        try:
            await ch.send(text)
        except Exception:
            pass

def preferred_send_channel(interaction: Interaction) -> Optional[discord.abc.Messageable]:
    """เลือกห้องส่งวิดีโอจาก guild_settings.send_channel_id (ถ้าไม่ตั้งจะใช้ห้องที่สั่ง)"""
    guild = interaction.guild
    if guild is None:
        return None
    cid_str = get_guild_setting(guild.id, "send_channel_id", "")
    if not cid_str:
        return interaction.channel
    try:
        cid = int(cid_str)
    except Exception:
        return interaction.channel
    ch = guild.get_channel(cid) or bot.get_channel(cid)
    return ch or interaction.channel

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
        super().__init__(placeholder="เลือกรายการทั้งหมด", min_values=1, max_values=1, options=options, custom_id='shop:select')

    async def callback(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        if get_setting("shop_open", "1") != "1":
            return await interaction.followup.send("ตอนนี้ร้านปิดชั่วคราว ⛔ กรุณามาใหม่ภายหลัง", ephemeral=True)

        item_id = int(self.values[0])
        item = get_item(item_id)
        if not item or not item["is_active"]:
            return await interaction.followup.send("รายการนี้ไม่พร้อมจำหน่ายแล้วครับ", ephemeral=True)

        price = item["price_cents"]
        bal = get_balance(interaction.user.id)
        if bal < price:
            need = price - bal
            return await interaction.followup.send(
                f"ยอดเงินของคุณไม่พอสำหรับ **{item['name']}** (ต้องการ {fmt_thb(price)})\n"
                f"ยอดคงเหลือของคุณ: {fmt_thb(bal)}\nโปรดติดต่อแอดมินเพื่อเติมเงินอีก {fmt_thb(need)}",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"ยืนยันซื้อ **{item['name']}** ราคา {fmt_thb(price)} ?\n"
            f"เลือกรูปแบบการส่งด้านล่าง👇",
            view=ConfirmBuyView(item_id=item_id),
            ephemeral=True,
        )

class ConfirmBuyView(View):
    def __init__(self, item_id: int):
        super().__init__(timeout=None)
        self.item_id = item_id

    async def _handle(self, interaction: Interaction, dest: Literal["dm", "channel"]):
        item = get_item(self.item_id)
        if not item or not item["is_active"]:
            return await interaction.response.edit_message(content="รายการนี้ไม่พร้อมจำหน่ายแล้วครับ", view=None)

        if get_setting("shop_open", "1") != "1":
            return await interaction.response.edit_message(content="ตอนนี้ร้านปิดชั่วคราว ⛔", view=None)

        price = item["price_cents"]
        bal = get_balance(interaction.user.id)
        if bal < price:
            return await interaction.response.edit_message(content="ยอดเงินไม่พอ", view=None)

        await interaction.response.edit_message(content="กำลังเตรียมไฟล์ให้คุณ... ⏳", view=None)
        add_purchase(interaction.user.id, item["id"], price)

        channel = None
        if dest == "channel":
            channel = preferred_send_channel(interaction)

        ok, err = await deliver_file(
            user=interaction.user,
            channel=channel,
            item_name=item["name"],
            gdrive_url=item["gdrive_url"],
            filename=item["filename"] or "video.mp4",
        )

        if not ok:
            add_balance(interaction.user.id, price)
            where_txt = "ในห้องที่กำหนด" if dest == "channel" else "ทาง DM"
            return await interaction.followup.send(
                f"ขออภัย ส่งไฟล์ไม่สำเร็จ ({err}) ❌\n"
                f"ได้ทำการคืนเงิน {fmt_thb(price)} ให้แล้วครับ\n"
                f"ปลายทาง: {where_txt}",
                ephemeral=True,
            )

        where_txt = "ในห้องที่กำหนด" if dest == "channel" else "ทาง DM"
        await interaction.followup.send(
            f"ซื้อ **{item['name']}** เสร็จสิ้น ✅ | ราคา {fmt_thb(price)}\n"
            f"ได้ทำการส่งไฟล์ {where_txt} แล้วครับ 🎬\n"
            f"ยอดคงเหลือ: {fmt_thb(get_balance(interaction.user.id))}",
            ephemeral=True,
        )

        # log purchase by channel id (if set)
        if interaction.guild:
            await log_to_channel_id(
                interaction.guild,
                get_guild_setting(interaction.guild.id, "log_purchase_channel_id", ""),
                f"🛒 **Purchase** by {interaction.user.mention} | Item: **{item['name']}** | Price: {fmt_thb(price)} | {now_utc_iso()}",
            )

    @discord.ui.button(label="ส่งไฟล์ทาง DM", style=discord.ButtonStyle.success, custom_id='shop:file_dm')
    async def file_dm(self, interaction: Interaction, button: Button):
        await self._handle(interaction, "dm")

    @discord.ui.button(label="ส่งไฟล์ในห้องที่กำหนด", style=discord.ButtonStyle.success, custom_id='shop:file_chan')
    async def file_chan(self, interaction: Interaction, button: Button):
        await self._handle(interaction, "channel")

# ---- Modal สำหรับโอนเงิน ----
class TransferModal(Modal, title="โอนเงินให้ผู้ใช้"):
    to_user = TextInput(label="ผู้รับ (ใส่ @mention หรือ ID)", placeholder="@someone หรือ 1234567890", required=True, max_length=64)
    amount = TextInput(label="จำนวนเงิน (บาท เช่น 10 หรือ 10.50)", placeholder="0.00", required=True, max_length=16)

    def __init__(self, opener_id: int):
        super().__init__(timeout=None, custom_id="shop:transfer_modal")
        self.opener_id = opener_id

    async def on_submit(self, interaction: Interaction):
        to_id = parse_user_id(str(self.to_user.value))
        if not to_id:
            return await interaction.response.send_message("รูปแบบผู้รับไม่ถูกต้อง", ephemeral=True)
        if to_id == interaction.user.id:
            return await interaction.response.send_message("ไม่สามารถโอนให้ตัวเองได้", ephemeral=True)
        try:
            amount_thb = float(str(self.amount.value).replace(",", "").strip())
        except Exception:
            return await interaction.response.send_message("จำนวนเงินไม่ถูกต้อง", ephemeral=True)
        if amount_thb <= 0:
            return await interaction.response.send_message("จำนวนเงินต้องมากกว่า 0", ephemeral=True)

        ok, msg = transfer_balance(interaction.user.id, to_id, to_satang(amount_thb))
        if ok:
            bal = fmt_thb(get_balance(interaction.user.id))
            try:
                target_user = await interaction.client.fetch_user(to_id)
                to_display = target_user.mention
            except Exception:
                to_display = f"<@{to_id}>"
            await interaction.response.send_message(
                f"โอนเงินสำเร็จ ✅ จำนวน {amount_thb:.2f} บาท ให้ {to_display}\nยอดคงเหลือของคุณ: {bal}",
                ephemeral=True
            )
            if interaction.guild:
                await log_to_channel_id(
                    interaction.guild,
                    get_guild_setting(interaction.guild.id, "log_transfer_channel_id", ""),
                    f"🤝 **Transfer** {interaction.user.mention} → {to_display} | Amount: {amount_thb:.2f} บาท | {now_utc_iso()}",
                )
        else:
            await interaction.response.send_message(f"โอนเงินไม่สำเร็จ: {msg}", ephemeral=True)

class MenuView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ShopSelect())

    @discord.ui.button(emoji="🔄", label="รีเซ็ตเมนู", style=discord.ButtonStyle.secondary, custom_id='shop:reset')
    async def reset_btn(self, interaction: Interaction, button: Button):
        await interaction.response.edit_message(content="เมนูรีเฟรชแล้ว ✅", view=MenuView())

    @discord.ui.button(emoji="💰", label="เช็คยอดเงิน", style=discord.ButtonStyle.secondary, custom_id='shop:balance')
    async def balance_btn(self, interaction: Interaction, button: Button):
        await interaction.response.send_message(
            f"ยอดคงเหลือของคุณ: **{fmt_thb(get_balance(interaction.user.id))}**",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🧾", label="ประวัติการซื้อ", style=discord.ButtonStyle.secondary, custom_id='shop:history')
    async def history_btn(self, interaction: Interaction, button: Button):
        rows = get_my_purchases(interaction.user.id, limit=20)
        if not rows:
            return await interaction.response.send_message("ยังไม่มีประวัติการซื้อครับ", ephemeral=True)
        lines = [f"- {r['name']} | {fmt_thb(r['price_cents'])} | {r['created_at']}" for r in rows]
        await interaction.response.send_message("ประวัติการซื้อ 20 รายการล่าสุด:\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(emoji="🤝", label="โอนเงิน", style=discord.ButtonStyle.primary, custom_id='shop:transfer')
    async def transfer_btn(self, interaction: Interaction, button: Button):
        await interaction.response.send_modal(TransferModal(opener_id=interaction.user.id))

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
    await interaction.response.send_message("ประวัติของคุณ:\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="transfer", description="โอนเงินให้ผู้ใช้อื่น")
@app_commands.describe(user="ผู้รับ", amount_thb="จำนวนเงิน (บาท)")
async def transfer_cmd(interaction: Interaction, user: discord.User, amount_thb: float):
    if amount_thb <= 0:
        return await interaction.response.send_message("จำนวนเงินต้องมากกว่า 0", ephemeral=True)
    if user.id == interaction.user.id:
        return await interaction.response.send_message("ไม่สามารถโอนให้ตัวเองได้", ephemeral=True)

    ok, msg = transfer_balance(interaction.user.id, user.id, to_satang(amount_thb))
    if ok:
        await interaction.response.send_message(
            f"โอนเงินสำเร็จ ✅ จำนวน {amount_thb:.2f} บาท ให้ {user.mention}\n"
            f"ยอดคงเหลือของคุณ: {fmt_thb(get_balance(interaction.user.id))}",
            ephemeral=True
        )
        if interaction.guild:
            await log_to_channel_id(
                interaction.guild,
                get_guild_setting(interaction.guild.id, "log_transfer_channel_id", ""),
                f"🤝 **Transfer** {interaction.user.mention} → {user.mention} | Amount: {amount_thb:.2f} บาท | {now_utc_iso()}",
            )
    else:
        await interaction.response.send_message(f"โอนเงินไม่สำเร็จ: {msg}", ephemeral=True)

@bot.tree.command(name="ping", description="ทดสอบบอท")
async def ping_cmd(interaction: Interaction):
    await interaction.response.send_message("pong! ✅", ephemeral=True)

# ---------- Slash Commands: admin ----------
def require_admin(inter: Interaction) -> Optional[str]:
    return None if is_admin(inter) else "ต้องเป็นแอดมินเท่านั้น"

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

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
    if interaction.guild:
        await log_to_channel_id(
            interaction.guild,
            get_guild_setting(interaction.guild.id, "log_topup_channel_id", ""),
            f"➕ **Topup** {user.mention} by {interaction.user.mention} | Amount: {amount_thb:.2f} บาท | {now_utc_iso()}",
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

# ---- Admin: check balance by picking user ----
@bot.tree.command(name="admin_check_balance", description="(แอดมิน) เช็คยอดเงินของสมาชิกโดยเลือกชื่อ")
@app_commands.describe(user="เลือกผู้ใช้ที่ต้องการตรวจสอบ")
async def admin_check_balance(interaction: Interaction, user: discord.User):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    bal = fmt_thb(get_balance(user.id))
    await interaction.response.send_message(f"ยอดเงินของ {user.mention}: **{bal}**", ephemeral=True)

# ---- Admin: set channels BY ID (text input) ----
@bot.tree.command(name="admin_set_send_channel_id", description="(แอดมิน) ตั้งค่า ID ห้องสำหรับส่งวิดีโอในกลุ่ม")
@app_commands.describe(channel_id="ระบุเลข ID ของห้อง (เช่น 123456789012345678)")
async def admin_set_send_channel_id(interaction: Interaction, channel_id: str):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("ต้องใช้ในกิลด์เท่านั้น", ephemeral=True)
    set_guild_setting(interaction.guild.id, "send_channel_id", channel_id.strip())
    await interaction.response.send_message(f"ตั้งค่า send_channel_id = `{channel_id}` เรียบร้อย", ephemeral=True)

@bot.tree.command(name="admin_set_log_transfer_id", description="(แอดมิน) ตั้งค่า ID ห้องสำหรับบันทึกประวัติการโอน")
@app_commands.describe(channel_id="ระบุเลข ID ของห้อง")
async def admin_set_log_transfer_id(interaction: Interaction, channel_id: str):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("ต้องใช้ในกิลด์เท่านั้น", ephemeral=True)
    set_guild_setting(interaction.guild.id, "log_transfer_channel_id", channel_id.strip())
    await interaction.response.send_message(f"ตั้งค่า log_transfer_channel_id = `{channel_id}` เรียบร้อย", ephemeral=True)

@bot.tree.command(name="admin_set_log_purchase_id", description="(แอดมิน) ตั้งค่า ID ห้องสำหรับบันทึกประวัติการซื้อ")
@app_commands.describe(channel_id="ระบุเลข ID ของห้อง")
async def admin_set_log_purchase_id(interaction: Interaction, channel_id: str):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("ต้องใช้ในกิลด์เท่านั้น", ephemeral=True)
    set_guild_setting(interaction.guild.id, "log_purchase_channel_id", channel_id.strip())
    await interaction.response.send_message(f"ตั้งค่า log_purchase_channel_id = `{channel_id}` เรียบร้อย", ephemeral=True)

@bot.tree.command(name="admin_set_log_topup_id", description="(แอดมิน) ตั้งค่า ID ห้องสำหรับบันทึกประวัติการเติมเงิน")
@app_commands.describe(channel_id="ระบุเลข ID ของห้อง")
async def admin_set_log_topup_id(interaction: Interaction, channel_id: str):
    msg = require_admin(interaction)
    if msg:
        return await interaction.response.send_message(msg, ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("ต้องใช้ในกิลด์เท่านั้น", ephemeral=True)
    set_guild_setting(interaction.guild.id, "log_topup_channel_id", channel_id.strip())
    await interaction.response.send_message(f"ตั้งค่า log_topup_channel_id = `{channel_id}` เรียบร้อย", ephemeral=True)

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

    # Register persistent menu view
    bot.add_view(MenuView())

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
