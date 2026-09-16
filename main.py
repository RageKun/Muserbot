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
PORT = int(os.getenv("PORT", 3568))

if not API_ID or not API_HASH or not STRING_SESSION:
    print("❌ ERROR: Missing credentials in .env or Environment Variables!")
    sys.exit(1)

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("KnightX")

logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ---------------------------------------------------------
# Telegram & Voice Clients
# ---------------------------------------------------------
KnightX = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
knight_call = PyTgCalls(KnightX)

BATCH_SIZE = 100
SLEEP_BETWEEN_BATCHES = 0.4

active_tasks = {}
raid_tasks = {}
music_queue = []
current_playing = None

_RAND_SENTENCES = [
    "GM everyone!", "Hello!", "Ping!", "Stay safe ✌️", "Automation test.",
    "Have a great day!", "Knight X Power here.", "Quick reminder!"
]

# ---------------------------------------------------------
# Web Server (Required for Render Web Service & Cloudflare)
# ---------------------------------------------------------
async def handle_home(request):
    return web.Response(text="KnightX Userbot & Web Server is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_home)
    app.router.add_get('/health', handle_home)
    runner = web.AppRunner(app)
    await runner.setup()
    # 0.0.0.0 allows Render to bind properly to its internal port
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server running on port {PORT}")

# ---------------------------------------------------------
# Music Helpers
# ---------------------------------------------------------
def get_yt_stream(query):
    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch'
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        return info['title'], info['url']

# ---------------------------------------------------------
# Music Commands (.play, .skip, .stop, .queue)
# ---------------------------------------------------------
@KnightX.on(events.NewMessage(pattern=r"^\.play\s+(.+)$", outgoing=True))
async def cmd_play(event):
    global current_playing
    query = event.pattern_match.group(1).strip()
    status = await event.respond(f"🔍 Searching: `{query}`...")
    
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
                await status.edit("❌ Please start an active voice chat in this group first.")
    except Exception as e:
        logger.exception("Play error:")
        await status.edit(f"❌ Failed to stream audio: `{e}`")

@KnightX.on(events.NewMessage(pattern=r"^\.skip$", outgoing=True))
async def cmd_skip(event):
    global current_playing
    if not current_playing:
        return await event.respond("❌ Nothing is currently playing.")
    
    if len(music_queue) > 0:
        next_song = music_queue.pop(0)
        await knight_call.play(current_playing['chat_id'], MediaStream(next_song['url']))
        current_playing = next_song
        await event.respond(f"⏭ Skipped! Now playing: **{next_song['title']}**")
    else:
        await knight_call.leave_call(current_playing['chat_id'])
        current_playing = None
        await event.respond("⏭ Skipped! Queue is empty, left the call.")

@KnightX.on(events.NewMessage(pattern=r"^\.stop$", outgoing=True))
async def cmd_stop_music(event):
    global current_playing
    chat_id = event.chat_id
    
    # Check for text task stop
    task = active_tasks.pop(chat_id, None)
    if task:
        task.cancel()

    # Check for music stop
    if current_playing:
        await knight_call.leave_call(current_playing['chat_id'])
        music_queue.clear()
        current_playing = None
        return await event.respond("🛑 Music stopped and queue cleared.")
    
    await event.respond("🛑 Stopped active tasks.")

@KnightX.on(events.NewMessage(pattern=r"^\.queue$", outgoing=True))
async def cmd_queue(event):
    if not current_playing:
        return await event.respond("📭 The queue is currently empty.")
    
    text = f"**▶️ Now Playing:** {current_playing['title']}\n\n**📜 Upcoming Queue:**\n"
    if not music_queue:
        text += "No tracks in queue."
    else:
        for idx, song in enumerate(music_queue, 1):
            text += f"{idx}. {song['title']}\n"
    
    await event.respond(text)

# ---------------------------------------------------------
# Safe Math Evaluation
# ---------------------------------------------------------
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
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_NAMES:
            raise ValueError("Disallowed function call")
        for arg in node.args:
            self.visit(arg)

    def visit_Name(self, node):
        if node.id not in _ALLOWED_NAMES:
            raise ValueError(f"Disallowed variable: {node.id}")

def safe_eval(expr: str):
    parsed = ast.parse(expr.strip(), mode="eval")
    _MathEvalVisitor().visit(parsed)
    return eval(compile(parsed, "<math>", "eval"), {"__builtins__": {}}, _ALLOWED_NAMES)

def extract_first_math_expression(text: str):
    if not text:
        return None
    expr = text.strip()
    if re.fullmatch(r"[\d\s\.\+\-\*\/\^\%\(\),eEpiPIrtginsoclqurta-]+", expr):
        return expr.replace("^", "**")
    m = re.search(r"`([^`]+)`", text)
    if m and re.search(r"[0-9]", m.group(1)):
        return m.group(1).replace("^", "**")
    m = re.search(r"([0-9\.\s\+\-\*\/\^\%\(\)eEpiPIrtginsoclqurta,-]{3,})", text)
    return m.group(1).strip().replace("^", "**") if m else None

# ---------------------------------------------------------
# Utility Commands (.purge, .timedel, .ping, .calculate, .scrape, etc.)
# ---------------------------------------------------------
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
    except Exception as e:
        logger.exception("Error deleting: %s", e)
    return deleted

@KnightX.on(events.NewMessage(pattern=r"^\.purge(?:\s+(all))?$", outgoing=True))
async def cmd_purge(event):
    is_all = bool(event.pattern_match.group(1))
    info_msg = await event.respond("🗡️ Preparing purge...")
    try:
        if not is_all:
            deleted = await delete_messages_in_chat(await event.get_input_chat())
            await info_msg.edit(f"✅ Deleted ~{deleted} message(s).")
        else:
            total = 0
            async for dialog in KnightX.iter_dialogs():
                try:
                    total += await delete_messages_in_chat(dialog.entity)
                except Exception:
                    pass
            await info_msg.edit(f"✅ Deleted ~{total} message(s) across all chats.")
    finally:
        await asyncio.sleep(2)
        await info_msg.delete()
        await event.delete()

@KnightX.on(events.NewMessage(pattern=r"^\.ping$", outgoing=True))
async def cmd_ping(event):
    t0 = time.perf_counter()
    msg = await event.respond("🏓 Pong...")
    latency = int((time.perf_counter() - t0) * 1000)
    await msg.edit(f"🏓 Pong! `{latency} ms`")

@KnightX.on(events.NewMessage(pattern=r"^\.calculate(?:\s+(.+))?$", outgoing=True))
async def cmd_calculate(event):
    expr = event.pattern_match.group(1) or extract_first_math_expression((await event.get_reply_message() or event).text)
    if not expr:
        return await event.respond("❌ Provide or reply to a valid math expression.")
    try:
        res = safe_eval(expr)
        await event.respond(f"🧮 Result:\n`{expr}` = `{res}`")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

@KnightX.on(events.NewMessage(pattern=r"^\.scrape\s+(.+)$", outgoing=True))
async def cmd_scrape(event):
    target = event.pattern_match.group(1).strip()
    status_msg = await event.respond(f"⏳ Preparing to scrape `{target}`...")
    try:
        target_id = int(target) if target.lstrip('-').isdigit() else target
        source_entity = await KnightX.get_entity(target_id)
        source_title = getattr(source_entity, "title", str(target))

        created = await KnightX(CreateChannelRequest(
            title=f"Scrape Dump ({source_title})",
            about=f"Scraped from {target}",
            megagroup=False
        ))
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

        await status_msg.edit(f"✅ Finished! Forwarded `{count}` messages.")
    except Exception as e:
        await status_msg.edit(f"❌ Scraping error: `{e}`")

# ---------------------------------------------------------
# Lifecycle Entry Point
# ---------------------------------------------------------
async def main():
    await start_web_server()
    await KnightX.start()
    await knight_call.start()

    me = await KnightX.get_me()
    logger.info("KnightX Online as: %s (%s)", me.username or me.first_name, me.id)

    try:
        await KnightX.run_until_disconnected()
    finally:
        await knight_call.stop()
        await KnightX.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user.")
