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
from pycoingecko import CoinGeckoAPI

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

# Initialize CoinGecko client
cg = CoinGeckoAPI()

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
APP_URL = os.getenv('APP_URL')
BSCSCAN_API_KEY = os.getenv('BSCSCAN_API_KEY')
BNB_RPC_URL = os.getenv('BNB_RPC_URL')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS')
ADMIN_CHAT_ID = os.getenv('ADMIN_USER_ID')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
PORT = int(os.getenv('PORT', 8080))

# Validate environment variables
missing_vars = []
for var, name in [
    (TELEGRAM_BOT_TOKEN, 'TELEGRAM_BOT_TOKEN'),
    (CLOUDINARY_CLOUD_NAME, 'CLOUDINARY_CLOUD_NAME'),
    (APP_URL, 'APP_URL'),
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
TARGET_ADDRESS = '0x4BDECe4E422fA015336234e4fC4D39ae6dD75b01'
CONFIG_FILE = 'config.json'

# Video mapping for Cloudinary
cloudinary_videos = {
    'MicroPets Buy': 'SMALLBUY_b3px1p',
    'Medium Bullish Buy': 'MEDIUMBUY_MPEG_e02zdz',
    'Whale Buy': 'micropets_big_msapxz',
    'Extra Large Buy': 'micropets_big_msapxz'
}

# In-memory data
transactions = []
transaction_cache = []
active_chats = {TELEGRAM_CHAT_ID}
last_transaction_hash = None
is_tracking_enabled = False
recent_errors = []
last_transaction_fetch = 0
TRANSACTION_CACHE_DURATION = 2 * 60 * 1000  # 2 minutes
cached_market_cap = '$10,000,000'
last_market_cap_fetch = 0
MARKET_CAP_CACHE_DURATION = 5 * 60 * 1000  # 5 minutes
posted_transactions = set()
lock = asyncio.Lock()

# Load configuration
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {'small_threshold': 100, 'medium_threshold': 500, 'large_threshold': 1000, 'emoji': '💰'}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

config = load_config()

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
    with open('posted_transactions.txt', 'r') as f:
        return set(line.strip() for line in f)

def log_posted_transaction(transaction_hash):
    with open('posted_transactions.txt', 'a') as f:
        f.write(transaction_hash + '\n')

def get_bnb_to_usd():
    try:
        data = cg.get_price(ids='binancecoin', vs_currencies='usd')
        price = float(data['binancecoin']['usd'])
        logger.info(f"Fetched BNB price: ${price:.2f}")
        return price
    except Exception as e:
        logger.error(f"Error fetching BNB price: {e}")
        return 600  # Fallback price

def get_token_price():
    try:
        # Try CoinGecko by contract address
        data = cg.get_coin_by_contract(contract_address=CONTRACT_ADDRESS, platform_id='binance-smart-chain')
        price = float(data.get('market_data', {}).get('current_price', {}).get('usd', 0))
        logger.info(f"CoinGecko response for $PETS: price=${price:.10f}")
        if price > 0:
            return price
        logger.warning("Token price not found on CoinGecko, trying PancakeSwap.")
    except Exception as e:
        logger.error(f"Error fetching token price from CoinGecko: {e}")

    # Fallback to PancakeSwap API
    try:
        response = requests.get(f"https://api.pancakeswap.info/api/v2/tokens/{CONTRACT_ADDRESS}", timeout=10)
        response.raise_for_status()
        data = response.json()
        price = float(data['data']['price'])
        logger.info(f"PancakeSwap price for $PETS: ${price:.10f}")
        if price > 0:
            return price
        logger.warning("Token price not found on PancakeSwap, using fallback.")
    except Exception as e:
        logger.error(f"Error fetching token price from PancakeSwap: {e}")
    return 0.0000001  # Fallback price

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_token_supply():
    url = f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={CONTRACT_ADDRESS}&apikey={BSCSCAN_API_KEY}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            supply = int(data['result']) / 1e18  # Adjust for 18 decimals
            logger.info(f"Fetched token supply: {supply:,.0f} tokens")
            return supply
        logger.error(f"API Error fetching token supply: {data['message']}")
        return 1000000000000  # Fallback supply (1 trillion)
    except Exception as e:
        logger.error(f"Error fetching token supply: {e}")
        return 1000000000000

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def extract_market_cap_coingecko():
    global last_market_cap_fetch, cached_market_cap
    if datetime.now().timestamp() * 1000 - last_market_cap_fetch < MARKET_CAP_CACHE_DURATION and cached_market_cap != '$10,000,000':
        return int(cached_market_cap.replace('$', '').replace(',', ''))
    try:
        price = get_token_price()
        token_supply = get_token_supply()
        market_cap = token_supply * price
        market_cap_int = int(market_cap)
        cached_market_cap = f'${market_cap_int:,}'
        last_market_cap_fetch = datetime.now().timestamp() * 1000
        logger.info(f"Calculated market cap: ${market_cap_int:,} (price=${price:.10f}, supply={token_supply:,.0f})")
        return market_cap_int
    except Exception as e:
        logger.error(f"Error calculating market cap: {e}")
        return 10000000  # Fallback market cap

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_transaction_details(transaction_hash):
    url = f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get('result'):
            value_wei = int(data['result'].get('value', '0'), 16)
            bnb_value = float(w3.from_wei(value_wei, 'ether'))
            logger.info(f"Transaction {transaction_hash}: BNB value={bnb_value:.6f}")
            return bnb_value
        logger.error(f"No transaction details for {transaction_hash}")
        return None
    except Exception as e:
        logger.error(f"Error fetching transaction details for {transaction_hash}: {e}")
        return None

def extract_bnb_value(transaction_soup):
    potential_values = transaction_soup.find_all('span', {'data-bs-toggle': 'tooltip'})
    for value_span in potential_values:
        value_text = value_span.text.strip().replace(',', '')
        if re.match(r'^\d+(\.\d+)?$', value_text):
            try:
                return float(value_text)
            except ValueError:
                continue
    logger.error("No valid BNB value found in transaction details")
    return None

def check_execute_function(transaction_hash):
    transaction_url = f"https://bscscan.com/tx/{transaction_hash}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(transaction_url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        bnb_value = extract_bnb_value(soup)
        execute_badge = soup.find(string=re.compile("Execute", re.IGNORECASE))
        if not bnb_value:
            bnb_value = get_transaction_details(transaction_hash)
        logger.info(f"Transaction {transaction_hash}: Execute={bool(execute_badge)}, BNB={bnb_value}")
        return bool(execute_badge), bnb_value
    except Exception as e:
        logger.error(f"Error checking transaction {transaction_hash}: {e}")
        bnb_value = get_transaction_details(transaction_hash)
        return False, bnb_value

def get_balance_before_transaction(wallet_address):
    url = f"https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={CONTRACT_ADDRESS}&address={wallet_address}&tag=latest&apikey={BSCSCAN_API_KEY}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            balance = Decimal(data['result']) / Decimal(1e18)
            logger.info(f"Balance for {shorten_address(wallet_address)}: {balance:,.0f} tokens")
            return balance
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
        logger.info(f"Returning {len(transaction_cache)} cached transactions")
        return transaction_cache
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=account&action=tokentx&contractaddress={CONTRACT_ADDRESS}&address={TARGET_ADDRESS}&page=1&offset=50&sort=desc&apikey={BSCSCAN_API_KEY}",
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
            return
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(chat_id, f"{options['caption']}\n\n⚠️ Video unavailable.", parse_mode='Markdown')
                logger.info(f"Sent fallback text to chat {chat_id}")

async def process_transaction(context, transaction, bnb_to_usd_rate, chat_id=TELEGRAM_CHAT_ID):
    global posted_transactions
    if transaction['transactionHash'] in posted_transactions:
        logger.info(f"Transaction {transaction['transactionHash']} already processed")
        return False

    is_execute, bnb_value = check_execute_function(transaction['transactionHash'])
    if not bnb_value:
        logger.info(f"Transaction {transaction['transactionHash']} lacks BNB value")
        return False

    pets_amount = float(transaction['value']) / 1e18
    usd_value = bnb_value * bnb_to_usd_rate
    logger.info(f"Transaction {transaction['transactionHash']}: PETS={pets_amount:,.0f}, USD={usd_value:,.2f}")
    if usd_value < 1:
        logger.info(f"Transaction {transaction['transactionHash']} below $1")
        return False

    market_cap = extract_market_cap_coingecko()
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
        f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): {pets_amount:,.0f}\n"
        f"🏦 Market Cap: ${market_cap:,.0f}\n"
        f"🔼 Holding Change: {holding_change_text}\n"
        f"🤑 Hodler: {shorten_address(wallet_address)}\n"
        f"[🔍 View on BscScan]({tx_url})\n\n"
        f"💰 [Staking](https://pets.micropets.io/petdex) "
        f"📈 [Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
        f"🛍 [Merch](https://micropets.store/) "
        f"🤑 [Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
    )

    try:
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        logger.info(f"Sent message for transaction {transaction['transactionHash']} to chat {chat_id}")
        posted_transactions.add(transaction['transactionHash'])
        log_posted_transaction(transaction['transactionHash'])
        return True
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        return False

async def monitor_transactions(context):
    global last_transaction_hash, is_tracking_enabled
    async with lock:
        if not is_tracking_enabled:
            logger.info("Tracking disabled")
            return
        try:
            posted_transactions.update(load_posted_transactions())
            logger.info("Checking for new transactions")
            txs = await fetch_bscscan_transactions()
            if not txs:
                logger.info("No new transactions")
                return
            bnb_to_usd_rate = get_bnb_to_usd()
            for tx in reversed(txs):
                if last_transaction_hash and tx['transactionHash'] == last_transaction_hash:
                    break
                await process_transaction(context, tx, bnb_to_usd_rate)
                last_transaction_hash = tx['transactionHash']
            if last_transaction_hash:
                logger.info(f"Updated last transaction: {last_transaction_hash}")
        except Exception as e:
            logger.error(f"Error monitoring transactions: {e}")
            recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
            if len(recent_errors) > 50:
                recent_errors.pop(0)
        await asyncio.sleep(int(os.getenv('POLL_INTERVAL', 60)))

def is_admin(update):
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

# Command handlers
async def start(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /start for chat {chat_id}")
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id, "👋 Welcome to PETS Tracker! Use /track to start buy alerts.")

async def track(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /track for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    active_chats.add(str(chat_id))
    global is_tracking_enabled
    is_tracking_enabled = True
    await context.bot.send_message(chat_id, "🚀 Tracking started")
    asyncio.create_task(monitor_transactions(context))

async def stop(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /stop for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    active_chats.discard(str(chat_id))
    global is_tracking_enabled
    is_tracking_enabled = False
    logger.info("Tracking disabled")
    await context.bot.send_message(chat_id, "🛑 Tracking stopped")

async def stats(update, context):
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
        logger.info(f"BNB to USD rate: ${bnb_to_usd_rate:.2f}")
        market_cap = extract_market_cap_coingecko()
        processed = []
        seen_hashes = set()
        for tx in txs[:5]:
            if tx['transactionHash'] in seen_hashes:
                logger.info(f"Skipping duplicate {tx['transactionHash']}")
                continue
            logger.info(f"Processing {tx['transactionHash']}")
            if await process_transaction(context, tx, bnb_to_usd_rate, chat_id=chat_id):
                processed.append(tx['transactionHash'])
            seen_hashes.add(tx['transactionHash'])
        if not processed:
            await context.bot.send_message(chat_id, "🚫 No recent buys meet criteria")
        else:
            await context.bot.send_message(
                chat_id,
                f"✅ Processed {len(processed)} buys:\n" + "\n".join(processed)
            )
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Failed to fetch data")

async def help_command(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /help for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id,
        "🆘 *Commands:*\n\n"
        "/start - Start bot\n"
        "/track - Enable alerts\n"
        "/stop - Latest buys\n"
        "/stats - View buys\n"
        "/volume - Buy volume\n"
        "/status - Track status\n"
        "/test - Test tx\n"
        "/noV - Test no video\n"
        "/set_emoji - Set emoji\n"
        "/set_threshold - Set thresholds\n"
        "/debug - Debug info\n"
        "/help - This message",
        parse_mode='Markdown'
    )

async def status(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /status for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id,
        f"🔍 *Status:* {'Enabled' if str(chat_id) in active_chats else 'Disabled'}\n"
        f"*Total transactions:* {len(transactions)}",
        parse_mode='Markdown'
    )

async def volume(update Moran, context):
    chat_id = update.efficient_chat.id
    logger.info(f"Processing /volume for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Generating volume")
    try:
        transactions = await fetch_bscscan_transactions()
        volume_data = [
            {'tokens': round(float(w3.from_wei(int(tx['value']), 'ether')), 2), 'block': str(tx['blockNumber'])}]
            for tx in transactions[:5]
        ]
        await context.bot.send_message(chat_id, f"📊 Volume:\n{json.dumps(volume_data, indent=2)}")
    except Exception as e:
        logger.error(f"Error in /volume: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Failed to generate chart")

async def debug(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /debug for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    status = {
        'trackingEnabled': is_tracking_enabled,
        'activeChats': list(active_chats),
        'transactionCount': len(transactions),
        'lastTxHash': last_transaction_hash,
        'recentErrors': recent_errors[-5:],
        'apiStatus': {
            'bscWeb3': bool(w3),
            'lastTransactionFetch': datetime.fromtimestamp(
                last_transaction_fetch / 1000
            ).isoformat() if last_transaction_fetch else None
        }
    }
    await context.bot.send_message(
        chat_id, f"🔍 Debug:\n```json\n{json.dumps(status, indent=2)}\n```", parse_mode='Markdown'
    )

async def test(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /test for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test buy")
    try:
        test_tx_hash = '0xRandomTestTx'
        test_pets_amount = random.randint(1000, 50000)
        usd_value = random.uniform(50, 2000)
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randint(10**15, 10**16):0x40}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = config['emoji'] * emoji_count
        market_cap = extract_market_cap_coingecko()
        holding_change_text = "N/A"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚀 MicroPets Buy! Test\n\n"
            f"{emojis}\n"
            f"💵 BNB: {bnb_value:.4f} ($ {usd_value:.2f})\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): {test_pets_amount:,}\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔲 Holding: {holding_change_text}\n"
            f"🦸 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍]({tx_url})\n\n"
            f"[💰 Staking](https://pets.micropets.io/) "
            f"[📈 Chart](https://www.dextools.io/address/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🤑 Buy](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        await context.bot.send_message(chat_id, "🚀 Test completed")
    except Exception as e:
        logger.error(f"Test error: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Test failed")

async def no_video(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /noV for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Testing buy (no video)")
    try:
        test_tx_hash = '0xRandomTestNoV'
        test_pets_amount = random.randint(1000, 50000)
        usd_value = random.uniform(50, 5000)
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        wallet_address = f"0x{random.randint(10**15, 10**16):0x40}"
        emoji_count = min(int(USD_value) // 10, 100)
        emojis = config(['emoji'] * emoji_count
        market_cap = extract_market_cap_coingecko()
        holding_change_text = "N/A"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚀 MicroPets Buy! BNBchain\n\n"
            f"{emojis}\n"
            f"💵 BNB: {bnb_value:.4f} ($ {usd_value:.2f})\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): {test_pets_amount:,}\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔲 Holding: {holding_change_text}\n"
            f"🦀 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍]({tx_url})\n\n"
            f"[💰 Staking](https://pets.micropets.io/) "
            f"[📈 Chart](https://www.dextools.io/address/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🤑 Buy](https://pancakeswap.finance/swap?outputCurrency={target_ADDRESS})"
        )
        await context.bot.send_message(chat_id, message, parse_mode='Markdown')
        await context.bot.send_message(chat_id, "🚀 Test completed")
    except Exception as e:
        logger.error(f"/noV error: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 50:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Test failed")

async def set_emoji(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /set_emoji for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    emoji = ' '.join(context.args)
    if not emoji:
        await context.bot.send_message(chat_id, "🚫 Provide emoji")
        return
    config['emoji'] = emoji
    save_config(config)
    await context.bot.send_message(chat_id, f"Emoji set: {emoji}")
    logger.info(f"Emoji set: {emoji}")

async def set_threshold(update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /set_threshold for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    if len(context.args) != 2:
        await context.bot.send_message(chat_id, "🚫 Usage: /set_threshold <small|medium|large> value")
        return
    threshold_type, value = context.args
    try:
        value = int(value)
        if threshold_type in ['small', 'medium', 'large']:
            config[f'{threshold_type}_threshold'] = value
            save_config(config)
            await context.bot.send_message(chat_id, f"{threshold_type.capitalize()}: {value}")
            logger.info(f"{threshold_type.capitalize()}: {value}")
        else:
            await context.bot.send_message(chat_id, "🚫 Invalid type")
    except ValueError:
        await context.bot.send_message(chat_id, "🚫 Value must be integer")

# FastAPI routes
@app.get("/api/transactions")
async def get_transactions():
    logger.info("GET /transactions")
    return transactions

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received webhook")
    try:
        update = Update.de_json(await request.json(), bot_app.bot)
        await bot_app.process_update(update)
        return {"status": "OK"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': f"Error: {e}"})
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
    logger.info("Bot initialized")
    webhook_url = f"{APP_URL}/webhook"
    await bot_app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set: {webhook_url}")
    asyncio.create_task(monitor_transactions(bot_app))

@app.on_event("shutdown")
async def shutdown_event():
    await bot_app.shutdown()
    logger.info("Bot shutdown")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
