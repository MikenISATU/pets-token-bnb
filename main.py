import os
import logging
import requests
import random
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from web3 import Web3
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
import asyncio
from datetime import datetime
from decimal import Decimal
import json
import telegram
import aiohttp
import time

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
telegram_logger = logging.getLogger("telegram")
telegram_logger.setLevel(logging.WARNING)

# Check python-telegram-bot version
logger.info(f"python-telegram-bot version: {telegram.__version__}")
if not telegram.__version__.startswith('20'):
    logger.error(f"Expected python-telegram-bot v20.0+, got {telegram.__version__}")
    raise SystemExit(1)

# FastAPI app
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
COINMARKETCAP_API_KEY = os.getenv('COINMARKETCAP_API_KEY', 'b655515b-867c-4441-828e-cca367130f7a')
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
    logger.error(f"Missing environment variables: {', '.join(missing_vars)}")
    raise SystemExit(1)

logger.info(f"Environment loaded: APP_URL={APP_URL}, PORT={PORT}")

# Constants
EMOJI = '💰'
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
cloudinary_videos = {
    'MicroPets Buy': 'SMALLBUY_b3px1p',
    'Medium Bullish Buy': 'MEDIUMBUY_MPEG_e02zdz',
    'Whale Buy': 'micropets_big_msap',
    'Extra Large Buy': 'micropets_big_msapxz'
}
BNB_ADDRESS = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'  # BNB token address on BSC

# In-memory data
transaction_cache = []
active_chats = {TELEGRAM_CHAT_ID}
last_transaction_hash = None
is_tracking_enabled = True
recent_errors = []
last_transaction_fetch = 0
TRANSACTION_CACHE_THRESHOLD = 2 * 60 * 1000
posted_transactions = set()

# Initialize Web3
try:
    w3 = Web3(Web3.HTTPProvider(BNB_RPC_URL, request_kwargs={'timeout': 60}))
    logger.info("Web3 initialized with BNB_RPC_URL")
except Exception as e:
    logger.error(f"Failed to initialize Web3: {e}")
    w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed2.binance.org', request_kwargs={'timeout': 60}))
    logger.info("Web3 initialized with fallback")

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
    return f"{address[:6]}...{address[-4:]}" if address else ''

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

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_bnb_to_usd():
    try:
        # Try GeckoTerminal first
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/bsc/token_price/{BNB_ADDRESS}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        price = float(response.json()['data']['attributes']['token_prices'][BNB_ADDRESS.lower()])
        logger.info(f"BNB price from GeckoTerminal: ${price:.2f}")
        time.sleep(2)  # Respect rate limit (30 calls/min = 2s/call)
        return price
    except Exception as e:
        logger.error(f"GeckoTerminal BNB price fetch failed: {e}, status={getattr(e.response, 'status_code', 'N/A')}")
        # Fallback to CoinMarketCap
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'BNB', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            price = float(response.json()['data']['BNB']['quote']['USD']['price'])
            logger.info(f"BNB price from CoinMarketCap: ${price:.2f}")
            return price
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap BNB price fetch failed: {cmc_e}")
            return 600  # Hardcoded fallback

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_pets_price_from_pancakeswap():
    try:
        # Try GeckoTerminal first
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/bsc/token_price/{CONTRACT_ADDRESS}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        price = float(response.json()['data']['attributes']['token_prices'][CONTRACT_ADDRESS.lower()])
        logger.info(f"$PETS price from GeckoTerminal: ${price:.10f}")
        time.sleep(2)  # Respect rate limit
        if price <= 0:
            raise ValueError("GeckoTerminal returned invalid price")
        return price
    except Exception as e:
        logger.error(f"GeckoTerminal $PETS price fetch failed: {e}, status={getattr(e.response, 'status_code', 'N/A')}")
        # Fallback to CoinMarketCap
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'PETS', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            price = float(response.json()['data']['PETS']['quote']['USD']['price'])
            logger.info(f"$PETS price from CoinMarketCap: ${price:.10f}")
            return price
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap $PETS price fetch failed: {cmc_e}")
            # Fallback to PancakeSwap reserves
            try:
                pair_address = Web3.to_checksum_address(TARGET_ADDRESS)
                pair_contract = w3.eth.contract(address=pair_address, abi=PANCAKESWAP_PAIR_ABI)
                reserves = pair_contract.functions.getReserves().call()
                reserve0, reserve1, _ = reserves
                bnb_per_pets = reserve1 / reserve0 / 1e18 if reserve0 > 0 else 0
                bnb_to_usd = get_bnb_to_usd()
                price = bnb_per_pets * bnb_to_usd
                if price <= 0:
                    raise ValueError("PancakeSwap returned invalid price")
                logger.info(f"$PETS price from PancakeSwap: ${price:.10f}")
                return price
            except Exception as pcs_e:
                logger.error(f"PancakeSwap $PETS price fetch failed: {pcs_e}")
                return 0.00003886  # Hardcoded fallback

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_token_supply():
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            supply = int(data['result']) / 1e18
            logger.info(f"Token supply: {supply:,.0f} tokens")
            return supply
        logger.error(f"BscScan API error: {data['message']}")
        return 6_604_885_020
    except Exception as e:
        logger.error(f"Failed to fetch token supply: {e}")
        return 6_604_885_020

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def extract_market_cap():
    try:
        price = get_pets_price_from_pancakeswap()
        token_supply = get_token_supply()
        market_cap = int(token_supply * price)
        logger.info(f"Market cap: ${market_cap:,}")
        return market_cap
    except Exception as e:
        logger.error(f"Failed to calculate market cap: {e}")
        return 256600

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_transaction_details(transaction_hash):
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data.get('result'):
            value_wei = int(data['result'].get('value', '0'), 16)
            bnb_value = float(w3.from_wei(value_wei, 'ether'))
            logger.info(f"Transaction {transaction_hash}: BNB value={bnb_value:.6f}")
            return bnb_value
        return None
    except Exception as e:
        logger.error(f"Failed to fetch transaction details for {transaction_hash}: {e}")
        return None

def check_execute_function(transaction_hash):
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=transaction&action=gettxreceiptstatus&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        status = data.get('result', {}).get('status', '')
        bnb_value = get_transaction_details(transaction_hash)
        tx_response = requests.get(
            f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        tx_response.raise_for_status()
        input_data = tx_response.json().get('result', {}).get('input', '')
        is_execute = 'execute' in input_data.lower()
        logger.info(f"Transaction {transaction_hash}: Execute={is_execute}, BNB={bnb_value}")
        return is_execute, bnb_value
    except Exception as e:
        logger.error(f"Failed to check transaction {transaction_hash}: {e}")
        return False, get_transaction_details(transaction_hash)

def get_balance_before_transaction(wallet_address, block_number):
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=account&action=tokenbalancehistory&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&address={wallet_address}&blockno={block_number}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data['status'] == '1':
            balance = Decimal(data['result']) / Decimal(1e18)
            logger.info(f"Balance for {shorten_address(wallet_address)} at block {block_number}: {balance:,.0f} tokens")
            return balance
        return None
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")
        return None

def calculate_percent_increase(last_balance, current_balance):
    if last_balance is None or last_balance == 0:
        return None
    return ((current_balance - last_balance) / last_balance) * 100

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
async def fetch_geckoterminal_transactions():
    global transaction_cache, last_transaction_fetch
    if (datetime.now().timestamp() * 1000 - last_transaction_fetch < TRANSACTION_CACHE_THRESHOLD and transaction_cache):
        logger.info(f"Returning {len(transaction_cache)} cached transactions")
        return transaction_cache
    try:
        # Fetch recent trades for the $PETS pool on BSC
        pool_address = TARGET_ADDRESS.lower()  # Use the same TARGET_ADDRESS (PancakeSwap pair)
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/bsc/pools/{pool_address}/trades?page=1",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"GeckoTerminal raw trades response: {data}")
        
        # Check if the response contains trade data
        if not isinstance(data, dict) or 'data' not in data:
            logger.error(f"Invalid GeckoTerminal response: {data}")
            raise ValueError(f"Invalid GeckoTerminal response: {data}")
        
        trades = data.get('data', [])
        if not trades:
            logger.info("No trades returned from GeckoTerminal")
            return []

        # Filter for buy transactions (where the user receives $PETS)
        transaction_cache = []
        for trade in trades:
            attributes = trade.get('attributes', {})
            # Determine if it's a buy (user receives $PETS, pays with BNB or another token)
            is_buy = attributes.get('kind') == 'buy'
            if not is_buy:
                continue
            tx_hash = attributes.get('tx_hash')
            token_amount = float(attributes.get('token_1_amount', '0'))  # $PETS amount
            bnb_amount = float(attributes.get('token_0_amount', '0'))  # BNB amount (assuming token_0 is BNB)
            wallet_address = attributes.get('tx_to_address')  # Buyer address
            block_number = int(attributes.get('block_number', 0))
            
            # Only include trades with valid data
            if token_amount <= 0 or not tx_hash or not wallet_address:
                logger.info(f"Skipping invalid trade: {trade}")
                continue
                
            transaction_cache.append({
                'transactionHash': tx_hash,
                'to': wallet_address,  # Buyer address
                'from': attributes.get('tx_from_address'),  # Seller (likely the pool)
                'value': str(int(token_amount * 1e18)),  # Convert to wei-like format for consistency
                'blockNumber': block_number,
                'bnb_value': bnb_amount  # Store BNB amount directly from trade
            })
        
        last_transaction_fetch = datetime.now().timestamp() * 1000
        logger.info(f"Fetched {len(transaction_cache)} buy transactions from GeckoTerminal")
        time.sleep(2)  # Respect GeckoTerminal rate limit (30 calls/min = 2s/call)
        return transaction_cache
    except Exception as e:
        logger.error(f"Failed to fetch GeckoTerminal transactions: {e}")
        return transaction_cache or []

async def send_video_with_retry(context, chat_id, video_url, options, max_retries=5, delay=2):
    for i in range(max_retries):
        try:
            logger.info(f"Attempt {i+1}/{max_retries} to send video to chat {chat_id}")
            await context.bot.send_video(chat_id=chat_id, video=video_url, **options)
            return
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(
                    chat_id,
                    f"{options['caption']}\n\n⚠️ Video unavailable.",
                    parse_mode='Markdown'
                )

async def process_transaction(context, transaction, bnb_to_usd_rate, pets_price, chat_id=TELEGRAM_CHAT_ID):
    global posted_transactions
    tx_hash = transaction['transactionHash']
    if tx_hash in posted_transactions:
        logger.info(f"Transaction {tx_hash} already posted, skipping")
        return False
    
    # Use the BNB value directly from the trade data
    bnb_value = transaction.get('bnb_value', 0)
    logger.info(f"Processing {tx_hash}: bnb_value={bnb_value}")
    if bnb_value <= 0:
        logger.info(f"Skipping {tx_hash}: Invalid BNB value ({bnb_value})")
        return False
    
    pets_amount = float(transaction['value']) / 1e18
    usd_value = pets_amount * pets_price
    logger.info(f"Transaction {tx_hash}: PETS amount={pets_amount:,.0f}, USD value=${usd_value:.2f}")
    if usd_value < 1:
        logger.info(f"Skipping {tx_hash}: USD value (${usd_value:.2f}) below threshold")
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
    tx_url = f"https://bscscan.com/tx/{tx_hash}"
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
    await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
    posted_transactions.add(tx_hash)
    log_posted_transaction(tx_hash)
    logger.info(f"Successfully processed transaction {tx_hash}")
    return True

async def monitor_transactions(context):
    global last_transaction_hash, is_tracking_enabled
    while is_tracking_enabled:
        async with asyncio.Lock():
            if not is_tracking_enabled:
                logger.info("Tracking disabled")
                break
            try:
                posted_transactions.update(load_posted_transactions())
                txs = await fetch_geckoterminal_transactions()
                if not txs:
                    logger.info("No new transactions")
                    continue
                bnb_to_usd_rate = get_bnb_to_usd()
                pets_price = get_pets_price_from_pancakeswap()
                new_last_hash = last_transaction_hash
                for tx in reversed(txs):
                    if tx['transactionHash'] in posted_transactions:
                        continue
                    if last_transaction_hash and tx['transactionHash'] == last_transaction_hash:
                        break
                    if await process_transaction(context, tx, bnb_to_usd_rate, pets_price):
                        new_last_hash = tx['transactionHash']
                last_transaction_hash = new_last_hash
            except Exception as e:
                logger.error(f"Error monitoring transactions: {e}")
                recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
                if len(recent_errors) > 5:
                    recent_errors.pop(0)
            await asyncio.sleep(int(os.getenv('POLL_INTERVAL', 60)))

async def polling_fallback():
    logger.info("Starting polling fallback")
    while True:
        try:
            await bot_app.run_polling(poll_interval=3, timeout=10)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(10)

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
async def set_webhook_with_retry(bot_app):
    webhook_url = f"https://{APP_URL}/webhook"
    logger.info(f"Attempting to set webhook: {webhook_url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{APP_URL}/webhook") as response:
                if response.status != 200:
                    logger.error(f"Webhook URL not accessible: {response.status}")
                    raise Exception("Webhook URL inaccessible")
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        raise

def is_admin(update):
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

# Command handlers
async def start(update: Update, context):
    chat_id = update.effective_chat.id
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id, "👋 Welcome to PETS Tracker! Use /track to start buy alerts.")

async def track(update: Update, context):
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    global is_tracking_enabled
    is_tracking_enabled = True
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id, "🚀 Tracking started")
    asyncio.create_task(monitor_transactions(context))

async def stop(update: Update, context):
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    global is_tracking_enabled
    is_tracking_enabled = False
    active_chats.discard(str(chat_id))
    await context.bot.send_message(chat_id, "🛑 Tracking stopped")

async def stats(update: Update, context):
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Fetching $PETS data")
    try:
        txs = await fetch_geckoterminal_transactions()
        if not txs:
            await context.bot.send_message(chat_id, "🚫 No recent buys")
            return
        bnb_to_usd_rate = get_bnb_to_usd()
        pets_price = get_pets_price_from_pancakeswap()
        processed = []
        seen_hashes = set()
        for tx in reversed(txs[:5]):
            if tx['transactionHash'] in seen_hashes:
                continue
            if await process_transaction(context, tx, bnb_to_usd_rate, pets_price, chat_id=chat_id):
                processed.append(tx['transactionHash'])
            seen_hashes.add(tx['transactionHash'])
        await context.bot.send_message(
            chat_id,
            f"✅ Processed {len(processed)} buys:\n" + "\n".join(processed) if processed else "🚫 No transactions found"
        )
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        await context.bot.send_message(chat_id, "🚫 Failed to fetch data")

async def help_command(update: Update, context):
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id,
        "🆘 *Commands:*\n\n"
        "/start - Start bot\n"
        "/track - Enable alerts\n"
        "/stop - Disable alerts\n"
        "/stats - View buys\n"
        "/status - Track status\n"
        "/test - Test transaction\n"
        "/noV - Test without video\n"
        "/debug - Debug info\n"
        "/help - This message",
        parse_mode='Markdown'
    )

async def status(update: Update, context):
    chat_id = update.effective_chat.id
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
            'lastTransactionFetch': datetime.fromtimestamp(last_transaction_fetch / 1000).isoformat() if last_transaction_fetch else None
        }
    }
    await context.bot.send_message(
        chat_id,
        f"🔍 Debug:\n```json\n{json.dumps(status, indent=2)}\n```",
        parse_mode='Markdown'
    )

async def test(update: Update, context):
    chat_id = update.effective_chat.id
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
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        await context.bot.send_message(chat_id, "🚖 Success")
    except Exception as e:
        logger.error(f"Test error: {e}")
        await context.bot.send_message(chat_id, "🚫 Failed")

async def no_video(update: Update, context):
    chat_id = update.effective_chat.id
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
        await context.bot.send_message(chat_id, "🚫 Failed")

# FastAPI routes
@app.get("/")
async def health_check():
    return {"status": "Bot is running"}

@app.get("/webhook")
async def webhook_get():
    logger.info("Received GET request to /webhook")
    return {"status": "This endpoint only accepts POST requests for Telegram webhooks."}

@app.get("/api/transactions")
async def get_transactions():
    logger.info("GET /transactions")
    return transaction_cache

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received webhook POST")
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
    logger.info("Starting bot")
    await bot_app.initialize()
    try:
        await set_webhook_with_retry(bot_app)
        asyncio.create_task(monitor_transactions(bot_app))
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}, switching to polling")
        asyncio.create_task(polling_fallback())
    logger.info("Bot startup complete")

@app.on_event("shutdown")
async def shutdown_event():
    await bot_app.bot.delete_webhook()
    logger.info("Webhook deleted")
    await bot_app.shutdown()
    logger.info("Bot shutdown")

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
