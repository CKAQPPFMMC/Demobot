import asyncio
import re
import os

from pyrogram import Client, filters, idle
from pyrogram.errors import UserAlreadyParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream

API_ID = 30954067                 # my.telegram.org
API_HASH = "14b365d5ed5bc10b0379a4c9a31be351"
BOT_TOKEN = "8994535308:AAFfyuYAMQOfnnTzjcSAonJyYEMADUZmzPQ"   # @BotFather
ADMIN_ID = 8815360015           # @userinfobot
SESSIONS = ["test1"]  # শুধু আপনার নিজের ২টা test account
SILENCE = "silence.mp3"

SESSIONS = SESSIONS[:2]
bot = Client("admin_bot", API_ID, API_HASH, bot_token=BOT_TOKEN)
accounts = {}
target = {"chat_id": None, "username": None}


async def start_accounts():
    for name in SESSIONS:
        acc = {"app": None, "calls": None, "status": "Offline"}
        accounts[name] = acc
        try:
            app = Client(name, API_ID, API_HASH)
            calls = PyTgCalls(app)
            await app.start()
            await calls.start()
            acc.update(app=app, calls=calls, status="Left")
        except Exception as e:
            print(f"{name} offline: {e}")


def keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("JOIN NOW", callback_data="join"),
        InlineKeyboardButton("LEAVE", callback_data="leave"),
    ]])


def report():
    lines = [f"{n}: {a['status']}" for n, a in accounts.items()]
    return "Status:\n" + "\n".join(lines)


@bot.on_message(filters.user(ADMIN_ID) & filters.text)
async def on_text(_, msg):
    text = msg.text.strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]+)", text) or re.match(r"@?([A-Za-z0-9_]+)$", text)
    if not m:
        return await msg.reply("Send your public channel @username or t.me link.")
    online = next((a for a in accounts.values() if a["app"]), None)
    if not online:
        return await msg.reply("All accounts offline.\n" + report())
    try:
        chat = await online["app"].get_chat(m.group(1))
    except Exception as e:
        return await msg.reply(f"Could not resolve chat: {e}")
    target["chat_id"] = chat.id
    target["username"] = m.group(1)
    await msg.reply(f"Target set: {chat.title}\n" + report(), reply_markup=keyboard())


@bot.on_callback_query(filters.user(ADMIN_ID))
async def on_button(_, cb):
    if not target["chat_id"]:
        return await cb.answer("Set a target first", show_alert=True)
    for acc in accounts.values():
        if not acc["calls"]:
            acc["status"] = "Offline"
            continue
        try:
            if cb.data == "join":
                try:
                    await acc["app"].join_chat(target["username"])
                except UserAlreadyParticipant:
                    pass
                await acc["calls"].play(target["chat_id"], MediaStream(SILENCE))
                acc["status"] = "Joined"
            else:
                await acc["calls"].leave_call(target["chat_id"])
                acc["status"] = "Left"
        except Exception as e:
            print(f"error: {e}")
            acc["status"] = "Failed"
    await cb.message.edit_text(report(), reply_markup=keyboard())
    await cb.answer()


async def main():
    await start_accounts()
    await bot.start()
    print("Bot running")
    await idle()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
