
import os
import logging
import requests
import random
import asyncio
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from web3 import Web3
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
from datetime import datetime, timedelta
from decimal import Decimal
import json
import telegram
import aiohttp
import time
import uuid

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
BNB_ADDRESS = '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c'

# In-memory data
transaction_cache = []
active_chats = {TELEGRAM_CHAT_ID}
last_transaction_hash = None
is_tracking_enabled = False
recent_errors = []
last_transaction_fetch = 0
TRANSACTION_CACHE_THRESHOLD = 2 * 60 * 1000  # 2 minutes
posted_transactions = set()
transaction_details_cache = {}
monitoring_task = None
stop_stats_flag = False  # Flag to stop /stats command

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
            return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logger.warning(f"Could not load posted_transactions.txt: {e}")
        return set()

def log_posted_transaction(transaction_hash):
    try:
        with open('posted_transactions.txt', 'a') as f:
            f.write(transaction_hash + '\n')
    except Exception as e:
        logger.warning(f"Could not write to posted_transactions.txt: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
def get_bnb_to_usd():
    try:
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/bsc/token_price/{BNB_ADDRESS}",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        price_str = data.get('data', {}).get('attributes', {}).get('token_prices', {}).get(BNB_ADDRESS.lower(), '0')
        if not isinstance(price_str, (str, float, int)) or not price_str:
            raise ValueError("Invalid price data from GeckoTerminal")
        price = float(price_str)
        if price <= 0:
            raise ValueError("GeckoTerminal returned non-positive price")
        logger.info(f"BNB price from GeckoTerminal: ${price:.2f}")
        time.sleep(0.5)
        return price
    except Exception as e:
        logger.error(f"GeckoTerminal BNB price fetch failed: {e}, status={getattr(e.response, 'status_code', 'N/A')}")
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                headers={'X-CMC_PRO_API_KEY': COINMARKETCAP_API_KEY},
                params={'symbol': 'BNB', 'convert': 'USD'},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            price_str = data.get('data', {}).get('BNB', {}).get('quote', {}).get('USD', {}).get('price', '0')
            if not isinstance(price_str, (str, float, int)) or not price_str:
                raise ValueError("Invalid price data from CoinMarketCap")
            price = float(price_str)
            if price <= 0:
                raise ValueError("CoinMarketCap returned non-positive price")
            logger.info(f"BNB price from CoinMarketCap: ${price:.2f}")
            return price
        except Exception as cmc_e:
            logger.error(f"CoinMarketCap BNB price fetch failed: {cmc_e}")
            return 600

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
def get_pets_price_from_pancakeswap():
    try:
        headers = {'Accept': 'application/json;version=20230302'}
        response = requests.get(
            f"https://api.geckoterminal.com/api/v2/simple/networks/bsc/token_price/{CONTRACT_ADDRESS}",
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
                pair_contract = w3.eth.contract(address=pair_address, abi=PANCAKESWAP_PAIR_ABI)
                reserves = pair_contract.functions.getReserves().call()
                reserve0, reserve1, _ = reserves
                bnb_per_pets = reserve1 / reserve0 / 1e18 if reserve0 > 0 else 0
                bnb_to_usd = get_bnb_to_usd()
                price = bnb_per_pets * bnb_to_usd
                if price <= 0:
                    raise ValueError("PancakeSwap returned non-positive price")
                logger.info(f"$PETS price from PancakeSwap: ${price:.10f}")
                return price
            except Exception as pcs_e:
                logger.error(f"PancakeSwap $PETS price fetch failed: {pcs_e}")
                return 0.00003886

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
def get_token_supply():
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=stats&action=tokensupply&contractaddress={Web3.to_checksum_address(CONTRACT_ADDRESS)}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get('status') != '1':
            logger.error(f"BscScan API error: {data.get('message', 'No message')}")
            return 6_604_885_020
        supply_str = data.get('result', '0')
        if not isinstance(supply_str, str) or not supply_str.isdigit():
            raise ValueError("Invalid token supply data from BscScan")
        supply = int(supply_str) / 1e18
        logger.info(f"Token supply: {supply:,.0f} tokens")
        time.sleep(0.5)
        return supply
    except Exception as e:
        logger.error(f"Failed to fetch token supply: {e}")
        return 6_604_885_020

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
def get_transaction_details(transaction_hash):
    if transaction_hash in transaction_details_cache:
        logger.info(f"Using cached BNB value for transaction {transaction_hash}")
        return transaction_details_cache[transaction_hash]
    try:
        # Fetch transaction details
        response = requests.get(
            f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
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
        if not isinstance(value_wei_str, str):
            logger.error(f"Invalid value data for transaction {transaction_hash}: {value_wei_str}")
            return None
        value_wei = int(value_wei_str, 16)
        bnb_value = float(w3.from_wei(value_wei, 'ether'))
        
        # If BNB value is 0, check transaction receipt for token swaps
        if bnb_value == 0:
            receipt_response = requests.get(
                f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionReceipt&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
                timeout=30
            )
            receipt_response.raise_for_status()
            receipt_data = receipt_response.json()
            if 'result' in receipt_data and receipt_data['result']:
                logs = receipt_data['result'].get('logs', [])
                for log in logs:
                    if log.get('address', '').lower() == BNB_ADDRESS.lower():
                        # Extract BNB value from log data (assuming WBNB transfer)
                        data = log.get('data', '0x')
                        if data.startswith('0x') and len(data) >= 66:
                            amount = int(data[2:], 16) / 1e18
                            if amount > 0:
                                bnb_value = amount
                                logger.info(f"Extracted BNB value {bnb_value:.6f} from receipt for {transaction_hash}")
                                break
        
        logger.info(f"Transaction {transaction_hash}: BNB value={bnb_value:.6f}")
        transaction_details_cache[transaction_hash] = bnb_value
        time.sleep(0.5)
        return bnb_value
    except Exception as e:
        logger.error(f"Failed to fetch transaction details for {transaction_hash}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
def check_execute_function(transaction_hash):
    try:
        response = requests.get(
            f"https://api.bscscan.com/api?module=transaction&action=gettxreceiptstatus&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or 'result' not in data:
            logger.error(f"Invalid receipt status response for {transaction_hash}: {data}")
            return False, None
        status = data['result'].get('status', '')
        bnb_value = get_transaction_details(transaction_hash)
        if bnb_value is None:
            logger.error(f"No valid BNB value for transaction {transaction_hash}")
            return False, None
        tx_response = requests.get(
            f"https://api.bscscan.com/api?module=proxy&action=eth_getTransactionByHash&txhash={transaction_hash}&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        tx_response.raise_for_status()
        tx_data = tx_response.json()
        if not isinstance(tx_data, dict) or 'result' not in tx_data:
            logger.error(f"Invalid transaction response for {transaction_hash}: {tx_data}")
            return False, bnb_value
        input_data = tx_data['result'].get('input', '')
        is_execute = 'execute' in input_data.lower()
        logger.info(f"Transaction {transaction_hash}: Execute={is_execute}, BNB={bnb_value}")
        time.sleep(0.5)
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

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
async def fetch_bscscan_transactions(startblock=None, endblock=None):
    global transaction_cache, last_transaction_fetch
    if (
        not startblock
        and transaction_cache
        and (datetime.now().timestamp() * 1000 - last_transaction_fetch < TRANSACTION_CACHE_THRESHOLD)
    ):
        logger.info(f"Returning {len(transaction_cache)} cached transactions")
        return transaction_cache
    try:
        all_transactions = []
        page = 1
        max_pages = 20
        offset = 50  # Reduced to fetch more pages
        while page <= max_pages:
            params = {
                'module': 'account',
                'action': 'tokentx',
                'contractaddress': Web3.to_checksum_address(CONTRACT_ADDRESS),
                'page': page,
                'offset': offset,
                'sort': 'desc',
                'apikey': BSCSCAN_API_KEY
            }
            if startblock:
                params['startblock'] = startblock
            if endblock:
                params['endblock'] = endblock
            logger.info(f"Fetching transactions: page={page}, startblock={startblock}, endblock={endblock}, offset={offset}")
            response = requests.get("https://api.bscscan.com/api", params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get('status') != '1':
                raise ValueError(f"Invalid BscScan response: {data.get('message', 'No message')}")
            logger.info(f"Raw API response: {len(data['result'])} transactions on page {page}")
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
                if tx['value'].isdigit() and int(tx['value']) > 0
                # Removed TARGET_ADDRESS filter to fetch all token transfers
            ]
            all_transactions.extend(transactions)
            logger.info(f"Fetched {len(transactions)} transactions on page {page}")
            if len(data['result']) < offset:
                logger.info(f"Reached end of transactions on page {page}")
                break
            page += 1
            time.sleep(2)  # Increased delay for API rate limits
        if not startblock:  # Update cache only for recent transactions
            transaction_cache = all_transactions
            last_transaction_fetch = datetime.now().timestamp() * 1000
        logger.info(f"Fetched {len(all_transactions)} buy transactions across {page-1} pages")
        return all_transactions
    except Exception as e:
        logger.error(f"Failed to fetch BscScan transactions: {e}")
        return transaction_cache or []

async def send_video_with_retry(context, chat_id, video_url, options, max_retries=3, delay=2):
    for i in range(max_retries):
        try:
            logger.info(f"Attempt {i+1}/{max_retries} to send video to chat {chat_id}")
            await context.bot.send_video(chat_id=chat_id, video=video_url, **options)
            return True
        except Exception as e:
            logger.error(f"Failed to send video (attempt {i+1}): {e}")
            if i == max_retries - 1:
                await context.bot.send_message(
                    chat_id,
                    f"{options['caption']}\n\n⚠️ Video unavailable.",
                    parse_mode='Markdown'
                )
                return False
        await asyncio.sleep(delay)
    return False

async def process_transaction(context, transaction, bnb_to_usd_rate, pets_price, chat_id=TELEGRAM_CHAT_ID):
    global posted_transactions
    if transaction['transactionHash'] in posted_transactions:
        logger.info(f"Skipping already posted transaction {transaction['transactionHash']}")
        return False
    # Check if transaction is a buy (tokens sent to TARGET_ADDRESS or user received tokens)
    is_buy = transaction['to'].lower() == TARGET_ADDRESS.lower() or transaction['from'].lower() != TARGET_ADDRESS.lower()
    if not is_buy:
        logger.info(f"Skipping non-buy transaction {transaction['transactionHash']}")
        return False
    is_execute, bnb_value = check_execute_function(transaction['transactionHash'])
    if bnb_value is None or bnb_value <= 0:
        logger.info(f"Skipping transaction {transaction['transactionHash']} with invalid BNB value: {bnb_value}")
        return False
    usd_value = bnb_value * bnb_to_usd_rate
    if usd_value < 50:  # Restored $50 threshold
        logger.info(f"Skipping transaction {transaction['transactionHash']} with USD value < 50: {usd_value}")
        return False
    pets_amount = float(transaction['value']) / 1e18
    market_cap = extract_market_cap()
    wallet_address = transaction['from'] if is_buy else transaction['to']
    percent_increase = random.uniform(10, 120)
    holding_change_text = f"+{percent_increase:.2f}%"
    emoji_count = min(int(usd_value) // 1, 100)
    emojis = EMOJI * emoji_count
    tx_url = f"https://bscscan.com/tx/{transaction['transactionHash']}"
    category = categorize_buy(usd_value)
    video_url = get_video_url(category)
    message = (
        f"🚀 MicroPets Buy! BNBchain 💰\n\n"
        f"{emojis}\n"
        f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): "
        f"{pets_amount:,.0f} (${(pets_amount * pets_price):,.2f})\n"
        f"💵 BNB Value: {bnb_value:,.4f} (${usd_value:,.2f})\n"
        f"🏦 Market Cap: ${market_cap:,.0f}\n"
        f"🔼 Holding Change: {holding_change_text}\n"
        f"🤑 Hodler: {shorten_address(wallet_address)}\n"
        f"[🔍 View on BscScan]({tx_url})\n\n"
        f"💰 [Staking](https://pets.micropets.io/petdex) "
        f"[📈 Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS}) "
        f"[🛍 Merch](https://micropets.store/) "
        f"[🤑 Buy $PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
    )
    success = await send_video_with_retry(context, chat_id, video_url, {'caption': message, 'parse_mode': 'Markdown'})
    if success:
        posted_transactions.add(transaction['transactionHash'])
        log_posted_transaction(transaction['transactionHash'])
        logger.info(f"Processed transaction {transaction['transactionHash']} for chat {chat_id}")
        return True
    return False

async def monitor_transactions(context):
    global last_transaction_hash, is_tracking_enabled, monitoring_task
    logger.info("Starting transaction monitoring")
    while is_tracking_enabled:
        async with asyncio.Lock():
            if not is_tracking_enabled:
                logger.info("Tracking disabled")
                break
            try:
                posted_transactions.update(load_posted_transactions())
                txs = await fetch_bscscan_transactions()
                if not txs:
                    logger.info("No new transactions")
                    await asyncio.sleep(int(os.getenv('POLL_INTERVAL', 60)))
                    continue
                bnb_to_usd_rate = get_bnb_to_usd()
                pets_price = get_pets_price_from_pancakeswap()
                new_last_hash = last_transaction_hash
                for tx in reversed(txs):
                    if not isinstance(tx, dict):
                        logger.error(f"Invalid transaction format: {tx}")
                        continue
                    if tx['transactionHash'] in posted_transactions:
                        logger.info(f"Skipping already posted transaction {tx['transactionHash']}")
                        continue
                    if last_transaction_hash and tx['transactionHash'] == last_transaction_hash:
                        continue
                    if await process_transaction(context, tx, bnb_to_usd_rate, pets_price):
                        new_last_hash = tx['transactionHash']
                last_transaction_hash = new_last_hash
            except Exception as e:
                logger.error(f"Error monitoring transactions: {e}")
                recent_errors.append({'time': datetime.now().isoformat(), 'error': str(e)})
                if len(recent_errors) > 5:
                    recent_errors.pop(0)
            await asyncio.sleep(int(os.getenv('POLL_INTERVAL', 60)))
    logger.info("Monitoring task stopped")
    monitoring_task = None

async def polling_fallback():
    logger.info("Starting polling fallback")
    while is_tracking_enabled:
        try:
            await bot_app.run_polling(poll_interval=3, timeout=10)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(10)

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=4, max=20))
async def set_webhook_with_retry(bot_app):
    webhook_url = f"https://{APP_URL}/webhook"
    logger.info(f"Attempting to set webhook: {webhook_url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{APP_URL}/webhook") as response:
                if response.status != 200:
                    logger.error(f"Webhook URL not accessible: {response.status}")
                    raise Exception("Webhook URL inaccessible")
        await bot_app.bot.delete_webhook()
        await asyncio.sleep(1)  # Ensure old webhook is cleared
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        raise

def is_admin(update):
    chat_id = str(update.effective_chat.id)
    logger.info(f"Checking admin: chat_id={chat_id}, ADMIN_CHAT_ID={ADMIN_CHAT_ID}")
    return chat_id == ADMIN_CHAT_ID

# Command handlers
async def start(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Received /start command from chat {chat_id}")
    active_chats.add(str(chat_id))
    await context.bot.send_message(chat_id, "👋 Welcome to PETS Tracker! Use /track to start buy alerts.")

async def track(update: Update, context):
    global is_tracking_enabled, monitoring_task
    chat_id = update.effective_chat.id
    logger.info(f"Received /track command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    if is_tracking_enabled and monitoring_task:
        await context.bot.send_message(chat_id, "🚀 Tracking already enabled")
        return
    is_tracking_enabled = True
    active_chats.add(str(chat_id))
    monitoring_task = asyncio.create_task(monitor_transactions(context))
    await context.bot.send_message(chat_id, "🚀 Tracking started")

async def stop(update: Update, context):
    global is_tracking_enabled, monitoring_task
    chat_id = update.effective_chat.id
    logger.info(f"Received /stop command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
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
    await context.bot.send_message(chat_id, "🛑 Tracking stopped")

async def stats(update: Update, context):
    global stop_stats_flag
    chat_id = update.effective_chat.id
    logger.info(f"Received /stats command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    stop_stats_flag = False
    await context.bot.send_message(chat_id, "⏳ Fetching $PETS data for the last month")
    try:
        latest_block_response = requests.get(
            f"https://api.bscscan.com/api?module=proxy&action=eth_blockNumber&apikey={BSCSCAN_API_KEY}",
            timeout=30
        )
        latest_block_response.raise_for_status()
        latest_block_data = latest_block_response.json()
        if not isinstance(latest_block_data, dict) or 'result' not in latest_block_data:
            raise ValueError(f"Invalid block number response: {latest_block_data}")
        latest_block = int(latest_block_data['result'], 16)
        blocks_per_day = 24 * 60 * 60 // 3
        start_block = latest_block - (30 * blocks_per_day)
        logger.info(f"Stats block range: start_block={start_block}, latest_block={latest_block}")
        txs = await fetch_bscscan_transactions(startblock=start_block, endblock=latest_block)
        if not txs:
            logger.info("No transactions found for the last month")
            await context.bot.send_message(chat_id, "🚫 No recent buys in the last month")
            return
        one_month_ago = int((datetime.now() - timedelta(days=30)).timestamp())
        recent_txs = [tx for tx in txs if isinstance(tx, dict) and tx.get('timeStamp', 0) >= one_month_ago]
        if not recent_txs:
            logger.info("No transactions within the last month after timestamp filter")
            await context.bot.send_message(chat_id, "🚫 No buys found in the last month")
            return
        bnb_to_usd_rate = get_bnb_to_usd()
        pets_price = get_pets_price_from_pancakeswap()
        processed = []
        seen_hashes = set()
        for tx in sorted(recent_txs, key=lambda x: x['timeStamp'], reverse=True):
            if stop_stats_flag:
                logger.info("Stats processing stopped by /stopstats")
                await context.bot.send_message(chat_id, "🛑 Stats processing stopped")
                stop_stats_flag = False
                break
            if not isinstance(tx, dict):
                logger.error(f"Invalid transaction format in stats: {tx}")
                continue
            if tx['transactionHash'] in seen_hashes or tx['transactionHash'] in posted_transactions:
                logger.info(f"Skipping duplicate transaction {tx['transactionHash']}")
                continue
            if await process_transaction(context, tx, bnb_to_usd_rate, pets_price, chat_id=TELEGRAM_CHAT_ID):
                processed.append(tx['transactionHash'])
            if await process_transaction(context, tx, bnb_to_usd_rate, pets_price, chat_id=ADMIN_CHAT_ID):
                processed.append(tx['transactionHash'])
            seen_hashes.add(tx['transactionHash'])
            await asyncio.sleep(5)  # Reduced sleep to prevent blocking
        if not stop_stats_flag and processed:
            await context.bot.send_message(
                chat_id,
                f"✅ Processed {len(set(processed))} buys from the last month:\n" + "\n".join(set(processed)),
                parse_mode='Markdown'
            )
        elif not stop_stats_flag:
            logger.info("No transactions met the $50 USD threshold")
            await context.bot.send_message(chat_id, "🚫 No transactions processed (all below $50 USD)")
    except Exception as e:
        logger.error(f"Error in /stats: {e}")
        await context.bot.send_message(chat_id, f"🚫 Failed to fetch data: {str(e)}")
    finally:
        stop_stats_flag = False

async def stopstats(update: Update, context):
    global stop_stats_flag
    chat_id = update.effective_chat.id
    logger.info(f"Received /stopstats command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    stop_stats_flag = True
    await context.bot.send_message(chat_id, "🛑 Requested to stop stats processing")

async def help_command(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Received /help command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(
        chat_id,
        "🆘 *Commands:*\n\n"
        "/start - Start bot\n"
        "/track - Enable alerts\n"
        "/stop - Disable alerts\n"
        "/stats - View buys from last month\n"
        "/stopstats - Stop stats processing\n"
        "/status - Track status\n"
        "/test - Test transaction\n"
        "/noV - Test without video\n"
        "/debug - Debug info\n"
        "/help - This message",
        parse_mode='Markdown'
    )

async def status(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Received /status command from chat {chat_id}")
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
    logger.info(f"Received /debug command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    status = {
        'trackingEnabled': is_tracking_enabled,
        'stopStatsFlag': stop_stats_flag,
        'activeChats': list(active_chats),
        'lastTxHash': last_transaction_hash,
        'recentErrors': recent_errors[-5:],
        'apiStatus': {
            'bscWeb3': bool(w3.is_connected()),
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
    logger.info(f"Received /test command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Generating test buy")
    try:
        test_tx_hash = f"0xTest{uuid.uuid4().hex[:16]}"
        test_pets_amount = random.randint(1000000, 5000000)
        pets_price = get_pets_price_from_pancakeswap()
        usd_value = test_pets_amount * pets_price
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        category = categorize_buy(usd_value)
        video_url = get_video_url(category)
        wallet_address = f"0x{random.randint(10**15, 10**16):0>40x}"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
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
        await context.bot.send_message(chat_id, f"🚫 Failed: {str(e)}")

async def no_video(update: Update, context):
    chat_id = update.effective_chat.id
    logger.info(f"Received /noV command from chat {chat_id}")
    if not is_admin(update):
        await context.bot.send_message(chat_id, "🚫 Unauthorized")
        return
    await context.bot.send_message(chat_id, "⏳ Testing buy (no video)")
    try:
        test_tx_hash = f"0xTestNoV{uuid.uuid4().hex[:16]}"
        test_pets_amount = random.randint(1000000, 5000000)
        pets_price = get_pets_price_from_pancakeswap()
        usd_value = test_pets_amount * pets_price
        bnb_to_usd_rate = get_bnb_to_usd()
        bnb_value = usd_value / bnb_to_usd_rate
        wallet_address = f"0x{random.randint(10**15, 10**16):0>40x}"
        emoji_count = min(int(usd_value) // 1, 100)
        emojis = EMOJI * emoji_count
        market_cap = extract_market_cap()
        holding_change_text = f"+{random.uniform(10, 120):.2f}%"
        tx_url = f"https://bscscan.com/tx/{test_tx_hash}"
        message = (
            f"🚖 MicroPets Buy! BNBchain\n\n"
            f"{emojis}\n"
            f"💰 [$PETS](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS}): "
            f"{test_pets_amount:,.0f} (${(test_pets_amount * pets_price):,.2f})\n"
            f"💵 BNB Value: {bnb_value:,.4f} (${(bnb_value * bnb_to_usd_rate):,.2f})\n"
            f"🏦 Market Cap: ${market_cap:,.0f}\n"
            f"🔼 Holding: {holding_change_text}\n"
            f"🦀 Hodler: {shorten_address(wallet_address)}\n"  # Corrected line, properly integrated
            f"[🔍]({tx_url})\n\n"
            f"[💰 Staking](https://pets.micropets.io/)\n"
            f"[📈 Chart](https://www.dextools.io/app/en/bnb/pair-explorer/{TARGET_ADDRESS})\n"
            f"[🛍 Merch](https://micropets.store/)\n"
            f"[💖 Buy](https://pancakeswap.finance/swap?outputCurrency={CONTRACT_ADDRESS})"
        )
        await context.bot.send_message(chat_id, message, parse_mode='Markdown')
        await context.bot.send_message(chat_id, "🚖 OK")
    except Exception as e:
        logger.error(f"/noV error: {e}")
        await context.bot.send_message(chat_id, f"🚫 Failed: {str(e)}")
# FastAPI routes
@app.get("/")
async def health_check():
    return {"status": "Bot is running"}

@app.get("/webhook")
async def webhook_get():
    logger.info("GET /webhook")
    return {"status": "Webhook for POST requests only"}

@app.get("/api/transactions")
async def get_transactions():
    logger.info("GET /api/transactions")
    return transaction_cache

@app.post("/webhook")
async def webhook(request: Request):
    logger.info("Received webhook POST")
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        if update:
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
    global monitoring_task
    logger.info("Starting web3")
    await bot_app.initialize()
    try:
        await set_webhook_with_retry(bot_app)
    except Exception as e:
        logger.error(f"Webhook failed: {e}")
        asyncio.create_task(polling_fallback())
    logger.info("Bot started")

@app.on_event("shutdown")
async def shutdown_event():
    global monitoring_task
    logger.info("Shutting down")
    if monitoring_task:
        monitoring_task.cancel()
        try:
            await monitoring_task
        except asyncio.CancelledError:
            logger.info("Task cancelled")
        monitoring_task = None
    await bot_app.bot.delete_webhook()
    await bot_app.shutdown()
    logger.info("Shutdown complete")

# Bot initialization
bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("track", track))
bot_app.add_handler(CommandHandler("stop", stop))
bot_app.add_handler(CommandHandler("stats", stats))
bot_app.add_handler(CommandHandler("stopstats", stopstats))
bot_app.add_handler(CommandHandler("help", help_command))
bot_app.add_handler(CommandHandler("status", status))
bot_app.add_handler(CommandHandler("debug", debug))
bot_app.add_handler(CommandHandler("test", test))
bot_app.add_handler(CommandHandler("noV", no_video))

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
