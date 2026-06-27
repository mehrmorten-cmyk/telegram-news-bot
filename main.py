#!/usr/bin/env python3
"""
ربات خبریابی هوشمند استان فارس — نسخه ۱
Fars Province Smart News Bot v1 — Built by Viktor

Features:
- Smart Search: Google News RSS + Google X/Twitter search
- Web RSS sources for news gathering
- Gemini AI for relevance filtering
- On-demand protocol rewriting via inline buttons (saves API cost)
- SQLite deduplication
- Designed for Render.com free tier (cron-activated)
"""

import os
import re
import json
import time
import sqlite3
import logging
import hashlib
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

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
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
PORT = int(os.environ.get("PORT", "10000"))

# Gemini AI
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Smart Search defaults
DEFAULT_KEYWORDS = os.environ.get(
    "DEFAULT_KEYWORDS",
    "استان فارس,شیراز,فارس,Fars province,Shiraz,"
    "مرودشت,جهرم,لارستان,فسا,کازرون,داراب,نی‌ریز,آباده,اقلید,سپیدان,لامرد,فیروزآباد"
).split(",")

DEFAULT_WEB_SOURCES = os.environ.get("DEFAULT_WEB_SOURCES", "").strip()

# X/Twitter search via Google (free)
ENABLE_X_SEARCH = os.environ.get("ENABLE_X_SEARCH", "1") == "1"

log = logging.getLogger("FarsNewsBot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)

# ============================================================
# Database
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS smart_keywords (
            keyword TEXT PRIMARY KEY,
            added   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS web_sources (
            name     TEXT PRIMARY KEY,
            feed_url TEXT NOT NULL,
            added    TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS seen_articles (
            link_hash TEXT PRIMARY KEY,
            link      TEXT,
            title     TEXT,
            source    TEXT,
            seen_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sent_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            link_hash  TEXT NOT NULL,
            chat_id    TEXT,
            message_id INTEGER,
            title_fa   TEXT,
            summary_fa TEXT,
            category   TEXT,
            link       TEXT,
            source     TEXT,
            sent_at    TEXT DEFAULT (datetime('now')),
            rewritten  INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    _restore_defaults()


def _restore_defaults():
    """Restore default keywords and web sources from env (survives ephemeral disk)."""
    conn = get_db()

    # Keywords
    existing = {r["keyword"] for r in conn.execute("SELECT keyword FROM smart_keywords").fetchall()}
    for kw in DEFAULT_KEYWORDS:
        kw = kw.strip()
        if kw and kw not in existing:
            conn.execute("INSERT OR IGNORE INTO smart_keywords (keyword) VALUES (?)", (kw,))

    # Web sources
    if DEFAULT_WEB_SOURCES:
        for url in DEFAULT_WEB_SOURCES.split(","):
            url = url.strip()
            if url:
                name = urlparse(url).netloc or url[:30]
                conn.execute("INSERT OR IGNORE INTO web_sources (name, feed_url) VALUES (?, ?)", (name, url))

    conn.commit()
    conn.close()
    log.info("Defaults restored from environment variables.")


def get_setting(key, default=""):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
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
# Telegram API Helpers
# ============================================================

def tg_request(method, **kwargs):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=kwargs, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            log.error(f"TG API {method} failed: {data}")
        return data
    except Exception as e:
        log.error(f"TG API {method} error: {e}")
        return {"ok": False}


def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    params = {"chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode}
    if reply_markup:
        params["reply_markup"] = reply_markup
    return tg_request("sendMessage", **params)


def edit_message(chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
    }
    if reply_markup:
        params["reply_markup"] = reply_markup
    return tg_request("editMessageText", **params)


def answer_callback(callback_query_id, text=""):
    return tg_request("answerCallbackQuery", callback_query_id=callback_query_id, text=text[:200])


def reply_to_admin(chat_id, text, reply_markup=None):
    return send_message(chat_id, text, reply_markup=reply_markup)


# ============================================================
# Google News Search
# ============================================================

def google_news_search(keyword, max_results=20):
    """Search Google News RSS for Persian keyword."""
    encoded = quote(keyword)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=fa&gl=IR&ceid=IR:fa"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        root = ElementTree.fromstring(resp.content)
        articles = []
        for item in root.findall(".//item")[:max_results]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub = item.findtext("pubDate", "")
            source = item.findtext("source", "")
            if title and link:
                articles.append({
                    "title": title, "link": link, "description": desc,
                    "pub_date": pub, "source": source,
                })
        return articles
    except Exception as e:
        log.error(f"Google News search error ({keyword}): {e}")
        return []


def google_news_search_en(keyword, max_results=15):
    """Search Google News RSS for English keyword."""
    encoded = quote(keyword)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        root = ElementTree.fromstring(resp.content)
        articles = []
        for item in root.findall(".//item")[:max_results]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub = item.findtext("pubDate", "")
            source = item.findtext("source", "")
            if title and link:
                articles.append({
                    "title": title, "link": link, "description": desc,
                    "pub_date": pub, "source": source,
                })
        return articles
    except Exception as e:
        log.error(f"Google News EN search error ({keyword}): {e}")
        return []


def google_x_search(keyword, max_results=10):
    """Search X/Twitter posts via Google News RSS.
    
    Uses site:x.com filter in Google search to find tweets.
    """
    if not ENABLE_X_SEARCH:
        return []
    encoded = quote(f"{keyword} site:x.com OR site:twitter.com")
    url = f"https://news.google.com/rss/search?q={encoded}&hl=fa&gl=IR&ceid=IR:fa"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        root = ElementTree.fromstring(resp.content)
        articles = []
        for item in root.findall(".//item")[:max_results]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            desc = item.findtext("description", "")
            pub = item.findtext("pubDate", "")
            if title and link:
                articles.append({
                    "title": title, "link": link, "description": desc,
                    "pub_date": pub, "source": "X/Twitter",
                })
        return articles
    except Exception as e:
        log.error(f"Google X search error ({keyword}): {e}")
        return []


# ============================================================
# Web RSS Sources
# ============================================================

def clean_html(text):
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()


def check_web_source(name, feed_url):
    """Check a web RSS source and return new articles."""
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            log.warning(f"No entries in feed: {name}")
            return []

        conn = get_db()
        new_articles = []
        for entry in feed.entries[:15]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            if not link or not title:
                continue

            link_hash = hashlib.sha256(link.encode()).hexdigest()[:32]
            exists = conn.execute("SELECT 1 FROM seen_articles WHERE link_hash = ?", (link_hash,)).fetchone()
            if exists:
                continue

            desc = clean_html(entry.get("summary", entry.get("description", "")))[:300]
            new_articles.append({
                "title": title,
                "link": link,
                "description": desc,
                "source": name,
                "link_hash": link_hash,
            })

        conn.close()
        return new_articles

    except Exception as e:
        log.error(f"Web source error ({name}): {e}")
        return []


def check_all_web_sources():
    """Check all registered web RSS sources."""
    conn = get_db()
    sources = conn.execute("SELECT name, feed_url FROM web_sources").fetchall()
    conn.close()

    all_articles = []
    for src in sources:
        articles = check_web_source(src["name"], src["feed_url"])
        all_articles.extend(articles)
        time.sleep(1)

    return all_articles


# ============================================================
# Gemini AI — Relevance Filter
# ============================================================

def gemini_filter_articles(articles):
    """Use Gemini AI to filter articles relevant to Fars province."""
    if not GEMINI_API_KEY or not articles:
        return []

    article_texts = []
    for i, art in enumerate(articles):
        desc = clean_html(art.get("description", ""))[:300]
        article_texts.append(
            f"[{i}] Title: {art['title']}\n"
            f"    Source: {art.get('source', '?')}\n"
            f"    Description: {desc}"
        )

    articles_block = "\n\n".join(article_texts)

    prompt = f"""تو یک فیلتر هوشمند خبری هستی. وظیفه تو:

۱. از بین مقالات زیر، فقط آن‌هایی را انتخاب کن که واقعاً مربوط به **استان فارس** (شهرهای شیراز، مرودشت، جهرم، لارستان، فسا، کازرون، داراب، نی‌ریز، آباده، اقلید، سپیدان، لامرد، فیروزآباد و سایر شهرهای استان فارس) هستند.

۲. برای هر مقاله مرتبط:
   - عنوان فارسی بنویس (ترجمه اگر انگلیسی است)
   - خلاصه ۲-۳ جمله‌ای فارسی بنویس
   - دسته‌بندی کن: سیاسی / اجتماعی / اقتصادی / فرهنگی / حوادث / ورزشی / سلامت / محیط‌زیست

۳. مقالاتی که فقط به «خبرگزاری فارس» اشاره دارند ولی درباره استان فارس نیستند را رد کن.
۴. مقالات ملی/بین‌المللی که ربطی به استان فارس ندارند را رد کن.

مقالات:
{articles_block}

پاسخ JSON:
{{
  "results": [
    {{
      "index": 0,
      "is_relevant": true,
      "title_fa": "عنوان فارسی",
      "summary_fa": "خلاصه فارسی ۲-۳ جمله",
      "category": "دسته‌بندی"
    }}
  ]
}}

اگر هیچ مقاله مرتبطی نیست: {{"results": []}}"""

    try:
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        resp = requests.post(
            api_url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 4096,
                    "responseMimeType": "application/json",
                },
            },
            timeout=30,
        )
        if resp.status_code != 200:
            log.error(f"Gemini filter error: {resp.status_code} — {resp.text[:300]}")
            return []

        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)

        relevant = []
        for item in result.get("results", []):
            if item.get("is_relevant", False):
                idx = item.get("index", -1)
                if 0 <= idx < len(articles):
                    relevant.append({
                        "title_fa": item.get("title_fa", articles[idx]["title"]),
                        "summary_fa": item.get("summary_fa", ""),
                        "category": item.get("category", ""),
                        "link": articles[idx]["link"],
                        "source": articles[idx].get("source", ""),
                        "original_title": articles[idx]["title"],
                        "link_hash": articles[idx].get("link_hash") or hashlib.sha256(articles[idx]["link"].encode()).hexdigest()[:32],
                    })
        return relevant

    except Exception as e:
        log.error(f"Gemini filter error: {e}")
        return []


# ============================================================
# Gemini AI — Protocol Rewriting (On-Demand)
# ============================================================

PROTOCOL_PROMPT = """تو یک نویسنده خبری حرفه‌ای در فرهنگ مقاومت ایران هستی. وظیفه تو بازنویسی اخبار طبق پروتکل زیر است:

**واژگان مجاز (حتماً استفاده کن):**
- رژیم ولایت فقیه (نه حکومت اسلامی)
- قیام ۵۷ (نه انقلاب اسلامی)
- قرارگاه اشرف (نه اردوگاه اشرف)
- خمینی (نه امام خمینی)
- مجاهدین (نه منافقین)
- فروغ جاویدان (نه مرصاد)
- خامنه‌ای جلاد (نه رهبر انقلاب)
- عوامل و مهره‌های رژیم (نه مسئولین)
- شعار اصلی: «زن، مقاومت، آزادی» (نه «زن، زندگی، آزادی»)

**واژگان ممنوعه (هرگز استفاده نکن):**
حکومت اسلامی، انقلاب اسلامی، اردوگاه اشرف، امام خمینی، منافقین، عملیات مرصاد، آیت‌الله خامنه‌ای، رهبر انقلاب، مسئولین، شعار «زن، زندگی، آزادی»

**پروتکل امنیت اینستاگرام:**
- بجای «می‌جنگیم/جنگ» ← «ایستادگی می‌کنیم»، «به پیش می‌رویم»
- بجای «کشتن/خون» ← «از میان برداشتن موانع»، «هزینه دادن»
- لحن: برنده، خبری، امیدوارکننده

**تحلیل چندلایه:**
۱. لایه بین‌المللی: اثرگذاری تحولات خارجی
۲. لایه داخلی: بحران‌های درونی رژیم
۳. لایه قیام: نقش کانون‌های شورشی و مقاومت

**منابع ممنوعه (اگر منبع خبر از اینهاست، ذکر نکن):**
habilian.ir, bbc.com/persian, fa.wikipedia.org, nejatngo.org, ensani.ir, irdc.ir"""


def gemini_rewrite_single(title_fa, summary_fa, link, source, category):
    """Rewrite a single article per strategic protocol using Gemini.
    
    Returns dict with title_rewritten, body_rewritten, hashtags or None on failure.
    """
    if not GEMINI_API_KEY:
        return None

    prompt = f"""{PROTOCOL_PROMPT}

حالا این خبر را بازنویسی کن:

عنوان: {title_fa}
خلاصه: {summary_fa}
منبع: {source}
دسته: {category}
لینک: {link}

پاسخ JSON:
{{
  "title_rewritten": "عنوان بازنویسی‌شده (کوتاه و جذاب، حداکثر ۱۵ کلمه)",
  "body_rewritten": "متن بازنویسی‌شده (۳-۵ جمله، با فرهنگ مقاومت، لحن ایمن اینستاگرام، بدون واژگان ممنوعه)",
  "hashtags": ["هشتگ۱", "هشتگ۲", "هشتگ۳"]
}}"""

    try:
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        resp = requests.post(
            api_url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "application/json",
                },
            },
            timeout=30,
        )
        if resp.status_code != 200:
            log.error(f"Gemini rewrite error: {resp.status_code}")
            return None

        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
        hashtags = result.get("hashtags", [])
        return {
            "title_rewritten": result.get("title_rewritten", title_fa),
            "body_rewritten": result.get("body_rewritten", summary_fa),
            "hashtags": " ".join(f"#{h.replace('#', '')}" for h in hashtags) if hashtags else "",
        }

    except Exception as e:
        log.error(f"Gemini rewrite error: {e}")
        return None


# ============================================================
# Smart Search — Orchestration
# ============================================================

def check_smart_sources():
    """Run smart search: gather from Google News + X + Web RSS → filter with Gemini → send to channel."""
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set — smart search skipped.")
        return 0

    paused = get_setting("paused", "0") == "1"
    if paused:
        log.info("Smart search paused.")
        return 0

    conn = get_db()
    keywords = [r["keyword"] for r in conn.execute("SELECT keyword FROM smart_keywords").fetchall()]
    conn.close()

    if not keywords:
        log.info("No smart keywords configured.")
        return 0

    log.info(f"Smart search starting with {len(keywords)} keywords...")

    # 1. Gather articles from Google News (Persian + English)
    all_articles = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        # Detect language
        is_english = all(ord(c) < 128 or c in " -_" for c in kw)
        if is_english:
            articles = google_news_search_en(kw, max_results=15)
        else:
            articles = google_news_search(kw, max_results=15)
        all_articles.extend(articles)

        # Also search X/Twitter via Google
        if ENABLE_X_SEARCH:
            x_articles = google_x_search(kw, max_results=5)
            all_articles.extend(x_articles)

        time.sleep(0.5)

    # 2. Gather from web RSS sources
    web_articles = check_all_web_sources()
    all_articles.extend(web_articles)

    # 3. Deduplicate by link
    seen_links = set()
    unique = []
    for art in all_articles:
        link = art.get("link", "")
        if not link or link in seen_links:
            continue
        seen_links.add(link)
        art["link_hash"] = hashlib.sha256(link.encode()).hexdigest()[:32]
        unique.append(art)

    log.info(f"Collected {len(all_articles)} total, {len(unique)} unique articles.")

    if not unique:
        return 0

    # 4. Remove already-seen articles
    conn = get_db()
    new_articles = []
    for art in unique:
        exists = conn.execute("SELECT 1 FROM seen_articles WHERE link_hash = ?", (art["link_hash"],)).fetchone()
        if not exists:
            new_articles.append(art)
    conn.close()

    log.info(f"{len(new_articles)} new articles after dedup.")

    if not new_articles:
        return 0

    # 5. Gemini filter in batches of 15
    total_sent = 0
    for i in range(0, len(new_articles), 15):
        batch = new_articles[i:i + 15]
        relevant = gemini_filter_articles(batch)

        log.info(f"Batch {i // 15 + 1}: {len(relevant)} relevant out of {len(batch)}")

        conn = get_db()
        for art in relevant:
            # Send raw article with inline rewrite button
            result = send_article_with_button(art)

            # Mark as seen
            conn.execute(
                "INSERT OR IGNORE INTO seen_articles (link_hash, link, title, source) VALUES (?, ?, ?, ?)",
                (art["link_hash"], art["link"], art.get("title_fa", ""), art.get("source", "")),
            )

            if result:
                total_sent += 1

        # Mark filtered-out articles as seen too (no duplicates)
        relevant_hashes = {a["link_hash"] for a in relevant}
        for art in batch:
            if art["link_hash"] not in relevant_hashes:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_articles (link_hash, link, title, source) VALUES (?, ?, ?, ?)",
                    (art["link_hash"], art["link"], art.get("title", ""), art.get("source", "")),
                )

        conn.commit()
        conn.close()

        if i + 15 < len(new_articles):
            time.sleep(2)

    set_setting("last_check", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    log.info(f"Smart search done. Sent {total_sent} articles.")
    return total_sent


def send_article_with_button(art):
    """Send a raw article to the channel with an inline 'rewrite' button."""
    category_emoji = {
        "سیاسی": "🏛", "اجتماعی": "👥", "اقتصادی": "💰",
        "فرهنگی": "🎭", "حوادث": "🚨", "ورزشی": "⚽",
        "سلامت": "🏥", "محیط‌زیست": "🌿",
    }
    cat = art.get("category", "")
    emoji = category_emoji.get(cat, "📰")
    source = art.get("source", "")
    link = art["link"]
    title = art.get("title_fa", "")
    summary = art.get("summary_fa", "")
    link_hash = art.get("link_hash", "")

    caption = (
        f"🔍 <b>خبر استان فارس</b>\n"
        f"{emoji} <b>{cat}</b> | 🌐 {source}\n\n"
        f"<b>{title}</b>\n\n"
        f"{summary}\n\n"
        f'🔗 <a href="{link}">مشاهده خبر کامل</a>'
    )

    # Inline keyboard with rewrite button
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "📝 بازنویسی با پروتکل مقاومت",
                    "callback_data": f"rewrite:{link_hash}",
                }
            ]
        ]
    }

    result = send_message(CHANNEL_ID, caption, reply_markup=reply_markup)

    # Save to sent_messages for later rewriting
    if result.get("ok"):
        msg_id = result["result"]["message_id"]
        conn = get_db()
        conn.execute(
            "INSERT INTO sent_messages (link_hash, chat_id, message_id, title_fa, summary_fa, category, link, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (link_hash, CHANNEL_ID, msg_id, title, summary, cat, link, source),
        )
        conn.commit()
        conn.close()
        return True

    return False


# ============================================================
# Callback Handler (Inline Button Press)
# ============================================================

def handle_callback(callback_query):
    """Handle inline button presses (rewrite, etc)."""
    data = callback_query.get("data", "")
    callback_id = callback_query.get("id", "")
    message = callback_query.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id", 0)
    user_id = str(callback_query.get("from", {}).get("id", ""))

    # Only admins can press buttons
    if user_id not in ADMIN_CHAT_IDS:
        answer_callback(callback_id, "⛔ دسترسی ندارید.")
        return

    if data.startswith("rewrite:"):
        link_hash = data[8:]
        handle_rewrite_callback(callback_id, chat_id, message_id, link_hash)
    else:
        answer_callback(callback_id, "❓ دستور ناشناخته")


def handle_rewrite_callback(callback_id, chat_id, message_id, link_hash):
    """Handle rewrite button press — rewrite article with protocol."""
    # Acknowledge immediately
    answer_callback(callback_id, "⏳ در حال بازنویسی با پروتکل مقاومت...")

    # Fetch original article from DB
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM sent_messages WHERE link_hash = ? ORDER BY sent_at DESC LIMIT 1",
        (link_hash,),
    ).fetchone()
    conn.close()

    if not row:
        edit_message(chat_id, message_id, "❌ مقاله یافت نشد.")
        return

    if row["rewritten"]:
        # Already rewritten — just notify
        answer_callback(callback_id, "✅ قبلاً بازنویسی شده.")
        return

    # Call Gemini for rewriting
    result = gemini_rewrite_single(
        row["title_fa"], row["summary_fa"], row["link"], row["source"], row["category"],
    )

    if not result:
        edit_message(
            chat_id, message_id,
            f"❌ خطا در بازنویسی. متن اصلی:\n\n"
            f"<b>{row['title_fa']}</b>\n\n{row['summary_fa']}\n\n"
            f'🔗 <a href="{row["link"]}">مشاهده خبر کامل</a>',
        )
        return

    category_emoji = {
        "سیاسی": "🏛", "اجتماعی": "👥", "اقتصادی": "💰",
        "فرهنگی": "🎭", "حوادث": "🚨", "ورزشی": "⚽",
        "سلامت": "🏥", "محیط‌زیست": "🌿",
    }
    cat = row["category"] or ""
    emoji = category_emoji.get(cat, "📰")
    hashtags = result.get("hashtags", "")

    rewritten_caption = (
        f"✊ <b>خبر استان فارس — بازنویسی مقاومت</b>\n"
        f"{emoji} <b>{cat}</b> | 🌐 {row['source']}\n\n"
        f"<b>{result['title_rewritten']}</b>\n\n"
        f"{result['body_rewritten']}"
    )
    if hashtags:
        rewritten_caption += f"\n\n{hashtags}"
    rewritten_caption += f'\n\n🔗 <a href="{row["link"]}">مشاهده خبر کامل</a>'

    # Edit the message — remove the button
    edit_message(chat_id, message_id, rewritten_caption)

    # Mark as rewritten
    conn = get_db()
    conn.execute("UPDATE sent_messages SET rewritten = 1 WHERE link_hash = ?", (link_hash,))
    conn.commit()
    conn.close()

    log.info(f"Rewritten article: {row['title_fa'][:50]}")


# ============================================================
# Cleanup
# ============================================================

def cleanup_old_records():
    """Clean up old records (>7 days) to keep DB small."""
    conn = get_db()
    conn.execute("DELETE FROM seen_articles WHERE seen_at < datetime('now', '-7 days')")
    conn.execute("DELETE FROM sent_messages WHERE sent_at < datetime('now', '-7 days')")
    conn.commit()
    conn.close()
    log.info("Old records cleaned up.")


# ============================================================
# Bot Commands
# ============================================================

def is_admin(chat_id):
    return str(chat_id) in ADMIN_CHAT_IDS


def handle_command(chat_id, text):
    """Handle bot commands from admin."""
    global ENABLE_X_SEARCH, CHANNEL_ID, CHECK_INTERVAL

    if not is_admin(chat_id):
        send_message(chat_id, "⛔ فقط ادمین‌ها می‌توانند از این ربات استفاده کنند.")
        return

    parts = text.strip().split(maxsplit=1)
    command = parts[0].lower().split("@")[0]  # Remove @botname
    arg = parts[1].strip() if len(parts) > 1 else ""

    conn = get_db()

    try:
        # ---- Search Commands ----
        if command == "/start":
            reply_to_admin(chat_id,
                "🔍 <b>ربات خبریابی هوشمند استان فارس</b>\n\n"
                "این ربات به‌صورت خودکار اخبار استان فارس را از گوگل نیوز، "
                "ایکس/توییتر و وب‌سایت‌ها جمع‌آوری می‌کند.\n\n"
                "برای مشاهده دستورات: /help"
            )

        elif command == "/help":
            reply_to_admin(chat_id,
                "📖 <b>راهنمای دستورات</b>\n\n"
                "<b>🔍 جستجو و بررسی:</b>\n"
                "/check — بررسی فوری اخبار جدید\n"
                "/status — وضعیت کلی ربات\n"
                "/pause — توقف جستجوی خودکار\n"
                "/resume — ادامه جستجوی خودکار\n"
                "/clear — پاک کردن تاریخچه\n\n"
                "<b>🔑 کلمات کلیدی:</b>\n"
                "/addkeyword کلمه — اضافه کردن\n"
                "/removekeyword کلمه — حذف\n"
                "/listkeywords — لیست\n\n"
                "<b>🌐 منابع وب (RSS):</b>\n"
                "/addweb نام URL — اضافه کردن\n"
                "/removeweb نام — حذف\n"
                "/listweb — لیست\n\n"
                "<b>🐦 ایکس/توییتر:</b>\n"
                "/xon — فعال کردن جستجوی ایکس\n"
                "/xoff — غیرفعال کردن جستجوی ایکس\n\n"
                "<b>📝 پروتکل مقاومت:</b>\n"
                "دکمه «📝 بازنویسی» زیر هر خبر\n\n"
                "<b>👤 مدیریت:</b>\n"
                "/addadmin شناسه — اضافه کردن ادمین\n"
                "/set_channel شناسه — تغییر کانال\n"
                "/set_interval ثانیه — تغییر فاصله بررسی"
            )

        elif command == "/check":
            reply_to_admin(chat_id, "🔍 در حال بررسی اخبار جدید...")
            threading.Thread(target=_run_check_and_report, args=(chat_id,), daemon=True).start()

        elif command == "/status":
            kw_count = conn.execute("SELECT COUNT(*) as c FROM smart_keywords").fetchone()["c"]
            web_count = conn.execute("SELECT COUNT(*) as c FROM web_sources").fetchone()["c"]
            seen_count = conn.execute("SELECT COUNT(*) as c FROM seen_articles").fetchone()["c"]
            sent_count = conn.execute("SELECT COUNT(*) as c FROM sent_messages").fetchone()["c"]
            rewritten_count = conn.execute("SELECT COUNT(*) as c FROM sent_messages WHERE rewritten = 1").fetchone()["c"]
            paused = get_setting("paused", "0") == "1"
            last_check = get_setting("last_check", "هنوز بررسی نشده")
            x_enabled = ENABLE_X_SEARCH
            gemini = "✅" if GEMINI_API_KEY else "❌"

            reply_to_admin(chat_id,
                f"📊 <b>وضعیت ربات خبریابی فارس</b>\n\n"
                f"🔍 جستجوی خودکار: {'⏸ متوقف' if paused else '▶️ فعال'}\n"
                f"🕐 آخرین بررسی: {last_check}\n"
                f"⏱ فاصله بررسی: {CHECK_INTERVAL} ثانیه\n\n"
                f"🔑 کلمات کلیدی: {kw_count}\n"
                f"🌐 منابع وب: {web_count}\n"
                f"🐦 جستجوی ایکس: {'✅ فعال' if x_enabled else '❌ غیرفعال'}\n"
                f"🤖 Gemini AI: {gemini}\n\n"
                f"📰 اخبار دیده‌شده: {seen_count}\n"
                f"📤 اخبار ارسال‌شده: {sent_count}\n"
                f"📝 بازنویسی‌شده: {rewritten_count}\n\n"
                f"🆔 کانال مقصد: {CHANNEL_ID}"
            )

        elif command == "/pause":
            set_setting("paused", "1")
            reply_to_admin(chat_id, "⏸ جستجوی خودکار متوقف شد.")

        elif command == "/resume":
            set_setting("paused", "0")
            reply_to_admin(chat_id, "▶️ جستجوی خودکار ادامه یافت.")

        elif command == "/clear":
            conn.execute("DELETE FROM seen_articles")
            conn.execute("DELETE FROM sent_messages")
            conn.commit()
            reply_to_admin(chat_id, "🗑 تاریخچه پاک شد.")

        # ---- Keyword Commands ----
        elif command == "/addkeyword":
            if not arg:
                reply_to_admin(chat_id, "⚠️ لطفاً کلمه کلیدی وارد کنید.\nمثال: /addkeyword فیروزآباد")
                return
            conn.execute("INSERT OR IGNORE INTO smart_keywords (keyword) VALUES (?)", (arg,))
            conn.commit()
            reply_to_admin(chat_id, f"✅ کلمه کلیدی اضافه شد: <b>{arg}</b>")

        elif command == "/removekeyword":
            if not arg:
                reply_to_admin(chat_id, "⚠️ لطفاً کلمه کلیدی وارد کنید.")
                return
            conn.execute("DELETE FROM smart_keywords WHERE keyword = ?", (arg,))
            conn.commit()
            reply_to_admin(chat_id, f"🗑 کلمه کلیدی حذف شد: <b>{arg}</b>")

        elif command == "/listkeywords":
            rows = conn.execute("SELECT keyword FROM smart_keywords ORDER BY keyword").fetchall()
            if not rows:
                reply_to_admin(chat_id, "📭 هیچ کلمه کلیدی تنظیم نشده.")
                return
            kw_list = "\n".join(f"  • {r['keyword']}" for r in rows)
            reply_to_admin(chat_id, f"🔑 <b>کلمات کلیدی ({len(rows)}):</b>\n{kw_list}")

        # ---- Web Source Commands ----
        elif command == "/addweb":
            if not arg or " " not in arg:
                reply_to_admin(chat_id, "⚠️ فرمت: /addweb نام URL\nمثال: /addweb mojahedin https://mojahedin.org/rss/")
                return
            parts_w = arg.split(maxsplit=1)
            name, url = parts_w[0], parts_w[1]
            conn.execute("INSERT OR REPLACE INTO web_sources (name, feed_url) VALUES (?, ?)", (name, url))
            conn.commit()
            reply_to_admin(chat_id, f"✅ منبع وب اضافه شد: <b>{name}</b>\n🔗 {url}")

        elif command == "/removeweb":
            if not arg:
                reply_to_admin(chat_id, "⚠️ لطفاً نام منبع وارد کنید.")
                return
            conn.execute("DELETE FROM web_sources WHERE name = ?", (arg,))
            conn.commit()
            reply_to_admin(chat_id, f"🗑 منبع وب حذف شد: <b>{arg}</b>")

        elif command == "/listweb":
            rows = conn.execute("SELECT name, feed_url FROM web_sources ORDER BY name").fetchall()
            if not rows:
                reply_to_admin(chat_id, "📭 هیچ منبع وبی تنظیم نشده.")
                return
            src_list = "\n".join(f"  • <b>{r['name']}</b>: {r['feed_url']}" for r in rows)
            reply_to_admin(chat_id, f"🌐 <b>منابع وب ({len(rows)}):</b>\n{src_list}")

        # ---- X/Twitter Commands ----
        elif command == "/xon":
            ENABLE_X_SEARCH = True
            reply_to_admin(chat_id, "🐦 جستجوی ایکس/توییتر فعال شد.")

        elif command == "/xoff":
            ENABLE_X_SEARCH = False
            reply_to_admin(chat_id, "🐦 جستجوی ایکس/توییتر غیرفعال شد.")

        # ---- Admin Commands ----
        elif command == "/addadmin":
            if not arg or not arg.isdigit():
                reply_to_admin(chat_id, "⚠️ فرمت: /addadmin شناسه_عددی\nشناسه خود: /myid")
                return
            if arg not in ADMIN_CHAT_IDS:
                ADMIN_CHAT_IDS.append(arg)
            reply_to_admin(chat_id, f"✅ ادمین اضافه شد: {arg}\n⚠️ بعد از ری‌استارت از بین می‌رود. ADMIN_CHAT_IDS را در Render آپدیت کنید.")

        elif command == "/myid":
            reply_to_admin(chat_id, f"🆔 شناسه شما: <code>{chat_id}</code>")

        elif command == "/set_channel":
            if not arg:
                reply_to_admin(chat_id, f"🆔 کانال فعلی: <code>{CHANNEL_ID}</code>\nبرای تغییر: /set_channel شناسه_کانال")
                return
            CHANNEL_ID = arg
            reply_to_admin(chat_id, f"✅ کانال مقصد تغییر کرد: <code>{arg}</code>\n⚠️ PERSONAL_CHANNEL_ID را در Render آپدیت کنید.")

        elif command == "/set_interval":
            if not arg or not arg.isdigit():
                reply_to_admin(chat_id, f"⏱ فاصله فعلی: {CHECK_INTERVAL} ثانیه\nبرای تغییر: /set_interval 600")
                return
            CHECK_INTERVAL = max(60, int(arg))
            reply_to_admin(chat_id, f"✅ فاصله بررسی: {CHECK_INTERVAL} ثانیه")

        elif command == "/set_webhook":
            # Auto-detect URL from request
            if arg:
                wh_url = arg.rstrip("/") + "/webhook"
            else:
                reply_to_admin(chat_id, "⚠️ فرمت: /set_webhook https://your-app.onrender.com")
                return

            resp = tg_request("setWebhook", url=wh_url)
            if resp.get("ok"):
                reply_to_admin(chat_id, f"✅ Webhook تنظیم شد:\n{wh_url}")
            else:
                reply_to_admin(chat_id, f"❌ خطا: {resp}")

        else:
            reply_to_admin(chat_id, "❓ دستور ناشناخته. برای راهنما: /help")

    finally:
        conn.close()


def _run_check_and_report(chat_id):
    """Run smart search and report results to admin."""
    try:
        count = check_smart_sources()
        if count > 0:
            reply_to_admin(chat_id, f"✅ بررسی تمام شد. {count} خبر جدید ارسال شد.")
        else:
            reply_to_admin(chat_id, "✅ بررسی تمام شد. خبر جدیدی یافت نشد.")
    except Exception as e:
        log.error(f"Check error: {e}")
        reply_to_admin(chat_id, f"❌ خطا در بررسی: {e}")


# ============================================================
# Background Checker
# ============================================================

def background_checker():
    """Background thread that runs smart search periodically."""
    log.info("Background checker started.")
    time.sleep(10)  # Wait for startup

    while True:
        try:
            paused = get_setting("paused", "0") == "1"
            if not paused:
                check_smart_sources()
                cleanup_old_records()
        except Exception as e:
            log.error(f"Background checker error: {e}")

        time.sleep(CHECK_INTERVAL)


# ============================================================
# Flask Routes
# ============================================================

@app.route("/")
def health():
    """Health check endpoint — also triggers check if needed."""
    last = get_setting("last_check", "")
    paused = get_setting("paused", "0") == "1"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Trigger check if enough time has passed
    if not paused and GEMINI_API_KEY:
        should_check = True
        if last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed < CHECK_INTERVAL:
                    should_check = False
            except ValueError:
                pass

        if should_check:
            threading.Thread(target=check_smart_sources, daemon=True).start()

    return jsonify({
        "status": "running",
        "bot": "Fars Province Smart News Bot v1",
        "time": now_str,
        "last_check": last or "never",
        "paused": paused,
        "gemini": bool(GEMINI_API_KEY),
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram webhook endpoint."""
    data = request.get_json(silent=True)
    if not data:
        return "ok"

    # Handle callback queries (button presses)
    if "callback_query" in data:
        handle_callback(data["callback_query"])
        return "ok"

    # Handle messages
    msg = data.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "")

    if text and text.startswith("/"):
        handle_command(chat_id, text)

    return "ok"


@app.route("/set_webhook")
def set_webhook_route():
    """Convenience route to set webhook via browser."""
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    proto = request.headers.get("X-Forwarded-Proto", "https")
    if host:
        wh_url = f"{proto}://{host}/webhook"
        result = tg_request("setWebhook", url=wh_url)
        return jsonify({"webhook_url": wh_url, "result": result})
    return jsonify({"error": "Could not determine host"}), 400


# ============================================================
# Startup
# ============================================================

def startup():
    log.info("=" * 50)
    log.info("Fars Province Smart News Bot v1 starting...")
    log.info("=" * 50)

    init_db()

    # Start background thread
    t = threading.Thread(target=background_checker, daemon=True)
    t.start()

    # Auto-set webhook if on Render
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        wh_url = f"{render_url}/webhook"
        result = tg_request("setWebhook", url=wh_url)
        log.info(f"Webhook set: {wh_url} → {result.get('ok')}")

    log.info(f"Listening on port {PORT}")
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    startup()
