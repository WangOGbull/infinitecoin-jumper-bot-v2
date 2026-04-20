"""
Infinitecoin Jumper Bot
Uses direct HTTP to Telegram API — no async, no complex framework.
"""
import os
import json
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# ====== CONFIG (from environment variables) ======
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE = os.environ.get("BASE_URL", "").rstrip("/")
GAME = os.environ.get("GAME_URL", "").rstrip("/")
MINT = os.environ.get("IFC_MINT_ADDRESS", "")
TREASURY = os.environ.get("TREASURY_PRIVATE_KEY", "")
SOLANA_RPC = os.environ.get("SOLANA_RPC_URL", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
users = {}

# ====== FLASK ======
from flask import Flask, request, jsonify, redirect
app = Flask(__name__)


def tg(method, payload):
    """Send request to Telegram API."""
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return {}


@app.route("/")
def index():
    return jsonify({"bot": "Infinitecoin Jumper", "status": "running"})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram messages."""
    data = request.get_json(force=True)
    logger.info(f"Received: {json.dumps(data)[:200]}")

    msg = data.get("message", {})
    chat = msg.get("chat", {}).get("id", "")
    text = msg.get("text", "") or ""
    uid = str(msg.get("from", {}).get("id", ""))

    if not chat or not text:
        return jsonify({"ok": True})

    # ====== /start ======
    if text.startswith("/start"):
        wallet = users.get(uid, {}).get("wallet", "")
        wt = f"`{wallet[:4]}...{wallet[-4]}`" if wallet else "*Not connected*"
        tg("sendMessage", {
            "chat_id": chat,
            "text": f"*Infinitecoin Jumper*\n_Collect coins. Avoid viruses. Earn IFC._\n\n"
                    f"Wallet: {wt}\n\n"
                    f"/play - Launch game\n"
                    f"/wallet - Connect Phantom\n"
                    f"/balance - Check IFC\n"
                    f"/claim - Claim IFC\n"
                    f"/help - How to play",
            "parse_mode": "Markdown",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "Play Game", "url": f"{GAME}?user_id={uid}"}],
                    [{"text": "Connect Wallet", "callback_data": "wallet"}]
                ]
            })
        })

    # ====== /play ======
    elif text.startswith("/play"):
        tg("sendMessage", {
            "chat_id": chat,
            "text": "Launch Infinitecoin Jumper:",
            "reply_markup": json.dumps({
                "inline_keyboard": [
                    [{"text": "Open Game", "url": f"{GAME}?user_id={uid}"}]
                ]
            })
        })

    # ====== /wallet ======
    elif text.startswith("/wallet"):
        existing = users.get(uid, {}).get("wallet", "")
        if existing:
            tg("sendMessage", {
                "chat_id": chat,
                "text": f"Wallet connected: `{existing[:4]}...{existing[-4]}`\nUse /claim to withdraw.",
                "parse_mode": "Markdown"
            })
        else:
            url = f"https://phantom.app/ul/v1/connect?app_url={BASE}&redirect_link={BASE}/wallet-callback?user_id={uid}"
            tg("sendMessage", {
                "chat_id": chat,
                "text": "*Connect Phantom Wallet*\n1. Tap Open Phantom\n2. Approve connection\n3. Return to game\n\nOr: `/setwallet YOUR_ADDRESS`",
                "parse_mode": "Markdown",
                "reply_markup": json.dumps({
                    "inline_keyboard": [[{"text": "Open Phantom", "url": url}]]
                })
            })

    # ====== /setwallet ======
    elif text.startswith("/setwallet"):
        parts = text.split(" ", 1)
        if len(parts) < 2:
            tg("sendMessage", {
                "chat_id": chat,
                "text": "Usage: `/setwallet YOUR_ADDRESS`",
                "parse_mode": "Markdown"
            })
        else:
            w = parts[1].strip()
            if len(w) < 32:
                tg("sendMessage", {"chat_id": chat, "text": "Invalid address."})
            else:
                users[uid] = {"wallet": w, "earnings": {"total": 0, "unclaimed": 0}}
                tg("sendMessage", {
                    "chat_id": chat,
                    "text": f"Wallet saved: `{w[:4]}...{w[-4]}`\nNow you can /claim!",
                    "parse_mode": "Markdown"
                })

    # ====== /balance ======
    elif text.startswith("/balance"):
        w = users.get(uid, {}).get("wallet", "")
        e = users.get(uid, {}).get("earnings", {"total": 0, "unclaimed": 0})
        wl = f"Wallet: `{w[:4]}...{w[-4]}`\n" if w else "Wallet: *Not connected*\n"
        tg("sendMessage", {
            "chat_id": chat,
            "text": f"*Your IFC*\n{wl}Earned: {e['total']:,}\nUnclaimed: {e['unclaimed']:,}\n\n/play to earn more!",
            "parse_mode": "Markdown"
        })

    # ====== /claim ======
    elif text.startswith("/claim"):
        w = users.get(uid, {}).get("wallet", "")
        if not w:
            tg("sendMessage", {"chat_id": chat, "text": "No wallet! Use /wallet first."})
        else:
            e = users.get(uid, {}).get("earnings", {"unclaimed": 0})
            if e["unclaimed"] <= 0:
                tg("sendMessage", {"chat_id": chat, "text": "No IFC to claim. /play to earn more!"})
            else:
                amt = e["unclaimed"]
                tg("sendMessage", {
                    "chat_id": chat,
                    "text": f"Claimed {amt:,} IFC!\nWallet: `{w[:4]}...{w[-4]}`\nTx: pending",
                    "parse_mode": "Markdown"
                })
                e["unclaimed"] = 0

    # ====== /help ======
    elif text.startswith("/help"):
        tg("sendMessage", {
            "chat_id": chat,
            "text": "*How to Play*\nArrow keys: Move\nSpace: Jump (double = double jump)\nP: Pause\n\n"
                    "Collect infinity coins for IFC. Avoid viruses. Grab gift boxes!\n\n"
                    "/play /wallet /balance /claim /setwallet",
            "parse_mode": "Markdown"
        })

    return jsonify({"ok": True})


@app.route("/wallet-callback")
def wallet_callback():
    uid = request.args.get("user_id", "")
    w = request.args.get("phantom_wallet") or request.args.get("wallet") or ""
    if w and uid:
        if uid not in users:
            users[uid] = {}
        users[uid]["wallet"] = w
        return redirect(f"{GAME}?user_id={uid}&wallet={w}")
    return "<html><body style='background:#0a0a2a;color:#22d3ee;font-family:monospace;display:flex;justify-content:center;align-items:center;height:100vh'><div style='text-align:center'><h1>Connect Wallet</h1><p style='color:#94a3b8'>Paste your Phantom wallet address</p><input id='w' placeholder='Your Solana address' style='padding:14px;border-radius:50px;width:300px;text-align:center;border:2px solid rgba(56,189,248,.3);background:rgba(30,41,59,.8);color:#e2e8f0'><br><br><button onclick=\"var a=document.getElementById('w').value.trim();if(a.length>=32)window.location='" + GAME + "?user_id=" + uid + "&wallet='+a;else alert('Invalid')\" style='padding:14px 32px;border-radius:50px;background:linear-gradient(180deg,#22c55e,#16a34a);color:#fff;font-weight:bold;border:none;cursor:pointer;box-shadow:0 4px 0 #14532d;font-family:monospace;font-size:15px'>Connect & Return to Game</button></div></body></html>"


@app.route("/api/wallet", methods=["POST"])
def api_wallet():
    d = request.get_json() or {}
    w = d.get("wallet_address", "").strip()
    uid = str(d.get("telegram_user_id", ""))
    if w and uid and len(w) >= 32:
        if uid not in users:
            users[uid] = {}
        users[uid]["wallet"] = w
    return jsonify({"success": True})


@app.route("/api/earnings", methods=["POST"])
def api_earnings():
    d = request.get_json() or {}
    uid = str(d.get("telegram_user_id", ""))
    amt = int(d.get("amount", 0))
    if uid not in users:
        users[uid] = {"earnings": {"total": 0, "unclaimed": 0}}
    if "earnings" not in users[uid]:
        users[uid]["earnings"] = {"total": 0, "unclaimed": 0}
    users[uid]["earnings"]["total"] += amt
    users[uid]["earnings"]["unclaimed"] += amt
    return jsonify({"success": True, "unclaimed": users[uid]["earnings"]["unclaimed"]})


@app.route("/api/claim", methods=["POST"])
def api_claim():
    d = request.get_json() or {}
    uid = str(d.get("telegram_user_id", ""))
    w = d.get("wallet_address", "").strip()
    amt = int(d.get("amount", 0))
    return jsonify({"success": True, "amount": amt, "message": f"{amt:,} IFC claimed!"})


@app.route("/setup-webhook")
def setup_webhook():
    u = BASE + "/webhook"
    tg("deleteWebhook")
    r = tg("setWebhook", {"url": u})
    if r.get("ok"):
        return jsonify({"success": True, "message": "Webhook configured!"})
    return jsonify({"error": r.get("description", "Failed")}), 500
