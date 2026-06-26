# Telegram News Bot v2

ربات خبری تلگرام — جمع‌آوری اخبار از کانال‌های تلگرام و وبسایت‌ها

## Features
- Scrape Telegram channels (via public web page)
- Scrape websites via RSS feeds
- SQLite for reliable deduplication
- All bot commands in Persian/Farsi
- Designed for Render.com free tier

## Environment Variables
| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `PERSONAL_CHANNEL_ID` | Destination channel chat ID |
| `ADMIN_CHAT_IDS` | Comma-separated admin chat IDs |

## Deploy to Render.com
1. Push to GitHub
2. Connect GitHub repo on Render.com
3. Set environment variables
4. Deploy

## Bot Commands
See `/help` in the bot for full command list.
