import os
import logging
import requests
import random
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from web3 import Web3
from tenacity import retry, stop_after_attempt, wait_fixed
from dotenv import load_dotenv
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import telegram

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress HTTPX and Telegram logs
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
telegram_logger = logging.getLogger("telegram")
telegram_logger.setLevel(logging.WARNING)

# Check python-telegram-bot version
logger.info(f"python-telegram-bot version: {telegram.__version__}")
if not telegram.__version__.startswith('20'):
    logger.error(f"Expected python-telegram-bot v20.0+, got {telegram.__version__}")
    raise SystemExit(1)

# FastAPI app for webhooks
app = FastAPI()

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
APP_URL = os.getenv('RAILWAY_PUBLIC_DOMAIN', os.getenv('APP_URL'))  # Railway-specific
BSCSCAN_API_KEY = os.getenv('BSCSCAN_API_KEY')
BNB_RPC_URL = os.getenv('BNB_RPC_URL')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS', '0x2466858ab5edAd0BB597FE9f008F568B00d25Fe3').lower()
ADMIN_CHAT_ID = os.getenv('ADMIN_USER_ID')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
PORT = int(os.getenv('PORT', 8080))  # Railway assigns PORT
COINMARKETCAP_API_KEY = os.getenv('COINMARKETCAP_API_KEY', '')

# Validate environment variables
missing_vars = []
for var, name in [
    (TELEGRAM_BOT_TOKEN, 'TELEGRAM_BOT_TOKEN'),
    (CLOUDINARY_CLOUD_NAME, 'CLOUDINARY_CLOUD_NAME'),
    (APP_URL, 'APP_URL/RAILWAY_PUBLIC_DOMAIN'),
    (BSCSCAN_API_KEY, 'BSCSCAN_API_KEY'),
    (BNB_RPC_URL, 'BNB_RPC_URL'),
    (CONTRACT_ADDRESS, 'CONTRACT_ADDRESS'),
    (ADMIN_CHAT_ID, 'ADMIN_USER_ID'),
    (TELEGRAM_CHAT_ID, 'TELEGRAM_CHAT_ID')
]:
    if not var:
        missing_vars.append(name)
if missing_vars:
    logger.error(f"Missing critical environment variables: {', '.join(missing_vars)}")
    raise SystemExit(1)

logger.info(f"Environment variables loaded: APP_URL={APP_URL}, TELEGRAM_BOT_TOKEN=*****, BSCSCAN_API_KEY=*****, CLOUDINARY_CLOUD_NAME={CLOUDINARY_CLOUD_NAME}, ADMIN_CHAT_ID={ADMIN_CHAT_ID}, CONTRACT_ADDRESS={CONTRACT_ADDRESS}, PORT={PORT}, BNB_RPC_URL=*****")

# Constants
TARGET_ADDRESS = '0x4BDECe4E422fA015336234e4fC4D39ae6dD75b01'.lower()
EMOJI = '💰'
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', 60))

# PancakeSwap Pair ABI
PANCAKESWAP_PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"internalType": "uint112", "name": "_reserve0", "type": "uint112"},
            {"internalType": "uint112", "name": "_reserve1", "type": "uint112"},
            {"internalType": "uint32", "name": "_blockTimestampLast", "type": "uint32"}
        ],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    }
]

# Video mapping for Cloudinary
cloudinary_videos = {
    'MicroPets Buy': 'SMALLBUY_b3px1p',
    'Medium Bullish Buy': 'MEDIUMBUY_MPEG_e02zdz',
    'Whale Buy': 'micropets_big_msap',
    'Extra Large Buy': 'micropets_big_msapxz'
}

# In-memory data
transaction_cache = []
active_chats = {TELEGRAM_CHAT_ID}
last_transaction_hash = None
is_tracking_enabled = False
recent_errors = []
last_transaction_fetch = 0
TRANSACTION_CACHE_THRESHOLD = 2 * 60 * 1000  # 2 minutes
cached_market_cap = '$256,000'
last_market_cap_cache = 0
MARKET_CAP_CACHE_DURATION = 5 * 60 * 1000  # 5 minutes
posted_transactions = set()
lock = asyncio.Lock()

# Initialize Web3
try:
    w3 = Web3(Web3.HTTPProvider(BNB_RPC_URL, request_kwargs={'timeout': 60}))
    logger.info("Web3 initialized with BNB_RPC_URL")
except Exception as e:
    logger.error(f"Failed to initialize Web3 with BNB_RPC_URL: {e}")
    try:
        w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed2.binance.org', request_kwargs={'timeout': 60}))
        logger.info("Web3 initialized with fallback bsc-dataseed2")
    except Exception as e:
        logger.error(f"Failed to initialize Web3 with fallback: {e}")
        raise SystemExit(1)

# Helper functions
def get_video_url(category):
    public_id = cloudinary_videos.get(category, 'micropets_big_msapxz')
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/video/upload/w_1280/{public_id}.mp4"

def categorize_buy(usd_value):
    if usd_value < 100:
        return 'MicroPets Buy'
    elif usd_value < 500:
        return 'Medium Bullish Buy'
    elif usd_value < 1000:
        return 'Whale Buy'
    return 'Extra Large Buy'

def shorten_address(address):
    if not address:
        return ''
    return f"{address[:6]}...{address[-4:]}"

def load_posted_transactions():
    try:
        if not os.path.exists('posted_transactions.txt'):
            return set()
        with open('posted_transactions.txt', 'r') as f:
            return set(line.strip() for line in f)
    except Exception as e:
        logger.warning(f"Could not load posted_transactions.txt: {e}")
        return set()

def log_posted_transaction(transaction_hash):
    try:
        with open('posted_transactions.txt', 'a') as f:
            f.write(transaction_hash + '\n')
    except Exception as e:
        logger.warning(f"Could not write to posted_transactions.txt: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_bnb_to_usd():
    try:
        response = requests.get(
            'https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd',
            timeout=10
        )
        response.raise_for_status()
        price = float(response.json()['binancecoin']['usd'])
        logger.info(f"Fetched BNB price: ${price:.2f}")
        return price
    except Exception as e:
        logger.error(f"Error fetching BNB price: {e}")
        return 600  # Fallback price

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_pets_price_from_pancakeswap():
    try:
        pair_address = TARGET_ADDRESS
        pair_contract = w3.eth.contract(address=pair_address, abi=PANCAKESWAP_PAIR_ABI)
        reserves = pair_contract.functions.getReserves().call()
        reserve0, reserve1, _ = reserves
        bnb_per_pets = reserve0 / reserve1 / 1e18  # WBNB reserve / PETS reserve
        if bnb_per_pets <= 0:
            logger.warning("Invalid BNB per PETS ratio, inverting reserves")
            bnb_per_pets = reserve1 / reserve0 / 1e18  # Try PETS/WBNB order
        bnb_to_usd = get_bnb_to_usd()
        pets_price_usd = bnb_per_pets * bnb_to_usd
        if pets_price_usd <= 0:
            raise ValueError("Invalid PETS price from PancakeSwap")
        logger.info(f"PancakeSwap reserves: reserve0={reserve0}, reserve1={reserve1}, BNB/PETS={bnb_per_pets:.10f}, PETS/USD={pets_price_usd:.10f}")
        return pets_price_usd
    except Exception as e:
        logger.error(f"Error fetching $PETS price from PancakeSwap: {e}")
        return 0.00004014  # Fallback to match ~$256K with 6.38B supply

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_token_supply():
    url = f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={CONTRACT_ADDRESS}&apikey={BSCSCAN_API_KEY}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            supply = int(data['result']) / 1e18
            logger.info(f"Fetched token supply from BscScan: {supply:,.0f} tokens")
            return supply
        logger.error(f"API Error fetching token supply: {data['message']}")
    except Exception as e:
        logger.error(f"Error fetching token supply from BscScan: {e}")
    return 6380000000  # Adjusted to match DexTools ~$256K market cap

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def extract_market_cap():
    global last_market_cap_cache, cached_market_cap
    if (datetime.now().timestamp() * 1000 - last_market_cap_cache <
            MARKET_CAP_CACHE_DURATION and cached_market_cap != '$256,000'):
        return int(cached_market_cap.replace('$', '').replace(',', ''))
    try:
        price = get_pets_price_from_pancakeswap()
        token_supply = get_token_supply()
        market_cap = token_supply * price
        market_cap_int = int(market_cap)
        cached_market_cap = f'${market_cap_int:,}'
        last_market_cap_cache = datetime.now().timestamp() * 1000
        logger.info(
            f"Calculated market cap for $PETS: ${market_cap_int:,} "
            f"(price=${price:.10f}, supply={token_supply:,.0f})"
        )
        return market_cap_int
    except Exception as e:
        logger.error(f"Error calculating market cap: {e}")
        return 256000  # Fallback to match DexTools ~$256K

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
async def fetch_bscscan_transactions():
    global transaction_cache, last_transaction_fetch
    if (datetime.now().timestamp() * 1000 - last_transaction_fetch <
            TRANSACTION_CACHE_THRESHOLD and transaction_cache):
        logger.info(f"Returning {len(transaction_cache)} cached transactions")
        return transaction_cache
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=account&action=tokentx"
            f"&contractaddress={CONTRACT_ADDRESS}&address={TARGET_ADDRESS}"
            f"&page=1&offset=50&sort=desc&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            transaction_cache = [
                {
                    'transactionHash': tx['hash'],
                    'to': tx['to'],
                    'from': tx['from'],
                    'value': tx['value'],
                    'blockNumber': tx['blockNumber']
                }
                for tx in data['result'] if tx['from'].lower() == TARGET_ADDRESS.lower()
            ]
            last_transaction_fetch = datetime.now().timestamp() * 1000
            logger.info(f"Fetched {len(transaction_cache)} buy transactions from BscScan")
            return transaction_cache
        raise ValueError(f"Invalid BscScan response: {data['message']}")
    except Exception as e:
        if isinstance(e, requests.HTTPError) and e.response.status_code == 429:
            logger.warning("BscScan rate limit hit, using cached transactions")
            return transaction_cache or []
        logger.error(f"Error fetching BscScan transactions: {e}")
        return transaction_cache or []

async def send_video_with_retry(context, chat_id, video_url, options, max_retries=5, delay=2):
    for i in range(max_retries):
        try:
            logger.info(f"Attempt {i+1}/{max_retries} to send video to chat {chat_id}")
            await context.bot.send_video(chat_id=chat_id, video=video_url, **options)
            return True
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(
                    chat_id,
                    f"{options['caption']}\n\n⚠️ Video unavailable.",
                    parse_mode='Markdown'
                )
                logger.info(f"Sent fallback text to chat {chat_id}")
                return False
    return False

async def process_transaction(context, transaction, bnb_to_usd_rate, pets_price, chat_id=TELEGRAM_CHAT_ID):
    global posted_transactions
    if transaction['transactionHash'] in posted_transactions:
        logger.info(f"Transaction {transaction['transactionHash']} already processed")
        return False

    pets_amount = float(transaction['value']) / 1e18
    usd_value = pets_amount * pets_price
    logger.info(
        f"Transaction {transaction['transactionHash']}: "
        f"PETS={pets_amount:,.0f}, USD={usd_value:,.2f}, PETS_price={pets_price:.10f}"
    )
    if usd_value < 5:
        logger.info(f"Transaction {transaction['transactionHash']} below threshold $5")
        return False

    market_cap = extract_market_cap()
    wallet_address = transaction['to']
    emoji_count = min(int(usd_value) // 5, 100)
    emojis = EMOJI * emoji_count
    tx_url = f"https://bscscan.com/tx/{transaction['transactionHash']}"
    category = categorize_buy(usd_value)
    video_url = get_video_url(category)

    message = (
        f"🚀 MicroPets Buy! BNBchain 💰\n\n"
        f"{emojis}\n"
        f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): "
        f"{pets_amount:,.0f} (${usd_value:,.2f})\n"
        f"🏦 Market Cap: ${market_cap:,.0f}\n"
        f"🤑 Hodler: {shorten_address(wallet_address)}\n"
        f"[🔍 View on BscScan]({tx_url})\n\n"
        f"💰 [Staking](https://pets.micropets.io/petdex) "
        f"📈 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
        f"🛍 [Merch](https://micropets.store/) "
        f"🤑 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
    )

    try:
        success = await send_video_with_retry(
            context,
            chat_id,
            video_url,
            {'caption': message, 'parse_mode': 'Markdown'}
        )
        if success:
            logger.info(f"Sent message for transaction {transaction['transactionHash']} to chat {chat_id}")
            posted_transactions.add(transaction['transactionHash'])
            log_posted_transaction(transaction['transactionHash'])
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        return False

async def monitor_transactions(context):
    global last_transaction_hash, is_tracking_enabled
    while is_tracking_enabled:
        try:
            async with lock:
                logger.info("Starting transaction monitoring")
                posted_transactions.update(load_posted_transactions())
                logger.info(f"Loaded {len(posted_transactions)} posted transactions")
                txs = await fetch_bscscan_transactions()
                logger.info(f"Fetched {len(txs)} transactions")
                if not txs:
                    logger.info("No new transactions")
                else:
                    bnb_to_usd_rate = get_bnb_to_usd()
                    pets_price = get_pets_price_from_pancakeswap()
                    logger.info(f"BNB price: ${bnb_to_usd_rate:.2f}, PETS price: ${pets_price:.10f}")
                    new_last_hash = last_transaction_hash
                    for tx in reversed(txs):
                        logger.info(f"Checking transaction {tx['transactionHash']}")
                        if tx['transactionHash'] in posted_transactions:
                            logger.info(f"Skipping processed transaction {tx['transactionHash']}")
                            continue
                        if last_transaction_hash and tx['transactionHash'] == last_transaction_hash:
                            logger.info(f"Reached last processed transaction {last_transaction_hash}")
                            break
                        if await process_transaction(context, tx, bnb_to_usd_rate, pets_price):
                            logger.info(f"Processed transaction {tx['transactionHash']}")
                            new_last_hash = tx['transactionHash']
                        else:
                            logger.info(f"Skipped transaction {tx['transactionHash']} (below threshold or error)")
                    if new_last_hash != last_transaction_hash:
                        last_transaction_hash = new_last_hash
                        logger.info(f"Updated last transaction: {last_transaction_hash}")
        except Exception as e:
            logger.error(f"Error monitoring transactions: {e}")
            recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
            if len(recent_errors) > 5:
                recent_errors.pop(0)
        logger.info(f"Sleeping for {POLL_INTERVAL} seconds")
        await asyncio.sleep(POLL_INTERVAL)

def is_admin(update):
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

# Command handlers
async def start(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /start for chat {chat_id}")
    active_chats.add(str(chat_id))
    await context.bot.send_message(
        chat_id=chat_id,
        text="👋 Welcome to PETS Tracker! Use /track to start buy alerts."
    )

async def track(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /track for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    active_chats.add(str(chat_id))
    global is_tracking_enabled
    if not is_tracking_enabled:
        is_tracking_enabled = True
        await context.bot.send_message(chat_id, "🚀 Tracking started")
        asyncio.create_task(monitor_transactions(context))
    else:
        await context.bot.send_message(chat_id, "🚀 Tracking already started")

async def stop(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stop for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    global is_tracking_enabled
    is_tracking_enabled = False
    logger.info("Tracking disabled")
    await context.bot.send_message(chat_id, "🛑 Tracking stopped")

async def stats(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stats for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Fetching $PETS data")
    try:
        txs = await fetch_bscscan_transactions()
        logger.info(f"Fetched {len(txs)} transactions: {txs[:5]}")
        if not txs:
            await context.bot.send_message(chat_id, "🚫 No recent buys")
            return
        bnb_to_usd_rate = get_bnb_to_usd()
        pets_price = get_pets_price_from_pancakeswap()
        processed = []
        seen_hashes = set()
        for tx in reversed(txs[:5]):
            if tx['transactionHash'] in seen_hashes:
                logger.info(f"Skipping duplicate transaction {tx['transactionHash']}")
                continue
            logger.info(f"Processing {tx['transactionHash']}")
            if await process_transaction(context, tx, bnb_to_usd_rate, pets_price, chat_id=chat_id):
                processed.append(tx['transactionHash'])
            seen_hashes.add(tx['transactionHash'])
        if not processed:
            await context.bot.send_message(chat_id, "🚫 No transactions found meeting criteria")
        else:
            await context.bot.send_message(
                chat_id,
                f"✅ Processed {len(processed)} buys:\n" + "\n".join(processed)
            )
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Failed to fetch data")

async def status(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /status for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id,
        f"🔍 *Status:* {'Enabled' if is_tracking_enabled else 'Disabled'}",
        parse_mode='Markdown'
    )

async def debug(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /debug for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    status = {
        'trackingEnabled': is_tracking_enabled,
        'activeChats': list(active_chats),
        'lastTxHash': last_transaction_hash,
        'recentErrors': recent_errors[-5:],
        'apiStatus': {
            'bscWeb3': bool(w3),
            'lastTransactionFetch': (
                datetime.fromtimestamp(last_transaction_fetch / 1000).isoformat()
                if last_transaction_fetch else None
            )
        }
    }
    await context.bot.send_message(
        chat_id,
        f"🔍 Debug:\n```json\n{json.dumps(status, indent=2)}\n```",
        parse_mode='Markdown'
    )

# FastAPI routes
@app.get("/health")
async def health_check():
    logger.info("Health check accessed")
    return {"status": "Bot is running", "tracking_enabled": is_tracking_enabled}

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received webhook request")
    try:
        update = Update.de_json(await request.json(), bot_app.bot)
        if update:
            logger.info(f"Processing update: {update.update_id}")
            await bot_app.process_update(update)
            return {"status": "OK"}
        else:
            logger.warning("Received empty update")
            return {"status": "No update"}, 400
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        return {"status": "error"}, 500

@app.on_event("startup")
async def startup_event():
    logger.info("Starting bot on Railway")
    try:
        await bot_app.initialize()
        logger.info("Bot initialized")
        webhook_url = f"https://{APP_URL}/webhook"
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {webhook_url}")
        # Test webhook setup
        webhook_info = await bot_app.bot.get_webhook_info()
        logger.info(f"Webhook info: {webhook_info}")
        if webhook_info.url != webhook_url:
            logger.error(f"Webhook setup failed: Expected {webhook_url}, got {webhook_info.url}")
            raise SystemExit(1)
    except Exception as e:
        logger.error(f"Startup error: {e}")
        raise SystemExit(1)

@app.on_event("shutdown")
async def shutdown_event():
    try:
        await bot_app.bot.delete_webhook()
        logger.info("Webhook deleted")
        await bot_app.shutdown()
        logger.info("Bot shutdown")
    except Exception as e:
        logger.error(f"Shutdown error: {e}")

# Bot initialization
bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("track", track))
bot_app.add_handler(CommandHandler("stop", stop))
bot_app.add_handler(CommandHandler("stats", stats))
bot_app.add_handler(CommandHandler("status", status))
bot_app.add_handler(CommandHandler("debug", debug))

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting Uvicorn server on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
