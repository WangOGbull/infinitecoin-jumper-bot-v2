"""
Infinitecoin Jumper Bot - Pure Requests (no dependency conflicts)
Features: $2 min balance check, 24hr escrow, daily bonus (500 IFC free)
Env vars: TELEGRAM_BOT_TOKEN, BASE_URL, GAME_URL, IFC_MINT_ADDRESS, TREASURY_PRIVATE_KEY, SOLANA_RPC_URL
"""
import os, json, logging, base58, time, requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "https://infinitecoin-jumper-bot-v2.onrender.com").rstrip("/")
GAME_URL = os.environ.get("GAME_URL", "https://candid-squirrel-c33256.netlify.app").rstrip("/")
IFC_MINT = os.environ.get("IFC_MINT_ADDRESS", "C8KsvkMBuqmvX416MWTJGKW9S9MpKiUjmpnj1fhzpump")
TREASURY_KEY = os.environ.get("TREASURY_PRIVATE_KEY", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

MIN_BALANCE_USD = 2.0
IFC_PRICE_USD = 0.01
MIN_IFC_BALANCE = int(MIN_BALANCE_USD / IFC_PRICE_USD)
ESCROW_HOURS = 24
DAILY_BONUS_AMOUNT = 500

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

user_db = {}
earnings_db = {}
escrow_db = {}
daily_bonus_db = {}

# ========== SOLANA ==========
escrow_ready = False
solana_client = None
mint_pubkey = None
treasury_kp = None
treasury_ata = None

try:
    from solana.rpc.api import Client
    from solana.transaction import Transaction
    from solders.pubkey import Pubkey
    from solders.keypair import Keypair
    from spl.token.instructions import transfer_checked, TransferCheckedParams, get_associated_token_address, create_associated_token_account, CreateAssociatedTokenAccountParams
    from spl.token.constants import TOKEN_PROGRAM_ID
    
    escrow_ready = bool(TREASURY_KEY)
    if escrow_ready:
        solana_client = Client(SOLANA_RPC)
        mint_pubkey = Pubkey.from_string(IFC_MINT)
        treasury_kp = Keypair.from_bytes(base58.b58decode(TREASURY_KEY))
        treasury_ata = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
        logger.info(f"Escrow LIVE - treasury: {treasury_kp.pubkey()}")
    else:
        logger.info("Escrow DEMO - set TREASURY_PRIVATE_KEY for live mode")
except ImportError as e:
    logger.info(f"Solana libs not installed: {e}")
    escrow_ready = False
except Exception as e:
    logger.error(f"Escrow init failed: {e}")
    escrow_ready = False

# ========== TELEGRAM API HELPERS ==========
def tg(method, payload=None):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=30)
        return r.json()
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
        return {"ok": False}

def send_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def answer_callback(query_id, text=None):
    payload = {"callback_query_id": query_id}
    if text:
        payload["text"] = text
    return tg("answerCallbackQuery", payload)

# ========== DATABASE ==========
def get_db(user_id):
    uid = str(user_id)
    if uid not in user_db:
        user_db[uid] = {}
    if uid not in earnings_db:
        earnings_db[uid] = {"total_earned": 0, "unclaimed": 0, "total_claimed": 0}
    if uid not in escrow_db:
        escrow_db[uid] = {"hold_time": 0, "amount": 0, "released": True}
    if uid not in daily_bonus_db:
        daily_bonus_db[uid] = 0
    return user_db[uid], earnings_db[uid], escrow_db[uid], daily_bonus_db[uid]

# ========== SOLANA FUNCTIONS ==========
def get_wallet_balance(wallet_address):
    if not escrow_ready:
        return 0
    try:
        recipient_pk = Pubkey.from_string(wallet_address)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
        resp = solana_client.get_account_info_json_parsed(recipient_ata)
        if resp.value and resp.value.data and resp.value.data.parsed:
            info = resp.value.data.parsed.get("info", {})
            return float(info.get("tokenAmount", {}).get("uiAmount", 0))
        return 0
    except Exception as e:
        logger.error(f"Balance check error: {e}")
        return 0

def has_minimum_balance(wallet_address):
    balance = get_wallet_balance(wallet_address)
    usd_value = balance * IFC_PRICE_USD
    return {"has_min": usd_value >= MIN_BALANCE_USD, "balance": balance, "usd_value": usd_value}

def transfer_ifc(recipient, amount):
    if not escrow_ready:
        logger.info(f"DEMO transfer: {amount} IFC to {recipient[:10]}...")
        return {"success": True, "tx": f"demo_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}", "message": "Demo mode - transfer simulated"}
    try:
        logger.info(f"Transferring {amount} IFC to {recipient[:10]}...")
        recipient_pk = Pubkey.from_string(recipient)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
        if not solana_client.get_account_info(recipient_ata).value:
            logger.info(f"Creating ATA for {recipient[:10]}...")
            tx = Transaction()
            tx.add(create_associated_token_account(CreateAssociatedTokenAccountParams(
                payer=treasury_kp.pubkey(), owner=recipient_pk, mint=mint_pubkey,
                associated_token=recipient_ata, system_program=Pubkey.from_string("11111111111111111111111111111111"),
                spl_token=TOKEN_PROGRAM_ID)))
            solana_client.send_transaction(tx, treasury_kp)
        ix = transfer_checked(TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID, source=treasury_ata, mint=mint_pubkey,
            dest=recipient_ata, owner=treasury_kp.pubkey(), amount=int(amount * 1_000_000),
            decimals=6, signers=[]))
        tx = Transaction()
        tx.add(ix)
        result = solana_client.send_transaction(tx, treasury_kp)
        logger.info(f"Transfer success: {result.value}")
        return {"success": True, "tx": str(result.value), "message": f"{amount:,} IFC sent!"}
    except Exception as e:
        logger.error(f"Transfer error: {e}")
        return {"success": False, "tx": "", "message": str(e)}

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

# ========== COMMAND HANDLERS ==========
def handle_start(data):
    chat = data["chat"]["id"]
    user = data["from"]["id"]
    uid = str(user)
    _, e, esc, _ = get_db(uid)
    wallet = user_db.get(uid, {}).get("wallet", "")
    wallet_text = f"`{wallet[:4]}...{wallet[-4]}`" if wallet else "*Not connected*"
    
    lines = [
        "*Infinitecoin Jumper*",
        "_Collect coins. Avoid viruses. Earn IFC._",
        "",
        f"Wallet: {wallet_text}",
        f"Earned: {e['total_earned']:,} IFC",
        f"Unclaimed: {e['unclaimed']:,} IFC",
    ]
    if is_escrow_active(uid):
        esc_data = escrow_db.get(uid, {})
        remaining = get_escrow_remaining_hours(uid)
        lines.append(f"Escrow: {esc_data.get('amount', 0):,} IFC ({remaining:.1f}h left)")
    lines.extend([
        "",
        "/play - Launch game",
        "/wallet - Connect Phantom",
        "/balance - Check IFC & balance",
        "/claim - Claim IFC",
        "/daily - Daily bonus (500 IFC)",
        "/help - How to play",
    ])
    
    keyboard = {
        "inline_keyboard": [
            [{"text": "Play Game", "url": f"{GAME_URL}?user_id={uid}"}],
            [{"text": "Connect Wallet", "callback_data": "wallet"}],
        ]
    }
    send_message(chat, "\n".join(lines), reply_markup=keyboard)

def handle_play(data):
    chat = data["chat"]["id"]
    uid = str(data["from"]["id"])
    keyboard = {"inline_keyboard": [[{"text": "Open Game", "url": f"{GAME_URL}?user_id={uid}"}]]}
    send_message(chat, "Launch Infinitecoin Jumper:", reply_markup=keyboard)

def handle_wallet(data):
    chat = data["chat"]["id"]
    uid = str(data["from"]["id"])
    existing = get_db(uid)[0].get("wallet", "")
    if existing:
        send_message(chat, f"Wallet connected: `{existing[:4]}...{existing[-4]}`\nUse /balance to check your IFC balance or /claim to withdraw.", parse_mode="Markdown")
        return
    phantom_url = f"https://phantom.app/ul/v1/connect?app_url={BASE_URL}&redirect_link={BASE_URL}/wallet-callback?user_id={uid}"
    keyboard = {"inline_keyboard": [[{"text": "Open Phantom", "url": phantom_url}]]}
    send_message(chat, "*Connect Phantom Wallet*\n1. Tap Open Phantom\n2. Approve connection\n3. Return to bot\n\nOr manually: `/setwallet YOUR_ADDRESS`", reply_markup=keyboard)

def handle_setwallet(data, args):
    chat = data["chat"]["id"]
    uid = str(data["from"]["id"])
    if not args:
        send_message(chat, "Usage: `/setwallet YOUR_SOLANA_ADDRESS`")
        return
    wallet = args[0].strip()
    if len(wallet) < 32:
        send_message(chat, "Invalid address. Must be 32-44 characters.")
        return
    user_db[uid]["wallet"] = wallet
    send_message(chat, f"Wallet saved: `{wallet[:4]}...{wallet[-4]}`\nNow you can /claim or check /balance!", parse_mode="Markdown")

def handle_balance(data):
    chat = data["chat"]["id"]
    uid = str(data["from"]["id"])
    wallet = get_db(uid)[0].get("wallet", "")
    e = get_db(uid)[1]
    esc = get_db(uid)[2]
    
    lines = ["*Your IFC Status*"]
    if wallet:
        lines.append(f"Wallet: `{wallet[:4]}...{wallet[-4]}`")
        bal_info = has_minimum_balance(wallet)
        lines.append(f"Wallet Balance: {bal_info['balance']:,.2f} IFC (${bal_info['usd_value']:.2f})")
        lines.append(f"Min Required: {MIN_IFC_BALANCE:,} IFC (${MIN_BALANCE_USD})")
        lines.append(f"Status: {'Can claim' if bal_info['has_min'] else 'Need more IFC for claiming'}")
    else:
        lines.append(f"Wallet: *Not connected*")
    lines.extend([
        f"Earned: {e['total_earned']:,} IFC",
        f"Unclaimed: {e['unclaimed']:,} IFC",
        f"Claimed: {e['total_claimed']:,} IFC",
    ])
    if is_escrow_active(uid):
        remaining = get_escrow_remaining_hours(uid)
        lines.append(f"Escrow: {esc['amount']:,} IFC ({remaining:.1f}h remaining)")
    elif esc['amount'] > 0 and not esc['released']:
        lines.append(f"Escrow: {esc['amount']:,} IFC (ready to release!)")
    lines.append(f"\nDaily Bonus: {get_daily_remaining_text(uid)}")
    lines.append(f"\n/play to earn more!")
    send_message(chat, "\n".join(lines), parse_mode="Markdown")

def handle_claim(data):
    chat = data["chat"]["id"]
    uid = str(data["from"]["id"])
    wallet = get_db(uid)[0].get("wallet", "")
    e = get_db(uid)[1]
    esc = get_db(uid)[2]
    
    if not wallet:
        send_message(chat, "No wallet! Use /wallet first.")
        return
    
    if is_escrow_active(uid):
        remaining = get_escrow_remaining_hours(uid)
        send_message(chat, f"Escrow Active\nAmount: {esc['amount']:,} IFC\nTime remaining: {remaining:.1f} hours\n\nFund your wallet with ${MIN_BALANCE_USD} worth of IFC and try again after the hold period.")
        return
    
    if not is_escrow_active(uid) and esc['amount'] > 0 and not esc['released']:
        bal_info = has_minimum_balance(wallet)
        if bal_info['has_min']:
            total = esc['amount'] + e['unclaimed']
            if total <= 0:
                send_message(chat, "No IFC to claim.")
                return
            result = transfer_ifc(wallet, total)
            if result['success']:
                e['total_claimed'] += total
                e['unclaimed'] = 0
                clear_escrow(uid)
            send_message(chat, f"{'Released' if result['success'] else 'Failed'}: {result.get('message', '')}\nAmount: {total:,} IFC (includes escrow)\nTx: `{result.get('tx', 'N/A')}`", parse_mode="Markdown")
            return
        else:
            total = esc['amount'] + e['unclaimed']
            if total > 0:
                start_escrow(uid, total)
                e['unclaimed'] = 0
            send_message(chat, f"Insufficient Balance\nYour balance: {bal_info['balance']:,.2f} IFC (${bal_info['usd_value']:.2f})\nRequired: {MIN_IFC_BALANCE:,} IFC (${MIN_BALANCE_USD})\n\nYour claim ({total:,} IFC) has been held in escrow for 24 hours.\nFund your wallet and try again.")
            return
    
    if e['unclaimed'] <= 0:
        send_message(chat, "No IFC to claim. /play to earn more!")
        return
    
    bal_info = has_minimum_balance(wallet)
    if not bal_info['has_min']:
        start_escrow(uid, e['unclaimed'])
        e['unclaimed'] = 0
        send_message(chat, f"Claim Held in Escrow\nYour balance: {bal_info['balance']:,.2f} IFC (${bal_info['usd_value']:.2f})\nRequired: {MIN_IFC_BALANCE:,} IFC (${MIN_BALANCE_USD})\n\nYour claim ({escrow_db[uid]['amount']:,} IFC) is held for {ESCROW_HOURS} hours.\nFund your wallet with ${MIN_BALANCE_USD} worth of IFC and click /claim again after {ESCROW_HOURS}h.")
        return
    
    result = transfer_ifc(wallet, e['unclaimed'])
    if result['success']:
        e['total_claimed'] += e['unclaimed']
        e['unclaimed'] = 0
    send_message(chat, f"{'Claimed' if result['success'] else 'Failed'}: {result.get('message', '')}\nTx: `{result.get('tx', 'N/A')}`", parse_mode="Markdown")

def handle_daily(data):
    chat = data["chat"]["id"]
    uid = str(data["from"]["id"])
    wallet = get_db(uid)[0].get("wallet", "")
    e = get_db(uid)[1]
    
    if not is_daily_available(uid):
        remaining = get_daily_remaining_text(uid)
        send_message(chat, f"Daily bonus on cooldown.\nNext bonus available in: {remaining}\nCome back in 24 hours!")
        return
    
    daily_bonus_db[uid] = int(time.time() * 1000)
    
    # Transfer 500 IFC from treasury to player wallet
    tx_result = {"success": False, "message": "No wallet"}
    if wallet:
        tx_result = transfer_ifc(wallet, DAILY_BONUS_AMOUNT)
        if tx_result.get('success'):
            e['total_earned'] += DAILY_BONUS_AMOUNT
            e['total_claimed'] += DAILY_BONUS_AMOUNT
    else:
        # No wallet - add to unclaimed
        e['total_earned'] += DAILY_BONUS_AMOUNT
        e['unclaimed'] += DAILY_BONUS_AMOUNT
    
    msg = f"DAILY BONUS CLAIMED!\n+{DAILY_BONUS_AMOUNT:,} IFC"
    if tx_result.get('success') and wallet:
        msg += f"\nSent to: `{wallet[:4]}...{wallet[-4]}`\nTx: `{tx_result.get('tx', 'N/A')}`"
    else:
        msg += f"\nAdded to your unclaimed balance.\nUse /claim to withdraw."
    msg += "\n\nNext bonus in 24 hours."
    
    send_message(chat, msg, parse_mode="Markdown")

def handle_help(data):
    chat = data["chat"]["id"]
    send_message(chat,
        "*How to Play*\n"
        "Arrow keys: Move | Space: Jump (double = double jump) | P: Pause\n\n"
        "*Claim System*\n"
        f"- You need ${MIN_BALANCE_USD} worth of IFC in your wallet to claim\n"
        f"- If you don't have enough, claims are held {ESCROW_HOURS}h in escrow\n"
        f"- Fund your wallet and claim again to release\n"
        f"- Daily Bonus: {DAILY_BONUS_AMOUNT} IFC FREE every 24h (no $2 needed!)\n\n"
        "/play - Launch game\n"
        "/wallet - Connect Phantom wallet\n"
        "/setwallet - Manually set wallet\n"
        "/balance - Check IFC and wallet balance\n"
        "/claim - Claim IFC (needs $2 balance)\n"
        "/daily - Free daily bonus\n"
        "/help - Show this help"
    )

def handle_callback(data):
    query_id = data["id"]
    from_user = data["from"]["id"]
    callback_data = data.get("data", "")
    
    answer_callback(query_id)
    
    if callback_data == "wallet":
        handle_wallet({"chat": {"id": from_user}, "from": {"id": from_user}})

# ========== FLASK ROUTES ==========
@app.route("/")
def index():
    return jsonify({
        "bot": "Infinitecoin Jumper",
        "escrow": "LIVE" if escrow_ready else "DEMO",
        "escrow_ready": escrow_ready,
        "users": len(user_db),
        "min_balance_usd": MIN_BALANCE_USD,
        "escrow_hours": ESCROW_HOURS,
        "daily_bonus": DAILY_BONUS_AMOUNT
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "users": len(user_db),
        "escrow": "LIVE" if escrow_ready else "DEMO",
        "escrow_ready": escrow_ready,
        "treasury_key_set": bool(TREASURY_KEY),
        "ifc_mint": IFC_MINT[:10] + "...",
        "claim_system": "active"
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        
        if "callback_query" in data:
            handle_callback(data["callback_query"])
        elif "message" in data and "text" in data["message"]:
            msg = data["message"]
            text = msg["text"].strip()
            parts = text.split()
            cmd = parts[0].lower().split("@")[0]  # Remove bot username
            args = parts[1:] if len(parts) > 1 else []
            
            handlers = {
                "/start": handle_start,
                "/play": handle_play,
                "/wallet": handle_wallet,
                "/setwallet": handle_setwallet,
                "/balance": handle_balance,
                "/claim": handle_claim,
                "/daily": handle_daily,
                "/help": handle_help,
            }
            
            if cmd in handlers:
                if cmd == "/setwallet":
                    handlers[cmd](msg, args)
                else:
                    handlers[cmd](msg)
        
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"ok": True})  # Always return 200 to Telegram

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
    """Process claim with $2 min balance check and escrow"""
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    amount = int(data.get("amount", 0))
    
    if not uid or not wallet or amount <= 0:
        return jsonify({"error": "Invalid parameters"}), 400
    
    _, e, esc, _ = get_db(uid)
    
    if is_escrow_active(uid):
        remaining = get_escrow_remaining_hours(uid)
        return jsonify({"success": False, "message": f"Escrow active: {remaining:.1f}h remaining", "escrow": True, "time_remaining_hours": remaining})
    
    if not is_escrow_active(uid) and esc['amount'] > 0 and not esc['released']:
        bal_info = has_minimum_balance(wallet)
        if bal_info['has_min']:
            total = esc['amount'] + e['unclaimed']
            result = transfer_ifc(wallet, total)
            if result['success']:
                e['total_claimed'] += total
                e['unclaimed'] = 0
                clear_escrow(uid)
            return jsonify(result)
        else:
            total = esc['amount'] + e['unclaimed']
            start_escrow(uid, total)
            e['unclaimed'] = 0
            return jsonify({"success": False, "message": f"Insufficient balance (${bal_info['usd_value']:.2f}). Need ${MIN_BALANCE_USD}. Claim held in escrow.", "escrow": True, "balance": bal_info['balance'], "usd_value": bal_info['usd_value']})
    
    bal_info = has_minimum_balance(wallet)
    if not bal_info['has_min']:
        start_escrow(uid, e['unclaimed'])
        e['unclaimed'] = 0
        return jsonify({"success": False, "message": f"Insufficient balance (${bal_info['usd_value']:.2f}). You need ${MIN_BALANCE_USD} worth of IFC. Claim held in escrow for {ESCROW_HOURS}h.", "escrow": True, "balance": bal_info['balance'], "usd_value": bal_info['usd_value']})
    
    result = transfer_ifc(wallet, amount)
    if result['success']:
        e['total_claimed'] += amount
        e['unclaimed'] = max(0, e['unclaimed'] - amount)
    return jsonify(result)

@app.route("/api/daily", methods=["POST"])
def api_daily():
    """Process daily bonus - Transfer 500 IFC from treasury to wallet"""
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    wallet = data.get("wallet_address", "").strip()
    amount = int(data.get("amount", DAILY_BONUS_AMOUNT))
    
    if not uid:
        return jsonify({"error": "Missing user_id"}), 400
    
    if not is_daily_available(uid):
        return jsonify({"success": False, "message": f"Daily bonus on cooldown. Next: {get_daily_remaining_text(uid)}"})
    
    daily_bonus_db[uid] = int(time.time() * 1000)
    
    # Transfer from treasury to player wallet
    tx_result = {"success": False, "message": "No wallet"}
    if wallet:
        tx_result = transfer_ifc(wallet, amount)
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
        "message": f"+{amount} IFC sent to your wallet!" if (tx_result.get('success') and wallet) else f"+{amount} IFC added!",
        "amount": amount,
        "tx": tx_result.get('tx', ''),
        "transferred": tx_result.get('success', False) and bool(wallet)
    })

@app.route("/api/balance/<uid>", methods=["GET"])
def api_get_balance(uid):
    wallet = user_db.get(str(uid), {}).get("wallet", "")
    _, e, esc, _ = get_db(uid)
    result = {
        "earned": e['total_earned'], "unclaimed": e['unclaimed'], "claimed": e['total_claimed'],
        "escrow_active": is_escrow_active(uid),
        "escrow_amount": esc['amount'] if not esc['released'] else 0,
        "daily_available": is_daily_available(uid),
        "daily_remaining": get_daily_remaining_text(uid),
        "min_balance_usd": MIN_BALANCE_USD, "escrow_hours": ESCROW_HOURS
    }
    if wallet:
        bal_info = has_minimum_balance(wallet)
        result['wallet_balance'] = bal_info['balance']
        result['wallet_usd'] = bal_info['usd_value']
        result['can_claim'] = bal_info['has_min']
    return jsonify(result)

@app.route("/setup-webhook")
def setup_webhook():
    try:
        requests.post(f"{TELEGRAM_API}/deleteWebhook", json={}, timeout=10)
        result = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": f"{BASE_URL}/webhook"}, timeout=10).json()
        return jsonify({"success": True, "message": "Webhook configured!", "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== INIT ==========
if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!"); exit(1)
    logger.info(f"Bot starting on port {os.environ.get('PORT', 10000)}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
