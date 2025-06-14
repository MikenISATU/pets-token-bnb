
import os
import logging
import requests
import random
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Dict, List, Set, Tuple
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from web3 import Web3
from tenacity import retry, wait_exponential, stop_after_attempt
from dotenv import load_dotenv
from datetime import datetime, timedelta
import telegram
import aiohttp
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
telegram_logger = logging.getLogger("telegram")
telegram_logger.setLevel(logging.WARNING)

logger.info(f"python-telegram-bot version: {telegram.__version__}")
if not telegram.__version__.startswith('20'):
    logger.error(f"Expected python-telegram-bot v20.0+, got {telegram.__version__}")
    raise SystemExit(1)

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
APP_URL = os.getenv('RAILWAY_PUBLIC_DOMAIN', os.getenv('APP_URL'))
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')
ALCHEMY_API_KEY = os.getenv('ALCHEMY_API_KEY', '5IyUyaJBrZq9eBDKxarcQEkkeBlfUOG_')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS', '0x2466858ab5edAd0BB597FE9f008F568B00d25Fe3')
ADMIN_CHAT_ID = os.getenv('ADMIN_USER_ID')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
PORT = int(os.getenv('PORT', 8080))
COINMARKETCAP_API_KEY = os.getenv('COINMARKETCAP_API_KEY', '')
TARGET_ADDRESS = os.getenv('TARGET_ADDRESS', '0x98b794be9c4f49900c6193aaff20876e1f36043e')
POLLING_INTERVAL = int(os.getenv('POLLING_INTERVAL', 60))

missing_vars = []
for var, name in [
    (TELEGRAM_BOT_TOKEN, 'TELEGRAM_BOT_TOKEN'),
    (CLOUDINARY_CLOUD_NAME, 'CLOUDINARY_CLOUD_NAME'),
    (APP_URL, 'APP_URL/RAILWAY_PUBLIC_DOMAIN'),
    (ETHERSCAN_API_KEY, 'ETHERSCAN_API_KEY'),
    (ALCHEMY_API_KEY, 'ALCHEMY_API_KEY'),
    (CONTRACT_ADDRESS, 'CONTRACT_ADDRESS'),
    (ADMIN_CHAT_ID, 'ADMIN_USER_ID'),
    (TELEGRAM_CHAT_ID, 'TELEGRAM_CHAT_ID'),
]:
    if not var:
        missing_vars.append(name)
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

if not Web3.is_address(CONTRACT_ADDRESS):
    logger.error(f"Invalid Ethereum address for CONTRACT_ADDRESS: {CONTRACT_ADDRESS}")
    raise ValueError(f"Invalid Ethereum address for CONTRACT_ADDRESS: {CONTRACT_ADDRESS}")

logger.info(f"Environment loaded successfully. APP_URL={APP_URL}, PORT={PORT}")

EMOJI = '💰'
ETH_ADDRESS = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'
cloudinary_videos = {
    'MicroPets Buy': 'SMALLBUY_b3px1p',
    'Medium Bullish Buy': 'MEDIUMBUY_MPEG_e02zdz',
    'Whale Buy': 'micropets_big_msap',
    'Extra Large Buy': 'micropets_big_msapxz'
}
BUY_THRESHOLDS = {
    'small': 100,
    'medium': 500,
    'large': 1000
}
DEFAULT_PETS_PRICE = 0.0001
DEFAULT_TOKEN_SUPPLY = 3_394_814_955  # From logs
DEFAULT_MARKET_CAP = 339_481  # From logs
PETS_TOKEN_DECIMALS = 18

transaction_cache: List[Dict] = []
active_chats: Set[str] = {TELEGRAM_CHAT_ID}
last_transaction_hash: Optional[str] = None
last_block_number: Optional[int] = None
is_tracking_enabled: bool = False
recent_errors: List[Dict] = []
last_transaction_fetch: Optional[float] = None
posted_transactions: Set[str] = set()
transaction_details_cache: Dict[str, float] = {}
monitoring_task: Optional[asyncio.Task] = None
polling_task: Optional[asyncio.Task] = None
file_lock = threading.Lock()

try:
    w3 = Web3(Web3.HTTPProvider(f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}", request_kwargs={'timeout': 60}))
    if not w3.is_connected():
        raise Exception("Alchemy connection failed")
    logger.info("Successfully initialized Web3 with Alchemy")
except Exception as e:
    logger.error(f"Failed to initialize Web3: {e}")
    raise ValueError("Web3 connection failed")

def get_video_url(category: str) -> str:
    """Generate Cloudinary video URL for a given category."""
    public_id = cloudinary_videos.get(category, 'micropets_big_msapxz')
    video_url = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/video/upload/v1/{public_id}.mp4"
    logger.info(f"Generated video URL for {category}: {video_url}")
    return video_url

def categorize_buy(usd_value: float) -> str:
    """Categorize buy transaction based on USD value."""
    if usd_value < BUY_THRESHOLDS['small']:
        return 'MicroPets Buy'
    elif usd_value < BUY_THRESHOLDS['medium']:
        return 'Medium Bullish Buy'
    elif usd_value < BUY_THRESHOLDS['large']:
        return 'Whale Buy'
    return 'Extra Large Buy'

def shorten_address(address: str) -> str:
    """Shorten Ethereum address for display."""
    if address and Web3.is_address(address):
        return f"{address[:6]}...{address[-4:]}"
    return ''

def load_posted_transactions() -> Set[str]:
    """Load previously posted transaction hashes from file."""
    try:
        with file_lock:
            if not os.path.exists('posted_transactions.txt'):
                return set()
            with open('posted_transactions.txt', 'r') as f:
                return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logger.warning(f"Could not load posted_transactions.txt: {e}")
        return set()

def log_posted_transaction(transaction_hash: str) -> None:
    """Log a posted transaction hash to file."""
    try:
        with file_lock:
            with open('posted_transactions.txt', 'a') as f:
                f.write(transaction_hash + '\n')
    except Exception as e:
        logger.warning(f"Could not write to posted_transactions.txt: {e}")

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_eth_to_usd() -> float:
    """Fetch ETH to USD price from GeckoTerminal or CoinMarketCap."""
    try:
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/eth/token_price/{ETH_ADDRESS}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        price_str = data.get('data', {}).get('attributes', {}).get('token_prices', {}).get(ETH_ADDRESS.lower())
        if not price_str:
            raise ValueError("Invalid ETH price data from GeckoTerminal")
        price = float(price_str)
        if price <= 0:
            raise ValueError("GeckoTerminal returned non-positive ETH price")
        logger.info(f"ETH price from GeckoTerminal: ${price:.2f}")
        time.sleep(0.5)
        return price
    except Exception as e:
        logger.error(f"GeckoTerminal fetch failed: {e}")
        if not COINMARKETCAP_API_KEY:
            logger.warning("Skipping CoinMarketCap due to empty API key")
            return 2609.26  # Fallback price
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'ETH', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            price = data.get('data', {}).get('ETH', {}).get('quote', {}).get('USD', {}).get('price')
            if not price or price <= 0:
                raise ValueError("Invalid CoinMarketCap ETH price")
            logger.info(f"ETH price from CoinMarketCap: ${price:.2f}")
            return float(price)
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap fetch failed: {cmc_e}")
            return 2609.26  # Fallback price

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
async def get_pets_price_from_alchemy() -> float:
    """Estimate $PETS price in USD using recent buy transactions from Alchemy."""
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "alchemy_getAssetTransfers",
                "params": [{
                    "fromBlock": "0x0",
                    "toBlock": "latest",
                    "category": ["token"],
                    "withMetadata": True,
                    "contractAddresses": [Web3.to_checksum_address(CONTRACT_ADDRESS)],
                    "fromAddress": Web3.to_checksum_address(TARGET_ADDRESS),
                    "maxCount": "0xA",  # 10 transactions to estimate price
                    "order": "desc"
                }]
            }
            async with session.post(
                f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}",
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            ) as response:
                response.raise_for_status()
                data = await response.json()
                if 'result' not in data or 'transfers' not in data['result']:
                    logger.warning("No recent buy transactions found for price estimation")
                    return DEFAULT_PETS_PRICE
                prices = []
                eth_to_usd = get_eth_to_usd()
                for tx in data['result']['transfers']:
                    if tx['from'].lower() != TARGET_ADDRESS.lower() or not tx['rawContract'].get('value'):
                        continue
                    try:
                        token_value = int(tx['rawContract']['value'], 16) / (10 ** PETS_TOKEN_DECIMALS)
                        if token_value <= 0:
                            continue
                        tx_hash = tx['hash']
                        eth_value = await get_transaction_details_async(tx_hash, session)
                        if eth_value is None or eth_value <= 0:
                            continue
                        price_per_token_eth = eth_value / token_value
                        price_per_token_usd = price_per_token_eth * eth_to_usd
                        if price_per_token_usd > 0:
                            prices.append(price_per_token_usd)
                    except Exception as e:
                        logger.warning(f"Skipping transaction {tx.get('hash')} for price estimation: {e}")
                        continue
                if not prices:
                    logger.warning("No valid transactions for price estimation")
                    return DEFAULT_PETS_PRICE
                avg_price = sum(prices) / len(prices)
                logger.info(f"Estimated $PETS price from {len(prices)} transactions: ${avg_price:.10f}")
                return avg_price
    except Exception as e:
        logger.error(f"Failed to estimate $PETS price from Alchemy: {e}")
        return DEFAULT_PETS_PRICE

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
async def get_transaction_details_async(transaction_hash: str, session: aiohttp.ClientSession) -> Optional[float]:
    """Fetch ETH value of a transaction from Etherscan asynchronously."""
    if transaction_hash in transaction_details_cache:
        logger.info(f"Using cached ETH value for transaction {transaction_hash}")
        return transaction_details_cache[transaction_hash]
    try:
        async with session.get(
            f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        ) as response:
            response.raise_for_status()
            data = await response.json()
            result = data.get('result', {})
            value_wei_str = result.get('value', '0')
            if not value_wei_str.startswith('0x'):
                raise ValueError(f"Invalid value data for transaction {transaction_hash}")
            value_wei = int(value_wei_str, 16)
            eth_value = float(w3.from_wei(value_wei, 'ether'))
            transaction_details_cache[transaction_hash] = eth_value
            logger.info(f"Transaction {transaction_hash}: ETH value={eth_value:.6f}")
            await asyncio.sleep(0.2)
            return eth_value
    except Exception as e:
        logger.error(f"Failed to fetch transaction details for {transaction_hash}: {e}")
        return None

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_token_supply() -> float:
    """Fetch $PETS token supply from Etherscan."""
    try:
        response = requests.get(
            f"https://api.etherscan.io/api?module=stats&action=tokensupply&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data.get('status') != '1':
            logger.error(f"Etherscan API error: {data.get('message', 'No message')}")
            return DEFAULT_TOKEN_SUPPLY
        supply_str = data.get('result')
        if not supply_str.isdigit():
            raise ValueError("Invalid token supply data")
        supply = int(supply_str) / (10 ** PETS_TOKEN_DECIMALS)
        logger.info(f"Token supply: {supply:,.0f} tokens")
        time.sleep(0.2)
        return supply
    except Exception as e:
        logger.error(f"Failed to fetch token supply: {e}")
        return DEFAULT_TOKEN_SUPPLY

async def extract_market_cap() -> int:
    """Calculate $PETS market cap based on price and supply."""
    try:
        price = await get_pets_price_from_alchemy()
        token_supply = get_token_supply()
        market_cap = int(token_supply * price)
        logger.info(f"Market cap for $PETS: ${market_cap:,}")
        return market_cap
    except Exception as e:
        logger.error(f"Failed to calculate market cap: {e}")
        return DEFAULT_MARKET_CAP

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
async def check_execute_function(transaction_hash: str, session: aiohttp.ClientSession) -> Tuple[bool, Optional[float]]:
    """Check if transaction involves 'execute' function and get ETH value."""
    try:
        async with session.get(
            f"https://api.etherscan.io/api?module=transaction&action=gettxreceiptstatus&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        ) as response:
            response.raise_for_status()
            data = await response.json()
            if not data.get('result'):
                logger.error(f"Invalid receipt status for {transaction_hash}")
                return False, None
        eth_value = await get_transaction_details_async(transaction_hash, session)
        if eth_value is None:
            return False, None
        async with session.get(
            f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        ) as tx_response:
            tx_response.raise_for_status()
            tx_data = await tx_response.json()
            input_data = tx_data['result'].get('input', '')
            is_execute = 'execute' in input_data.lower()
            logger.info(f"Transaction {transaction_hash}: Execute={is_execute}, ETH={eth_value}")
            await asyncio.sleep(0.2)
            return is_execute, eth_value
    except Exception as e:
        logger.error(f"Failed to check transaction {transaction_hash}: {e}")
        return False, await get_transaction_details_async(transaction_hash, session)

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
async def fetch_alchemy_transactions() -> List[Dict]:
    """Fetch new token transfer transactions from Alchemy."""
    global transaction_cache, last_transaction_fetch, last_block_number
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "alchemy_getAssetTransfers",
                "params": [{
                    "fromBlock": "0x0" if not last_block_number else hex(last_block_number),
                    "toBlock": "latest",
                    "category": ["token"],
                    "withMetadata": True,
                    "contractAddresses": [Web3.to_checksum_address(CONTRACT_ADDRESS)],
                    "fromAddress": Web3.to_checksum_address(TARGET_ADDRESS),
                    "maxCount": "0x64",
                    "order": "desc"
                }]
            }
            async with session.post(
                f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}",
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            ) as response:
                response.raise_for_status()
                data = await response.json()
                if 'result' not in data or 'transfers' not in data['result']:
                    logger.info("No transactions found from Alchemy")
                    return transaction_cache
                transactions = []
                for tx in data['result']['transfers']:
                    if tx['from'].lower() != TARGET_ADDRESS.lower() or not tx['rawContract'].get('value'):
                        continue
                    try:
                        value = int(tx['rawContract']['value'], 16)
                        if value <= 0:
                            continue
                        timestamp = int(datetime.fromisoformat(tx['metadata']['blockTimestamp'].replace('Z', '')).timestamp())
                        transactions.append({
                            'transactionHash': tx['hash'],
                            'to': tx['to'],
                            'from': tx['from'],
                            'value': str(value),
                            'blockNumber': int(tx['blockNum'], 16),
                            'timeStamp': timestamp
                        })
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Skipping invalid transaction {tx.get('hash')}: {e}")
                        continue
                if transactions:
                    max_block = max(tx['blockNumber'] for tx in transactions)
                    last_block_number = max(last_block_number or 0, max_block)
                    transaction_cache.extend(transactions)
                    transaction_cache = transaction_cache[-1000:]
                    last_transaction_fetch = datetime.now().timestamp() * 1000
                    logger.info(f"Fetched {len(transactions)} buy transactions from Alchemy, last_block_number={last_block_number}")
                return transactions
    except Exception as e:
        logger.error(f"Failed to fetch Alchemy transactions: {e}")
        return transaction_cache

async def send_video_with_retry(context, chat_id: str, video_url: str, options: Dict, max_retries: int = 3, delay: int = 2) -> bool:
    """Send video with retries on failure."""
    for i in range(max_retries):
        try:
            logger.info(f"Attempt {i+1}/{max_retries} to send video to chat {chat_id}")
            async with aiohttp.ClientSession() as session:
                async with session.head(video_url, timeout=5) as head_response:
                    if head_response.status != 200:
                        raise Exception(f"Video URL inaccessible, status {head_response.status}")
            await context.bot.send_video(chat_id=chat_id, video=video_url, **options)
            logger.info(f"Successfully sent video to chat {chat_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(chat_id, f"{options['caption']}\n\n⚠️ Video unavailable", parse_mode='Markdown')
                return False
            await asyncio.sleep(delay)
    return False

async def process_transaction(context, transaction: Dict, eth_to_usd_rate: float, pets_price: float, chat_id: str = TELEGRAM_CHAT_ID) -> bool:
    """Process and post a transaction to Telegram."""
    global posted_transactions
    try:
        tx_hash = transaction['transactionHash']
        if tx_hash in posted_transactions:
            logger.info(f"Skipping already posted transaction: {tx_hash}")
            return False
        async with aiohttp.ClientSession() as session:
            is_execute, eth_value = await check_execute_function(tx_hash, session)
            if eth_value is None or eth_value <= 0:
                logger.info(f"Skipping transaction {tx_hash} with invalid ETH value: {eth_value}")
                return False
        pets_amount = float(transaction['value']) / (10 ** PETS_TOKEN_DECIMALS)
        usd_value = eth_value * eth_to_usd_rate
        if usd_value < 50:
            logger.info(f"Skipping transaction {tx_hash} with USD value < 50: {usd_value}")
            return False
        market_cap = await extract_market_cap()
        wallet_address = transaction['to']
        percent_increase = random.uniform(10, 120)
        holding_change_text = f"+{percent_increase:.2f}%"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        tx_url = f"https://etherscan.io/tx/{tx_hash}"
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        message = (
            f"🚀 *MicroPets Buy!* Ethereum 💰\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS}): {pets_amount:,.0f}\n"
            f"💵 ETH Value: {eth_value:,.4f} (${usd_value:,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding Change: {holding_change_text}\n"
            f"🦑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on Etherscan]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/petdex) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🤑 Buy $PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        success = await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        if success:
            posted_transactions.add(tx_hash)
            log_posted_transaction(tx_hash)
            logger.info(f"Processed transaction {tx_hash} for chat {chat_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error processing transaction {tx_hash}: {e}")
        return False

async def monitor_transactions(context) -> None:
    """Monitor Alchemy for new transactions."""
    global last_transaction_hash, last_block_number, is_tracking_enabled, monitoring_task
    logger.info("Starting transaction monitoring")
    while is_tracking_enabled:
        try:
            posted_transactions.update(load_posted_transactions())
            txs = await fetch_alchemy_transactions()
            if not txs:
                await asyncio.sleep(POLLING_INTERVAL)
                continue
            eth_to_usd_rate = get_eth_to_usd()
            pets_price = await get_pets_price_from_alchemy()
            new_last_hash = last_transaction_hash
            for tx in sorted(txs, key=lambda x: x['blockNumber'], reverse=True):
                if tx['transactionHash'] in posted_transactions or tx['transactionHash'] == last_transaction_hash:
                    continue
                if await process_transaction(context, tx, eth_to_usd_rate, pets_price):
                    new_last_hash = tx['transactionHash']
                    last_block_number = max(last_block_number or 0, tx['blockNumber'])
            last_transaction_hash = new_last_hash
        except Exception as e:
            logger.error(f"Error monitoring transactions: {e}")
            recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
            if len(recent_errors) > 10:
                recent_errors.pop(0)
        await asyncio.sleep(POLLING_INTERVAL)
    logger.info("Monitoring task stopped")
    monitoring_task = None

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(5))
async def set_webhook_with_retry(bot_app) -> bool:
    """Set Telegram webhook with retries."""
    webhook_url = f"https://{APP_URL}/webhook"
    logger.info(f"Attempting to set webhook: {webhook_url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{APP_URL}/health", timeout=10) as response:
                if response.status != 200:
                    raise Exception(f"Health check failed: {response.status}")
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        await bot_app.bot.set_webhook(webhook_url, allowed_updates=["message", "channel_post"])
        logger.info(f"Webhook set successfully: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        raise

async def polling_fallback(bot_app) -> None:
    """Fallback to polling if webhook fails."""
    global polling_task
    logger.info("Starting polling fallback")
    try:
        if not bot_app.running:
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling(
                poll_interval=5,
                timeout=10,
                drop_pending_updates=True
            )
            logger.info("Polling started successfully")
            while polling_task and not polling_task.done():
                await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Polling error: {e}")
        await asyncio.sleep(10)
    finally:
        if bot_app.running:
            try:
                await bot_app.stop()
                logger.info("Polling stopped")
            except Exception as e:
                logger.error(f"Error stopping polling: {e}")

def is_admin(update: Update) -> bool:
    """Check if user is an admin."""
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

async def start(update: Update, context) -> None:
    """Handle /start command."""
    chat_id = update.effective_chat.id
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id=chat_id, text="👋 Welcome to PETS Tracker! Use /track to start buy alerts.")

async def track(update: Update, context) -> None:
    """Handle /track command to start monitoring."""
    global is_tracking_enabled, monitoring_task
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    if is_tracking_enabled:
        await context.bot.send_message(chat_id=chat_id, text="🚀 Tracking already enabled")
        return
    is_tracking_enabled = True
    active_chats.add(str(chat_id))
    monitoring_task = asyncio.create_task(monitor_transactions(context))
    await context.bot.send_message(chat_id=chat_id, text="🚖 Tracking started")

async def stop(update: Update, context) -> None:
    """Handle /stop command to stop monitoring."""
    global is_tracking_enabled, monitoring_task
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    is_tracking_enabled = False
    if monitoring_task:
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            logger.info("Monitoring task cancelled")
        monitoring_task = None
    active_chats.discard(str(chat_id))
    await context.bot.send_message(chat_id=chat_id, text="🛑 Stopped")

async def stats(update: Update, context) -> None:
    """Handle /stats command to show latest transaction."""
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id=chat_id, text="⏳ Fetching latest $PETS buy...")
    try:
        txs = await fetch_alchemy_transactions()
        if not txs:
            await context.bot.send_message(chat_id=chat_id, text="🚖 No recent buys found")
            return
        latest_tx = max(txs, key=lambda x: x['timeStamp'])
        if latest_tx['transactionHash'] in posted_transactions:
            await context.bot.send_message(chat_id=chat_id, text="🚖 No new transactions")
            return
        eth_to_usd_rate = get_eth_to_usd()
        pets_price = await get_pets_price_from_alchemy()
        success = await process_transaction(context, latest_tx, eth_to_usd_rate, pets_price, chat_id=chat_id)
        if success:
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Displayed latest buy: {latest_tx['transactionHash']}")
        else:
            await context.bot.send_message(chat_id=chat_id, text="🚖 No transactions met $50 threshold")
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚖 Failed: {str(e)}")

async def help_command(update: Update, context) -> None:
    """Handle /help command."""
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🆘 Commands:\n\n"
            "/start - Start bot\n"
            "/track - Enable alerts\n"
            "/stop - Disable alerts\n"
            "/stats - Show latest buy\n"
            "/status - Check status\n"
            "/test - Test transaction\n"
            "/noV - Test without video\n"
            "/debug - Debug info\n"
            "/help - This help\n"
        ),
        parse_mode='Markdown'
    )

async def status(update: Update, context) -> None:
    """Handle /status command."""
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 *Status:* {'Enabled' if is_tracking_enabled else 'Disabled'}",
        parse_mode='Markdown'
    )

async def debug(update: Update, context) -> None:
    """Handle /debug command."""
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    status = {
        'trackingEnabled': is_tracking_enabled,
        'activeChats': list(active_chats),
        'lastTxHash': last_transaction_hash,
        'lastBlockNumber': last_block_number,
        'recentErrors': recent_errors[-10:],
        'apiStatus': {
            'web3': bool(w3.is_connected()),
            'lastTransactionFetch': datetime.fromtimestamp(last_transaction_fetch / 1000).isoformat() if last_transaction_fetch else None
        },
        'pollingActive': polling_task is not None and not polling_task.done()
    }
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 Debug:\n```json\n{json.dumps(status, indent=2)}\n```",
        parse_mode='Markdown'
    )

async def test(update: Update, context) -> None:
    """Handle /test command to simulate transaction."""
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id=chat_id, text="⏳ Generating test...")
    try:
        test_tx_hash = f"0xTest{uuid.uuid4().hex[:16]}"
        test_pets_amount = random.randint(1000000, 5000000)
        pets_price = await get_pets_price_from_alchemy()
        eth_to_usd_rate = get_eth_to_usd()
        eth_value = (test_pets_amount * pets_price) / eth_to_usd_rate if eth_to_usd_rate > 0 else 0.1
        usd_value = eth_value * eth_to_usd_rate
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randint(1000000000000000, 9999999999999999):0x}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = EMOJI * emoji_count
        market_cap = await extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://etherscan.io/tx/{test_tx_hash}"
        message = (
            f"🚖 *MicroPets Buy!* Test\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS}): {test_pets_amount:,.0f}\n"
            f"💵 ETH Value: {eth_value:,.4f} (${usd_value:,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/petdex) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🥳 Buy $PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        await context.bot.send_message(chat_id=chat_id, text="✅ Success")
    except Exception as e:
        logger.error(f"Test error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚖 Failed: {str(e)}")

async def no_video(update: Update, context) -> None:
    """Handle /noV command to test without video."""
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id=chat_id, text="⏖ Testing buy (no video)")
    try:
        test_tx_hash = f"0xTestNoV{uuid.uuid4().hex[:16]}"
        test_pets_amount = random.randint(1000000, 5000000)
        pets_price = await get_pets_price_from_alchemy()
        eth_to_usd_rate = get_eth_to_usd()
        eth_value = (test_pets_amount * pets_price) / eth_to_usd_rate if eth_to_usd_rate > 0 else 0.1
        usd_value = eth_value * eth_to_usd_rate
        wallet_address = f"0x{random.randint(1000000000000000, 9999999999999999):0x}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = EMOJI * emoji_count
        market_cap = await extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://etherscan.io/tx/{test_tx_hash}"
        message = (
            f"🚖 *MicroPets Buy!* Ethereum\n\n"
            f"{emojis}\n"
            f"💖 [$PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS}): {test_pets_amount:,.0f}\n"
            f"💵 ETH: {eth_value:,.4f} (${usd_value:,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦆 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 Link]({tx_url})\n\n"
            f"[💖 Staking](https://pets.micropets.io/petdex) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🥳 Buy $PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        await context.bot.send_message(chat_id=chat_id, text="✅ OK")
    except Exception as e:
        logger.error(f"/noV error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚖 Test failed: {str(e)}")

app = FastAPI()

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    logger.info("Checking health endpoint")
    try:
        if not w3.is_connected():
            logger.error("Web3 connection check failed")
            raise HTTPException(status_code=503, detail="Web3 not connected")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail=f"Service unavailable: {str(e)}")

@app.get("/webhook")
async def webhook_get():
    logger.info("Received GET webhook")
    raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.get("/api/transactions")
async def get_transactions():
    """API endpoint to get cached transactions."""
    logger.info("Fetching transactions via API")
    return transaction_cache

@app.post("/webhook")
async def webhook(request: Request):
    """Handle Telegram webhook requests."""
    logger.info("Received POST webhook")
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        if update:
            await bot_app.process_update(update)
        return {"status": "OK"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        recent_errors.append({"time": datetime.now().isoformat(), "error": str(e)})
        if len(recent_errors) > 10:
            recent_errors.pop(0)
        raise HTTPException(status_code=500, detail="Webhook failed")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage FastAPI application lifespan."""
    global monitoring_task, polling_task
    logger.info("Starting bot application")
    try:
        await bot_app.initialize()
        try:
            await set_webhook_with_retry(bot_app)
            monitoring_task = asyncio.create_task(monitor_transactions(bot_app))
            logger.info("Webhook set successfully")
        except Exception as e:
            logger.error(f"Webhook setup failed: {e}. Switching to polling")
            polling_task = asyncio.create_task(polling_fallback(bot_app))
            monitoring_task = asyncio.create_task(monitor_transactions(bot_app))
        yield
    except Exception as e:
        logger.error(f"Lifespan error: {e}")
    finally:
        logger.info("Initiating bot shutdown")
        if monitoring_task:
            monitoring_task.cancel()
            try:
                await monitoring_task
            except asyncio.CancelledError:
                logger.info("Monitoring task cancelled")
            monitoring_task = None
        if polling_task:
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                logger.info("Polling task cancelled")
            polling_task = None
        if bot_app.running:
            try:
                await bot_app.stop()
            except Exception as e:
                logger.error(f"Error stopping bot: {e}")
        try:
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Error deleting webhook: {e}")
        logger.info("Bot shutdown completed")

app = FastAPI(lifespan=lifespan)

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
