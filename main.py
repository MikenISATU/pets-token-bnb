from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import requests
import time
import asyncio

# ============ CONFIG ============ #
BSCSCAN_API_KEY = "PUT_YOUR_API_KEY_HERE"
TELEGRAM_BOT_TOKEN = "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"

ALLOWED_IDS = {
    'users': [123456789],
    'groups': [-1001234567890]
}
# ================================ #

def is_authorized(chat_id: int) -> bool:
    return chat_id in ALLOWED_IDS['users'] or chat_id in ALLOWED_IDS['groups']

# ========== BUY INFO ========== #
def get_recent_buys(contract_address, api_key, limit=1):
    url = (
        f"https://api.bscscan.com/api"
        f"?module=account&action=tokentx"
        f"&contractaddress={contract_address}&page=1&offset={limit}"
        f"&sort=desc&apikey={api_key}"
    )
    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        if data['status'] != '1':
            return f"❌ Error: {data.get('message', 'Unknown error')}"

        tx = data['result'][0]
        value = int(tx['value']) / 10**int(tx['tokenDecimal'])
        output = (
            f"📦 Last Buy for CA:\n`{contract_address}`\n\n"
            f"💰 Tokens: `{value}`\n"
            f"🧑 From: `{tx['from']}`\n"
            f"🧑 To: `{tx['to']}`\n"
            f"⏱️ Time: `{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(int(tx['timeStamp'])))}`\n"
            f"🔗 Tx: https://bscscan.com/tx/{tx['hash']}"
        )
        return output

    except Exception as e:
        return f"⚠️ Request error: {str(e)}"

# ========== STATS INFO ========== #
def get_token_stats(contract_address):
    url = (
        f"https://api.bscscan.com/api"
        f"?module=token&action=tokeninfo"
        f"&contractaddress={contract_address}&apikey={BSCSCAN_API_KEY}"
    )
    try:
        res = requests.get(url)
        data = res.json()
        if data['status'] != '1':
            return "❌ Token info not found."

        token = data['result'][0]
        return (
            f"📊 Token Stats for `{contract_address}`:\n\n"
            f"🪙 Name: {token.get('tokenName')}\n"
            f"🔢 Symbol: {token.get('symbol')}\n"
            f"📦 Total Supply: {token.get('totalSupply')}\n"
            f"👥 Holders: {token.get('holdersCount')}\n"
            f"↔️ Transfers: {token.get('transfersCount')}"
        )
    except Exception as e:
        return f"⚠️ Error fetching stats: {str(e)}"

# ========== TRACK LOOP ========== #
last_tx_hash = {}

async def track_buys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ Not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /track <contract_address>")
        return

    ca = context.args[0]
    await update.message.reply_text(f"📡 Tracking last buys for:\n`{ca}`", parse_mode="Markdown")

    while True:
        url = (
            f"https://api.bscscan.com/api"
            f"?module=account&action=tokentx"
            f"&contractaddress={ca}&page=1&offset=1&sort=desc&apikey={BSCSCAN_API_KEY}"
        )
        try:
            res = requests.get(url, timeout=10).json()
            if res['status'] != '1':
                await update.message.reply_text("❌ No transaction found or token error.")
                return

            tx = res['result'][0]
            if last_tx_hash.get(ca) != tx['hash']:
                last_tx_hash[ca] = tx['hash']
                value = int(tx['value']) / 10**int(tx['tokenDecimal'])
                msg = (
                    f"📥 New Buy!\n"
                    f"💰 Amount: `{value}` {tx['tokenSymbol']}\n"
                    f"🧑 From: `{tx['from']}`\n"
                    f"🔗 https://bscscan.com/tx/{tx['hash']}"
                )
                await update.message.reply_text(msg, parse_mode="Markdown")

            await asyncio.sleep(15)  # Adjust polling interval as needed

        except Exception as e:
            await update.message.reply_text(f"⚠️ Error: {str(e)}")
            return

# ========== COMMANDS ========== #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello! I’m your token tracking bot.\nUse `/lastbuy`, `/stats`, or `/track`.")

async def last_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ You are not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage:\n`/lastbuy <contract_address>`", parse_mode="Markdown")
        return

    ca = context.args[0]
    result = get_recent_buys(ca, BSCSCAN_API_KEY)
    await update.message.reply_text(result, parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("⛔ You are not authorized.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage:\n`/stats <contract_address>`", parse_mode="Markdown")
        return

    ca = context.args[0]
    result = get_token_stats(ca)
    await update.message.reply_text(result, parse_mode="Markdown")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"🆔 Your Chat ID is:\n`{chat_id}`", parse_mode="Markdown")

# ========== RUN ========== #
def run_bot():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lastbuy", last_buy))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("track", track_buys))
    app.add_handler(CommandHandler("getid", get_id))
    print("🤖 Bot is running...")
    app.run_polling()

# Uncomment to run
# run_bot()
