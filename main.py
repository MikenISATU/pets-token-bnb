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
APP_URL = os.getenv('RAILWAY_PUBLIC_DOMAIN', os.getenv('APP_URL'))
BSCSCAN_API_KEY = os.getenv('BSCSCAN_API_KEY')
BNB_RPC_URL = os.getenv('BNB_RPC_URL')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS', '0x2466858ab5edAd0BB597FE9f008F568B00d25Fe3')
ADMIN_CHAT_ID = os.getenv('ADMIN_USER_ID')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
PORT = int(os.getenv('PORT', 8080))
COINMARKETCAP_API_KEY = os.getenv('COINMARKETCAP_API_KEY', '')
TARGET_ADDRESS = os.getenv('TARGET_ADDRESS', '0x4BdEcE4E422fA015336234e4fC4D39ae6dD75b01')

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
    (TELEGRAM_CHAT_ID, 'TELEGRAM_CHAT_ID'),
    (TARGET_ADDRESS, 'TARGET_ADDRESS')
]:
    if not var:
        missing_vars.append(name)
if missing_vars:
    logger.error(f"Missing critical environment variables: {', '.join(missing_vars)}")
    raise SystemExit(1)

logger.info(f"Environment variables loaded: APP_URL={APP_URL}, TELEGRAM_BOT_TOKEN=*****, BSCSCAN_API_KEY=*****, CLOUDINARY_CLOUD_NAME={CLOUDINARY_CLOUD_NAME}, ADMIN_CHAT_ID={ADMIN_CHAT_ID}, CONTRACT_ADDRESS={CONTRACT_ADDRESS}, TARGET_ADDRESS={TARGET_ADDRESS}, PORT={PORT}, BNB_RPC_URL=*****")

# Constants
EMOJI = '💰'

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
posted_transactions = set()

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
def get_pets_price_from_coingecko():
    try:
        response = requests.get(
            'https://api.coingecko.com/api/v3/simple/price?ids=micropets&vs_currencies=usd',
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        price = float(data.get('micropets', {}).get('usd', 0))
        if price == 0:
            logger.warning("CoinGecko returned zero price for $PETS")
            return None
        logger.info(f"Fetched $PETS price from CoinGecko: ${price:.10f}")
        return price
    except Exception as e:
        logger.error(f"Error fetching $PETS price from CoinGecko: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_pets_price_from_coinmarketcap():
    if not COINMARKETCAP_API_KEY:
        logger.warning("CoinMarketCap API key not provided, skipping")
        return None
    try:
        url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
        headers = {
            'Accepts': 'application/json',
            'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY
        }
        params = {
            'symbol': 'PETS',
            'convert': 'USD'
        }
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        price = float(data['data']['PETS']['quote']['USD']['price'])
        logger.info(f"Fetched $PETS price from CoinMarketCap: ${price:.10f}")
        return price
    except Exception as e:
        logger.error(f"Error fetching $PETS price from CoinMarketCap: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_pets_price_from_pancakeswap():
    try:
        pair_address = Web3.to_checksum_address(TARGET_ADDRESS)
        pair_contract = w3.eth.contract(address=pair_address, abi=PANCAKESWAP_PAIR_ABI)
        reserves = pair_contract.functions.getReserves().call()
        reserve0, reserve1, _ = reserves
        bnb_per_pets = reserve1 / reserve0 / 1e18 if reserve0 > 0 else 0
        bnb_to_usd = get_bnb_to_usd()
        pets_price_usd = bnb_per_pets * bnb_to_usd
        if pets_price_usd <= 0:
            raise ValueError("Invalid price from PancakeSwap")
        logger.info(f"Fetched $PETS price from PancakeSwap: ${pets_price_usd:.10f}")
        return pets_price_usd
    except Exception as e:
        logger.error(f"Error fetching $PETS price from PancakeSwap: {e}")
        return get_pets_price_from_coingecko() or get_pets_price_from_coinmarketcap() or 0.00003886

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def get_token_supply():
    url = f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&apikey={BSCSCAN_API_KEY}"
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
    return 6_604_885_020

@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def extract_market_cap():
    try:
        price = get_pets_price_from_pancakeswap()
        token_supply = get_token_supply()
        market_cap = token_supply * price
        market_cap_int = int(market_cap)
        logger.info(
            f"Calculated real-time market cap for $PETS: ${market_cap_int:,} "
            f"(price=${price:.10f}, supply={token_supply:,.0f})"
        )
        return market_cap_int
    except Exception as e:
        logger.error(f"Error calculating market cap: {e}")
        return 256600

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

def check_execute_function(transaction_hash):
    url = f"https://api.bscscan.com/api?module=transaction&action=gettxreceiptstatus&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        status = data.get('result', {}).get('status', '')
        bnb_value = get_transaction_details(transaction_hash)
        tx_url = f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}"
        tx_response = requests.get(tx_url, timeout=30)
        tx_response.raise_for_status()
        tx_data = tx_response.json()
        input_data = tx_data.get('result', {}).get('input', '')
        is_execute = 'execute' in input_data.lower()
        logger.info(
            f"Transaction {transaction_hash}: Execute={is_execute}, "
            f"BNB={bnb_value}, Status={status}"
        )
        return is_execute, bnb_value
    except Exception as e:
        logger.error(f"Error checking transaction {transaction_hash}: {e}")
        bnb_value = get_transaction_details(transaction_hash)
        return False, bnb_value

def get_balance_before_transaction(wallet_address, block_number):
    url = f"https://api.bscscan.com/api?module=account&action=tokenbalancehistory&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&address={wallet_address}&blockno={block_number}&apikey={BSCSCAN_API_KEY}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            balance = Decimal(data['result']) / Decimal(1e18)
            logger.info(f"Balance for {shorten_address(wallet_address)} at block {block_number}: {balance:,.0f} tokens")
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
    if (datetime.now().timestamp() * 1000 - last_transaction_fetch <
            TRANSACTION_CACHE_THRESHOLD and transaction_cache):
        logger.info(f"Returning {len(transaction_cache)} cached transactions")
        return transaction_cache
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=account&action=tokentx"
            f"&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&address={Web3.to_checksum_address(TARGET_ADDRESS)}"
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
            return
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(
                    chat_id,
                    f"{options['caption']}\n\n⚠️ Video unavailable.",
                    parse_mode='Markdown'
                )
                logger.info(f"Sent fallback text to chat {chat_id}")

async def process_transaction(context, transaction, bnb_to_usd_rate, pets_price, chat_id=TELEGRAM_CHAT_ID):
    global posted_transactions
    if transaction['transactionHash'] in posted_transactions:
        logger.info(f"Transaction {transaction['transactionHash']} already processed")
        return False

    is_execute, bnb_value = check_execute_function(transaction['transactionHash'])
    if not bnb_value:
        logger.info(f"Transaction {transaction['transactionHash']} lacks BNB value")
        return False

    pets_amount = float(transaction['value']) / 1e18
    usd_value = pets_amount * pets_price
    logger.info(
        f"Transaction {transaction['transactionHash']}: "
        f"PETS={pets_amount:,.0f}, USD={usd_value:,.2f}, PETS_price={pets_price:.10f}, "
        f"BNB={bnb_value:.6f} (${(bnb_value * bnb_to_usd_rate):,.2f})"
    )
    if usd_value < 1:
        logger.info(f"Transaction {transaction['transactionHash']} below threshold $1")
        return False

    market_cap = extract_market_cap()
    wallet_address = transaction['to']
    balance_before = get_balance_before_transaction(wallet_address, transaction['blockNumber'])
    percent_increase = calculate_percent_increase(
        balance_before,
        balance_before + Decimal(pets_amount) if balance_before is not None else None
    )
    holding_change_text = f"+{percent_increase:.2f}%" if percent_increase else "N/A"
    emoji_count = min(int(usd_value) // 1, 100)
    emojis = EMOJI * emoji_count
    tx_url = f"https://bscscan.com/tx/{transaction['transactionHash']}"
    category = categorize_buy(usd_value)
    video_url = get_video_url(category)

    message = (
        f"🚀 MicroPets Buy! BNBchain 💰\n\n"
        f"{emojis}\n"
        f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): "
        f"{pets_amount:,.0f} (${usd_value:,.2f})\n"
        f"💵 BNB Value: {bnb_value:,.4f} (${(bnb_value * bnb_to_usd_rate):,.2f})\n"
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
        await send_video_with_retry(
            context,
            chat_id,
            video_url,
            {'caption': message, 'parse_mode': 'Markdown'}
        )
        logger.info(
            f"Sent message for transaction {transaction['transactionHash']} to chat {chat_id}"
        )
        posted_transactions.add(transaction['transactionHash'])
        log_posted_transaction(transaction['transactionHash'])
        return True
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        return False

async def monitor_transactions(context):
    global last_transaction_hash, is_tracking_enabled
    async with asyncio.Lock():
        if not is_tracking_enabled:
            logger.info("Tracking disabled")
            return
        logger.info("Starting transaction monitoring")
        try:
            posted_transactions.update(load_posted_transactions())
            logger.info(f"Loaded {len(posted_transactions)} posted transactions")
            txs = await fetch_bscscan_transactions()
            logger.info(f"Fetched {len(txs)} transactions")
            if not txs:
                logger.info("No new transactions")
                return
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
        logger.info(f"Sleeping for {int(os.getenv('POLL_INTERVAL', 60))} seconds")
        await asyncio.sleep(int(os.getenv('POLL_INTERVAL', 60)))

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
    is_tracking_enabled = True
    await context.bot.send_message(chat_id, "🚀 Tracking started")
    asyncio.create_task(monitor_transactions(context))

async def stop(update: Update, context):
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
            await context.bot.send_message(chat_id, "🚫 No transactions found meeting $1 threshold")
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

async def help_command(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /help for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🆘 *Commands:*\n\n"
            "/start - Start bot\n"
            "/track - Enable alerts\n"
            "/stop - Disable alerts\n"
            "/stats - View buys\n"
            "/status - Track status\n"
            "/test - Test transaction\n"
            "/noV - Test without video\n"
            "/debug - Debug info\n"
            "/help - This message"
        ),
        parse_mode='Markdown'
    )

async def status(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /status for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id,
        f"🔍 *Status:* {'Enabled' if str(chat_id) in active_chats else 'Disabled'}",
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

async def test(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /test for chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test buy")
    try:
        test_tx_hash = '0xRandomTestTx'
        test_pets_amount = random.randint(1000, 50000)
        usd_value = random.uniform(1, 500)
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        pets_price = get_pets_price_from_pancakeswap()
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randint(10**15, 10**16):0>40x}"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = "N/A"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚖 MicroPets Buy! Test\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): "
            f"{test_pets_amount:,.0f} (${(test_pets_amount * pets_price):,.2f})\n"
            f"💵 BNB Value: {bnb_value:,.4f} (${(bnb_value * bnb_to_usd_rate):,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦲 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍]({tx_url})\n\n"
            f"[💰 Staking](https://pets.micropets.io/) "
            f"[📈 Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🤑 Buy](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await send_video_with_retry(
            context,
            chat_id,
            video_url,
            {'caption': message, 'parse_mode': 'Markdown'}
        )
        await context.bot.send_message(chat_id, "🚖 Success")
    except Exception as e:
        logger.error(f"Test error: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Failed")

async def no_video(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Processing /noV for {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Testing buy (no video)")
    try:
        test_tx_hash = '0xRandomTestNoV'
        test_pets_amount = random.randint(1000, 50000)
        usd_value = random.uniform(1, 5000)
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        pets_price = get_pets_price_from_pancakeswap()
        wallet_address = f"0x{random.randint(10**15, 10**16):0>40x}"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = "N/A"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚖 MicroPets Buy! BNBchain\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): "
            f"{test_pets_amount:,.0f} (${(test_pets_amount * pets_price):,.2f})\n"
            f"💵 BNB Value: {bnb_value:,.4f} (${(bnb_value * bnb_to_usd_rate):,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦀 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍]({tx_url})\n\n"
            f"[💰 Staking](https://pets.micropets.io/) "
            f"[📈 Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[💖 Buy](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await context.bot.send_message(chat_id, message, parse_mode='Markdown')
        await context.bot.send_message(chat_id, "🚖 OK")
    except Exception as e:
        logger.error(f"/noV error: {e}")
        recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        await context.bot.send_message(chat_id, "🚫 Failed")

# FastAPI routes
@app.get("/")
async def health_check():
    return {"status": "Bot is running"}

@app.get("/webhook")
async def webhook_get():
    logger.info("Received GET request to /webhook")
    return {"status": "This endpoint only accepts POST requests for Telegram webhooks. Use GET / for health checks."}

@app.get("/api/transactions")
async def get_transactions():
    logger.info("GET /transactions")
    return transaction_cache

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received webhook")
    try:
        update = Update.de_json(await request.json(), bot_app.bot)
        await bot_app.process_update(update)
        return {"status": "OK"}
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
        asyncio.create_task(monitor_transactions(bot_app))
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
bot_app.add_handler(CommandHandler("help", help_command))
bot_app.add_handler(CommandHandler("status", status))
bot_app.add_handler(CommandHandler("debug", debug))
bot_app.add_handler(CommandHandler("test", test))
bot_app.add_handler(CommandHandler("noV", no_video))

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting Uvicorn server on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
