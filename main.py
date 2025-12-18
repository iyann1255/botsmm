import os
import time
import json
import math
import sqlite3
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8329932894:AAFZraICrLDMWAFutaSNXAyx4CBXXbz0Xjk").strip()

ZAYN_API_KEY = os.getenv("ZAYN_API_KEY", "XExeyYNlEDJoAWT8uTw0oQVIQZpfCqcQZr6KrKW1jy").strip()
ZAYN_API_URL = os.getenv("ZAYN_API_URL", "https://zaynflazz.com/api/sosial-media").strip()
ZAYN_PROFILE_URL = os.getenv("ZAYN_PROFILE_URL", "https://zaynflazz.com/api/profile").strip()

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "5504473114").replace(" ", "").split(",") if x.strip().isdigit()]

DEFAULT_MARKUP_PERCENT = float(os.getenv("DEFAULT_MARKUP_PERCENT", "10"))
NONSELLER_MARKUP_PERCENT = float(os.getenv("NONSELLER_MARKUP_PERCENT", "15"))

PRICE_PER_1000 = float(os.getenv("PRICE_PER_1000", "1"))  # multiplier (kalau kamu mau konversi)
COOLDOWN_SECONDS = float(os.getenv("COOLDOWN_SECONDS", "2"))
SERVICES_CACHE_TTL = int(os.getenv("SERVICES_CACHE_TTL", "300"))
DB_PATH = os.getenv("DB_PATH", "smm_bot.db").strip()

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))
BOT_NAME = os.getenv("BOT_NAME", "SMM Bot").strip()

if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN belum diisi.")
if not ZAYN_API_KEY:
    raise SystemExit("ENV ZAYN_API_KEY belum diisi.")
if not ADMIN_IDS:
    raise SystemExit("ENV ADMIN_IDS belum diisi. Contoh: ADMIN_IDS=5504473114")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("smm-bot")

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_seller INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                created_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                provider_order_id TEXT,
                service_id TEXT,
                service_name TEXT,
                link TEXT,
                quantity INTEGER,
                price INTEGER,
                status TEXT,
                created_at INTEGER
            )
            """
        )

def ensure_user(user_id: int, username: str = ""):
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users(user_id, username, is_seller, balance, created_at) VALUES(?,?,?,?,?)",
                (user_id, username or "", 0, 0, now),
            )
        else:
            conn.execute("UPDATE users SET username=? WHERE user_id=?", (username or "", user_id))

def get_user(user_id: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row

def set_balance(user_id: int, amount: int):
    with db() as conn:
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))

def add_balance(user_id: int, delta: int):
    with db() as conn:
        conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (delta, user_id))

def set_seller(user_id: int, is_seller: bool):
    with db() as conn:
        conn.execute("UPDATE users SET is_seller=? WHERE user_id=?", (1 if is_seller else 0, user_id))

def create_order(
    user_id: int,
    provider_order_id: str,
    service_id: str,
    service_name: str,
    link: str,
    quantity: int,
    price: int,
    status: str,
):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO orders(user_id, provider_order_id, service_id, service_name, link, quantity, price, status, created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (user_id, provider_order_id, service_id, service_name, link, quantity, price, status, int(time.time())),
        )

def list_orders(user_id: int, limit: int = 10) -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

def get_order_by_provider_id(provider_order_id: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM orders WHERE provider_order_id=?", (provider_order_id,)).fetchone()

def update_order_status(provider_order_id: str, status: str):
    with db() as conn:
        conn.execute("UPDATE orders SET status=? WHERE provider_order_id=?", (status, provider_order_id))

# =========================
# RATE LIMIT
# =========================
_last_action: Dict[int, float] = {}

def cooldown_ok(user_id: int) -> bool:
    now = time.time()
    last = _last_action.get(user_id, 0.0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_action[user_id] = now
    return True

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# =========================
# ZAYN API CLIENT
# =========================
_services_cache: Dict[str, Any] = {"ts": 0, "data": None}

def _post(url: str, payload: Dict[str, Any]) -> Any:
    # Banyak panel SMM pakai form-encoded POST.
    r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    # Bisa json, bisa text json
    try:
        return r.json()
    except Exception:
        return json.loads(r.text)

def zayn_services(force: bool = False) -> List[Dict[str, Any]]:
    now = int(time.time())
    if (not force) and _services_cache["data"] and (now - int(_services_cache["ts"]) < SERVICES_CACHE_TTL):
        return _services_cache["data"]

    payload = {"key": ZAYN_API_KEY, "action": "services"}
    data = _post(ZAYN_API_URL, payload)

    # Normalisasi: beberapa API balikin list langsung, ada juga {data:[...]}
    services = None
    if isinstance(data, list):
        services = data
    elif isinstance(data, dict):
        for k in ("data", "services", "result"):
            if k in data and isinstance(data[k], list):
                services = data[k]
                break
        if services is None and "response" in data and isinstance(data.get("response"), list):
            services = data["response"]

    if not isinstance(services, list):
        raise ValueError(f"Format services tidak dikenali: {str(data)[:220]}")

    _services_cache["ts"] = now
    _services_cache["data"] = services
    return services

def zayn_add_order(service_id: str, link: str, quantity: int) -> Dict[str, Any]:
    payload = {
        "key": ZAYN_API_KEY,
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": quantity,
    }
    data = _post(ZAYN_API_URL, payload)
    if not isinstance(data, dict):
        raise ValueError("Response add order bukan dict.")
    return data

def zayn_status(order_id: str) -> Dict[str, Any]:
    payload = {"key": ZAYN_API_KEY, "action": "status", "order": order_id}
    data = _post(ZAYN_API_URL, payload)
    if not isinstance(data, dict):
        raise ValueError("Response status bukan dict.")
    return data

def zayn_profile() -> Dict[str, Any]:
    # Umumnya profile endpoint cukup key doang
    payload = {"key": ZAYN_API_KEY}
    data = _post(ZAYN_PROFILE_URL, payload)
    if not isinstance(data, dict):
        raise ValueError("Response profile bukan dict.")
    return data

# =========================
# PRICING
# =========================
def pick_service_fields(svc: Dict[str, Any]) -> Tuple[str, str, float, str]:
    """
    Return: (service_id, name, rate_per_1000, category)
    Tries common keys.
    """
    sid = str(svc.get("service") or svc.get("id") or svc.get("service_id") or "")
    name = str(svc.get("name") or svc.get("service_name") or "Unknown Service")
    cat = str(svc.get("category") or svc.get("type") or svc.get("group") or "-")

    rate = svc.get("rate") or svc.get("price") or svc.get("harga") or svc.get("cost")
    try:
        rate_f = float(rate)
    except Exception:
        rate_f = 0.0

    return sid, name, rate_f, cat

def calc_price_idr(user_row: sqlite3.Row, base_rate_per_1000: float, quantity: int) -> int:
    # Base rate * quantity/1000 * multiplier
    raw = base_rate_per_1000 * (quantity / 1000.0) * PRICE_PER_1000
    markup = DEFAULT_MARKUP_PERCENT if int(user_row["is_seller"]) == 1 else NONSELLER_MARKUP_PERCENT
    final = raw * (1.0 + (markup / 100.0))
    # bulatkan ke atas biar ga rugi
    return int(math.ceil(final))

def rupiah(n: int) -> str:
    s = f"{n:,}".replace(",", ".")
    return f"Rp{s}"

# =========================
# UI Helpers
# =========================
def main_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📦 Layanan", callback_data="menu:services")],
        [InlineKeyboardButton("🛒 Buat Order", callback_data="menu:order")],
        [InlineKeyboardButton("🔎 Cek Status", callback_data="menu:status")],
        [InlineKeyboardButton("🧾 Riwayat", callback_data="menu:history")],
        [InlineKeyboardButton("💰 Saldo", callback_data="menu:balance")],
    ]
    return InlineKeyboardMarkup(kb)

def admin_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("➕ Add Saldo", callback_data="admin:addsaldo")],
        [InlineKeyboardButton("🧾 Export CSV", callback_data="admin:export")],
        [InlineKeyboardButton("👑 Set Seller", callback_data="admin:seller")],
    ]
    return InlineKeyboardMarkup(kb)

def short(s: str, n: int = 70) -> str:
    s = s or ""
    return s if len(s) <= n else (s[: n - 1] + "…")

# =========================
# Conversation State (simple)
# =========================
STATE: Dict[int, Dict[str, Any]] = {}  # per user_id

def set_state(user_id: int, key: str, value: Any):
    STATE.setdefault(user_id, {})
    STATE[user_id][key] = value

def get_state(user_id: int, key: str, default=None):
    return STATE.get(user_id, {}).get(key, default)

def clear_state(user_id: int):
    STATE.pop(user_id, None)

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    text = (
        f"**{BOT_NAME}**\n"
        "Pilih menu. Jangan panik, ini bukan ujian nasional.\n"
    )
    await update.message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    msg = (
        "**Command:**\n"
        "/start - menu\n"
        "/saldo - cek saldo\n"
        "/layanan <kata> - cari layanan\n"
        "/order - buat order (step)\n"
        "/status <order_id> - cek status\n"
        "/riwayat - order terakhir\n\n"
        "**Admin:**\n"
        "/setsaldo <user_id> <angka>\n"
        "/addsaldo <user_id> <angka>\n"
        "/setseller <user_id> <0/1>\n"
        "/exportcsv - export semua order\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    row = get_user(u.id)
    role = "SELLER" if int(row["is_seller"]) == 1 else "USER"
    await update.message.reply_text(
        f"Role: **{role}**\nSaldo: **{rupiah(int(row['balance']))}**",
        parse_mode=ParseMode.MARKDOWN,
    )

async def layanan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    q = " ".join(context.args).strip().lower()
    try:
        services = zayn_services()
    except Exception as e:
        await update.message.reply_text(f"Gagal ambil layanan: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    hits = []
    for svc in services:
        sid, name, rate, cat = pick_service_fields(svc)
        if not sid:
            continue
        hay = f"{sid} {name} {cat}".lower()
        if (not q) or (q in hay):
            hits.append((sid, name, rate, cat))
        if len(hits) >= 12:
            break

    if not hits:
        await update.message.reply_text("Ga ketemu. Coba kata kunci yang lebih waras.")
        return

    row = get_user(u.id)
    lines = []
    for sid, name, rate, cat in hits:
        # contoh harga utk qty 1000
        p = calc_price_idr(row, rate, 1000) if rate else 0
        lines.append(f"• `{sid}` — **{short(name, 42)}**\n  `{cat}` | rate/1k: `{rate}` | harga/1k: **{rupiah(p)}**")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    clear_state(u.id)
    set_state(u.id, "mode", "order")
    await update.message.reply_text(
        "Oke, bikin order.\nKirim **Service ID** dulu (contoh: `1234`).",
        parse_mode=ParseMode.MARKDOWN,
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    if not context.args:
        await update.message.reply_text("Pakai: `/status <order_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    oid = context.args[0].strip()

    try:
        data = zayn_status(oid)
    except Exception as e:
        await update.message.reply_text(f"Gagal cek status: `{e}`", parse_mode=ParseMode.MARKDOWN)
        return

    # normalisasi status
    status = data.get("status") or data.get("data", {}).get("status") or data.get("result", {}).get("status") or "UNKNOWN"
    remains = data.get("remains") or data.get("data", {}).get("remains") or data.get("result", {}).get("remains")
    startc = data.get("start_count") or data.get("data", {}).get("start_count") or data.get("result", {}).get("start_count")

    # update local order jika ada
    if get_order_by_provider_id(oid):
        update_order_status(oid, str(status))

    msg = f"Order: `{oid}`\nStatus: **{status}**"
    if startc is not None:
        msg += f"\nStart: `{startc}`"
    if remains is not None:
        msg += f"\nRemains: `{remains}`"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def riwayat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    rows = list_orders(u.id, limit=10)
    if not rows:
        await update.message.reply_text("Riwayat kosong. Dompet kamu masih damai.")
        return
    lines = []
    for r in rows:
        lines.append(
            f"• `{r['provider_order_id']}` | **{short(r['service_name'], 28)}** | qty `{r['quantity']}` | {rupiah(int(r['price']))}\n"
            f"  status: `{r['status']}`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ===== Admin commands =====
async def setsaldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Nope. Ini area admin.")
    if len(context.args) < 2:
        return await update.message.reply_text("Pakai: /setsaldo <user_id> <angka>")
    user_id = int(context.args[0])
    amount = int(context.args[1])
    ensure_user(user_id, "")
    set_balance(user_id, amount)
    await update.message.reply_text(f"OK. Saldo `{user_id}` = **{rupiah(amount)}**", parse_mode=ParseMode.MARKDOWN)

async def addsaldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Nope. Ini area admin.")
    if len(context.args) < 2:
        return await update.message.reply_text("Pakai: /addsaldo <user_id> <angka>")
    user_id = int(context.args[0])
    delta = int(context.args[1])
    ensure_user(user_id, "")
    add_balance(user_id, delta)
    row = get_user(user_id)
    await update.message.reply_text(
        f"OK. Saldo `{user_id}` sekarang **{rupiah(int(row['balance']))}**",
        parse_mode=ParseMode.MARKDOWN,
    )

async def setseller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Nope. Ini area admin.")
    if len(context.args) < 2:
        return await update.message.reply_text("Pakai: /setseller <user_id> <0/1>")
    user_id = int(context.args[0])
    val = int(context.args[1])
    ensure_user(user_id, "")
    set_seller(user_id, val == 1)
    await update.message.reply_text(f"OK. `{user_id}` seller = `{val}`", parse_mode=ParseMode.MARKDOWN)

async def exportcsv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Nope. Ini area admin.")

    with db() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

    if not rows:
        return await update.message.reply_text("Belum ada order buat di-export.")

    path = "orders_export.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "user_id", "provider_order_id", "service_id", "service_name", "link", "quantity", "price", "status", "created_at"])
        for r in rows:
            w.writerow([r["id"], r["user_id"], r["provider_order_id"], r["service_id"], r["service_name"], r["link"], r["quantity"], r["price"], r["status"], r["created_at"]])

    await update.message.reply_document(document=open(path, "rb"), filename=path, caption="Export CSV: orders")

# ===== Menu callbacks =====
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = q.from_user
    ensure_user(u.id, u.username or "")
    await q.answer()

    data = q.data or ""
    if data == "menu:services":
        await q.message.reply_text("Ketik: `/layanan <kata kunci>`\nContoh: `/layanan instagram`", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu:order":
        clear_state(u.id)
        set_state(u.id, "mode", "order")
        await q.message.reply_text("Gas. Kirim **Service ID** dulu.", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu:status":
        await q.message.reply_text("Ketik: `/status <order_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu:history":
        fake_update = Update(update.update_id, message=q.message)
        await riwayat(fake_update, context)
        return

    if data == "menu:balance":
        fake_update = Update(update.update_id, message=q.message)
        await saldo(fake_update, context)
        return

    if data == "admin:menu":
        if not is_admin(u.id):
            return await q.message.reply_text("Nope. Ini area admin.")
        await q.message.reply_text("Admin menu:", reply_markup=admin_menu())
        return

    if data.startswith("admin:"):
        if not is_admin(u.id):
            return await q.message.reply_text("Nope. Ini area admin.")
        await q.message.reply_text("Pakai command admin ya:\n/setsaldo, /addsaldo, /setseller, /exportcsv")
        return

# =========================
# ORDER FLOW (message step)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or "")
    if not cooldown_ok(u.id):
        return  # silent anti-spam

    mode = get_state(u.id, "mode", "")
    if mode != "order":
        return

    text = (update.message.text or "").strip()
    step = get_state(u.id, "step", "service")

    if step == "service":
        service_id = text
        try:
            services = zayn_services()
        except Exception as e:
            clear_state(u.id)
            return await update.message.reply_text(f"Gagal ambil layanan: `{e}`", parse_mode=ParseMode.MARKDOWN)

        picked = None
        for svc in services:
            sid, name, rate, cat = pick_service_fields(svc)
            if sid and sid == service_id:
                picked = (sid, name, rate, cat)
                break

        if not picked:
            return await update.message.reply_text("Service ID ga valid. Cek lagi via `/layanan <kata>`.", parse_mode=ParseMode.MARKDOWN)

        sid, name, rate, cat = picked
        set_state(u.id, "service_id", sid)
        set_state(u.id, "service_name", name)
        set_state(u.id, "service_rate", rate)
        set_state(u.id, "step", "link")

        await update.message.reply_text(
            f"OK service: **{short(name, 60)}**\nSekarang kirim **link/username** target.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if step == "link":
        link = text
        if len(link) < 4:
            return await update.message.reply_text("Link-nya kependekan. Kirim yang bener.")
        set_state(u.id, "link", link)
        set_state(u.id, "step", "qty")
        await update.message.reply_text("Oke. Sekarang kirim **quantity** (angka).", parse_mode=ParseMode.MARKDOWN)
        return

    if step == "qty":
        try:
            qty = int(text)
        except Exception:
            return await update.message.reply_text("Quantity harus angka.")
        if qty <= 0:
            return await update.message.reply_text("Quantity minimal 1.")

        row = get_user(u.id)
        rate = float(get_state(u.id, "service_rate", 0.0) or 0.0)
        price = calc_price_idr(row, rate, qty) if rate else 0

        # store qty & price
        set_state(u.id, "quantity", qty)
        set_state(u.id, "price", price)
        set_state(u.id, "step", "confirm")

        bal = int(row["balance"])
        svc_name = get_state(u.id, "service_name", "Unknown")

        msg = (
            "**Konfirmasi Order**\n"
            f"Service: **{short(str(svc_name), 60)}**\n"
            f"Link: `{short(get_state(u.id, 'link',''), 120)}`\n"
            f"Qty: `{qty}`\n"
            f"Harga: **{rupiah(price)}**\n"
            f"Saldo kamu: **{rupiah(bal)}**\n\n"
            "Balas: `YA` untuk lanjut, atau `BATAL`."
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    if step == "confirm":
        if text.upper() == "BATAL":
            clear_state(u.id)
            return await update.message.reply_text("Order dibatalin. Santuy.")
        if text.upper() != "YA":
            return await update.message.reply_text("Balas `YA` atau `BATAL`.")

        row = get_user(u.id)
        price = int(get_state(u.id, "price", 0))
        if int(row["balance"]) < price:
            clear_state(u.id)
            return await update.message.reply_text("Saldo kurang. Isi saldo dulu, jangan ngarep doang.")

        service_id = str(get_state(u.id, "service_id", ""))
        link = str(get_state(u.id, "link", ""))
        qty = int(get_state(u.id, "quantity", 0))
        svc_name = str(get_state(u.id, "service_name", "Unknown"))

        # call provider
        try:
            resp = zayn_add_order(service_id, link, qty)
        except Exception as e:
            clear_state(u.id)
            return await update.message.reply_text(f"Gagal buat order ke provider: `{e}`", parse_mode=ParseMode.MARKDOWN)

        # normalisasi order_id
        provider_oid = resp.get("order") or resp.get("order_id") or resp.get("data", {}).get("order")
        if not provider_oid:
            # beberapa API balikin {status:false,msg:...}
            clear_state(u.id)
            return await update.message.reply_text(
                f"Provider nggak ngasih order id.\nResponse: `{str(resp)[:220]}`",
                parse_mode=ParseMode.MARKDOWN,
            )

        # charge user
        add_balance(u.id, -price)

        # save order
        create_order(
            user_id=u.id,
            provider_order_id=str(provider_oid),
            service_id=service_id,
            service_name=svc_name,
            link=link,
            quantity=qty,
            price=price,
            status="PENDING",
        )

        clear_state(u.id)
        await update.message.reply_text(
            f"Done. Order masuk.\nOrder ID: `{provider_oid}`\nCek: `/status {provider_oid}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

# =========================
# Admin quick menu command
# =========================
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Nope. Ini area admin.")
    await update.message.reply_text("Admin menu:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Admin Menu", callback_data="admin:menu")]]))

# =========================
# Provider balance check (optional)
# =========================
async def provider_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        return await update.message.reply_text("Nope. Ini area admin.")
    try:
        prof = zayn_profile()
    except Exception as e:
        return await update.message.reply_text(f"Gagal ambil profile provider: `{e}`", parse_mode=ParseMode.MARKDOWN)

    # normalisasi field umum
    bal = prof.get("balance") or prof.get("saldo") or prof.get("data", {}).get("balance") or prof.get("data", {}).get("saldo")
    currency = prof.get("currency") or prof.get("data", {}).get("currency") or "IDR"
    await update.message.reply_text(
        f"Provider balance: **{bal} {currency}**\nRaw: `{str(prof)[:220]}`",
        parse_mode=ParseMode.MARKDOWN,
    )

# =========================
# MAIN
# =========================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("layanan", layanan))
    app.add_handler(CommandHandler("order", order_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("riwayat", riwayat))

    app.add_handler(CommandHandler("admin", admin))

    # admin commands
    app.add_handler(CommandHandler("setsaldo", setsaldo))
    app.add_handler(CommandHandler("addsaldo", addsaldo))
    app.add_handler(CommandHandler("setseller", setseller_cmd))
    app.add_handler(CommandHandler("exportcsv", exportcsv))
    app.add_handler(CommandHandler("providerbalance", provider_balance))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

if __name__ == "__main__":
    init_db()
    app = build_app()
    log.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
