import os
import re
import json
import time
import math
import sqlite3
import asyncio
from typing import Any, Dict, Optional, Tuple, List

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("ENV BOT_TOKEN wajib diisi.")

API_KEY = os.getenv("ZAYN_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("ENV ZAYN_API_KEY wajib diisi (API Key dari panel ZaynFlazz).")

# API URL ZaynFlazz (dari dokumentasi)
ZAYN_API_URL = os.getenv("ZAYN_API_URL", "https://zaynflazz.com/api/sosial-media").strip()
ZAYN_PROFILE_URL = os.getenv("ZAYN_PROFILE_URL", "https://zaynflazz.com/api/profile").strip()

# Admin bot (bisa banyak, pisah koma)
ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(","):
    if x.isdigit():
        ADMIN_IDS.add(int(x))

# Markup default (%)
DEFAULT_MARKUP_PERCENT = float(os.getenv("DEFAULT_MARKUP_PERCENT", "10"))  # seller
NONSELLER_MARKUP_PERCENT = float(os.getenv("NONSELLER_MARKUP_PERCENT", "15"))  # non-seller

# Harga panel biasanya "per 1000"
PRICE_PER_1000 = os.getenv("PRICE_PER_1000", "1").strip() == "1"

# Anti-spam
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "2"))

# DB
DB_PATH = os.getenv("DB_PATH", "smm_bot.db")

# Cache layanan
SERVICES_CACHE_TTL = int(os.getenv("SERVICES_CACHE_TTL", "300"))  # detik
_services_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])

# State order per user
ORDER_STATE: Dict[int, Dict[str, Any]] = {}

# =========================
# DB helpers
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance REAL NOT NULL DEFAULT 0,
        is_seller INTEGER NOT NULL DEFAULT 0,
        markup_percent REAL,
        last_ts INTEGER NOT NULL DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        provider TEXT NOT NULL,
        provider_order_id TEXT,
        service_id TEXT NOT NULL,
        service_name TEXT,
        target TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'created',
        created_at INTEGER NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_user(user_id: int) -> Dict[str, Any]:
    ensure_user(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row)

def set_user_balance(user_id: int, balance: float):
    ensure_user(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, user_id))
    conn.commit()
    conn.close()

def add_user_balance(user_id: int, amount: float):
    u = get_user(user_id)
    set_user_balance(user_id, float(u["balance"]) + float(amount))

def set_user_seller(user_id: int, is_seller: bool):
    ensure_user(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_seller=? WHERE user_id=?", (1 if is_seller else 0, user_id))
    conn.commit()
    conn.close()

def set_user_markup(user_id: int, percent: Optional[float]):
    ensure_user(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET markup_percent=? WHERE user_id=?", (percent,))
    conn.commit()
    conn.close()

def can_pass_cooldown(user_id: int) -> bool:
    ensure_user(user_id)
    u = get_user(user_id)
    now = int(time.time())
    if now - int(u["last_ts"]) < COOLDOWN_SECONDS:
        return False
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_ts=? WHERE user_id=?", (now, user_id))
    conn.commit()
    conn.close()
    return True

def save_order(user_id: int, provider: str, provider_order_id: Optional[str],
               service_id: str, service_name: str, target: str, quantity: int,
               price: float, status: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO orders (user_id, provider, provider_order_id, service_id, service_name,
                        target, quantity, price, status, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, provider, provider_order_id, service_id, service_name,
          target, quantity, price, status, int(time.time())))
    conn.commit()
    oid = cur.lastrowid
    conn.close()
    return oid

def update_order_status(local_id: int, status: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status=? WHERE id=?", (status, local_id))
    conn.commit()
    conn.close()

# =========================
# ZaynFlazz API
# =========================
async def zayn_post(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=payload) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except Exception:
                return {"status": False, "raw": text, "http": resp.status}

async def zayn_services() -> List[Dict[str, Any]]:
    global _services_cache
    ts, data = _services_cache
    now = time.time()
    if data and (now - ts) < SERVICES_CACHE_TTL:
        return data

    # action "layanan" (umum di dokumentasi panel sejenis)
    res = await zayn_post(ZAYN_API_URL, {"api_key": API_KEY, "action": "layanan"})
    # Respon umumnya { "data": [ {sid, kategori, layanan, min, max, harga, catatan}, ... ] }
    services = []
    if isinstance(res, dict) and "data" in res:
        d = res["data"]
        if isinstance(d, list):
            services = d
        elif isinstance(d, dict):
            # kalau API ngirim 1-by-1 dict, bungkus jadi list
            services = [d]

    _services_cache = (now, services)
    return services

async def zayn_add_order(service_id: str, target: str, quantity: int) -> Dict[str, Any]:
    # action "pemesanan" + param layanan/target/jumlah (umum)
    return await zayn_post(ZAYN_API_URL, {
        "api_key": API_KEY,
        "action": "pemesanan",
        "layanan": str(service_id),
        "target": target,
        "jumlah": str(quantity),
    })

async def zayn_status(order_id: str) -> Dict[str, Any]:
    return await zayn_post(ZAYN_API_URL, {
        "api_key": API_KEY,
        "action": "status",
        "id": str(order_id),
    })

async def zayn_profile() -> Dict[str, Any]:
    return await zayn_post(ZAYN_PROFILE_URL, {
        "api_key": API_KEY,
        "action": "profile",
    })

# =========================
# Pricing
# =========================
def parse_price_idr(s: Any) -> float:
    # "10.200" -> 10200
    if s is None:
        return 0.0
    txt = str(s).strip()
    txt = txt.replace(",", ".")
    # buang non digit/dot
    txt = re.sub(r"[^0-9.]", "", txt)
    if not txt:
        return 0.0
    # kalau format 10.200 (indo) itu 10200, bukan 10.2
    parts = txt.split(".")
    if len(parts) > 1:
        # interpret as thousand separator if last group length == 3
        if len(parts[-1]) == 3:
            txt2 = "".join(parts)
            return float(txt2)
    return float(txt)

def compute_sell_price(panel_price: float, qty: int, markup_percent: float) -> float:
    base = panel_price
    if PRICE_PER_1000:
        base = (panel_price / 1000.0) * float(qty)
    else:
        base = panel_price * float(qty)
    sell = base * (1.0 + markup_percent / 100.0)
    # pembulatan “biar cakep”
    return math.ceil(sell)

def get_user_markup(user: Dict[str, Any]) -> float:
    if user.get("markup_percent") is not None:
        return float(user["markup_percent"])
    if int(user.get("is_seller", 0)) == 1:
        return DEFAULT_MARKUP_PERCENT
    return NONSELLER_MARKUP_PERCENT

# =========================
# Bot
# =========================
app = Client(
    "zayn_smm_bot",
    bot_token=BOT_TOKEN,
    # api_id/api_hash tidak wajib untuk bot token-only di pyrogram
)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, m: Message):
    ensure_user(m.from_user.id)
    await m.reply(
        "SMM Bot siap jualan.\n\n"
        "Perintah:\n"
        "• /services [keyword]\n"
        "• /order\n"
        "• /status <id>\n"
        "• /saldo\n\n"
        "Tip: /services tiktok buat cari layanan cepat."
    )

@app.on_message(filters.command("saldo") & filters.private)
async def saldo_cmd(_, m: Message):
    u = get_user(m.from_user.id)
    await m.reply(f"Saldo kamu: Rp{int(float(u['balance'])):,}".replace(",", "."))

@app.on_message(filters.command("services") & filters.private)
async def services_cmd(_, m: Message):
    if not can_pass_cooldown(m.from_user.id):
        return await m.reply("Santai dulu. Jangan spam command, gue bukan mesin fotokopi.")
    kw = ""
    if len(m.command) >= 2:
        kw = " ".join(m.command[1:]).strip().lower()

    services = await zayn_services()
    if not services:
        return await m.reply("Layanan kosong / API lagi ngambek. Coba lagi bentar.")

    # filter keyword
    if kw:
        services = [s for s in services if kw in str(s.get("kategori", "")).lower() or kw in str(s.get("layanan", "")).lower()]

    # tampilkan 10 teratas
    out = []
    for s in services[:10]:
        sid = s.get("sid") or s.get("id") or s.get("service")
        cat = str(s.get("kategori", "")).strip()
        name = str(s.get("layanan", "")).strip()
        minq = s.get("min", "-")
        maxq = s.get("max", "-")
        price = parse_price_idr(s.get("harga", 0))
        out.append(
            f"• SID `{sid}`\n"
            f"  {cat}\n"
            f"  {name}\n"
            f"  Min/Max: {minq}/{maxq} | Harga panel: Rp{int(price):,}".replace(",", ".")
        )

    msg = "Top layanan:\n\n" + "\n\n".join(out)
    msg += "\n\nLanjut order: /order"
    await m.reply(msg)

@app.on_message(filters.command("order") & filters.private)
async def order_cmd(_, m: Message):
    if not can_pass_cooldown(m.from_user.id):
        return await m.reply("Pelan-pelan. Rate limit dulu ya.")
    ORDER_STATE[m.from_user.id] = {"step": "sid"}
    await m.reply("Kirim *SID* layanan yang mau kamu order.\nContoh: `1234`", quote=True)

@app.on_message(filters.private & filters.text)
async def order_flow(_, m: Message):
    uid = m.from_user.id
    if uid not in ORDER_STATE:
        return

    st = ORDER_STATE[uid]
    step = st.get("step")

    if step == "sid":
        sid = m.text.strip()
        if not re.fullmatch(r"[0-9]+", sid):
            return await m.reply("SID harus angka. Ulang lagi.")
        services = await zayn_services()
        svc = None
        for s in services:
            if str(s.get("sid") or s.get("id") or "") == sid:
                svc = s
                break
        if not svc:
            return await m.reply("SID gak ketemu. Cek via /services [keyword] dulu.")

        st["sid"] = sid
        st["svc"] = svc
        st["step"] = "target"
        await m.reply("Oke. Sekarang kirim *target* (link/username) sesuai layanan itu.")

    elif step == "target":
        target = m.text.strip()
        if len(target) < 3:
            return await m.reply("Target kependekan. Ulang.")
        st["target"] = target
        st["step"] = "qty"
        await m.reply("Kirim *jumlah/qty* (angka).")

    elif step == "qty":
        txt = m.text.strip().replace(".", "").replace(",", "")
        if not txt.isdigit():
            return await m.reply("Qty harus angka.")
        qty = int(txt)
        svc = st["svc"]
        minq = int(str(svc.get("min", "1")).replace(".", "").replace(",", "") or "1")
        maxq_raw = str(svc.get("max", "999999")).replace(".", "").replace(",", "")
        maxq = int(maxq_raw) if maxq_raw.isdigit() else 999999

        if qty < minq or qty > maxq:
            return await m.reply(f"Qty harus di range {minq} - {maxq}.")

        panel_price = parse_price_idr(svc.get("harga", 0))
        u = get_user(uid)
        markup = get_user_markup(u)
        sell_price = compute_sell_price(panel_price, qty, markup)

        st["qty"] = qty
        st["sell_price"] = sell_price
        st["step"] = "confirm"

        name = str(svc.get("layanan", "")).strip()
        await m.reply(
            "Konfirmasi order:\n\n"
            f"• SID: `{st['sid']}`\n"
            f"• Layanan: {name}\n"
            f"• Target: `{st['target']}`\n"
            f"• Qty: `{qty}`\n"
            f"• Total: *Rp{int(sell_price):,}*\n\n".replace(",", ".") +
            "Balas: `YES` untuk lanjut, atau `NO` buat batal."
        )

    elif step == "confirm":
        ans = m.text.strip().upper()
        if ans == "NO":
            ORDER_STATE.pop(uid, None)
            return await m.reply("Batal. Nggak jadi order, dompet aman.")
        if ans != "YES":
            return await m.reply("Balas `YES` atau `NO` aja. Jangan essay.")

        u = get_user(uid)
        bal = float(u["balance"])
        price = float(st["sell_price"])
        if bal < price:
            ORDER_STATE.pop(uid, None)
            return await m.reply(f"Saldo kurang. Saldo kamu Rp{int(bal):,}".replace(",", ".") + f", butuh Rp{int(price):,}".replace(",", "."))

        # Potong saldo dulu (biar anti kabur)
        set_user_balance(uid, bal - price)

        svc = st["svc"]
        sid = st["sid"]
        target = st["target"]
        qty = st["qty"]
        svc_name = str(svc.get("layanan", "")).strip()

        # Call API
        res = await zayn_add_order(sid, target, qty)

        # Respon sukses umumnya: {"data":{"id":"1119","start_count":"200"}} :contentReference[oaicite:2]{index=2}
        provider_order_id = None
        if isinstance(res, dict) and "data" in res and isinstance(res["data"], dict):
            provider_order_id = str(res["data"].get("id") or res["data"].get("order_id") or "")

        local_id = save_order(
            user_id=uid,
            provider="zaynflazz",
            provider_order_id=provider_order_id if provider_order_id else None,
            service_id=str(sid),
            service_name=svc_name,
            target=target,
            quantity=qty,
            price=price,
            status="submitted" if provider_order_id else "unknown",
        )

        ORDER_STATE.pop(uid, None)

        if provider_order_id:
            await m.reply(
                "Order masuk.\n\n"
                f"• Order ID (Zayn): `{provider_order_id}`\n"
                f"• Local ID: `{local_id}`\n\n"
                f"Cek: /status {provider_order_id}"
            )
        else:
            # kalau API error, balikin saldo biar adil
            add_user_balance(uid, price)
            update_order_status(local_id, "failed")
            await m.reply("Order gagal (API). Saldo dibalikin. Coba lagi nanti.")

@app.on_message(filters.command("status") & filters.private)
async def status_cmd(_, m: Message):
    if len(m.command) < 2:
        return await m.reply("Format: /status <id>")
    oid = m.command[1].strip()
    if not re.fullmatch(r"[0-9]+", oid):
        return await m.reply("ID harus angka.")

    res = await zayn_status(oid)
    # contoh sukses: {"data":{"id":"23","start_count":"123","status":"Success","remains":"0"}} :contentReference[oaicite:3]{index=3}
    if isinstance(res, dict) and isinstance(res.get("data"), dict):
        d = res["data"]
        await m.reply(
            "Status order:\n\n"
            f"• ID: `{d.get('id')}`\n"
            f"• Start: `{d.get('start_count')}`\n"
            f"• Status: *{d.get('status')}*\n"
            f"• Remains: `{d.get('remains')}`"
        )
    else:
        await m.reply("Gagal cek status. ID salah atau API lagi ngambek.")

# =========================
# Admin
# =========================
@app.on_message(filters.command("addsaldo") & filters.private)
async def addsaldo_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return
    if len(m.command) < 3:
        return await m.reply("Format: /addsaldo <user_id> <amount>")
    uid = m.command[1].strip()
    amt = m.command[2].strip().replace(".", "").replace(",", "")
    if not uid.isdigit() or not re.fullmatch(r"[0-9]+", amt):
        return await m.reply("Param salah.")
    add_user_balance(int(uid), float(amt))
    u = get_user(int(uid))
    await m.reply(f"OK. Saldo user {uid} sekarang Rp{int(float(u['balance'])):,}".replace(",", "."))

@app.on_message(filters.command("setseller") & filters.private)
async def setseller_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return
    if len(m.command) < 3:
        return await m.reply("Format: /setseller <user_id> <on|off>")
    uid = m.command[1].strip()
    flag = m.command[2].strip().lower()
    if not uid.isdigit() or flag not in ("on", "off"):
        return await m.reply("Param salah.")
    set_user_seller(int(uid), flag == "on")
    await m.reply(f"OK. seller={flag} untuk user {uid}")

@app.on_message(filters.command("setmarkup") & filters.private)
async def setmarkup_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return
    if len(m.command) < 3:
        return await m.reply("Format: /setmarkup <user_id> <percent|default>")
    uid = m.command[1].strip()
    val = m.command[2].strip().lower()
    if not uid.isdigit():
        return await m.reply("User ID salah.")
    if val == "default":
        set_user_markup(int(uid), None)
        return await m.reply("OK. Markup user balik ke default.")
    try:
        pct = float(val)
        set_user_markup(int(uid), pct)
        await m.reply(f"OK. Markup user {uid} = {pct}%")
    except:
        await m.reply("Markup harus angka atau 'default'.")

async def main():
    init_db()
    await app.start()
    print("ZaynFlazz SMM bot running...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
