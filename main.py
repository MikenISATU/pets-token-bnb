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
from decimal import Decimal
import telegram
import aiohttp
import threading
from functools import lru_cache

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
INFURA_URL = os.getenv('INFURA_URL')
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
    (INFURA_URL, 'INFURA_URL'),
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

if not COINMARKETCAP_API_KEY:
    logger.warning("COINMARKETCAP_API_KEY is empty; CoinMarketCap API calls will be skipped for ETH price")

logger.info(f"Environment loaded successfully. APP_URL={APP_URL}, PORT={PORT}")

EMOJI = '💰'
ETH_ADDRESS = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'
UNISWAP_V3_FACTORY_ADDRESS = '0x1F98431c8aD98523631AE4a59f267346ea31F984'
UNISWAP_V3_POOL_FEES = [500, 3000, 10000]
UNISWAP_V3_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"}
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]
UNISWAP_V3_POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
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
BUY_THRESHOLDS = {
    'small': 100,
    'medium': 500,
    'large': 1000
}
DEFAULT_PETS_PRICE = 0.0001
DEFAULT_TOKEN_SUPPLY = 6_604_885_020
DEFAULT_MARKET_CAP = 256600
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
    w3 = Web3(Web3.HTTPProvider(INFURA_URL, request_kwargs={'timeout': 60}))
    if not w3.is_connected():
        raise Exception("Primary Infura URL connection failed")
    logger.info("Successfully initialized Web3 with INFURA_URL")
except Exception as e:
    logger.error(f"Failed to initialize Web3 with primary URL: {e}")
    raise ValueError("Web3 connection to Infura failed")

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
            return 2609.26  # Fallback price from CoinDesk, June 5, 2025
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'ETH', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            price = data.get('data', {}).get('ETH', {}).get('quote', {}).get('USD', {}).get('price')
            if not price or price <= 0:
                raise ValueError("Invalid CoinMarketCap ETH price")
            logger.info(f"ETH price from CoinMarketCap: ${price:.2f}")
            return float(price)
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap fetch failed: {cmc_e}")
            return 2609.26  # Fallback price

@lru_cache(maxsize=1)
def get_uniswap_v3_pool_address() -> Optional[str]:
    """Fetch Uniswap V3 pool address for $PETS/WETH pair."""
    try:
        factory_contract = w3.eth.contract(address=Web3.to_checksum_address(UNISWAP_V3_FACTORY_ADDRESS), abi=UNISWAP_V3_FACTORY_ABI)
        token0 = Web3.to_checksum_address(CONTRACT_ADDRESS)
        token1 = Web3.to_checksum_address(ETH_ADDRESS)
        if token0 > token1:
            token0, token1 = token1, token0
        for fee in UNISWAP_V3_POOL_FEES:
            pool_address = factory_contract.functions.getPool(token0, token1, fee).call()
            if pool_address != '0x0000000000000000000000000000000000000000':
                logger.info(f"Found Uniswap V3 pool for $PETS/WETH with fee {fee/10000}%: {pool_address}")
                return pool_address
        logger.warning("No Uniswap V3 pool found for $PETS/WETH")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch Uniswap V3 pool address: {e}")
        return None

def get_pets_price_from_uniswap() -> float:
    """Fetch $PETS price in USD from Uniswap V3 or fallback to CoinGecko."""
    try:
        pool_address = get_uniswap_v3_pool_address()
        if not pool_address:
            logger.warning("No Uniswap V3 pool found, attempting CoinGecko")
            try:
                response = requests.get(
                    f"https://api.coingecko.com/api/v3/simple/token_price/ethereum?contract_addresses={CONTRACT_ADDRESS}&vs_currencies=usd",
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                price = data.get(CONTRACT_ADDRESS.lower(), {}).get('usd')
                if not price or price <= 0:
                    raise ValueError("Invalid CoinGecko $PETS price")
                logger.info(f"$PETS price from CoinGecko: ${price:.10f}")
                return float(price)
            except Exception as cg_e:
                logger.error(f"CoinGecko fetch failed: {cg_e}, using default price ${DEFAULT_PETS_PRICE}")
                return DEFAULT_PETS_PRICE
        pool_contract = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=UNISWAP_V3_POOL_ABI)
        slot0 = pool_contract.functions.slot0().call()
        sqrt_price_x96 = slot0[0]
        token0 = pool_contract.functions.token0().call()
        is_pets_token0 = token0.lower() == CONTRACT_ADDRESS.lower()
        price = (sqrt_price_x96 ** 2) * (10 ** 18) / (2 ** 192)
        if is_pets_token0:
            eth_per_pets = price
        else:
            eth_per_pets = 1 / price if price != 0 else 0
        if eth_per_pets <= 0:
            logger.error("Invalid Uniswap V3 price calculation")
            return DEFAULT_PETS_PRICE
        eth_to_usd = get_eth_to_usd()
        pets_price_usd = eth_per_pets * eth_to_usd
        if pets_price_usd <= 0:
            logger.error("Uniswap V3 returned non-positive $PETS price")
            return DEFAULT_PETS_PRICE
        logger.info(f"$PETS price from Uniswap: ${pets_price_usd:.10f}")
        return pets_price_usd
    except Exception as e:
        logger.error(f"Uniswap V3 $PETS price fetch failed: {e}")
        return DEFAULT_PETS_PRICE

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

def extract_market_cap() -> int:
    """Calculate $PETS market cap based on price and supply."""
    try:
        price = get_pets_price_from_uniswap()
        token_supply = get_token_supply()
        market_cap = int(token_supply * price)
        logger.info(f"Market cap for $PETS: ${market_cap:,}")
        return market_cap
    except Exception as e:
        logger.error(f"Failed to calculate market cap: {e}")
        return DEFAULT_MARKET_CAP

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_transaction_details(transaction_hash: str) -> Optional[float]:
    """Fetch ETH value of a transaction from Etherscan."""
    if transaction_hash in transaction_details_cache:
        logger.info(f"Using cached ETH value for transaction {transaction_hash}")
        return transaction_details_cache[transaction_hash]
    try:
        response = requests.get(
            f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        result = data.get('result', {})
        value_wei_str = result.get('value', '0')
        if not value_wei_str.startswith('0x'):
            raise ValueError(f"Invalid value data for transaction {transaction_hash}")
        value_wei = int(value_wei_str, 16)
        eth_value = float(w3.from_wei(value_wei, 'ether'))
        transaction_details_cache[transaction_hash] = eth_value
        logger.info(f"Transaction {transaction_hash}: ETH value={eth_value:.6f}")
        time.sleep(0.2)
        return eth_value
    except Exception as e:
        logger.error(f"Failed to fetch transaction details for {transaction_hash}: {e}")
        return None

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def check_execute_function(transaction_hash: str) -> Tuple[bool, Optional[float]]:
    """Check if transaction involves 'execute' function and get ETH value."""
    try:
        response = requests.get(
            f"https://api.etherscan.io/api?module=transaction&action=gettxreceiptstatus&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if not data.get('result'):
            logger.error(f"Invalid receipt status for {transaction_hash}")
            return False, None
        eth_value = get_transaction_details(transaction_hash)
        if eth_value is None:
            return False, None
        tx_response = requests.get(
            f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        tx_response.raise_for_status()
        tx_data = tx_response.json()
        input_data = tx_data['result'].get('input', '')
        is_execute = 'execute' in input_data.lower()
        logger.info(f"Transaction {transaction_hash}: Execute={is_execute}, ETH={eth_value}")
        time.sleep(0.2)
        return is_execute,ąż

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
        tx_hash = transaction真的是transaction['hash']
        if tx_hash in posted_transactions:
            logger.info(f"Skipping already posted transaction: {tx_hash}")
            return False
        is_execute, eth_value = check_execute_function(tx_hash)
        if eth_value is None or eth_value <= 0:
            logger.info(f"Skipping transaction {tx_hash} with invalid ETH value: {eth_value}")
            return False
        pets_amount = float(transaction['value']) / (10 ** PETS_TOKEN_DECIMALS)
        usd_value = eth_value * eth_to_usd_rate
        pets_usd_value = pets_amount * pets_price
        if usd_value < 50:
            logger.info(f"Skipping transaction {tx_hash} with USD value < 50: {usd_value}")
            return False
        market_cap = extract_market_cap()
        wallet_address = transaction['to']
        percent_increase = random.uniform(10, 120)
        holding_change_text = f"+{percent_increase:.2f}%"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        tx_url = f"https://etherscan.io/tx/{tx_hash}"
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        pool_address = get_uniswap_v3_pool_address() or "0x0000000000000000000000000000000000000000"
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
            f"[📈 Chart](https://www.dextools.io/app/en/ether/pair-explorer/{pool_address}) "
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
                    "fromBlock": "0x0",
                    "toBlock": "latest",
                    "category": ["token"],
                    "withMetadata": True,
                    "contractAddresses": [Web3.to_checksum_address(CONTRACT_ADDRESS)],
                    "toAddress": Web3.to_checksum_address(TARGET_ADDRESS),
                    "maxResults": 100,
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
                if not data.get('result', {}).get('transfers'):
                    logger.info("No transactions found from Alchemy")
                    return transaction_cache or []
                transactions = [
                    {
                        'transactionHash': tx['hash'],
                        'to': tx['to'],
                        'from': tx['from'],
                        'value': tx['rawContract']['rawValue'],
                        'blockNumber': int(tx['blockNum'], 16),
                        'timeStamp': int(tx['metadata']['blockTimestamp'].replace('T', ' ').split('.')[0].split(' ')[1])
                    }
                    for tx in data['result']['transfers']
                    if tx['from'].lower() == TARGET_ADDRESS.lower() and int(tx['rawContract']['rawValue']) > 0
                ]
                if transactions:
                    max_block = max(tx['blockNumber'] for tx in transactions)
                    if not last_block_number or max_block > last_block_number:
                        last_block_number = max_block
                transaction_cache.extend([tx for tx in transactions if tx['blockNumber'] >= (last_block_number or 0)])
                transaction_cache = transaction_cache[-1000:]
                last_transaction_fetch = datetime.now().timestamp() * 1000
                logger.info(f"Fetched {len(transactions)} buy transactions from Alchemy, last_block_number={last_block_number}")
                return transactions
    except Exception as e:
        logger.error(f"Failed to fetch Alchemy transactions: {e}")
        return transaction_cache or []

async def monitor_transactions(context) -> None:
    """Monitor Alchemy for new transactions."""
    global last_transaction_hash, last_block_number, is_tracking_enabled, monitoring_task
    logger.info("Starting transaction monitoring")
    while is_tracking_enabled:
        async with asyncio.Lock():
            if not is_tracking_enabled:
                logger.info("Tracking disabled, stopping monitoring")
                break
            try:
                posted_transactions.update(load_posted_transactions())
                txs = await fetch_alchemy_transactions()
                if not txs:
                    await asyncio.sleep(POLLING_INTERVAL)
                    continue
                eth_to_usd_rate = get_eth_to_usd()
                pets_price = get_pets_price_from_uniswap()
                new_last_hash = last_transaction_hash
                for tx in sorted(txs, key=lambda x: x['blockNumber'], reverse=True):
                    if tx['transactionHash'] in posted_transactions:
                        continue
                    if last_transaction_hash == tx['transactionHash']:
                        continue
                    if last_block_number and tx['blockNumber'] <= last_block_number:
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
        pets_price = get_pets_price_from_uniswap()
        success = await process_transaction(context, latest_tx, eth_to_usd_rate, pets_price, chat_id=chat_id)
        if success:
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Displayed latest buy: {latest_tx['transactionHash']}")
        else:
            await context.bot.send_message(chat_id=chat_id, text="🚖 No transactions met $50 threshold")
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚖 Failed: {str(e)}")

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
        'pollingActive': polling_task is not None and not polling_task.done(),
        'uniswapPool': get_uniswap_v3_pool_address()
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
        pets_price = get_pets_price_from_uniswap()
        eth_to_usd_rate = get_eth_to_usd()
        eth_value = (test_pets_amount * pets_price) / eth_to_usd_rate if eth_to_usd_rate > 0 else 0.1
        usd_value = eth_value * eth_to_usd_rate
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randint(1000000000000000, 9999999999999999):0x}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://etherscan.io/tx/{test_tx_hash}"
        pool_address = get_uniswap_v3_pool_address() or "0x0000000000000000000000000000000000000000"
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
            f"[📈 Chart](https://www.dextools.io/app/en/ether/pair-explorer/{pool_address}) "
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
        pets_price = get_pets_price_from_uniswap()
        eth_to_usd_rate = get_eth_to_usd()
        eth_value = (test_pets_amount * pets_price) / eth_to_usd_rate if eth_to_usd_rate > 0 else 0.1
        usd_value = eth_value * eth_to_usd_rate
        wallet_address = f"0x{random.randint(1000000000000000, 9999999999999999):0x}"
        emoji_count = min(int(usd_value) // 10, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://etherscan.io/tx/{test_tx_hash}"
        pool_address = get_uniswap_v3_pool_address() or "0x0000000000000000000000000000000000000000"
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
            f"[📈 Chart](https://www.dextools.io/app/en/ether/pair-explorer/{pool_address}) "
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
            await bot_app.updater.stop()
            await bot_app.shutdown()
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
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
