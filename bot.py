"""
YouTube Monitor — Discord Bot (FULLY WORKING)
--------------------------------------
Tracks both YouTube videos AND channels.
Only Server Owner and Admins can use commands.
ADDED: Bulk tracking commands
FIXED: No fake notifications, URL validation, error handling
"""

import os
import json
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import innertube_fallback

load_dotenv()

# ============ ENVIRONMENT VARIABLES ============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))
USE_INNERTUBE_FALLBACK = os.getenv("USE_INNERTUBE_FALLBACK", "true").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Fixed list of Telegram user IDs allowed to run admin commands, e.g. "111111111,222222222"
TELEGRAM_ADMIN_IDS = {
    int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN)

# Holds the running Telegram Application's bot instance once started, so the
# background check loops can push notifications to Telegram too.
telegram_bot_instance = None

# Validate
if not DISCORD_TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN not set")
if not D1_WORKER_URL:
    raise SystemExit("❌ D1_WORKER_URL not set")
if not D1_API_KEY:
    raise SystemExit("❌ D1_API_KEY not set")
if not YOUTUBE_API_KEY:
    print("⚠️ YOUTUBE_API_KEY not set - limited functionality")
if not TELEGRAM_ENABLED:
    print("⚠️ TELEGRAM_BOT_TOKEN not set - Telegram bot disabled, Discord-only mode")
elif not TELEGRAM_ADMIN_IDS:
    print("⚠️ TELEGRAM_ADMIN_IDS not set - no one will be able to use Telegram admin commands")

print(f"🚀 Starting with:")
print(f"📋 D1 Worker: {D1_WORKER_URL}")
print(f"📋 Check Interval: {CHECK_INTERVAL_MINUTES} minutes")
# ===============================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Cooldown Tracking ----------
NOTIFICATION_COOLDOWN = {}  # video_id -> last_notification_time

# ---------- Permission Check ----------

def is_admin_or_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("Use in a server.", ephemeral=True)
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message("❌ Only Admins/Owner can use this.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def telegram_admin_only(func):
    """Decorator for Telegram command handlers - mirrors is_admin_or_owner() for Discord."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in TELEGRAM_ADMIN_IDS:
            await update.message.reply_text("❌ Only admins can use this.")
            return
        await func(update, context)
    return wrapper

# ---------- Web Server ----------

keep_alive_app = Flask(__name__)

@keep_alive_app.route("/")
def home():
    return "YouTube Monitor is running."

@keep_alive_app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

def run_keep_alive():
    keep_alive_app.run(host="0.0.0.0", port=PORT, debug=False)

def start_keep_alive():
    t = Thread(target=run_keep_alive)
    t.daemon = True
    t.start()

# ---------- D1 API ----------

def _d1_headers():
    return {
        "Authorization": f"Bearer {D1_API_KEY}",
        "Content-Type": "application/json",
    }

async def api_request(method: str, endpoint: str, data: Optional[Dict] = None) -> Dict:
    url = f"{D1_WORKER_URL}/{endpoint}"
    headers = _d1_headers()
    
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=headers, json=data, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 404:
                        return {}
                    else:
                        print(f"⚠️ API error {resp.status}")
                        return {}
        except Exception as e:
            print(f"⚠️ API attempt {attempt + 1} failed: {e}")
        await asyncio.sleep(1)
    return {}

# ---------- Video API ----------

async def api_get_tracked_videos() -> Dict:
    try:
        result = await api_request("GET", "tracked_videos")
        if result and isinstance(result, list):
            tracked = {}
            for row in result:
                video_id = row.get("video_id")
                if video_id:
                    tracked[video_id] = {
                        "video_id": video_id,
                        "url": row.get("url", ""),
                        "title": row.get("title", "Unknown"),
                        "channel": row.get("channel", "Unknown"),
                        "channel_id": row.get("channel_id", ""),
                        "start_time": row.get("start_time", datetime.now(timezone.utc).isoformat()),
                        "recovered": bool(row.get("recovered", 0)),
                        "recovered_at": row.get("recovered_at"),
                        "last_stats": json.loads(row.get("last_stats", "{}")) if row.get("last_stats") else {},
                    }
            return tracked
    except Exception as e:
        print(f"❌ Error getting tracked videos: {e}")
    return {}

async def api_add_tracked_video(video_id: str, url: str, title: str, channel: str, channel_id: str, start_time: str):
    await api_request("POST", "tracked_videos", {
        "video_id": video_id,
        "url": url,
        "title": title,
        "channel": channel,
        "channel_id": channel_id,
        "start_time": start_time
    })

async def api_update_video_stats(video_id: str, stats: Dict):
    await api_request("PATCH", f"tracked_videos/{video_id}", {"last_stats": json.dumps(stats)})

async def api_mark_video_removed(video_id: str, removed_at: str):
    await api_request("PATCH", f"tracked_videos/{video_id}", {"recovered_at": removed_at})

async def api_remove_tracked_video(video_id: str):
    await api_request("DELETE", f"tracked_videos/{video_id}")

# ---------- Channel API ----------

async def api_get_tracked_channels() -> Dict:
    try:
        result = await api_request("GET", "tracked_channels")
        if result and isinstance(result, list):
            tracked = {}
            for row in result:
                channel_id = row.get("video_id")
                if channel_id:
                    tracked[channel_id] = {
                        "channel_id": channel_id,
                        "channel_name": row.get("title", "Unknown"),
                        "channel_url": row.get("url", ""),
                        "start_time": row.get("start_time", datetime.now(timezone.utc).isoformat()),
                        "recovered": bool(row.get("recovered", 0)),
                        "recovered_at": row.get("recovered_at"),
                        "last_stats": json.loads(row.get("last_stats", "{}")) if row.get("last_stats") else {},
                    }
            return tracked
    except Exception as e:
        print(f"Error getting tracked channels: {e}")
    return {}

async def api_add_tracked_channel(channel_id: str, channel_name: str, channel_url: str, start_time: str):
    await api_request("POST", "tracked_channels", {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_url": channel_url,
        "start_time": start_time
    })

async def api_update_channel_stats(channel_id: str, stats: Dict):
    await api_request("PATCH", f"tracked_channels/{channel_id}", {"last_stats": json.dumps(stats)})

async def api_mark_channel_removed(channel_id: str, removed_at: str):
    await api_request("PATCH", f"tracked_channels/{channel_id}", {"recovered_at": removed_at})

async def api_remove_tracked_channel(channel_id: str):
    await api_request("DELETE", f"tracked_channels/{channel_id}")

# ---------- Config API ----------

async def api_get_config() -> Dict:
    result = await api_request("GET", "config")
    return result if isinstance(result, dict) else {}

async def api_set_config(key: str, value):
    await api_request("POST", "config", {key: value})

# ---------- YouTube API ----------

def extract_video_id(url: str) -> Optional[str]:
    url = url.split('?')[0].split('&')[0]
    patterns = [
        r'(?:youtube\.com\/watch\?v=)([\w-]+)',
        r'(?:youtu\.be\/)([\w-]+)',
        r'(?:youtube\.com\/embed\/)([\w-]+)',
        r'(?:youtube\.com\/shorts\/)([\w-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.match(r'^[\w-]{11}$', url):
        return url
    return None

def extract_channel_id(url: str) -> Optional[str]:
    url = url.split('?')[0].split('&')[0]
    patterns = [
        r'(?:youtube\.com\/channel\/)([\w-]+)',
        r'(?:youtube\.com\/c\/)([\w-]+)',
        r'(?:youtube\.com\/@)([\w-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

async def get_channel_info_with_status(channel_id: str) -> Dict:
    """
    Returns {"status": ..., "data": ...}
    status is one of:
      "ok"        - channel found, data populated
      "not_found" - YouTube confirmed the channel is gone (404, or 200 with no items)
      "quota"     - API key missing or quota/auth error (403) - NOT a confirmed removal
      "error"     - network/timeout/other transient error - NOT a confirmed removal
    """
    if not YOUTUBE_API_KEY:
        return {"status": "quota", "data": None}

    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "snippet,statistics",
        "id": channel_id,
        "key": YOUTUBE_API_KEY
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("items"):
                        item = data["items"][0]
                        snippet = item.get("snippet", {})
                        stats = item.get("statistics", {})
                        return {"status": "ok", "data": {
                            "channel_id": channel_id,
                            "channel_name": snippet.get("title", "Unknown"),
                            "channel_url": f"https://youtube.com/channel/{channel_id}",
                            "subscribers": int(stats.get("subscriberCount", 0)),
                            "videos_count": int(stats.get("videoCount", 0)),
                            "views": int(stats.get("viewCount", 0)),
                            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                            "description": snippet.get("description", "")[:200],
                        }}
                    # 200 OK but empty items list = YouTube confirms it doesn't exist
                    return {"status": "not_found", "data": None}
                elif resp.status == 403:
                    print(f"⚠️ YouTube API quota exceeded")
                    return await _channel_status_with_fallback("quota", channel_id)
                elif resp.status == 404:
                    return {"status": "not_found", "data": None}
                else:
                    print(f"⚠️ Unexpected YouTube API status {resp.status} for channel {channel_id}")
                    return {"status": "error", "data": None}
    except Exception as e:
        print(f"Channel info error: {e}")
    return {"status": "error", "data": None}

async def _channel_status_with_fallback(official_status: str, channel_id: str) -> Dict:
    """
    Called when the official API couldn't give a definitive answer
    (currently just "quota"). Tries InnerTube as a fallback; if InnerTube
    is also inconclusive, folds its result back into the original
    official_status so callers see the same three-state contract
    ("ok" / "not_found" / anything else = inconclusive) they always have.
    """
    if not USE_INNERTUBE_FALLBACK:
        return {"status": official_status, "data": None}

    print(f"↩️ Falling back to InnerTube for channel {channel_id}")
    fallback = await asyncio.to_thread(innertube_fallback.get_channel_info_with_status, channel_id)

    if fallback["status"] == "ok":
        return fallback
    if fallback["status"] == "not_found":
        return fallback
    # "blocked" or "error" from InnerTube - still inconclusive, keep the
    # original official status so it's treated the same as before
    return {"status": official_status, "data": None}

async def get_channel_info(channel_id: str) -> Optional[Dict]:
    """Convenience wrapper for callers that only care about found-or-not (commands, etc)."""
    result = await get_channel_info_with_status(channel_id)
    return result["data"]

async def get_channels_info_batch(channel_ids: list) -> Dict[str, Dict]:
    """
    Batched channels.list lookup - costs 1 unit per request for up to 50
    IDs, instead of 1 unit per individual channels.list call. Returns
    channel_id -> {"status": ..., "data": ...} with the same status
    contract as get_channel_info_with_status.
    """
    results: Dict[str, Dict] = {}

    if not channel_ids:
        return results

    if not YOUTUBE_API_KEY:
        for cid in channel_ids:
            results[cid] = await _channel_status_with_fallback("quota", cid)
        return results

    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i:i + 50]
        url = "https://www.googleapis.com/youtube/v3/channels"
        params = {
            "part": "snippet,statistics",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        found_ids = set()
                        for item in data.get("items", []):
                            cid = item["id"]
                            found_ids.add(cid)
                            snippet = item.get("snippet", {})
                            stats = item.get("statistics", {})
                            results[cid] = {"status": "ok", "data": {
                                "channel_id": cid,
                                "channel_name": snippet.get("title", "Unknown"),
                                "channel_url": f"https://youtube.com/channel/{cid}",
                                "subscribers": int(stats.get("subscriberCount", 0)),
                                "videos_count": int(stats.get("videoCount", 0)),
                                "views": int(stats.get("viewCount", 0)),
                                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                                "description": snippet.get("description", "")[:200],
                            }}
                        for cid in chunk:
                            if cid not in found_ids:
                                results[cid] = {"status": "not_found", "data": None}
                    elif resp.status == 403:
                        print(f"⚠️ YouTube API quota exceeded (batch of {len(chunk)})")
                        for cid in chunk:
                            results[cid] = await _channel_status_with_fallback("quota", cid)
                    else:
                        print(f"⚠️ Unexpected batch status {resp.status}")
                        for cid in chunk:
                            results[cid] = {"status": "error", "data": None}
        except Exception as e:
            print(f"Batch channel info error: {e}")
            for cid in chunk:
                results[cid] = {"status": "error", "data": None}

    return results

async def get_channel_info_by_handle(handle: str) -> Optional[Dict]:
    if not YOUTUBE_API_KEY:
        return None
    
    handle = handle.lstrip('@').split('?')[0].split('&')[0]
    
    try:
        channel_url = "https://www.googleapis.com/youtube/v3/channels"
        channel_params = {
            "part": "snippet,statistics",
            "forHandle": handle,
            "key": YOUTUBE_API_KEY
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(channel_url, params=channel_params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("items"):
                        item = data["items"][0]
                        snippet = item.get("snippet", {})
                        stats = item.get("statistics", {})
                        channel_id = item.get("id", "")
                        return {
                            "channel_id": channel_id,
                            "channel_name": snippet.get("title", "Unknown"),
                            "channel_url": f"https://youtube.com/channel/{channel_id}",
                            "subscribers": int(stats.get("subscriberCount", 0)),
                            "videos_count": int(stats.get("videoCount", 0)),
                            "views": int(stats.get("viewCount", 0)),
                            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                            "description": snippet.get("description", "")[:200],
                        }
    except Exception as e:
        print(f"Channel by handle error: {e}")
    
    # Fallback: Search method
    try:
        search_url = "https://www.googleapis.com/youtube/v3/search"
        search_params = {
            "part": "snippet",
            "q": handle,
            "type": "channel",
            "maxResults": 1,
            "key": YOUTUBE_API_KEY
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, params=search_params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("items"):
                        item = data["items"][0]
                        channel_id = item["snippet"].get("channelId")
                        if channel_id:
                            return await get_channel_info(channel_id)
    except Exception as e:
        print(f"Search method error: {e}")
    
    return None

async def get_video_info_with_status(video_id: str) -> Dict:
    """
    Get video info from YouTube API with proper error handling.
    Returns {"status": ..., "data": ...}
    status is one of:
      "ok"        - video found, data populated
      "not_found" - YouTube confirmed the video is gone (404, or 200 with no items)
      "quota"     - API key missing or quota/auth error (403) - NOT a confirmed removal
      "error"     - network/timeout/other transient error - NOT a confirmed removal
    """
    if not YOUTUBE_API_KEY:
        return {"status": "quota", "data": None}

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,statistics",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("items"):
                        item = data["items"][0]
                        snippet = item.get("snippet", {})
                        stats = item.get("statistics", {})
                        channel_id = snippet.get("channelId", "")
                        channel_info = await get_channel_info(channel_id) if channel_id else None
                        return {"status": "ok", "data": {
                            "video_id": video_id,
                            "title": snippet.get("title", "Unknown"),
                            "channel": snippet.get("channelTitle", "Unknown"),
                            "channel_id": channel_id,
                            "published_at": snippet.get("publishedAt", ""),
                            "views": int(stats.get("viewCount", 0)),
                            "likes": int(stats.get("likeCount", 0)),
                            "comments": int(stats.get("commentCount", 0)),
                            "subscribers": channel_info.get("subscribers", 0) if channel_info else 0,
                            "url": f"https://youtube.com/watch?v={video_id}",
                            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                        }}
                    else:
                        # 200 OK but empty items list = YouTube confirms it doesn't exist
                        return {"status": "not_found", "data": None}
                elif resp.status == 403:
                    print(f"⚠️ YouTube API quota exceeded")
                    return await _video_status_with_fallback("quota", video_id)
                elif resp.status == 404:
                    return {"status": "not_found", "data": None}
                else:
                    print(f"⚠️ Unexpected YouTube API status {resp.status} for video {video_id}")
                    return {"status": "error", "data": None}
    except asyncio.TimeoutError:
        print(f"⏰ Timeout checking video {video_id}")
        return {"status": "error", "data": None}
    except Exception as e:
        print(f"Video info error: {e}")
    return {"status": "error", "data": None}

async def _video_status_with_fallback(official_status: str, video_id: str) -> Dict:
    """Same pattern as _channel_status_with_fallback, for videos."""
    if not USE_INNERTUBE_FALLBACK:
        return {"status": official_status, "data": None}

    print(f"↩️ Falling back to InnerTube for video {video_id}")
    fallback = await asyncio.to_thread(innertube_fallback.get_video_info_with_status, video_id)

    if fallback["status"] == "ok":
        return fallback
    if fallback["status"] == "not_found":
        return fallback
    return {"status": official_status, "data": None}

async def get_video_info(video_id: str) -> Optional[Dict]:
    """Convenience wrapper for callers that only care about found-or-not (commands, etc)."""
    result = await get_video_info_with_status(video_id)
    return result["data"]

async def get_videos_info_batch(video_ids: list) -> Dict[str, Dict]:
    """
    Batched videos.list lookup - costs 1 unit per request for up to 50 IDs,
    instead of 1 unit per individual videos.list call. Used by the
    background check loop where subscriber count isn't needed (that's
    what made single-item lookups cost 2 units instead of 1 - see
    get_video_info_with_status, which fetches channel info too for
    display purposes). Returns video_id -> {"status": ..., "data": ...}
    with the same status contract as get_video_info_with_status.
    """
    results: Dict[str, Dict] = {}

    if not video_ids:
        return results

    if not YOUTUBE_API_KEY:
        for vid in video_ids:
            results[vid] = await _video_status_with_fallback("quota", vid)
        return results

    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,statistics",
            "id": ",".join(chunk),
            "key": YOUTUBE_API_KEY,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        found_ids = set()
                        for item in data.get("items", []):
                            vid = item["id"]
                            found_ids.add(vid)
                            snippet = item.get("snippet", {})
                            stats = item.get("statistics", {})
                            results[vid] = {"status": "ok", "data": {
                                "video_id": vid,
                                "title": snippet.get("title", "Unknown"),
                                "channel": snippet.get("channelTitle", "Unknown"),
                                "channel_id": snippet.get("channelId", ""),
                                "published_at": snippet.get("publishedAt", ""),
                                "views": int(stats.get("viewCount", 0)),
                                "likes": int(stats.get("likeCount", 0)),
                                "comments": int(stats.get("commentCount", 0)),
                                "subscribers": 0,  # skipped intentionally - see docstring
                                "url": f"https://youtube.com/watch?v={vid}",
                                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                            }}
                        # IDs YouTube didn't return in items = confirmed not found
                        for vid in chunk:
                            if vid not in found_ids:
                                results[vid] = {"status": "not_found", "data": None}
                    elif resp.status == 403:
                        print(f"⚠️ YouTube API quota exceeded (batch of {len(chunk)})")
                        for vid in chunk:
                            results[vid] = await _video_status_with_fallback("quota", vid)
                    else:
                        print(f"⚠️ Unexpected batch status {resp.status}")
                        for vid in chunk:
                            results[vid] = {"status": "error", "data": None}
        except Exception as e:
            print(f"Batch video info error: {e}")
            for vid in chunk:
                results[vid] = {"status": "error", "data": None}

    return results

def format_number(num: int) -> str:
    if num >= 1000000000:
        return f"{num/1000000000:.1f}B"
    elif num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    return str(num)

# ---------- Embeds (FIXED URL VALIDATION) ----------

def build_video_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
    """Build embed for removed/unavailable video - FIXED URL VALIDATION"""
    
    title = info.get("title", "Unknown Video")
    channel = info.get("channel", "Unknown Channel")
    url = info.get("url", "")
    
    # Validate URL
    valid_url = None
    if url and isinstance(url, str) and url.startswith(("http://", "https://")):
        valid_url = url
    
    embed = discord.Embed(
        title=f"🚫 Video Removed / Unavailable",
        description=f"**{title}**\nby **{channel}**",
        color=discord.Color.red(),
    )
    
    # Add link properly
    if valid_url:
        embed.url = valid_url
        embed.description += f"\n\n[Original Link]({valid_url})"
    else:
        embed.description += f"\n\n⚠️ No valid link available"
    
    if old_stats:
        stats_text = (
            f"📺 Views: {format_number(old_stats.get('views', 0))}\n"
            f"👍 Likes: {format_number(old_stats.get('likes', 0))}\n"
            f"💬 Comments: {format_number(old_stats.get('comments', 0))}\n"
            f"👥 Subscribers: {format_number(old_stats.get('subscribers', 0))}"
        )
        embed.add_field(name="📊 Last Known Stats", value=stats_text, inline=False)
    
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    
    embed.add_field(
        name="📹 Channel",
        value=f"[{channel}](https://youtube.com/@{channel.replace(' ', '')})",
        inline=True
    )
    
    try:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        time_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
        embed.add_field(name="⏱️ Removed in", value=time_str, inline=True)
    except:
        pass
    
    embed.set_footer(text=f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed

def build_channel_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
    """Build embed for removed/banned channel - FIXED URL VALIDATION"""
    
    channel_name = info.get("channel_name", "Unknown Channel")
    channel_url = info.get("channel_url", "")
    
    # Validate URL
    valid_url = None
    if channel_url and isinstance(channel_url, str) and channel_url.startswith(("http://", "https://")):
        valid_url = channel_url
    
    embed = discord.Embed(
        title=f"🚫 Channel Banned / Removed",
        description=f"**{channel_name}**",
        color=discord.Color.red(),
    )
    
    # Add link properly
    if valid_url:
        embed.url = valid_url
        embed.description += f"\n\n[Channel Link]({valid_url})"
    else:
        embed.description += f"\n\n⚠️ No valid link available"
    
    if old_stats:
        stats_text = (
            f"👥 Subscribers: {format_number(old_stats.get('subscribers', 0))}\n"
            f"📹 Videos: {format_number(old_stats.get('videos_count', 0))}\n"
            f"👀 Views: {format_number(old_stats.get('views', 0))}"
        )
        embed.add_field(name="📊 Last Known Stats", value=stats_text, inline=False)
    
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    
    try:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        time_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
        embed.add_field(name="⏱️ Removed in", value=time_str, inline=True)
    except:
        pass
    
    embed.set_footer(text=f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed

# ---------- Telegram Notification Text Builders ----------
# Mirror build_video_removed_embed / build_channel_removed_embed but as
# plain HTML text for Telegram, which has no native embed concept.

def _format_elapsed(start_iso: str) -> str:
    try:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s" if hours > 0 else f"{minutes}m {seconds}s"
    except Exception:
        return "unknown"

def build_telegram_video_removed_message(info: Dict, start_iso: str, old_stats: Dict = None) -> str:
    title = info.get("title", "Unknown Video")
    channel = info.get("channel", "Unknown Channel")
    url = info.get("url", "")

    lines = [f"🚨 <b>Video Removed / Unavailable</b>", "", f"<b>{title}</b>", f"by {channel}"]
    if url and url.startswith(("http://", "https://")):
        lines.append(f'<a href="{url}">Original Link</a>')
    else:
        lines.append("⚠️ No valid link available")

    if old_stats:
        lines.append("")
        lines.append("📊 <b>Last Known Stats</b>")
        lines.append(f"📺 Views: {format_number(old_stats.get('views', 0))}")
        lines.append(f"👍 Likes: {format_number(old_stats.get('likes', 0))}")
        lines.append(f"💬 Comments: {format_number(old_stats.get('comments', 0))}")
        lines.append(f"👥 Subscribers: {format_number(old_stats.get('subscribers', 0))}")

    lines.append("")
    lines.append(f"⏱️ Removed in: {_format_elapsed(start_iso)}")
    lines.append(f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return "\n".join(lines)

def build_telegram_channel_removed_message(info: Dict, start_iso: str, old_stats: Dict = None) -> str:
    channel_name = info.get("channel_name", "Unknown Channel")
    channel_url = info.get("channel_url", "")

    lines = [f"🚨 <b>Channel Banned / Removed</b>", "", f"<b>{channel_name}</b>"]
    if channel_url and channel_url.startswith(("http://", "https://")):
        lines.append(f'<a href="{channel_url}">Channel Link</a>')
    else:
        lines.append("⚠️ No valid link available")

    if old_stats:
        lines.append("")
        lines.append("📊 <b>Last Known Stats</b>")
        lines.append(f"👥 Subscribers: {format_number(old_stats.get('subscribers', 0))}")
        lines.append(f"📹 Videos: {format_number(old_stats.get('videos_count', 0))}")
        lines.append(f"👀 Views: {format_number(old_stats.get('views', 0))}")

    lines.append("")
    lines.append(f"⏱️ Removed in: {_format_elapsed(start_iso)}")
    lines.append(f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return "\n".join(lines)

async def send_telegram_notification(text: str, thumbnail: str = None):
    """Best-effort push to the configured Telegram chat. Never raises -
    a Telegram delivery failure should never block or crash the Discord
    notification path."""
    if not TELEGRAM_ENABLED or telegram_bot_instance is None:
        return
    config = await api_get_config()
    chat_id = config.get("telegram_chat_id")
    if not chat_id:
        return
    try:
        if thumbnail:
            await telegram_bot_instance.send_photo(
                chat_id=chat_id, photo=thumbnail, caption=text, parse_mode=ParseMode.HTML
            )
        else:
            await telegram_bot_instance.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False
            )
    except Exception as e:
        print(f"❌ Failed to send Telegram notification: {e}")

# ---------- Background Checks (FIXED - No Fake Notifications) ----------

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_videos():
    global NOTIFICATION_COOLDOWN
    
    try:
        print("🔄 Checking videos...")
        tracked = await api_get_tracked_videos()
        
        if not tracked:
            print("📭 No videos being tracked")
            return
        
        print(f"📊 Found {len(tracked)} tracked videos")
        
        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            return
        
        channel = bot.get_channel(int(channel_id))
        if not channel:
            return
        
        active_ids = [vid for vid, meta in tracked.items() if not meta.get("recovered")]
        if not active_ids:
            return
        
        # One batched call covers up to 50 IDs for 1 unit total, instead
        # of 1 unit per video checked individually.
        results = await get_videos_info_batch(active_ids)
        
        # Only re-check the ones that came back "not_found" - a real
        # removal needs to be confirmed twice before we alert on it.
        not_found_ids = [vid for vid, r in results.items() if r["status"] == "not_found"]
        if not_found_ids:
            print(f"⚠️ {len(not_found_ids)} video(s) not found - verifying...")
            await asyncio.sleep(5)
            retry_results = await get_videos_info_batch(not_found_ids)
            results.update(retry_results)
        
        for video_id in active_ids:
            meta = tracked[video_id]
            result = results.get(video_id)
            if result is None:
                continue
            status = result["status"]
            
            if status == "ok":
                data = result["data"]
                # The batch lookup skips the subscriber-count call to save quota,
                # so it always comes back as 0. Don't let that clobber the last
                # known real subscriber count that the "Video Removed" embed
                # relies on later - carry the previous value forward instead.
                if not data.get("subscribers"):
                    prev_subs = meta.get("last_stats", {}).get("subscribers", 0)
                    data["subscribers"] = prev_subs
                await api_update_video_stats(video_id, data)
                print(f"📊 Updated stats: {data.get('title')[:50]}...")
                continue
            
            if status in ("quota", "error"):
                # Not a confirmed removal - could be quota exceeded, missing key,
                # a timeout, or a transient API error. Skip this cycle entirely
                # rather than risk a false "removed" notification.
                print(f"⏭️ Skipping {video_id} this cycle - status: {status}")
                continue
            
            # status == "not_found" here means BOTH the first batch call and
            # the retry batch call confirmed it missing - a real removal.
            current_time = datetime.now(timezone.utc).timestamp()
            if video_id in NOTIFICATION_COOLDOWN:
                cooldown_time = NOTIFICATION_COOLDOWN[video_id]
                if current_time - cooldown_time < 3600:  # 1 hour cooldown
                    print(f"⏰ Skipping {video_id} - cooldown active")
                    continue
            
            print(f"🚨 Video removed: {meta.get('title', video_id)[:50]}...")
            
            old_stats = meta.get("last_stats", {})
            
            # Create valid URL
            video_url = meta.get("url", "")
            if not video_url or not video_url.startswith(("http://", "https://")):
                video_url = f"https://youtube.com/watch?v={video_id}"
            
            cached_info = {
                "title": meta.get("title", "Unknown Video"),
                "channel": meta.get("channel", "Unknown Channel"),
                "url": video_url,
                "thumbnail": old_stats.get("thumbnail", ""),
            }
            
            embed = build_video_removed_embed(cached_info, meta["start_time"], old_stats)
            
            try:
                await channel.send(content=f"🚨 **Video Removed!**", embed=embed)
                await api_mark_video_removed(video_id, datetime.now(timezone.utc).isoformat())
                NOTIFICATION_COOLDOWN[video_id] = current_time
                print(f"✅ Notification sent for: {meta.get('title', video_id)[:50]}...")
            except Exception as e:
                print(f"❌ Failed to send: {e}")

            tg_text = build_telegram_video_removed_message(cached_info, meta["start_time"], old_stats)
            await send_telegram_notification(tg_text, thumbnail=cached_info.get("thumbnail") or None)
                
    except Exception as e:
        print(f"❌ Video check error: {e}")
        import traceback
        traceback.print_exc()

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_channels():
    global NOTIFICATION_COOLDOWN
    
    try:
        print("🔄 Checking channels...")
        tracked = await api_get_tracked_channels()
        
        if not tracked:
            print("📭 No channels being tracked")
            return
        
        print(f"📊 Found {len(tracked)} tracked channels")
        
        config = await api_get_config()
        notify_channel_id = config.get("notify_channel_id")
        if not notify_channel_id:
            return
        
        channel = bot.get_channel(int(notify_channel_id))
        if not channel:
            return
        
        active_ids = [cid for cid, meta in tracked.items() if not meta.get("recovered")]
        if not active_ids:
            return
        
        # One batched call covers up to 50 IDs for 1 unit total, instead
        # of 1 unit per channel checked individually.
        results = await get_channels_info_batch(active_ids)
        
        not_found_ids = [cid for cid, r in results.items() if r["status"] == "not_found"]
        if not_found_ids:
            print(f"⚠️ {len(not_found_ids)} channel(s) not found - verifying...")
            await asyncio.sleep(5)
            retry_results = await get_channels_info_batch(not_found_ids)
            results.update(retry_results)
        
        for tracked_channel_id in active_ids:
            meta = tracked[tracked_channel_id]
            result = results.get(tracked_channel_id)
            if result is None:
                continue
            status = result["status"]
            
            if status == "ok":
                await api_update_channel_stats(tracked_channel_id, result["data"])
                print(f"📊 Updated stats: {result['data'].get('channel_name')[:50]}... - {format_number(result['data'].get('subscribers', 0))} subscribers")
                continue
            
            if status in ("quota", "error"):
                # Not a confirmed removal - skip this cycle rather than risk
                # a false "removed" notification.
                print(f"⏭️ Skipping {tracked_channel_id} this cycle - status: {status}")
                continue
            
            # status == "not_found" here means BOTH the first batch call and
            # the retry batch call confirmed it missing - a real removal.
            current_time = datetime.now(timezone.utc).timestamp()
            if tracked_channel_id in NOTIFICATION_COOLDOWN:
                cooldown_time = NOTIFICATION_COOLDOWN[tracked_channel_id]
                if current_time - cooldown_time < 3600:  # 1 hour cooldown
                    print(f"⏰ Skipping {tracked_channel_id} - cooldown active")
                    continue
            
            print(f"🚨 Channel removed: {meta.get('channel_name', tracked_channel_id)[:50]}...")
            
            old_stats = meta.get("last_stats", {})
            
            # Create valid URL
            channel_url = meta.get("channel_url", "")
            if not channel_url or not channel_url.startswith(("http://", "https://")):
                channel_url = f"https://youtube.com/channel/{tracked_channel_id}"
            
            cached_info = {
                "channel_name": meta.get("channel_name", "Unknown Channel"),
                "channel_url": channel_url,
                "thumbnail": old_stats.get("thumbnail", ""),
            }
            
            embed = build_channel_removed_embed(cached_info, meta["start_time"], old_stats)
            
            try:
                await channel.send(content=f"🚨 **Channel Banned/Removed!**", embed=embed)
                await api_mark_channel_removed(tracked_channel_id, datetime.now(timezone.utc).isoformat())
                NOTIFICATION_COOLDOWN[tracked_channel_id] = current_time
                print(f"✅ Notification sent for: {meta.get('channel_name', tracked_channel_id)[:50]}...")
            except Exception as e:
                print(f"❌ Failed to send: {e}")

            tg_text = build_telegram_channel_removed_message(cached_info, meta["start_time"], old_stats)
            await send_telegram_notification(tg_text, thumbnail=cached_info.get("thumbnail") or None)
                
    except Exception as e:
        print(f"❌ Channel check error: {e}")
        import traceback
        traceback.print_exc()

@check_tracked_videos.before_loop
async def before_video_check():
    await bot.wait_until_ready()
    print("✅ Video check loop started")

@check_tracked_channels.before_loop
async def before_channel_check():
    await bot.wait_until_ready()
    print("✅ Channel check loop started")

# ---------- Discord Commands ----------

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        check_tracked_videos.start()
        check_tracked_channels.start()
        print(f"✅ Bot is ready!")
        print(f"✅ Logged in as {bot.user}")
        print(f"✅ Checking every {CHECK_INTERVAL_MINUTES} minutes")
        print(f"✅ In {len(bot.guilds)} guilds")
    except Exception as e:
        print(f"Error in on_ready: {e}")

# ============================================================
# VIDEO COMMANDS
# ============================================================

@bot.tree.command(name="trackvideo", description="Start tracking a YouTube video")
@app_commands.describe(url="YouTube video URL")
@is_admin_or_owner()
async def trackvideo(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        video_id = extract_video_id(url)
        if not video_id:
            await interaction.followup.send("❌ Invalid YouTube URL")
            return
        
        tracked = await api_get_tracked_videos()
        if video_id in tracked and not tracked[video_id].get("recovered"):
            await interaction.followup.send("⚠️ Already tracking this video")
            return
        
        info = await get_video_info(video_id)
        if not info:
            await interaction.followup.send("❌ Could not get video info. Is the video public?")
            return
        
        if video_id in tracked:
            await api_remove_tracked_video(video_id)
        
        await api_add_tracked_video(
            video_id, url, info["title"], info["channel"],
            info.get("channel_id", ""), datetime.now(timezone.utc).isoformat()
        )
        await api_update_video_stats(video_id, info)
        
        await interaction.followup.send(
            f"⏱️ Tracking **{info['title']}**\n"
            f"📹 Channel: {info['channel']}\n"
            f"👥 Subscribers: {format_number(info.get('subscribers', 0))}"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="trackvideobulk", description="Track multiple YouTube videos at once")
@app_commands.describe(urls="List of YouTube URLs (one per line, separated by newlines)")
@is_admin_or_owner()
async def trackvideobulk(interaction: discord.Interaction, urls: str):
    await interaction.response.defer(thinking=True)
    
    try:
        url_list = [u.strip() for u in urls.split('\n') if u.strip()]
        
        if not url_list:
            await interaction.followup.send("❌ No URLs provided")
            return
        
        if len(url_list) > 20:
            await interaction.followup.send("❌ Maximum 20 videos per bulk operation")
            return
        
        success = []
        failed = []
        already_tracking = []
        
        for url in url_list:
            try:
                video_id = extract_video_id(url)
                if not video_id:
                    failed.append(f"{url} - Invalid URL")
                    continue
                
                tracked = await api_get_tracked_videos()
                if video_id in tracked and not tracked[video_id].get("recovered"):
                    already_tracking.append(url)
                    continue
                
                info = await get_video_info(video_id)
                if not info:
                    failed.append(f"{url} - Could not get video info")
                    continue
                
                if video_id in tracked:
                    await api_remove_tracked_video(video_id)
                
                await api_add_tracked_video(
                    video_id, url, info["title"], info["channel"],
                    info.get("channel_id", ""), datetime.now(timezone.utc).isoformat()
                )
                await api_update_video_stats(video_id, info)
                
                success.append(f"✅ {info['title'][:30]}... ({info['channel']})")
                
            except Exception as e:
                failed.append(f"{url} - {str(e)}")
        
        response = []
        response.append(f"**📊 Bulk Track Results:**")
        response.append(f"✅ Success: {len(success)}")
        
        if success:
            response.append("\n**Added:**")
            response.extend(success[:10])
            if len(success) > 10:
                response.append(f"...and {len(success) - 10} more")
        
        if already_tracking:
            response.append(f"\n⚠️ Already tracking: {len(already_tracking)}")
        
        if failed:
            response.append(f"\n❌ Failed: {len(failed)}")
            response.extend(failed[:5])
            if len(failed) > 5:
                response.append(f"...and {len(failed) - 5} more")
        
        await interaction.followup.send("\n".join(response))
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="untrackvideo", description="Stop tracking a video")
@app_commands.describe(video_id_or_url="Video ID or URL")
@is_admin_or_owner()
async def untrackvideo(interaction: discord.Interaction, video_id_or_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        video_id = extract_video_id(video_id_or_url)
        if not video_id:
            video_id = video_id_or_url.strip()
        
        tracked = await api_get_tracked_videos()
        if video_id in tracked:
            await api_remove_tracked_video(video_id)
            await interaction.followup.send(f"✅ Stopped tracking `{video_id}`")
        else:
            await interaction.followup.send("❌ Not being tracked")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="listvideos", description="List tracked videos")
@is_admin_or_owner()
async def listvideos(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        tracked = await api_get_tracked_videos()
        if not tracked:
            await interaction.followup.send("📭 No videos being tracked")
            return
        
        lines = ["**📊 Tracked Videos:**"]
        active = [f"`{v}` — {m.get('title', 'Unknown')[:40]} ⏳" for v, m in tracked.items() if not m.get('recovered')]
        removed = [f"`{v}` — {m.get('title', 'Unknown')[:40]} ✅" for v, m in tracked.items() if m.get('recovered')]
        
        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 30)
        
        if active:
            lines.append("\n**Currently Tracking:**")
            lines.extend(active[:10])
        if removed:
            lines.append("\n**Removed:**")
            lines.extend(removed[:5])
        
        await interaction.followup.send("\n".join(lines))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="checkvideo", description="Check if a video exists")
@app_commands.describe(video_id_or_url="Video ID or URL")
@is_admin_or_owner()
async def checkvideo(interaction: discord.Interaction, video_id_or_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        video_id = extract_video_id(video_id_or_url)
        if not video_id:
            video_id = video_id_or_url.strip()
        
        info = await get_video_info(video_id)
        
        if info:
            embed = discord.Embed(
                title=f"✅ Video Available",
                description=f"**{info.get('title', 'Unknown')}**\nby **{info.get('channel', 'Unknown')}**",
                color=discord.Color.green(),
                url=info.get("url", "")
            )
            embed.add_field(
                name="📊 Stats",
                value=(
                    f"Views: {format_number(info.get('views', 0))}\n"
                    f"Likes: {format_number(info.get('likes', 0))}\n"
                    f"Comments: {format_number(info.get('comments', 0))}\n"
                    f"👥 Subscribers: {format_number(info.get('subscribers', 0))}"
                ),
                inline=True
            )
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Video not available (may be removed or private)")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="videostats", description="Get video stats")
@app_commands.describe(video_id_or_url="Video ID or URL")
@is_admin_or_owner()
async def videostats(interaction: discord.Interaction, video_id_or_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        video_id = extract_video_id(video_id_or_url)
        if not video_id:
            video_id = video_id_or_url.strip()
        
        info = await get_video_info(video_id)
        
        if info:
            embed = discord.Embed(
                title=f"📊 Video Stats",
                color=discord.Color.blue(),
                url=info.get("url", "")
            )
            embed.add_field(name="Title", value=info.get("title", "Unknown")[:100], inline=False)
            embed.add_field(name="Channel", value=info.get("channel", "Unknown"), inline=True)
            embed.add_field(name="Views", value=format_number(info.get("views", 0)), inline=True)
            embed.add_field(name="Likes", value=format_number(info.get("likes", 0)), inline=True)
            embed.add_field(name="Comments", value=format_number(info.get("comments", 0)), inline=True)
            embed.add_field(name="👥 Subscribers", value=format_number(info.get("subscribers", 0)), inline=True)
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Video not found")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# ============================================================
# CHANNEL COMMANDS
# ============================================================

@bot.tree.command(name="trackchannel", description="Start tracking a YouTube channel")
@app_commands.describe(channel_url="YouTube channel URL (@handle or /channel/ID)")
@is_admin_or_owner()
async def trackchannel(interaction: discord.Interaction, channel_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        clean_url = channel_url.split('?')[0].split('&')[0]
        channel_id = extract_channel_id(clean_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', clean_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send(f"❌ Could not find channel @{handle}")
                    return
            else:
                await interaction.followup.send("❌ Invalid channel URL")
                return
        
        info = await get_channel_info(channel_id)
        if not info:
            await interaction.followup.send("❌ Could not get channel info")
            return
        
        tracked = await api_get_tracked_channels()
        if channel_id in tracked and not tracked[channel_id].get("recovered"):
            await interaction.followup.send(f"⚠️ Already tracking @{info['channel_name']}")
            return
        
        if channel_id in tracked:
            await api_remove_tracked_channel(channel_id)
        
        await api_add_tracked_channel(
            channel_id,
            info["channel_name"],
            info["channel_url"],
            datetime.now(timezone.utc).isoformat()
        )
        await api_update_channel_stats(channel_id, info)
        
        await interaction.followup.send(
            f"⏱️ Tracking **{info['channel_name']}**\n"
            f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
            f"📹 Videos: {format_number(info.get('videos_count', 0))}"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="trackchannelbulk", description="Track multiple YouTube channels at once")
@app_commands.describe(urls="List of YouTube channel URLs (one per line, separated by newlines)")
@is_admin_or_owner()
async def trackchannelbulk(interaction: discord.Interaction, urls: str):
    await interaction.response.defer(thinking=True)
    
    try:
        url_list = [u.strip() for u in urls.split('\n') if u.strip()]
        
        if not url_list:
            await interaction.followup.send("❌ No URLs provided")
            return
        
        if len(url_list) > 20:
            await interaction.followup.send("❌ Maximum 20 channels per bulk operation")
            return
        
        success = []
        failed = []
        already_tracking = []
        
        for url in url_list:
            try:
                clean_url = url.split('?')[0].split('&')[0]
                channel_id = extract_channel_id(clean_url)
                
                if not channel_id:
                    handle_match = re.search(r'@([\w-]+)', clean_url)
                    if handle_match:
                        handle = handle_match.group(1)
                        info = await get_channel_info_by_handle(handle)
                        if info:
                            channel_id = info.get("channel_id")
                        else:
                            failed.append(f"{url} - Could not find channel")
                            continue
                    else:
                        failed.append(f"{url} - Invalid URL")
                        continue
                
                info = await get_channel_info(channel_id)
                if not info:
                    failed.append(f"{url} - Could not get channel info")
                    continue
                
                tracked = await api_get_tracked_channels()
                if channel_id in tracked and not tracked[channel_id].get("recovered"):
                    already_tracking.append(url)
                    continue
                
                if channel_id in tracked:
                    await api_remove_tracked_channel(channel_id)
                
                await api_add_tracked_channel(
                    channel_id,
                    info["channel_name"],
                    info["channel_url"],
                    datetime.now(timezone.utc).isoformat()
                )
                await api_update_channel_stats(channel_id, info)
                
                success.append(f"✅ {info['channel_name']} - {format_number(info.get('subscribers', 0))} subscribers")
                
            except Exception as e:
                failed.append(f"{url} - {str(e)}")
        
        response = []
        response.append(f"**📊 Bulk Track Results:**")
        response.append(f"✅ Success: {len(success)}")
        
        if success:
            response.append("\n**Added:**")
            response.extend(success[:10])
            if len(success) > 10:
                response.append(f"...and {len(success) - 10} more")
        
        if already_tracking:
            response.append(f"\n⚠️ Already tracking: {len(already_tracking)}")
        
        if failed:
            response.append(f"\n❌ Failed: {len(failed)}")
            response.extend(failed[:5])
            if len(failed) > 5:
                response.append(f"...and {len(failed) - 5} more")
        
        await interaction.followup.send("\n".join(response))
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="untrackchannel", description="Stop tracking a channel")
@app_commands.describe(channel_id_or_url="Channel ID or URL")
@is_admin_or_owner()
async def untrackchannel(interaction: discord.Interaction, channel_id_or_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        clean_url = channel_id_or_url.split('?')[0].split('&')[0]
        channel_id = extract_channel_id(clean_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', clean_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send("❌ Could not find channel")
                    return
            else:
                channel_id = clean_url.strip()
        
        tracked = await api_get_tracked_channels()
        if channel_id in tracked:
            await api_remove_tracked_channel(channel_id)
            await interaction.followup.send(f"✅ Stopped tracking `{channel_id}`")
        else:
            await interaction.followup.send("❌ Not being tracked")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="listchannels", description="List tracked channels")
@is_admin_or_owner()
async def listchannels(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        tracked = await api_get_tracked_channels()
        if not tracked:
            await interaction.followup.send("📭 No channels being tracked")
            return
        
        lines = ["**📊 Tracked Channels:**"]
        active = [f"`{c}` — {m.get('channel_name', 'Unknown')[:40]} ⏳" for c, m in tracked.items() if not m.get('recovered')]
        removed = [f"`{c}` — {m.get('channel_name', 'Unknown')[:40]} ✅" for c, m in tracked.items() if m.get('recovered')]
        
        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 30)
        
        if active:
            lines.append("\n**Currently Tracking:**")
            lines.extend(active[:10])
        if removed:
            lines.append("\n**Removed:**")
            lines.extend(removed[:5])
        
        await interaction.followup.send("\n".join(lines))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="checkchannel", description="Check if a channel exists")
@app_commands.describe(channel_url="Channel URL or @handle")
@is_admin_or_owner()
async def checkchannel(interaction: discord.Interaction, channel_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        clean_url = channel_url.split('?')[0].split('&')[0]
        channel_id = extract_channel_id(clean_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', clean_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send(f"❌ Could not find @{handle}")
                    return
            else:
                await interaction.followup.send("❌ Invalid channel URL")
                return
        
        info = await get_channel_info(channel_id)
        
        if info:
            embed = discord.Embed(
                title=f"✅ Channel Available",
                description=f"**{info.get('channel_name', 'Unknown')}**",
                color=discord.Color.green(),
                url=info.get("channel_url", "")
            )
            embed.add_field(
                name="📊 Stats",
                value=(
                    f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
                    f"📹 Videos: {format_number(info.get('videos_count', 0))}\n"
                    f"👀 Views: {format_number(info.get('views', 0))}"
                ),
                inline=True
            )
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Channel not available (banned/deleted)")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="channelstats", description="Get channel stats")
@app_commands.describe(channel_url="Channel URL or @handle")
@is_admin_or_owner()
async def channelstats(interaction: discord.Interaction, channel_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        clean_url = channel_url.split('?')[0].split('&')[0]
        channel_id = extract_channel_id(clean_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', clean_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send(f"❌ Could not find @{handle}")
                    return
            else:
                await interaction.followup.send("❌ Invalid channel URL")
                return
        
        info = await get_channel_info(channel_id)
        
        if info:
            embed = discord.Embed(
                title=f"📊 Channel Stats",
                description=f"**{info.get('channel_name', 'Unknown')}**",
                color=discord.Color.blue(),
                url=info.get("channel_url", "")
            )
            embed.add_field(name="👥 Subscribers", value=format_number(info.get("subscribers", 0)), inline=True)
            embed.add_field(name="📹 Videos", value=format_number(info.get("videos_count", 0)), inline=True)
            embed.add_field(name="👀 Views", value=format_number(info.get("views", 0)), inline=True)
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Channel not found")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# ============================================================
# SETUP COMMANDS
# ============================================================

@bot.tree.command(name="setchannel", description="Set notification channel")
@is_admin_or_owner()
async def setchannel(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        await api_set_config("notify_channel_id", str(interaction.channel_id))
        await interaction.followup.send(f"✅ Notifications will be sent to {interaction.channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="helpyt", description="Show bot instructions")
@is_admin_or_owner()
async def helpyt(interaction: discord.Interaction):
    instructions = (
        "**🎬 YouTube Monitor - Commands:**\n\n"
        "**Video Tracking:**\n"
        "`/trackvideo url:URL` - Track a video\n"
        "`/trackvideobulk urls:URL1\\nURL2\\nURL3` - Track multiple videos\n"
        "`/untrackvideo id/url` - Stop tracking\n"
        "`/listvideos` - List tracked videos\n"
        "`/checkvideo id/url` - Check if video exists\n"
        "`/videostats id/url` - Get video stats\n\n"
        "**Channel Tracking:**\n"
        "`/trackchannel url:URL` - Track a channel\n"
        "`/trackchannelbulk urls:URL1\\nURL2\\nURL3` - Track multiple channels\n"
        "`/untrackchannel id/url` - Stop tracking\n"
        "`/listchannels` - List tracked channels\n"
        "`/checkchannel url` - Check if channel exists\n"
        "`/channelstats url` - Get channel stats\n\n"
        "**Setup:**\n"
        "`/setchannel` - Set notification channel\n"
        "`/helpyt` - Show this help\n\n"
        "**Permissions:**\n"
        "• Server Owner: All commands\n"
        "• Server Admins: All commands"
    )
    await interaction.response.send_message(instructions)

# ============================================================
# TELEGRAM COMMANDS
# ------------------------------------------------------------
# Mirrors the Discord slash commands above 1:1. Uses the exact same
# api_* / get_video_info / get_channel_info / extract_*_id functions,
# so both platforms share identical logic and the same D1 database -
# only the response formatting differs (Telegram has no embeds).
# ============================================================

@telegram_admin_only
async def tg_trackvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /trackvideo <youtube_url>")
        return
    url = context.args[0]
    try:
        video_id = extract_video_id(url)
        if not video_id:
            await update.message.reply_text("❌ Invalid YouTube URL")
            return

        tracked = await api_get_tracked_videos()
        if video_id in tracked and not tracked[video_id].get("recovered"):
            await update.message.reply_text("⚠️ Already tracking this video")
            return

        info = await get_video_info(video_id)
        if not info:
            await update.message.reply_text("❌ Could not get video info. Is the video public?")
            return

        if video_id in tracked:
            await api_remove_tracked_video(video_id)

        await api_add_tracked_video(
            video_id, url, info["title"], info["channel"],
            info.get("channel_id", ""), datetime.now(timezone.utc).isoformat()
        )
        await api_update_video_stats(video_id, info)

        await update.message.reply_text(
            f"⏱️ Tracking {info['title']}\n"
            f"📹 Channel: {info['channel']}\n"
            f"👥 Subscribers: {format_number(info.get('subscribers', 0))}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_trackvideobulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split(None, 1)
    urls_text = text[1] if len(text) > 1 else ""
    urls = [u.strip() for u in urls_text.replace(",", "\n").split("\n") if u.strip()]
    if not urls:
        await update.message.reply_text("Usage: /trackvideobulk <url1>\\n<url2>\\n...")
        return
    if len(urls) > 20:
        await update.message.reply_text("❌ Maximum 20 videos per bulk operation")
        return

    tracked = await api_get_tracked_videos()
    results = []
    for url in urls:
        try:
            video_id = extract_video_id(url)
            if not video_id:
                results.append(f"❌ Invalid URL: {url[:40]}")
                continue
            if video_id in tracked and not tracked[video_id].get("recovered"):
                results.append(f"⚠️ Already tracking: {video_id}")
                continue
            info = await get_video_info(video_id)
            if not info:
                results.append(f"❌ Could not fetch: {video_id}")
                continue
            if video_id in tracked:
                await api_remove_tracked_video(video_id)
            await api_add_tracked_video(
                video_id, url, info["title"], info["channel"],
                info.get("channel_id", ""), datetime.now(timezone.utc).isoformat()
            )
            await api_update_video_stats(video_id, info)
            results.append(f"✅ {info['title'][:40]}")
        except Exception as e:
            results.append(f"❌ Error on {url[:30]}: {e}")

    await update.message.reply_text("\n".join(results))

@telegram_admin_only
async def tg_untrackvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /untrackvideo <video_id_or_url>")
        return
    video_id_or_url = context.args[0]
    try:
        video_id = extract_video_id(video_id_or_url) or video_id_or_url
        tracked = await api_get_tracked_videos()
        if video_id in tracked:
            await api_remove_tracked_video(video_id)
            await update.message.reply_text(f"✅ Stopped tracking `{video_id}`")
        else:
            await update.message.reply_text("❌ Not being tracked")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_listvideos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tracked = await api_get_tracked_videos()
        if not tracked:
            await update.message.reply_text("📭 No videos being tracked")
            return

        lines = ["📊 Tracked Videos:"]
        active = [f"{v} — {m.get('title', 'Unknown')[:40]} ⏳" for v, m in tracked.items() if not m.get('recovered')]
        removed = [f"{v} — {m.get('title', 'Unknown')[:40]} ✅" for v, m in tracked.items() if m.get('recovered')]

        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 20)

        if active:
            lines.append("\nCurrently Tracking:")
            lines.extend(active[:10])
        if removed:
            lines.append("\nRemoved:")
            lines.extend(removed[:5])

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_checkvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /checkvideo <video_id_or_url>")
        return
    video_id_or_url = context.args[0]
    try:
        video_id = extract_video_id(video_id_or_url) or video_id_or_url
        info = await get_video_info(video_id)
        if info:
            text = (
                f"✅ <b>Video Available</b>\n\n"
                f"<b>{info['title']}</b>\nby {info['channel']}\n\n"
                f"📊 Stats\n"
                f"Views: {format_number(info.get('views', 0))}\n"
                f"Likes: {format_number(info.get('likes', 0))}\n"
                f"Comments: {format_number(info.get('comments', 0))}\n"
                f"👥 Subscribers: {format_number(info.get('subscribers', 0))}"
            )
            if info.get("thumbnail"):
                await update.message.reply_photo(photo=info["thumbnail"], caption=text, parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ Video not available (may be removed or private)")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_videostats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /videostats <video_id_or_url>")
        return
    video_id_or_url = context.args[0]
    try:
        video_id = extract_video_id(video_id_or_url) or video_id_or_url
        info = await get_video_info(video_id)
        if info:
            text = (
                f"📊 <b>{info['title']}</b>\nby {info['channel']}\n\n"
                f"Views: {format_number(info.get('views', 0))}\n"
                f"Likes: {format_number(info.get('likes', 0))}\n"
                f"Comments: {format_number(info.get('comments', 0))}\n"
                f"👥 Subscribers: {format_number(info.get('subscribers', 0))}"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ Video not found")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def _resolve_channel_id(clean_url: str) -> Optional[str]:
    channel_id = extract_channel_id(clean_url)
    if channel_id:
        return channel_id
    handle_match = re.search(r'@([\w-]+)', clean_url)
    if handle_match:
        info = await get_channel_info_by_handle(handle_match.group(1))
        if info:
            return info.get("channel_id")
    return None

@telegram_admin_only
async def tg_trackchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /trackchannel <channel_url_or_@handle>")
        return
    channel_url = context.args[0]
    try:
        clean_url = channel_url.split('?')[0].split('&')[0]
        channel_id = await _resolve_channel_id(clean_url)
        if not channel_id:
            await update.message.reply_text("❌ Invalid channel URL or handle not found")
            return

        info = await get_channel_info(channel_id)
        if not info:
            await update.message.reply_text("❌ Could not get channel info")
            return

        tracked = await api_get_tracked_channels()
        if channel_id in tracked and not tracked[channel_id].get("recovered"):
            await update.message.reply_text(f"⚠️ Already tracking @{info['channel_name']}")
            return

        if channel_id in tracked:
            await api_remove_tracked_channel(channel_id)

        await api_add_tracked_channel(
            channel_id, info["channel_name"], info["channel_url"],
            datetime.now(timezone.utc).isoformat()
        )
        await api_update_channel_stats(channel_id, info)

        await update.message.reply_text(
            f"⏱️ Tracking {info['channel_name']}\n"
            f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
            f"📹 Videos: {format_number(info.get('videos_count', 0))}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_trackchannelbulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split(None, 1)
    urls_text = text[1] if len(text) > 1 else ""
    urls = [u.strip() for u in urls_text.replace(",", "\n").split("\n") if u.strip()]
    if not urls:
        await update.message.reply_text("Usage: /trackchannelbulk <url1>\\n<url2>\\n...")
        return
    if len(urls) > 20:
        await update.message.reply_text("❌ Maximum 20 channels per bulk operation")
        return

    tracked = await api_get_tracked_channels()
    results = []
    for channel_url in urls:
        try:
            clean_url = channel_url.split('?')[0].split('&')[0]
            channel_id = await _resolve_channel_id(clean_url)
            if not channel_id:
                results.append(f"❌ Invalid: {channel_url[:40]}")
                continue
            info = await get_channel_info(channel_id)
            if not info:
                results.append(f"❌ Could not fetch: {channel_url[:40]}")
                continue
            if channel_id in tracked and not tracked[channel_id].get("recovered"):
                results.append(f"⚠️ Already tracking: {info['channel_name'][:30]}")
                continue
            if channel_id in tracked:
                await api_remove_tracked_channel(channel_id)
            await api_add_tracked_channel(
                channel_id, info["channel_name"], info["channel_url"],
                datetime.now(timezone.utc).isoformat()
            )
            await api_update_channel_stats(channel_id, info)
            results.append(f"✅ {info['channel_name'][:40]}")
        except Exception as e:
            results.append(f"❌ Error on {channel_url[:30]}: {e}")

    await update.message.reply_text("\n".join(results))

@telegram_admin_only
async def tg_untrackchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /untrackchannel <channel_id_or_url>")
        return
    channel_id_or_url = context.args[0]
    try:
        clean_url = channel_id_or_url.split('?')[0].split('&')[0]
        channel_id = await _resolve_channel_id(clean_url) or channel_id_or_url
        tracked = await api_get_tracked_channels()
        if channel_id in tracked:
            await api_remove_tracked_channel(channel_id)
            await update.message.reply_text(f"✅ Stopped tracking `{channel_id}`")
        else:
            await update.message.reply_text("❌ Not being tracked")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_listchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tracked = await api_get_tracked_channels()
        if not tracked:
            await update.message.reply_text("📭 No channels being tracked")
            return

        lines = ["📊 Tracked Channels:"]
        active = [f"{c} — {m.get('channel_name', 'Unknown')[:40]} ⏳" for c, m in tracked.items() if not m.get('recovered')]
        removed = [f"{c} — {m.get('channel_name', 'Unknown')[:40]} ✅" for c, m in tracked.items() if m.get('recovered')]

        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 20)

        if active:
            lines.append("\nCurrently Tracking:")
            lines.extend(active[:10])
        if removed:
            lines.append("\nRemoved:")
            lines.extend(removed[:5])

        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_checkchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /checkchannel <channel_url_or_@handle>")
        return
    channel_url = context.args[0]
    try:
        clean_url = channel_url.split('?')[0].split('&')[0]
        channel_id = await _resolve_channel_id(clean_url)
        if not channel_id:
            await update.message.reply_text("❌ Invalid channel URL or handle not found")
            return
        info = await get_channel_info(channel_id)
        if info:
            text = (
                f"✅ <b>Channel Available</b>\n\n"
                f"<b>{info['channel_name']}</b>\n\n"
                f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
                f"📹 Videos: {format_number(info.get('videos_count', 0))}\n"
                f"👀 Views: {format_number(info.get('views', 0))}"
            )
            if info.get("thumbnail"):
                await update.message.reply_photo(photo=info["thumbnail"], caption=text, parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ Channel not available (banned/deleted)")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_channelstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /channelstats <channel_url_or_@handle>")
        return
    channel_url = context.args[0]
    try:
        clean_url = channel_url.split('?')[0].split('&')[0]
        channel_id = await _resolve_channel_id(clean_url)
        if not channel_id:
            await update.message.reply_text("❌ Invalid channel URL or handle not found")
            return
        info = await get_channel_info(channel_id)
        if info:
            text = (
                f"📊 <b>{info['channel_name']}</b>\n\n"
                f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
                f"📹 Videos: {format_number(info.get('videos_count', 0))}\n"
                f"👀 Views: {format_number(info.get('views', 0))}"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("❌ Channel not found")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Sets the notification target for Telegram.
    - No argument: uses the chat this command was run in (works in Groups/DMs).
    - With an argument (@channelusername or numeric chat ID): sets that
      chat/channel as the target. This is REQUIRED for Channels, since a
      command run from inside a Channel arrives as a channel_post with no
      attached user, so it can't pass the admin check at all. Run this
      from a private DM with the bot instead, e.g.:
        /setchannel @your_channel_username
        /setchannel -1001234567890
    """
    try:
        if context.args:
            target = context.args[0]
            try:
                chat = await context.bot.get_chat(target)
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Could not find that chat ({e}).\n\n"
                    "Make sure:\n"
                    "1. The bot is added as an ADMIN of the channel (Channel Settings → Administrators → Add Admin), with at least 'Post Messages' permission.\n"
                    "2. You used its @username (public channels), or its numeric chat ID starting with -100 (private channels - forward a message from the channel to @userinfobot or @getidsbot to find it)."
                )
                return
            chat_id = chat.id
            await api_set_config("telegram_chat_id", str(chat_id))
            label = chat.title or (f"@{chat.username}" if chat.username else str(chat_id))
            await update.message.reply_text(f"✅ Notifications will be sent to {label} (id {chat_id})")
        else:
            await api_set_config("telegram_chat_id", str(update.effective_chat.id))
            await update.message.reply_text(f"✅ Notifications will be sent to this chat (id {update.effective_chat.id})")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

@telegram_admin_only
async def tg_helpyt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instructions = (
        "🎬 YouTube Monitor - Commands:\n\n"
        "Video Tracking:\n"
        "/trackvideo <url> - Track a video\n"
        "/trackvideobulk <url1>\\n<url2>... - Track multiple videos\n"
        "/untrackvideo <id/url> - Stop tracking\n"
        "/listvideos - List tracked videos\n"
        "/checkvideo <id/url> - Check if video exists\n"
        "/videostats <id/url> - Get video stats\n\n"
        "Channel Tracking:\n"
        "/trackchannel <url> - Track a channel\n"
        "/trackchannelbulk <url1>\\n<url2>... - Track multiple channels\n"
        "/untrackchannel <id/url> - Stop tracking\n"
        "/listchannels - List tracked channels\n"
        "/checkchannel <url> - Check if channel exists\n"
        "/channelstats <url> - Get channel stats\n\n"
        "Setup:\n"
        "/setchannel - Set this chat as the notification target (Groups/DMs)\n"
        "/setchannel @channel_or_id - Set a Channel as target (run from a DM with the bot, not from inside the Channel)\n"
        "/helpyt - Show this help\n\n"
        "Permissions:\n"
        "• Only IDs listed in TELEGRAM_ADMIN_IDS can use these commands"
    )
    await update.message.reply_text(instructions)

def build_telegram_app() -> Optional[Application]:
    if not TELEGRAM_ENABLED:
        return None
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("trackvideo", tg_trackvideo))
    application.add_handler(CommandHandler("trackvideobulk", tg_trackvideobulk))
    application.add_handler(CommandHandler("untrackvideo", tg_untrackvideo))
    application.add_handler(CommandHandler("listvideos", tg_listvideos))
    application.add_handler(CommandHandler("checkvideo", tg_checkvideo))
    application.add_handler(CommandHandler("videostats", tg_videostats))
    application.add_handler(CommandHandler("trackchannel", tg_trackchannel))
    application.add_handler(CommandHandler("trackchannelbulk", tg_trackchannelbulk))
    application.add_handler(CommandHandler("untrackchannel", tg_untrackchannel))
    application.add_handler(CommandHandler("listchannels", tg_listchannels))
    application.add_handler(CommandHandler("checkchannel", tg_checkchannel))
    application.add_handler(CommandHandler("channelstats", tg_channelstats))
    application.add_handler(CommandHandler("setchannel", tg_setchannel))
    application.add_handler(CommandHandler("helpyt", tg_helpyt))
    return application

# ============================================================
# MAIN
# ============================================================

async def run_discord_bot():
    async with bot:
        await bot.start(DISCORD_TOKEN)

async def run_telegram_bot():
    global telegram_bot_instance
    application = build_telegram_app()
    if application is None:
        return
    async with application:
        telegram_bot_instance = application.bot
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        print("✅ Telegram bot started")
        try:
            await asyncio.Event().wait()  # run forever until the process is killed
        finally:
            await application.updater.stop()
            await application.stop()

async def main():
    tasks_to_run = [run_discord_bot()]
    if TELEGRAM_ENABLED:
        tasks_to_run.append(run_telegram_bot())
    else:
        print("ℹ️ Skipping Telegram bot startup (TELEGRAM_BOT_TOKEN not set)")
    await asyncio.gather(*tasks_to_run)

if __name__ == "__main__":
    print("🚀 Starting YouTube Monitor (Admin + Owner Only)...")
    print(f"📋 Discord Token: {'✅ Set' if DISCORD_TOKEN else '❌ Not set'}")
    print(f"📋 YouTube API Key: {'✅ Set' if YOUTUBE_API_KEY else '❌ Not set'}")
    print(f"📋 D1 Worker URL: {'✅ Set' if D1_WORKER_URL else '❌ Not set'}")
    print(f"📋 D1 API Key: {'✅ Set' if D1_API_KEY else '❌ Not set'}")
    print(f"📋 Telegram Bot: {'✅ Set' if TELEGRAM_ENABLED else '⏭️ Not configured'}")
    print("👥 Only Server Owners and Admins can use commands")
    
    start_keep_alive()
    print(f"✅ Web server started on port {PORT}")
    
    try:
        asyncio.run(main())
    except discord.LoginFailure:
        print("❌ Invalid Discord token")
    except Exception as e:
        print(f"❌ Failed to start bot(s): {e}")