"""
Infinitecoin Jumper Bot - Simple Treasury Protection
Rules:
1. Everyone: 25K IFC max per 24h claim
2. First-time whale (100K+ unclaimed): 25% of balance, max 150K
3. After whale bonus: back to 25K/day
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

# ========== SIMPLE CAPS ==========
NORMAL_CAP = 25000          # Everyone: 25K/day
WHALE_THRESHOLD = 100000    # 100K+ = whale
WHALE_PERCENT = 0.25        # 25% first claim
WHALE_MAX = 150000          # Max 150K whale claim
COOLDOWN_HOURS = 24         # 24h between claims

# Token price from DexScreener
IFC_PRICE_USD = None
_last_price_fetch = 0

def get_token_price():
    global IFC_PRICE_USD, _last_price_fetch
    now = time.time()
    if IFC_PRICE_USD is not None and (now - _last_price_fetch) < 300:
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
        logger.error("Price fetch error: %s", e)
    if IFC_PRICE_USD is None:
        IFC_PRICE_USD = 0.00000329
    return IFC_PRICE_USD

DAILY_BONUS_AMOUNT = 500
ESCROW_HOURS = 24

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ========== FILE STORAGE ==========
DATA_DIR = os.path.dirname(__file__) or "."

def _load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error("Load failed %s: %s", path, e)
    return default if default is not None else {}

def _save_json(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Save failed %s: %s", path, e)

USER_DB_FILE = os.path.join(DATA_DIR, "user_db.json")
EARNINGS_DB_FILE = os.path.join(DATA_DIR, "earnings_db.json")
ESCROW_DB_FILE = os.path.join(DATA_DIR, "escrow_db.json")
DAILY_DB_FILE = os.path.join(DATA_DIR, "daily_db.json")
HIGH_SCORES_FILE = os.path.join(DATA_DIR, "high_scores.json")

def get_db(user_id):
    uid = str(user_id)
    user_db.setdefault(uid, {})
    earnings_db.setdefault(uid, {
        "total_earned": 0, "unclaimed": 0, "total_claimed": 0,
        "last_claim_time": 0, "used_whale_bonus": False,
        "daily_claimed": {}
    })
    escrow_db.setdefault(uid, {"hold_time": 0, "amount": 0, "released": True})
    daily_bonus_db.setdefault(uid, 0)
    return user_db[uid], earnings_db[uid], escrow_db[uid], daily_bonus_db[uid]

user_db = _load_json(USER_DB_FILE, {})
earnings_db = _load_json(EARNINGS_DB_FILE, {})
escrow_db = _load_json(ESCROW_DB_FILE, {})
daily_bonus_db = _load_json(DAILY_DB_FILE, {})
high_scores_db = _load_json(HIGH_SCORES_FILE, {})

# ========== SOLANA SETUP ==========
escrow_ready = False
solana_client = None
mint_pubkey = None
treasury_kp = None
treasury_ata = None
get_associated_token_address = None
TOKEN_PROGRAM_ID = None
ASSOCIATED_TOKEN_PROGRAM_ID = None

def _setup_solana():
    global escrow_ready, solana_client, mint_pubkey, treasury_kp, treasury_ata
    global get_associated_token_address, TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID

    try:
        from solders.pubkey import Pubkey
        from solders.keypair import Keypair
    except ImportError:
        logger.error("solders not installed")
        return

    try:
        from solana.rpc.api import Client
    except ImportError:
        logger.error("solana.rpc not found")
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
        TOKEN_PROGRAM_ID = TOKEN_2022_PROGRAM if mint_owner == str(TOKEN_2022_PROGRAM) else DEFAULT_TOKEN_PROGRAM
    except Exception:
        TOKEN_PROGRAM_ID = DEFAULT_TOKEN_PROGRAM

    try:
        from spl.token.instructions import get_associated_token_address as _gata
        get_associated_token_address = _gata
    except ImportError:
        def _gata_fallback(owner, mint):
            seeds = [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)]
            result = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
            return result[0]
        get_associated_token_address = _gata_fallback

    escrow_ready = bool(TREASURY_KEY and get_associated_token_address)
    if escrow_ready:
        try:
            treasury_kp = Keypair.from_base58_string(TREASURY_KEY)
            _ta = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
            treasury_ata = _ta if isinstance(_ta, Pubkey) else Pubkey.from_string(str(_ta))
            logger.info("ESCROW LIVE: %s", treasury_kp.pubkey())
        except Exception as e:
            logger.error("Solana init failed: %s", e)
            escrow_ready = False
    else:
        logger.warning("ESCROW DEMO mode")

_setup_solana()

def get_treasury_balance():
    if not escrow_ready: return 0
    try:
        resp = solana_client.get_token_account_balance(treasury_ata)
        if hasattr(resp, 'value') and resp.value:
            return float(resp.value.ui_amount or 0)
        return 0
    except Exception:
        return 0

def get_wallet_balance(wallet_address):
    if not escrow_ready: return 0
    try:
        from solders.pubkey import Pubkey
        recipient_pk = Pubkey.from_string(wallet_address)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
        if not isinstance(recipient_ata, Pubkey):
            recipient_ata = Pubkey.from_string(str(recipient_ata))
        resp = solana_client.get_token_account_balance(recipient_ata)
        if hasattr(resp, 'value') and resp.value:
            return float(resp.value.ui_amount or 0)
        return 0
    except Exception:
        return 0

def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {"success": False, "tx": None, "message": "Treasury not ready"}
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
                data=b""
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

        tx = SoldersTx.new_signed_with_payer(instructions, treasury_kp.pubkey(), [treasury_kp], blockhash)
        tx_b64 = base64.b64encode(bytes(tx)).decode('utf-8')

        send_resp = requests.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 2,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        }, timeout=15).json()

        if 'result' in send_resp:
            return {"success": True, "tx": send_resp['result'], "message": f"Sent {amount:,} INFINITE"}
        else:
            return {"success": False, "tx": None, "message": f"RPC error: {send_resp.get('error', 'unknown')}"}
    except Exception as e:
        return {"success": False, "tx": None, "message": str(e)}

# ========== CLAIM LOGIC ==========
def calculate_claimable(unclaimed, used_whale_bonus):
    """Simple 3-rule system."""
    # Rule 3: Already used whale bonus = normal cap
    if used_whale_bonus:
        return min(unclaimed, NORMAL_CAP)

    # Rule 2: First time + 100K+ = whale bonus (25%, max 150K)
    if unclaimed >= WHALE_THRESHOLD:
        whale_amount = int(unclaimed * WHALE_PERCENT)
        return min(whale_amount, WHALE_MAX)

    # Rule 1: Normal player
    return min(unclaimed, NORMAL_CAP)

def is_escrow_active(uid):
    e = escrow_db.get(str(uid), {})
    if not e or e.get("amount", 0) <= 0:
        return False
    hours = (time.time() * 1000 - e.get("hold_time", 0)) / (1000 * 60 * 60)
    return hours < ESCROW_HOURS and not e.get("released", True)

def get_escrow_remaining(uid):
    e = escrow_db.get(str(uid), {})
    if not e.get("hold_time"): return 0
    elapsed = time.time() * 1000 - e["hold_time"]
    remaining = (ESCROW_HOURS * 60 * 60 * 1000) - elapsed
    return max(0, remaining / (1000 * 60 * 60))

def is_daily_available(uid):
    last = daily_bonus_db.get(str(uid), 0)
    if not last: return True
    return (time.time() * 1000 - last) / (1000 * 60 * 60) >= 24

# ========== TELEGRAM HANDLERS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    _, e, _, _ = get_db(uid)
    wallet = user_db.get(uid, {}).get("wallet")
    wallet_text = f"`{wallet[:4]}...{wallet[-4:]}`" if wallet else "*Not connected*"

    # Whale bonus status
    whale_msg = ""
    if not e.get("used_whale_bonus") and e["unclaimed"] >= WHALE_THRESHOLD:
        whale_amount = min(int(e["unclaimed"] * WHALE_PERCENT), WHALE_MAX)
        whale_msg = f"
🎉 Whale bonus available: {whale_amount:,} IFC!"

    lines = [
        "*Infinitecoin Jumper*",
        f"Wallet: {wallet_text}",
        f"Earned: {e['total_earned']:,} INFINITE",
        f"Unclaimed: {e['unclaimed']:,} INFINITE",
        whale_msg,
        f"
📅 Daily cap: {NORMAL_CAP:,} IFC",
        f"🐋 Whale threshold: {WHALE_THRESHOLD:,} IFC",
        f"
/play /wallet /claim /daily /balance"
    ]
    await update.message.reply_text("
".join(lines), parse_mode="Markdown",
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
        await update.message.reply_text(f"Wallet: `{existing[:4]}...{existing[-4:]}`", parse_mode="Markdown")
        return
    phantom_url = f"https://phantom.app/ul/v1/connect?app_url={BASE_URL}&redirect_link={BASE_URL}/wallet-callback?user_id={uid}"
    await update.message.reply_text("*Connect Phantom*
Or: `/setwallet ADDRESS`",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Phantom", url=phantom_url)]]))

async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: `/setwallet ADDRESS`", parse_mode="Markdown"); return
    wallet = context.args[0].strip()
    if len(wallet) < 32:
        await update.message.reply_text("Invalid address."); return
    user_db.setdefault(uid, {})["wallet"] = wallet
    _save_json(USER_DB_FILE, user_db)
    await update.message.reply_text("Wallet saved! Now /claim or /balance.", parse_mode="Markdown")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    lines = ["*Your INFINITE Status*"]
    if wallet:
        bal = get_wallet_balance(wallet)
        lines.append(f"Balance: {bal:,.2f} INFINITE")
    lines.extend([
        f"Earned: {e['total_earned']:,}",
        f"Unclaimed: {e['unclaimed']:,}",
        f"Claimed: {e['total_claimed']:,}",
        f"
Daily cap: {NORMAL_CAP:,} IFC",
        f"Whale bonus: {'Used' if e.get('used_whale_bonus') else 'Available'}"
    ])
    await update.message.reply_text("
".join(lines), parse_mode="Markdown")

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]

    if not wallet:
        await update.message.reply_text("No wallet! Use /wallet first."); return
    if e['unclaimed'] <= 0:
        await update.message.reply_text("No INFINITE to claim. /play to earn!"); return

    # 24h cooldown check
    last_claim = e.get("last_claim_time", 0)
    hours_since = (time.time() * 1000 - last_claim) / (1000 * 60 * 60) if last_claim else 999
    if hours_since < COOLDOWN_HOURS:
        remaining = COOLDOWN_HOURS - hours_since
        await update.message.reply_text(f"Cooldown: {remaining:.1f}h remaining."); return

    # Calculate claimable
    claimable = calculate_claimable(e['unclaimed'], e.get("used_whale_bonus", False))

    if claimable <= 0:
        await update.message.reply_text("Nothing to claim."); return

    result = transfer_ifc(wallet, claimable)

    if result.get('success'):
        e['total_claimed'] += claimable
        e['unclaimed'] -= claimable
        e["last_claim_time"] = int(time.time() * 1000)

        # Mark whale bonus used if this was a whale claim
        if claimable > NORMAL_CAP:
            e["used_whale_bonus"] = True

        _save_json(EARNINGS_DB_FILE, earnings_db)

        msg = f"Claimed {claimable:,} INFINITE!"
        if claimable > NORMAL_CAP:
            msg += f"
🎉 Whale bonus applied!"
        msg += f"
Tx: `{result.get('tx', 'N/A')}`"
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Claim failed: {result.get('message')}")

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    if not is_daily_available(uid):
        await update.message.reply_text("Daily bonus on cooldown."); return
    daily_bonus_db[uid] = int(time.time() * 1000)
    _save_json(DAILY_DB_FILE, daily_bonus_db)
    tx = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    if tx.get('success'):
        _, e, _, _ = get_db(uid)
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['total_claimed'] += DAILY_BONUS_AMOUNT
        _save_json(EARNINGS_DB_FILE, earnings_db)
        await update.message.reply_text(f"DAILY BONUS! +{DAILY_BONUS_AMOUNT:,} INFINITE!
Tx: `{tx.get('tx')}`", parse_mode="Markdown")
    else:
        _, e, _, _ = get_db(uid)
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['unclaimed'] += DAILY_BONUS_AMOUNT
        _save_json(EARNINGS_DB_FILE, earnings_db)
        await update.message.reply_text(f"Bonus added! ({tx.get('message', '')})")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "wallet": await cmd_wallet(update, context)

# ========== FLASK ROUTES ==========
@app.route("/")
def index():
    return jsonify({
        "bot": "Infinitecoin Jumper",
        "escrow": "LIVE" if escrow_ready else "DEMO",
        "users": len(user_db),
        "treasury_balance": get_treasury_balance(),
        "token_price_usd": get_token_price(),
        "normal_cap": NORMAL_CAP,
        "whale_threshold": WHALE_THRESHOLD,
        "whale_percent": WHALE_PERCENT,
        "whale_max": WHALE_MAX,
        "cooldown_hours": COOLDOWN_HOURS
    })

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
    wallet = request.args.get("phantom_wallet") or request.args.get("wallet") or ""
    if wallet and uid:
        user_db.setdefault(uid, {})["wallet"] = wallet
        _save_json(USER_DB_FILE, user_db)
        return redirect(f"{GAME_URL}?user_id={uid}&wallet={wallet}")
    return '<h1>Connect Wallet</h1><form>...</form>'

# ========== GAME API ==========
@app.route("/api/status", methods=["GET"])
def api_status():
    uid = request.args.get("user_id", "")
    resp = {
        "normal_cap": NORMAL_CAP,
        "whale_threshold": WHALE_THRESHOLD,
        "whale_percent": WHALE_PERCENT,
        "whale_max": WHALE_MAX,
        "cooldown_hours": COOLDOWN_HOURS,
        "treasury_balance": get_treasury_balance(),
        "token_price_usd": get_token_price()
    }
    if uid:
        _, e, _, _ = get_db(uid)
        last_claim = e.get("last_claim_time", 0)
        hours_since = (time.time() * 1000 - last_claim) / (1000 * 60 * 60) if last_claim else 999
        claimable = calculate_claimable(e['unclaimed'], e.get("used_whale_bonus", False))
        resp.update({
            "unclaimed": e['unclaimed'],
            "used_whale_bonus": e.get("used_whale_bonus", False),
            "can_claim": hours_since >= COOLDOWN_HOURS and e['unclaimed'] > 0,
            "cooldown_remaining": max(0, COOLDOWN_HOURS - hours_since),
            "claimable_now": claimable,
            "is_whale": e['unclaimed'] >= WHALE_THRESHOLD and not e.get("used_whale_bonus", False)
        })
    return jsonify(resp)

@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    uid = str(data.get("telegram_user_id", ""))
    if not wallet or not uid or len(wallet) < 32:
        return jsonify({"error": "Invalid"}), 400
    user_db.setdefault(uid, {})["wallet"] = wallet
    _save_json(USER_DB_FILE, user_db)
    return jsonify({"success": True})

@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    amount = int(data.get("amount", 0))
    if not uid or amount <= 0:
        return jsonify({"error": "Invalid"}), 400
    _, e, _, _ = get_db(uid)
    e["total_earned"] += amount
    e["unclaimed"] += amount
    _save_json(EARNINGS_DB_FILE, earnings_db)
    return jsonify({"success": True, "unclaimed": e["unclaimed"]})

@app.route("/api/claim", methods=["POST"])
def api_claim():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()

    if not uid or not wallet:
        return jsonify({"error": "Invalid"}), 400

    _, e, _, _ = get_db(uid)

    if e['unclaimed'] <= 0:
        return jsonify({"success": False, "message": "No IFC to claim"})

    # Cooldown check
    last_claim = e.get("last_claim_time", 0)
    hours_since = (time.time() * 1000 - last_claim) / (1000 * 60 * 60) if last_claim else 999
    if hours_since < COOLDOWN_HOURS:
        remaining = COOLDOWN_HOURS - hours_since
        return jsonify({"success": False, "message": f"Cooldown: {remaining:.1f}h", "cooldown": True})

    # Calculate claimable
    claimable = calculate_claimable(e['unclaimed'], e.get("used_whale_bonus", False))

    if claimable <= 0:
        return jsonify({"success": False, "message": "Nothing to claim"})

    result = transfer_ifc(wallet, claimable)

    if result.get('success'):
        e['total_claimed'] += claimable
        e['unclaimed'] -= claimable
        e["last_claim_time"] = int(time.time() * 1000)

        was_whale = claimable > NORMAL_CAP
        if was_whale:
            e["used_whale_bonus"] = True

        _save_json(EARNINGS_DB_FILE, earnings_db)

        return jsonify({
            "success": True,
            "tx": result.get("tx"),
            "amount": claimable,
            "was_whale": was_whale,
            "message": f"Sent {claimable:,} INFINITE" + (" (Whale bonus!)" if was_whale else "")
        })

    return jsonify({"success": False, "message": result.get("message", "Transfer failed")})

@app.route("/api/daily", methods=["POST"])
def api_daily():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    if not uid: return jsonify({"error": "Missing"}), 400
    if not is_daily_available(uid): return jsonify({"success": False, "message": "Cooldown"})
    daily_bonus_db[uid] = int(time.time() * 1000)
    _save_json(DAILY_DB_FILE, daily_bonus_db)
    result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT) if wallet else {"success": False}
    if result.get('success'):
        _, e, _, _ = get_db(uid)
        e['total_earned'] += DAILY_BONUS_AMOUNT; e['total_claimed'] += DAILY_BONUS_AMOUNT
        _save_json(EARNINGS_DB_FILE, earnings_db)
    return jsonify({"success": True, "tx": result.get("tx", ""), "transferred": result.get("success", False)})

@app.route("/api/balance/<uid>", methods=["GET"])
def api_balance(uid):
    _, e, _, _ = get_db(uid)
    claimable = calculate_claimable(e['unclaimed'], e.get("used_whale_bonus", False))
    return jsonify({
        "earned": e['total_earned'],
        "unclaimed": e['unclaimed'],
        "claimed": e['total_claimed'],
        "claimable_now": claimable,
        "used_whale_bonus": e.get("used_whale_bonus", False),
        "is_whale": e['unclaimed'] >= WHALE_THRESHOLD and not e.get("used_whale_bonus", False)
    })

# ========== LEADERBOARD ==========
@app.route("/api/score", methods=["POST"])
def api_score():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    distance = int(data.get("distance", 0))
    username = data.get("username", "Anonymous")
    if not wallet or len(wallet) < 32 or distance < 0:
        return jsonify({"error": "Invalid"}), 400
    existing = high_scores_db.get(wallet, {"best_distance": 0})
    if distance > existing.get("best_distance", 0):
        high_scores_db[wallet] = {"best_distance": distance, "username": username, "last_updated": time.time()}
        _save_json(HIGH_SCORES_FILE, high_scores_db)
        return jsonify({"success": True, "new_record": True, "best_distance": distance})
    return jsonify({"success": True, "new_record": False, "best_distance": existing.get("best_distance", 0)})

@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
    sorted_scores = sorted(high_scores_db.items(), key=lambda x: x[1].get("best_distance", 0), reverse=True)[:10]
    leaderboard = []
    for rank, (wallet, data) in enumerate(sorted_scores, 1):
        leaderboard.append({"rank": rank, "wallet": wallet[:4] + "..." + wallet[-4:], "full_wallet": wallet, "username": data.get("username", "Anonymous"), "distance": data.get("best_distance", 0)})
    return jsonify({"leaderboard": leaderboard, "total_players": len(high_scores_db)})

@app.route("/api/highscore/<wallet>", methods=["GET"])
def api_highscore(wallet):
    w = wallet.strip()
    if not w or len(w) < 32: return jsonify({"error": "Invalid"}), 400
    data = high_scores_db.get(w, {"best_distance": 0, "username": "Anonymous"})
    return jsonify({"best_distance": data.get("best_distance", 0), "username": data.get("username", "Anonymous")})

# ========== INIT ==========
telegram_app = None
_bot_loop = None

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
    telegram_app.add_handler(CallbackQueryHandler(on_callback))
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Bot started")
    while True: await asyncio.sleep(3600)

def init_bot():
    global _bot_loop
    _bot_loop = asyncio.new_event_loop()
    def _run():
        asyncio.set_event_loop(_bot_loop)
        _bot_loop.run_until_complete(_bot_main())
    threading.Thread(target=_run, daemon=True).start()
    time.sleep(0.5)

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set!")
else:
    init_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
