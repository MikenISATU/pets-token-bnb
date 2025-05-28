import os
import json
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
from bs4 import BeautifulSoup
import re
from decimal import Decimal
import time

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Suppress HTTPX and Telegram logs
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
telegram_logger = logging.getLogger("telegram")
telegram_logger.setLevel(logging.WARNING)

# FastAPI app for webhooks
app = FastAPI()

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
APP_URL = os.getenv('APP_URL')
BSCSCAN_API_KEY = os.getenv('BSCSCAN_API_KEY')
ADMIN_CHAT_ID = '1888498588'
TELEGRAM_CHAT_ID = '-1001523714483'
PETS_BSC_ADDRESS = os.getenv('PETS_BSC_ADDRESS') or '0x2466858ab5edad0bb597fe9f008f568b00d25fe3'
PORT = int(os.getenv('PORT', 8080))

# Validate environment variables
missing_vars = []
for var, name in [
    (TELEGRAM_BOT_TOKEN, 'TELEGRAM_BOT_TOKEN'),
    (CLOUDINARY_CLOUD_NAME, 'CLOUDINARY_CLOUD_NAME'),
    (APP_URL, 'APP_URL'),
    (BSCSCAN_API_KEY, 'BSCSCAN_API_KEY'),
    (PETS_BSC_ADDRESS, 'PETS_BSC_ADDRESS')
]:
    if not var:
        missing_vars.append(name)
if missing_vars:
    logger.error(f"Missing critical environment variables: {', '.join(missing_vars)}")
    raise SystemExit(1)

logger.info(f"Environment variables loaded: APP_URL={APP_URL}, TELEGRAM_BOT_TOKEN=****, BSCSCAN_API_KEY=****, CLOUDINARY_CLOUD_NAME={CLOUDINARY_CLOUD_NAME}, ADMIN_CHAT_ID={ADMIN_CHAT_ID}, PETS_BSC_ADDRESS={PETS_BSC_ADDRESS}, PORT={PORT}")

# Constants
TARGET_ADDRESS = '0x4BDECe4E422fA015336234e4FC4D39ae6dD75b01'
PANCAKESWAP_ROUTER = '0x10ED43C718714eb63d5aA57B78B54704E256024E'
CONFIG_FILE = 'config.json'

# Video mapping for Cloudinary
category_videos = {
    'MicroPets Buy': 'SMALLBUY_b3px1p',
    'Medium Bullish Buy': 'MEDIUMBUY_MPEG_e02zdz',
    'Whale Buy': 'micropets_big_msapxz',
    'Extra Large Buy': 'micropets_big_msapxz'
}

# In-memory data
transactions = []
transaction_cache = []
active_chats = {TELEGRAM_CHAT_ID}
last_tx_hash = None
is_tracking_enabled = False
recent_errors = []
last_transaction_fetch = 0
TRANSACTION_CACHE_DURATION = 2 * 60 * 1000  # 2 minutes
cached_market_cap = '$10,000,000'  # Fallback with commas
last_market_cap_fetch = 0
MARKET_CAP_CACHE_DURATION = 5 * 60 * 1000  # 5 minutes
posted_transactions = set()
lock = asyncio.Lock()

# Load configuration
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as file:
            return json.load(file)
    return {'small_threshold': 100, 'medium_threshold': 500, 'large_threshold': 1000, 'emoji': '💰'}

def save_config(config):
    with open(CONFIG_FILE, 'w') as file:
        json.dump(config, file, indent=4)

config = load_config()

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

# Helper functions
def get_video_url(category):
    public_id = category_videos.get(category, 'micropets_big_msapxz')
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/video/upload/w_1280/{public_id}.mp4"

def categorize_buy(usd_value):
    if usd_value < config['small_threshold']:
        return 'MicroPets Buy'
    elif usd_value < config['medium_threshold']:
        return 'Medium Bullish Buy'
    elif usd_value < config['large_threshold']:
        return 'Whale Buy'
    return 'Extra Large Buy'

def shorten_address(address):
    if not address:
        return ''
    return f"{address[:6]}...{address[-4:]}"

def load_posted_transactions():
    if not os.path.exists('posted_transactions.txt'):
        return set()
    with open('posted_transactions.txt', 'r') as file:
        return set(line.strip() for line in file)

def log_posted_transaction(transaction_hash):
    with open('posted_transactions.txt', 'a') as file:
        file.write(transaction_hash + '\n')

def get_bnb_to_usd():
    try:
        response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=binancecoin&vs_currencies=usd', timeout=10)
        response.raise_for_status()
        data = response.json()
        return float(data['binancecoin']['usd'])
    except Exception as e:
        logger.error(f"Error fetching BNB price: {e}")
        return 600  # Fallback price

def extract_bnb_value(transaction_soup):
    potential_values = transaction_soup.find_all('span', {'data-bs-toggle': 'tooltip'})
    for value_span in potential_values:
        value_text = value_span.text.strip().replace(',', '')
        if re.match(r'^\d+(\.\d+)?$', value_text):
            try:
                return float(value_text)
            except ValueError:
                continue
    logger.error("No valid BNB value found in transaction details.")
    return None

@retry(stop=stop_after_attempt(5), wait=wait_fixed(5))  # Increased retries and delay
def extract_market_cap_bscscan():
    global last_market_cap_fetch, cached_market_cap
    if datetime.now().timestamp() * 1000 - last_market_cap_fetch < MARKET_CAP_CACHE_DURATION and cached_market_cap != '$10,000,000':
        return int(cached_market_cap.replace('$', '').replace(',', ''))
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
    url_mc = f'https://bscscan.com/token/{PETS_BSC_ADDRESS}'
    try:
        response = requests.get(url_mc, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        market_cap_element = soup.find('div', id='ContentPlaceHolder1_tr_marketcap')
        if market_cap_element:
            market_cap_text = market_cap_element.get_text(strip=True)
            market_cap_value = Decimal(market_cap_text.replace('Onchain Market Cap$', '').replace(',', '').strip())
            cached_market_cap = f'${int(market_cap_value):,}'
            last_market_cap_fetch = datetime.now().timestamp() * 1000
            return int(market_cap_value)
        logger.error("Market cap element not found on BscScan.")
        cached_market_cap = '$10,000,000'
        return 10000000
    except Exception as e:
        logger.error(f"Error fetching market cap: {e}")
        time.sleep(5)  # Add delay before retry
        return 10000000

def check_execute_function(transaction_hash):
    transaction_url = f"https://bscscan.com/tx/{transaction_hash}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
    try:
        response = requests.get(transaction_url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        bnb_value = extract_bnb_value(soup)
        execute_badge = soup.find(string=re.compile("Execute", re.IGNORECASE))
        return bool(execute_badge), bnb_value
    except Exception as e:
        logger.error(f"Error checking transaction {transaction_hash}: {e}")
        return False, None

def get_balance_before_transaction(wallet_address):
    url = f'https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={PETS_BSC_ADDRESS}&address={wallet_address}&tag=latest&apikey={BSCSCAN_API_KEY}'
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            return Decimal(data['result']) / Decimal(1e18)
        logger.error(f"API Error: {data['message']}")
        return None
    except Exception as e:
        logger.error(f"Error fetching balance: {e}")
        return None

def calculate_percent_increase(last_balance, current_balance):
    if last_balance is None or last_balance == 0:
        return None
    try:
        return ((current_balance - last_balance) / last_balance) * 100
    except ZeroDivisionError:
        return None

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
async def fetch_bscscan_transactions():
    global transaction_cache, last_transaction_fetch
    if datetime.now().timestamp() * 1000 - last_transaction_fetch < TRANSACTION_CACHE_DURATION and transaction_cache:
        logger.info("Returning cached BscScan transactions")
        return transaction_cache
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=account&action=tokentx&contractaddress={PETS_BSC_ADDRESS}&address={TARGET_ADDRESS}&page=1&offset=50&sort=desc&apikey={BSCSCAN_API_KEY}",
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
                for tx in data['result']
            ]
            last_transaction_fetch = datetime.now().timestamp() * 1000
            logger.info(f"Fetched {len(transaction_cache)} transactions from BscScan")
            return transaction_cache
        raise ValueError("Invalid BscScan response")
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
            return
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(chat_id, f"{options['caption']}\n\n⚠️ Video unavailable.", parse_mode='Markdown')
                logger.info(f"Sent fallback text message to chat {chat_id}")

async def process_transaction(context, transaction, bnb_to_usd_rate):
    global posted_transactions
    if transaction['transactionHash'] in posted_transactions:
        logger.info(f"Transaction {transaction['transactionHash']} already processed. Skipping.")
        return

    is_execute, bnb_value = check_execute_function(transaction['transactionHash'])
    if not is_execute or not bnb_value:
        logger.info(f"Transaction {transaction['transactionHash']} is not an 'Execute' transaction or lacks BNB value. Skipping.")
        return

    pets_amount = float(transaction['value']) / 1e18
    usd_value = bnb_value * bnb_to_usd_rate
    if usd_value < 50:  # Minimum buy threshold
        logger.info(f"Transaction {transaction['transactionHash']} below $50 threshold. Skipping.")
        return

    market_cap = extract_market_cap_bscscan()
    wallet_address = transaction['to']
    balance_before = get_balance_before_transaction(wallet_address)
    percent_increase = calculate_percent_increase(balance_before, balance_before + Decimal(pets_amount))
    holding_change_text = f"+{percent_increase:.2f}%" if percent_increase is not None else "N/A"
    emoji_count = min(int(usd_value) // 10, 100)
    emojis = config['emoji'] * emoji_count
    tx_url = f"https://bscscan.com/tx/{transaction['transactionHash']}"
    category = categorize_buy(usd_value)
    video_url = get_video_url(category)

    message = (
        f"🚀 MicroPets Buy! BNBchain 💰\n\n"
        f"{emojis}\n"
        f"💵 BNB Value: {bnb_value:,.4f} ($ {usd_value:,.2f})\n"
        f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS}): {pets_amount:,.0f}\n"
        f"🏦 Market Cap: ${market_cap:,.0f}\n"
        f"🔼 Holding Change: {holding_change_text}\n"
        f"🤑 Hodler: {shorten_address(wallet_address)}\n"
        f"[🔍 View on BscScan]({tx_url})\n\n"
        f"💰 [Staking](https://pets.micropets.io/petdex) "
        f"📈 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
        f"🛍 [Merch](https://micropets.store/) "
        f"🤑 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
    )

    try:
        await send_video_with_retry(context, TELEGRAM_CHAT_ID, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        logger.info(f"Message sent to chat {TELEGRAM_CHAT_ID} for transaction {transaction['transactionHash']}")
        posted_transactions.add(transaction['transactionHash'])
        log_posted_transaction(transaction['transactionHash'])
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)

async def monitor_transactions(context):
    global last_tx_hash, is_tracking_enabled
    async with lock:
        if not is_tracking_enabled:
            logger.info("Tracking is disabled.")
            return
        try:
            posted_transactions.update(load_posted_transactions())
            logger.info("Checking for new transactions...")
            txs = await fetch_bscscan_transactions()
            if not txs:
                logger.info("No new transactions found.")
                return
            bnb_to_usd_rate = get_bnb_to_usd()
            for tx in reversed(txs):  # Process latest first
                if last_tx_hash and tx['transactionHash'] == last_tx_hash:
                    break
                if tx['from'].lower() != TARGET_ADDRESS.lower():
                    logger.info(f"Transaction {tx['transactionHash']} is not from target address. Skipping.")
                    continue
                if tx['transactionHash'] in posted_transactions:
                    logger.info(f"Transaction {tx['transactionHash']} already processed. Skipping.")
                    continue
                await process_transaction(context, tx, bnb_to_usd_rate)
                last_tx_hash = tx['transactionHash']
            if last_tx_hash:
                logger.info(f"Updated last transaction hash: {last_tx_hash}")
        except Exception as e:
            logger.error(f"Error during transaction monitoring: {e}")
            recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
            if len(recent_errors) > 50:
                recent_errors.pop(0)
        await asyncio.sleep(60)

def is_admin(update):
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

# Command handlers
async def start(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /start for chat {chat_id}")
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id, "👋 Welcome to PETS Tracker! Use /track to start receiving buy alerts for $PETS.")

async def track(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /track for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    active_chats.add(str(chat_id))
    global is_tracking_enabled
    is_tracking_enabled = True
    await context.bot.send_message(chat_id, "🚀 Tracking $PETS buys started!")
    asyncio.create_task(monitor_transactions(context))

async def stop(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stop for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    active_chats.discard(str(chat_id))
    global is_tracking_enabled
    is_tracking_enabled = False
    await context.bot.send_message(chat_id, "🛑 **Tracking $PETS buys stopped.**")

async def stats(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stats for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Fetching $PETS transaction data...")
    try:
        txs = await fetch_bscscan_transactions()
        if not txs:
            await context.bot.send_message(chat_id, "🚫 No recent transactions found.")
            return
        latest_tx = txs[0]  # Latest transaction
        bnb_to_usd_rate = get_bnb_to_usd()
        is_execute, bnb_value = check_execute_function(latest_tx['transactionHash'])
        if not is_execute or not bnb_value:
            await context.bot.send_message(chat_id, "🚫 Latest transaction is not an 'Execute' transaction.")
            return
        pets_amount = float(latest_tx['value']) / 1e18
        usd_value = bnb_value * bnb_to_usd_rate
        market_cap = extract_market_cap_bscscan()
        wallet_address = latest_tx['to']
        balance_before = get_balance_before_transaction(wallet_address)
        percent_increase = calculate_percent_increase(balance_before, balance_before + Decimal(pets_amount))
        holding_change_text = f"+{percent_increase:.2f}%" if percent_increase is not None else "N/A"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = config['emoji'] * emoji_count
        tx_url = f"https://bscscan.com/tx/{latest_tx['transactionHash']}"
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)

        message = (
            f"🚀 MicroPets Buy! BNBchain 💰\n\n"
            f"{emojis}\n"
            f"💵 BNB Value: {bnb_value:,.4f} ($ {usd_value:,.2f})\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS}): {pets_amount:,.0f}\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding Change: {holding_change_text}\n"
            f"🤑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on BscScan]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/petdex) "
            f"📈 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
            f"🛍 [Merch](https://micropets.store/) "
            f"🤑 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
        )
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Error fetching $PETS data. APIs may be down or rate-limited. Please try again later.")

async def help_command(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /help for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(
        chat_id,
        "🆘 *Available commands:*\n/start - Start the bot\n/track - Enable buy alerts\n/stop - Disable buy alerts\n"
        "/stats - View latest buy from $PETS\n/volume - View buy volume chart\n/status - Check tracking status\n"
        "/test - Show a sample buy template\n/noV - Show sample format without video\n/set_emoji - Set emoji for messages\n"
        "/set_threshold - Set video thresholds\n/debug - View bot status and errors\n/help - Show this message",
        parse_mode='Markdown'
    )

async def status(update, context):
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

async def volume(update, context):
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
        await context.bot.send_message(chat_id, "🚫 Error generating volume chart.")

async def debug(update, context):
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
            'lastTransactionFetch': datetime.fromtimestamp(last_transaction_fetch / 1000).isoformat() if last_transaction_fetch else 'N/A'
        }
    }
    await context.bot.send_message(chat_id, f"🔍 **Debug Info**\n```json\n{json.dumps(status, indent=2)}\n```", parse_mode='Markdown')

async def test(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /test for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test $PETS buy data...")
    try:
        test_tx_hash = '0xRandomTxHash456'
        test_pets_amount = random.randint(1000, 50000)
        usd_value = random.uniform(50, 2000)
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randrange(16**12):012x}{random.randrange(16**4):04x}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = config['emoji'] * emoji_count
        market_cap = extract_market_cap_bscscan()
        holding_change_text = "N/A"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚀 MicroPets Buy! BNBchain 💰\n\n"
            f"{emojis}\n"
            f"💵 BNB Value: {bnb_value:,.4f} ($ {usd_value:,.2f})\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS}): {test_pets_amount:,.0f}\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding Change: {holding_change_text}\n"
            f"🤑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on BscScan]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/petdex) "
            f"📈 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
            f"🛍 [Merch](https://micropets.store/) "
            f"🤑 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
        )
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        await context.bot.send_message(chat_id, "🚀 Test $PETS buy executed successfully!")
    except Exception as e:
        logger.error(f"Error in /test: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Error executing test command.")

async def no_video(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /noV for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test $PETS buy data (no video)...")
    try:
        test_tx_hash = '0xRandomTxHash456'
        test_pets_amount = random.randint(1000, 50000)
        usd_value = random.uniform(50, 2000)
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        wallet_address = f"0x{random.randrange(16**12):012x}{random.randrange(16**4):04x}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = config['emoji'] * emoji_count
        market_cap = extract_market_cap_bscscan()
        holding_change_text = "N/A"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚀 MicroPets Buy! BNBchain 💰\n\n"
            f"{emojis}\n"
            f"💵 BNB Value: {bnb_value:,.4f} ($ {usd_value:,.2f})\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS}): {test_pets_amount:,.0f}\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding Change: {holding_change_text}\n"
            f"🤑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on BscScan]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/petdex) "
            f"📈 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
            f"🛍 [Merch](https://micropets.store/) "
            f"🤑 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={PETS_BSC_ADDRESS})"
        )
        await context.bot.send_message(chat_id, message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in /noV: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 **Error executing /noV command.**")

async def set_emoji(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /set_emoji for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    emoji = ' '.join(context.args)
    if not emoji:
        await context.bot.send_message(chat_id, "🚫 Please provide an emoji.")
        return
    config['emoji'] = emoji
    save_config(config)
    await context.bot.send_message(chat_id, f"Emoji updated to: {emoji}")
    logger.info(f"Emoji updated to: {emoji}")

async def set_threshold(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /set_threshold for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 You are not authorized to use this bot.")
        return
    if len(context.args) != 2:
        await context.bot.send_message(chat_id, "🚫 Usage: /set_threshold <small|medium|large> <value>")
        return
    threshold_type, value = context.args
    try:
        value = int(value)
        if threshold_type in ['small', 'medium', 'large']:
            config[f'{threshold_type}_threshold'] = value
            save_config(config)
            await context.bot.send_message(chat_id, f"{threshold_type.capitalize()} threshold updated to: {value}")
            logger.info(f"{threshold_type.capitalize()} threshold updated to: {value}")
        else:
            await context.bot.send_message(chat_id, "🚫 Invalid threshold type. Use 'small', 'medium', or 'large'.")
    except ValueError:
        await context.bot.send_message(chat_id, "🚫 Threshold value must be an integer.")

# FastAPI routes
@app.get("/api/transactions")
async def get_transactions():
    logger.info("GET /api/transactions called")
    return transactions

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received webhook update")
    try:
        update = Update.de_json(await request.json(), bot_app.bot)
        await bot_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Failed to process webhook: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': f"Webhook failed: {e}"})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        return {"status": "error"}, 500

# Bot initialization
bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("track", track))
bot_app.add_handler(CommandHandler("stop", stop))
bot_app.add_handler(CommandHandler("stats", stats))
bot_app.add_handler(CommandHandler("help", help_command))
bot_app.add_handler(CommandHandler("status", status))
bot_app.add_handler(CommandHandler("volume", volume))
bot_app.add_handler(CommandHandler("debug", debug))
bot_app.add_handler(CommandHandler("test", test))
bot_app.add_handler(CommandHandler("noV", no_video))
bot_app.add_handler(CommandHandler("set_emoji", set_emoji))
bot_app.add_handler(CommandHandler("set_threshold", set_threshold))

@app.on_event("startup")
async def startup_event():
    await bot_app.initialize()
    logger.info("Telegram bot application initialized")
    webhook_url = f"{APP_URL}/webhook"
    await bot_app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

@app.on_event("shutdown")
async def shutdown_event():
    await bot_app.shutdown()
    logger.info("Telegram bot application shut down")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
