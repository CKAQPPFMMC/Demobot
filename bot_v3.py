import asyncio
import os
import random
import re
import sqlite3
import time

from cryptography.fernet import Fernet
from pyrogram import Client, filters, idle
from pyrogram.errors import (FloodWait, PasswordHashInvalid, PhoneCodeExpired,
                             PhoneCodeInvalid, PhoneNumberInvalid,
                             SessionPasswordNeeded, UserAlreadyParticipant)
from pyrogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream

# ---------------- CONFIG (environment variables) ----------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ENC_KEY = os.getenv("ENC_KEY", "")
MAX_ACCOUNTS = int(os.getenv("MAX_ACCOUNTS", "100"))   # ek user max koyta account add korte pare
OWNER_SESSION = "test1"
SILENCE = "silence.mp3"
DEFAULT_COUNTS = "1,5,10,30,50"

fernet = Fernet(ENC_KEY.encode())
bot = Client("admin_bot", API_ID, API_HASH, bot_token=BOT_TOKEN)

# ---------------- DATABASE (bot.db) ----------------
db = sqlite3.connect("bot.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS accounts(
    name TEXT PRIMARY KEY, owner_id INTEGER, phone TEXT, string TEXT, added_at INTEGER)""")
db.execute("CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, expires INTEGER, max_acc INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
db.commit()

accounts = {}   # name -> {"app","calls","status","tag","owner"}
targets = {}    # user_id -> {"chat_id","invite","username","title"}
joined = {}     # user_id -> [account names in call]
selected = {}   # user_id -> chosen join count
state = {}      # user_id -> {"step": ...}
JOIN_DELAY = (2, 3)   # prottek account join er majhe 2-3 sec random delay
joining = set()       # je user er join cholche


# ---------------- HELPERS ----------------
def is_admin(uid):
    return uid == ADMIN_ID


def get_sub(uid):
    row = db.execute("SELECT expires, max_acc FROM subs WHERE user_id=?", (uid,)).fetchone()
    return row if row and row[0] > time.time() else None


def can_use(uid):
    return is_admin(uid) or get_sub(uid) is not None


def max_join(uid):
    if is_admin(uid):
        return 10**6
    sub = get_sub(uid)
    return sub[1] if sub else 0


def days_left(exp):
    return max(0, int((exp - time.time()) // 86400))


def get_counts():
    row = db.execute("SELECT value FROM settings WHERE key='counts'").fetchone()
    raw = row[0] if row else DEFAULT_COUNTS
    return [int(x) for x in raw.split(",")]


def user_accounts(uid):
    return [n for n, a in accounts.items() if a["owner"] == uid]


def count_saved(uid):
    return db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?", (uid,)).fetchone()[0]


def parse_link(text):
    t = text.strip()
    m = re.search(r"t\.me/(\+[\w-]+|joinchat/[\w-]+)", t)
    if m:
        return {"invite": "https://t.me/" + m.group(1), "username": None}
    m = re.search(r"t\.me/([A-Za-z]\w{3,})", t) or re.match(r"@?([A-Za-z]\w{3,})$", t)
    if m and m.group(1).lower() != "joinchat":
        return {"invite": None, "username": m.group(1)}
    return None


async def safe_answer(cb, *a, **k):
    try:
        await cb.answer(*a, **k)
    except Exception:
        pass


async def start_account(name, owner, string=None):
    if string is None:
        app = Client(name, API_ID, API_HASH)
    else:
        app = Client(name, API_ID, API_HASH, session_string=string, in_memory=True)
    calls = PyTgCalls(app)
    await app.start()
    await calls.start()
    me = await app.get_me()
    accounts[name] = {"app": app, "calls": calls, "status": "Left", "owner": owner,
                      "tag": f"@{me.username}" if me.username else (me.first_name or str(me.id))}


async def stop_account(name):
    acc = accounts.pop(name, None)
    if acc:
        try:
            await acc["app"].stop()
        except Exception:
            pass


async def load_all():
    try:
        await start_account(OWNER_SESSION, ADMIN_ID)
    except Exception as e:
        print(f"owner account offline: {e}")
    for name, owner, string in db.execute("SELECT name, owner_id, string FROM accounts").fetchall():
        try:
            await start_account(name, owner, fernet.decrypt(string.encode()).decode())
        except Exception as e:
            print(f"{name} offline: {e}")


async def drop_state(uid):
    st = state.pop(uid, None)
    if st and st.get("client"):
        try:
            await st["client"].disconnect()
        except Exception:
            pass


# ---------------- KEYBOARDS ----------------
def menu_kb(uid):
    rows = []
    if targets.get(uid):
        sel = selected.get(uid, 1)
        btns = [Btn(("✅ " if c == sel else "") + str(c), callback_data=f"c:{c}") for c in get_counts()]
        rows += [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([Btn("✏️ Custom", callback_data="c:custom")])
        rows.append([Btn("JOIN CALL", callback_data="join"), Btn("LEAVE CALL", callback_data="leave")])
    rows.append([Btn("🔗 Live Call Link", callback_data="u:target")])
    rows.append([Btn("➕ Add Account", callback_data="u:add"), Btn("📱 My Accounts", callback_data="u:accs")])
    rows.append([Btn("ℹ️ Subscription", callback_data="u:sub")])
    if is_admin(uid):
        rows.append([Btn("🛠 Admin Panel", callback_data="adm:home")])
    return Markup(rows)


def admin_kb():
    return Markup([
        [Btn("📋 All Accounts", callback_data="adm:accounts"), Btn("📊 Stats", callback_data="adm:stats")],
        [Btn("👤 Add Subscription", callback_data="adm:addsub"), Btn("📜 Subscriptions", callback_data="adm:subs")],
        [Btn("🔢 Edit Count Buttons", callback_data="adm:counts")],
        [Btn("🏠 User Menu", callback_data="u:home")],
    ])


def back(data="u:home"):
    return Markup([[Btn("⬅️ Back", callback_data=data)]])


def cancel_kb():
    return Markup([[Btn("✖️ Cancel", callback_data="u:cancel")]])


def stats_text():
    saved = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    users = db.execute("SELECT COUNT(DISTINCT owner_id) FROM accounts").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM subs WHERE expires>?", (time.time(),)).fetchone()[0]
    in_call = sum(len(v) for v in joined.values())
    return (f"🛠 Admin Panel\n\nTotal accounts: {saved + 1} (owner 1 + users {saved})\n"
            f"Online: {len(accounts)}\nUsers with accounts: {users}\n"
            f"Active subscriptions: {active}\nIn call now: {in_call}\n"
            f"Count buttons: {', '.join(map(str, get_counts()))}")


def status_text(uid):
    t = targets.get(uid)
    names = user_accounts(uid)
    in_call = len([n for n in joined.get(uid, []) if n in accounts])
    return (f"🔗 Live call: {t['title'] if t else 'not set'}\n"
            f"📱 Accounts: {len(names)}\n"
            f"👥 Selected: {selected.get(uid, 1)}\n"
            f"🎧 In call: {in_call}")


# ---------------- /start /admin ----------------
@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(_, msg):
    uid = msg.from_user.id
    await drop_state(uid)
    sub = "✅ Subscription active" if can_use(uid) else "❌ Subscription nei (account add free, call join e subscription lagbe)"
    await msg.reply(f"Welcome!\n{sub}\n\n" + status_text(uid), reply_markup=menu_kb(uid))


@bot.on_message(filters.command("admin") & filters.private & filters.user(ADMIN_ID))
async def admin_cmd(_, msg):
    await drop_state(msg.from_user.id)
    await msg.reply(stats_text(), reply_markup=admin_kb())


# ---------------- TEXT HANDLER ----------------
@bot.on_message(filters.private & filters.text & ~filters.command(["start", "admin"]))
async def on_text(_, msg):
    uid = msg.from_user.id
    text = msg.text.strip()
    st = state.get(uid)
    if not st:
        return await msg.reply("/start din.")
    step = st["step"]

    # ---- add account: phone ----
    if step == "phone":
        phone = text.replace(" ", "")
        client = Client(f"login_{uid}", API_ID, API_HASH, in_memory=True)
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except PhoneNumberInvalid:
            await client.disconnect()
            return await msg.reply("❌ Number thik na. Country code soho din (+8801XXXXXXXXX).",
                                   reply_markup=cancel_kb())
        except FloodWait as e:
            await client.disconnect()
            return await msg.reply(f"⏳ {e.value} second pore try korun.", reply_markup=cancel_kb())
        except Exception as e:
            await client.disconnect()
            return await msg.reply(f"❌ Error: {e}", reply_markup=cancel_kb())
        st.update(step="code", client=client, phone=phone, hash=sent.phone_code_hash)
        return await msg.reply("📩 Telegram e code gese. Spaces diye pathan (jemon: 1 2 3 4 5).",
                               reply_markup=cancel_kb())

    # ---- add account: OTP ----
    if step == "code":
        code = re.sub(r"\D", "", text)
        try:
            await st["client"].sign_in(st["phone"], st["hash"], code)
        except SessionPasswordNeeded:
            st["step"] = "password"
            return await msg.reply("🔐 2-Step Verification password pathan.", reply_markup=cancel_kb())
        except PhoneCodeInvalid:
            return await msg.reply("❌ Code vul. Abar pathan.", reply_markup=cancel_kb())
        except PhoneCodeExpired:
            await drop_state(uid)
            return await msg.reply("⌛ Code expire. Abar Add Account korun.", reply_markup=menu_kb(uid))
        except Exception as e:
            await drop_state(uid)
            return await msg.reply(f"❌ Error: {e}", reply_markup=menu_kb(uid))
        return await finish_login(msg, uid)

    # ---- add account: 2FA ----
    if step == "password":
        try:
            await st["client"].check_password(text)
        except PasswordHashInvalid:
            return await msg.reply("❌ Password vul. Abar pathan.", reply_markup=cancel_kb())
        except Exception as e:
            await drop_state(uid)
            return await msg.reply(f"❌ Error: {e}", reply_markup=menu_kb(uid))
        try:
            await msg.delete()
        except Exception:
            pass
        return await finish_login(msg, uid)

    # ---- live call link ----
    if step == "target":
        parsed = parse_link(text)
        names = user_accounts(uid)
        if not parsed:
            return await msg.reply("❌ Link thik na. Public @username ba t.me/+invite link din.",
                                   reply_markup=cancel_kb())
        if not names:
            state.pop(uid, None)
            return await msg.reply("Age ekta account add korun.", reply_markup=menu_kb(uid))
        app = accounts[names[0]]["app"]
        try:
            try:
                chat = await app.join_chat(parsed["invite"] or parsed["username"])
            except UserAlreadyParticipant:
                chat = await app.get_chat(parsed["invite"] or parsed["username"])
        except Exception as e:
            return await msg.reply(f"❌ Chat paoa jayni: {e}", reply_markup=cancel_kb())
        targets[uid] = {"chat_id": chat.id, "title": chat.title, **parsed}
        state.pop(uid, None)
        return await msg.reply(f"✅ Link set: {chat.title}\nEkhon koyta account join hobe select korun.",
                               reply_markup=menu_kb(uid))

    # ---- custom count ----
    if step == "custom":
        if not text.isdigit() or int(text) < 1:
            return await msg.reply("Ekta number din (jemon 25).", reply_markup=cancel_kb())
        selected[uid] = int(text)
        state.pop(uid, None)
        return await msg.reply(status_text(uid), reply_markup=menu_kb(uid))

    # ---- admin: add subscription ----
    if step == "addsub" and is_admin(uid):
        parts = text.split()
        if len(parts) < 2 or not all(p.isdigit() for p in parts):
            return await msg.reply("Format: user_id days max_join\nExample: 12345678 30 50",
                                   reply_markup=back("adm:home"))
        user_id, days = int(parts[0]), int(parts[1])
        mx = int(parts[2]) if len(parts) > 2 else 50
        db.execute("INSERT OR REPLACE INTO subs VALUES(?,?,?)",
                   (user_id, int(time.time() + days * 86400), mx))
        db.commit()
        state.pop(uid, None)
        return await msg.reply(f"✅ {user_id}: {days} din, ekbare max {mx} account join.",
                               reply_markup=admin_kb())

    # ---- admin: edit count buttons ----
    if step == "counts" and is_admin(uid):
        nums = [x.strip() for x in text.split(",")]
        if not nums or len(nums) > 9 or not all(x.isdigit() and int(x) > 0 for x in nums):
            return await msg.reply("Format: 1,5,10,30,50 (max 9 ta number)", reply_markup=back("adm:home"))
        clean = sorted({int(x) for x in nums})
        db.execute("INSERT OR REPLACE INTO settings VALUES('counts', ?)", (",".join(map(str, clean)),))
        db.commit()
        state.pop(uid, None)
        return await msg.reply(f"✅ Buttons updated: {', '.join(map(str, clean))}", reply_markup=admin_kb())


async def finish_login(msg, uid):
    st = state[uid]
    client, phone = st["client"], st["phone"]
    string = await client.export_session_string()
    await drop_state(uid)
    name = f"u{uid}_{int(time.time())}"
    try:
        await start_account(name, uid, string)
    except Exception as e:
        return await msg.reply(f"❌ Account start hoy nai: {e}", reply_markup=menu_kb(uid))
    db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)",
               (name, uid, phone, fernet.encrypt(string.encode()).decode(), int(time.time())))
    db.commit()
    await msg.reply(f"✅ Account added: {accounts[name]['tag']}\nTotal: {count_saved(uid)}",
                    reply_markup=menu_kb(uid))


# ---------------- USER MENU CALLBACKS ----------------
@bot.on_callback_query(filters.regex(r"^u:"))
async def user_menu_cb(_, cb):
    uid = cb.from_user.id
    parts = cb.data.split(":")
    action = parts[1]

    if action == "cancel":
        await drop_state(uid)
        action = "home"

    if action == "home":
        await cb.message.edit_text(status_text(uid), reply_markup=menu_kb(uid))

    elif action == "add":
        if count_saved(uid) >= MAX_ACCOUNTS:
            return await cb.answer("Account limit sesh.", show_alert=True)
        await drop_state(uid)
        state[uid] = {"step": "phone"}
        await cb.message.edit_text(
            "➕ Account add korar steps:\n"
            "1) Phone number (country code soho)\n"
            "2) Telegram e asha login code\n"
            "3) 2-Step password (thakle)\n\n"
            "⚠️ Ei login diye bot apnar account e access pabe. Shudhu nijer account add korun.\n\n"
            "📱 Ekhon phone number pathan (+8801XXXXXXXXX):", reply_markup=cancel_kb())

    elif action == "accs":
        rows = db.execute("SELECT name, phone FROM accounts WHERE owner_id=?", (uid,)).fetchall()
        if not rows:
            return await cb.message.edit_text("Kono account nei.", reply_markup=back())
        kb = []
        for name, phone in rows:
            tag = accounts[name]["tag"] if name in accounts else phone
            kb.append([Btn(f"🗑 Remove {tag}", callback_data=f"u:delacc:{name}")])
        kb.append([Btn("⬅️ Back", callback_data="u:home")])
        await cb.message.edit_text(f"📱 Apnar accounts: {len(rows)}", reply_markup=Markup(kb))

    elif action == "delacc":
        name = parts[2]
        row = db.execute("SELECT owner_id FROM accounts WHERE name=?", (name,)).fetchone()
        if row and row[0] == uid:
            await stop_account(name)
            db.execute("DELETE FROM accounts WHERE name=?", (name,))
            db.commit()
            if name in joined.get(uid, []):
                joined[uid].remove(name)
            await safe_answer(cb, "Removed")
        cb.data = "u:accs"
        return await user_menu_cb(_, cb)

    elif action == "target":
        if not user_accounts(uid):
            return await cb.answer("Age ekta account add korun.", show_alert=True)
        state[uid] = {"step": "target"}
        await cb.message.edit_text("🔗 Live call er link pathan (public @username ba t.me/+invite):",
                                   reply_markup=cancel_kb())

    elif action == "sub":
        if is_admin(uid):
            txt = "👑 Admin: unlimited"
        elif get_sub(uid):
            exp, mx = get_sub(uid)
            txt = f"✅ {days_left(exp)} din baki\nEkbare max join: {mx}"
        else:
            txt = "❌ Subscription nei.\nAccount add free, kintu call join korte subscription lagbe."
        await cb.message.edit_text(txt, reply_markup=back())

    await safe_answer(cb)


# ---------------- COUNT SELECT ----------------
@bot.on_callback_query(filters.regex(r"^c:"))
async def count_cb(_, cb):
    uid = cb.from_user.id
    val = cb.data.split(":")[1]
    if val == "custom":
        state[uid] = {"step": "custom"}
        await cb.message.edit_text("✏️ Koyta account join korate chan? Number pathan:", reply_markup=cancel_kb())
    else:
        selected[uid] = int(val)
        await cb.message.edit_text(status_text(uid), reply_markup=menu_kb(uid))
    await safe_answer(cb)


# ---------------- JOIN / LEAVE CALL ----------------
async def join_one(uid, name, tgt):
    acc = accounts.get(name)
    if not acc:
        return False
    for attempt in range(2):
        try:
            try:
                await acc["app"].join_chat(tgt["invite"] or tgt["username"])
            except UserAlreadyParticipant:
                pass
            await acc["calls"].play(tgt["chat_id"], MediaStream(SILENCE))
            acc["status"] = "Joined"
            joined.setdefault(uid, []).append(name)
            return True
        except FloodWait as e:
            print(f"{name} FloodWait {e.value}s")
            if attempt == 0 and e.value <= 60:
                await asyncio.sleep(e.value + 1)
                continue
            acc["status"] = "Failed"
            return False
        except Exception as e:
            print(f"{name} join error: {e}")
            acc["status"] = "Failed"
            return False
    return False


@bot.on_callback_query(filters.regex(r"^(join|leave)$"))
async def call_cb(_, cb):
    uid = cb.from_user.id
    tgt = targets.get(uid)
    if not tgt:
        return await cb.answer("Age live call link din.", show_alert=True)

    if cb.data == "join":
        if not can_use(uid):
            return await cb.answer("❌ Call join korte subscription lagbe.", show_alert=True)
        names = user_accounts(uid)
        if not names:
            return await cb.answer("Age account add korun.", show_alert=True)
        want = min(selected.get(uid, 1), max_join(uid))
        have = joined.setdefault(uid, [])
        pool = [n for n in names if n not in have]
        to_join = pool[:max(0, want - len(have))]
        if not to_join:
            return await cb.answer("Already joined ba aro account nei.", show_alert=True)
        if uid in joining:
            return await cb.answer("⏳ Join cholche, opekkha korun.", show_alert=True)
        await safe_answer(cb, "⏳ Joining...")
        est = int(len(to_join) * sum(JOIN_DELAY) / 2)
        await cb.message.edit_text(f"⏳ {len(to_join)} ta account join hocche (~{est} sec)...")
        joining.add(uid)
        ok = 0
        try:
            for i, n in enumerate(to_join, 1):
                if await join_one(uid, n, tgt):
                    ok += 1
                if i < len(to_join):
                    if i % 5 == 0:
                        try:
                            await cb.message.edit_text(f"⏳ {i}/{len(to_join)} joined...")
                        except Exception:
                            pass
                    await asyncio.sleep(random.uniform(*JOIN_DELAY))
        finally:
            joining.discard(uid)
        note = ""
        if selected.get(uid, 1) > len(names):
            note += f"\n⚠️ Apnar kache shudhu {len(names)} ta account ache."
        if selected.get(uid, 1) > max_join(uid):
            note += f"\n⚠️ Apnar plan e max {max_join(uid)} ta."
        await cb.message.edit_text(f"✅ {ok}/{len(to_join)} joined{note}\n\n" + status_text(uid),
                                   reply_markup=menu_kb(uid))
    else:
        if uid in joining:
            return await cb.answer("⏳ Join cholche, shesh hole leave korun.", show_alert=True)
        await safe_answer(cb, "Leaving...")
        for name in list(joined.get(uid, [])):
            acc = accounts.get(name)
            try:
                if acc:
                    await acc["calls"].leave_call(tgt["chat_id"])
                    acc["status"] = "Left"
            except Exception as e:
                print(f"{name} leave error: {e}")
        joined[uid] = []
        await cb.message.edit_text("👋 Sob account call theke leave koreche.\n\n" + status_text(uid),
                                   reply_markup=menu_kb(uid))


# ---------------- ADMIN CALLBACKS ----------------
@bot.on_callback_query(filters.regex(r"^adm:") & filters.user(ADMIN_ID))
async def admin_cb(_, cb):
    parts = cb.data.split(":")
    action = parts[1]
    uid = cb.from_user.id

    if action in ("home", "stats"):
        state.pop(uid, None)
        await cb.message.edit_text(stats_text(), reply_markup=admin_kb())

    elif action == "accounts":
        rows = db.execute("SELECT owner_id, name, phone FROM accounts ORDER BY owner_id").fetchall()
        lines = [f"Total: {len(rows) + 1} (owner 1 + users {len(rows)})\n"]
        kb = []
        for owner, name, phone in rows[:40]:
            tag = accounts[name]["tag"] if name in accounts else "offline"
            lines.append(f"user {owner} | {tag} | {phone}")
            kb.append([Btn(f"🗑 {tag} ({owner})", callback_data=f"adm:delacc:{name}")])
        if len(rows) > 40:
            lines.append(f"... aro {len(rows) - 40} ta")
        kb.append([Btn("⬅️ Back", callback_data="adm:home")])
        await cb.message.edit_text("\n".join(lines), reply_markup=Markup(kb))

    elif action == "delacc":
        name = parts[2]
        row = db.execute("SELECT owner_id FROM accounts WHERE name=?", (name,)).fetchone()
        if row:
            await stop_account(name)
            db.execute("DELETE FROM accounts WHERE name=?", (name,))
            db.commit()
            if name in joined.get(row[0], []):
                joined[row[0]].remove(name)
            await safe_answer(cb, "Removed")
        cb.data = "adm:accounts"
        return await admin_cb(_, cb)

    elif action == "addsub":
        state[uid] = {"step": "addsub"}
        await cb.message.edit_text("Format: user_id days max_join\nExample: 12345678 30 50",
                                   reply_markup=back("adm:home"))

    elif action == "counts":
        state[uid] = {"step": "counts"}
        await cb.message.edit_text(
            f"Ekhon: {', '.join(map(str, get_counts()))}\n\nNotun number comma diye pathan.\n"
            "Example: 1,5,10,30,50", reply_markup=back("adm:home"))

    elif action == "subs":
        rows = db.execute("SELECT user_id, expires, max_acc FROM subs ORDER BY expires DESC").fetchall()
        if not rows:
            return await cb.message.edit_text("Kono subscription nei.", reply_markup=back("adm:home"))
        lines, kb = [], []
        for user_id, exp, mx in rows[:40]:
            s = f"{days_left(exp)}d left" if exp > time.time() else "expired"
            lines.append(f"{user_id} - {s} - max join {mx}")
            kb.append([Btn(f"❌ Remove {user_id}", callback_data=f"adm:rmsub:{user_id}")])
        kb.append([Btn("⬅️ Back", callback_data="adm:home")])
        await cb.message.edit_text("\n".join(lines), reply_markup=Markup(kb))

    elif action == "rmsub":
        db.execute("DELETE FROM subs WHERE user_id=?", (int(parts[2]),))
        db.commit()
        await safe_answer(cb, "Removed")
        cb.data = "adm:subs"
        return await admin_cb(_, cb)

    await safe_answer(cb)


# ---------------- EXPIRY WATCHER ----------------
async def expiry_watcher():
    while True:
        await asyncio.sleep(60)
        for uid in list(joined):
            if joined[uid] and not can_use(uid):
                tgt = targets.get(uid)
                for name in joined[uid]:
                    try:
                        await accounts[name]["calls"].leave_call(tgt["chat_id"])
                        accounts[name]["status"] = "Left"
                    except Exception:
                        pass
                joined[uid] = []
                try:
                    await bot.send_message(uid, "⛔ Subscription expire. Accounts call theke leave koreche.")
                except Exception:
                    pass


async def main():
    await bot.start()
    await load_all()
    asyncio.create_task(expiry_watcher())
    print("Bot running")
    await idle()


if __name__ == "__main__":
    asyncio.run(main())
