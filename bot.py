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

# Config
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

# Solana escrow
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
        logger.info("Escrow DEMO - set TREASURY_PRIVATE_KEY for live mode")
except ImportError:
    logger.info("Solana libs not installed - pip install solana solders base58")
