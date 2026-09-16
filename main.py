import os
import re
import ast
import sys
import math
import time
import random
import asyncio
import logging
import tempfile
from datetime import datetime, timedelta, timezone

from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.tl.functions.channels import CreateChannelRequest

from yt_dlp import YoutubeDL
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls.exceptions import NoActiveGroupCall

# ---------------------------------------------------------
# Configuration (Loaded from .env or Render Environment)
# ---------------------------------------------------------
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID", 0))
except ValueError:
    API_ID = 0

API_HASH = os.getenv("API_HASH", "").strip()
STRING_SESSION = os.getenv("STRING_SESSION", "").strip()
PORT = int(os.getenv("PORT", 3568))  # Render sets this automatically

if not API_ID or not API_HASH or not STRING_SESSION:
    print("❌ ERROR: Missing credentials!")
    print("Please configure API_ID, API_HASH, and STRING_SESSION in your environment.")
    sys.exit(1)

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("KnightX")

logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ===== TELETHON v1.30.0 CRASH FIX (date isoformat bug) =====
try:
    from telethon._updates import messagebox
    _orig_messagebox_trace = messagebox.MessageBox._trace

    def _safe_messagebox_trace(self, text, *args):
        try:
            if isinstance(getattr(self, "date", None), int):
                self.date = datetime.utcfromtimestamp(self.date)
        except Exception:
            pass
        try:
            return _orig_messagebox_trace(self, text, *args)
        except Exception:
            return

    messagebox.MessageBox._trace = _safe_messagebox_trace
except Exception:
    pass
# ===== END PATCH =====

# ---------------------------------------------------------
# Clients Initialization
# ---------------------------------------------------------
KnightX = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
knight_call = PyTgCalls(KnightX)

# Constants & Globals
BATCH_SIZE = 100
SLEEP_BETWEEN_BATCHES = 0.4

active_tasks = {}   # chat_id -> asyncio.Task (lyrics/spam)
raid_tasks = {}     # chat_id -> asyncio.Task (background raid)

music_queue = []    # List of dictionaries for queued songs
current_playing = None # Track currently playing song and chat

_RAND_SENTENCES = [
    "GM everyone!", "Hello!", "Ping!", "Stay safe ✌️", "Automation test.",
    "Have a great day!", "Knight X Power here.", "Quick reminder!"
]

# ---------------------------------------------------------
# Web Server (Required for Render Health Checks)
# ---------------------------------------------------------
async def handle_home(request):
    return web.Response(text="KnightX Userbot & Web Server is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_home)
    app.router.add_get('/health', handle_home)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Local web server started on port {PORT}")

# ---------------------------------------------------------
# Music Player Helpers
# ---------------------------------------------------------
def get_yt_stream(query):
    """Uses yt-dlp to extract the raw audio stream URL and handles cookies."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch'
    }
    
    # Check for cookies file to bypass YouTube bot restrictions
    for path in ["cookies.txt", "/etc/secrets/cookies.txt"]:
        if os.path.exists(path):
            ydl_opts['cookiefile'] = path
            break

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        return info['title'], info['url']

# ---------------------------------------------------------
# General Helpers
# ---------------------------------------------------------
async def safe_delete_message(entity, message_id):
    try:
        await KnightX.delete_messages(entity, message_id)
    except Exception:
        pass

# Safe math evaluator logic
_ALLOWED_NAMES = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
_ALLOWED_NAMES.update({"abs": abs, "round": round, "pow": pow, "min": min, "max": max})

class _MathEvalVisitor(ast.NodeVisitor):
    def generic_visit(self, node):
        allowed_nodes = (
            ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant, ast.Call,
            ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
            ast.Mod, ast.USub, ast.UAdd, ast.FloorDiv, ast.Tuple, ast.List
        )
        if not isinstance(node, allowed_nodes):
            raise ValueError(f"Disallowed expression: {type(node).__name__}")
        super().generic_visit(node)
    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function names allowed")
        if node.func.id not in _ALLOWED_NAMES:
            raise ValueError(f"Function '{node.func.id}' not allowed")
        for arg in node.args:
            self.visit(arg)
    def visit_Name(self, node):
        if node.id not in _ALLOWED_NAMES:
            raise ValueError(f"Name '{node.id}' is not allowed")

def safe_eval(expr: str):
    expr = expr.strip()
    parsed = ast.parse(expr, mode="eval")
    _MathEvalVisitor().visit(parsed)
    code = compile(parsed, "<math>", "eval")
    return eval(code, {"__builtins__": {}}, _ALLOWED_NAMES)

def extract_first_math_expression(text: str):
    if not text:
        return None
    expr = text.strip()
    if re.fullmatch(r"[\d\s\.\+\-\*\/\^\%\(\),eEpiPIrtginsoclqurta-]+", expr):
        return expr.replace("^", "**")
    m = re.search(r"`([^`]+)`", text)
    if m:
        candidate = m.group(1).replace("^", "**")
        if re.search(r"[0-9]", candidate):
            return candidate
    m = re.search(r"([0-9\.\s\+\-\*\/\^\%\(\)eEpiPIrtginsoclqurta,-]{3,})", text)
    if m:
        return m.group(1).strip().replace("^", "**")
    return None

async def delete_messages_in_chat(dialog, limit=None):
    deleted = 0
    try:
        async for msg in KnightX.iter_messages(dialog, from_user='me'):
            batch = [msg.id]
            async for m in KnightX.iter_messages(dialog, from_user='me', offset_id=msg.id, limit=BATCH_SIZE - 1):
                batch.append(m.id)
                if len(batch) >= BATCH_SIZE:
                    break
            if not batch:
                continue
            try:
                await KnightX.delete_messages(dialog, batch)
                deleted += len(batch)
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                await KnightX.delete_messages(dialog, batch)
                deleted += len(batch)
            await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
            if limit and deleted >= limit:
                break
    except Exception:
        pass
    return deleted

# ---------------------------------------------------------
# Commands: Music Player
# ---------------------------------------------------------
@KnightX.on(events.NewMessage(pattern=r"^\.play\s+(.+)$", outgoing=True))
async def cmd_play(event):
    global current_playing
    query = event.pattern_match.group(1).strip()
    status = await event.respond(f"🔍 Searching YouTube for: `{query}`...")
    
    try:
        title, stream_url = get_yt_stream(query)
        chat_id = event.chat_id

        if current_playing:
            music_queue.append({'title': title, 'url': stream_url, 'chat_id': chat_id})
            await status.edit(f"📝 Added to queue: **{title}**")
        else:
            try:
                await knight_call.play(chat_id, MediaStream(stream_url))
                current_playing = {'title': title, 'chat_id': chat_id}
                await status.edit(f"▶️ Now playing: **{title}**")
            except NoActiveGroupCall:
                await status.edit("❌ You must start a Voice Chat in this group first!")
    except Exception as e:
        logger.exception("Music play error:")
        await status.edit(f"❌ Error playing song: `{e}`")

@KnightX.on(events.NewMessage(pattern=r"^\.skip$", outgoing=True))
async def cmd_skip(event):
    global current_playing
    if not current_playing:
        return await event.respond("❌ Nothing is playing right now.")
    
    if len(music_queue) > 0:
        next_song = music_queue.pop(0)
        await knight_call.play(current_playing['chat_id'], MediaStream(next_song['url']))
        current_playing = next_song
        await event.respond(f"⏭ Skipped! Now playing: **{next_song['title']}**")
    else:
        await knight_call.leave_call(current_playing['chat_id'])
        current_playing = None
        await event.respond("⏭ Skipped! Queue is empty, leaving voice chat.")

@KnightX.on(events.NewMessage(pattern=r"^\.queue$", outgoing=True))
async def cmd_queue(event):
    if not current_playing:
        return await event.respond("📭 The queue is currently empty.")
    
    text = f"**▶️ Currently Playing:** {current_playing['title']}\n\n**📜 Queue:**\n"
    if not music_queue:
        text += "Empty."
    else:
        for i, song in enumerate(music_queue, 1):
            text += f"{i}. {song['title']}\n"
    
    await event.respond(text)

# ---------------------------------------------------------
# Commands: Automation Tasks
# ---------------------------------------------------------
async def _lyrics_worker(chat_id, lines):
    try:
        for line in lines:
            if not line:
                continue
            await KnightX.send_message(chat_id, line)
            await asyncio.sleep(random.uniform(3.0, 4.0))
    except asyncio.CancelledError:
        return
    except errors.FloodWaitError as fw:
        await asyncio.sleep(fw.seconds + 1)
    finally:
        active_tasks.pop(chat_id, None)

@KnightX.on(events.NewMessage(pattern=r"^\.lyrics(?:\s+(.+))?$", outgoing=True))
async def cmd_lyrics(event):
    provided = event.pattern_match.group(1)
    text = provided.strip() if provided and provided.strip() else getattr(await event.get_reply_message(), "text", None)
    
    if not text:
        info = await event.respond("❌ Usage: .lyrics <paragraph> — or reply to a message with .lyrics")
        await asyncio.sleep(3)
        return await event.delete()
        
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        sentences = re.split(r'(?<=[\.\!\?])\s+', text)
        if len(sentences) <= 1 and len(text) > 120:
            sentences = re.split(r',\s+', text)
        lines = [s.strip() for s in sentences if s.strip()]
        
    chat_id = event.chat_id
    if old := active_tasks.get(chat_id):
        old.cancel()
        
    await event.delete()
    active_tasks[chat_id] = asyncio.create_task(_lyrics_worker(chat_id, lines))

async def _raid_worker_once(chat_id, times, message_text, delay_between=0.8):
    try:
        for _ in range(int(times)):
            await KnightX.send_message(chat_id, message_text)
            await asyncio.sleep(delay_between)
    except asyncio.CancelledError:
        return
    except errors.FloodWaitError as fw:
        await asyncio.sleep(fw.seconds + 1)
    finally:
        active_tasks.pop(chat_id, None)

@KnightX.on(events.NewMessage(pattern=r"^\.spam\s+(\d+)\s+(.+)", outgoing=True))
async def cmd_spam(event):
    times = int(event.pattern_match.group(1))
    body = event.pattern_match.group(2).strip()
    chat_id = event.chat_id
    
    if old := active_tasks.get(chat_id):
        old.cancel()
        
    await event.delete()
    active_tasks[chat_id] = asyncio.create_task(_raid_worker_once(chat_id, times, body))

async def _background_raid(chat_id, min_delay=3.0, max_delay=7.0):
    try:
        while True:
            await KnightX.send_message(chat_id, random.choice(_RAND_SENTENCES))
            await asyncio.sleep(random.uniform(min_delay, max_delay))
    except asyncio.CancelledError:
        return
    except errors.FloodWaitError as fw:
        await asyncio.sleep(fw.seconds + 1)
    finally:
        raid_tasks.pop(chat_id, None)

@KnightX.on(events.NewMessage(pattern=r"^\.raid\s+(on|off)$", outgoing=True))
async def cmd_raid_onoff(event):
    mode = event.pattern_match.group(1).lower()
    chat_id = event.chat_id
    
    if mode == "on":
        if chat_id in raid_tasks:
            notice = await event.respond("⚠ Raid already running.")
            await asyncio.sleep(2)
            await notice.delete()
            return
        raid_tasks[chat_id] = asyncio.create_task(_background_raid(chat_id))
        await event.respond("🚀 Raid ON.")
    else:
        if t := raid_tasks.get(chat_id):
            t.cancel()
            raid_tasks.pop(chat_id, None)
            done = await event.respond("🛑 Raid OFF.")
            await asyncio.sleep(1.5)
            await done.delete()
        else:
            await event.respond("ℹ Raid is not running.")
    await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.stop$", outgoing=True))
async def cmd_stop(event):
    global current_playing
    chat_id = event.chat_id
    stopped = False
    
    if task := active_tasks.pop(chat_id, None):
        task.cancel()
        stopped = True
        
    if rtask := raid_tasks.pop(chat_id, None):
        rtask.cancel()
        stopped = True

    if current_playing and current_playing.get('chat_id') == chat_id:
        await knight_call.leave_call(chat_id)
        music_queue.clear()
        current_playing = None
        stopped = True

    msg = await event.respond("🛑 Stopped active tasks and music." if stopped else "ℹ️ Nothing running.")
    await asyncio.sleep(2)
    try:
        await msg.delete()
        await event.delete()
    except Exception:
        pass

# ---------------------------------------------------------
# Commands: Utilities
# ---------------------------------------------------------
@KnightX.on(events.NewMessage(pattern=r"^\.purge(?:\s+(all))?$", outgoing=True))
async def cmd_purge(event):
    is_all = bool(event.pattern_match.group(1))
    info_msg = await event.respond("🗡️ Preparing purge...")
    try:
        if not is_all:
            await info_msg.edit("🔎 Scanning this chat...")
            deleted = await delete_messages_in_chat(await event.get_input_chat())
            await info_msg.edit(f"✅ Deleted ~{deleted} message(s) here.")
        else:
            await info_msg.edit("⚠️ Purging all. This takes time...")
            total = 0
            async for dialog in KnightX.iter_dialogs():
                total += await delete_messages_in_chat(dialog.entity)
            await info_msg.edit(f"✅ Deleted ~{total} message(s) globally.")
    finally:
        await asyncio.sleep(2)
        await info_msg.delete()
        await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.timedel\s+(\d+)\s+(.+)", outgoing=True))
async def cmd_timedel(event):
    try:
        delay = int(event.pattern_match.group(1))
        sent = await event.respond(event.pattern_match.group(2))
        await event.delete()
        await asyncio.sleep(delay)
        await safe_delete_message(sent.chat_id, sent.id)
    except Exception:
        pass

@KnightX.on(events.NewMessage(pattern=r"^\.tagadmins$", outgoing=True))
async def cmd_tagadmins(event):
    chat = await event.get_input_chat()
    checking = await event.respond("👀 Scanning admins...")
    try:
        admins = []
        async for user in KnightX.iter_participants(chat, filter=ChannelParticipantsAdmins):
            if not user.bot:
                admins.append(f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})")
        if admins:
            await checking.edit(" **Hello all**\n\n" + " ".join(admins))
        else:
            await checking.edit("⚠️ No admins found.")
    except Exception as e:
        await checking.edit(f"❌ Error: {e}")
    finally:
        await asyncio.sleep(3)
        await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.info(?:\s+(.+))?$", outgoing=True))
async def cmd_info(event):
    target = event.pattern_match.group(1)
    user = None
    try:
        if target:
            target = target.strip()
            user = await KnightX.get_entity(int(target) if target.isdigit() else target)
        elif reply := await event.get_reply_message():
            user = await reply.get_sender()
        else:
            return await event.respond("❌ Provide @username or ID.")
            
        bot_str = "Yes 🤖" if getattr(user, "bot", False) else "No 👤"
        name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
        
        await event.respond(
            f"🔍 **User Info**\n—————————————\n"
            f"👤 **Name:** {name or 'Unknown'}\n🆔 **ID:** `{user.id}`\n"
            f"🏷 **Username:** @{getattr(user, 'username', 'None')}\n"
            f"🤖 **Bot:** {bot_str}\n—————————————"
        )
    except Exception as e:
        await event.respond(f"❌ Error: {e}")
    finally:
        await asyncio.sleep(2)
        await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.whois(?:\s+(.+))?$", outgoing=True))
async def cmd_whois(event):
    target = event.pattern_match.group(1)
    user = None
    try:
        if target:
            target = target.strip()
            user = await KnightX.get_entity(int(target) if target.isdigit() else target)
        elif reply := await event.get_reply_message():
            user = await reply.get_sender()
        else:
            return await event.respond("❌ Provide @username or ID.")

        bot_str = "Yes 🤖" if getattr(user, "bot", False) else "No 👤"
        name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
        phone = getattr(user, "phone", "Hidden")
        about = getattr(user, "about", None)

        info = f"🔎 **Whois**\n—————————————\n👤 **Name:** {name or 'Unknown'}\n" \
               f"🆔 **ID:** `{user.id}`\n🏷 **Username:** @{getattr(user, 'username', 'None')}\n" \
               f"📞 **Phone:** {phone}\n🤖 **Bot:** {bot_str}\n"
        if about: info += f"💬 **Bio:** {about}\n"
        info += "—————————————"

        photo_path = await KnightX.download_profile_photo(user, file=tempfile.gettempdir())
        if photo_path:
            await KnightX.send_file(event.chat_id, photo_path, caption=info)
            os.remove(photo_path)
        else:
            await event.respond(info)
    except Exception as e:
        await event.respond(f"❌ Error: {e}")
    finally:
        await asyncio.sleep(2)
        await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.ping$", outgoing=True))
async def cmd_ping(event):
    t0 = time.perf_counter()
    msg = await event.respond("🏓 Pong...")
    latency = int((time.perf_counter() - t0) * 1000)
    await msg.edit(f"🏓 Pong! `{latency} ms`")
    await asyncio.sleep(2)
    await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.calculate(?:\s+(.+))?$", outgoing=True))
async def cmd_calculate(event):
    expr = event.pattern_match.group(1) or extract_first_math_expression(getattr(await event.get_reply_message(), "text", None))
    if not expr:
        return await event.respond("❌ Provide a math expression.")
    try:
        await event.respond(f"🧮 Result:\n`{expr}` = `{safe_eval(expr)}`")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

@KnightX.on(events.NewMessage(pattern=r"^\.t&d$", outgoing=True))
async def cmd_t_and_d(event):
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    await event.respond(f"🕒 Current date & time (Asia/Kolkata):\n{now.strftime('%Y-%m-%d %H:%M:%S')}")
    await asyncio.sleep(2)
    await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.scrape\s+(.+)$", outgoing=True))
async def cmd_scrape(event):
    target = event.pattern_match.group(1).strip()
    status_msg = await event.respond(f"⏳ Preparing scrape for `{target}`...")
    try:
        source_entity = await KnightX.get_entity(int(target) if target.lstrip('-').isdigit() else target)
        title = getattr(source_entity, "title", str(target))

        created = await KnightX(CreateChannelRequest(title=f"Scrape ({title})", about=f"Scrape dump", megagroup=False))
        dest_entity = created.chats[0]
        await status_msg.edit("🚀 Scraping started...")

        count = 0
        batch = []
        async for msg in KnightX.iter_messages(source_entity, reverse=True):
            batch.append(msg.id)
            if len(batch) >= 100:
                await KnightX.forward_messages(dest_entity, batch, source_entity)
                count += len(batch)
                batch.clear()
                await asyncio.sleep(2)

        if batch:
            await KnightX.forward_messages(dest_entity, batch, source_entity)
            count += len(batch)

        await status_msg.edit(f"✅ Scraping Completed! Forwarded `{count}` messages to new channel.")
    except Exception as e:
        await status_msg.edit(f"❌ Scrape failed: `{e}`")

@KnightX.on(events.NewMessage(pattern=r"^\.help$", outgoing=True))
async def cmd_help(event):
    help_text = """
🛡 **Knight X Power — Help Menu** 🛡

🎵 **Music Player**
• `.play <song name>` — Play or queue a song from YouTube
• `.skip` — Skip to the next song in queue
• `.stop` — Stop music and all background tasks
• `.queue` — Check the current music queue

🧹 **Purge / Delete**
• `.purge` / `.purge all` — Delete messages here / everywhere
• `.timedel <sec> <text>` — Send message, auto-delete after sec

🤖 **Automation**
• `.lyrics <text>` — Send text line-by-line
• `.spam <count> <text>` — Repeat message <count> times
• `.raid on` / `.raid off` — Toggle background random messages

📂 **Utility**
• `.scrape <channel>` — Clone a channel to a private dump
• `.tagadmins` — Tag group admins
• `.info` / `.whois` — Get user information
• `.calculate <expr>` — Safe math calculator
• `.ping` / `.t&d` — Network ping / Date & Time

⚡ *Online & Running*
"""
    await event.respond(help_text)
    await asyncio.sleep(2)
    await event.delete()

# ---------------------------------------------------------
# Startup Sequence
# ---------------------------------------------------------
async def main():
    await start_web_server()
    await KnightX.start()
    await knight_call.start()

    me = await KnightX.get_me()
    logger.info("Userbot started as %s (%s)", me.username or me.first_name, me.id)
    print("🛡️ Userbot Online as:", me.username or me.first_name)

    try:
        await KnightX.run_until_disconnected()
    except asyncio.CancelledError:
        logger.info("Runner cancelled.")
    except Exception as e:
        logger.exception("Run loop error: %s", e)
    finally:
        for t in list(active_tasks.values()) + list(raid_tasks.values()):
            try: t.cancel()
            except Exception: pass
        await knight_call.stop()
        await KnightX.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user.")
