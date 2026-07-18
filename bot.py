"""
YouTube Video Monitor — Discord Bot (FIXED)
--------------------------------------
Fixed listvideos command and notification issues.
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
    return "YouTube Video Monitor is running."

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

async def api_get_tracked() -> Dict:
    try:
        result = await api_request("GET", "tracked")
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
                        "start_time": row.get("start_time", datetime.now(timezone.utc).isoformat()),
                        "recovered": bool(row.get("recovered", 0)),
                        "recovered_at": row.get("recovered_at"),
                        "last_stats": json.loads(row.get("last_stats", "{}")) if row.get("last_stats") else {},
                    }
            return tracked
    except Exception as e:
        print(f"Error getting tracked: {e}")
    return {}

async def api_add_tracked(video_id: str, url: str, title: str, channel: str, start_time: str):
    await api_request("POST", "tracked", {
        "video_id": video_id,
        "url": url,
        "title": title,
        "channel": channel,
        "start_time": start_time
    })

async def api_update_stats(video_id: str, stats: Dict):
    await api_request("PATCH", f"tracked/{video_id}", {"last_stats": json.dumps(stats)})

async def api_mark_removed(video_id: str, removed_at: str):
    await api_request("PATCH", f"tracked/{video_id}", {"recovered_at": removed_at})

async def api_remove_tracked(video_id: str):
    await api_request("DELETE", f"tracked/{video_id}")

async def api_get_config() -> Dict:
    result = await api_request("GET", "config")
    return result if isinstance(result, dict) else {}

async def api_set_config(key: str, value):
    await api_request("POST", "config", {key: value})

# ---------- YouTube API ----------

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
    # If it's just a video ID (11 characters)
    if re.match(r'^[\w-]{11}$', url):
        return url
    return None

async def get_video_info(video_id: str) -> Optional[Dict]:
    """Get video info from YouTube API"""
    if not YOUTUBE_API_KEY:
        print("⚠️ YouTube API key not set")
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
                        return {
                            "video_id": video_id,
                            "title": snippet.get("title", "Unknown"),
                            "channel": snippet.get("channelTitle", "Unknown"),
                            "channel_id": snippet.get("channelId", ""),
                            "published_at": snippet.get("publishedAt", ""),
                            "views": int(stats.get("viewCount", 0)),
                            "likes": int(stats.get("likeCount", 0)),
                            "comments": int(stats.get("commentCount", 0)),
                            "url": f"https://youtube.com/watch?v={video_id}",
                            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                        }
                elif resp.status == 403:
                    print(f"YouTube API quota exceeded or invalid key")
                    return None
                else:
                    print(f"YouTube API error: {resp.status}")
                    return None
    except Exception as e:
        print(f"YouTube API error: {e}")
    
    return None

def format_number(num: int) -> str:
    """Format large numbers"""
    if num >= 1000000:
        return f"{num/1000000:.1f}M"
    elif num >= 1000:
        return f"{num/1000:.1f}K"
    return str(num)

def build_removed_embed(info: Dict, start_iso: str, old_stats: Dict = None) -> discord.Embed:
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
    
    # Add last known stats
    if old_stats:
        views = old_stats.get("views", 0)
        likes = old_stats.get("likes", 0)
        comments = old_stats.get("comments", 0)
        
        stats_text = (
            f"Views: {format_number(views)}\n"
            f"Likes: {format_number(likes)}\n"
            f"Comments: {format_number(comments)}"
        )
        embed.add_field(name="📊 Last Known Stats", value=stats_text, inline=False)
    
    # Add thumbnail
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    
    # Add channel info
    embed.add_field(
        name="📹 Channel",
        value=f"[{channel}](https://youtube.com/@{channel.replace(' ', '')})",
        inline=True
    )
    
    # Time tracking
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
            name="⏱️ Tracked For",
            value=" ".join(time_str),
            inline=True
        )
    except:
        pass
    
    embed.set_footer(text=f"Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return embed

# ---------- Background check ----------

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_tracked_videos():
    try:
        tracked = await api_get_tracked()
        if not tracked:
            return
        
        config = await api_get_config()
        channel_id = config.get("notify_channel_id")
        if not channel_id:
            return
        
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            print(f"Channel {channel_id} not found")
            return
        
        # Check each tracked video
        for video_id, meta in tracked.items():
            if meta.get("recovered"):
                continue
            
            # Check if video still exists
            info = await get_video_info(video_id)
            
            if info is None:
                # Video is removed/unavailable!
                print(f"🚨 Video removed: {video_id} - {meta.get('title', 'Unknown')}")
                
                old_stats = meta.get("last_stats", {})
                
                cached_info = {
                    "title": meta.get("title", "Unknown Video"),
                    "channel": meta.get("channel", "Unknown Channel"),
                    "url": meta.get("url", f"https://youtube.com/watch?v={video_id}"),
                    "thumbnail": old_stats.get("thumbnail", ""),
                }
                
                embed = build_removed_embed(cached_info, meta["start_time"], old_stats)
                try:
                    await channel.send(content=f"🚨 **Video Removed!**", embed=embed)
                    await api_mark_removed(video_id, datetime.now(timezone.utc).isoformat())
                    print(f"✅ Notification sent for: {video_id}")
                except Exception as e:
                    print(f"Failed to send notification: {e}")
            else:
                # Video still exists - update stats
                await api_update_stats(video_id, info)
                print(f"📊 Updated stats: {video_id}")
                
    except Exception as e:
        print(f"Error in check_tracked_videos: {e}")

@check_tracked_videos.before_loop
async def before_check():
    await bot.wait_until_ready()

# ---------- Discord commands ----------

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        check_tracked_videos.start()
        print(f"✅ Bot is ready")
        print(f"✅ Logged in as {bot.user}")
        print(f"✅ Checking every {CHECK_INTERVAL_MINUTES} minutes")
        print(f"✅ In {len(bot.guilds)} guilds")
        
        if not YOUTUBE_API_KEY:
            print("⚠️ YouTube API key not set")
    except Exception as e:
        print(f"Error in on_ready: {e}")

@bot.tree.command(name="trackvideo", description="Start tracking a YouTube video")
@app_commands.describe(url="YouTube video URL")
@is_owner()
async def trackvideo(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)
    
    try:
        video_id = extract_video_id(url)
        if not video_id:
            await interaction.followup.send("❌ Invalid YouTube URL")
            return
        
        tracked = await api_get_tracked()
        if video_id in tracked and not tracked[video_id].get("recovered"):
            await interaction.followup.send(f"⚠️ Already tracking this video")
            return
        
        info = await get_video_info(video_id)
        if not info:
            await interaction.followup.send(f"❌ Could not get video info. Is the video public?")
            return
        
        # If previously tracked, remove old entry
        if video_id in tracked:
            await api_remove_tracked(video_id)
        
        # Add to tracking
        await api_add_tracked(
            video_id,
            url,
            info["title"],
            info["channel"],
            datetime.now(timezone.utc).isoformat()
        )
        
        # Save initial stats
        await api_update_stats(video_id, info)
        
        await interaction.followup.send(
            f"⏱️ Started tracking **{info['title']}**\n"
            f"📹 Channel: {info['channel']}\n"
            f"🔗 Link: {url}\n\n"
            f"Will notify if video becomes unavailable!"
        )
    except Exception as e:
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
        
        tracked = await api_get_tracked()
        if video_id in tracked:
            await api_remove_tracked(video_id)
            await interaction.followup.send(f"✅ Stopped tracking video `{video_id}`")
        else:
            await interaction.followup.send(f"❌ Video not being tracked")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="listvideos", description="List all tracked videos")
@is_owner()
async def listvideos(interaction: discord.Interaction):
    try:
        tracked = await api_get_tracked()
        
        if not tracked:
            await interaction.response.send_message("📭 Nothing is being tracked")
            return
        
        # Separate active and removed
        active = []
        removed = []
        
        for video_id, meta in tracked.items():
            title = meta.get("title", "Unknown")[:50]
            if meta.get("recovered"):
                removed.append(f"`{video_id}` — {title} ✅")
            else:
                active.append(f"`{video_id}` — {title} ⏳")
        
        # Build response
        lines = ["**📊 Tracked Videos:**"]
        lines.append(f"Active: {len(active)} | Removed: {len(removed)}")
        lines.append("─" * 30)
        
        if active:
            lines.append("\n**Currently Tracking:**")
            lines.extend(active[:10])  # Show first 10
            if len(active) > 10:
                lines.append(f"*...and {len(active) - 10} more*")
        
        if removed:
            lines.append("\n**Removed Videos:**")
            lines.extend(removed[:5])  # Show first 5
            if len(removed) > 5:
                lines.append(f"*...and {len(removed) - 5} more*")
        
        # Send response (split if too long)
        response = "\n".join(lines)
        if len(response) > 2000:
            # Split into chunks
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
        await interaction.response.send_message(f"❌ Error getting list: {e}")

@bot.tree.command(name="setchannel", description="Set notification channel")
@is_owner()
async def setchannel(interaction: discord.Interaction):
    try:
        await api_set_config("notify_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(f"✅ Notifications will be sent to {interaction.channel.mention}")
    except Exception as e:
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
                    f"Comments: {format_number(info.get('comments', 0))}"
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
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Video `{video_id}` not found")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="helpvideo", description="Show bot instructions")
@is_owner()
async def helpvideo(interaction: discord.Interaction):
    instructions = (
        "**🎬 YouTube Video Monitor - Commands:**\n\n"
        "**Tracking:**\n"
        "`/trackvideo url:URL` - Start tracking a video\n"
        "`/untrackvideo id/url` - Stop tracking\n"
        "`/listvideos` - List all tracked videos\n\n"
        "**Checking:**\n"
        "`/checkvideo id/url` - Check if video exists\n"
        "`/videostats id/url` - Get video stats\n\n"
        "**Setup:**\n"
        "`/setchannel` - Set notification channel\n"
        "`/helpvideo` - Show this help\n\n"
        "**What triggers a notification:**\n"
        "• Video deleted\n"
        "• Made private/unlisted\n"
        "• Copyright claimed\n"
        "• Blocked/removed\n\n"
        "**Stats shown:**\n"
        "• Title & Channel\n"
        "• Views, Likes, Comments\n"
        "• Thumbnail\n"
        "• How long it was tracked\n\n"
        "**Time Tracking:**\n"
        "• Shows exactly how long the video was tracked\n"
        "• Shows when the video went unavailable\n"
        "• UTC timezone for consistency"
    )
    await interaction.response.send_message(instructions)

if __name__ == "__main__":
    print("🚀 Starting YouTube Video Monitor...")
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