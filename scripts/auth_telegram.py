"""One-time Telegram auth script. Run this interactively to create the session file."""
from dotenv import load_dotenv
import os
import asyncio
from telethon import TelegramClient

load_dotenv()

api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
phone = os.getenv("TELEGRAM_PHONE", "")
session = os.getenv("TELEGRAM_SESSION", "harvest_session")

async def main():
    client = TelegramClient(session, api_id, api_hash)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"Logged in as @{me.username or me.first_name} (ID: {me.id})")
    print(f"Session saved to {session}.session")
    await client.disconnect()

asyncio.run(main())
