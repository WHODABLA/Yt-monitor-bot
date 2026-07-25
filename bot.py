"""
YouTube Monitor — Discord Bot (ADMIN + OWNER ONLY)
--------------------------------------
Tracks both YouTube videos AND channels.
Only Server Owner and Admins can use commands.
ADDED: Bulk tracking commands
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

load_dotenv()

# ============ ENVIRONMENT VARIABLES ============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
D1_WORKER_URL = os.getenv("D1_WORKER_URL", "").rstrip("/")
D1_API_KEY = os.getenv("D1_API_KEY")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
PORT = int(os.getenv("PORT", "8080"))

# Validate
if not DISCORD_TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN not set")
if not D1_WORKER_URL:
    raise SystemExit("❌ D1_WORKER_URL not set")
if not D1_API_KEY:
    raise SystemExit("❌ D1_API_KEY not set")
if not YOUTUBE_API_KEY:
    print("⚠️ YOUTUBE_API_KEY not set - limited functionality")

print(f"🚀 Starting with:")
print(f"📋 D1 Worker: {D1_WORKER_URL}")
print(f"📋 Check Interval: {CHECK_INTERVAL_MINUTES} minutes")
# ===============================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

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
                        print(f"API error {resp.status}")
                        return {}
        except Exception as e:
            print(f"API attempt {attempt + 1} failed: {e}")
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
        print(f"Error getting tracked videos: {e}")
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

async def get_channel_info(channel_id: str) -> Optional[Dict]:
    if not YOUTUBE_API_KEY:
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
    except Exception as e:
        print(f"Channel info error: {e}")
    return None

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

async def get_video_info(video_id: str) -> Optional[Dict]:
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
                            "url": f"https://youtube.com/watch?v={video_id}",
                            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                        }
    except Exception as e:
        print(f"Video info error: {e}")
    return None

def format_number(num: int) -> str:
    if num >= 1000000000:
        return f"{num/1000000000:.1f}B"
    elif num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    return str(num)

# ---------- Embeds ----------

def build_channel_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
    channel_name = info.get("channel_name", "Unknown Channel")
    channel_url = info.get("channel_url", "")
    
    embed = discord.Embed(
        title=f"🚫 Channel Banned / Removed",
        description=f"**{channel_name}**\n\nThe channel has been banned, deleted, or is no longer available.\n[Channel Link]({channel_url})",
        color=discord.Color.red(),
        url=channel_url,
    )
    
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

def build_video_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
    title = info.get("title", "Unknown Video")
    channel = info.get("channel", "Unknown Channel")
    url = info.get("url", "")
    
    embed = discord.Embed(
        title=f"🚫 Video Removed / Unavailable",
        description=f"**{title}**\nby **{channel}**\n\nThe video is no longer available or was removed.\n[Original Link]({url})",
        color=discord.Color.red(),
        url=url,
    )
    
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
    
    embed.add_field(name="📹 Channel", value=f"[{channel}](https://youtube.com/@{channel.replace(' ', '')})", inline=True)
    
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

# ---------- Background Checks ----------

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_videos():
    try:
        print("🔄 Checking videos...")
        tracked = await api_get_tracked_videos()
        if not tracked:
            return
        
        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            return
        
        channel = bot.get_channel(int(channel_id))
        if not channel:
            return
        
        for video_id, meta in tracked.items():
            if meta.get("recovered"):
                continue
            
            info = await get_video_info(video_id)
            
            if info is None:
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
                    print(f"✅ Video removed notification sent: {video_id}")
                except Exception as e:
                    print(f"Failed to send: {e}")
            else:
                await api_update_video_stats(video_id, info)
    except Exception as e:
        print(f"Video check error: {e}")

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_channels():
    try:
        print("🔄 Checking channels...")
        tracked = await api_get_tracked_channels()
        if not tracked:
            return
        
        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            return
        
        channel = bot.get_channel(int(channel_id))
        if not channel:
            return
        
        for channel_id, meta in tracked.items():
            if meta.get("recovered"):
                continue
            
            info = await get_channel_info(channel_id)
            
            if info is None:
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
                    print(f"✅ Channel removed notification sent: {channel_id}")
                except Exception as e:
                    print(f"Failed to send: {e}")
            else:
                await api_update_channel_stats(channel_id, info)
    except Exception as e:
        print(f"Channel check error: {e}")

@check_tracked_videos.before_loop
async def before_video_check():
    await bot.wait_until_ready()
    print("Video check loop started")

@check_tracked_channels.before_loop
async def before_channel_check():
    await bot.wait_until_ready()
    print("Channel check loop started")

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
        # Split by newlines
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
        
        # Build response
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
    try:
        tracked = await api_get_tracked_videos()
        if not tracked:
            await interaction.response.send_message("📭 No videos being tracked")
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
        
        await interaction.response.send_message("\n".join(lines))
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}")

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
            await interaction.followup.send(f"❌ Video not available")
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
        # Split by newlines
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
        
        # Build response
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
    try:
        tracked = await api_get_tracked_channels()
        if not tracked:
            await interaction.response.send_message("📭 No channels being tracked")
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
        
        await interaction.response.send_message("\n".join(lines))
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}")

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
    try:
        await api_set_config("notify_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(f"✅ Notifications will be sent to {interaction.channel.mention}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}")

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
        "**Bulk Tracking Example:**\n"
        "```\n/trackvideobulk urls:\nhttps://youtube.com/watch?v=VIDEO1\nhttps://youtube.com/watch?v=VIDEO2\nhttps://youtube.com/watch?v=VIDEO3\n```\n\n"
        "**Permissions:**\n"
        "• Server Owner: All commands\n"
        "• Server Admins: All commands"
    )
    await interaction.response.send_message(instructions)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 Starting YouTube Monitor (Admin + Owner Only)...")
    print(f"📋 Discord Token: {'✅ Set' if DISCORD_TOKEN else '❌ Not set'}")
    print(f"📋 YouTube API Key: {'✅ Set' if YOUTUBE_API_KEY else '❌ Not set'}")
    print(f"📋 D1 Worker URL: {'✅ Set' if D1_WORKER_URL else '❌ Not set'}")
    print(f"📋 D1 API Key: {'✅ Set' if D1_API_KEY else '❌ Not set'}")
    print("👥 Only Server Owners and Admins can use commands")
    
    start_keep_alive()
    print(f"✅ Web server started on port {PORT}")
    
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid Discord token")
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")