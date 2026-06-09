"""
Infinitecoin Jumper Bot - v7.1 (MongoDB + Redis + Anti-Multiwallet Security)
Free: 10K/day total | Holders (0.1 SOL worth INFINITE): 150K/day total
Wallet locked 1x forever. Daily claim tracking via Redis TTL.
MongoDB for persistent data. Redis for rate limits, daily caps, sessions.
"""

import os
import json
import logging
import time
import requests
import asyncio
import threading
import base64
import struct
import hashlib
import ssl  # FIX 1: Required for Redis TLS
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# MongoDB
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# Redis
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "https://your-app.up.railway.app").rstrip("/")
GAME_URL = os.environ.get("GAME_URL", "https://your-game.vercel.app").rstrip("/")
IFC_MINT = os.environ.get("IFC_MINT_ADDRESS", "C8KsvkMBuqmvX416MWTJGKW9S9MpKiUjmpnj1fhzpump")
TREASURY_KEY = os.environ.get("TREASURY_PRIVATE_KEY", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MONGODB_URI = os.environ.get("MONGODB_URI", "")
REDIS_URL = os.environ.get("REDIS_URL", "")

# FIX 3: Server-side earnings cap per gameplay run
MAX_EARNINGS_PER_RUN = 5000

# ========== DATABASE INIT ==========
mongo_client = None
db = None
redis_client = None

def init_databases():
    global mongo_client, db, redis_client
    
    if not MONGODB_URI:
        logger.error("MONGODB_URI not set!")
        raise ValueError("MONGODB_URI required")
    if not REDIS_URL:
        logger.error("REDIS_URL not set!")
        raise ValueError("REDIS_URL required")
    
    # MongoDB
    mongo_client = MongoClient(MONGODB_URI, maxPoolSize=50, serverSelectionTimeoutMS=5000)
    db = mongo_client.get_default_database()
    
    # Create unique indexes
    db.players.create_index("telegram_uid", unique=True)
    db.players.create_index("wallet_address", unique=True, sparse=True)
    db.scores.create_index([("wallet_address", ASCENDING), ("best_distance", DESCENDING)])
    db.audit_logs.create_index("timestamp", expireAfterSeconds=60*60*24*30)
    
    logger.info("MongoDB connected: %s", db.name)
    
    # FIX 1: Redis — corrected for redis-py 4.x+ (Upstash / Railway TLS)
    if REDIS_URL.startswith("rediss://"):
        ssl_context = ssl.SSLContext()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            ssl_context=ssl_context
        )
    else:
        redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True
        )
    
    redis_client.ping()
    logger.info("Redis connected")

# ========== REDIS HELPERS ==========
def _redis_key(prefix, identifier):
    return f"infinitecoin:{prefix}:{identifier}"

def get_daily_claimed(wallet):
    if not wallet:
        return 0, 0, True
    key = _redis_key("daily", wallet)
    data = redis_client.hgetall(key)
    if not data:
        return 0, 0, True
    
    first_claim = int(data.get("first_claim", 0))
    total = int(data.get("total", 0))
    
    if first_claim == 0:
        return 0, 0, True
    
    now_ms = int(time.time() * 1000)
    hours_since = (now_ms - first_claim) / (1000 * 60 * 60)
    
    if hours_since >= 24:
        redis_client.delete(key)
        return 0, 0, True
    
    return total, first_claim, False

def add_daily_claimed(wallet, amount):
    if not wallet:
        return
    key = _redis_key("daily", wallet)
    now_ms = int(time.time() * 1000)
    
    pipe = redis_client.pipeline()
    exists = redis_client.exists(key)
    
    if not exists:
        pipe.hset(key, mapping={"first_claim": now_ms, "total": amount})
        pipe.expire(key, 24 * 60 * 60)
    else:
        pipe.hincrby(key, "total", amount)
    
    pipe.execute()
    logger.info("Redis daily claim: wallet=%s... amount=%s", wallet[:6], amount)

def get_daily_bonus_status(wallet):
    if not wallet:
        return True, 0
    key = _redis_key("bonus", wallet)
    last = redis_client.get(key)
    if not last:
        return True, 0
    
    last_ms = int(last)
    now_ms = int(time.time() * 1000)
    remaining_ms = (24 * 60 * 60 * 1000) - (now_ms - last_ms)
    
    if remaining_ms <= 0:
        redis_client.delete(key)
        return True, 0
    
    return False, remaining_ms

def set_daily_bonus_claimed(wallet):
    if not wallet:
        return
    key = _redis_key("bonus", wallet)
    now_ms = int(time.time() * 1000)
    redis_client.setex(key, 24 * 60 * 60, str(now_ms))

def rate_limit_check(identifier, max_requests=10, window_seconds=60):
    key = _redis_key("ratelimit", identifier)
    current = redis_client.get(key)
    
    if not current:
        redis_client.setex(key, window_seconds, 1)
        return True, max_requests - 1
    
    count = int(current)
    if count >= max_requests:
        return False, 0
    
    redis_client.incr(key)
    return True, max_requests - count - 1

# ========== TOKEN PRICES ==========
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
                return IFC_PRICE_USD
    except Exception as e:
        logger.error("DexScreener IFC failed: %s", e)
    if IFC_PRICE_USD is None:
        IFC_PRICE_USD = 0.00000329
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
            return SOL_PRICE_USD
    except Exception as e:
        logger.error("SOL price fetch failed: %s", e)
    if SOL_PRICE_USD is None:
        SOL_PRICE_USD = 86.24
    return SOL_PRICE_USD

# ========== CLAIM CAPS ==========
FREE_CAP = 10000
HOLDER_CAP = 150000
DAILY_BONUS_AMOUNT = 500

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ========== MONGO HELPERS ==========
def get_or_create_player(uid):
    uid = str(uid)
    player = db.players.find_one({"telegram_uid": uid})
    if not player:
        player = {
            "telegram_uid": uid,
            "wallet_address": None,
            "wallet_linked_at": None,
            "wallet_signature": None,
            "total_earned": 0,
            "unclaimed": 0,
            "total_claimed": 0,
            "is_holder": False,
            "holder_checked_at": 0,
            "created_at": int(time.time()),
            "last_active": int(time.time())
        }
        db.players.insert_one(player)
        logger.info("New player created: uid=%s", uid)
    return player

def audit_log(action, uid, wallet, details, ip=None):
    db.audit_logs.insert_one({
        "action": action,
        "telegram_uid": str(uid) if uid else None,
        "wallet": wallet[:10] + "..." if wallet else None,
        "details": details,
        "ip": ip or request.remote_addr,
        "timestamp": int(time.time())
    })

# ========== WALLET UNIQUENESS & SECURITY ==========
def _get_player_by_wallet(wallet):
    if not wallet:
        return None
    return db.players.find_one({"wallet_address": wallet.strip()})

def _can_link_wallet(uid, wallet):
    if not wallet or len(wallet) < 32:
        return False, None, "Invalid wallet address"
    
    wallet = wallet.strip()
    existing = _get_player_by_wallet(wallet)
    
    if existing:
        if str(existing["telegram_uid"]) == str(uid):
            return True, existing, "Already linked to you"
        return False, existing, "Wallet already linked to another account"
    
    current_player = db.players.find_one({"telegram_uid": str(uid)})
    if current_player and current_player.get("wallet_address"):
        if current_player["wallet_address"] != wallet:
            return False, None, "You already have a wallet linked"
    
    return True, None, "Available"

# ========== FIX 2: REAL WALLET SIGNATURE VERIFICATION ==========
def verify_wallet_signature(wallet, message, signature):
    if not wallet or not message or not signature:
        return False
    try:
        sig_bytes = base64.b64decode(signature)
        if len(sig_bytes) != 64:
            return False
    except Exception:
        return False
    
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
        from solders.pubkey import Pubkey
        
        # Solana wallet addresses are 32-byte Ed25519 public keys
        pubkey = Pubkey.from_string(wallet)
        verify_key = VerifyKey(bytes(pubkey))
        
        # Verify Ed25519 signature on the UTF-8 message
        verify_key.verify(message.encode('utf-8'), sig_bytes)
        return True
    except ImportError:
        logger.error("PyNaCl not installed. Run: pip install pynacl")
        return False
    except BadSignatureError:
        return False
    except Exception as e:
        logger.error("Signature verification error: %s", e)
        return False

def generate_link_message(uid, wallet):
    timestamp = int(time.time())
    return f"Infinitecoin Jumper: Link wallet {wallet[:8]}... to Telegram {uid} at {timestamp}"

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
        
        db.players.update_one(
            {"wallet_address": wallet_address},
            {"$set": {"is_holder": result, "holder_checked_at": int(now)}}
        )
        return result
    except Exception as e:
        logger.error("Holder check error: %s", e)
        return False

def get_daily_cap(wallet_address):
    if wallet_address and is_holder(wallet_address):
        return HOLDER_CAP
    return FREE_CAP

def get_wallet_daily_remaining(wallet):
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_daily_claimed(wallet)
    return max(0, cap - claimed)

def get_wallet_daily_reset_time(wallet):
    _, first_claim, _ = get_daily_claimed(wallet)
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
                        return total
                    return 0.0
        except Exception as e:
            logger.warning("RPC fail %s: %s", url.split('/')[2], str(e)[:80])
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

def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {"success": False, "tx": None, "message": "Treasury not ready"}
    
    recipient_bal = get_wallet_balance(recipient)
    treasury_bal = get_treasury_balance()
    if treasury_bal < amount:
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
                data=bytes([1])
            )
            instructions.append(create_ix)
        
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
            return {"success": True, "tx": send_resp['result'], "message": f"Sent {amount:,} INFINITE", "recipient_balance": recipient_bal}
        else:
            return {"success": False, "tx": None, "message": f"RPC error: {send_resp.get('error', 'unknown')}", "recipient_balance": recipient_bal}
    except Exception as e:
        logger.error("Transfer error: %s", e)
        return {"success": False, "tx": None, "message": str(e), "recipient_balance": recipient_bal}

# ========== TELEGRAM HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address")
    
    wallet_text = f"`{wallet[:4]}...{wallet[-4:]}`" if wallet else "*Not connected*"
    holder_status = "💎 HOLDER" if (wallet and player.get("is_holder")) else "👤 Free"
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    
    status_lines = [
        "*Infinitecoin Jumper*", "_Collect coins. Avoid viruses. Earn INFINITE._", "",
        f"Status: {holder_status}",
        f"Wallet: {wallet_text}",
        f"Earned: {player['total_earned']:,} INFINITE",
        f"Unclaimed: {player['unclaimed']:,} INFINITE",
        f"Claimed today: {claimed:,} / {cap:,} INFINITE",
    ]
    status_lines.extend(["", "/play - Launch game", "/wallet - Connect wallet (1x only)",
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
    player = get_or_create_player(uid)
    existing = player.get("wallet_address")
    
    if existing:
        holder = player.get("is_holder", False)
        tier = "💎 HOLDER (150K/day)" if holder else "👤 Free (10K/day)"
        await update.message.reply_text(
            f"Wallet locked: `{existing[:4]}...{existing[-4:]}`\n{tier}\nUse /balance or /claim.", parse_mode="Markdown")
        return
    
    msg = "Use /setwallet ADDRESS SIGNATURE to link your wallet.\nYou must sign a message proving ownership."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    player = get_or_create_player(uid)
    existing = player.get("wallet_address")
    
    if existing:
        await update.message.reply_text(f"Wallet already locked: `{existing[:4]}...{existing[-4:]}`\nCannot change.", parse_mode="Markdown")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/setwallet ADDRESS SIGNATURE`\nGet signature from Phantom wallet.", parse_mode="Markdown")
        return
    
    wallet = context.args[0].strip()
    signature = context.args[1].strip()
    
    if len(wallet) < 32:
        await update.message.reply_text("Invalid address."); return
    
    allowed, _ = rate_limit_check(f"setwallet:{uid}", max_requests=3, window_seconds=300)
    if not allowed:
        await update.message.reply_text("Too many attempts. Wait 5 minutes.")
        return
    
    message = generate_link_message(uid, wallet)
    if not verify_wallet_signature(wallet, message, signature):
        await update.message.reply_text("Invalid signature. You must sign the link message with your wallet.")
        return
    
    can_set, existing_player, reason = _can_link_wallet(uid, wallet)
    if not can_set:
        await update.message.reply_text(f"Cannot link: {reason}")
        audit_log("WALLET_LINK_REJECTED", uid, wallet, {"reason": reason})
        return
    
    # FIX 4: Atomic update with race-condition protection
    try:
        db.players.update_one(
            {"telegram_uid": uid},
            {"$set": {
                "wallet_address": wallet,
                "wallet_linked_at": int(time.time()),
                "wallet_signature": signature[:64],
                "last_active": int(time.time())
            }}
        )
    except DuplicateKeyError:
        audit_log("WALLET_LINK_RACE", uid, wallet, {"reason": "DuplicateKeyError"})
        await update.message.reply_text("Wallet was just linked to another account. Please try a different wallet.")
        return
    
    audit_log("WALLET_LINKED", uid, wallet, {"message": message})
    await update.message.reply_text(f"✅ Wallet locked! Now /claim or /balance.", parse_mode="Markdown")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address")
    e = player
    
    lines = ["*Your INFINITE Status*"]
    if wallet:
        lines.append(f"Wallet: `{wallet[:4]}...{wallet[-4:]}`")
        bal = get_wallet_balance(wallet)
        holder = is_holder(wallet)
        req = get_required_infinite_for_holder()
        cap = get_daily_cap(wallet)
        claimed, _, _ = get_daily_claimed(wallet)
        remaining = max(0, cap - claimed)
        
        lines.append(f"Balance: {bal:,.2f} INFINITE")
        if holder:
            lines.append("Tier: 💎 *HOLDER* — 150K/day claim cap")
        else:
            lines.append("Tier: 👤 *Free* — 10K/day claim cap")
            if req:
                lines.append(f"Hold {req:,.0f} INFINITE to unlock 150K/day")
        lines.append(f"Claimed today: {claimed:,} / {cap:,} INFINITE")
        lines.append(f"Remaining today: {remaining:,} INFINITE")
    else:
        lines.append("Wallet: *Not connected*")
    
    lines.extend([
        f"Earned: {e['total_earned']:,} INFINITE",
        f"Unclaimed: {e['unclaimed']:,} INFINITE",
        f"Claimed: {e['total_claimed']:,} INFINITE",
        "\n/play to earn!"
    ])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address")
    e = player
    
    if not wallet:
        await update.message.reply_text("No wallet! Use /wallet first."); return
    if e['unclaimed'] <= 0:
        await update.message.reply_text("No INFINITE to claim. /play to earn!"); return
    
    cap = get_daily_cap(wallet)
    claimed, _, reset = get_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    
    if remaining <= 0:
        reset_text = get_wallet_daily_reset_text(wallet)
        msg = f"Cap reached: {claimed:,}/{cap:,} INFINITE today\nResets in: {reset_text}"
        if not is_holder(wallet):
            req = get_required_infinite_for_holder()
            if req:
                msg += f"\n\nHold {req:,.0f} INFINITE (0.1 SOL) to unlock 150K/day"
        await update.message.reply_text(msg); return
    
    claimable = min(e['unclaimed'], remaining)
    if claimable <= 0:
        await update.message.reply_text("Nothing to claim."); return
    
    wallet_balance = get_wallet_balance(wallet)
    result = transfer_ifc(wallet, claimable)
    
    if result['success']:
        db.players.update_one(
            {"telegram_uid": uid},
            {"$inc": {"total_claimed": claimable, "unclaimed": -claimable}}
        )
        add_daily_claimed(wallet, claimable)
        audit_log("CLAIM_SUCCESS", uid, wallet, {"amount": claimable, "tx": result.get("tx")})
    else:
        audit_log("CLAIM_FAILED", uid, wallet, {"amount": claimable, "error": result.get("message")})
    
    tier = "HOLDER" if is_holder(wallet) else "Free"
    status = "✅ Claimed" if result['success'] else "❌ Failed"
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
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address")
    e = player
    
    if wallet:
        available, remaining_ms = get_daily_bonus_status(wallet)
        if not available:
            hours = int(remaining_ms / (1000 * 60 * 60))
            mins = int((remaining_ms % (1000 * 60 * 60)) / (1000 * 60))
            await update.message.reply_text(f"Daily bonus already claimed for this wallet.\nNext: {hours}h {mins}m")
            return
    
    allowed, _ = rate_limit_check(f"daily:{uid}", max_requests=1, window_seconds=86400)
    if not allowed:
        await update.message.reply_text("Daily bonus already claimed today.")
        return
    
    set_daily_bonus_claimed(wallet) if wallet else None
    
    wallet_balance = get_wallet_balance(wallet) if wallet else 0
    result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    
    if result.get('success'):
        db.players.update_one(
            {"telegram_uid": uid},
            {"$inc": {"total_earned": DAILY_BONUS_AMOUNT, "total_claimed": DAILY_BONUS_AMOUNT}}
        )
        audit_log("DAILY_SUCCESS", uid, wallet, {"tx": result.get("tx")})
        await update.message.reply_text(
            f"DAILY BONUS! +{DAILY_BONUS_AMOUNT:,} INFINITE!\n"
            f"Wallet balance: {wallet_balance:,.2f} INFINITE\n"
            f"Tx: `{result.get('tx')}`",
            parse_mode="Markdown"
        )
    else:
        db.players.update_one(
            {"telegram_uid": uid},
            {"$inc": {"total_earned": DAILY_BONUS_AMOUNT, "unclaimed": DAILY_BONUS_AMOUNT}}
        )
        audit_log("DAILY_ESCROW", uid, wallet, {"added_to_unclaimed": DAILY_BONUS_AMOUNT})
        await update.message.reply_text(
            f"Bonus added to unclaimed! ({result.get('message', '')})\n"
            f"Wallet balance: {wallet_balance:,.2f} INFINITE"
        )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = get_required_infinite_for_holder()
    req_text = f"Hold {req:,.0f} INFINITE to unlock 150K/day" if req else ""
    await update.message.reply_text(
        f"*How to Play*\nArrows: Move | Space: Jump\n\n*Claims*\n"
        f"👤 Free: 10K/day total (claim multiple times)\n"
        f"💎 Holders: 150K/day total (claim multiple times)\n"
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
    return jsonify({
        "bot": "Infinitecoin Jumper v7.1",
        "escrow": "LIVE" if escrow_ready else "DEMO",
        "database": "MongoDB + Redis",
        "anti_multiwallet": "ENABLED"
    })

@app.route("/health")
def health():
    try:
        mongo_client.admin.command('ping')
        redis_client.ping()
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return jsonify({
        "status": "ok",
        "database": db_status,
        "escrow_ready": escrow_ready
    })

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
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address", "")
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    
    result = {
        "telegram_user_id": uid,
        "wallet_address": wallet,
        "earned": player['total_earned'],
        "unclaimed": player['unclaimed'],
        "claimed": player['total_claimed'],
        "daily_cap": cap,
        "daily_claimed": claimed,
        "daily_remaining": remaining,
        "daily_reset_ms": get_wallet_daily_reset_time(wallet),
        "is_holder": player.get("is_holder", False)
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
        player = get_or_create_player(uid)
        existing = player.get("wallet_address")
        if existing:
            return redirect(f"{GAME_URL}?user_id={uid}&wallet={existing}")
        
        return '<h1>Signature Required</h1><p>Use /setwallet in Telegram with your wallet signature.</p>'
    
    return '<h1>Connect Wallet</h1><p>Use /setwallet ADDRESS SIGNATURE in the bot.</p>'

@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    signature = data.get("signature", "").strip()
    
    logger.info("API /api/wallet uid=%s wallet=%s...", uid, wallet[:6] if wallet else "none")
    
    if not wallet or not uid or len(wallet) < 32:
        return jsonify({"error": "Invalid"}), 400
    
    allowed, _ = rate_limit_check(f"api_wallet:{uid}", max_requests=5, window_seconds=300)
    if not allowed:
        return jsonify({"error": "Too many attempts"}), 429
    
    message = generate_link_message(uid, wallet)
    if not verify_wallet_signature(wallet, message, signature):
        return jsonify({"error": "Invalid signature"}), 403
    
    player = get_or_create_player(uid)
    existing_wallet = player.get("wallet_address")
    if existing_wallet:
        if existing_wallet.strip() == wallet:
            return jsonify({"success": True, "message": "Already connected"})
        return jsonify({"error": "Wallet locked. Cannot change."}), 409
    
    can_set, existing_player, reason = _can_link_wallet(uid, wallet)
    if not can_set:
        audit_log("API_WALLET_REJECTED", uid, wallet, {"reason": reason})
        return jsonify({"error": reason}), 409
    
    # FIX 4: Atomic update with race-condition protection
    try:
        db.players.update_one(
            {"telegram_uid": uid},
            {"$set": {
                "wallet_address": wallet,
                "wallet_linked_at": int(time.time()),
                "wallet_signature": signature[:64],
                "last_active": int(time.time())
            }}
        )
    except DuplicateKeyError:
        audit_log("API_WALLET_RACE", uid, wallet, {"reason": "DuplicateKeyError"})
        return jsonify({"error": "Wallet already linked to another account"}), 409
    
    audit_log("API_WALLET_LINKED", uid, wallet, {"message": message})
    return jsonify({"success": True})

@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    amount = int(data.get("amount", 0))
    
    if not uid:
        return jsonify({"error": "Missing user_id"}), 400
    
    # FIX 3: Server-side earnings cap validation
    if amount <= 0 or amount > MAX_EARNINGS_PER_RUN:
        return jsonify({"error": f"Invalid amount. Max per run: {MAX_EARNINGS_PER_RUN}"}), 400
    
    allowed, _ = rate_limit_check(f"earnings:{uid}", max_requests=100, window_seconds=60)
    if not allowed:
        return jsonify({"error": "Rate limit exceeded"}), 429
    
    db.players.update_one(
        {"telegram_uid": uid},
        {"$inc": {"total_earned": amount, "unclaimed": amount}, "$set": {"last_active": int(time.time())}}
    )
    
    player = get_or_create_player(uid)
    audit_log("EARNINGS", uid, player.get("wallet_address"), {"amount": amount})
    return jsonify({"success": True, "unclaimed": player['unclaimed']})

@app.route("/api/claim", methods=["POST"])
def api_claim():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    wallet = data.get("wallet_address", "").strip()
    
    if not uid or not wallet:
        return jsonify({"error": "Invalid"}), 400
    
    allowed, _ = rate_limit_check(f"api_claim:{uid}", max_requests=10, window_seconds=60)
    if not allowed:
        return jsonify({"error": "Too many claims"}), 429
    
    player = get_or_create_player(uid)
    if player.get("wallet_address") != wallet:
        return jsonify({"error": "Wallet mismatch"}), 403
    
    e = player
    if e['unclaimed'] <= 0:
        return jsonify({"success": False, "message": "No IFC to claim"})
    
    cap = get_daily_cap(wallet)
    claimed, _, reset = get_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    
    if remaining <= 0:
        reset_text = get_wallet_daily_reset_text(wallet)
        req = get_required_infinite_for_holder()
        msg = f"Cap reached: {claimed:,}/{cap:,} INFINITE today. Resets in {reset_text}."
        if not is_holder(wallet) and req:
            msg += f" Hold {req:,.0f} INFINITE (0.1 SOL) to unlock 150K/day."
        return jsonify({"success": False, "message": msg, "cap_reached": True})
    
    claimable = min(e['unclaimed'], remaining)
    requested_amount = int(data.get("amount", 0))
    if requested_amount > 0:
        amount = min(requested_amount, claimable)
    else:
        amount = claimable
    
    if amount <= 0:
        return jsonify({"success": False, "message": "Nothing to claim"})
    
    wallet_balance = get_wallet_balance(wallet)
    result = transfer_ifc(wallet, amount)
    
    if result.get('success'):
        db.players.update_one(
            {"telegram_uid": uid},
            {"$inc": {"total_claimed": amount, "unclaimed": -amount}}
        )
        add_daily_claimed(wallet, amount)
        audit_log("API_CLAIM_SUCCESS", uid, wallet, {"amount": amount, "tx": result.get("tx")})
        
        new_claimed, _, _ = get_daily_claimed(wallet)
        new_remaining = max(0, cap - new_claimed)
        tier = "HOLDER" if is_holder(wallet) else "Free"
        
        return jsonify({
            "success": True,
            "tx": result.get("tx"),
            "amount": amount,
            "message": f"{tier}: {result['message']}",
            "wallet_balance": wallet_balance,
            "daily_claimed": new_claimed,
            "daily_cap": cap,
            "daily_remaining": new_remaining
        })
    
    audit_log("API_CLAIM_FAILED", uid, wallet, {"amount": amount, "error": result.get("message")})
    return jsonify({
        "success": False,
        "message": result.get("message", "Transfer failed"),
        "wallet_balance": wallet_balance
    })

@app.route("/api/daily", methods=["POST"])
def api_daily():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    wallet = data.get("wallet_address", "").strip()
    
    if not uid:
        return jsonify({"error": "Missing"}), 400
    
    player = get_or_create_player(uid)
    if wallet and player.get("wallet_address") != wallet:
        return jsonify({"error": "Wallet mismatch"}), 403
    
    if wallet:
        available, remaining_ms = get_daily_bonus_status(wallet)
        if not available:
            hours = int(remaining_ms / (1000 * 60 * 60))
            mins = int((remaining_ms % (1000 * 60 * 60)) / (1000 * 60))
            return jsonify({"success": False, "message": f"Wallet already claimed daily bonus today. Next: {hours}h {mins}m"})
    
    allowed, _ = rate_limit_check(f"api_daily:{uid}", max_requests=1, window_seconds=86400)
    if not allowed:
        return jsonify({"success": False, "message": "Daily bonus already claimed"})
    
    set_daily_bonus_claimed(wallet) if wallet else None
    
    wallet_balance = get_wallet_balance(wallet) if wallet else 0
    result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    
    if result.get('success'):
        db.players.update_one(
            {"telegram_uid": uid},
            {"$inc": {"total_earned": DAILY_BONUS_AMOUNT, "total_claimed": DAILY_BONUS_AMOUNT}}
        )
        audit_log("API_DAILY_SUCCESS", uid, wallet, {"tx": result.get("tx")})
        return jsonify({
            "success": True,
            "tx": result.get("tx", ""),
            "transferred": True,
            "wallet_balance": wallet_balance
        })
    else:
        db.players.update_one(
            {"telegram_uid": uid},
            {"$inc": {"total_earned": DAILY_BONUS_AMOUNT, "unclaimed": DAILY_BONUS_AMOUNT}}
        )
        audit_log("API_DAILY_ESCROW", uid, wallet, {"added_to_unclaimed": DAILY_BONUS_AMOUNT})
        return jsonify({
            "success": False,
            "message": result.get("message", "Transfer failed"),
            "transferred": False,
            "wallet_balance": wallet_balance
        })

@app.route("/api/balance/<uid>", methods=["GET"])
def api_get_balance(uid):
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address", "")
    
    cap = get_daily_cap(wallet)
    claimed, _, _ = get_daily_claimed(wallet)
    remaining = max(0, cap - claimed)
    holder = player.get("is_holder", False)
    
    result = {
        "earned": player['total_earned'],
        "unclaimed": player['unclaimed'],
        "claimed": player['total_claimed'],
        "daily_cap": cap,
        "daily_claimed": claimed,
        "daily_remaining": remaining,
        "daily_reset_ms": get_wallet_daily_reset_time(wallet),
        "is_holder": holder
    }
    
    if wallet:
        bal = get_wallet_balance(wallet)
        result.update({
            "wallet_balance": bal,
            "can_claim": True,
        })
        result["daily_bonus_available"] = get_daily_bonus_status(wallet)[0]
        result["daily_bonus_next"] = "Available now!" if get_daily_bonus_status(wallet)[0] else f"{int(get_daily_bonus_status(wallet)[1]/(1000*60*60))}h"
    
    return jsonify(result)

# ========== LEADERBOARD ROUTES (ANONYMIZED + RANK) ==========
@app.route("/api/score", methods=["POST"])
def api_score():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    distance = int(data.get("distance", data.get("score", 0)))
    username = data.get("username", "Anonymous")
    uid = str(data.get("telegram_user_id", data.get("user_id", "")))
    
    if not wallet or len(wallet) < 32:
        return jsonify({"error": "Invalid wallet"}), 400
    if distance < 0:
        return jsonify({"error": "Invalid distance"}), 400
    
    allowed, _ = rate_limit_check(f"score:{wallet}", max_requests=30, window_seconds=60)
    if not allowed:
        return jsonify({"error": "Too many score submissions"}), 429
    
    player = db.players.find_one({"telegram_uid": uid})
    if not player or player.get("wallet_address") != wallet:
        audit_log("SCORE_REJECTED", uid, wallet, {"reason": "wallet_mismatch", "distance": distance})
        return jsonify({"error": "Wallet not linked to this account"}), 403
    
    existing = db.scores.find_one({"wallet_address": wallet})
    new_record = False
    
    if not existing or distance > existing.get("best_distance", 0):
        db.scores.update_one(
            {"wallet_address": wallet},
            {"$set": {
                "best_distance": distance,
                "username": username,
                "telegram_uid": uid,
                "last_updated": int(time.time())
            }},
            upsert=True
        )
        new_record = True
    
    audit_log("SCORE_SUBMITTED", uid, wallet, {"distance": distance, "new_record": new_record})
    best = db.scores.find_one({"wallet_address": wallet}, {"best_distance": 1}) or {"best_distance": 0}
    
    return jsonify({"success": True, "new_record": new_record, "best_distance": best.get("best_distance", 0)})

@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    rows = list(db.scores.find().sort("best_distance", DESCENDING).limit(10))
    leaderboard = []
    
    for rank, doc in enumerate(rows, 1):
        wallet = doc["wallet_address"]
        masked = wallet[:4] + "..." + wallet[-4:]
        leaderboard.append({
            "rank": rank,
            "wallet": masked,
            "username": doc.get("username", "Anonymous"),
            "distance": doc["best_distance"]
        })
    
    total = db.scores.count_documents({})
    return jsonify({"leaderboard": leaderboard, "total_players": total})

@app.route("/api/leaderboard/rank", methods=["GET"])
def api_leaderboard_rank():
    wallet = request.args.get("wallet", "").strip()
    if not wallet or len(wallet) < 32:
        return jsonify({"error": "Invalid wallet"}), 400
    
    doc = db.scores.find_one({"wallet_address": wallet})
    if not doc:
        return jsonify({"rank": None, "message": "No score recorded"})
    
    best = doc["best_distance"]
    higher = db.scores.count_documents({"best_distance": {"$gt": best}})
    rank = higher + 1
    
    # Also get total players and percentile
    total = db.scores.count_documents({})
    percentile = round((higher / total) * 100, 1) if total > 0 else 0
    
    return jsonify({
        "rank": rank,
        "best_distance": best,
        "total_players": total,
        "percentile": percentile,
        "username": doc.get("username", "Anonymous")
    })

@app.route("/api/leaderboard/my-rank/<uid>", methods=["GET"])
def api_my_rank(uid):
    """Get current player's rank by UID — for in-game display."""
    player = get_or_create_player(uid)
    wallet = player.get("wallet_address")
    
    if not wallet:
        return jsonify({"rank": None, "message": "No wallet linked"})
    
    doc = db.scores.find_one({"wallet_address": wallet})
    if not doc:
        return jsonify({"rank": None, "message": "No score recorded"})
    
    best = doc["best_distance"]
    higher = db.scores.count_documents({"best_distance": {"$gt": best}})
    rank = higher + 1
    total = db.scores.count_documents({})
    
    # Get nearby players (3 above, 3 below)
    above = list(db.scores.find({"best_distance": {"$gt": best}}).sort("best_distance", ASCENDING).limit(3))
    below = list(db.scores.find({"best_distance": {"$lt": best}}).sort("best_distance", DESCENDING).limit(3))
    
    nearby = []
    for i, d in enumerate(reversed(above), 1):
        nearby.append({
            "rank": rank - i,
            "wallet": d["wallet_address"][:4] + "..." + d["wallet_address"][-4:],
            "username": d.get("username", "Anonymous"),
            "distance": d["best_distance"],
            "relation": "above"
        })
    
    nearby.append({
        "rank": rank,
        "wallet": wallet[:4] + "..." + wallet[-4:],
        "username": doc.get("username", "Anonymous"),
        "distance": best,
        "relation": "you"
    })
    
    for i, d in enumerate(below, 1):
        nearby.append({
            "rank": rank + i,
            "wallet": d["wallet_address"][:4] + "..." + d["wallet_address"][-4:],
            "username": d.get("username", "Anonymous"),
            "distance": d["best_distance"],
            "relation": "below"
        })
    
    return jsonify({
        "rank": rank,
        "best_distance": best,
        "total_players": total,
        "nearby": nearby
    })

@app.route("/api/highscore/<wallet>", methods=["GET"])
def api_highscore(wallet):
    w = wallet.strip()
    if not w or len(w) < 32:
        return jsonify({"error": "Invalid wallet"}), 400
    
    doc = db.scores.find_one({"wallet_address": w}, {"best_distance": 1})
    best = doc["best_distance"] if doc else 0
    
    return jsonify({"best_distance": best, "username": "Anonymous"})

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

# Initialize databases first
init_databases()
_setup_solana()

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set!")
else:
    init_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
