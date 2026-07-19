"""
YouTube Monitor — Discord Bot (COMPLETE)
--------------------------------------
Tracks both YouTube videos AND channels.
Sends notifications when videos go unavailable or channels get banned/deleted.
"""

import os
import json
import base64
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

load_dotenv()

# ============ ENVIRONMENT VARIABLES ============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))

# Validate required variables
if not DISCORD_TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN not set")
if not D1_WORKER_URL:
    raise SystemExit("❌ D1_WORKER_URL not set")
if not D1_API_KEY:
    raise SystemExit("❌ D1_API_KEY not set")
if not YOUTUBE_API_KEY:
    print("⚠️ YOUTUBE_API_KEY not set - limited functionality")
# ===============================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Owner-only check ----------

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return False
        if interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ---------- keep-alive web server ----------

keep_alive_app = Flask(__name__)

@keep_alive_app.route("/")
def home():
    return "YouTube Monitor (Videos + Channels) is running."

@keep_alive_app.route("/health")
def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

def run_keep_alive():
    keep_alive_app.run(host="0.0.0.0", port=PORT, debug=False)

def start_keep_alive():
    t = Thread(target=run_keep_alive)
    t.daemon = True
    t.start()

# ---------- D1 storage API ----------

def _d1_headers():
    return {
        "Authorization": f"Bearer {D1_API_KEY}",
        "Content-Type": "application/json",
    }

async def api_request(method: str, endpoint: str, data: Optional[Dict] = None) -> Dict:
    """Generic API request handler with retries"""
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
                        text = await resp.text()
                        print(f"API error {resp.status}: {text[:100]}")
                        return {}
        except asyncio.TimeoutError:
            print(f"API timeout (attempt {attempt + 1})")
        except Exception as e:
            print(f"API error (attempt {attempt + 1}): {e}")
        await asyncio.sleep(1)
    return {}

# ============================================================
# VIDEO API FUNCTIONS
# ============================================================

async def api_get_tracked_videos() -> Dict:
    """Get all tracked videos from D1"""
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
        print(f"Error getting tracked videos: {e}")
    return {}

async def api_add_tracked_video(video_id: str, url: str, title: str, channel: str, channel_id: str, start_time: str):
    """Add a video to tracking"""
    await api_request("POST", "tracked_videos", {
        "video_id": video_id,
        "url": url,
        "title": title,
        "channel": channel,
        "channel_id": channel_id,
        "start_time": start_time
    })

async def api_update_video_stats(video_id: str, stats: Dict):
    """Update video stats"""
    await api_request("PATCH", f"tracked_videos/{video_id}", {"last_stats": json.dumps(stats)})

async def api_mark_video_removed(video_id: str, removed_at: str):
    """Mark video as removed"""
    await api_request("PATCH", f"tracked_videos/{video_id}", {"recovered_at": removed_at})

async def api_remove_tracked_video(video_id: str):
    """Remove video from tracking"""
    await api_request("DELETE", f"tracked_videos/{video_id}")

# ============================================================
# CHANNEL API FUNCTIONS
# ============================================================

async def api_get_tracked_channels() -> Dict:
    """Get all tracked channels from D1"""
    try:
        result = await api_request("GET", "tracked_channels")
        if result and isinstance(result, list):
            tracked = {}
            for row in result:
                # For channels, video_id stores the channel_id
                channel_id = row.get("video_id")
                if channel_id:
                    tracked[channel_id] = {
                        "channel_id": channel_id,
                        "channel_name": row.get("title", "Unknown"),  # title stores channel_name
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
    """Add a channel to tracking"""
    await api_request("POST", "tracked_channels", {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_url": channel_url,
        "start_time": start_time
    })

async def api_update_channel_stats(channel_id: str, stats: Dict):
    """Update channel stats"""
    await api_request("PATCH", f"tracked_channels/{channel_id}", {"last_stats": json.dumps(stats)})

async def api_mark_channel_removed(channel_id: str, removed_at: str):
    """Mark channel as removed/banned"""
    await api_request("PATCH", f"tracked_channels/{channel_id}", {"recovered_at": removed_at})

async def api_remove_tracked_channel(channel_id: str):
    """Remove channel from tracking"""
    await api_request("DELETE", f"tracked_channels/{channel_id}")

# ============================================================
# CONFIG API FUNCTIONS
# ============================================================

async def api_get_config() -> Dict:
    """Get bot configuration"""
    result = await api_request("GET", "config")
    return result if isinstance(result, dict) else {}

async def api_set_config(key: str, value):
    """Set bot configuration"""
    await api_request("POST", "config", {key: value})

# ============================================================
# YOUTUBE API FUNCTIONS
# ============================================================

def extract_video_id(url: str) -> Optional[str]:
    """Extract video ID from YouTube URL"""
    patterns = [
        r'(?:youtube\.com\/watch\?v=)([\w-]+)',
        r'(?:youtu\.be\/)([\w-]+)',
        r'(?:youtube\.com\/embed\/)([\w-]+)',
        r'(?:youtube\.com\/v\/)([\w-]+)',
        r'(?:youtube\.com\/shorts\/)([\w-]+)',
        r'(?:youtube\.com\/watch\?.*v=)([\w-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.match(r'^[\w-]{11}$', url):
        return url
    return None

def extract_channel_id(url: str) -> Optional[str]:
    """Extract channel ID from YouTube URL"""
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

async def get_channel_info(channel_id: str) -> Optional[Dict]:
    """Get channel info including subscribers"""
    if not YOUTUBE_API_KEY:
        print("⚠️ YouTube API key not set")
        return None
    
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
                elif resp.status == 404:
                    print(f"Channel {channel_id} not found (may be banned/deleted)")
                    return None
                elif resp.status == 403:
                    print(f"YouTube API quota exceeded")
                    return None
                else:
                    print(f"YouTube API error: {resp.status}")
                    return None
    except Exception as e:
        print(f"Channel info error: {e}")
    return None

async def get_channel_info_by_handle(handle: str) -> Optional[Dict]:
    """Get channel info by @handle"""
    if not YOUTUBE_API_KEY:
        return None
    
    handle = handle.lstrip('@')
    
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "part": "snippet,statistics",
        "forHandle": handle,
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
                    else:
                        print(f"No channel found for handle: {handle}")
                        return None
                elif resp.status == 404:
                    print(f"Channel not found: {handle}")
                    return None
                else:
                    print(f"YouTube API error: {resp.status}")
                    return None
    except Exception as e:
        print(f"Channel by handle error: {e}")
    return None

async def get_video_info(video_id: str) -> Optional[Dict]:
    """Get video info from YouTube API"""
    if not YOUTUBE_API_KEY:
        return None
    
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
                        
                        # Get channel info for subscribers
                        channel_info = await get_channel_info(channel_id) if channel_id else None
                        
                        return {
                            "video_id": video_id,
                            "title": snippet.get("title", "Unknown"),
                            "channel": snippet.get("channelTitle", "Unknown"),
                            "channel_id": channel_id,
                            "published_at": snippet.get("publishedAt", ""),
                            "views": int(stats.get("viewCount", 0)),
                            "likes": int(stats.get("likeCount", 0)),
                            "comments": int(stats.get("commentCount", 0)),
                            "subscribers": channel_info.get("subscribers", 0) if channel_info else 0,
                            "channel_videos": channel_info.get("videos_count", 0) if channel_info else 0,
                            "url": f"https://youtube.com/watch?v={video_id}",
                            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                        }
                elif resp.status == 403:
                    print(f"YouTube API quota exceeded")
                    return None
                else:
                    print(f"YouTube API error: {resp.status}")
                    return None
    except Exception as e:
        print(f"Video info error: {e}")
    return None

def format_number(num: int) -> str:
    """Format large numbers"""
    if num >= 1000000000:
        return f"{num/1000000000:.1f}B"
    elif num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    return str(num)

# ============================================================
# EMBED BUILDERS
# ============================================================

def build_channel_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
    """Build embed for removed/banned channel"""
    
    channel_name = info.get("channel_name", "Unknown Channel")
    channel_url = info.get("channel_url", "")
    
    embed = discord.Embed(
        title=f"🚫 Channel Banned / Removed",
        description=(
            f"**{channel_name}**\n\n"
            f"The channel has been banned, deleted, or is no longer available.\n"
            f"[Channel Link]({channel_url})"
        ),
        color=discord.Color.red(),
        url=channel_url,
    )
    
    if old_stats:
        subscribers = old_stats.get("subscribers", 0)
        videos = old_stats.get("videos_count", 0)
        views = old_stats.get("views", 0)
        
        stats_text = (
            f"👥 Subscribers: {format_number(subscribers)}\n"
            f"📹 Videos: {format_number(videos)}\n"
            f"👀 Views: {format_number(views)}"
        )
        embed.add_field(name="📊 Last Known Stats", value=stats_text, inline=False)
    
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    
    # Time tracking - Removed in
    try:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        time_str = []
        if hours > 0:
            time_str.append(f"{hours}h")
        if minutes > 0:
            time_str.append(f"{minutes}m")
        time_str.append(f"{seconds}s")
        
        embed.add_field(
            name="⏱️ Removed in",
            value=" ".join(time_str),
            inline=True
        )
    except:
        pass
    
    embed.set_footer(text=f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed

def build_video_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
    """Build embed for removed/unavailable video"""
    
    title = info.get("title", "Unknown Video")
    channel = info.get("channel", "Unknown Channel")
    url = info.get("url", "")
    
    embed = discord.Embed(
        title=f"🚫 Video Removed / Unavailable",
        description=(
            f"**{title}**\n"
            f"by **{channel}**\n\n"
            f"The video is no longer available or was removed.\n"
            f"[Original Link]({url})"
        ),
        color=discord.Color.red(),
        url=url,
    )
    
    if old_stats:
        views = old_stats.get("views", 0)
        likes = old_stats.get("likes", 0)
        comments = old_stats.get("comments", 0)
        subscribers = old_stats.get("subscribers", 0)
        
        stats_text = (
            f"📺 Views: {format_number(views)}\n"
            f"👍 Likes: {format_number(likes)}\n"
            f"💬 Comments: {format_number(comments)}\n"
            f"👥 Subscribers: {format_number(subscribers)}"
        )
        embed.add_field(name="📊 Last Known Stats", value=stats_text, inline=False)
    
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    
    embed.add_field(
        name="📹 Channel",
        value=f"[{channel}](https://youtube.com/@{channel.replace(' ', '')})",
        inline=True
    )
    
    # Time tracking - Removed in
    try:
        start = datetime.fromisoformat(start_iso)
        delta = datetime.now(timezone.utc) - start
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        time_str = []
        if hours > 0:
            time_str.append(f"{hours}h")
        if minutes > 0:
            time_str.append(f"{minutes}m")
        time_str.append(f"{seconds}s")
        
        embed.add_field(
            name="⏱️ Removed in",
            value=" ".join(time_str),
            inline=True
        )
    except:
        pass
    
    embed.set_footer(text=f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed

# ============================================================
# BACKGROUND CHECKS
# ============================================================

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_videos():
    """Check all tracked videos"""
    try:
        print("🔄 Checking tracked videos...")
        tracked = await api_get_tracked_videos()
        
        if not tracked:
            print("📭 No videos being tracked")
            return
        
        print(f"📊 Found {len(tracked)} tracked videos")
        
        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            print("⚠️ No notification channel set")
            return
        
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            print(f"❌ Channel {channel_id} not found")
            return
        
        for video_id, meta in tracked.items():
            if meta.get("recovered"):
                continue
            
            print(f"🔍 Checking video: {meta.get('title', video_id)}")
            
            info = await get_video_info(video_id)
            
            if info is None:
                print(f"🚨 Video removed: {video_id} - {meta.get('title', 'Unknown')}")
                
                old_stats = meta.get("last_stats", {})
                
                cached_info = {
                    "title": meta.get("title", "Unknown Video"),
                    "channel": meta.get("channel", "Unknown Channel"),
                    "url": meta.get("url", f"https://youtube.com/watch?v={video_id}"),
                    "thumbnail": old_stats.get("thumbnail", ""),
                }
                
                embed = build_video_removed_embed(cached_info, meta["start_time"], old_stats)
                try:
                    await channel.send(content=f"🚨 **Video Removed!**", embed=embed)
                    await api_mark_video_removed(video_id, datetime.now(timezone.utc).isoformat())
                    print(f"✅ Notification sent for video: {video_id}")
                except Exception as e:
                    print(f"❌ Failed to send video notification: {e}")
            else:
                await api_update_video_stats(video_id, info)
                print(f"📊 Updated video stats: {info.get('title')}")
                
    except Exception as e:
        print(f"❌ Error in check_tracked_videos: {e}")
        import traceback
        traceback.print_exc()

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_channels():
    """Check all tracked channels"""
    try:
        print("🔄 Checking tracked channels...")
        tracked = await api_get_tracked_channels()
        
        if not tracked:
            print("📭 No channels being tracked")
            return
        
        print(f"📊 Found {len(tracked)} tracked channels")
        
        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            print("⚠️ No notification channel set")
            return
        
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            print(f"❌ Channel {channel_id} not found")
            return
        
        for channel_id, meta in tracked.items():
            if meta.get("recovered"):
                continue
            
            print(f"🔍 Checking channel: {meta.get('channel_name', channel_id)}")
            
            info = await get_channel_info(channel_id)
            
            if info is None:
                print(f"🚨 Channel removed/banned: {channel_id} - {meta.get('channel_name', 'Unknown')}")
                
                old_stats = meta.get("last_stats", {})
                
                cached_info = {
                    "channel_name": meta.get("channel_name", "Unknown Channel"),
                    "channel_url": meta.get("channel_url", f"https://youtube.com/channel/{channel_id}"),
                    "thumbnail": old_stats.get("thumbnail", ""),
                }
                
                embed = build_channel_removed_embed(cached_info, meta["start_time"], old_stats)
                try:
                    await channel.send(content=f"🚨 **Channel Banned/Removed!**", embed=embed)
                    await api_mark_channel_removed(channel_id, datetime.now(timezone.utc).isoformat())
                    print(f"✅ Notification sent for channel: {channel_id}")
                except Exception as e:
                    print(f"❌ Failed to send channel notification: {e}")
            else:
                await api_update_channel_stats(channel_id, info)
                print(f"📊 Updated channel stats: {info.get('channel_name')} - {format_number(info.get('subscribers', 0))} subscribers")
                
    except Exception as e:
        print(f"❌ Error in check_tracked_channels: {e}")
        import traceback
        traceback.print_exc()

@check_tracked_videos.before_loop
async def before_video_check():
    await bot.wait_until_ready()
    print("Video check loop started")

@check_tracked_channels.before_loop
async def before_channel_check():
    await bot.wait_until_ready()
    print("Channel check loop started")

# ============================================================
# DISCORD COMMANDS
# ============================================================

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        check_tracked_videos.start()
        check_tracked_channels.start()
        print(f"✅ Bot is ready")
        print(f"✅ Logged in as {bot.user}")
        print(f"✅ Checking videos every {CHECK_INTERVAL_MINUTES} minutes")
        print(f"✅ Checking channels every {CHECK_INTERVAL_MINUTES} minutes")
        print(f"✅ In {len(bot.guilds)} guilds")
        
        if not YOUTUBE_API_KEY:
            print("⚠️ YouTube API key not set")
    except Exception as e:
        print(f"Error in on_ready: {e}")

# ============================================================
# VIDEO COMMANDS
# ============================================================

@bot.tree.command(name="trackvideo", description="Start tracking a YouTube video")
@app_commands.describe(url="YouTube video URL")
@is_owner()
async def trackvideo(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        print(f"📝 Tracking video: {url}")
        video_id = extract_video_id(url)
        if not video_id:
            await interaction.followup.send("❌ Invalid YouTube URL")
            return
        
        tracked = await api_get_tracked_videos()
        if video_id in tracked and not tracked[video_id].get("recovered"):
            await interaction.followup.send(f"⚠️ Already tracking this video")
            return
        
        info = await get_video_info(video_id)
        if not info:
            await interaction.followup.send(f"❌ Could not get video info. Is the video public?")
            return
        
        if video_id in tracked:
            await api_remove_tracked_video(video_id)
        
        await api_add_tracked_video(
            video_id,
            url,
            info["title"],
            info["channel"],
            info.get("channel_id", ""),
            datetime.now(timezone.utc).isoformat()
        )
        
        await api_update_video_stats(video_id, info)
        
        await interaction.followup.send(
            f"⏱️ Started tracking video **{info['title']}**\n"
            f"📹 Channel: {info['channel']}\n"
            f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
            f"🔗 Link: {url}\n\n"
            f"Will notify if video becomes unavailable!"
        )
    except Exception as e:
        print(f"❌ Error in trackvideo: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="untrackvideo", description="Stop tracking a YouTube video")
@app_commands.describe(video_id_or_url="Video ID or YouTube URL")
@is_owner()
async def untrackvideo(interaction: discord.Interaction, video_id_or_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        video_id = extract_video_id(video_id_or_url)
        if not video_id:
            video_id = video_id_or_url.strip()
        
        tracked = await api_get_tracked_videos()
        if video_id in tracked:
            await api_remove_tracked_video(video_id)
            await interaction.followup.send(f"✅ Stopped tracking video `{video_id}`")
        else:
            await interaction.followup.send(f"❌ Video not being tracked")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="listvideos", description="List all tracked videos")
@is_owner()
async def listvideos(interaction: discord.Interaction):
    try:
        tracked = await api_get_tracked_videos()
        
        if not tracked:
            await interaction.response.send_message("📭 No videos being tracked")
            return
        
        active = []
        removed = []
        
        for video_id, meta in tracked.items():
            title = meta.get("title", "Unknown")[:50]
            if meta.get("recovered"):
                removed.append(f"`{video_id}` — {title} ✅")
            else:
                active.append(f"`{video_id}` — {title} ⏳")
        
        lines = ["**📊 Tracked Videos:**"]
        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 30)
        
        if active:
            lines.append("\n**Currently Tracking:**")
            lines.extend(active[:10])
            if len(active) > 10:
                lines.append(f"*...and {len(active) - 10} more*")
        
        if removed:
            lines.append("\n**Removed Videos:**")
            lines.extend(removed[:5])
            if len(removed) > 5:
                lines.append(f"*...and {len(removed) - 5} more*")
        
        response = "\n".join(lines)
        if len(response) > 2000:
            chunks = []
            current_chunk = []
            for line in lines:
                if len("\n".join(current_chunk + [line])) > 2000:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                current_chunk.append(line)
            if current_chunk:
                chunks.append("\n".join(current_chunk))
            
            for chunk in chunks:
                await interaction.response.send_message(chunk)
        else:
            await interaction.response.send_message(response)
            
    except Exception as e:
        print(f"❌ Error in listvideos: {e}")
        import traceback
        traceback.print_exc()
        await interaction.response.send_message(f"❌ Error: {e}")

@bot.tree.command(name="checkvideo", description="Check if a video exists")
@app_commands.describe(video_id_or_url="Video ID or YouTube URL")
@is_owner()
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
            await interaction.followup.send(f"❌ Video `{video_id}` is NOT available (removed/private/unlisted)")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="videostats", description="Get current video stats")
@app_commands.describe(video_id_or_url="Video ID or YouTube URL")
@is_owner()
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
            embed.add_field(
                name="Views",
                value=format_number(info.get("views", 0)),
                inline=True
            )
            embed.add_field(
                name="Likes",
                value=format_number(info.get("likes", 0)),
                inline=True
            )
            embed.add_field(
                name="Comments",
                value=format_number(info.get("comments", 0)),
                inline=True
            )
            embed.add_field(
                name="👥 Subscribers",
                value=format_number(info.get("subscribers", 0)),
                inline=True
            )
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Video `{video_id}` not found")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# ============================================================
# CHANNEL COMMANDS
# ============================================================

@bot.tree.command(name="trackchannel", description="Start tracking a YouTube channel")
@app_commands.describe(
    channel_url="YouTube channel URL (youtube.com/@handle or youtube.com/channel/ID)"
)
@is_owner()
async def trackchannel(interaction: discord.Interaction, channel_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        print(f"📝 Tracking channel: {channel_url}")
        
        # Extract channel ID or handle
        channel_id = extract_channel_id(channel_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', channel_url)
            if handle_match:
                handle = handle_match.group(1)
                print(f"🔍 Looking up handle: @{handle}")
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                    print(f"✅ Found channel ID: {channel_id}")
                else:
                    await interaction.followup.send(f"❌ Could not find channel @{handle}")
                    return
            else:
                await interaction.followup.send(f"❌ Invalid channel URL. Use youtube.com/@handle or youtube.com/channel/ID")
                return
        
        info = await get_channel_info(channel_id)
        if not info:
            await interaction.followup.send(f"❌ Could not get channel info. Is the channel valid?")
            return
        
        tracked = await api_get_tracked_channels()
        if channel_id in tracked and not tracked[channel_id].get("recovered"):
            await interaction.followup.send(f"⚠️ Already tracking channel @{info['channel_name']}")
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
            f"⏱️ Started tracking channel **{info['channel_name']}**\n"
            f"👥 Subscribers: {format_number(info.get('subscribers', 0))}\n"
            f"📹 Videos: {format_number(info.get('videos_count', 0))}\n"
            f"🔗 Link: {info['channel_url']}\n\n"
            f"Will notify if channel gets banned or deleted!"
        )
        
    except Exception as e:
        print(f"❌ Error in trackchannel: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="untrackchannel", description="Stop tracking a YouTube channel")
@app_commands.describe(channel_id_or_url="Channel ID or URL")
@is_owner()
async def untrackchannel(interaction: discord.Interaction, channel_id_or_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        channel_id = extract_channel_id(channel_id_or_url)
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', channel_id_or_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send(f"❌ Could not find channel")
                    return
            else:
                channel_id = channel_id_or_url.strip()
        
        tracked = await api_get_tracked_channels()
        if channel_id in tracked:
            await api_remove_tracked_channel(channel_id)
            await interaction.followup.send(f"✅ Stopped tracking channel `{channel_id}`")
        else:
            await interaction.followup.send(f"❌ Channel not being tracked")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="listchannels", description="List all tracked channels")
@is_owner()
async def listchannels(interaction: discord.Interaction):
    try:
        tracked = await api_get_tracked_channels()
        
        if not tracked:
            await interaction.response.send_message("📭 No channels being tracked")
            return
        
        active = []
        removed = []
        
        for channel_id, meta in tracked.items():
            name = meta.get("channel_name", "Unknown")[:50]
            if meta.get("recovered"):
                removed.append(f"`{channel_id}` — {name} ✅")
            else:
                active.append(f"`{channel_id}` — {name} ⏳")
        
        lines = ["**📊 Tracked Channels:**"]
        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 30)
        
        if active:
            lines.append("\n**Currently Tracking:**")
            lines.extend(active[:10])
            if len(active) > 10:
                lines.append(f"*...and {len(active) - 10} more*")
        
        if removed:
            lines.append("\n**Removed Channels:**")
            lines.extend(removed[:5])
            if len(removed) > 5:
                lines.append(f"*...and {len(removed) - 5} more*")
        
        response = "\n".join(lines)
        if len(response) > 2000:
            chunks = []
            current_chunk = []
            for line in lines:
                if len("\n".join(current_chunk + [line])) > 2000:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                current_chunk.append(line)
            if current_chunk:
                chunks.append("\n".join(current_chunk))
            
            for chunk in chunks:
                await interaction.response.send_message(chunk)
        else:
            await interaction.response.send_message(response)
            
    except Exception as e:
        print(f"❌ Error in listchannels: {e}")
        import traceback
        traceback.print_exc()
        await interaction.response.send_message(f"❌ Error: {e}")

@bot.tree.command(name="checkchannel", description="Check if a channel exists")
@app_commands.describe(channel_url="Channel URL or @handle")
@is_owner()
async def checkchannel(interaction: discord.Interaction, channel_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        channel_id = extract_channel_id(channel_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', channel_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send(f"❌ Could not find channel @{handle}")
                    return
            else:
                await interaction.followup.send(f"❌ Invalid channel URL")
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
            await interaction.followup.send(f"❌ Channel is NOT available (banned/deleted)")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="channelstats", description="Get current channel stats")
@app_commands.describe(channel_url="Channel URL or @handle")
@is_owner()
async def channelstats(interaction: discord.Interaction, channel_url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        channel_id = extract_channel_id(channel_url)
        
        if not channel_id:
            handle_match = re.search(r'@([\w-]+)', channel_url)
            if handle_match:
                handle = handle_match.group(1)
                info = await get_channel_info_by_handle(handle)
                if info:
                    channel_id = info.get("channel_id")
                else:
                    await interaction.followup.send(f"❌ Could not find channel @{handle}")
                    return
            else:
                await interaction.followup.send(f"❌ Invalid channel URL")
                return
        
        info = await get_channel_info(channel_id)
        
        if info:
            embed = discord.Embed(
                title=f"📊 Channel Stats",
                description=f"**{info.get('channel_name', 'Unknown')}**",
                color=discord.Color.blue(),
                url=info.get("channel_url", "")
            )
            embed.add_field(
                name="👥 Subscribers",
                value=format_number(info.get("subscribers", 0)),
                inline=True
            )
            embed.add_field(
                name="📹 Videos",
                value=format_number(info.get("videos_count", 0)),
                inline=True
            )
            embed.add_field(
                name="👀 Views",
                value=format_number(info.get("views", 0)),
                inline=True
            )
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            if info.get("description"):
                embed.add_field(name="Description", value=info["description"][:200], inline=False)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Channel not found")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# ============================================================
# SETUP COMMANDS
# ============================================================

@bot.tree.command(name="setchannel", description="Set notification channel")
@is_owner()
async def setchannel(interaction: discord.Interaction):
    try:
        await api_set_config("notify_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(f"✅ Notifications will be sent to {interaction.channel.mention}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}")

@bot.tree.command(name="helpyt", description="Show bot instructions")
@is_owner()
async def helpyt(interaction: discord.Interaction):
    instructions = (
        "**🎬 YouTube Monitor - Commands:**\n\n"
        "**Video Tracking:**\n"
        "`/trackvideo url:URL` - Start tracking a video\n"
        "`/untrackvideo id/url` - Stop tracking video\n"
        "`/listvideos` - List tracked videos\n"
        "`/checkvideo id/url` - Check if video exists\n"
        "`/videostats id/url` - Get video stats\n\n"
        "**Channel Tracking:**\n"
        "`/trackchannel url:URL` - Start tracking a channel\n"
        "`/untrackchannel id/url` - Stop tracking channel\n"
        "`/listchannels` - List tracked channels\n"
        "`/checkchannel url` - Check if channel exists\n"
        "`/channelstats url` - Get channel stats\n\n"
        "**Setup:**\n"
        "`/setchannel` - Set notification channel\n"
        "`/helpyt` - Show this help\n\n"
        "**What triggers notifications:**\n"
        "• Video deleted/private/unlisted\n"
        "• Channel banned/deleted\n\n"
        "**Stats shown:**\n"
        "• Views, Likes, Comments (videos)\n"
        "• Subscribers, Videos, Views (channels)\n"
        "• Thumbnail\n"
        "• Removed in (how long it was tracked)"
    )
    await interaction.response.send_message(instructions)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 Starting YouTube Monitor (Videos + Channels)...")
    print(f"📋 Discord Token: {'✅ Set' if DISCORD_TOKEN else '❌ Not set'}")
    print(f"📋 YouTube API Key: {'✅ Set' if YOUTUBE_API_KEY else '❌ Not set'}")
    print(f"📋 D1 Worker URL: {'✅ Set' if D1_WORKER_URL else '❌ Not set'}")
    print(f"📋 D1 API Key: {'✅ Set' if D1_API_KEY else '❌ Not set'}")
    
    start_keep_alive()
    print(f"✅ Web server started on port {PORT}")
    
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid Discord token")
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")