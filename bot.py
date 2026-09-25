import os
import logging
import asyncio
import traceback
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN environment variable is missing!")

PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("BitFusion399Bot")

# ---------- SIMPLE HEALTH SERVER (Railway needs an open port) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"BitFusion399Bot is alive")
    def log_message(self, *args, **kwargs):
        pass  # silence logs

def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"✅ Health server listening on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server error: {e}")

# ---------- COINGECKO CLIENT ----------
HEADERS = {
    "User-Agent": "BitFusion399Bot/1.0 (+https://t.me/BitFusion399Bot)",
    "Accept": "application/json",
}

_cache = {}   # simple in-memory cache: key -> (timestamp, data)
CACHE_TTL = 60  # seconds

async def fetch_json(url: str, retries: int = 3):
    now = asyncio.get_event_loop().time()
    if url in _cache:
        ts, data = _cache[url]
        if now - ts < CACHE_TTL:
            return data

    timeout = aiohttp.ClientTimeout(total=15)
    for attempt in range(1, retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        _cache[url] = (now, data)
                        return data
                    elif resp.status == 429:
                        logger.warning(f"429 rate limited, retrying ({attempt}/{retries})...")
                        await asyncio.sleep(2 * attempt)
                    else:
                        logger.error(f"HTTP {resp.status} for {url}")
                        await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Fetch error ({attempt}/{retries}): {e}")
            await asyncio.sleep(1.5)
    return None

async def get_price(coin_id: str):
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coin_id}&vs_currencies=usd"
        f"&include_24hr_change=true&include_market_cap=true"
    )
    data = await fetch_json(url)
    if data and coin_id in data:
        return data[coin_id]
    return None

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [
        [InlineKeyboardButton("💰 BTC Price", callback_data="price_btc"),
         InlineKeyboardButton("📊 Market", callback_data="market")],
        [InlineKeyboardButton("🔥 Trending", callback_data="trending"),
         InlineKeyboardButton("🆘 Help", callback_data="help")],
    ]
    await update.message.reply_text(
        f"👋 Welcome *{user.first_name}* to *BitFusion399Bot*!\n\n"
        "🔐 Your secure & intelligent crypto assistant.\n\n"
        "Try:\n"
        "• `/price bitcoin`\n"
        "• `/market`\n"
        "• `/trending`\n"
        "• `/convert 1 bitcoin usd`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*BitFusion399Bot Commands*\n\n"
        "• `/start` – Welcome menu\n"
        "• `/price <coin>` – Live price (e.g. `/price bitcoin`)\n"
        "• `/market` – Top 10 coins\n"
        "• `/trending` – Trending coins\n"
        "• `/convert <amt> <from> <to>` – Convert\n"
        "• `/help` – This menu\n\n"
        "⚠️ Not financial advice. DYOR!"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode="Markdown")

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/price <coin>`\nExample: `/price bitcoin`",
            parse_mode="Markdown",
        )
        return
    coin = context.args[0].lower()
    msg = await update.message.reply_text(f"🔎 Fetching price for *{coin}*...", parse_mode="Markdown")
    data = await get_price(coin)
    if not data:
        await msg.edit_text(
            f"❌ Could not find *{coin}*.\nTry full names like `bitcoin`, `ethereum`, `solana`.",
            parse_mode="Markdown",
        )
        return
    price = data.get("usd", 0)
    change = data.get("usd_24h_change", 0) or 0
    cap = data.get("usd_market_cap", 0) or 0
    arrow = "🟢" if change >= 0 else "🔴"
    text = (
        f"💎 *{coin.capitalize()}*\n\n"
        f"💵 Price: *${price:,.4f}*\n"
        f"{arrow} 24h: *{change:+.2f}%*\n"
        f"🏦 Market Cap: *${cap:,.0f}*\n\n"
        f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await msg.edit_text(text, parse_mode="Markdown")

async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=usd&order=market_cap_desc&per_page=10&page=1"
    )
    data = await fetch_json(url)
    target = update.message or update.callback_query.message
    if not data:
        await target.reply_text("⚠️ Failed to fetch market data. Try again in a minute.")
        return
    lines = ["📊 *Top 10 Cryptos by Market Cap*\n"]
    for i, c in enumerate(data, 1):
        change = c.get("price_change_percentage_24h") or 0
        arrow = "🟢" if change >= 0 else "🔴"
        lines.append(f"{i}. *{c['symbol'].upper()}* – ${c['current_price']:,.4f} {arrow} {change:+.2f}%")
    await target.reply_text("\n".join(lines), parse_mode="Markdown")

async def trending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = "https://api.coingecko.com/api/v3/search/trending"
    data = await fetch_json(url)
    target = update.message or update.callback_query.message
    if not data:
        await target.reply_text("⚠️ Failed to fetch trending coins.")
        return
    lines = ["🔥 *Trending Coins (24h)*\n"]
    for i, item in enumerate(data.get("coins", [])[:7], 1):
        c = item["item"]
        lines.append(f"{i}. *{c['name']}* ({c['symbol'].upper()}) – Rank #{c.get('market_cap_rank', 'N/A')}")
    await target.reply_text("\n".join(lines), parse_mode="Markdown")

async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text(
            "Usage: `/convert <amount> <from> <to>`\nExample: `/convert 1 bitcoin usd`",
            parse_mode="Markdown",
        )
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")
        return
    frm, to = context.args[1].lower(), context.args[2].lower()
    data = await fetch_json(
        f"https://api.coingecko.com/api/v3/simple/price?ids={frm},{to}&vs_currencies=usd"
    )
    if not data or frm not in data or to not in data:
        await update.message.reply_text("❌ Invalid coin name(s).")
        return
    usd_value = amount * data[frm]["usd"]
    result = usd_value / data[to]["usd"]
    await update.message.reply_text(
        f"💱 *{amount} {frm.upper()}* = *{result:,.6f} {to.upper()}*\n_≈ ${usd_value:,.2f} USD_",
        parse_mode="Markdown",
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "price_btc":
        btc = await get_price("bitcoin")
        if btc:
            change = btc.get("usd_24h_change", 0) or 0
            arrow = "🟢" if change >= 0 else "🔴"
            await query.message.reply_text(
                f"💎 *Bitcoin*\n💵 ${btc['usd']:,.2f}\n{arrow} 24h: {change:+.2f}%",
                parse_mode="Markdown",
            )
    elif data == "market":
        await market_cmd(update, context)
    elif data == "trending":
        await trending_cmd(update, context)
    elif data == "help":
        await help_cmd(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)
    logger.error("".join(traceback.format_exception(None, context.error, context.error.__traceback__)))

# ---------- MAIN ----------
def main():
    # Start health server in background thread (Railway needs open PORT)
    threading.Thread(target=start_health_server, daemon=True).start()

    logger.info("🤖 Building BitFusion399Bot application...")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("trending", trending_cmd))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    logger.info("✅ Bot is starting polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
