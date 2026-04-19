"""
Infinitecoin Jumper Bot
"""
import os
import json
import logging
import base58
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.environ.get("BASE_URL", "https://infinitecoin-bot.onrender.com").rstrip("/")
GAME_URL = os.environ.get("GAME_URL", "https://infinitecoin-jumper.onrender.com").rstrip("/")
IFC_MINT = os.environ.get("IFC_MINT_ADDRESS", "C8KsvkMBuqmvX416MWTJGKW9S9MpKiUjmpnj1fhzpump")
TREASURY_KEY = os.environ.get("TREASURY_PRIVATE_KEY", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)
user_db = {}
earnings_db = {}
escrow_ready = False
try:
    from solana.rpc.api import Client
    from solana.transaction import Transaction
    from solders.pubkey import Pubkey
    from solders.keypair import Keypair
    from spl.token.instructions import transfer_checked, TransferCheckedParams
    from spl.token.instructions import get_associated_token_address, create_associated_token_account
    from spl.token.instructions import CreateAssociatedTokenAccountParams
    from spl.token.constants import TOKEN_PROGRAM_ID
    escrow_ready = bool(TREASURY_KEY)
    if escrow_ready:
        solana_client = Client(SOLANA_RPC)
        mint_pubkey = Pubkey.from_string(IFC_MINT)
        treasury_kp = Keypair.from_bytes(base58.b58decode(TREASURY_KEY))
        treasury_ata = get_associated_token_address(treasury_kp.pubkey(), mint_pubkey)
        logger.info("Escrow LIVE")
    else:
        logger.info("Escrow DEMO")
except ImportError:
    logger.info("Solana libs not installed")
def get_db(user_id):
    uid = str(user_id)
    if uid not in user_db:
        user_db[uid] = {}
    if uid not in earnings_db:
        earnings_db[uid] = {"total_earned": 0, "unclaimed": 0, "total_claimed": 0}
    return user_db[uid], earnings_db[uid]
def set_wallet(uid, wallet):
    get_db(uid)[0]["wallet"] = wallet
def transfer_ifc(recipient, amount):
    if not escrow_ready:
        return {"success": True, "tx": "demo_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"), "message": "Demo mode"}
    try:
        recipient_pk = Pubkey.from_string(recipient)
        recipient_ata = get_associated_token_address(recipient_pk, mint_pubkey)
        if not solana_client.get_account_info(recipient_ata).value:
            tx = Transaction()
            tx.add(create_associated_token_account(CreateAssociatedTokenAccountParams(payer=treasury_kp.pubkey(), owner=recipient_pk, mint=mint_pubkey, associated_token=recipient_ata, system_program=Pubkey.from_string("11111111111111111111111111111111"), spl_token=TOKEN_PROGRAM_ID)))
            solana_client.send_transaction(tx, treasury_kp)
        ix = transfer_checked(TransferCheckedParams(program_id=TOKEN_PROGRAM_ID, source=treasury_ata, mint=mint_pubkey, dest=recipient_ata, owner=treasury_kp.pubkey(), amount=int(amount * 1000000), decimals=6, signers=[]))
        tx = Transaction()
        tx.add(ix)
        result = solana_client.send_transaction(tx, treasury_kp)
        return {"success": True, "tx": str(result.value), "message": str(amount) + " IFC sent!"}
    except Exception as e:
        logger.error("Transfer error: " + str(e))
        return {"success": False, "tx": "", "message": str(e)}
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    wallet = get_db(uid)[0].get("wallet")
    wallet_text = "`" + wallet[:4] + "..." + wallet[-4] + "`" if wallet else "*Not connected*"
    await update.message.reply_text("*Infinitecoin Jumper*\n_Collect coins. Avoid viruses. Earn IFC._\n\nWallet: " + wallet_text + "\n\n/play - Launch game\n/wallet - Connect Phantom\n/balance - Check IFC\n/claim - Claim IFC\n/help - How to play", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Play Game", url=GAME_URL + "?user_id=" + uid)], [InlineKeyboardButton("Connect Wallet", callback_data="wallet")]]))
async def cmd_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text("Launch Infinitecoin Jumper:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Game", url=GAME_URL + "?user_id=" + uid)]]))
async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    existing = get_db(uid)[0].get("wallet")
    if existing:
        await update.message.reply_text("Wallet connected: `" + existing[:4] + "..." + existing[-4] + "`\nUse /claim to withdraw.", parse_mode="Markdown")
        return
    phantom_url = "https://phantom.app/ul/v1/connect?app_url=" + BASE_URL + "&redirect_link=" + BASE_URL + "/wallet-callback?user_id=" + uid
    await update.message.reply_text("*Connect Phantom Wallet*\n1. Tap Open Phantom\n2. Approve connection\n3. Return to game\n\nOr: `/setwallet YOUR_ADDRESS`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Phantom", url=phantom_url)]]))
async def cmd_setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: `/setwallet YOUR_ADDRESS`", parse_mode="Markdown")
        return
    wallet = context.args[0].strip()
    if len(wallet) < 32:
        await update.message.reply_text("Invalid address.")
        return
    set_wallet(uid, wallet)
    await update.message.reply_text("Wallet saved: `" + wallet[:4] + "..." + wallet[-4] + "`\nNow you can /claim!", parse_mode="Markdown")
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    e = get_db(uid)[1]
    wallet_line = "Wallet: `" + wallet[:4] + "..." + wallet[-4] + "`\n" if wallet else "Wallet: *Not connected*\n"
    await update.message.reply_text("*Your IFC*\n" + wallet_line + "Earned: " + str(e['total_earned']) + "\nUnclaimed: " + str(e['unclaimed']) + "\nClaimed: " + str(e['total_claimed']) + "\n\n/play to earn more!", parse_mode="Markdown")
async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    wallet = get_db(uid)[0].get("wallet")
    if not wallet:
        await update.message.reply_text("No wallet! Use /wallet first.")
        return
    e = get_db(uid)[1]
    if e["unclaimed"] <= 0:
        await update.message.reply_text("No IFC to claim. /play to earn more!")
        return
    result = transfer_ifc(wallet, e["unclaimed"])
    if result["success"]:
        e["total_claimed"] += e["unclaimed"]
        e["unclaimed"] = 0
    await update.message.reply_text(("Claimed: " if result['success'] else "Failed: ") + result.get('message', '') + "\nTx: `" + result.get('tx', 'N/A') + "`", parse_mode="Markdown")
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*How to Play*\nArrow keys: Move\nSpace: Jump (double tap = double jump)\nP: Pause\n\nCollect infinity coins for IFC. Avoid viruses. Grab gift boxes!\n\n/play /wallet /balance /claim /setwallet", parse_mode="Markdown")
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "wallet":
        await cmd_wallet(update, context)
@app.route("/")
def index():
    return jsonify({"bot": "Infinitecoin Jumper", "escrow": "LIVE" if escrow_ready else "DEMO", "users": len(user_db)})
@app.route("/health")
def health():
    return jsonify({"status": "ok", "escrow": "LIVE" if escrow_ready else "DEMO"})
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        telegram_app.create_task(telegram_app.process_update(update))
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Webhook error: " + str(e))
        return jsonify({"ok": False}), 200
@app.route("/wallet-callback")
def wallet_callback():
    uid = request.args.get("user_id", "")
    wallet = request.args.get("phantom_wallet") or request.args.get("wallet") or request.args.get("address") or ""
    if wallet and uid:
        set_wallet(uid, wallet)
        return redirect(GAME_URL + "?user_id=" + uid + "&wallet=" + wallet)
    return "<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width'><style>body{background:#0a0a2a;color:#e2e8f0;font-family:monospace;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;padding:20px}.box{background:rgba(15,23,42,.95);border:1px solid rgba(56,189,248,.3);border-radius:24px;padding:40px;max-width:380px;text-align:center}h1{color:#22d3ee;font-size:22px}p{color:#94a3b8;font-size:13px;margin-bottom:24px}input{background:rgba(30,41,59,.8);border:2px solid rgba(56,189,248,.3);color:#e2e8f0;padding:14px;border-radius:50px;font-family:monospace;font-size:14px;width:100%;text-align:center;margin-bottom:16px}button{background:linear-gradient(180deg,#22c55e,#16a34a);border:none;color:#fff;padding:14px 32px;border-radius:50px;font-family:monospace;font-weight:bold;font-size:15px;cursor:pointer;width:100%;box-shadow:0 4px 0 #14532d}</style></head><body><div class='box'><h1>Connect Wallet</h1><p>Paste your Phantom wallet address</p><input type='text' id='w' placeholder='Your Solana address' maxlength='44'><button onclick='s()'>Connect & Return to Game</button></div><script>function s(){var w=document.getElementById(\"w\").value.trim();if(w.length<32){alert(\"Invalid address\");return}window.location.href=\"" + GAME_URL + "?user_id=" + uid + "&wallet=\"+w}</script></body></html>"
@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    data = request.get_json() or {}
    wallet = data.get("wallet_address", "").strip()
    uid = str(data.get("telegram_user_id", ""))
    if not wallet or not uid or len(wallet) < 32:
        return jsonify({"error": "Invalid"}), 400
    set_wallet(uid, wallet)
    return jsonify({"success": True})
@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    data = request.get_json() or {}
    uid = str(data.get("telegram_user_id", ""))
    amount = int(data.get("amount", 0))
    if not uid:
        return jsonify({"error": "Missing user_id"}), 400
    _, e = get_db(uid)
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
        return jsonify({"error": "Invalid"}), 400
    result = transfer_ifc(wallet, amount)
    if result["success"]:
        _, e = get_db(uid)
        e["total_claimed"] += amount
        e["unclaimed"] = max(0, e["unclaimed"] - amount)
    return jsonify(result)
@app.route("/setup-webhook")
def setup_webhook():
    try:
        telegram_app.bot.delete_webhook(drop_pending_updates=True)
        telegram_app.bot.set_webhook(url=BASE_URL + "/webhook")
        return jsonify({"success": True, "message": "Webhook configured!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
def init_bot():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("play", cmd_play))
    telegram_app.add_handler(CommandHandler("wallet", cmd_wallet))
    telegram_app.add_handler(CommandHandler("setwallet", cmd_setwallet))
    telegram_app.add_handler(CommandHandler("balance", cmd_balance))
    telegram_app.add_handler(CommandHandler("claim", cmd_claim))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
    telegram_app.add_handler(CallbackQueryHandler(on_callback))
    telegram_app.initialize()
if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        exit(1)
    init_bot()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
