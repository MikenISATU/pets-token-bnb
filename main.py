import os
import json
import logging
import urllib.request  # Replace requests with urllib.request
from web3 import Web3
print("Web3 imported successfully")
from tenacity import retry, stop_after_attempt, wait_fixed
from dotenv import load_dotenv
import asyncio
from datetime import datetime
import random
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
RENDER_URL = os.getenv('RENDER_URL')  # Render app URL, e.g., https://pets-token-bnb.onrender.com
BSCSCAN_API_KEY = os.getenv('BSCSCAN_API_KEY')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')
PETS_BSC_ADDRESS = os.getenv('PETS_BSC_ADDRESS') or '0x2466858ab5edad0bb597fe9f008f568b00d25fe3'

# Validate environment variables
missing_vars = []
for var, name in [
    (TELEGRAM_BOT_TOKEN, 'TELEGRAM_BOT_TOKEN'),
    (CLOUDINARY_CLOUD_NAME, 'CLOUDINARY_CLOUD_NAME'),
    (RENDER_URL, 'RENDER_URL'),
    (BSCSCAN_API_KEY, 'BSCSCAN_API_KEY'),
    (ADMIN_CHAT_ID, 'ADMIN_CHAT_ID'),
    (PETS_BSC_ADDRESS, 'PETS_BSC_ADDRESS')
]:
    if not var:
        missing_vars.append(name)
if missing_vars:
    logger.error(f"Missing critical environment variables: {', '.join(missing_vars)}")
    raise SystemExit(1)

logger.info(f"Environment variables loaded: RENDER_URL={RENDER_URL}, TELEGRAM_BOT_TOKEN=****, BSCSCAN_API_KEY=****, CLOUDINARY_CLOUD_NAME={CLOUDINARY_CLOUD_NAME}, ADMIN_CHAT_ID={ADMIN_CHAT_ID}, PETS_BSC_ADDRESS={PETS_BSC_ADDRESS}")

# Constants
PANCAKESWAP_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
PAIR_ADDRESS = '0xYourPetsBnbPairAddress'  # Replace with the actual $PETS/BNB pair address
PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
        ],
        "type": "function"
    }
]

# Initialize Web3
try:
    w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed1.binance.org', request_kwargs={'timeout': 60}))
    logger.info("Web3 initialized with bsc-dataseed1")
except Exception as e:
    logger.error(f"Failed to initialize Web3 with bsc-dataseed1: {e}")
    try:
        w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed2.binance.org', request_kwargs={'timeout': 60}))
        logger.info("Web3 initialized with bsc-dataseed2")
    except Exception as e:
        logger.error(f"Failed to initialize Web3 with fallback: {e}")
        raise SystemExit(1)

# In-memory data
transactions = []
transaction_cache = []
active_chats = {ADMIN_CHAT_ID}
last_tx_hash = None
is_tracking_enabled = False
recent_errors = []
last_transaction_fetch = 0
TRANSACTION_CACHE_DURATION = 2 * 60 * 1000  # 2 minutes
cached_market_cap = '$10M'
last_market_cap_fetch = 0
MARKET_CAP_CACHE_DURATION = 5 * 60 * 1000  # 5 minutes
cached_prices = {'bnbPrice': 600, 'petsPrice': 0.0001}
last_price_fetch = 0
PRICE_CACHE_DURATION = 10 * 60 * 1000  # 10 minutes
tx_cache = {}

# Video mapping
category_videos = {
    'MicroPets Buy': 'SMALLBUY_b3px1p',
    'Medium Bullish Buy': 'MEDIUMBUY_MPEG_e02zdz',
    'Whale Buy': 'micropets_big_msapxz',
    'Extra Large Buy': 'micropets_big_msapxz'
}
category_video_displays = {
    'MicroPets Buy': '[Small Buy Video]',
    'Medium Bullish Buy': '[Medium Buy Video]',
    'Whale Buy': '[Large Buy Video]',
    'Extra Large Buy': '[Extra Large Buy Video]'
}

# Helper functions
async def get_pancake_swap_price():
    try:
        pair_contract = w3.eth.contract(address=PAIR_ADDRESS, abi=PAIR_ABI)
        reserves = pair_contract.functions.getReserves().call()
        reserve0 = w3.from_wei(reserves[0], 'ether')  # $PETS
        reserve1 = w3.from_wei(reserves[1], 'ether')  # BNB
        pets_per_bnb = reserve0 / reserve1
        prices = await fetch_prices()
        return {'petsPrice': round(prices['bnbPrice'] / pets_per_bnb, 6), 'bnbPrice': prices['bnbPrice']}
    except Exception as e:
        logger.error(f"Error fetching PancakeSwap price: {e}")
        return await fetch_prices()

async def fetch_prices():
    global last_price_fetch
    if datetime.now().timestamp() * 1000 - last_price_fetch < PRICE_CACHE_DURATION:
        logger.info("Returning cached prices")
        return cached_prices
    try:
        with urllib.request.urlopen('https://api.coingecko.com/api/v3/simple/price?ids=binancecoin,micropets&vs_currencies=usd', timeout=10) as response:
            if response.getcode() != 200:
                raise ValueError(f"HTTP error: {response.getcode()}")
            data = json.loads(response.read().decode())
        cached_prices.update({
            'bnbPrice': data.get('binancecoin', {}).get('usd', 600),
            'petsPrice': data.get('micropets', {}).get('usd', 0.0001)
        })
        last_price_fetch = datetime.now().timestamp() * 1000
        return cached_prices
    except Exception as e:
        logger.error(f"Error fetching prices: {e}")
        return cached_prices

async def get_token_value_in_bnb_usd(amount_in_pets):
    if not w3:
        return {'pets': '0 $PETS', 'bnb': '0 BNB', 'usd': '$0.00'}
    tokens = w3.from_wei(amount_in_pets, 'ether')
    prices = await get_pancake_swap_price()
    usd_value = round(float(tokens) * prices['petsPrice'], 2)
    bnb_value = round(usd_value / prices['bnbPrice'], 4)
    return {
        'pets': f"{float(tokens):.2f} $PETS",
        'bnb': f"{bnb_value} BNB",
        'usd': f"${usd_value}"
    }

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
async def get_market_cap():
    global cached_market_cap, last_market_cap_fetch
    if datetime.now().timestamp() * 1000 - last_market_cap_fetch < MARKET_CAP_CACHE_DURATION:
        return cached_market_cap
    try:
        url = f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={PETS_BSC_ADDRESS}&apikey={BSCSCAN_API_KEY}"
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.getcode() != 200:
                raise ValueError(f"HTTP error: {response.getcode()}")
            data = json.loads(response.read().decode())
        if data['status'] == '1':
            total_supply = w3.from_wei(int(data['result']), 'ether')
            prices = await get_pancake_swap_price()
            market_cap = round(float(total_supply) * prices['petsPrice'], 2)
            cached_market_cap = f"${market_cap}"
            last_market_cap_fetch = datetime.now().timestamp() * 1000
            return cached_market_cap
        raise ValueError("Invalid BscScan token supply response")
    except Exception as e:
        logger.error(f"Error calculating market cap: {e}")
        cached_market_cap = '$10M'
        last_market_cap_fetch = datetime.now().timestamp() * 1000
        return cached_market_cap

def categorize_buy(amount):
    if not w3:
        return 'Unknown Buy'
    tokens = float(w3.from_wei(amount, 'ether'))
    if tokens < 1000:
        return 'MicroPets Buy'
    elif tokens < 10000:
        return 'Medium Bullish Buy'
    elif tokens < 50000:
        return 'Whale Buy'
    return 'Extra Large Buy'

def shorten_address(address):
    if not address:
        return ''
    return f"{address[:6]}...{address[-4:]}"

def generate_random_address():
    return f"0x{random.randrange(16**12):012x}{random.randrange(16**4):04x}"

def get_video_url(category):
    public_id = category_videos.get(category, 'micropets_big_msapxz')
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/video/upload/w_1280/{public_id}.mp4"

def get_video_display(category):
    return category_video_displays.get(category, '[Default Video]')

async def is_dex_trade(tx_hash):
    if tx_hash in tx_cache:
        return tx_cache[tx_hash]
    if not w3:
        return False
    try:
        logger.info(f"Checking DEX trade for tx {tx_hash}")
        tx = w3.eth.get_transaction(tx_hash)
        is_dex = tx and tx['to'] and tx['to'].lower() == PANCAKESWAP_ROUTER.lower()
        tx_cache[tx_hash] = is_dex
        if len(tx_cache) > 100:
            tx_cache.pop(next(iter(tx_cache)))
        return is_dex
    except Exception as e:
        logger.error(f"[DEX Check Error] TxHash: {tx_hash}, Error: {e}")
        return False

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
async def fetch_bscscan_transactions():
    global transaction_cache, last_transaction_fetch
    if datetime.now().timestamp() * 1000 - last_transaction_fetch < TRANSACTION_CACHE_DURATION and transaction_cache:
        logger.info("Returning cached BscScan transactions")
        return transaction_cache
    try:
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&contractaddress={PETS_BSC_ADDRESS}&sort=desc&limit=10&apikey={BSCSCAN_API_KEY}"
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.getcode() != 200:
                raise ValueError(f"HTTP error: {response.getcode()}")
            data = json.loads(response.read().decode())
        if data['status'] == '1':
            transaction_cache = [
                {
                    'transactionHash': tx['hash'],
                    'to': tx['to'],
                    'value': tx['value'],
                    'blockNumber': tx['blockNumber']
                }
                for tx in data['result']
                if tx['to'].lower() == PANCAKESWAP_ROUTER.lower()
            ]
            last_transaction_fetch = datetime.now().timestamp() * 1000
            logger.info(f"Fetched {len(transaction_cache)} buy transactions from BscScan")
            return transaction_cache
        raise ValueError("Invalid BscScan response")
    except Exception as e:
        if isinstance(e, ValueError) and "HTTP error: 429" in str(e):
            logger.warning("BscScan rate limit hit, using cached transactions")
            return transaction_cache or []
        logger.error(f"Error fetching BscScan transactions: {e}")
        return transaction_cache or []

async def send_video_with_retry(context: ContextTypes.DEFAULT_TYPE, chat_id, video_url, options, max_retries=5, delay=2):
    for i in range(max_retries):
        try:
            logger.info(f"Attempt {i+1}/{max_retries} to send video to chat {chat_id}")
            await context.bot.send_video(chat_id=chat_id, video=video_url, **options)
            return
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                raise e
            await asyncio.sleep(delay)

def escape_markdown(text):
    return ''.join(f"\\{c}" if c in '*_[]()~`>#+=|{}.!' else c for c in text)

def is_admin(update: Update):
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /start for chat {chat_id}")
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id, "👋 Welcome to PETS Tracker! Use /track to start receiving buy alerts for $PETS.")

async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /track for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    active_chats.add(str(chat_id))
    global is_tracking_enabled
    is_tracking_enabled = True
    await context.bot.send_message(chat_id, "🚀 **Tracking $PETS buys started!**")
    asyncio.create_task(monitor_transactions(context))

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stop for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    active_chats.discard(str(chat_id))
    global is_tracking_enabled
    is_tracking_enabled = False
    await context.bot.send_message(chat_id, "🛑 **Tracking $PETS buys stopped.**")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stats for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Fetching $PETS transaction data...")
    try:
        market_cap = await get_market_cap()
        txs = await fetch_bscscan_transactions()
        last_tx = txs[0] if txs else {}
        wallet_address = generate_random_address()
        values = await get_token_value_in_bnb_usd(last_tx.get('value', 0)) if last_tx.get('value') else {'pets': '0 $PETS', 'bnb': '0 BNB', 'usd': '$0.00'}
        emoji_count = min(int(float(w3.from_wei(int(last_tx.get('value', 0)), 'ether')) / 10), 100) if last_tx.get('value') else 0
        emojis = '💰' * emoji_count
        is_pair_trade = await is_dex_trade(last_tx.get('transactionHash', '')) if last_tx.get('transactionHash') else False
        category = categorize_buy(last_tx.get('value', 0)) if last_tx.get('value') else 'No Recent Buy'
        tx_url = f"https://bscscan.com/tx/{last_tx.get('transactionHash', '')}" if last_tx.get('transactionHash') else '#'
        message = (
            f"🚀 **Latest {category} of $PETS{' (Pair Trade)' if is_pair_trade else ''}!**\n\n"
            f"{emojis}\n💰\n💵 Amount: {values['pets']}\n💱 Value: {values['bnb']} ({values['usd']})\n"
            f"🏦 Market Cap: {market_cap}\n🔼 Position: +{emoji_count}%\n🤑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on BscScan]({tx_url})\n\n"
            f"📍 [Staking](https://pets.micropets.io/petdex)  📊 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{PETS_BSC_ADDRESS})  "
            f"🛍️ [Merch](https://micropets.store/)  💰 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
        )
        await context.bot.send_message(chat_id, message, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /stats for chat {chat_id}: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 **Error fetching $PETS data. APIs may be down or rate-limited. Please try again later.**")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /help for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(
        chat_id,
        "🆘 *Available commands:*\n/start - Start the bot\n/track - Enable buy alerts\n/stop - Disable buy alerts\n"
        "/stats - View latest buy from $PETS\n/volume - View buy volume chart\n/status - Check tracking status\n"
        "/test - Show a sample buy template\n/noV - Show sample format without video\n/debug - View bot status and errors\n"
        "/help - Show this message",
        parse_mode='Markdown'
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /status for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(
        chat_id,
        f"🔍 *Status:* {'Tracking enabled' if str(chat_id) in active_chats else 'Tracking disabled'}\n"
        f"*Total tracked transactions:* {len(transactions)}",
        parse_mode='Markdown'
    )

async def volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /volume for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Generating $PETS buy volume chart...")
    try:
        txs = await fetch_bscscan_transactions()
        volume_data = [
            {'tokens': round(float(w3.from_wei(int(tx['value']), 'ether')), 2), 'block': tx['blockNumber']}
            for tx in txs[:5]
        ]
        await context.bot.send_message(chat_id, f"📊 $PETS Buy Volume:\n{json.dumps(volume_data, indent=2)}")
    except Exception as e:
        logger.error(f"Error in /volume: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 **Error generating volume chart.**")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /debug for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    status = {
        'trackingEnabled': is_tracking_enabled,
        'activeChats': list(active_chats),
        'transactionCount': len(transactions),
        'lastTxHash': last_tx_hash,
        'recentErrors': recent_errors[-5:],
        'apiStatus': {
            'bscWeb3': bool(w3),
            'lastPriceFetch': datetime.fromtimestamp(last_price_fetch / 1000).isoformat() if last_price_fetch else 'N/A',
            'lastTransactionFetch': datetime.fromtimestamp(last_transaction_fetch / 1000).isoformat() if last_transaction_fetch else 'N/A'
        }
    }
    await context.bot.send_message(chat_id, f"🔍 **Debug Info**\n```json\n{json.dumps(status, indent=2)}\n```", parse_mode='Markdown')

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /test for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test $PETS buy data...")
    try:
        test_tx_hash = '0xRandomTxHash456'
        test_pets_amount = random.randint(1000, 50000)
        category = categorize_buy(w3.to_wei(test_pets_amount, 'ether'))
        video_url = get_video_url(category)
        wallet_address = generate_random_address()
        values = await get_token_value_in_bnb_usd(w3.to_wei(test_pets_amount, 'ether'))
        emoji_count = min(int(test_pets_amount / 10), 100)
        emojis = '💰' * emoji_count
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚀 **New {category} of $PETS (Test)!**\n\n"
            f"{emojis}\n💰\n💵 Amount: {values['pets']}\n💱 Value: {values['bnb']} ({values['usd']})\n"
            f"🏦 Market Cap: $15M\n🔼 Position: +{emoji_count}%\n🤑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on BscScan]({tx_url})\n\n"
            f"📍 [Staking](https://pets.micropets.io/petdex)  📊 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{PETS_BSC_ADDRESS})  "
            f"🛍️ [Merch](https://micropets.store/)  💰 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
        )
        await send_video_with_retry(context, ADMIN_CHAT_ID, video_url, {'caption': message, 'parse_mode': 'MarkdownV2'})
        await context.bot.send_message(chat_id, "🚀 **Test $PETS buy executed successfully!**")
    except Exception as e:
        logger.error(f"Error in /test: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': f"Test failed: {e}"})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 **Error executing test command.**")

async def no_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /noV for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test $PETS buy data (no video)...")
    try:
        test_tx_hash = '0xRandomTxHash456'
        test_pets_amount = random.randint(1000, 50000)
        category = categorize_buy(w3.to_wei(test_pets_amount, 'ether'))
        wallet_address = generate_random_address()
        values = await get_token_value_in_bnb_usd(w3.to_wei(test_pets_amount, 'ether'))
        emoji_count = min(int(test_pets_amount / 10), 100)
        emojis = '💰' * emoji_count
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚀 **New {category} of $PETS (Test)!**\n\n"
            f"{emojis}\n💰\n💵 Amount: {values['pets']}\n💱 Value: {values['bnb']} ({values['usd']})\n"
            f"🏦 Market Cap: $15M\n🔼 Position: +{emoji_count}%\n🤑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on BscScan]({tx_url})\n\n"
            f"📍 [Staking](https://pets.micropets.io/petdex)  📊 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{PETS_BSC_ADDRESS})  "
            f"🛍️ [Merch](https://micropets.store/)  💰 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
        )
        await context.bot.send_message(chat_id, message, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in /noV: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': f"NoV failed: {e}"})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 **Error executing /noV command.**")

async def monitor_transactions(context: ContextTypes.DEFAULT_TYPE):
    global last_tx_hash
    if not is_tracking_enabled:
        logger.info("Tracking is disabled.")
        return
    poll_interval = 120  # 2 minutes
    while is_tracking_enabled:
        try:
            logger.info("Polling BSC for new $PETS transactions...")
            txs = await fetch_bscscan_transactions()
            for tx in txs:
                if last_tx_hash and tx['transactionHash'] == last_tx_hash:
                    break
                if not any(t['transactionHash'] == tx['transactionHash'] for t in transactions):
                    tokens = float(w3.from_wei(int(tx['value']), 'ether'))
                    if tokens < 5000:
                        continue  # Skip buys < 5000 $PETS
                    logger.info(f"New $PETS transaction detected: {tx['transactionHash']}")
                    transactions.append(tx)
                    if len(transactions) > 100:
                        transactions.pop(0)
                    is_pair_trade = await is_dex_trade(tx['transactionHash'])
                    category = categorize_buy(tx['value'])
                    video_url = get_video_url(category)
                    wallet_address = generate_random_address()
                    values = await get_token_value_in_bnb_usd(tx['value'])
                    market_cap = await get_market_cap()
                    emoji_count = min(int(tokens / 10), 100)
                    emojis = '💰' * emoji_count
                    tx_url = f"https://bscscan.com/tx/{tx['transactionHash']}"
                    message = (
                        f"🚀 **New {category} of $PETS{' (Pair Trade)' if is_pair_trade else ''}!**\n\n"
                        f"{emojis}\n💰\n💵 Amount: {values['pets']}\n💱 Value: {values['bnb']} ({values['usd']})\n"
                        f"🏦 Market Cap: {market_cap}\n🔼 Position: +{emoji_count}%\n🤑 Hodler: {shorten_address(wallet_address)}\n"
                        f"[🔍 View on BscScan]({tx_url})\n\n"
                        f"📍 [Staking](https://pets.micropets.io/petdex)  📊 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{PETS_BSC_ADDRESS})  "
                        f"🛍️ [Merch](https://micropets.store/)  💰 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
                    )
                    try:
                        await send_video_with_retry(context, ADMIN_CHAT_ID, video_url, {'caption': message, 'parse_mode': 'MarkdownV2'})
                        logger.info(f"Video sent to admin chat {ADMIN_CHAT_ID} for tx {tx['transactionHash']}")
                    except Exception as e:
                        logger.error(f"Failed to send video to admin chat {ADMIN_CHAT_ID}: {e}")
                        recent_errors.append({'time': datetime.now().isoformat(), 'error': f"Video send failed: {e}"})
                        if len(recent_errors) > 50:
                            recent_errors.pop(0)
                        try:
                            await context.bot.send_message(ADMIN_CHAT_ID, f"{message}\n\n⚠️ Video unavailable.", parse_mode='MarkdownV2')
                            logger.info(f"Sent fallback text message to chat {ADMIN_CHAT_ID}")
                        except Exception as e:
                            logger.error(f"Failed to send fallback text message to chat {ADMIN_CHAT_ID}: {e}")
                            recent_errors.append({'time': datetime.now().isoformat(), 'error': f"Fallback text failed: {e}"})
                            if len(recent_errors) > 50:
                                recent_errors.pop(0)
            if txs:
                last_tx_hash = txs[0]['transactionHash']
        except Exception as e:
            logger.error(f"BSC polling failed: {e}")
            recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
            if len(recent_errors) > 50:
                recent_errors.pop(0)
        await asyncio.sleep(poll_interval)

async def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("track", track))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("volume", volume))
    application.add_handler(CommandHandler("debug", debug))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(CommandHandler("noV", no_video))

    # Start monitoring transactions asynchronously
    await asyncio.create_task(monitor_transactions(application))

    # Run polling for the bot to handle commands
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
