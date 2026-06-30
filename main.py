#!/usr/bin/env python3
import os
import re
import time
import sqlite3
import logging
import hashlib
import threading
import json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

# ============================================================
# Configuration & Constants
# ============================================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8842107952:AAFszVHNfL331IRN1YWIi6hP9QTY4o3vhxk")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")
PORT = int(os.environ.get("PORT", "10000"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

HUB_CATEGORIES = [
    "۱. 🚨 اعتراضات و مطالبات", "۲. ⚖️ حقوق بشر و امنیتی", "۳. 🚧 خدمات شهری و زیرساخت",
    "۴. 💰 معیشت و بازار", "۵. 🏥 دارو و سلامت", "۶. 🌦 هواشناسی و جاده",
    "۷. 🎓 مدارس و دانشگاه", "۸. 💼 استخدام", "۹. 🗝 نیازمندی‌ها و دیوار",
    "۱۰. 🔍 گم‌شده‌ها", "۱۱. 🎭 فرهنگی و ورزش"
]

# لیست اتوماتیک و کامل شما
PROVINCES = {
    "fars": {
        "name": "فارس و شیراز", 
        "channel": "-1004352884396",
        "sources": [
            "akhbarfars", "shiraz_news", "YeRoozeShiraz", "sums1401", "shiraztopnews", 
            "FouriFars", "FarsFouri", "avaye_shiraz", "ostan", "shirazu_twitter", 
            "shiraz_news24", "shirazu1", "SaberinFars", "SUTimes", "LineFars", 
            "sSADP", "shorasenfi_shirazunii", "shiraz_salam", "Azad_shiraz", 
            "Shiraz_us", "Fars_today", "eghtesadefars", "fars_iau", "ub_3v", 
            "dorhamishiraziha", "News_Neyriz", "Shiraz_Fouri"
        ]
    },
    "hormozgan": {
        "name": "هرمزگان و بندرعباس", 
        "channel": "-1003915149928",
        "sources": [
            "hormozgan_online", "bndonline", "bandarabbasnews", "akhbar_hormozgan", 
            "hormozgan_today", "bandar_news", "bnd_wall", "bnd_job"
        ]
    }
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("SmartViktor")
_db_lock = threading.Lock()

# ============================================================
# Database Layer (SQLite)
# ============================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with _db_lock:
        conn = get_db()
        # جدول ویکتور برای آیدی پست ها
        conn.execute("CREATE TABLE IF NOT EXISTS seen_posts (post_id TEXT PRIMARY KEY, source TEXT NOT NULL, seen_at TEXT DEFAULT (datetime('now')))")
        # جدول جدید برای جلوگیری از محتوای تکراری
        conn.execute("CREATE TABLE IF NOT EXISTS seen_contents (hash TEXT PRIMARY KEY)")
        # جدول جدید برای کارکرد دکمه بازنویسی
        conn.execute("CREATE TABLE IF NOT EXISTS msg_logs_v2 (hash TEXT PRIMARY KEY, channel_id TEXT, msg_id TEXT, title TEXT, prov TEXT, type TEXT)")
        conn.commit()
        conn.close()

# ============================================================
# AI & Content Hashing
# ============================================================
def get_content_hash(text):
    """تولید کد یکتا از متن برای جلوگیری از خبر تکراری در کانال‌های مختلف"""
    if not text: return "empty"
    clean_text = "".join(re.sub(r'[^\w]', '', text[:100]).split())
    return hashlib.md5(clean_text.encode('utf-8')).hexdigest()

def clean_html(text):
    if not text: return ""
    return text.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")

def ai_curator(text, province):
    if not GEMINI_API_KEY: return HUB_CATEGORIES[0], "گزارش جدید"
    prompt = f"سردبیر {province} باش. متن را بررسی کن. اگر مربوط به این استان نیست کلمه NO را برگردان. وگرنه یک دسته انتخاب کن و یک تیتر ۶ کلمه‌ای بساز. خروجی فقط JSON:\n{{\"category\": \"...\", \"title\": \"...\"}}\nلیست دسته‌ها: {', '.join(HUB_CATEGORIES)}\nمتن: {text[:500]}"
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}, timeout=15)
        res = r.json()['candidates'][0]['content']['parts'][0]['text']
        if "NO" in res.upper(): return None, None
        data = json.loads(res)
        return data.get("category", HUB_CATEGORIES[0]), data.get("title", "گزارش ویژه")
    except Exception as e:
        log.error(f"AI Error: {e}")
        return HUB_CATEGORIES[0], "خبر محلی"

# ============================================================
# Scraper & Downloader (Original Viktor Logic)
# ============================================================
def download_media(url, max_size_mb=20):
    try:
        resp = requests.get(url, stream=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200: return None
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            total += len(chunk)
            if total > max_size_mb * 1024 * 1024: return None
            chunks.append(chunk)
        return b"".join(chunks)
    except: return None

def scrape_channel(username):
    """اسکرپر ویکتور با اضافه شدن فیلتر ۲۴ ساعته"""
    url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200: return []
    except: return []

    soup = BeautifulSoup(resp.text, "html.parser")
    posts = []
    now_utc = datetime.now(timezone.utc)

    for widget in reversed(soup.find_all("div", class_="tgme_widget_message_wrap")):
        msg = widget.find("div", class_="tgme_widget_message")
        t_tag = widget.find("time")
        if not msg or not t_tag: continue
        
        # فیلتر زمان (فقط ۲۴ ساعت اخیر)
        try:
            dt = datetime.fromisoformat(t_tag.get("datetime").replace('Z', '+00:00'))
            if now_utc - dt > timedelta(hours=24): continue
        except: continue

        post_id = msg.get("data-post", "")
        if not post_id: continue

        post = {"id": post_id, "text": "", "media_url": None, "type": "text", "link": f"https://t.me/{post_id}"}

        text_div = msg.find("div", class_="tgme_widget_message_text")
        if text_div: post["text"] = text_div.get_text(separator="\n").strip()
        if not post["text"]: continue

        # یافتن عکس یا ویدیو
        video_tag = msg.find("video")
        if video_tag and video_tag.get("src"):
            post["media_url"] = video_tag.get("src")
            post["type"] = "video"
        else:
            photo_wrap = msg.find("a", class_="tgme_widget_message_photo_wrap")
            if photo_wrap:
                style = photo_wrap.get("style", "")
                match = re.search(r"url\('([^']+)'\)", style)
                if match:
                    post["media_url"] = match.group(1)
                    post["type"] = "photo"

        posts.append(post)
    return posts

# ============================================================
# Core Engine
# ============================================================
def tg_request(method, **kwargs):
    try:
        resp = requests.post(f"{TG_API}/{method}", **kwargs, timeout=60)
        return resp.json()
    except: return {"ok": False}

def run_smart_engine():
    log.info("🚀 --- STARTING AUTOMATED ENGINE ---")
    init_db()
    
    for p_id, config in PROVINCES.items():
        log.info(f"🔎 Scanning {config['name']}...")
        for username in config['sources']:
            posts = scrape_channel(username)
            for p in posts:
                with _db_lock:
                    conn = get_db()
                    # چک آیدی پست
                    if conn.execute("SELECT 1 FROM seen_posts WHERE post_id=?", (p['id'],)).fetchone():
                        conn.close(); continue
                    
                    # چک محتوای تکراری
                    c_hash = get_content_hash(p['text'])
                    if conn.execute("SELECT 1 FROM seen_contents WHERE hash=?", (c_hash,)).fetchone():
                        conn.execute("INSERT INTO seen_posts (post_id, source) VALUES (?, ?)", (p['id'], username))
                        conn.commit(); conn.close(); continue
                    conn.close()

                # هوش مصنوعی
                cat, title = ai_curator(p['text'], config['name'])
                if not cat:
                    with _db_lock:
                        conn = get_db()
                        conn.execute("INSERT INTO seen_posts (post_id, source) VALUES (?, ?)", (p['id'], username))
                        conn.execute("INSERT INTO seen_contents (hash) VALUES (?)", (c_hash,))
                        conn.commit(); conn.close()
                    continue

                # آماده‌سازی ارسال
                safe_txt = clean_html(p['text'][:900])
                cap = f"<b>{clean_html(cat)}</b>\n📌 <b>{clean_html(title)}</b>\n\n{safe_txt}\n\n🔗 <a href='{p['link']}'>منبع اصلی</a>"
                kb = {"inline_keyboard": [[{"text": "📝 بازنویسی مقاومت", "callback_data": f"rw:{c_hash}"}]]}
                
                # ارسال مدیا (متد ویکتور)
                sent = False
                if p['media_url']:
                    media_bytes = download_media(p['media_url'])
                    if media_bytes:
                        method = "sendVideo" if p['type'] == "video" else "sendPhoto"
                        files = {p['type']: ("media.mp4" if p['type']=="video" else "media.jpg", media_bytes)}
                        data = {"chat_id": config['channel'], "caption": cap, "parse_mode": "HTML", "reply_markup": json.dumps(kb)}
                        res = tg_request(method, data=data, files=files)
                        sent = res.get("ok", False)

                if not sent:
                    res = tg_request("sendMessage", json={"chat_id": config['channel'], "text": cap, "parse_mode": "HTML", "reply_markup": kb})
                    sent = res.get("ok", False)

                if sent:
                    msg_id = res['result']['message_id']
                    with _db_lock:
                        conn = get_db()
                        conn.execute("INSERT INTO seen_posts (post_id, source) VALUES (?, ?)", (p['id'], username))
                        conn.execute("INSERT INTO seen_contents (hash) VALUES (?)", (c_hash,))
                        conn.execute("INSERT INTO msg_logs_v2 VALUES (?, ?, ?, ?, ?, ?)", (c_hash, config['channel'], str(msg_id), p['text'][:800], p_id, p['type']))
                        conn.commit(); conn.close()
                    log.info(f"✅ SENT: {p['id']}")
                time.sleep(2)
    log.info("🏁 --- ENGINE FINISHED ---")

# ============================================================
# Flask Endpoints
# ============================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Automated Smart Viktor Running."

@app.route('/check')
def check():
    threading.Thread(target=run_smart_engine).start()
    return "Engine Triggered."

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True)
    if not data or "callback_query" not in data: return "OK"
    
    cb = data["callback_query"]
    h = cb["data"][3:]
    tg_request("answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "⏳ در حال بازنویسی..."})
    
    with _db_lock:
        conn = get_db()
        row = conn.execute("SELECT title, channel_id, msg_id, prov, type FROM msg_logs_v2 WHERE hash=?", (h,)).fetchone()
        conn.close()
        
    if row:
        title, c_id, m_id, prov, m_type = row
        prompt = f"این خبر را طبق پروتکل مقاومت و واژگان انقلابی بازنویسی کن. فقط متن نهایی:\n{title}"
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}", json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
            new_txt = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            method = "editMessageCaption" if m_type != "text" else "editMessageText"
            tg_request(method, json={"chat_id": c_id, "message_id": int(m_id), "caption" if m_type != "text" else "text": f"✊ <b>نسخه مقاومت</b>\n\n{clean_html(new_txt)}", "parse_mode": "HTML"})
        except Exception as e: log.error(f"Rewrite error: {e}")
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
