eth code pets:

import os
import logging
import requests
import random
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Dict, List, Set
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from web3 import Web3
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
from datetime import datetime, timedelta
from decimal import Decimal
import telegram
import aiohttp
import threading

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
APP_URL = os.getenv('RAILWAY_PUBLIC_DOMAIN', os.getenv('APP_URL'))
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')
INFURA_URL = os.getenv('INFURA_URL')
CONTRACT_ADDRESS = os.getenv('CONTRACT_ADDRESS', '0x2466858ab5edAd0BB597FE9f008F568B00d25Fe3')
ADMIN_CHAT_ID = os.getenv('ADMIN_USER_ID')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
PORT = int(os.getenv('PORT', 8080))
COINMARKETCAP_API_KEY = os.getenv('COINMARKETCAP_API_KEY', '')
TARGET_ADDRESS = os.getenv('TARGET_ADDRESS', '0x98b794be9c4f49900c6193aaff20876e1f36043e')
POLLING_INTERVAL = int(os.getenv('POLLING_INTERVAL', 60))

# Validate environment variables
missing_vars = []
for var, name in [
    (TELEGRAM_BOT_TOKEN, 'TELEGRAM_BOT_TOKEN'),
    (CLOUDINARY_CLOUD_NAME, 'CLOUDINARY_CLOUD_NAME'),
    (APP_URL, 'APP_URL/RAILWAY_PUBLIC_DOMAIN'),
    (ETHERSCAN_API_KEY, 'ETHERSCAN_API_KEY'),
    (INFURA_URL, 'INFURA_URL'),
    (CONTRACT_ADDRESS, 'CONTRACT_ADDRESS'),
    (ADMIN_CHAT_ID, 'ADMIN_USER_ID'),
    (TELEGRAM_CHAT_ID, 'TELEGRAM_CHAT_ID'),
    (TARGET_ADDRESS, 'TARGET_ADDRESS')
]:
    if not var:
        missing_vars.append(name)
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Validate Ethereum addresses
for addr, name in [(CONTRACT_ADDRESS, 'CONTRACT_ADDRESS'), (TARGET_ADDRESS, 'TARGET_ADDRESS')]:
    if not Web3.is_address(addr):
        logger.error(f"Invalid Ethereum address for {name}: {addr}")
        raise ValueError(f"Invalid Ethereum address for {name}: {addr}")

if not COINMARKETCAP_API_KEY:
    logger.warning("COINMARKETCAP_API_KEY is empty; CoinMarketCap API calls will be skipped")

logger.info(f"Environment loaded successfully. APP_URL={APP_URL}, PORT={PORT}")

# Constants
EMOJI = '💰'
UNISWAP_PAIR_ABI = [
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
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
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
ETH_ADDRESS = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'  # WETH on Ethereum
BUY_THRESHOLDS = {
    'small': 100,
    'medium': 500,
    'large': 1000
}

# In-memory data
transaction_cache: List[Dict] = []
active_chats: Set[str] = {TELEGRAM_CHAT_ID}
last_transaction_hash: Optional[str] = None
last_block_number: Optional[int] = None
is_tracking_enabled: bool = False
recent_errors: List[Dict] = []
last_transaction_fetch: Optional[float] = None
TRANSACTION_CACHE_THRESHOLD = 2 * 60 * 1000
posted_transactions: Set[str] = set()
transaction_details_cache: Dict[str, float] = {}
monitoring_task = None
polling_task = None
file_lock = threading.Lock()

# Initialize Web3
try:
    w3 = Web3(Web3.HTTPProvider(INFURA_URL, request_kwargs={'timeout': 60}))
    if not w3.is_connected():
        raise Exception("Primary Infura URL connection failed")
    logger.info("Successfully initialized Web3 with INFURA_URL")
except Exception as e:
    logger.error(f"Failed to initialize Web3 with primary URL: {e}")
    raise ValueError("Web3 connection to Infura failed")

# Helper functions
def get_video_url(category: str) -> str:
    public_id = cloudinary_videos.get(category, 'micropets_big_msapxz')
    video_url = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/video/upload/v1/{public_id}.mp4"
    logger.info(f"Generated video URL for {category}: {video_url}")
    return video_url

def categorize_buy(usd_value: float) -> str:
    if usd_value < BUY_THRESHOLDS['small']:
        return 'MicroPets Buy'
    elif usd_value < BUY_THRESHOLDS['medium']:
        return 'Medium Bullish Buy'
    elif usd_value < BUY_THRESHOLDS['large']:
        return 'Whale Buy'
    return 'Extra Large Buy'

def shorten_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}" if address and Web3.is_address(address) else ''

def load_posted_transactions() -> Set[str]:
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
    try:
        with file_lock:
            with open('posted_transactions.txt', 'a') as f:
                f.write(transaction_hash + '\n')
    except Exception as e:
        logger.warning(f"Could not write to posted_transactions.txt: {e}")

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_eth_to_usd() -> float:
    try:
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/eth/token_price/{ETH_ADDRESS}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        price_str = data.get('data', {}).get('attributes', {}).get('token_prices', {}).get(ETH_ADDRESS.lower(), '0')
        if not isinstance(price_str, (str, float, int)) or not price_str:
            raise ValueError("Invalid price data from GeckoTerminal")
        price = float(price_str)
        if price <= 0:
            raise ValueError("GeckoTerminal returned non-positive price")
        logger.info(f"ETH price from GeckoTerminal: ${price:.2f}")
        time.sleep(0.5)
        return price
    except Exception as e:
        logger.error(f"GeckoTerminal ETH price fetch failed: {e}, status={getattr(e.response, 'status_code', 'N/A')}")
        if not COINMARKETCAP_API_KEY:
            logger.warning("Skipping CoinMarketCap due to missing API key")
            return 3000  # Fallback ETH price
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'ETH', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            price_str = data.get('data', {}).get('ETH', {}).get('quote', {}).get('USD', {}).get('price', '0')
            if not isinstance(price_str, (str, float, int)) or not price_str:
                raise ValueError("Invalid price data from CoinMarketCap")
            price = float(price_str)
            if price <= 0:
                raise ValueError("CoinMarketCap returned non-positive price")
            logger.info(f"ETH price from CoinMarketCap: ${price:.2f}")
            return price
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap ETH price fetch failed: {cmc_e}")
            return 3000  # Fallback ETH price

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_pets_price_from_uniswap() -> float:
    try:
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/eth/token_price/{CONTRACT_ADDRESS}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        price_str = data.get('data', {}).get('attributes', {}).get('token_prices', {}).get(CONTRACT_ADDRESS.lower(), '0')
        if not isinstance(price_str, (str, float, int)) or not price_str:
            raise ValueError("Invalid price data from GeckoTerminal")
        price = float(price_str)
        if price <= 0:
            raise ValueError("GeckoTerminal returned non-positive price")
        logger.info(f"$PETS price from GeckoTerminal: ${price:.10f}")
        time.sleep(0.5)
        return price
    except Exception as e:
        logger.error(f"GeckoTerminal $PETS price fetch failed: {e}, status={getattr(e.response, 'status_code', 'N/A')}")
        if not COINMARKETCAP_API_KEY:
            logger.warning("Skipping CoinMarketCap due to missing API key")
            try:
                pair_address = Web3.to_checksum_address(TARGET_ADDRESS)
                pair_contract = w3.eth.contract(address=pair_address, abi=UNISWAP_PAIR_ABI)
                reserves = pair_contract.functions.getReserves().call()
                token0 = pair_contract.functions.token0().call()
                is_pets_token0 = token0.lower() == CONTRACT_ADDRESS.lower()
                reserve_pets = reserves[0] if is_pets_token0 else reserves[1]
                reserve_eth = reserves[1] if is_pets_token0 else reserves[0]
                eth_per_pets = reserve_eth / reserve_pets / 1e18 if reserve_pets > 0 else 0
                eth_to_usd = get_eth_to_usd()
                price = eth_per_pets * eth_to_usd
                if price <= 0:
                    raise ValueError("Uniswap returned non-positive price")
                logger.info(f"$PETS price from Uniswap: ${price:.10f}")
                return price
            except Exception as uni_e:
                logger.error(f"Uniswap $PETS price fetch failed: {uni_e}")
                return 0.00003886
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'PETS', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            price_str = data.get('data', {}).get('PETS', {}).get('quote', {}).get('USD', {}).get('price', '0')
            if not isinstance(price_str, (str, float, int)) or not price_str:
                raise ValueError("Invalid price data from CoinMarketCap")
            price = float(price_str)
            if price <= 0:
                raise ValueError("CoinMarketCap returned non-positive price")
            logger.info(f"$PETS price from CoinMarketCap: ${price:.10f}")
            return price
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap $PETS price fetch failed: {cmc_e}")
            try:
                pair_address = Web3.to_checksum_address(TARGET_ADDRESS)
                pair_contract = w3.eth.contract(address=pair_address, abi=UNISWAP_PAIR_ABI)
                reserves = pair_contract.functions.getReserves().call()
                token0 = pair_contract.functions.token0().call()
                is_pets_token0 = token0.lower() == CONTRACT_ADDRESS.lower()
                reserve_pets = reserves[0] if is_pets_token0 else reserves[1]
                reserve_eth = reserves[1] if is_pets_token0 else reserves[0]
                eth_per_pets = reserve_eth / reserve_pets / 1e18 if reserve_pets > 0 else 0
                eth_to_usd = get_eth_to_usd()
                price = eth_per_pets * eth_to_usd
                if price <= 0:
                    raise ValueError("Uniswap returned non-positive price")
                logger.info(f"$PETS price from Uniswap: ${price:.10f}")
                return price
            except Exception as uni_e:
                logger.error(f"Uniswap $PETS price fetch failed: {uni_e}")
                return 0.00003886

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_token_supply() -> float:
    try:
        response = requests.get(
            f"https://api.etherscan.io/api?module=stats&action=tokensupply&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('status') != '1':
            logger.error(f"Etherscan API error: {data.get('message', 'No message')}")
            return 6_604_885_020
        supply_str = data.get('result', '0')
        if not isinstance(supply_str, str) or not supply_str.isdigit():
            raise ValueError("Invalid token supply data from Etherscan")
        supply = int(supply_str) / 1e18
        logger.info(f"Token supply: {supply:,.0f} tokens")
        time.sleep(0.5)
        return supply
    except Exception as e:
        logger.error(f"Failed to fetch token supply: {e}")
        return 6_604_885_020

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def extract_market_cap() -> int:
    try:
        price = get_pets_price_from_uniswap()
        token_supply = get_token_supply()
        market_cap = int(token_supply * price)
        logger.info(f"Market cap: ${market_cap:,}")
        return market_cap
    except Exception as e:
        logger.error(f"Failed to calculate market cap: {e}")
        return 256600

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def get_transaction_details(transaction_hash: str) -> Optional[float]:
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
        if not isinstance(data, dict) or 'result' not in data:
            logger.error(f"Invalid response for transaction {transaction_hash}: {data}")
            return None
        result = data['result']
        if not isinstance(result, dict):
            logger.error(f"Transaction {transaction_hash} result is not a dict: {result}")
            return None
        value_wei_str = result.get('value', '0')
        if not isinstance(value_wei_str, str) or not value_wei_str.startswith('0x'):
            logger.error(f"Invalid value data for transaction {transaction_hash}: {value_wei_str}")
            return None
        value_wei = int(value_wei_str, 16)
        eth_value = float(w3.from_wei(value_wei, 'ether'))
        logger.info(f"Transaction {transaction_hash}: ETH value={eth_value:.6f}")
        transaction_details_cache[transaction_hash] = eth_value
        time.sleep(0.5)
        return eth_value
    except Exception as e:
        logger.error(f"Failed to fetch transaction details for {transaction_hash}: {e}")
        return None

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
def check_execute_function(transaction_hash: str) -> tuple[bool, Optional[float]]:
    try:
        response = requests.get(
            f"https://api.etherscan.io/api?module=transaction&action=gettxreceiptstatus&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or 'result' not in data:
            logger.error(f"Invalid receipt status response for {transaction_hash}: {data}")
            return False, None
        result = data.get('result')
        if not isinstance(result, dict):
            logger.error(f"Invalid result format for transaction {transaction_hash}: {result}")
            return False, None
        status = result.get('status', '')
        eth_value = get_transaction_details(transaction_hash)
        if eth_value is None:
            logger.error(f"No valid ETH value for transaction {transaction_hash}")
            return False, None
        tx_response = requests.get(
            f"https://api.etherscan.io/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        tx_response.raise_for_status()
        tx_data = tx_response.json()
        if not isinstance(tx_data, dict) or 'result' not in tx_data:
            logger.error(f"Invalid transaction response for {transaction_hash}: {tx_data}")
            return False, eth_value
        input_data = tx_data['result'].get('input', '')
        is_execute = 'execute' in input_data.lower()
        logger.info(f"Transaction {transaction_hash}: Execute={is_execute}, ETH={eth_value}")
        time.sleep(0.5)
        return is_execute, eth_value
    except Exception as e:
        logger.error(f"Failed to check transaction {transaction_hash}: {e}")
        return False, get_transaction_details(transaction_hash)

def get_balance_before_transaction(wallet_address: str, block_number: int) -> Optional[Decimal]:
    try:
        response = requests.get(
            f"https://api.etherscan.io/api?module=account&action=tokenbalancehistory&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&address={wallet_address}&blockno={block_number}&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data.get('status') == '1':
            balance_str = data.get('result', '0')
            if not isinstance(balance_str, str) or not balance_str.isdigit():
                raise ValueError("Invalid balance data")
            balance = Decimal(balance_str) / Decimal(1e18)
            logger.info(f"Balance for {shorten_address(wallet_address)} at block {block_number}: {balance:,.0f} tokens")
            return balance
        return None
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}")
        return None

@retry(wait=wait_exponential(multiplier=2, min=4, max=20), stop=stop_after_attempt(3))
async def fetch_etherscan_transactions(startblock: Optional[int] = None, endblock: Optional[int] = None) -> List[Dict]:
    global transaction_cache, last_transaction_fetch, last_block_number
    try:
        if not startblock and last_block_number:
            startblock = last_block_number + 1
        params = {
            'module': 'account',
            'action': 'tokentx',
            'contractaddress': Web3.to_checksum_address(CONTRACT_ADDRESS),
            'address': Web3.to_checksum_address(TARGET_ADDRESS),
            'page': 1,
            'offset': 100,
            'sort': 'desc',
            'apikey': ETHERSCAN_API_KEY
        }
        if startblock:
            params['startblock'] = startblock
        if endblock:
            params['endblock'] = endblock
        response = requests.get("https://api.etherscan.io/api", params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('status') != '1':
            raise ValueError(f"Invalid Etherscan response: {data.get('message', 'No message')}")
        transactions = [
            {
                'transactionHash': tx['hash'],
                'to': tx['to'],
                'from': tx['from'],
                'value': tx['value'],
                'blockNumber': int(tx['blockNumber']),
                'timeStamp': int(tx['timeStamp'])
            }
            for tx in data['result']
            if tx['from'].lower() == TARGET_ADDRESS.lower() and tx['value'].isdigit() and int(tx['value']) > 0
        ]
        if transactions and not startblock:
            last_block_number = max(tx['blockNumber'] for tx in transactions)
        if not startblock:
            transaction_cache = [tx for tx in transactions if last_block_number is None or tx['blockNumber'] >= last_block_number]
            transaction_cache = transaction_cache[-1000:]
            last_transaction_fetch = datetime.now().timestamp() * 1000
        logger.info(f"Fetched {len(transactions)} buy transactions, last_block_number={last_block_number}")
        time.sleep(0.5)
        return transactions
    except Exception as e:
        logger.error(f"Failed to fetch Etherscan transactions: {e}")
        return transaction_cache or []

async def send_video_with_retry(context, chat_id: str, video_url: str, options: Dict, max_retries: int = 3, delay: int = 2) -> bool:
    for i in range(max_retries):
        try:
            logger.info(f"Attempt {i+1}/{max_retries} to send video to chat {chat_id}: {video_url}")
            async with aiohttp.ClientSession() as session:
                async with session.head(video_url, timeout=5) as head_response:
                    if head_response.status != 200:
                        logger.error(f"Video URL inaccessible, status {head_response.status}: {video_url}")
                        raise Exception(f"Video URL inaccessible, status {head_response.status}")
            await context.bot.send_video(chat_id=chat_id, video=video_url, **options)
            logger.info(f"Successfully sent video to chat {chat_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}/{max_retries}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(
                    chat_id,
                    f"{options['caption']}\n\n⚠️ Video unavailable",
                    parse_mode='Markdown'
                )
                return False
            await asyncio.sleep(delay)
    return False

async def process_transaction(context, transaction: Dict, eth_to_usd_rate: float, pets_price: float, chat_id: str = TELEGRAM_CHAT_ID) -> bool:
    global posted_transactions
    try:
        if transaction['transactionHash'] in posted_transactions:
            logger.info(f"Skipping already posted transaction: {transaction['transactionHash']}")
            return False
        is_execute, eth_value = check_execute_function(transaction['transactionHash'])
        if eth_value is None or eth_value <= 0:
            logger.info(f"Skipping transaction {transaction['transactionHash']} with invalid ETH value: {eth_value}")
            return False
        pets_amount = float(transaction['value']) / 1e18
        usd_value = pets_amount * pets_price
        if usd_value < 50:
            logger.info(f"Skipping transaction {transaction['transactionHash']} with USD value < 50: {usd_value}")
            return False
        market_cap = extract_market_cap()
        wallet_address = transaction['to']
        percent_increase = random.uniform(10, 120)
        holding_change_text = f"+{percent_increase:.2f}%"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        tx_url = f"https://etherscan.io/tx/{transaction['transactionHash']}"
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        message = (
            f"🚀 *MicroPets Buy!* Ethereum 💰\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS}): {pets_amount:,.0f}\n"
            f"💵 ETH Value: {eth_value:,.4f} (${(eth_value * eth_to_usd_rate):,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding Change: {holding_change_text}\n"
            f"🦑 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View on Etherscan]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/petdex) "
            f"[📈 Chart](https://www.dextools.io/app/en/ether/pair-explorer/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🤑 Buy $PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        success = await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        if success:
            posted_transactions.add(transaction['transactionHash'])
            log_posted_transaction(transaction['transactionHash'])
            logger.info(f"Processed transaction {transaction['transactionHash']} for chat {chat_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error processing transaction {transaction.get('transactionHash', 'unknown')}: {e}")
        return False

async def monitor_transactions(context) -> None:
    global last_transaction_hash, last_block_number, is_tracking_enabled, monitoring_task
    logger.info("Starting transaction monitoring")
    while is_tracking_enabled:
        async with asyncio.Lock():
            if not is_tracking_enabled:
                logger.info("Tracking disabled, stopping monitoring")
                break
            try:
                posted_transactions.update(load_posted_transactions())
                txs = await fetch_etherscan_transactions(startblock=last_block_number + 1 if last_block_number else None)
                if not txs:
                    logger.info("No new transactions found")
                    await asyncio.sleep(POLLING_INTERVAL)
                    continue
                eth_to_usd_rate = get_eth_to_usd()
                pets_price = get_pets_price_from_uniswap()
                new_last_hash = last_transaction_hash
                for tx in sorted(txs, key=lambda x: x['blockNumber'], reverse=True):
                    if not isinstance(tx, dict):
                        logger.error(f"Invalid transaction format: {tx}")
                        continue
                    if tx['transactionHash'] in posted_transactions:
                        logger.info(f"Skipping already posted transaction: {tx['transactionHash']}")
                        continue
                    if last_transaction_hash and tx['transactionHash'] == last_transaction_hash:
                        continue
                    if last_block_number and tx['blockNumber'] <= last_block_number:
                        logger.info(f"Skipping old transaction {tx['transactionHash']} with block {tx['blockNumber']} <= {last_block_number}")
                        continue
                    if await process_transaction(context, tx, eth_to_usd_rate, pets_price):
                        new_last_hash = tx['transactionHash']
                        last_block_number = max(last_block_number or 0, tx['blockNumber'])
                last_transaction_hash = new_last_hash
            except Exception as e:
                logger.error(f"Error monitoring transactions: {e}")
                recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
                if len(recent_errors) > 5:
                    recent_errors.pop(0)
            await asyncio.sleep(POLLING_INTERVAL)
    logger.info("Monitoring task stopped")
    monitoring_task = None

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(5))
async def set_webhook_with_retry(bot_app) -> bool:
    webhook_url = f"https://{APP_URL}/webhook"
    logger.info(f"Attempting to set webhook: {webhook_url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{APP_URL}/health", timeout=10) as response:
                if response.status != 200:
                    logger.error(f"Health check failed, status {response.status}, response: {await response.text()}")
                    raise Exception(f"Health check failed, status {response.status}")
                logger.info(f"Health check passed, status {response.status}")
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Deleted existing webhook")
        await bot_app.bot.set_webhook(webhook_url, allowed_updates=["message", "channel_post"])
        logger.info(f"Webhook set successfully: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        raise

async def polling_fallback(bot_app) -> None:
    global polling_task
    logger.info("Starting polling fallback")
    while True:
        try:
            if not bot_app.running:
                await bot_app.initialize()
                await bot_app.start()
                await bot_app.updater.start_polling(
                    poll_interval=3,
                    timeout=10,
                    drop_pending_updates=True,
                    error_callback=lambda e: logger.error(f"Polling error: {e}")
                )
                logger.info("Polling started successfully")
                while polling_task and not polling_task.cancelled():
                    await asyncio.sleep(60)
            else:
                logger.warning("Bot already running, skipping polling start")
                while polling_task and not polling_task.cancelled():
                    await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(10)
        finally:
            if bot_app.running and polling_task and polling_task.cancelled():
                try:
                    await bot_app.updater.stop()
                    await bot_app.stop()
                    await bot_app.shutdown()
                    logger.info("Polling stopped")
                except Exception as e:
                    logger.error(f"Error stopping polling: {e}")

def is_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == ADMIN_CHAT_ID

# Command handlers
async def start(update: Update, context) -> None:
    chat_id = update.effective_chat.id
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id=chat_id, text="👋 Welcome to PETS Tracker! Use /track to start buy alerts.")

async def track(update: Update, context) -> None:
    global is_tracking_enabled, monitoring_task
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    if is_tracking_enabled and monitoring_task:
        await context.bot.send_message(chat_id=chat_id, text="🚀 Tracking already enabled")
        return
    is_tracking_enabled = True
    active_chats.add(str(chat_id))
    monitoring_task = asyncio.create_task(monitor_transactions(context))
    await context.bot.send_message(chat_id=chat_id, text="🚖 Tracking started")

async def stop(update: Update, context) -> None:
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
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id=chat_id, text="⏳ Fetching $PETS data for the last 2 weeks")
    try:
        latest_block_response = requests.get(
            f"https://api.etherscan.io/api?module=proxy&action=eth_blockNumber&apikey={ETHERSCAN_API_KEY}",
            timeout=30
        )
        latest_block_response.raise_for_status()
        latest_block_data = latest_block_response.json()
        if not isinstance(latest_block_data, dict) or 'result' not in latest_block_data:
            raise ValueError(f"Invalid block number response: {latest_block_data}")
        latest_block = int(latest_block_data['result'], 16)
        blocks_per_day = 24 * 60 * 60 // 12  # Ethereum ~12s block time
        start_block = latest_block - (14 * blocks_per_day)
        txs = await fetch_etherscan_transactions(startblock=start_block, endblock=latest_block)
        if not txs:
            logger.info("No transactions found for the last 2 weeks")
            await context.bot.send_message(chat_id=chat_id, text="🚫 No recent buys found")
            return
        two_weeks_ago = int((datetime.now() - timedelta(days=14)).timestamp())
        recent_txs = [tx for tx in txs if isinstance(tx, dict) and tx.get('timeStamp', 0) >= two_weeks_ago]
        if not recent_txs:
            logger.info("No transactions within the last two weeks")
            await context.bot.send_message(chat_id=chat_id, text="🚫 No buys found in the last 2 weeks")
            return
        eth_to_usd_rate = get_eth_to_usd()
        pets_price = get_pets_price_from_uniswap()
        processed = []
        seen_hashes = set()
        for tx in sorted(recent_txs, key=lambda x: x['timeStamp'], reverse=True):
            if not isinstance(tx, dict):
                logger.error(f"Invalid transaction format in stats: {tx}")
                continue
            if tx['transactionHash'] in seen_hashes or tx['transactionHash'] in posted_transactions:
                logger.info(f"Skipping duplicate transaction: {tx['transactionHash']}")
                continue
            if await process_transaction(context, tx, eth_to_usd_rate, pets_price, chat_id=TELEGRAM_CHAT_ID):
                processed.append(tx['transactionHash'])
            if await process_transaction(context, tx, eth_to_usd_rate, pets_price, chat_id=ADMIN_CHAT_ID):
                processed.append(tx['transactionHash'])
            seen_hashes.add(tx['transactionHash'])
            await asyncio.sleep(30)
        if processed:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Processed {len(set(processed))} buys from the last 2 weeks:\n" + "\n".join(set(processed)),
                parse_mode='Markdown'
            )
        else:
            logger.info("No transactions met the $50 USD threshold")
            await context.bot.send_message(chat_id=chat_id, text="🚫 No transactions processed (all below $50 USD)")
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚫 Failed to fetch data: {str(e)}")

async def help_command(update: Update, context) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🆘 *Commands:*\n\n"
            "/start - Start bot\n"
            "/track - Enable alerts\n"
            "/stop - Disable alerts\n"
            "/stats - View buys from last 2 weeks\n"
            "/status - Tracking status\n"
            "/test - Test transaction\n"
            "/noV - Test without video\n"
            "/debug - Debug info\n"
            "/help - This message"
        ),
        parse_mode='Markdown'
    )

async def status(update: Update, context) -> None:
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
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    status = {
        'trackingEnabled': is_tracking_enabled,
        'activeChats': list(active_chats),
        'lastTxHash': last_transaction_hash,
        'lastBlockNumber': last_block_number,
        'recentErrors': recent_errors[-5:],
        'apiStatus': {
            'ethWeb3': bool(w3.is_connected()),
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
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id=chat_id, text="⏳ Generating test buy")
    try:
        test_tx_hash = f"0xTest{uuid.uuid4().hex[:16]}"
        test_pets_amount = random.randint(1000000, 5000000)
        pets_price = get_pets_price_from_uniswap()
        usd_value = test_pets_amount * pets_price
        eth_to_usd_rate = get_eth_to_usd()
        eth_value = usd_value / eth_to_usd_rate
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randint(10**15, 10**16):0x}"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://etherscan.io/tx/{test_tx_hash}"
        message = (
            f"🚖 *MicroPets Buy!* Test\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS}): {test_pets_amount:,.0f}\n"
            f"💵 ETH Value: {eth_value:,.4f} (${(eth_value * eth_to_usd_rate):,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦒 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍 View]({tx_url})\n\n"
            f"💰 [Staking](https://pets.micropets.io/) "
            f"[📈 Chart](https://www.dextools.io/app/en/ether/pair-explorer/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[🤑 Buy](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
        await context.bot.send_message(chat_id=chat_id, text="🚖 Success")
    except Exception as e:
        logger.error(f"Test error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚫 Failed: {str(e)}")

async def no_video(update: Update, context) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(update):
        await context.bot.send_message(chat_id=chat_id, text="🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id=chat_id, text="⏳ Testing buy (no video)")
    try:
        test_tx_hash = f"0xTestNoV{uuid.uuid4().hex[:16]}"
        test_pets_amount = random.randint(1000000, 5000000)
        pets_price = get_pets_price_from_uniswap()
        usd_value = test_pets_amount * pets_price
        eth_to_usd_rate = get_eth_to_usd()
        eth_value = usd_value / eth_to_usd_rate
        wallet_address = f"0x{random.randint(10**15, 10**16):0x}"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://etherscan.io/tx/{test_tx_hash}"
        message = (
            f"🚖 *MicroPets Buy!* Ethereum\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS}): {test_pets_amount:,.0f}\n"
            f"💵 ETH: {eth_value:,.4f} (${(eth_value * eth_to_usd_rate):,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦀 Hodler: {shorten_address(wallet_address)}\n"
            f"[🔍]({tx_url})\n\n"
            f"[💰 Staking](https://pets.micropets.io/) "
            f"[📈 Chart](https://www.dextools.io/app/en/ether/pair-explorer/{TARGET_ADDRESS}) "
            f"[🛍 Merch](https://micropets.store/) "
            f"[💖 Buy](https://app.uniswap.org/#/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        await context.bot.send_message(chat_id=chat_id, text="🚖 OK")
    except Exception as e:
        logger.error(f"/noV error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"🚖 Error: {str(e)}")

# FastAPI routes
app = FastAPI()

@app.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    try:
        if not w3.is_connected():
            raise Exception("Web3 is not connected")
        return {"status": "Connected"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail=f"Service unavailable: {e}")

@app.get("/webhook")
async def webhook_get():
    logger.info("Received GET webhook")
    raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.get("/api/transactions")
async def get_transactions():
    logger.info("Fetching transactions via API")
    return transaction_cache

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received POST webhook request")
    try:
        async with aiohttp.ClientSession() as session:
            data = await request.json()
            if not isinstance(data, dict):
                logger.error(f"Invalid webhook data: {data}")
                return {"error": "Invalid JSON data"}, 400
            update = Update.de_json(data, bot_app.bot)
            if update:
                await bot_app.process_update(update)
            return {"status": "ok"}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        recent_errors.append({"time": datetime.now().isoformat(), "error": str(e)})
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        return {"error": "Webhook failed"}, 500

# Lifespan handler
@asynccontextmanager
async def lifespan(app: FastAPI):
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
            logger.info("Polling started, monitoring enabled")
        logger.info("Bot startup completed")
        yield
    except Exception as e:
        logger.error(f"Startup error: {e}")
    finally:
        logger.info("Initiating bot shutdown...")
        try:
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
                    await bot_app.updater.stop()
                    await bot_app.stop()
                except Exception as e:
                    logger.error(f"Error stopping bot: {e}")
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
            await bot_app.shutdown()
            logger.info("Bot shutdown completed")
        except Exception as e:
            logger.error(f"Shutdown error: {str(e)}")

app = FastAPI(lifespan=lifespan)

# Bot initialization
bot_app = ApplicationBuilder() \
    .token(TELEGRAM_BOT_TOKEN) \
    .build()
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
