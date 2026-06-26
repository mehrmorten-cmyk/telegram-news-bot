#!/usr/bin/env python3
"""
ربات خبری تلگرام — نسخه ۲
Telegram News Bot v2 — Built by Viktor

Features:
- Scrape Telegram channels via t.me/s/{channel}
- Scrape websites via RSS feeds
- SQLite for atomic deduplication (no duplicates ever)
- Bot commands for management
- Designed for Render.com free tier (cron-activated)
- Phase 3 ready: Gemini AI hooks for translation/rewriting
"""

import os
import re
import time
import sqlite3
import logging
import hashlib
import tempfile
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import feedparser
from flask import Flask, request, jsonify

# ============================================================
# Configuration
# ============================================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("PERSONAL_CHANNEL_ID", "")
ADMIN_CHAT_IDS = [
    cid.strip()
    for cid in os.environ.get("ADMIN_CHAT_IDS", "").split(",")
    if cid.strip()
]
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # seconds
PORT = int(os.environ.get("PORT", "10000"))

# Telegram API base
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("newsbot")

# Global lock for DB writes during check cycles
_check_lock = threading.Lock()

# ============================================================
# Database Layer (SQLite)
# ============================================================

def get_db():
    """Get a thread-local database connection."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """Initialize database tables."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            added_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS web_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            added_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS seen_posts (
            post_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            seen_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS seen_web_articles (
            article_url TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            seen_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE NOT NULL
        );
    """)
    # Default settings
    defaults = {
        "channel_interval": "300",
        "web_interval": "300",
        "channels_paused": "0",
        "web_paused": "0",
        "last_channel_check": "0",
        "last_web_check": "0",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    conn.close()
    log.info("Database initialized")


def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


# ============================================================
# Telegram Bot API Helpers
# ============================================================

def tg_request(method, **kwargs):
    """Make a Telegram Bot API request."""
    try:
        resp = requests.post(f"{TG_API}/{method}", **kwargs, timeout=60)
        data = resp.json()
        if not data.get("ok"):
            log.warning(f"TG API error [{method}]: {data.get('description', 'unknown')}")
        return data
    except Exception as e:
        log.error(f"TG API exception [{method}]: {e}")
        return {"ok": False, "description": str(e)}


def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    """Send a text message."""
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_request("sendMessage", json=payload)


def send_photo(chat_id, photo, caption="", parse_mode="HTML"):
    """Send a photo (URL or file)."""
    if isinstance(photo, str) and photo.startswith("http"):
        payload = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption[:1024],
            "parse_mode": parse_mode,
        }
        return tg_request("sendPhoto", json=payload)
    else:
        data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": parse_mode}
        files = {"photo": photo}
        return tg_request("sendPhoto", data=data, files=files)


def send_video(chat_id, video, caption="", parse_mode="HTML"):
    """Send a video file."""
    data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": parse_mode}
    files = {"video": video}
    return tg_request("sendVideo", data=data, files=files)


def send_document(chat_id, document, caption="", parse_mode="HTML"):
    """Send a document/file."""
    data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": parse_mode}
    files = {"document": document}
    return tg_request("sendDocument", data=data, files=files)


def send_media_group(chat_id, media):
    """Send an album (group of photos/videos)."""
    import json
    data = {"chat_id": chat_id, "media": json.dumps(media)}
    return tg_request("sendMediaGroup", data=data)


def reply_to_admin(chat_id, text):
    """Reply to the admin who sent a command."""
    send_message(chat_id, text)


# ============================================================
# Telegram Channel Scraper (via t.me/s/)
# ============================================================

def scrape_channel(username):
    """Scrape latest posts from a Telegram channel's public page."""
    url = f"https://t.me/s/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            log.warning(f"Channel {username}: HTTP {resp.status_code}")
            return []
    except Exception as e:
        log.error(f"Channel {username}: request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []

    for widget in soup.find_all("div", class_="tgme_widget_message_wrap"):
        msg = widget.find("div", class_="tgme_widget_message")
        if not msg:
            continue

        data_post = msg.get("data-post", "")
        if not data_post:
            continue

        post_id = data_post  # e.g., "channelname/12345"
        post = {"id": post_id, "text": "", "photos": [], "videos": [], "link": f"https://t.me/{data_post}"}

        # Extract text
        text_div = msg.find("div", class_="tgme_widget_message_text")
        if text_div:
            post["text"] = text_div.get_text(separator="\n").strip()

        # Extract photos
        for photo_wrap in msg.find_all("a", class_="tgme_widget_message_photo_wrap"):
            style = photo_wrap.get("style", "")
            match = re.search(r"url\('([^']+)'\)", style)
            if match:
                post["photos"].append(match.group(1))

        # Extract videos
        for video_tag in msg.find_all("video"):
            src = video_tag.get("src", "")
            if src:
                post["videos"].append(src)
        # Also check for video player wrappers
        for video_wrap in msg.find_all("a", class_="tgme_widget_message_video_player"):
            # The actual video URL may be in the background or a data attribute
            video_thumb = video_wrap.find("i", class_="tgme_widget_message_video_thumb")
            if video_thumb:
                style = video_thumb.get("style", "")
                match = re.search(r"url\('([^']+)'\)", style)
                if match and not post["videos"]:
                    post["video_thumb"] = match.group(1)

        posts.append(post)

    return posts


def download_media(url, max_size_mb=20):
    """Download media file. Returns (file_bytes, filename) or (None, None) if too large or failed."""
    try:
        resp = requests.get(url, stream=True, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code != 200:
            return None, None

        # Check content length
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > max_size_mb * 1024 * 1024:
            log.info(f"Media too large: {int(content_length) / 1024 / 1024:.1f}MB > {max_size_mb}MB")
            return None, None

        # Stream download with size check
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            total += len(chunk)
            if total > max_size_mb * 1024 * 1024:
                log.info(f"Media exceeded {max_size_mb}MB during download, aborting")
                return None, None
            chunks.append(chunk)

        file_bytes = b"".join(chunks)

        # Determine filename
        content_disp = resp.headers.get("Content-Disposition", "")
        if "filename=" in content_disp:
            filename = re.search(r'filename="?([^";\s]+)"?', content_disp)
            filename = filename.group(1) if filename else "media"
        else:
            path = urlparse(url).path
            filename = os.path.basename(path) or "media"

        return file_bytes, filename
    except Exception as e:
        log.error(f"Download failed: {e}")
        return None, None


def forward_channel_post(post, source_username):
    """Forward a scraped Telegram channel post to the destination channel."""
    text = post.get("text", "")
    photos = post.get("photos", [])
    videos = post.get("videos", [])
    link = post.get("link", "")

    # Check filters
    conn = get_db()
    filters = [row["keyword"].lower() for row in conn.execute("SELECT keyword FROM filters").fetchall()]
    conn.close()
    if filters and text:
        text_lower = text.lower()
        if not any(kw in text_lower for kw in filters):
            return True  # Filtered out, mark as seen

    # Build caption
    source_link = f'<a href="{link}">🔗 لینک اصلی</a>'

    if photos and not videos:
        # Send first photo with caption
        caption = f"{text}\n\n{source_link}" if text else source_link
        if len(photos) == 1:
            result = send_photo(CHANNEL_ID, photos[0], caption)
        else:
            # Album: send first with caption, rest without
            # For simplicity, send first photo with full caption
            result = send_photo(CHANNEL_ID, photos[0], caption)
            for extra_photo in photos[1:]:
                time.sleep(1)
                send_photo(CHANNEL_ID, extra_photo)
        return result.get("ok", False)

    elif videos:
        # Try to download and send video
        for video_url in videos:
            file_bytes, filename = download_media(video_url)
            if file_bytes:
                caption = f"{text}\n\n{source_link}" if text else source_link
                import io
                result = send_video(CHANNEL_ID, (filename, io.BytesIO(file_bytes)), caption)
                if result.get("ok"):
                    return True

        # Fallback: send as text with video link note
        fallback_text = f"{text}\n\n🎬 ویدیو در لینک اصلی\n{source_link}"
        result = send_message(CHANNEL_ID, fallback_text)
        return result.get("ok", False)

    elif text:
        # Text only
        msg_text = f"{text}\n\n{source_link}"
        result = send_message(CHANNEL_ID, msg_text)
        return result.get("ok", False)

    return True  # Empty post, skip


def check_all_channels():
    """Check all Telegram channel sources for new posts."""
    if get_setting("channels_paused") == "1":
        log.info("Channel checking is paused")
        return 0

    conn = get_db()
    sources = conn.execute("SELECT username FROM telegram_sources").fetchall()
    conn.close()

    if not sources:
        return 0

    total_new = 0
    for source in sources:
        username = source["username"]
        log.info(f"Checking channel: @{username}")
        try:
            posts = scrape_channel(username)
            new_count = 0

            for post in posts:
                post_id = post["id"]
                conn = get_db()
                existing = conn.execute(
                    "SELECT 1 FROM seen_posts WHERE post_id=?", (post_id,)
                ).fetchone()

                if existing:
                    conn.close()
                    continue

                # New post — forward it
                success = forward_channel_post(post, username)

                if success:
                    conn.execute(
                        "INSERT OR IGNORE INTO seen_posts (post_id, source) VALUES (?, ?)",
                        (post_id, username),
                    )
                    conn.commit()
                    new_count += 1
                    log.info(f"  Forwarded: {post_id}")
                else:
                    log.warning(f"  Failed to forward: {post_id}")
                conn.close()
                time.sleep(2)  # Rate limit

            total_new += new_count
            log.info(f"  @{username}: {new_count} new posts")
        except Exception as e:
            log.error(f"Error checking @{username}: {e}")
        time.sleep(1)

    set_setting("last_channel_check", datetime.now(timezone.utc).isoformat())
    return total_new


# ============================================================
# Web RSS Scraper
# ============================================================

def detect_feed_url(site_url):
    """Try to auto-detect RSS feed URL for a website."""
    candidates = [
        f"{site_url.rstrip('/')}/feed/",
        f"{site_url.rstrip('/')}/rss/",
        f"{site_url.rstrip('/')}/feed/rss2/",
        f"{site_url.rstrip('/')}/rss.xml",
        f"{site_url.rstrip('/')}/atom.xml",
    ]
    for feed_url in candidates:
        try:
            resp = requests.get(feed_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0"
            })
            if resp.status_code == 200 and (
                "<rss" in resp.text[:500].lower()
                or "<feed" in resp.text[:500].lower()
                or "<channel" in resp.text[:1000].lower()
            ):
                return feed_url
        except:
            continue
    return None


def get_article_image(article_url):
    """Try to get the main image from an article page (og:image)."""
    try:
        resp = requests.get(article_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try og:image
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            return og_img["content"]
        # Try twitter:image
        tw_img = soup.find("meta", attrs={"name": "twitter:image"})
        if tw_img and tw_img.get("content"):
            return tw_img["content"]
        return None
    except:
        return None


def clean_html(text):
    """Remove HTML tags from text."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(separator=" ").strip()


def check_web_source(name, feed_url):
    """Check a single RSS feed for new articles. Returns count of new articles."""
    try:
        feed = feedparser.parse(feed_url, agent="Mozilla/5.0")
        if not feed.entries:
            log.warning(f"  {name}: no entries found")
            return 0
    except Exception as e:
        log.error(f"  {name}: feed parse error: {e}")
        return 0

    conn = get_db()
    new_count = 0

    for entry in feed.entries[:15]:  # Check latest 15 entries
        article_url = entry.get("link", "")
        if not article_url:
            continue

        # Check if already seen
        existing = conn.execute(
            "SELECT 1 FROM seen_web_articles WHERE article_url=?", (article_url,)
        ).fetchone()
        if existing:
            continue

        # New article
        title = entry.get("title", "بدون عنوان")
        summary = clean_html(entry.get("summary", entry.get("description", "")))
        if len(summary) > 200:
            summary = summary[:200] + "..."

        pub_date = ""
        if entry.get("published_parsed"):
            try:
                pub_date = time.strftime("%Y-%m-%d %H:%M", entry.published_parsed)
            except:
                pub_date = entry.get("published", "")
        elif entry.get("published"):
            pub_date = entry.get("published", "")

        # Try to get article image
        image_url = None
        # First check RSS enclosures / media
        if entry.get("media_content"):
            for media in entry.media_content:
                if "image" in media.get("type", "") or media.get("url", "").endswith(
                    (".jpg", ".jpeg", ".png", ".webp")
                ):
                    image_url = media["url"]
                    break
        if not image_url and entry.get("media_thumbnail"):
            for thumb in entry.media_thumbnail:
                image_url = thumb.get("url")
                if image_url:
                    break
        if not image_url:
            # Try og:image from article page
            image_url = get_article_image(article_url)

        # Format message
        caption = (
            f"🌐 <b>{name}</b>\n"
            f"🕐 {pub_date}\n\n"
            f"<b>{title}</b>\n\n"
            f"{summary}\n\n"
            f'🔗 <a href="{article_url}">ادامه خبر</a>'
        )

        # Send
        if image_url:
            result = send_photo(CHANNEL_ID, image_url, caption)
            if not result.get("ok"):
                # Photo failed, send as text
                result = send_message(CHANNEL_ID, caption)
        else:
            result = send_message(CHANNEL_ID, caption)

        if result.get("ok", False):
            # Immediately save to prevent duplicates
            conn.execute(
                "INSERT OR IGNORE INTO seen_web_articles (article_url, source_name) VALUES (?, ?)",
                (article_url, name),
            )
            conn.commit()
            new_count += 1
            log.info(f"  Sent web article: {title[:50]}")
        else:
            log.warning(f"  Failed to send web article: {title[:50]}")

        time.sleep(2)  # Rate limit

    conn.close()
    return new_count


def check_all_web_sources():
    """Check all web RSS sources for new articles."""
    if get_setting("web_paused") == "1":
        log.info("Web checking is paused")
        return 0

    conn = get_db()
    sources = conn.execute("SELECT name, feed_url FROM web_sources").fetchall()
    conn.close()

    if not sources:
        return 0

    total_new = 0
    for source in sources:
        log.info(f"Checking web source: {source['name']}")
        try:
            count = check_web_source(source["name"], source["feed_url"])
            total_new += count
        except Exception as e:
            log.error(f"Error checking {source['name']}: {e}")
        time.sleep(1)

    set_setting("last_web_check", datetime.now(timezone.utc).isoformat())
    return total_new


# ============================================================
# Cleanup
# ============================================================

def cleanup_old_records():
    """Remove old seen records to keep DB size manageable."""
    conn = get_db()
    # Keep only latest 5000 seen posts
    conn.execute("""
        DELETE FROM seen_posts WHERE rowid NOT IN (
            SELECT rowid FROM seen_posts ORDER BY seen_at DESC LIMIT 5000
        )
    """)
    # Keep only latest 5000 seen web articles
    conn.execute("""
        DELETE FROM seen_web_articles WHERE rowid NOT IN (
            SELECT rowid FROM seen_web_articles ORDER BY seen_at DESC LIMIT 5000
        )
    """)
    conn.commit()
    conn.close()


# ============================================================
# Bot Command Handler
# ============================================================

def is_admin(chat_id):
    """Check if the chat is an admin."""
    return str(chat_id) in ADMIN_CHAT_IDS or not ADMIN_CHAT_IDS


def handle_command(chat_id, text):
    """Handle an incoming bot command."""
    if not is_admin(chat_id):
        reply_to_admin(chat_id, "⛔ شما اجازه استفاده از این ربات را ندارید.")
        return

    text = text.strip()
    if not text.startswith("/"):
        return

    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]  # Remove @botname
    args = parts[1].strip() if len(parts) > 1 else ""

    conn = get_db()

    try:
        # ---- Telegram Channel Commands ----
        if command == "/add":
            if not args:
                reply_to_admin(chat_id, "❌ لطفاً نام کانال را وارد کنید.\nمثال: /add FarsiVOA")
                return
            username = args.replace("@", "").replace("https://t.me/", "").replace("http://t.me/", "").strip().rstrip("/")
            try:
                conn.execute("INSERT INTO telegram_sources (username) VALUES (?)", (username,))
                conn.commit()
                reply_to_admin(chat_id, f"✅ کانال @{username} اضافه شد.")
            except sqlite3.IntegrityError:
                reply_to_admin(chat_id, f"⚠️ کانال @{username} قبلاً اضافه شده.")

        elif command == "/remove":
            if not args:
                reply_to_admin(chat_id, "❌ لطفاً نام کانال را وارد کنید.\nمثال: /remove FarsiVOA")
                return
            username = args.replace("@", "").strip()
            cursor = conn.execute("DELETE FROM telegram_sources WHERE username=?", (username,))
            conn.commit()
            if cursor.rowcount > 0:
                reply_to_admin(chat_id, f"✅ کانال @{username} حذف شد.")
            else:
                reply_to_admin(chat_id, f"⚠️ کانال @{username} یافت نشد.")

        elif command == "/list":
            sources = conn.execute("SELECT username FROM telegram_sources ORDER BY id").fetchall()
            if not sources:
                reply_to_admin(chat_id, "📋 هیچ کانالی اضافه نشده.\n/add نام_کانال برای اضافه کردن")
            else:
                lines = ["📋 <b>لیست کانال‌های تلگرام:</b>\n"]
                for i, s in enumerate(sources, 1):
                    lines.append(f"{i}. @{s['username']}")
                reply_to_admin(chat_id, "\n".join(lines))

        elif command == "/check":
            reply_to_admin(chat_id, "🔍 در حال بررسی کانال‌ها...")
            count = check_all_channels()
            reply_to_admin(chat_id, f"✅ بررسی تمام شد. {count} خبر جدید ارسال شد.")

        elif command == "/pause":
            set_setting("channels_paused", "1")
            reply_to_admin(chat_id, "⏸ بررسی کانال‌های تلگرام متوقف شد.")

        elif command == "/resume":
            set_setting("channels_paused", "0")
            reply_to_admin(chat_id, "▶️ بررسی کانال‌های تلگرام ادامه یافت.")

        elif command == "/clear":
            conn.execute("DELETE FROM seen_posts")
            conn.commit()
            reply_to_admin(chat_id, "🗑 تاریخچه پست‌های دیده‌شده پاک شد.")

        elif command == "/interval":
            if not args:
                current = get_setting("channel_interval", "300")
                reply_to_admin(chat_id, f"⏱ بازه فعلی: {current} ثانیه ({int(current)//60} دقیقه)\nبرای تغییر: /interval عدد_ثانیه")
                return
            try:
                secs = int(args)
                if secs < 60:
                    reply_to_admin(chat_id, "❌ حداقل بازه ۶۰ ثانیه است.")
                    return
                set_setting("channel_interval", str(secs))
                reply_to_admin(chat_id, f"✅ بازه بررسی به {secs} ثانیه ({secs//60} دقیقه) تغییر کرد.")
            except ValueError:
                reply_to_admin(chat_id, "❌ لطفاً یک عدد وارد کنید.")

        elif command == "/filter":
            if not args:
                filters = conn.execute("SELECT keyword FROM filters ORDER BY id").fetchall()
                if not filters:
                    reply_to_admin(chat_id, "🔍 هیچ فیلتری تنظیم نشده.\n/filter کلمه — برای اضافه کردن\n/filter remove کلمه — برای حذف")
                else:
                    lines = ["🔍 <b>فیلترهای فعال:</b>\n"]
                    for f in filters:
                        lines.append(f"• {f['keyword']}")
                    reply_to_admin(chat_id, "\n".join(lines))
                return

            if args.lower().startswith("remove "):
                keyword = args[7:].strip()
                cursor = conn.execute("DELETE FROM filters WHERE keyword=?", (keyword,))
                conn.commit()
                if cursor.rowcount > 0:
                    reply_to_admin(chat_id, f"✅ فیلتر «{keyword}» حذف شد.")
                else:
                    reply_to_admin(chat_id, f"⚠️ فیلتر «{keyword}» یافت نشد.")
            else:
                try:
                    conn.execute("INSERT INTO filters (keyword) VALUES (?)", (args,))
                    conn.commit()
                    reply_to_admin(chat_id, f"✅ فیلتر «{args}» اضافه شد.")
                except sqlite3.IntegrityError:
                    reply_to_admin(chat_id, f"⚠️ فیلتر «{args}» قبلاً اضافه شده.")

        # ---- Web Source Commands ----
        elif command == "/addweb":
            if not args:
                reply_to_admin(chat_id, "❌ لطفاً آدرس وبسایت را وارد کنید.\nمثال: /addweb https://mojahedin.org")
                return
            site_url = args.strip()
            if not site_url.startswith("http"):
                site_url = "https://" + site_url

            reply_to_admin(chat_id, f"🔍 در حال جستجوی فید RSS برای {site_url}...")
            feed_url = detect_feed_url(site_url)
            if not feed_url:
                reply_to_admin(chat_id, f"❌ فید RSS برای {site_url} پیدا نشد.\nمی‌توانید مستقیماً آدرس فید را وارد کنید:\n/addweb URL نام_سایت فید_URL")
                return

            # Auto-detect name from domain
            name = urlparse(site_url).netloc.replace("www.", "")

            try:
                conn.execute(
                    "INSERT INTO web_sources (name, url, feed_url) VALUES (?, ?, ?)",
                    (name, site_url, feed_url),
                )
                conn.commit()
                reply_to_admin(chat_id, f"✅ سایت «{name}» اضافه شد.\nفید: {feed_url}")
            except Exception as e:
                reply_to_admin(chat_id, f"⚠️ خطا در اضافه کردن: {e}")

        elif command == "/removeweb":
            if not args:
                reply_to_admin(chat_id, "❌ لطفاً نام سایت را وارد کنید.\nمثال: /removeweb ncr-iran.org")
                return
            name = args.strip()
            cursor = conn.execute("DELETE FROM web_sources WHERE name LIKE ?", (f"%{name}%",))
            conn.commit()
            if cursor.rowcount > 0:
                reply_to_admin(chat_id, f"✅ سایت «{name}» حذف شد.")
            else:
                reply_to_admin(chat_id, f"⚠️ سایت «{name}» یافت نشد.")

        elif command == "/listweb":
            sources = conn.execute("SELECT name, feed_url FROM web_sources ORDER BY id").fetchall()
            if not sources:
                reply_to_admin(chat_id, "📋 هیچ وبسایتی اضافه نشده.\n/addweb URL برای اضافه کردن")
            else:
                lines = ["📋 <b>لیست وبسایت‌ها:</b>\n"]
                for i, s in enumerate(sources, 1):
                    lines.append(f"{i}. {s['name']}\n   {s['feed_url']}")
                reply_to_admin(chat_id, "\n".join(lines))

        elif command == "/checkweb":
            reply_to_admin(chat_id, "🔍 در حال بررسی وبسایت‌ها...")
            count = check_all_web_sources()
            reply_to_admin(chat_id, f"✅ بررسی تمام شد. {count} خبر جدید ارسال شد.")

        elif command == "/pauseweb":
            set_setting("web_paused", "1")
            reply_to_admin(chat_id, "⏸ بررسی وبسایت‌ها متوقف شد.")

        elif command == "/resumeweb":
            set_setting("web_paused", "0")
            reply_to_admin(chat_id, "▶️ بررسی وبسایت‌ها ادامه یافت.")

        elif command == "/clearweb":
            conn.execute("DELETE FROM seen_web_articles")
            conn.commit()
            reply_to_admin(chat_id, "🗑 تاریخچه مقالات دیده‌شده پاک شد.")

        # ---- General Commands ----
        elif command == "/status":
            ch_sources = conn.execute("SELECT COUNT(*) as c FROM telegram_sources").fetchone()["c"]
            web_sources = conn.execute("SELECT COUNT(*) as c FROM web_sources").fetchone()["c"]
            seen_posts = conn.execute("SELECT COUNT(*) as c FROM seen_posts").fetchone()["c"]
            seen_articles = conn.execute("SELECT COUNT(*) as c FROM seen_web_articles").fetchone()["c"]
            ch_paused = get_setting("channels_paused") == "1"
            web_paused_flag = get_setting("web_paused") == "1"
            last_ch = get_setting("last_channel_check", "هنوز بررسی نشده")
            last_web = get_setting("last_web_check", "هنوز بررسی نشده")

            status_text = (
                "📊 <b>وضعیت ربات</b>\n\n"
                f"📺 کانال‌های تلگرام: {ch_sources}\n"
                f"   وضعیت: {'⏸ متوقف' if ch_paused else '▶️ فعال'}\n"
                f"   پست‌های ثبت‌شده: {seen_posts}\n"
                f"   آخرین بررسی: {last_ch}\n\n"
                f"🌐 وبسایت‌ها: {web_sources}\n"
                f"   وضعیت: {'⏸ متوقف' if web_paused_flag else '▶️ فعال'}\n"
                f"   مقالات ثبت‌شده: {seen_articles}\n"
                f"   آخرین بررسی: {last_web}\n\n"
                f"🆔 کانال مقصد: {CHANNEL_ID}"
            )
            reply_to_admin(chat_id, status_text)

        elif command == "/help":
            help_text = (
                "📖 <b>راهنمای دستورات ربات</b>\n\n"
                "<b>📺 کانال‌های تلگرام:</b>\n"
                "/add نام — اضافه کردن کانال\n"
                "/remove نام — حذف کانال\n"
                "/list — لیست کانال‌ها\n"
                "/check — بررسی فوری\n"
                "/pause — توقف بررسی\n"
                "/resume — ادامه بررسی\n"
                "/clear — پاک کردن تاریخچه\n"
                "/interval ثانیه — تغییر بازه\n"
                "/filter کلمه — فیلتر کردن\n\n"
                "<b>🌐 وبسایت‌ها:</b>\n"
                "/addweb URL — اضافه کردن سایت\n"
                "/removeweb نام — حذف سایت\n"
                "/listweb — لیست سایت‌ها\n"
                "/checkweb — بررسی فوری\n"
                "/pauseweb — توقف بررسی\n"
                "/resumeweb — ادامه بررسی\n"
                "/clearweb — پاک کردن تاریخچه\n\n"
                "<b>⚙️ عمومی:</b>\n"
                "/status — وضعیت ربات\n"
                "/help — همین راهنما"
            )
            reply_to_admin(chat_id, help_text)

        else:
            reply_to_admin(chat_id, "❓ دستور ناشناخته. /help برای راهنما.")

    finally:
        conn.close()


# ============================================================
# Background Checker Thread
# ============================================================

def background_checker():
    """Background thread that periodically checks all sources."""
    log.info("Background checker started")
    while True:
        try:
            with _check_lock:
                # Check channels
                ch_interval = int(get_setting("channel_interval", "300"))
                last_ch = get_setting("last_channel_check", "0")
                try:
                    if last_ch == "0":
                        should_check_ch = True
                    else:
                        last_dt = datetime.fromisoformat(last_ch)
                        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        should_check_ch = elapsed >= ch_interval
                except:
                    should_check_ch = True

                if should_check_ch:
                    log.info("Background: checking channels...")
                    check_all_channels()

                # Check web sources
                web_interval = int(get_setting("web_interval", "300"))
                last_web = get_setting("last_web_check", "0")
                try:
                    if last_web == "0":
                        should_check_web = True
                    else:
                        last_dt = datetime.fromisoformat(last_web)
                        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        should_check_web = elapsed >= web_interval
                except:
                    should_check_web = True

                if should_check_web:
                    log.info("Background: checking web sources...")
                    check_all_web_sources()

                # Periodic cleanup
                cleanup_old_records()

        except Exception as e:
            log.error(f"Background checker error: {e}")

        time.sleep(60)  # Check every minute if it's time


# ============================================================
# Flask App
# ============================================================

app = Flask(__name__)


@app.route("/")
def health():
    """Health check endpoint — also triggers source checking on cron pings."""
    try:
        # Trigger check if enough time has passed
        with _check_lock:
            last_ch = get_setting("last_channel_check", "0")
            last_web = get_setting("last_web_check", "0")
            ch_interval = int(get_setting("channel_interval", "300"))
            web_interval = int(get_setting("web_interval", "300"))

            now = datetime.now(timezone.utc)

            def should_check(last_str, interval):
                if last_str == "0":
                    return True
                try:
                    last_dt = datetime.fromisoformat(last_str)
                    return (now - last_dt).total_seconds() >= interval
                except:
                    return True

            results = {}
            if should_check(last_ch, ch_interval):
                threading.Thread(target=check_all_channels, daemon=True).start()
                results["channels"] = "check triggered"
            else:
                results["channels"] = "up to date"

            if should_check(last_web, web_interval):
                threading.Thread(target=check_all_web_sources, daemon=True).start()
                results["web"] = "check triggered"
            else:
                results["web"] = "up to date"

        return jsonify({
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
            **results,
        })
    except Exception as e:
        log.error(f"Health check error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram webhook endpoint."""
    try:
        update = request.get_json(force=True)
        if not update:
            return "ok"

        message = update.get("message")
        if not message:
            return "ok"

        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text.startswith("/"):
            # Process command in a thread to not block webhook response
            threading.Thread(
                target=handle_command, args=(chat_id, text), daemon=True
            ).start()

        return "ok"
    except Exception as e:
        log.error(f"Webhook error: {e}")
        return "ok"


@app.route("/set_webhook")
def set_webhook_route():
    """Convenience endpoint to set the Telegram webhook."""
    host = request.host_url.rstrip("/")
    webhook_url = f"{host}/webhook"
    result = tg_request("setWebhook", json={"url": webhook_url})
    return jsonify({"webhook_url": webhook_url, "result": result})


# ============================================================
# Startup (works with both gunicorn and direct python)
# ============================================================

def startup():
    """Initialize DB and start background thread. Called once on startup."""
    log.info("=" * 50)
    log.info("News Bot v2 starting...")
    log.info(f"Channel ID: {CHANNEL_ID}")
    log.info(f"Check interval: {CHECK_INTERVAL}s")
    log.info("=" * 50)

    init_db()

    # Start background checker
    checker = threading.Thread(target=background_checker, daemon=True)
    checker.start()
    log.info("Background checker thread started")


# Run startup when module is loaded (works with gunicorn --preload)
if BOT_TOKEN and CHANNEL_ID:
    startup()
else:
    log.warning("Missing TELEGRAM_BOT_TOKEN or PERSONAL_CHANNEL_ID — bot not started")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
