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
# Configuration
# ---------------------------------------------------------
load_dotenv()

try:
    API_ID = int(os.getenv("API_ID", 0))
except ValueError:
    API_ID = 0

API_HASH = os.getenv("API_HASH", "").strip()
STRING_SESSION = os.getenv("STRING_SESSION", "").strip()
PORT = int(os.getenv("PORT", 5000))

if not API_ID or not API_HASH or not STRING_SESSION:
    print("❌ ERROR: Missing credentials!")
    sys.exit(1)

# ---------------------------------------------------------
# Logging & Patch
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("KnightX")
logging.getLogger("telethon").setLevel(logging.WARNING)

try:
    from telethon._updates import messagebox
    _orig_messagebox_trace = messagebox.MessageBox._trace
    def _safe_messagebox_trace(self, text, *args):
        try:
            if isinstance(getattr(self, "date", None), int):
                self.date = datetime.utcfromtimestamp(self.date)
        except Exception: pass
        try: return _orig_messagebox_trace(self, text, *args)
        except Exception: return
    messagebox.MessageBox._trace = _safe_messagebox_trace
except Exception:
    pass

# ---------------------------------------------------------
# Clients & Globals
# ---------------------------------------------------------
KnightX = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
knight_call = PyTgCalls(KnightX)

BATCH_SIZE = 100
SLEEP_BETWEEN_BATCHES = 0.4
active_tasks = {}
raid_tasks = {}
music_queue = []
current_playing = None
_RAND_SENTENCES = ["GM!", "Hello!", "Ping!", "Stay safe ✌️", "Test.", "Knight X!"]

# ---------------------------------------------------------
# Admin Web Panel (Cookie Uploader)
# ---------------------------------------------------------
async def handle_home(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>KnightX Admin Panel</title>
        <style>
            body {{ font-family: Arial; padding: 20px; background: #121212; color: #fff; }}
            textarea {{ width: 100%; max-width: 600px; height: 200px; background: #222; color: #0f0; border: 1px solid #444; }}
            input, button {{ padding: 10px; margin-top: 10px; }}
            button {{ background: #007bff; color: white; border: none; cursor: pointer; }}
        </style>
    </head>
    <body>
        <h2>🍪 Upload YouTube Cookies</h2>
        <p>Paste the contents of your <b>youtube.com_cookies.txt</b> here to bypass bot protection.</p>
        <form method="POST" action="/update_cookies">
            <label>Security Passcode (Your API_ID):</label><br>
            <input type="password" name="passcode" required><br><br>
            
            <label>Cookie Data (Netscape Format):</label><br>
            <textarea name="cookies" required></textarea><br>
            
            <button type="submit">Save Cookies to Bot</button>
        </form>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

async def handle_update_cookies(request):
    data = await request.post()
    passcode = data.get("passcode", "")
    cookies = data.get("cookies", "")
    
    if str(passcode) != str(API_ID):
        return web.Response(text="❌ Unauthorized: Incorrect Passcode.", status=403)
        
    try:
        with open("cookies.txt", "w") as f:
            f.write(cookies)
        return web.Response(text="✅ Success! Cookies saved. Go to Telegram and test the .play command.")
    except Exception as e:
        return web.Response(text=f"❌ Failed to write file: {e}", status=500)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_home)
    app.router.add_post('/update_cookies', handle_update_cookies)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Admin Web Panel running on port {PORT}")

# ---------------------------------------------------------
# Music Player Logic
# ---------------------------------------------------------
def get_yt_stream(query):
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        # Force yt-dlp to natively return only the first search result
        'default_search': 'ytsearch1', 
        'extractor_args': {
            'youtube': {
                'client': ['android', 'tv', 'ios']
            }
        }
    }
    
    if os.path.exists("cookies.txt"):
        ydl_opts['cookiefile'] = "cookies.txt"
        logger.info("✅ Injecting cookies.txt")

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
        except Exception as e:
            # Safe fallback to YouTube Music without using invalid URL schemes
            if not query.startswith("http"):
                logger.warning(f"Standard search failed: {e}. Falling back to YT Music...")
                info = ydl.extract_info(f"ytmsearch:{query}", download=False)
            else:
                raise e
                
        # Dig into the search results to pull the video data
        if 'entries' in info:
            if len(info['entries']) == 0:
                raise Exception("No results found for that song.")
            info = info['entries'][0]
            
        return info.get('title', 'Unknown Title'), info.get('url')

@KnightX.on(events.NewMessage(pattern=r"^\.play\s+(.+)$", outgoing=True))
async def cmd_play(event):
    global current_playing
    query = event.pattern_match.group(1).strip()
    status = await event.respond(f"🔍 Searching: `{query}`...")
    
    try:
        title, stream_url = get_yt_stream(query)
        if current_playing:
            music_queue.append({'title': title, 'url': stream_url, 'chat_id': event.chat_id})
            await status.edit(f"📝 Added to queue: **{title}**")
        else:
            try:
                await knight_call.play(event.chat_id, MediaStream(stream_url))
                current_playing = {'title': title, 'chat_id': event.chat_id}
                await status.edit(f"▶️ Now playing: **{title}**")
            except NoActiveGroupCall:
                await status.edit("❌ Please start an active voice chat first.")
    except Exception as e:
        await status.edit(f"❌ Failed: `{e}`")

@KnightX.on(events.NewMessage(pattern=r"^\.skip$", outgoing=True))
async def cmd_skip(event):
    global current_playing
    if not current_playing: return await event.respond("❌ Nothing playing.")
    if len(music_queue) > 0:
        next_song = music_queue.pop(0)
        await knight_call.play(current_playing['chat_id'], MediaStream(next_song['url']))
        current_playing = next_song
        await event.respond(f"⏭ Skipped! Now playing: **{next_song['title']}**")
    else:
        await knight_call.leave_call(current_playing['chat_id'])
        current_playing = None
        await event.respond("⏭ Queue empty, left call.")

@KnightX.on(events.NewMessage(pattern=r"^\.stop$", outgoing=True))
async def cmd_stop(event):
    global current_playing
    chat_id = event.chat_id
    if task := active_tasks.pop(chat_id, None): task.cancel()
    if rtask := raid_tasks.pop(chat_id, None): rtask.cancel()
    if current_playing and current_playing.get('chat_id') == chat_id:
        await knight_call.leave_call(chat_id)
        music_queue.clear()
        current_playing = None
    msg = await event.respond("🛑 Stopped active tasks and music.")
    await asyncio.sleep(2)
    await msg.delete()
    await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.queue$", outgoing=True))
async def cmd_queue(event):
    if not current_playing: return await event.respond("📭 Queue is empty.")
    text = f"**▶️ Now Playing:** {current_playing['title']}\n\n**📜 Queue:**\n"
    text += "\n".join([f"{i}. {s['title']}" for i, s in enumerate(music_queue, 1)]) if music_queue else "Empty."
    await event.respond(text)

# ---------------------------------------------------------
# Safe Math Evaluator
# ---------------------------------------------------------
_ALLOWED_NAMES = {k: v for k, v in math.__dict__.items() if not k.startswith("__")}
_ALLOWED_NAMES.update({"abs": abs, "round": round, "pow": pow, "min": min, "max": max})

class _MathEvalVisitor(ast.NodeVisitor):
    def generic_visit(self, node):
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Constant, ast.Call, ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub, ast.UAdd, ast.FloorDiv, ast.Tuple, ast.List)
        if not isinstance(node, allowed): raise ValueError("Disallowed expression")
        super().generic_visit(node)
    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_NAMES: raise ValueError("Disallowed func")
        for arg in node.args: self.visit(arg)
    def visit_Name(self, node):
        if node.id not in _ALLOWED_NAMES: raise ValueError("Disallowed name")

def safe_eval(expr: str):
    parsed = ast.parse(expr.strip(), mode="eval")
    _MathEvalVisitor().visit(parsed)
    return eval(compile(parsed, "<math>", "eval"), {"__builtins__": {}}, _ALLOWED_NAMES)

def extract_math(text: str):
    if not text: return None
    expr = text.strip()
    if re.fullmatch(r"[\d\s\.\+\-\*\/\^\%\(\),eEpiPIrtginsoclqurta-]+", expr): return expr.replace("^", "**")
    if m := re.search(r"`([^`]+)`", text): return m.group(1).replace("^", "**")
    if m := re.search(r"([0-9\.\s\+\-\*\/\^\%\(\)eEpiPIrtginsoclqurta,-]{3,})", text): return m.group(1).strip().replace("^", "**")
    return None

# ---------------------------------------------------------
# Core Userbot Commands
# ---------------------------------------------------------
async def delete_messages_in_chat(dialog, limit=None):
    deleted = 0
    try:
        async for msg in KnightX.iter_messages(dialog, from_user='me'):
            batch = [msg.id]
            async for m in KnightX.iter_messages(dialog, from_user='me', offset_id=msg.id, limit=BATCH_SIZE - 1):
                batch.append(m.id)
                if len(batch) >= BATCH_SIZE: break
            if not batch: continue
            try:
                await KnightX.delete_messages(dialog, batch)
                deleted += len(batch)
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                await KnightX.delete_messages(dialog, batch)
                deleted += len(batch)
            await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
            if limit and deleted >= limit: break
    except Exception: pass
    return deleted

@KnightX.on(events.NewMessage(pattern=r"^\.purge(?:\s+(all))?$", outgoing=True))
async def cmd_purge(event):
    is_all = bool(event.pattern_match.group(1))
    info_msg = await event.respond("🗡️ Preparing purge...")
    if not is_all:
        deleted = await delete_messages_in_chat(await event.get_input_chat())
        await info_msg.edit(f"✅ Deleted ~{deleted} message(s).")
    else:
        total = 0
        async for dialog in KnightX.iter_dialogs(): total += await delete_messages_in_chat(dialog.entity)
        await info_msg.edit(f"✅ Deleted ~{total} message(s) globally.")
    await asyncio.sleep(2)
    await info_msg.delete()
    await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.calculate(?:\s+(.+))?$", outgoing=True))
async def cmd_calculate(event):
    expr = event.pattern_match.group(1) or extract_math(getattr(await event.get_reply_message(), "text", None))
    if not expr: return await event.respond("❌ Provide a math expression.")
    try: await event.respond(f"🧮 `{expr}` = `{safe_eval(expr)}`")
    except Exception as e: await event.respond(f"❌ Error: {e}")

@KnightX.on(events.NewMessage(pattern=r"^\.ping$", outgoing=True))
async def cmd_ping(event):
    t0 = time.perf_counter()
    msg = await event.respond("🏓 Pong...")
    await msg.edit(f"🏓 Pong! `{int((time.perf_counter() - t0) * 1000)} ms`")

@KnightX.on(events.NewMessage(pattern=r"^\.help$", outgoing=True))
async def cmd_help(event):
    await event.respond("🛡 **KnightX Userbot Online**\n\nCommands: `.play`, `.skip`, `.stop`, `.queue`, `.purge`, `.calculate`, `.ping`.")

# ---------------------------------------------------------
# Application Entry Point
# ---------------------------------------------------------
async def main():
    await start_web_server()
    await KnightX.start()
    await knight_call.start()

    me = await KnightX.get_me()
    logger.info("KnightX Online as: %s", me.username or me.first_name)

    try:
        await KnightX.run_until_disconnected()
    finally:
        await knight_call.stop()
        await KnightX.disconnect()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Stopped.")
