import os
import asyncio
import logging
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
API_ID = int(os.getenv("API_ID") or "0")
API_HASH = (os.getenv("API_HASH") or "").strip()

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("BOT_TOKEN / API_ID / API_HASH belum kebaca.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

app = Client(
    "test_smm_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.command("ping") & (filters.private | filters.group))
async def ping(_, m: Message):
    print("INCOMING /ping FROM:", m.chat.id, m.from_user.id)
    await m.reply("pong")

@app.on_message(filters.command("start") & (filters.private | filters.group))
async def start(_, m: Message):
    print("INCOMING /start FROM:", m.chat.id, m.from_user.id)
    await m.reply("start ok")

@app.on_message(filters.text & filters.private)
async def echo(_, m: Message):
    print("INCOMING TEXT:", m.text)
    await m.reply("kebaca: " + m.text[:100])

async def main():
    await app.start()
    me = await app.get_me()
    print("LOGGED IN AS:", f"@{me.username}", me.id)
    print("READY. coba /ping di PM & grup.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
