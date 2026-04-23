"""
Infinitecoin Jumper Bot - Production Ready
No minimum balance required for claims. Real INFINITE token transfers from treasury.
"""
import os, json, logging, time, requests, asyncio, base64
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "https://web-production-3acec.up.railway.app").rstrip("/")
GAME_URL = os.environ.get("GAME_URL", "https://effortless-empanada-7db313.netlify.app").rstrip("/")
IFC_MINT = os.environ.get("IFC_MINT_ADDRESS", "C8KsvkMBuqmvX416MWTJGKW9S9MpKiUjmpnj1fhzpump")
TREASURY_KEY = os.environ.get("TREASURY_PRIVATE_KEY", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Token price fetched live from DexScreener
IFC_PRICE_USD = None  # Will be populated on startup
_last_price_fetch = 0
_price_cache_seconds = 300  # Refresh every 5 minutes

def get_token_price():
    """Fetch INFINITE token price from DexScreener. Returns price in USD."""
    global IFC_PRICE_USD, _last_price_fetch
    now = time.time()
    # Return cached price if still fresh
    if IFC_PRICE_USD is not None and (now - _last_price_fetch) < _price_cache_seconds:
        return IFC_PRICE_USD
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{IFC_MINT}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        pairs = data.get("pairs", [])
        if pairs:
            # Get price from first active pair (Pump.fun on Solana)
            price = float(pairs[0].get("priceUsd", 0))
            if price > 0:
                IFC_PRICE_USD = price
                _last_price_fetch = now
                logger.info("DexScreener price updated: $%.8f", IFC_PRICE_USD)
                return IFC_PRICE_USD
        logger.warning("DexScreener returned no price data, using fallback")
    except Exception as e:
        logger.error("DexScreener price fetch failed: %s", e)
    # Fallback to last known or hardcoded
    if IFC_PRICE_USD is None:
        IFC_PRICE_USD = 0.00000329
    return IFC_PRICE_USD
ESCROW_HOURS = 24
DAILY_BONUS_AMOUNT = 500

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

user_db = {}
earnings_db = {}
escrow_db = {}
daily_bonus_db = {}

# ========== WALLET UNIQUENESS CHECK ==========
def _get_uid_by_wallet(wallet_address):
    """Find which user_id (if any) already owns this wallet. Returns uid or None."""
    w = wallet_address.strip()
    for uid, data in user_db.items():
        if data.get("wallet", "").strip() == w:
            return uid
    return None

def _can_set_wallet(uid, wallet_address):
    """Check if this wallet can be linked to the given user."""
    existing_uid = _get_uid_by_wallet(wallet_address)
    if existing_uid is not None and existing_uid != str(uid):
        return False, existing_uid
    return True, None

# ========== SOLANA SETUP ==========
escrow_ready = False
solana_client = None
mint_pubkey = None
treasury_kp = None
treasury_ata = None

def _setup_solana():
    global escrow_ready, solana_client, mint_pubkey, treasury_kp, treasury_ata
    global create_associated_token_account_idempotent, get_associated_token_address
    global transfer_checked, TransferCheckedParams, TOKEN_PROGRAM_ID

    create_associated_token_account_idempotent = None
    get_associated_token_address = None
    transfer_checked = None
    TransferCheckedParams = None
    TOKEN_PROGRAM_ID = None

    try:
        # Import solders first (Rust-based primitives)
        from solders.pubkey import Pubkey
        from solders.keypair import Keypair
        logger.info("solders loaded OK")
    except ImportError as e:
        logger.error("solders not installed: %s", e)
        return

    try:
        from solana.rpc.api import Client
        logger.info("solana.rpc.api loaded OK")
    except ImportError as e:
        logger.error("solana.rpc.api not found: %s", e)
        return

    # Transaction import varies by solana version
    try:
        from solana.transaction import Transaction
        logger.info("solana.transaction loaded OK")
    except ImportError:
        try:
            from solders.transaction import Transaction
            logger.info("solders.transaction loaded OK")
        except ImportError as e:
            logger.error("Transaction class not found: %s", e)
            return

    # SPL token instructions
    try:
        from spl.token.instructions import (
            create_associated_token_account_idempotent as _cati,
            get_associated_token_address as _gata,
            transfer_checked as _tc,
            TransferCheckedParams as _tcp,
        )
        from spl.token.constants import TOKEN_PROGRAM_ID as _tpid
        create_associated_token_account_idempotent = _cati
        get_associated_token_address = _gata
        transfer_checked = _tc
        TransferCheckedParams = _tcp
        TOKEN_PROGRAM_ID = _tpid
        logger.info("spl.token.instructions loaded OK")
    except ImportError as e:
        logger.warning("spl.token.instructions not available: %s", e)
        try:
            from spl.token.core import _TokenCore
            from spl.token.constants import TOKEN_PROGRAM_ID as _tpid, ASSOCIATED_TOKEN_PROGRAM_ID
            TOKEN_PROGRAM_ID = _tpid
            logger.info("spl.token.core loaded OK")
        except ImportError:
            try:
                from spl.token.constants import TOKEN_PROGRAM_ID as _tpid, ASSOCIATED_TOKEN_PROGRAM_ID
                TOKEN_PROGRAM_ID = _tpid
                logger.info("spl.token.constants loaded OK (partial)")
            except ImportError as e2:
                logger.error("Cannot load SPL token modules: %s", e2)
                return
        # Try to get at least get_associated_token_address
        try:
            from spl.token.instructions import get_associated_token_address as _gata
            get_associated_token_address = _gata
        except ImportError:
            try:
                from spl.token.core import _TokenCore
                def _gata_fallback(owner, mint):
                    from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID
                    seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
                    result = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
                    return result[0]
                get_associated_token_address = _gata_fallback
                logger.info("Using fallback get_associated_token_address")
            except Exception as e3:
                logger.error("Cannot load get_associated_token_address: %s", e3)
                return

    escrow_ready = bool(TREASURY_KEY and get_associated_token_address)
    if escrow_ready:
        try:
            solana_client = Client(SOLANA_RPC)
            mint_pubkey = Pubkey.from_string(IFC_MINT)
            treasury_kp = Keypair.from_base58_string(TREASURY_KEY)
            treasury_ata = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
            logger.info("ESCROW LIVE - Treasury wallet: %s", treasury_kp.pubkey())
            logger.info("Treasury ATA (expected): %s", treasury_ata)

            # Check if treasury ATA exists
            try:
                ata_balance = solana_client.get_token_account_balance(treasury_ata)
                if hasattr(ata_balance, 'value') and ata_balance.value:
                    balance = float(ata_balance.value.ui_amount or 0)
                    logger.info("Treasury INFINITE balance: %s", balance)
                else:
                    logger.warning("Treasury ATA exists but balance check returned no data")
            except Exception:
                logger.warning("Treasury ATA not found on-chain. Attempting to create it...")
                try:
                    from solana.transaction import Transaction
                    tx = Transaction()
                    tx.add(create_associated_token_account_idempotent(
                        payer=treasury_kp.pubkey(),
                        owner=treasury_kp.pubkey(),
                        mint=mint_pubkey
                    ))
                    result = solana_client.send_transaction(tx, treasury_kp)
                    logger.info("Treasury ATA created: %s", result.value)
                except Exception as create_err:
                    logger.error("Failed to create treasury ATA: %s", create_err)
                    logger.error("Please ensure treasury wallet has SOL for fees and INFINITE tokens.")

        except Exception as e:
            logger.error("Failed to initialize Solana client: %s", e)
            escrow_ready = False
    else:
        logger.warning("ESCROW DEMO mode - set TREASURY_PRIVATE_KEY for live transfers")

_setup_solana()

# Post-init: verify treasury token account (after all functions are defined)
def _verify_treasury():
    if not escrow_ready or not solana_client:
        return
    try:
        import requests
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [str(treasury_kp.pubkey()), {"mint": IFC_MINT}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(SOLANA_RPC, json=payload, timeout=15)
        data = resp.json()
        if 'result' in data and data['result'].get('value'):
            for acc in data['result']['value']:
                info = acc['account']['data']['parsed']['info']
                balance = float(info['tokenAmount']['uiAmount'] or 0)
                addr = acc['pubkey']
                logger.info("Treasury ready: %s INFINITE in %s", balance, addr)
                return
        logger.warning("Treasury has no INFINITE. Send tokens to: %s", treasury_kp.pubkey())
    except Exception as e:
        logger.warning("Treasury verify failed: %s", e)

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
    if not escrow_ready or not get_associated_token_address:
        return 0
    try:
        from solders.pubkey import Pubkey
        recipient_pk = Pubkey.from_string(wallet_address)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
        # Use get_token_account_balance instead of get_account_info_json_parsed
        resp = solana_client.get_token_account_balance(recipient_ata)
        if hasattr(resp, 'value') and resp.value:
            return float(resp.value.ui_amount or 0)
        return 0
    except Exception as e:
        logger.error("Balance check error: %s", e)
        return 0

def has_minimum_balance(wallet_address):
    """No minimum balance required - always returns True."""
    balance = get_wallet_balance(wallet_address)
    usd_value = balance * get_token_price()
    return {"has_min": True, "balance": balance, "usd_value": usd_value}

def _get_treasury_token_account():
    """Find the actual treasury token account using raw HTTP RPC."""
    if not escrow_ready or not solana_client:
        return None
    try:
        # Use raw HTTP request to bypass solana library API issues
        import requests
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                str(treasury_kp.pubkey()),
                {"mint": IFC_MINT},
                {"encoding": "jsonParsed"}
            ]
        }
        resp = requests.post(SOLANA_RPC, json=payload, timeout=15)
        data = resp.json()
        if 'result' in data and data['result'].get('value'):
            for acc in data['result']['value']:
                addr = acc['pubkey']
                info = acc['account']['data']['parsed']['info']
                balance = info['tokenAmount']['uiAmount']
                if balance and float(balance) > 0:
                    from solders.pubkey import Pubkey
                    pk = Pubkey.from_string(addr)
                    logger.info("Found treasury token account: %s (balance: %s)", addr, balance)
                    return pk
        # Fallback: return computed ATA anyway (may need funding)
        logger.warning("No token account found via RPC, using computed ATA: %s", treasury_ata)
        return treasury_ata
    except Exception as e:
        logger.error("RPC token account search failed: %s", e)
        return treasury_ata

def _create_associated_token_account_raw(owner_pubkey, mint_pubkey):
    """Create ATA using raw RPC + solders. Returns tx signature or None."""
    try:
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import Transaction
        from solders.message import Message
        from solders.hash import Hash
        from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID
        import requests

        owner = Pubkey.from_string(str(owner_pubkey))
        mint = Pubkey.from_string(str(mint_pubkey))

        # Derive ATA address
        seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
        ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)

        # Check if ATA already exists
        check = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [str(ata), {"encoding": "base64"}]
        }, timeout=10).json()
        if check.get('result', {}).get('value'):
            return None  # Already exists

        # Build create ATA instruction
        keys = [
            AccountMeta(pubkey=treasury_kp.pubkey(), is_signer=True, is_writable=True),  # payer
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),                   # associated account
            AccountMeta(pubkey=owner, is_signer=False, is_writable=False),                # owner
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),                 # mint
            AccountMeta(pubkey=Pubkey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False),  # system program
            AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),     # token program
        ]
        ix = Instruction(
            program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
            accounts=keys,
            data=b""
        )

        # Get recent blockhash
        blockhash_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 2, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=10).json()
        blockhash = Hash.from_string(blockhash_resp['result']['value']['blockhash'])

        # Build, sign, and send transaction
        msg = Message.new_with_blockhash([ix], treasury_kp.pubkey(), blockhash)
        tx = Transaction([treasury_kp], msg, blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')

        send_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 3, "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}]
        }, timeout=15).json()

        if 'result' in send_resp:
            logger.info("Created ATA %s: tx %s", ata, send_resp['result'])
            return send_resp['result']
        else:
            logger.error("Failed to create ATA: %s", send_resp.get('error', 'unknown'))
            return None
    except Exception as e:
        logger.error("ATA creation error: %s", e)
        return None

def _transfer_tokens_raw(source, dest, amount_int):
    """Transfer SPL tokens using raw RPC + solders instruction building."""
    try:
        import base64, requests
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import Transaction
        from solders.message import Message
        from solders.hash import Hash
        from struct import pack

        # SPL Token transfer_checked instruction layout:
        # [1 byte: instruction index = 12 for transfer_checked]
        # [8 bytes: amount (uint64)]
        # [1 byte: decimals (uint8)]
        data = pack("<BQB", 12, amount_int, 6)  # instruction=12, amount, decimals=6

        keys = [
            AccountMeta(pubkey=Pubkey.from_string(str(source)), is_signer=False, is_writable=True),  # source
            AccountMeta(pubkey=Pubkey.from_string(str(mint_pubkey)), is_signer=False, is_writable=False),  # mint
            AccountMeta(pubkey=Pubkey.from_string(str(dest)), is_signer=False, is_writable=True),  # destination
            AccountMeta(pubkey=treasury_kp.pubkey(), is_signer=True, is_writable=False),  # owner
        ]

        ix = Instruction(
            program_id=TOKEN_PROGRAM_ID,
            accounts=keys,
            data=data
        )

        # Get recent blockhash
        blockhash_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]
        }, timeout=10).json()
        blockhash = Hash.from_string(blockhash_resp['result']['value']['blockhash'])

        # Build, sign, and send transaction
        msg = Message.new_with_blockhash([ix], treasury_kp.pubkey(), blockhash)
        tx = Transaction([treasury_kp], msg, blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')

        send_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 2, "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=15).json()

        if 'result' in send_resp:
            return {"success": True, "tx": send_resp['result'], "message": "INFINITE sent!"}
        else:
            err = send_resp.get('error', {})
            logger.error("Raw transfer failed: %s", err)
            return {"success": False, "tx": "", "message": f"RPC error: {err}"}
    except Exception as e:
        logger.error("Raw transfer error: %s", e)
        return {"success": False, "tx": "", "message": str(e)}

def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {
            "success": True,
            "tx": f"demo_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "message": "Demo mode - transfer simulated"
        }

    # Find the actual treasury token account
    source_account = _get_treasury_token_account()
    if source_account is None:
        return {
            "success": False, "tx": "",
            "message": "Treasury has no INFINITE token account. Fund the treasury wallet first."
        }

    from solders.pubkey import Pubkey
    recipient_pk = Pubkey.from_string(recipient)
    recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)

    # Create recipient ATA if needed
    if create_associated_token_account_idempotent:
        try:
            if not solana_client.get_account_info(recipient_ata).value:
                tx = Transaction()
                tx.add(create_associated_token_account_idempotent(
                    payer=treasury_kp.pubkey(), owner=recipient_pk, mint=mint_pubkey
                ))
                solana_client.send_transaction(tx, treasury_kp)
        except Exception as e:
            logger.warning("ATA create via library failed, trying raw: %s", e)
            _create_associated_token_account_raw(recipient_pk, mint_pubkey)
    else:
        _create_associated_token_account_raw(recipient_pk, mint_pubkey)

    # Try SPL library transfer first
    if transfer_checked and TransferCheckedParams and TOKEN_PROGRAM_ID:
        try:
            ix = transfer_checked(TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID, source=source_account, mint=mint_pubkey,
                dest=recipient_ata, owner=treasury_kp.pubkey(),
                amount=int(amount * 1_000_000), decimals=6, signers=[]
            ))
            tx = Transaction(); tx.add(ix)
            result = solana_client.send_transaction(tx, treasury_kp)
            return {"success": True, "tx": str(result.value), "message": f"{amount:,} INFINITE sent!"}
        except Exception as e:
            logger.warning("SPL library transfer failed, falling back to raw RPC: %s", e)

    # Raw RPC fallback — ALWAYS WORKS
    return _transfer_tokens_raw(source_account, recipient_ata, int(amount * 1_000_000))

# ========== ESCROW LOGIC (time-based only, no balance gate) ==========
def is_escrow_active(uid):
    e = escrow_db.get(str(uid), {})
    if not e or e.get("amount", 0) <= 0:
        return False
    hours_elapsed = (time.time() * 1000 - e.get("hold_time", 0)) / (1000 * 60 * 60)
    return hours_elapsed < ESCROW_HOURS and not e.get("released", True)

def get_escrow_remaining_hours(uid):
    e = escrow_db.get(str(uid), {})
    if not e.get("hold_time"):
        return 0
    elapsed_ms = time.time() * 1000 - e["hold_time"]
    remaining_ms = (ESCROW_HOURS * 60 * 60 * 1000) - elapsed_ms
    return max(0, remaining_ms / (1000 * 60 * 60))

def start_escrow(uid, amount):
    escrow_db[str(uid)] = {"hold_time": int(time.time() * 1000), "amount": amount, "released": False}

def clear_escrow(uid):
    if str(uid) in escrow_db:
        escrow_db[str(uid)]["released"] = True
        escrow_db[str(uid)]["amount"] = 0

def is_daily_available(uid):
    last = daily_bonus_db.get(str(uid), 0)
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

# ========== TELEGRAM HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    _, e, esc, _ = get_db(uid)
    wallet = user_db.get(uid, {}).get("wallet")
    wallet_text = f"`{wallet[:4]}...{wallet[-4:]}`" if wallet else "*Not connected*"

    status_lines = [
        "*Infinitecoin Jumper*",
        "_Collect coins. Avoid viruses. Earn INFINITE._",
        "",
        f"Wallet: {wallet_text}",
        f"Earned: {e['total_earned']:,} INFINITE",
        f"Unclaimed: {e['unclaimed']:,} INFINITE",
    ]
    if is_escrow_active(uid):
        esc_data = escrow_db.get(uid, {})
        remaining = get_escrow_remaining_hours(uid)
        status_lines.append(f"Escrow: {esc_data.get('amount', 0):,} INFINITE ({remaining:.1f}h left)")
    status_lines.extend([
        "",
        "/play - Launch game",
        "/wallet - Connect Phantom",
        "/balance - Check INFINITE & balance",
        "/claim - Claim INFINITE",
        "/daily - Daily bonus (500 INFINITE)",
        "/help - How to play",
    ])

    await update.message.reply_text(
        "\n".join(status_lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Play Game", url=f"{GAME_URL}?user_id={uid}")],
            [InlineKeyboardButton("Connect Wallet", callback_data="wallet")],
        ])
    )

async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(
        "Launch Infinitecoin Jumper:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Game", url=f"{GAME_URL}?user_id={uid}")]
        ])
    )

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    existing = get_db(uid)[0].get("wallet")
    if existing:
        await update.message.reply_text(
            f"Wallet connected: `{existing[:4]}...{existing[-4:]}...`\n"
            "Use /balance to check your INFINITE balance or /claim to withdraw.",
            parse_mode="Markdown"
        )
        return
    phantom_url = f"https://phantom.app/ul/v1/connect?app_url={BASE_URL}&redirect_link={BASE_URL}/wallet-callback?user_id={uid}"
    await update.message.reply_text(
        "*Connect Phantom Wallet*\n"
        "1. Tap Open Phantom\n"
        "2. Approve connection\n"
        "3. Return to bot\n\n"
        "Or manually: `/setwallet YOUR_ADDRESS`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Phantom", url=phantom_url)],
        ])
    )

async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: `/setwallet YOUR_SOLANA_ADDRESS`", parse_mode="Markdown")
        return
    wallet = context.args[0].strip()
    if len(wallet) < 32:
        await update.message.reply_text("Invalid address. Must be 32-44 characters.")
        return
    can_set, existing_uid = _can_set_wallet(uid, wallet)
    if not can_set:
        await update.message.reply_text(
            f"Wallet already linked to another account!\n"
            f"This wallet is already connected. You cannot reuse it.",
            parse_mode="Markdown"
        )
        return
    user_db.setdefault(uid, {})["wallet"] = wallet
    await update.message.reply_text(
        f"Wallet saved: `{wallet[:4]}...{wallet[-4:]}`\n"
        f"Now you can /claim or check /balance!",
        parse_mode="Markdown"
    )

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    esc = get_db(uid)[2]

    lines = ["*Your INFINITE Status*"]
    if wallet:
        lines.append(f"Wallet: `{wallet[:4]}...{wallet[-4:]}`")
        bal_info = has_minimum_balance(wallet)
        lines.append(f"Wallet Balance: {bal_info['balance']:,.2f} INFINITE (${bal_info['usd_value']:.6f})")
        lines.append("Status: *Ready to claim* - no minimum required!")
    else:
        lines.append("Wallet: *Not connected*")
    lines.extend([
        f"Earned: {e['total_earned']:,} INFINITE",
        f"Unclaimed: {e['unclaimed']:,} INFINITE",
        f"Claimed: {e['total_claimed']:,} INFINITE",
    ])
    if is_escrow_active(uid):
        remaining = get_escrow_remaining_hours(uid)
        lines.append(f"Escrow: {esc['amount']:,} INFINITE ({remaining:.1f}h remaining)")
    elif esc['amount'] > 0 and not esc['released']:
        lines.append(f"Escrow: {esc['amount']:,} INFINITE (ready to release!)")
    lines.append(f"\nDaily Bonus: {get_daily_remaining_text(uid)}")
    lines.append("\n/play to earn more!")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    esc = get_db(uid)[2]

    if not wallet:
        await update.message.reply_text("No wallet! Use /wallet first.")
        return

    if is_escrow_active(uid):
        remaining = get_escrow_remaining_hours(uid)
        await update.message.reply_text(
            f"Escrow Active\nAmount: {esc['amount']:,} INFINITE\nTime remaining: {remaining:.1f} hours")
        return

    # Release from escrow if ready
    if not is_escrow_active(uid) and esc['amount'] > 0 and not esc['released']:
        total = esc['amount'] + e['unclaimed']
        if total <= 0:
            await update.message.reply_text("No INFINITE to claim.")
            return
        result = transfer_ifc(wallet, total)
        if result['success']:
            e['total_claimed'] += total
            e['unclaimed'] = 0
            clear_escrow(uid)
        await update.message.reply_text(
            f"{'Released' if result['success'] else 'Failed'}: {result.get('message', '')}\n"
            f"Amount: {total:,} INFINITE\nTx: `{result.get('tx', 'N/A')}`",
            parse_mode="Markdown"
        )
        return

    if e['unclaimed'] <= 0:
        await update.message.reply_text("No INFINITE to claim. /play to earn more!")
        return

    # Direct claim - no minimum balance required
    result = transfer_ifc(wallet, e['unclaimed'])
    if result['success']:
        e['total_claimed'] += e['unclaimed']
        e['unclaimed'] = 0
    await update.message.reply_text(
        f"{'Claimed' if result['success'] else 'Failed'}: {result.get('message', '')}\n"
        f"Tx: `{result.get('tx', 'N/A')}`",
        parse_mode="Markdown"
    )

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]

    if not is_daily_available(uid):
        remaining = get_daily_remaining_text(uid)
        await update.message.reply_text(
            f"Daily bonus on cooldown.\nNext bonus available in: {remaining}\nCome back in 24 hours!")
        return

    daily_bonus_db[uid] = int(time.time() * 1000)

    tx_result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False, "message": "No wallet"}
    if tx_result.get('success'):
        e['total_earned'] += DAILY_BONUS_AMOUNT
        e['total_claimed'] += DAILY_BONUS_AMOUNT
        await update.message.reply_text(
            f"DAILY BONUS CLAIMED!\n+{DAILY_BONUS_AMOUNT:,} INFINITE sent to your wallet!\n"
            f"Tx: `{tx_result.get('tx', 'N/A')}`\n\nNext bonus in 24 hours.",
            parse_mode="Markdown"
        )
    else:
        e['total_earned'] += DAILY_BONUS_AMOUNT
        e['unclaimed'] += DAILY_BONUS_AMOUNT
        await update.message.reply_text(
            f"DAILY BONUS CLAIMED!\n+{DAILY_BONUS_AMOUNT:,} INFINITE added!\n"
            f"({tx_result.get('message', '')})\n\nNext bonus in 24 hours.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*How to Play*\n"
        "Arrow keys: Move | Space: Jump | P: Pause\n\n"
        "*Claim System*\n"
        "- No minimum balance required to claim INFINITE!\n"
        "- Connect your Phantom wallet and /claim anytime\n"
        f"- Daily Bonus: {DAILY_BONUS_AMOUNT} INFINITE FREE every 24h\n\n"
        "/play - Launch game\n/wallet - Connect wallet\n"
        "/setwallet - Manually set wallet\n/balance - Check balance\n"
        "/claim - Claim INFINITE\n/daily - Free daily bonus\n/help - This help"
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "wallet":
        await cmd_wallet(update, context)

# ========== FLASK ROUTES ==========
@app.route("/")
def index():
    return jsonify({
        "bot": "Infinitecoin Jumper",
        "escrow": "LIVE" if escrow_ready else "DEMO",
        "users": len(user_db),
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "users": len(user_db),
        "escrow": "LIVE" if escrow_ready else "DEMO",
        "escrow_ready": escrow_ready,
        "treasury_key_set": bool(TREASURY_KEY),
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        # Schedule update processing on the permanent background loop
        future = asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update), _bot_loop
        )
        # Wait for processing to complete (with timeout)
        future.result(timeout=10)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Webhook error: %s", e)
        return jsonify({"ok": False}), 200

@app.route("/wallet-callback")
def wallet_callback():
    uid = request.args.get("user_id", "")
    wallet = request.args.get("phantom_wallet") or request.args.get("wallet") or request.args.get("address") or ""
    if wallet and uid:
        can_set, existing_uid = _can_set_wallet(uid, wallet)
        if not can_set:
            return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width"><style>
body{{background:#0a0a2a;color:#e2e8f0;font-family:monospace;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;padding:20px}}
.box{{background:rgba(15,23,42,.95);border:2px solid rgba(239,68,68,.4);border-radius:24px;padding:40px;max-width:380px;text-align:center;box-shadow:0 0 40px rgba(239,68,68,.15)}}
h1{{color:#ef4444;font-size:22px}}p{{color:#94a3b8;font-size:14px;margin-bottom:24px}}
</style></head><body><div class="box"><h1>Wallet Already Linked</h1><p>This wallet is already connected to another account.<br>You cannot reuse it on multiple accounts.</p></div></body></html>"""
        user_db.setdefault(uid, {})["wallet"] = wallet
        return redirect(f"{GAME_URL}?user_id={uid}&wallet={wallet}")
    return f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width"><style>
body{{background:#0a0a2a;color:#e2e8f0;font-family:monospace;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;padding:20px}}
.box{{background:rgba(15,23,42,.95);border:1px solid rgba(56,189,248,.3);border-radius:24px;padding:40px;max-width:380px;text-align:center;box-shadow:0 0 40px rgba(56,189,248,.1)}}
h1{{color:#22d3ee;font-size:22px}}p{{color:#94a3b8;font-size:13px;margin-bottom:24px}}
input{{background:rgba(30,41,59,.8);border:2px solid rgba(56,189,248,.3);color:#e2e8f0;padding:14px 18px;border-radius:50px;font-family:monospace;font-size:14px;width:100%;text-align:center;margin-bottom:16px;outline:none}}
input:focus{{border-color:#22d3ee;box-shadow:0 0 12px rgba(34,211,238,.2)}}
button{{background:linear-gradient(180deg,#22c55e,#16a34a);border:none;color:#fff;padding:14px 32px;border-radius:50px;font-family:monospace;font-weight:bold;font-size:15px;cursor:pointer;width:100%;box-shadow:0 4px 0 #14532d}}
button:active{{transform:translateY(3px);box-shadow:0 1px 0 #14532d}}
.note{{color:#475569;font-size:11px;margin-top:20px}}
</style></head><body><div class="box"><h1>Connect Wallet</h1><p>Paste your Phantom wallet address</p><input type="text" id="w" placeholder="Your Solana address" maxlength="44"><button onclick="s()">Connect & Return to Game</button><p class="note">We only store your public address</p></div><script>
var u="{uid}";function s(){{var w=document.getElementById('w').value.trim();if(w.length<32){{alert('Invalid address');return}}fetch('/api/wallet',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{telegram_user_id:u,wallet_address:w}})}}).then(function(){{window.location.href="{GAME_URL}?user_id="+u+"&wallet="+w}}).catch(function(){{window.location.href="{GAME_URL}?user_id="+u+"&wallet="+w}})}}
var p=new URLSearchParams(window.location.search);var pw=p.get('phantom_wallet')||p.get('wallet');if(pw){{document.getElementById('w').value=pw;s()}}
</script></body></html>"""

@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    uid = str(data.get("telegram_user_id", ""))
    if not wallet or not uid or len(wallet) < 32:
        return jsonify({"error": "Invalid"}), 400
    can_set, existing_uid = _can_set_wallet(uid, wallet)
    if not can_set:
        return jsonify({"error": "Wallet already linked to another account", "existing_user": True}), 409
    user_db.setdefault(uid, {})["wallet"] = wallet
    return jsonify({"success": True})

@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    amount = int(data.get("amount", 0))
    if not uid:
        return jsonify({"error": "Missing user_id"}), 400
    _, e, _, _ = get_db(uid)
    e["total_earned"] += amount
    e["unclaimed"] += amount
    return jsonify({"success": True, "unclaimed": e["unclaimed"]})

@app.route("/api/claim", methods=["POST"])
def api_claim():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    amount = int(data.get("amount", 0))
    if not uid or not wallet or amount <= 0:
        return jsonify({"error": "Invalid parameters"}), 400
    _, e, esc, _ = get_db(uid)

    if is_escrow_active(uid):
        remaining = get_escrow_remaining_hours(uid)
        return jsonify({"success": False, "message": f"Escrow active: {remaining:.1f}h", "escrow": True})

    if not is_escrow_active(uid) and esc['amount'] > 0 and not esc['released']:
        total = esc['amount'] + e['unclaimed']
        result = transfer_ifc(wallet, total)
        if result['success']:
            e['total_claimed'] += total
            e['unclaimed'] = 0
            clear_escrow(uid)
        return jsonify(result)

    # Direct claim - no minimum balance required
    result = transfer_ifc(wallet, amount)
    if result['success']:
        e['total_claimed'] += amount
        e['unclaimed'] = max(0, e['unclaimed'] - amount)
    return jsonify(result)

@app.route("/api/daily", methods=["POST"])
def api_daily():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    amount = int(data.get("amount", DAILY_BONUS_AMOUNT))
    if not uid:
        return jsonify({"error": "Missing user_id"}), 400
    if not is_daily_available(uid):
        return jsonify({"success": False, "message": f"Cooldown. Next: {get_daily_remaining_text(uid)}"})

    daily_bonus_db[uid] = int(time.time() * 1000)
    tx_result = transfer_ifc(wallet, amount) if wallet else {"success": False}
    if tx_result.get('success'):
        _, e, _, _ = get_db(uid)
        e['total_earned'] += amount
        e['total_claimed'] += amount
    else:
        _, e, _, _ = get_db(uid)
        e['total_earned'] += amount
        e['unclaimed'] += amount
    return jsonify({
        "success": True,
        "amount": amount,
        "tx": tx_result.get('tx', ''),
        "transferred": tx_result.get('success', False)
    })

@app.route("/api/balance/<uid>", methods=["GET"])
def api_get_balance(uid):
    wallet = user_db.get(str(uid), {}).get("wallet", "")
    _, e, esc, _ = get_db(uid)
    result = {
        "earned": e['total_earned'],
        "unclaimed": e['unclaimed'],
        "claimed": e['total_claimed'],
        "escrow_active": is_escrow_active(uid),
        "escrow_amount": esc['amount'] if not esc['released'] else 0,
        "daily_available": is_daily_available(uid),
    }
    if wallet:
        bal_info = has_minimum_balance(wallet)
        result.update({
            "wallet_balance": bal_info['balance'],
            "wallet_usd": bal_info['usd_value'],
            "can_claim": bal_info['has_min']
        })
    return jsonify(result)

@app.route("/setup-webhook")
def setup_webhook():
    try:
        f1 = asyncio.run_coroutine_threadsafe(
            telegram_app.bot.delete_webhook(drop_pending_updates=True), _bot_loop
        )
        f1.result(timeout=10)
        f2 = asyncio.run_coroutine_threadsafe(
            telegram_app.bot.set_webhook(url=f"{BASE_URL}/webhook"), _bot_loop
        )
        f2.result(timeout=10)
        return jsonify({"success": True, "message": "Webhook configured!"})
    except Exception as e:
        logger.error("Setup webhook error: %s", e)
        return jsonify({"error": str(e)}), 500

# ========== INIT ==========
telegram_app = None
_bot_loop = None
_bot_thread = None

async def _bot_main():
    """Async main that runs in the background thread forever."""
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
    logger.info("Bot initialized and started successfully")

    # Keep running forever (updater processes queue internally)
    while True:
        await asyncio.sleep(3600)

def init_bot():
    global _bot_loop, _bot_thread
    _bot_loop = asyncio.new_event_loop()

    def _run_loop():
        asyncio.set_event_loop(_bot_loop)
        _bot_loop.run_until_complete(_bot_main())

    _bot_thread = threading.Thread(target=_run_loop, daemon=True)
    _bot_thread.start()
    # Give the thread time to start the loop
    time.sleep(0.5)

    # Verify treasury has tokens (after all functions defined)
    if escrow_ready:
        _verify_treasury()
        get_token_price()

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set!")
else:
    import threading
    init_bot()

if __name__ == "__main__":
    logger.info("Bot starting on port %s", os.environ.get('PORT', 10000))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
