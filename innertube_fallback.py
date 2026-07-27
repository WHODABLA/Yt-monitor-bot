"""
InnerTube fallback for YouTube data (via yt-dlp) - No API key, no quota.
--------------------------------------------------------------------------
This is used ONLY as a fallback when the official YouTube Data API
returns a "quota" status. It is NOT a replacement for the official API -
InnerTube is undocumented, unofficial, and can break or rate-limit
without warning, so the primary path always stays the official API.

Returns the exact same {"status": ..., "data": {...}} shape as the
official get_video_info_with_status / get_channel_info_with_status in
bot.py, with the same keys inside "data". This means nothing downstream
(check_tracked_videos, check_tracked_channels, embeds, commands) needs
to know or care which source the data came from.

status values (same contract as the official-API functions):
  "ok"        - found, data populated
  "not_found" - confirmed gone (private/removed/terminated/deleted)
  "blocked"   - InnerTube itself is blocking us (bot check, rate limit) -
                NOT a confirmed removal, just means "can't tell right now"
  "error"     - anything else unexpected (extractor broke, network, etc) -
                NOT a confirmed removal
"""

import yt_dlp
from typing import Optional, Dict

# Substrings (checked lowercase) that mean YouTube itself told us the
# video/channel is gone - a real removal, not an access problem.
_NOT_FOUND_PATTERNS = [
    "video unavailable",
    "this video is private",
    "private video",
    "this video has been removed",
    "video has been removed by the uploader",
    "account associated with this video has been terminated",
    "this video is no longer available",
    "this video does not exist",
    "unable to find video",
    "this channel does not exist",
    "this channel was removed",
    "the channel does not exist",
    "404",
]

# Substrings that mean InnerTube is blocking/throttling us - transient,
# never treat as a removal.
_BLOCKED_PATTERNS = [
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "http error 429",
    "too many requests",
    "rate-limit",
    "rate limit",
    "precondition failed",
    "unable to download webpage",  # frequently a soft block, not a real 404
]


def _classify_download_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if any(p in msg for p in _BLOCKED_PATTERNS):
        return "blocked"
    if any(p in msg for p in _NOT_FOUND_PATTERNS):
        return "not_found"
    return "error"


def get_video_info_with_status(video_id: str) -> Dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "ignoreerrors": False,  # we need real exceptions to classify failures
        "skip_download": True,
    }
    url = f"https://youtube.com/watch?v={video_id}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            # yt-dlp gave us nothing but didn't raise - inconclusive
            return {"status": "error", "data": None}

        data = {
            "video_id": video_id,
            "title": info.get("title", "Unknown"),
            "channel": info.get("uploader", "Unknown"),
            "channel_id": info.get("channel_id", ""),
            "published_at": info.get("upload_date", ""),
            "views": info.get("view_count", 0) or 0,
            "likes": info.get("like_count", 0) or 0,
            "comments": info.get("comment_count", 0) or 0,
            "subscribers": info.get("channel_follower_count", 0) or 0,
            "url": f"https://youtube.com/watch?v={video_id}",
            "thumbnail": info.get("thumbnail", "") or "",
        }
        return {"status": "ok", "data": data}

    except yt_dlp.utils.DownloadError as e:
        status = _classify_download_error(e)
        print(f"⚠️ [InnerTube] video {video_id} -> {status}: {e}")
        return {"status": status, "data": None}
    except Exception as e:
        print(f"⚠️ [InnerTube] video {video_id} unexpected error: {e}")
        return {"status": "error", "data": None}


def get_channel_info_with_status(channel_id_or_handle: str) -> Dict:
    if channel_id_or_handle.startswith("@"):
        channel_url = f"https://youtube.com/{channel_id_or_handle}"
    elif channel_id_or_handle.startswith("UC"):
        channel_url = f"https://youtube.com/channel/{channel_id_or_handle}"
    else:
        channel_url = f"https://youtube.com/@{channel_id_or_handle}"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "ignoreerrors": False,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)

        if not info:
            return {"status": "error", "data": None}

        channel_id = info.get("channel_id", "") or (
            channel_id_or_handle if channel_id_or_handle.startswith("UC") else ""
        )

        data = {
            "channel_id": channel_id,
            "channel_name": info.get("title", info.get("channel", "Unknown")),
            "channel_url": info.get("webpage_url", channel_url),
            "subscribers": info.get("channel_follower_count", 0) or 0,
            "videos_count": info.get("playlist_count", 0) or 0,
            "views": info.get("view_count", 0) or 0,
            "thumbnail": (info.get("thumbnails") or [{}])[-1].get("url", "") if info.get("thumbnails") else "",
            "description": (info.get("description", "") or "")[:200],
        }
        return {"status": "ok", "data": data}

    except yt_dlp.utils.DownloadError as e:
        status = _classify_download_error(e)
        print(f"⚠️ [InnerTube] channel {channel_id_or_handle} -> {status}: {e}")
        return {"status": status, "data": None}
    except Exception as e:
        print(f"⚠️ [InnerTube] channel {channel_id_or_handle} unexpected error: {e}")
        return {"status": "error", "data": None}
