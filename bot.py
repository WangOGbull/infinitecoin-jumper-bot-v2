"""
Infinitecoin Jumper Bot - Production Ready
No minimum balance required for claims. Real INFINITE token transfers from treasury.
"""
import os, json, logging, time, requests, asyncio
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

# Token price for display only ($0.00000329 actual price)
IFC_PRICE_USD = 0.00000329
ESCROW_HOURS = 24
DAILY_BONUS_AMOUNT = 500

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

user_db = {}
earnings_db = {}
escrow_db = {}
daily_bonus_db = {}

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
        from solana.rpc.api import Client
        from solana.transaction import Transaction
        from solders.pubkey import Pubkey
        from solders.keypair import Keypair

        # Try primary import path
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
            logger.info("Solana imports loaded via spl.token.instructions")
        except ImportError:
            logger.warning("spl.token.instructions not available, trying fallback")
            try:
                from spl.token.core import _TokenCore
                from spl.token.constants import TOKEN_PROGRAM_ID as _tpid, ASSOCIATED_TOKEN_PROGRAM_ID
                TOKEN_PROGRAM_ID = _tpid

                def _gata_fallback(owner, mint):
                    seeds = [bytes(owner), bytes(ASSOCIATED_TOKEN_PROGRAM_ID), bytes(mint)]
                    result = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM_ID)
                    return result[0]
                get_associated_token_address = _gata_fallback
            except Exception:
                logger.error("Cannot load get_associated_token_address")
                return

        escrow_ready = bool(TREASURY_KEY and get_associated_token_address)
        if escrow_ready:
            solana_client = Client(SOLANA_RPC)
            mint_pubkey = Pubkey.from_string(IFC_MINT)
            treasury_kp = Keypair.from_base58_string(TREASURY_KEY)
            treasury_ata = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
            logger.info("ESCROW LIVE - Treasury: %s", treasury_kp.pubkey())
        else:
            logger.warning("ESCROW DEMO mode - set TREASURY_PRIVATE_KEY for live transfers")
    except ImportError as e:
        logger.error("Solana libraries not installed: %s", e)
    except Exception as e:
        logger.error("Solana init failed: %s", e)

_setup_solana()

telegram_app = None

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
        recipient_pk = Pubkey.from_string(wallet_address)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
        resp = solana_client.get_account_info_json_parsed(recipient_ata)
        if resp.value and hasattr(resp.value, 'data'):
            parsed = resp.value.data.parsed
            if parsed and 'info' in parsed:
                return float(parsed['info']['tokenAmount']['uiAmount'] or 0)
        return 0
    except Exception as e:
        logger.error("Balance check error: %s", e)
        return 0

def has_minimum_balance(wallet_address):
    """No minimum balance required - always returns True."""
    balance = get_wallet_balance(wallet_address)
    usd_value = balance * IFC_PRICE_USD
    return {"has_min": True, "balance": balance, "usd_value": usd_value}

def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {
            "success": True,
            "tx": f"demo_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "message": "Demo mode - transfer simulated"
        }
    try:
        from solana.transaction import Transaction
        recipient_pk = Pubkey.from_string(recipient)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)

        # Create recipient ATA if it doesn't exist
        if create_associated_token_account_idempotent and not solana_client.get_account_info(recipient_ata).value:
            tx = Transaction()
            tx.add(create_associated_token_account_idempotent(
                payer=treasury_kp.pubkey(),
                owner=recipient_pk,
                mint=mint_pubkey
            ))
            solana_client.send_transaction(tx, treasury_kp)

        # Transfer tokens (6 decimals)
        if transfer_checked and TransferCheckedParams and TOKEN_PROGRAM_ID:
            ix = transfer_checked(TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=treasury_ata,
                mint=mint_pubkey,
                dest=recipient_ata,
                owner=treasury_kp.pubkey(),
                amount=int(amount * 1_000_000),
                decimals=6,
                signers=[]
            ))
            tx = Transaction()
            tx.add(ix)
            result = solana_client.send_transaction(tx, treasury_kp)
            return {"success": True, "tx": str(result.value), "message": f"{amount:,} INFINITE sent!"}
        else:
            # Fallback: raw RPC transfer (less safe but works)
            from spl.token.client import Token
            token = Token(solana_client, mint_pubkey, TOKEN_PROGRAM_ID, treasury_kp)
            result = token.transfer(
                source=treasury_ata,
                dest=recipient_ata,
                owner=treasury_kp,
                amount=int(amount * 1_000_000),
            )
            return {"success": True, "tx": str(result), "message": f"{amount:,} INFINITE sent!"}
    except Exception as e:
        logger.error("Transfer error: %s", e)
        return {"success": False, "tx": "", "message": str(e)}

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
            f"Wallet connected: `{existing[:4]}...{existing[-4:]}`\n"
            f"Use /balance to check your INFINITE balance or /claim to withdraw.",
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
        telegram_app.create_task(telegram_app.process_update(update))
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Webhook error: %s", e)
        return jsonify({"ok": False}), 200

@app.route("/wallet-callback")
def wallet_callback():
    uid = request.args.get("user_id", "")
    wallet = request.args.get("phantom_wallet") or request.args.get("wallet") or request.args.get("address") or ""
    if wallet and uid:
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
        telegram_app.bot.delete_webhook(drop_pending_updates=True)
        result = telegram_app.bot.set_webhook(url=f"{BASE_URL}/webhook")
        return jsonify({"success": True, "message": "Webhook configured!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== INIT ==========
def init_bot():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("play", cmd_play))
    telegram_app.add_handler(CommandHandler("wallet", cmd_wallet))
    telegram_app.add_handler(CommandHandler("setwallet", cmd_setwallet))
    telegram_app.add_handler(CommandHandler("balance", cmd_balance))
    telegram_app.add_handler(CommandHandler("claim", cmd_claim))
    telegram_app.add_handler(CommandHandler("daily", cmd_daily))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
    telegram_app.add_handler(CallbackQueryHandler(on_callback))

    # Async init for Gunicorn compatibility
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(telegram_app.initialize())
        logger.info("Bot initialized successfully")
    except Exception as e:
        logger.warning("Async init warning (bot may still work): %s", e)

if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set!")
else:
    init_bot()

if __name__ == "__main__":
    logger.info("Bot starting on port %s", os.environ.get('PORT', 10000))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
