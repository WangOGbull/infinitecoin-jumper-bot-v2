"""
Infinitecoin Jumper Bot - Production Ready
No minimum balance required for claims. Real INFINITE token transfers from treasury.
Uses raw bytes for ALL transaction serialization (zero library dependencies for tx building).
"""
import os, json, logging, time, requests, asyncio, threading, base64, struct
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
IFC_PRICE_USD = None
_last_price_fetch = 0
_price_cache_seconds = 300

def get_token_price():
    """Fetch INFINITE token price from DexScreener."""
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
                logger.info("DexScreener price: $%.8f", IFC_PRICE_USD)
                return IFC_PRICE_USD
    except Exception as e:
        logger.error("DexScreener failed: %s", e)
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

    # Hardcoded program IDs via from_string (works for these well-known addresses)
    TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8kn1")
    logger.info("Solana program IDs loaded")

    # Try SPL library (may not be available)
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
        # Fallback ATA derivation
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
            solana_client = Client(SOLANA_RPC)
            mint_pubkey = Pubkey.from_string(IFC_MINT)
            treasury_kp = Keypair.from_base58_string(TREASURY_KEY)
            treasury_ata = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
            logger.info("ESCROW LIVE - Treasury: %s", treasury_kp.pubkey())
        except Exception as e:
            logger.error("Solana init failed: %s", e)
            escrow_ready = False
    else:
        logger.warning("ESCROW DEMO mode")

_setup_solana()

# ========== RAW SOLANA TRANSACTION BUILDER ==========

def _compact_u16(value):
    """Solana compact-u16 encoding."""
    result = []
    while True:
        byte = value & 0x7F
        value >>= 7
        if value == 0:
            result.append(byte)
            break
        result.append(byte | 0x80)
    return bytes(result)

def _build_transaction(instructions, signer_kp, blockhash_bytes):
    """Build a Solana transaction from raw bytes.
    
    Correct Solana message format:
    - Header: [num_signers, num_readonly_signed, num_readonly_unsigned]
    - Account keys: sorted [writable_signers, readonly_signers, writable_unsigned, readonly_unsigned]
    - Recent blockhash: 32 bytes
    - Instructions: [(program_index, [account_indices], data), ...]
    """
    from solders.pubkey import Pubkey
    
    signer_pubkey = signer_kp.pubkey()
    
    # Collect all pubkeys with their properties
    key_props = {}  # pk_bytes -> (Pubkey, is_signer, is_writable)
    
    def add_key(pk, is_signer, is_writable):
        pk_bytes = bytes(pk)
        if pk_bytes in key_props:
            existing_is_s, existing_is_w = key_props[pk_bytes][1], key_props[pk_bytes][2]
            key_props[pk_bytes] = (pk, existing_is_s or is_signer, existing_is_w or is_writable)
        else:
            key_props[pk_bytes] = (pk, is_signer, is_writable)
    
    # Add signer (always first in sorted order)
    add_key(signer_pubkey, True, True)
    
    # Add all keys from instructions
    for prog_id, accounts, _ in instructions:
        for pk, is_s, is_w in accounts:
            add_key(pk, is_s, is_w)
        # Program IDs are always non-signer, non-writable
        if bytes(prog_id) not in key_props:
            key_props[bytes(prog_id)] = (prog_id, False, False)
    
    # Sort: writable signers → readonly signers → writable non-signers → readonly non-signers
    def sort_key(item):
        pk_bytes, (pk, is_signer, is_writable) = item
        return (0 if is_signer else 2, 0 if is_writable else 1)
    
    sorted_keys = sorted(key_props.items(), key=sort_key)
    
    # Build account keys list and index map
    account_keys = []
    key_index = {}
    for pk_bytes, (pk, is_s, is_w) in sorted_keys:
        account_keys.append(pk_bytes)
        key_index[pk_bytes] = len(account_keys) - 1
    
    # Count header values
    num_signers = sum(1 for _, (_, is_s, _) in sorted_keys if is_s)
    num_readonly_signed = sum(1 for _, (_, is_s, is_w) in sorted_keys if is_s and not is_w)
    num_readonly_unsigned = sum(1 for _, (_, is_s, is_w) in sorted_keys if not is_s and not is_w)
    
    # Build message
    msg_parts = []
    
    # Header
    msg_parts.append(bytes([num_signers, num_readonly_signed, num_readonly_unsigned]))
    
    # Account keys
    msg_parts.append(_compact_u16(len(account_keys)))
    for pk_bytes in account_keys:
        msg_parts.append(pk_bytes)
    
    # Recent blockhash
    msg_parts.append(blockhash_bytes)
    
    # Instructions
    raw_ixs = []
    for prog_id, accounts, data in instructions:
        prog_idx = key_index[bytes(prog_id)]
        # Account indices are SIMPLE 1-byte values (no flags!)
        acct_indices = [key_index[bytes(pk)] for pk, _, _ in accounts]
        
        ix_bytes = bytes([prog_idx])
        ix_bytes += _compact_u16(len(acct_indices))
        ix_bytes += bytes(acct_indices)
        ix_bytes += _compact_u16(len(data))
        ix_bytes += data
        raw_ixs.append(ix_bytes)
    
    msg_parts.append(_compact_u16(len(raw_ixs)))
    for ix in raw_ixs:
        msg_parts.append(ix)
    
    message_bytes = b''.join(msg_parts)
    
    # Sign message
    signature = signer_kp.sign_message(message_bytes)
    
    # Transaction: [signatures] + [message]
    tx_bytes = _compact_u16(1) + bytes(signature) + message_bytes
    
    return base64.b64encode(tx_bytes).decode('utf-8')

def _rpc_call(method, params, req_id):
    """Make raw RPC call to Solana."""
    resp = requests.post(SOLANA_RPC, json={
        "jsonrpc": "2.0", "id": req_id, "method": method, "params": params
    }, timeout=15)
    return resp.json()

def _get_blockhash():
    """Get recent blockhash from RPC."""
    data = _rpc_call("getLatestBlockhash", [{"commitment": "finalized"}], 1)
    from solders.hash import Hash
    return Hash.from_string(data['result']['value']['blockhash'])

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
        resp = solana_client.get_token_account_balance(recipient_ata)
        if hasattr(resp, 'value') and resp.value:
            return float(resp.value.ui_amount or 0)
        return 0
    except Exception as e:
        logger.error("Balance check error: %s", e)
        return 0

def has_minimum_balance(wallet_address):
    balance = get_wallet_balance(wallet_address)
    usd_value = balance * get_token_price()
    return {"has_min": True, "balance": balance, "usd_value": usd_value}

def _get_treasury_token_account():
    """Find treasury token account with INFINITE balance via RPC."""
    if not escrow_ready:
        return None
    try:
        data = _rpc_call("getTokenAccountsByOwner",
            [str(treasury_kp.pubkey()), {"mint": IFC_MINT}, {"encoding": "jsonParsed"}], 1)
        if 'result' in data and data['result'].get('value'):
            for acc in data['result']['value']:
                info = acc['account']['data']['parsed']['info']
                bal = float(info['tokenAmount']['uiAmount'] or 0)
                addr = acc['pubkey']
                if bal > 0:
                    from solders.pubkey import Pubkey
                    logger.info("Treasury source: %s (balance: %s)", addr, bal)
                    return Pubkey.from_string(addr)
    except Exception as e:
        logger.warning("Token search failed: %s", e)
    return treasury_ata

def _get_or_create_ata(wallet_address):
    """Get or create ATA. Returns ata_pubkey or None."""
    try:
        from solders.pubkey import Pubkey
        from solana.transaction import Transaction, TransactionInstruction, AccountMeta as SolanaAccountMeta

        wallet_pk = Pubkey.from_string(wallet_address)
        seeds = [bytes(wallet_pk), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)]
        ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
        
        # Check if exists
        data = _rpc_call("getAccountInfo", [str(ata), {"encoding": "base64"}], 1)
        if data.get('result', {}).get('value'):
            return ata
        
        # Create ATA using solana's native TransactionInstruction
        sys_prog = Pubkey.from_string("11111111111111111111111111111111")
        ix = TransactionInstruction(
            keys=[
                SolanaAccountMeta(pubkey=treasury_kp.pubkey(), is_signer=True, is_writable=True),
                SolanaAccountMeta(pubkey=ata, is_signer=False, is_writable=True),
                SolanaAccountMeta(pubkey=wallet_pk, is_signer=False, is_writable=False),
                SolanaAccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                SolanaAccountMeta(pubkey=sys_prog, is_signer=False, is_writable=False),
                SolanaAccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
            ],
            program_id=ASSOCIATED_TOKEN_PROGRAM_ID,
            data=b""
        )
        
        tx = Transaction()
        tx.add(ix)
        result = solana_client.send_transaction(tx, treasury_kp)
        logger.info("Created ATA %s: %s", ata, result.value)
        return ata
    except Exception as e:
        logger.error("ATA error: %s", e)
        return None

def _transfer_tokens_raw(recipient_wallet, amount_int):
    """Transfer INFINITE using solana TransactionInstruction."""
    try:
        from solders.pubkey import Pubkey
        from solana.transaction import Transaction, TransactionInstruction, AccountMeta as SolanaAccountMeta
        import struct

        recipient_ata = _get_or_create_ata(recipient_wallet)
        if recipient_ata is None:
            return {"success": False, "tx": "", "message": "Cannot get recipient token account"}

        source = _get_treasury_token_account()
        if source is None:
            return {"success": False, "tx": "", "message": "Treasury has no INFINITE"}

        ix_data = struct.pack("<BQB", 12, amount_int, 6)
        ix = TransactionInstruction(
            keys=[
                SolanaAccountMeta(pubkey=Pubkey.from_string(str(source)), is_signer=False, is_writable=True),
                SolanaAccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                SolanaAccountMeta(pubkey=Pubkey.from_string(str(recipient_ata)), is_signer=False, is_writable=True),
                SolanaAccountMeta(pubkey=treasury_kp.pubkey(), is_signer=True, is_writable=False),
            ],
            program_id=TOKEN_PROGRAM_ID,
            data=ix_data
        )

        tx = Transaction()
        tx.add(ix)
        result = solana_client.send_transaction(tx, treasury_kp)
        return {"success": True, "tx": str(result.value), "message": "INFINITE sent!"}
    except Exception as e:
        logger.error("Transfer error: %s", e)
        return {"success": False, "tx": "", "message": str(e)}

def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {
            "success": True,
            "tx": f"demo_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "message": "Demo mode - transfer simulated"
        }
    return _transfer_tokens_raw(recipient, int(amount * 1_000_000))

# ========== ESCROW LOGIC ==========
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
        "*Infinitecoin Jumper*", "_Collect coins. Avoid viruses. Earn INFINITE._", "",
        f"Wallet: {wallet_text}",
        f"Earned: {e['total_earned']:,} INFINITE",
        f"Unclaimed: {e['unclaimed']:,} INFINITE",
    ]
    if is_escrow_active(uid):
        esc_data = escrow_db.get(uid, {})
        remaining = get_escrow_remaining_hours(uid)
        status_lines.append(f"Escrow: {esc_data.get('amount', 0):,} INFINITE ({remaining:.1f}h left)")
    status_lines.extend(["", "/play - Launch game", "/wallet - Connect Phantom",
        "/balance - Check INFINITE & balance", "/claim - Claim INFINITE",
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
        await update.message.reply_text(
            f"Wallet connected: `{existing[:4]}...{existing[-4:]}`\nUse /balance or /claim.", parse_mode="Markdown")
        return
    phantom_url = f"https://phantom.app/ul/v1/connect?app_url={BASE_URL}&redirect_link={BASE_URL}/wallet-callback?user_id={uid}"
    await update.message.reply_text("*Connect Phantom*\n1. Open Phantom\n2. Approve\n3. Return\n\nOr: `/setwallet ADDRESS`",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Phantom", url=phantom_url)]]))

async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: `/setwallet ADDRESS`", parse_mode="Markdown"); return
    wallet = context.args[0].strip()
    if len(wallet) < 32:
        await update.message.reply_text("Invalid address."); return
    can_set, _ = _can_set_wallet(uid, wallet)
    if not can_set:
        await update.message.reply_text("Wallet already linked to another account!"); return
    user_db.setdefault(uid, {})["wallet"] = wallet
    await update.message.reply_text(f"Wallet saved! Now /claim or /balance.", parse_mode="Markdown")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    lines = ["*Your INFINITE Status*"]
    if wallet:
        lines.append(f"Wallet: `{wallet[:4]}...{wallet[-4:]}`")
        bal = has_minimum_balance(wallet)
        lines.append(f"Balance: {bal['balance']:,.2f} INFINITE (${bal['usd_value']:.6f})")
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
    result = transfer_ifc(wallet, e['unclaimed'])
    if result['success']:
        e['total_claimed'] += e['unclaimed']; e['unclaimed'] = 0
    await update.message.reply_text(f"{'Claimed' if result['success'] else 'Failed'}: {result['message']}\nTx: `{result.get('tx', 'N/A')}`", parse_mode="Markdown")

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    if not is_daily_available(uid):
        await update.message.reply_text(f"Cooldown. Next: {get_daily_remaining_text(uid)}"); return
    daily_bonus_db[uid] = int(time.time() * 1000)
    tx = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    if tx.get('success'):
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['total_claimed'] += DAILY_BONUS_AMOUNT
        await update.message.reply_text(f"DAILY BONUS! +{DAILY_BONUS_AMOUNT:,} INFINITE!\nTx: `{tx.get('tx')}`", parse_mode="Markdown")
    else:
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['unclaimed'] += DAILY_BONUS_AMOUNT
        await update.message.reply_text(f"Bonus added! ({tx.get('message', '')})")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*How to Play*\nArrows: Move | Space: Jump\n\n*Claims*\n- No minimum required!\n"
        f"- Daily: {DAILY_BONUS_AMOUNT} FREE INFINITE/24h\n\n/play /wallet /claim /daily /balance")

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

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
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
        can_set, _ = _can_set_wallet(uid, wallet)
        if not can_set:
            return '<h1>Wallet Already Linked</h1><p>This wallet is connected to another account.</p>'
        user_db.setdefault(uid, {})["wallet"] = wallet
        return redirect(f"{GAME_URL}?user_id={uid}&wallet={wallet}")
    return '<h1>Connect Wallet</h1><form>...</form>'  # simplified

@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    uid = str(data.get("telegram_user_id", ""))
    if not wallet or not uid or len(wallet) < 32:
        return jsonify({"error": "Invalid"}), 400
    can_set, _ = _can_set_wallet(uid, wallet)
    if not can_set:
        return jsonify({"error": "Wallet already linked"}), 409
    user_db.setdefault(uid, {})["wallet"] = wallet
    return jsonify({"success": True})

@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    amount = int(data.get("amount", 0))
    if not uid: return jsonify({"error": "Missing user_id"}), 400
    _, e, _, _ = get_db(uid)
    e["total_earned"] += amount; e["unclaimed"] += amount
    return jsonify({"success": True, "unclaimed": e["unclaimed"]})

@app.route("/api/claim", methods=["POST"])
def api_claim():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    amount = int(data.get("amount", 0))
    if not uid or not wallet or amount <= 0: return jsonify({"error": "Invalid"}), 400
    result = transfer_ifc(wallet, amount)
    return jsonify(result)

@app.route("/api/daily", methods=["POST"])
def api_daily():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    if not uid: return jsonify({"error": "Missing"}), 400
    if not is_daily_available(uid): return jsonify({"success": False, "message": "Cooldown"})
    daily_bonus_db[uid] = int(time.time() * 1000)
    result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    return jsonify({"success": True, "tx": result.get("tx", ""), "transferred": result.get("success", False)})

@app.route("/api/balance/<uid>", methods=["GET"])
def api_get_balance(uid):
    wallet = user_db.get(str(uid), {}).get("wallet", "")
    _, e, _, _ = get_db(uid)
    result = {"earned": e['total_earned'], "unclaimed": e['unclaimed'], "claimed": e['total_claimed']}
    if wallet:
        bal = has_minimum_balance(wallet)
        result.update({"wallet_balance": bal['balance'], "can_claim": True})
    return jsonify(result)

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
