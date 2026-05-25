"""
Infinitecoin Jumper Bot - Holder Model v6 (Wallet-Based Daily Cap + Daily Bonus Fix)
Free: 10K/day total | Holders (0.1 SOL worth INFINITE): 150K/day total
Wallet locked 1x forever. Daily claim tracking resets 24h after first claim.
"""
import os, json, logging, time, requests, asyncio, threading, base64, struct, sqlite3
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "https://your-app.up.railway.app").rstrip("/")
GAME_URL = os.environ.get("GAME_URL", "https://your-game.vercel.app").rstrip("/")
IFC_MINT = os.environ.get("IFC_MINT_ADDRESS", "C8KsvkMBuqmvX416MWTJGKW9S9MpKiUjmpnj1fhzpump")
TREASURY_KEY = os.environ.get("TREASURY_PRIVATE_KEY", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Token prices
IFC_PRICE_USD = None
SOL_PRICE_USD = None
_last_price_fetch = 0
_price_cache_seconds = 300

def get_token_price():
    global IFC_PRICE_USD, _last_price_fetch
    now = time.time()
    if IFC_PRICE_USD is not None and (now - _last_price_fetch) < _price_cache_seconds:
        return IFC_PRICE_USD
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{IFC_MINT}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        pairs = data.get("pairs", [])
        if pairs:
            price = float(pairs[0].get("priceUsd", 0))
            if price > 0:
                IFC_PRICE_USD = price
                _last_price_fetch = now
                logger.info("DexScreener IFC price: $%.10f", IFC_PRICE_USD)
                return IFC_PRICE_USD
    except Exception as e:
        logger.error("DexScreener IFC failed: %s", e)
    if IFC_PRICE_USD is None:
        IFC_PRICE_USD = 0.00000329
        logger.info("Using IFC fallback price: $%.10f", IFC_PRICE_USD)
    return IFC_PRICE_USD

def get_sol_price():
    global SOL_PRICE_USD, _last_price_fetch
    now = time.time()
    if SOL_PRICE_USD is not None and (now - _last_price_fetch) < _price_cache_seconds:
        return SOL_PRICE_USD
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        price = float(data["solana"]["usd"])
        if price > 0:
            SOL_PRICE_USD = price
            _last_price_fetch = now
            logger.info("CoinGecko SOL price: $%.2f", SOL_PRICE_USD)
            return SOL_PRICE_USD
    except Exception as e:
        logger.error("SOL price fetch failed: %s", e)
    if SOL_PRICE_USD is None:
        SOL_PRICE_USD = 86.24
        logger.info("Using SOL fallback price: $%.2f", SOL_PRICE_USD)
    return SOL_PRICE_USD

# ========== CLAIM CAPS ==========
FREE_CAP = 10000
HOLDER_CAP = 150000
DAILY_BONUS_AMOUNT = 500

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ========== DATABASE PERSISTENCE ==========
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ========== SQLITE LEADERBOARD + DAILY CLAIMS ==========
DB_PATH = os.path.join(DATA_DIR, "leaderboard.db")

def _init_sqlite():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS high_scores (wallet TEXT PRIMARY KEY, best_distance INTEGER NOT NULL, username TEXT, last_updated REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS daily_claims (uid TEXT PRIMARY KEY, first_claim INTEGER NOT NULL, total INTEGER NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS wallet_daily_claims (wallet TEXT PRIMARY KEY, first_claim INTEGER NOT NULL, total INTEGER NOT NULL)")
    conn.commit()
    conn.close()

_init_sqlite()

def _save_score_supabase(wallet, distance, username):
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase not configured: URL=%s KEY=%s", bool(SUPABASE_URL), bool(SUPABASE_KEY))
        return
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        check_url = f"{SUPABASE_URL}/rest/v1/high_scores?wallet=eq.{wallet}&select=best_distance&limit=1"
        check_resp = requests.get(check_url, headers=headers, timeout=10)
        current_best = 0
        if check_resp.status_code == 200:
            data = check_resp.json()
            if data and len(data) > 0:
                current_best = data[0].get("best_distance", 0)
        if distance <= current_best:
            logger.info("Score %s not higher than current best %s for %s... skipping", distance, current_best, wallet[:6])
            return
        post_headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation"
        }
        payload = {
            "wallet": wallet,
            "best_distance": distance,
            "username": username,
            "last_updated": int(time.time())
        }
        url = f"{SUPABASE_URL}/rest/v1/high_scores"
        logger.info("Supabase POST new best: %s -> %s (was %s)", wallet[:6], distance, current_best)
        resp = requests.post(url, headers=post_headers, json=payload, timeout=10)
        logger.info("Supabase save response: HTTP %s body=%s", resp.status_code, resp.text[:200])
        if resp.status_code not in (200, 201):
            logger.warning("Supabase save score FAILED HTTP %s: %s", resp.status_code, resp.text[:300])
    except Exception as e:
        logger.error("Supabase save score error: %s", e)

def _get_leaderboard_supabase(limit=10):
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase leaderboard skipped: not configured")
        return []
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json"
        }
        url = f"{SUPABASE_URL}/rest/v1/leaderboard_best?order=best_distance.desc&limit={limit}"
        logger.info("Supabase GET leaderboard: %s", url)
        resp = requests.get(url, headers=headers, timeout=10)
        logger.info("Supabase leaderboard response: HTTP %s body=%s", resp.status_code, resp.text[:300])
        if resp.status_code == 200:
            data = resp.json()
            logger.info("Supabase leaderboard parsed %s rows", len(data))
            return [(r["wallet"], r["best_distance"], r.get("username", "Anonymous")) for r in data]
        else:
            logger.warning("Supabase leaderboard FAILED HTTP %s: %s", resp.status_code, resp.text[:300])
            return []
    except Exception as e:
        logger.error("Supabase leaderboard error: %s", e)
        return []

def _get_best_supabase(wallet):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json"
        }
        url = f"{SUPABASE_URL}/rest/v1/leaderboard_best?wallet=eq.{wallet}&limit=1"
        logger.info("Supabase GET best: %s", url)
        resp = requests.get(url, headers=headers, timeout=10)
        logger.info("Supabase best response: HTTP %s body=%s", resp.status_code, resp.text[:200])
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0]["best_distance"]
        return 0
    except Exception as e:
        logger.error("Supabase best error: %s", e)
        return 0

def _count_players_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json"
        }
        url = f"{SUPABASE_URL}/rest/v1/leaderboard_best?select=wallet"
        logger.info("Supabase GET count: %s", url)
        resp = requests.get(url, headers=headers, timeout=10)
        logger.info("Supabase count response: HTTP %s body=%s", resp.status_code, resp.text[:200])
        if resp.status_code == 200:
            data = resp.json()
            return len(data)
        return 0
    except Exception as e:
        logger.error("Supabase count error: %s", e)
        return 0

USER_DB_FILE = os.path.join(DATA_DIR, "users.json")
EARNINGS_DB_FILE = os.path.join(DATA_DIR, "earnings.json")
ESCROW_DB_FILE = os.path.join(DATA_DIR, "escrow.json")
DAILY_DB_FILE = os.path.join(DATA_DIR, "daily.json")
WALLET_DAILY_DB_FILE = os.path.join(DATA_DIR, "wallet_daily.json")
HIGHSCORE_DB_FILE = os.path.join(DATA_DIR, "highscores.json")
DAILY_CLAIMED_DB_FILE = os.path.join(DATA_DIR, "daily_claimed.json")

def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error("Load %s error: %s", path, e)
    return default

def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Save %s error: %s", path, e)

user_db = _load_json(USER_DB_FILE, {})
earnings_db = _load_json(EARNINGS_DB_FILE, {})
escrow_db = _load_json(ESCROW_DB_FILE, {})
daily_bonus_db = _load_json(DAILY_DB_FILE, {})
wallet_daily_db = _load_json(WALLET_DAILY_DB_FILE, {})
high_scores_db = _load_json(HIGHSCORE_DB_FILE, {})
daily_claimed_db = _load_json(DAILY_CLAIMED_DB_FILE, {})

logger.info("Loaded DBs: users=%s earnings=%s scores=%s", len(user_db), len(earnings_db), len(high_scores_db))

# ========== WALLET UNIQUENESS ==========
def _get_uid_by_wallet(wallet_address):
    w = wallet_address.strip()
    for uid, data in user_db.items():
        if data.get("wallet", "").strip() == w:
            return uid
    return None

def _can_set_wallet(uid, wallet_address):
    existing_uid = _get_uid_by_wallet(wallet_address)
    if existing_uid is not None and existing_uid != str(uid):
        return False, existing_uid
    return True, None

# ========== HOLDER STATUS ==========
_holder_cache = {}
_holder_cache_ttl = 300

def get_required_infinite_for_holder():
    sol_price = get_sol_price()
    infinite_price = get_token_price()
    if sol_price <= 0 or infinite_price <= 0:
        return None
    usd_needed = 0.1 * sol_price
    tokens_needed = usd_needed / infinite_price
    logger.info("Holder req: $%.2f / $%.10f = %.0f INFINITE", usd_needed, infinite_price, tokens_needed)
    return tokens_needed

def is_holder(wallet_address):
    global _holder_cache
    if not wallet_address:
        return False
    now = time.time()
    cached = _holder_cache.get(wallet_address)
    if cached and (now - cached[1]) < _holder_cache_ttl:
        return cached[0]
    try:
        balance = get_wallet_balance(wallet_address)
        required = get_required_infinite_for_holder()
        if required is None:
            return False
        result = balance >= required
        _holder_cache[wallet_address] = (result, now)
        logger.info("Holder check %s...: %.2f / %.0f INFINITE -> %s", wallet_address[:4], balance, required, result)
        return result
    except Exception as e:
        logger.error("Holder check error: %s", e)
        return False

# ========== SOLANA SETUP ==========
escrow_ready = False
solana_client = None
mint_pubkey = None
treasury_kp = None
treasury_ata = None
create_associated_token_account_idempotent = None
get_associated_token_address = None
transfer_checked = None
TransferCheckedParams = None
TOKEN_PROGRAM_ID = None
ASSOCIATED_TOKEN_PROGRAM_ID = None

def _setup_solana():
    global escrow_ready, solana_client, mint_pubkey, treasury_kp, treasury_ata
    global create_associated_token_account_idempotent, get_associated_token_address
    global transfer_checked, TransferCheckedParams
    global TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID

    try:
        from solders.pubkey import Pubkey
        from solders.keypair import Keypair
    except ImportError as e:
        logger.error("solders not installed: %s", e)
        return

    try:
        from solana.rpc.api import Client
    except ImportError as e:
        logger.error("solana.rpc not found: %s", e)
        return

    DEFAULT_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

    solana_client = Client(SOLANA_RPC)
    mint_pubkey = Pubkey.from_string(IFC_MINT)

    try:
        mint_info = solana_client.get_account_info(mint_pubkey)
        if hasattr(mint_info, 'value') and mint_info.value:
            mint_owner = str(mint_info.value.owner)
        else:
            mint_owner = mint_info.get('result', {}).get('value', {}).get('owner')

        if mint_owner == str(TOKEN_2022_PROGRAM):
            TOKEN_PROGRAM_ID = TOKEN_2022_PROGRAM
            logger.info("Detected Token-2022 program for mint")
        else:
            TOKEN_PROGRAM_ID = DEFAULT_TOKEN_PROGRAM
            logger.info("Detected standard SPL Token program for mint: %s", mint_owner)
    except Exception as e:
        logger.warning("Mint owner detection failed, defaulting to standard: %s", e)
        TOKEN_PROGRAM_ID = DEFAULT_TOKEN_PROGRAM

    try:
        from spl.token.instructions import (
            create_associated_token_account_idempotent as _cati,
            get_associated_token_address as _gata,
            transfer_checked as _tc,
            TransferCheckedParams as _tcp,
        )
        create_associated_token_account_idempotent = _cati
        get_associated_token_address = _gata
        transfer_checked = _tc
        TransferCheckedParams = _tcp
        logger.info("SPL library loaded")
    except ImportError:
        def _gata_fallback(owner, mint):
            seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
            result = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
            return result[0]
        get_associated_token_address = _gata_fallback
        create_associated_token_account_idempotent = None
        transfer_checked = None
        TransferCheckedParams = None
        logger.info("Using SPL fallback")

    escrow_ready = bool(TREASURY_KEY and get_associated_token_address)
    if escrow_ready:
        try:
            treasury_kp = Keypair.from_base58_string(TREASURY_KEY)
            _ta = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
            treasury_ata = _ta if isinstance(_ta, Pubkey) else Pubkey.from_string(str(_ta))
            logger.info("ESCROW LIVE - Treasury: %s, ATA: %s", treasury_kp.pubkey(), treasury_ata)
        except Exception as e:
            logger.error("Solana init failed: %s", e)
            escrow_ready = False
    else:
        logger.warning("ESCROW DEMO mode")

_setup_solana()

# ========== WALLET-BASED DAILY CLAIM TRACKING ==========
def get_wallet_daily_claimed(wallet):
    if not wallet:
        return 0, 0, True
    w = wallet.strip()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT first_claim, total FROM wallet_daily_claims WHERE wallet = ?", (w,))
        row = c.fetchone()
        conn.close()
        if not row:
            return 0, 0, True
        first_claim, total = row
        if first_claim == 0:
            return 0, 0, True
        hours_since = (time.time() * 1000 - first_claim) / (1000 * 60 * 60)
        if hours_since >= 24:
            return 0, 0, True
        return total, first_claim, False
    except Exception as e:
        logger.error("get_wallet_daily_claimed SQLite error: %s", e)
        return 0, 0, True

def add_wallet_daily_claimed(wallet, amount):
    if not wallet:
        return
    w = wallet.strip()
    now_ms = int(time.time() * 1000)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT first_claim, total FROM wallet_daily_claims WHERE wallet = ?", (w,))
        row = c.fetchone()
        if row:
            first_claim, total = row
            hours_since = (now_ms - first_claim) / (1000 * 60 * 60)
            if hours_since >= 24:
                c.execute("REPLACE INTO wallet_daily_claims (wallet, first_claim, total) VALUES (?, ?, ?)", (w, now_ms, amount))
            else:
                c.execute("UPDATE wallet_daily_claims SET total = total + ? WHERE wallet = ?", (amount, w))
        else:
            c.execute("INSERT INTO wallet_daily_claims (wallet, first_claim, total) VALUES (?, ?, ?)", (w, now_ms, amount))
        conn.commit()
        conn.close()
        logger.info("SQLite wallet daily claim added: wallet=%s... amount=%s", w[:6], amount)
    except Exception as e:
        logger.error("add_wallet_daily_claimed SQLite error: %s", e)

def get_daily_cap(wallet_address):
    if wallet_address and is_holder(wallet_address):
        return HOLDER_CAP
    return FREE_CAP

def get_wallet_daily_remaining(wallet):
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_wallet_daily_claimed(wallet)
    return max(0, cap - claimed)

def get_wallet_daily_reset_time(wallet):
    _, first_claim, _ = get_wallet_daily_claimed(wallet)
    if first_claim == 0:
        return 0
    reset_ms = first_claim + (24 * 60 * 60 * 1000)
    remaining_ms = reset_ms - int(time.time() * 1000)
    return max(0, remaining_ms)

def get_wallet_daily_reset_text(wallet):
    ms = get_wallet_daily_reset_time(wallet)
    if ms <= 0:
        return "Resets now"
    hours = int(ms / (1000 * 60 * 60))
    mins = int((ms % (1000 * 60 * 60)) / (1000 * 60))
    return f"{hours}h {mins}m"

# ========== DAILY BONUS (WALLET-BASED) ==========
def is_wallet_daily_bonus_available(wallet):
    if not wallet:
        return True
    last = wallet_daily_db.get(wallet, 0)
    if not last:
        return True
    hours_since = (time.time() * 1000 - last) / (1000 * 60 * 60)
    return hours_since >= 24

def get_wallet_daily_bonus_remaining_text(wallet):
    if not wallet:
        return "Available now!"
    last = wallet_daily_db.get(wallet, 0)
    if not last:
        return "Available now!"
    elapsed_ms = time.time() * 1000 - last
    remaining_ms = (24 * 60 * 60 * 1000) - elapsed_ms
    if remaining_ms <= 0:
        return "Available now!"
    hours = int(remaining_ms / (1000 * 60 * 60))
    mins = int((remaining_ms % (1000 * 60 * 60)) / (1000 * 60))
    return f"{hours}h {mins}m"

# ========== LEGACY UID-BASED FUNCTIONS ==========
def get_daily_claimed(uid):
    uid = str(uid)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT first_claim, total FROM daily_claims WHERE uid = ?", (uid,))
        row = c.fetchone()
        conn.close()
        if not row:
            return 0, 0, True
        first_claim, total = row
        if first_claim == 0:
            return 0, 0, True
        hours_since = (time.time() * 1000 - first_claim) / (1000 * 60 * 60)
        if hours_since >= 24:
            return 0, 0, True
        return total, first_claim, False
    except Exception as e:
        logger.error("get_daily_claimed SQLite error: %s", e)
        return 0, 0, True

def add_daily_claimed(uid, amount):
    uid = str(uid)
    now_ms = int(time.time() * 1000)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT first_claim, total FROM daily_claims WHERE uid = ?", (uid,))
        row = c.fetchone()
        if row:
            first_claim, total = row
            hours_since = (now_ms - first_claim) / (1000 * 60 * 60)
            if hours_since >= 24:
                c.execute("REPLACE INTO daily_claims (uid, first_claim, total) VALUES (?, ?, ?)", (uid, now_ms, amount))
            else:
                c.execute("UPDATE daily_claims SET total = total + ? WHERE uid = ?", (amount, uid))
        else:
            c.execute("INSERT INTO daily_claims (uid, first_claim, total) VALUES (?, ?, ?)", (uid, now_ms, amount))
        conn.commit()
        conn.close()
        logger.info("SQLite daily claim added: uid=%s amount=%s", uid, amount)
    except Exception as e:
        logger.error("add_daily_claimed SQLite error: %s", e)

def get_daily_remaining(uid, wallet_address):
    cap = get_daily_cap(wallet_address)
    claimed, _, _ = get_daily_claimed(uid)
    return max(0, cap - claimed)

def get_daily_reset_time(uid):
    _, first_claim, _ = get_daily_claimed(uid)
    if first_claim == 0:
        return 0
    reset_ms = first_claim + (24 * 60 * 60 * 1000)
    remaining_ms = reset_ms - int(time.time() * 1000)
    return max(0, remaining_ms)

def get_daily_reset_text(uid):
    ms = get_daily_reset_time(uid)
    if ms <= 0:
        return "Resets now"
    hours = int(ms / (1000 * 60 * 60))
    mins = int((ms % (1000 * 60 * 60)) / (1000 * 60))
    return f"{hours}h {mins}m"

# ========== DAILY COOLDOWN (legacy) ==========
def is_daily_available(uid):
    last = daily_bonus_db.get(str(uid), 0)
    if not last:
        return True
    hours_since = (time.time() * 1000 - last) / (1000 * 60 * 60)
    return hours_since >= 24

def is_daily_available_by_wallet(wallet):
    if not wallet:
        return True
    last = wallet_daily_db.get(wallet, 0)
    if not last:
        return True
    hours_since = (time.time() * 1000 - last) / (1000 * 60 * 60)
    return hours_since >= 24

def get_daily_remaining_text(uid):
    last = daily_bonus_db.get(str(uid), 0)
    if not last:
        return "Available now!"
    elapsed_ms = time.time() * 1000 - last
    remaining_ms = (24 * 60 * 60 * 1000) - elapsed_ms
    if remaining_ms <= 0:
        return "Available now!"
    hours = int(remaining_ms / (1000 * 60 * 60))
    mins = int((remaining_ms % (1000 * 60 * 60)) / (1000 * 60))
    return f"{hours}h {mins}m"

# ========== DATABASE ==========
def get_db(user_id):
    uid = str(user_id)
    user_db.setdefault(uid, {})
    earnings_db.setdefault(uid, {"total_earned": 0, "unclaimed": 0, "total_claimed": 0})
    escrow_db.setdefault(uid, {"hold_time": 0, "amount": 0, "released": True})
    daily_bonus_db.setdefault(uid, 0)
    return user_db[uid], earnings_db[uid], escrow_db[uid], daily_bonus_db[uid]

# ========== SOLANA FUNCTIONS ==========
def get_wallet_balance(wallet_address):
    if not wallet_address or len(wallet_address) < 32:
        return 0.0
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet_address,
            {"mint": IFC_MINT},
            {"encoding": "jsonParsed"}
        ]
    }
    endpoints = [
        SOLANA_RPC,
        "https://solana-rpc.publicnode.com",
        "https://rpc.ankr.com/solana",
        "https://api.mainnet-beta.solana.com"
    ]
    for url in endpoints:
        try:
            resp = requests.post(url, json=payload, timeout=15, headers={"Content-Type": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                if 'result' in data and 'value' in data['result']:
                    accounts = data['result']['value']
                    if accounts:
                        total = 0.0
                        for acc in accounts:
                            try:
                                info = acc['account']['data']['parsed']['info']
                                ui_amount = info['tokenAmount']['uiAmount']
                                if ui_amount is not None:
                                    total += float(ui_amount)
                            except Exception:
                                pass
                        logger.info("Wallet scan %s...: %.4f INFINITE (via %s)", wallet_address[:6], total, url.split('/')[2])
                        return total
                    logger.info("Wallet scan %s...: 0 INFINITE (empty)", wallet_address[:6])
                    return 0.0
                elif 'error' in data:
                    logger.warning("RPC error from %s: %s", url.split('/')[2], data['error'])
        except Exception as e:
            logger.warning("RPC fail %s: %s", url.split('/')[2], str(e)[:80])
    if escrow_ready and get_associated_token_address and solana_client:
        try:
            from solders.pubkey import Pubkey
            recipient_pk = Pubkey.from_string(wallet_address)
            recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
            if not isinstance(recipient_ata, Pubkey):
                recipient_ata = Pubkey.from_string(str(recipient_ata))
            resp = solana_client.get_token_account_balance(recipient_ata)
            if hasattr(resp, 'value') and resp.value:
                bal = float(resp.value.ui_amount or 0)
                logger.info("Wallet scan %s...: %.4f INFINITE (via client)", wallet_address[:6], bal)
                return bal
        except Exception as e:
            logger.error("Client balance fail: %s", e)
    logger.error("Wallet scan FAILED for %s", wallet_address[:6])
    return 0.0

def get_treasury_balance():
    if not escrow_ready or not treasury_ata or not solana_client:
        return 0.0
    try:
        resp = solana_client.get_token_account_balance(treasury_ata)
        if hasattr(resp, 'value') and resp.value:
            return float(resp.value.ui_amount or 0)
    except Exception as e:
        logger.error("Treasury scan failed: %s", e)
    return 0.0

def has_minimum_balance(wallet_address):
    balance = get_wallet_balance(wallet_address)
    usd_value = balance * get_token_price()
    required = get_required_infinite_for_holder()
    has_min = False
    if required and balance >= required:
        has_min = True
    return {"has_min": has_min, "balance": balance, "usd_value": usd_value, "required": required}

# ========== REAL TOKEN TRANSFER ==========
def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {"success": False, "tx": None, "message": "Treasury not ready"}
    recipient_bal = get_wallet_balance(recipient)
    logger.info("Pre-send scan: recipient %s... has %.4f INFINITE", recipient[:6], recipient_bal)
    treasury_bal = get_treasury_balance()
    logger.info("Pre-send scan: treasury has %.4f INFINITE", treasury_bal)
    if treasury_bal < amount:
        logger.error("Treasury too low: %.4f < %.4f", treasury_bal, amount)
        return {"success": False, "tx": None, "message": f"Treasury low ({treasury_bal:.2f} INFINITE)"}
    try:
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import Transaction as SoldersTx
        from solders.hash import Hash
        amount_raw = int(amount * 1_000_000)
        recipient_pk = Pubkey.from_string(recipient.strip())
        seeds = [bytes(recipient_pk), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)]
        recipient_ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
        acct_info = solana_client.get_account_info(recipient_ata)
        if hasattr(acct_info, 'value'):
            ata_exists = acct_info.value is not None
        else:
            ata_exists = acct_info.get('result', {}).get('value') is not None
        instructions = []
        if not ata_exists:
            sys_prog = Pubkey.from_string("11111111111111111111111111111111")
            # CreateIdempotent instruction for Associated Token Account program = data [1]
            create_ix = Instruction(
                program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
                accounts=[
                    AccountMeta(pubkey=treasury_kp.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=recipient_ata, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=recipient_pk, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=sys_prog, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                ],
                data=bytes([1])  # CreateIdempotent discriminator
            )
            instructions.append(create_ix)
            logger.info("ATA creation instruction added for %s...", recipient[:6])
        ix_data = struct.pack("<BQB", 12, amount_raw, 6)
        transfer_ix = Instruction(
            program_id=TOKEN_PROGRAM_ID,
            accounts=[
                AccountMeta(pubkey=treasury_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(pubkey=recipient_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=treasury_kp.pubkey(), is_signer=True, is_writable=False),
            ],
            data=ix_data
        )
        instructions.append(transfer_ix)
        bh_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}]
        }, timeout=10).json()
        blockhash = Hash.from_string(bh_resp['result']['value']['blockhash'])
        tx = SoldersTx.new_signed_with_payer(
            instructions,
            treasury_kp.pubkey(),
            [treasury_kp],
            blockhash
        )
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')
        send_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 2,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=15).json()
        if 'result' in send_resp:
            logger.info("Transfer SUCCESS: %s INFINITE to %s... tx=%s", amount, recipient[:6], send_resp['result'])
            return {"success": True, "tx": send_resp['result'], "message": f"Sent {amount:,} INFINITE", "recipient_balance": recipient_bal}
        else:
            err = send_resp.get('error', 'unknown')
            logger.error("RPC send error: %s", err)
            return {"success": False, "tx": None, "message": f"RPC error: {err}", "recipient_balance": recipient_bal}
    except Exception as e:
        logger.error("Transfer error: %s", e)
        return {"success": False, "tx": None, "message": str(e), "recipient_balance": recipient_bal}

# ========== TELEGRAM HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    _, e, esc, _ = get_db(uid)
    wallet = user_db.get(uid, {}).get("wallet")
    wallet_text = f"`{wallet[:4]}...{wallet[-4:]}`" if wallet else "*Not connected*"
    holder_status = "\U0001F48E HOLDER" if (wallet and is_holder(wallet)) else "\U0001F464 Free"
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_wallet_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    status_lines = [
        "*Infinitecoin Jumper*", "_Collect coins. Avoid viruses. Earn INFINITE._", "",
        f"Status: {holder_status}",
        f"Wallet: {wallet_text}",
        f"Earned: {e['total_earned']:,} INFINITE",
        f"Unclaimed: {e['unclaimed']:,} INFINITE",
        f"Claimed today: {claimed:,} / {cap:,} INFINITE",
    ]
    status_lines.extend(["", "/play - Launch game", "/wallet - Connect Phantom (1x only)",
        "/balance - Check INFINITE & holder status", "/claim - Claim INFINITE",
        "/daily - Daily bonus (500 INFINITE)", "/help - How to play"])
    await update.message.reply_text("\n".join(status_lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Play Game", url=f"{GAME_URL}?user_id={uid}")],
            [InlineKeyboardButton("Connect Wallet", callback_data="wallet")],
        ]))

async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text("Launch Infinitecoin Jumper:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Game", url=f"{GAME_URL}?user_id={uid}")]]))

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    existing = get_db(uid)[0].get("wallet")
    if existing:
        holder = is_holder(existing)
        tier = "\U0001F48E HOLDER (150K/day)" if holder else "\U0001F464 Free (10K/day)"
        await update.message.reply_text(
            f"Wallet locked: `{existing[:4]}...{existing[-4:]}`\n{tier}\nUse /balance or /claim.", parse_mode="Markdown")
        return
    phantom_url = f"https://phantom.app/ul/v1/connect?app_url={BASE_URL}&redirect_link={BASE_URL}/wallet-callback?user_id={uid}"
    await update.message.reply_text("*Connect Phantom* (ONE TIME ONLY)\n1. Open Phantom\n2. Approve\n3. Return\n\nOr: `/setwallet ADDRESS`",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Phantom", url=phantom_url)]]))

async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    existing = get_db(uid)[0].get("wallet")
    if existing:
        await update.message.reply_text(f"Wallet already locked: `{existing[:4]}...{existing[-4:]}`\nCannot change.", parse_mode="Markdown")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setwallet ADDRESS`", parse_mode="Markdown"); return
    wallet = context.args[0].strip()
    if len(wallet) < 32:
        await update.message.reply_text("Invalid address."); return
    can_set, _ = _can_set_wallet(uid, wallet)
    if not can_set:
        await update.message.reply_text("Wallet already linked to another account!"); return
    user_db.setdefault(uid, {})["wallet"] = wallet
    _save_json(USER_DB_FILE, user_db)
    await update.message.reply_text(f"Wallet locked! Now /claim or /balance.", parse_mode="Markdown")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    lines = ["*Your INFINITE Status*"]
    if wallet:
        lines.append(f"Wallet: `{wallet[:4]}...{wallet[-4:]}`")
        bal = has_minimum_balance(wallet)
        holder = is_holder(wallet)
        req = get_required_infinite_for_holder()
        cap = get_daily_cap(wallet)
        claimed, _, _ = get_wallet_daily_claimed(wallet)
        remaining = max(0, cap - claimed)
        lines.append(f"Balance: {bal['balance']:,.2f} INFINITE (${bal['usd_value']:.6f})")
        if holder:
            lines.append("Tier: \U0001F48E *HOLDER* — 150K/day claim cap")
        else:
            lines.append("Tier: \U0001F464 *Free* — 10K/day claim cap")
            if req:
                lines.append(f"Hold {req:,.0f} INFINITE to unlock 150K/day")
        lines.append(f"Claimed today: {claimed:,} / {cap:,} INFINITE")
        lines.append(f"Remaining today: {remaining:,} INFINITE")
        lines.append("Status: *Ready to claim*")
    else:
        lines.append("Wallet: *Not connected*")
    lines.extend([f"Earned: {e['total_earned']:,} INFINITE", f"Unclaimed: {e['unclaimed']:,} INFINITE",
        f"Claimed: {e['total_claimed']:,} INFINITE", f"\nDaily: {get_daily_remaining_text(uid)}", "\n/play to earn!"])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    if not wallet:
        await update.message.reply_text("No wallet! Use /wallet first."); return
    if e['unclaimed'] <= 0:
        await update.message.reply_text("No INFINITE to claim. /play to earn!"); return
    cap = get_daily_cap(wallet)
    claimed, _, reset = get_wallet_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    if remaining <= 0:
        req = get_required_infinite_for_holder()
        reset_text = get_wallet_daily_reset_text(wallet)
        msg = f"Cap reached: {claimed:,}/{cap:,} INFINITE today\nResets in: {reset_text}"
        if not is_holder(wallet) and req:
            msg += f"\n\nHold {req:,.0f} INFINITE (0.1 SOL) to unlock 150K/day"
        await update.message.reply_text(msg); return
    claimable = min(e['unclaimed'], remaining)
    if claimable <= 0:
        await update.message.reply_text("Nothing to claim."); return
    wallet_balance = get_wallet_balance(wallet)
    result = transfer_ifc(wallet, claimable)
    if result['success']:
        e['total_claimed'] += claimable
        e['unclaimed'] -= claimable
        add_wallet_daily_claimed(wallet, claimable)
        if wallet:
            wallet_daily_db[wallet] = int(time.time() * 1000)
            _save_json(WALLET_DAILY_DB_FILE, wallet_daily_db)
        _save_json(EARNINGS_DB_FILE, earnings_db)
    tier = "HOLDER" if is_holder(wallet) else "Free"
    status = "\u2705 Claimed" if result['success'] else "\u274c Failed"
    msg = (
        f"{status} ({tier})\n"
        f"Amount: {claimable:,} INFINITE\n"
        f"Your wallet balance: {wallet_balance:,.2f} INFINITE\n"
        f"Tx: `{result.get('tx', 'N/A')}`\n"
        f"Note: {result['message']}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    
    # WALLET-FIRST CHECK
    if wallet and not is_wallet_daily_bonus_available(wallet):
        remaining = get_wallet_daily_bonus_remaining_text(wallet)
        await update.message.reply_text(f"Daily bonus already claimed for this wallet.\nNext: {remaining}")
        return
    
    # Backup uid check only if no wallet
    if not wallet and not is_daily_available(uid):
        await update.message.reply_text(f"Cooldown. Next: {get_daily_remaining_text(uid)}")
        return

    daily_bonus_db[uid] = int(time.time() * 1000)
    _save_json(DAILY_DB_FILE, daily_bonus_db)
    if wallet:
        wallet_daily_db[wallet] = int(time.time() * 1000)
        _save_json(WALLET_DAILY_DB_FILE, wallet_daily_db)

    wallet_balance = get_wallet_balance(wallet) if wallet else 0
    result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    if result.get('success'):
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['total_claimed'] += DAILY_BONUS_AMOUNT
        _save_json(EARNINGS_DB_FILE, earnings_db)
        await update.message.reply_text(
            f"DAILY BONUS! +{DAILY_BONUS_AMOUNT:,} INFINITE!\n"
            f"Wallet balance: {wallet_balance:,.2f} INFINITE\n"
            f"Tx: `{result.get('tx')}`",
            parse_mode="Markdown"
        )
    else:
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['unclaimed'] += DAILY_BONUS_AMOUNT
        _save_json(EARNINGS_DB_FILE, earnings_db)
        await update.message.reply_text(
            f"Bonus added to unclaimed! ({result.get('message', '')})\n"
            f"Wallet balance: {wallet_balance:,.2f} INFINITE"
        )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = get_required_infinite_for_holder()
    req_text = f"Hold {req:,.0f} INFINITE to unlock 150K/day" if req else ""
    await update.message.reply_text(
        f"*How to Play*\nArrows: Move | Space: Jump\n\n*Claims*\n"
        f"\U0001F464 Free: 10K/day total (claim multiple times)\n"
        f"\U0001F48E Holders: 150K/day total (claim multiple times)\n"
        f"{req_text}\n"
        f"- Daily: {DAILY_BONUS_AMOUNT} FREE INFINITE/24h\n\n"
        f"/play /wallet /claim /daily /balance",
        parse_mode="Markdown")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "wallet": await cmd_wallet(update, context)

# ========== FLASK ROUTES ==========
@app.route("/")
def index():
    return jsonify({"bot": "Infinitecoin Jumper", "escrow": "LIVE" if escrow_ready else "DEMO", "users": len(user_db)})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "users": len(user_db), "escrow_ready": escrow_ready})

@app.route("/api/status")
def api_status():
    req = get_required_infinite_for_holder()
    sol_price = get_sol_price()
    return jsonify({
        "free_cap": FREE_CAP,
        "holder_cap": HOLDER_CAP,
        "holder_required_infinite": req,
        "sol_price": sol_price,
        "infinite_price": get_token_price(),
        "mint_address": IFC_MINT
    })

@app.route("/api/user/<uid>", methods=["GET"])
def api_get_user(uid):
    wallet = user_db.get(str(uid), {}).get("wallet", "")
    _, e, _, _ = get_db(uid)
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_wallet_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    result = {
        "telegram_user_id": uid,
        "wallet_address": wallet,
        "earned": e['total_earned'],
        "unclaimed": e['unclaimed'],
        "claimed": e['total_claimed'],
        "daily_cap": cap,
        "daily_claimed": claimed,
        "daily_remaining": remaining,
        "daily_reset_ms": get_wallet_daily_reset_time(wallet),
        "is_holder": is_holder(wallet) if wallet else False
    }
    logger.info("API /api/user/%s: wallet=%s...", uid, wallet[:6] if wallet else "none")
    return jsonify(result)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if telegram_app is None:
            return jsonify({"ok": False, "error": "Bot not ready"}), 503
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), _bot_loop)
        future.result(timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Webhook: %s", e)
        return jsonify({"ok": False}), 200

@app.route("/wallet-callback")
def wallet_callback():
    uid = request.args.get("user_id", "")
    wallet = request.args.get("phantom_wallet") or request.args.get("wallet") or request.args.get("address") or ""
    if wallet and uid:
        existing = user_db.get(uid, {}).get("wallet")
        if existing:
            return redirect(f"{GAME_URL}?user_id={uid}&wallet={existing}")
        can_set, _ = _can_set_wallet(uid, wallet)
        if not can_set:
            return '<h1>Wallet Already Linked</h1><p>This wallet is connected to another account.</p>'
        user_db.setdefault(uid, {})["wallet"] = wallet
        _save_json(USER_DB_FILE, user_db)
        return redirect(f"{GAME_URL}?user_id={uid}&wallet={wallet}")
    return '<h1>Connect Wallet</h1><p>Use /setwallet in the bot instead.</p>'

@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    logger.info("API /api/wallet uid=%s wallet=%s...", uid, wallet[:6] if wallet else "none")
    if not wallet or not uid or len(wallet) < 32:
        return jsonify({"error": "Invalid"}), 400
    existing_wallet = user_db.get(uid, {}).get("wallet")
    if existing_wallet:
        if existing_wallet.strip() == wallet:
            return jsonify({"success": True, "message": "Already connected"})
        return jsonify({"error": "Wallet locked. Cannot change."}), 409
    can_set, _ = _can_set_wallet(uid, wallet)
    if not can_set:
        return jsonify({"error": "Wallet already linked to another account"}), 409
    user_db.setdefault(uid, {})["wallet"] = wallet
    _save_json(USER_DB_FILE, user_db)
    return jsonify({"success": True})

@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    amount = int(data.get("amount", 0))
    logger.info("API /api/earnings uid=%s amount=%s", uid, amount)
    if not uid:
        logger.warning("API /api/earnings rejected: missing uid")
        return jsonify({"error": "Missing user_id"}), 400
    _, e, _, _ = get_db(uid)
    e["total_earned"] += amount
    e["unclaimed"] += amount
    _save_json(EARNINGS_DB_FILE, earnings_db)
    logger.info("API /api/earnings saved: uid=%s total=%s unclaimed=%s", uid, e["total_earned"], e["unclaimed"])
    return jsonify({"success": True, "unclaimed": e["unclaimed"]})

@app.route("/api/claim", methods=["POST"])
def api_claim():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    wallet = data.get("wallet_address", "").strip()
    logger.info("API /api/claim uid=%s wallet=%s...", uid, wallet[:6] if wallet else "none")
    if not uid or not wallet:
        logger.warning("API /api/claim rejected: missing uid or wallet")
        return jsonify({"error": "Invalid"}), 400
    _, e, _, _ = get_db(uid)
    logger.info("API /api/claim state: unclaimed=%s total_claimed=%s", e['unclaimed'], e['total_claimed'])
    if e['unclaimed'] <= 0:
        logger.info("API /api/claim rejected: zero unclaimed")
        return jsonify({"success": False, "message": "No IFC to claim"})
    cap = get_daily_cap(wallet)
    claimed, _, reset = get_wallet_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    if remaining <= 0:
        reset_text = get_wallet_daily_reset_text(wallet)
        req = get_required_infinite_for_holder()
        msg = f"Cap reached: {claimed:,}/{cap:,} INFINITE today. Resets in {reset_text}."
        if not is_holder(wallet) and req:
            msg += f" Hold {req:,.0f} INFINITE (0.1 SOL) to unlock 150K/day."
        logger.info("API /api/claim rejected: cap reached %s/%s", claimed, cap)
        return jsonify({"success": False, "message": msg, "cap_reached": True, "daily_claimed": claimed, "daily_cap": cap, "resets_in": reset_text})
    claimable = min(e['unclaimed'], remaining)
    logger.info("API /api/claim claimable=%s (cap=%s claimed=%s remaining=%s)", claimable, cap, claimed, remaining)
    if claimable <= 0:
        return jsonify({"success": False, "message": "Nothing to claim"})
    wallet_balance = get_wallet_balance(wallet)
    logger.info("API /api/claim pre-scan: %s... balance=%.4f", wallet[:6], wallet_balance)
    result = transfer_ifc(wallet, claimable)
    logger.info("API /api/claim transfer result: %s", result.get('success'))
    if result.get('success'):
        e['total_claimed'] += claimable
        e['unclaimed'] -= claimable
        add_wallet_daily_claimed(wallet, claimable)
        if wallet:
            wallet_daily_db[wallet] = int(time.time() * 1000)
            _save_json(WALLET_DAILY_DB_FILE, wallet_daily_db)
        _save_json(EARNINGS_DB_FILE, earnings_db)
        tier = "HOLDER" if is_holder(wallet) else "Free"
        new_claimed, _, _ = get_wallet_daily_claimed(wallet)
        new_remaining = max(0, cap - new_claimed)
        logger.info("API /api/claim SUCCESS: sent %s INFINITE (%s), daily=%s/%s", claimable, tier, new_claimed, cap)
        return jsonify({
            "success": True,
            "tx": result.get("tx"),
            "amount": claimable,
            "message": f"{tier}: {result['message']}",
            "wallet_balance": wallet_balance,
            "wallet_scanned": True,
            "daily_claimed": new_claimed,
            "daily_cap": cap,
            "daily_remaining": new_remaining
        })
    logger.error("API /api/claim FAILED: %s", result.get('message'))
    return jsonify({
        "success": False,
        "message": result.get("message", "Transfer failed"),
        "wallet_balance": wallet_balance,
        "wallet_scanned": True
    })

@app.route("/api/daily", methods=["POST"])
def api_daily():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    wallet = data.get("wallet_address", "").strip()
    logger.info("API /api/daily uid=%s wallet=%s...", uid, wallet[:6] if wallet else "none")
    if not uid: 
        return jsonify({"error": "Missing"}), 400
    
    # WALLET-FIRST CHECK
    if wallet and not is_wallet_daily_bonus_available(wallet):
        remaining = get_wallet_daily_bonus_remaining_text(wallet)
        return jsonify({"success": False, "message": f"Wallet already claimed daily bonus today. Next: {remaining}"})
    
    # Backup uid check for no-wallet users
    if not wallet and not is_daily_available(uid):
        return jsonify({"success": False, "message": "Cooldown"})

    daily_bonus_db[uid] = int(time.time() * 1000)
    _save_json(DAILY_DB_FILE, daily_bonus_db)
    if wallet:
        wallet_daily_db[wallet] = int(time.time() * 1000)
        _save_json(WALLET_DAILY_DB_FILE, wallet_daily_db)

    wallet_balance = get_wallet_balance(wallet) if wallet else 0
    result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    logger.info("API /api/daily transfer: %s", result.get('success'))

    # RETURN REAL STATUS — never fake success
    if result.get('success'):
        return jsonify({
            "success": True,
            "tx": result.get("tx", ""),
            "transferred": True,
            "wallet_balance": wallet_balance,
            "wallet_scanned": True
        })
    else:
        return jsonify({
            "success": False,
            "message": result.get("message", "Transfer failed. Wallet needs SOL for fees or token account creation."),
            "tx": result.get("tx", ""),
            "transferred": False,
            "wallet_balance": wallet_balance,
            "wallet_scanned": True
        })

@app.route("/api/balance/<uid>", methods=["GET"])
def api_get_balance(uid):
    query_wallet = request.args.get("wallet", "").strip()
    saved_wallet = user_db.get(str(uid), {}).get("wallet", "")

    # TRUST frontend wallet param FIRST — never serve stale saved wallet
    if query_wallet and len(query_wallet) >= 32:
        wallet = query_wallet
        if saved_wallet != query_wallet:
            user_db.setdefault(str(uid), {})["wallet"] = query_wallet
            _save_json(USER_DB_FILE, user_db)
            logger.info("Updated wallet for uid=%s: %s... (was %s)", uid, query_wallet[:6], saved_wallet[:6] if saved_wallet else "none")
    else:
        wallet = saved_wallet

    _, e, _, _ = get_db(uid)
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_wallet_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    holder = is_holder(wallet) if wallet else False
    result = {
        "earned": e['total_earned'],
        "unclaimed": e['unclaimed'],
        "claimed": e['total_claimed'],
        "daily_cap": cap,
        "daily_claimed": claimed,
        "daily_remaining": remaining,
        "daily_reset_ms": get_wallet_daily_reset_time(wallet),
        "is_holder": holder
    }
    if wallet:
        bal = has_minimum_balance(wallet)
        result.update({
            "wallet_balance": bal['balance'],
            "can_claim": True,
        })
        logger.info("API /api/balance/%s: wallet=%s... balance=%.2f holder=%s unclaimed=%s daily=%s/%s",
                    uid, wallet[:6], bal['balance'], holder, e['unclaimed'], claimed, cap)
    else:
        logger.info("API /api/balance/%s: NO WALLET unclaimed=%s daily=%s/%s", uid, e['unclaimed'], claimed, cap)
    
    if wallet:
        result["daily_bonus_available"] = is_wallet_daily_bonus_available(wallet)
        result["daily_bonus_next"] = get_wallet_daily_bonus_remaining_text(wallet)
    
    return jsonify(result)

# ========== LEADERBOARD ROUTES ==========
@app.route("/api/score", methods=["POST"])
def api_score():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    distance = int(data.get("distance", data.get("score", 0)))
    username = data.get("username", "Anonymous")
    logger.info("API /api/score REQUEST wallet=%s... distance=%s username=%s", wallet[:6] if wallet else "none", distance, username)
    if not wallet:
        logger.warning("API /api/score REJECTED: no wallet")
        return jsonify({"error": "No wallet"}), 400
    if len(wallet) < 32:
        logger.warning("API /api/score REJECTED: wallet too short (%s chars)", len(wallet))
        return jsonify({"error": "Invalid wallet"}), 400
    if distance < 0:
        logger.warning("API /api/score REJECTED: negative distance")
        return jsonify({"error": "Invalid distance"}), 400
    logger.info("API /api/score saving to Supabase: %s... -> %s", wallet[:6], distance)
    _save_score_supabase(wallet, distance, username)
    existing = high_scores_db.get(wallet, {"best_distance": 0})
    new_record = False
    if distance > existing.get("best_distance", 0):
        high_scores_db[wallet] = {
            "best_distance": distance,
            "username": username,
            "last_updated": time.time()
        }
        _save_json(HIGHSCORE_DB_FILE, high_scores_db)
        new_record = True
        logger.info("API /api/score NEW RECORD: %s -> %sm", wallet[:6], distance)
    else:
        logger.info("API /api/score no record: %s vs %s", distance, existing.get("best_distance", 0))
    best = _get_best_supabase(wallet)
    logger.info("API /api/score SUCCESS: best=%s new_record=%s", best, new_record)
    return jsonify({"success": True, "new_record": new_record, "best_distance": best})

@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    rows = _get_leaderboard_supabase(10)
    leaderboard = []
    for rank, (wallet, distance, username) in enumerate(rows, 1):
        leaderboard.append({
            "rank": rank,
            "wallet": wallet[:4] + "..." + wallet[-4:],
            "full_wallet": wallet,
            "username": username or "Anonymous",
            "distance": distance
        })
    total = _count_players_supabase()
    logger.info("API /api/leaderboard: returning %s entries (total: %s)", len(leaderboard), total)
    return jsonify({"leaderboard": leaderboard, "total_players": total})

@app.route("/api/highscore/<wallet>", methods=["GET"])
def api_highscore(wallet):
    w = wallet.strip()
    if not w or len(w) < 32:
        return jsonify({"error": "Invalid wallet"}), 400
    best = _get_best_supabase(w)
    return jsonify({
        "best_distance": best,
        "username": "Anonymous"
    })

@app.route("/setup-webhook")
def setup_webhook():
    try:
        f1 = asyncio.run_coroutine_threadsafe(telegram_app.bot.delete_webhook(drop_pending_updates=True), _bot_loop)
        f1.result(timeout=10)
        f2 = asyncio.run_coroutine_threadsafe(telegram_app.bot.set_webhook(url=f"{BASE_URL}/webhook"), _bot_loop)
        f2.result(timeout=10)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== INIT ==========
telegram_app = None
_bot_loop = None
_bot_thread = None

async def _bot_main():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).connection_pool_size(20).pool_timeout(30.0).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("play", cmd_play))
    telegram_app.add_handler(CommandHandler("wallet", cmd_wallet))
    telegram_app.add_handler(CommandHandler("setwallet", cmd_setwallet))
    telegram_app.add_handler(CommandHandler("balance", cmd_balance))
    telegram_app.add_handler(CommandHandler("claim", cmd_claim))
    telegram_app.add_handler(CommandHandler("daily", cmd_daily))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
    telegram_app.add_handler(CallbackQueryHandler(on_callback))
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Bot started")
    await asyncio.sleep(2)
    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
        await telegram_app.bot.set_webhook(url=f"{BASE_URL}/webhook")
        logger.info("Webhook auto-set to %s/webhook", BASE_URL)
    except Exception as e:
        logger.error("Auto webhook setup failed: %s", e)
    while True: await asyncio.sleep(3600)

def init_bot():
    global _bot_loop, _bot_thread
    _bot_loop = asyncio.new_event_loop()
    def _run_loop():
        asyncio.set_event_loop(_bot_loop)
        _bot_loop.run_until_complete(_bot_main())
    _bot_thread = threading.Thread(target=_run_loop, daemon=True)
    _bot_thread.start()
    time.sleep(0.5)

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set!")
else:
    init_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
