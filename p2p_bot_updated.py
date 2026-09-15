# -*- coding: utf-8 -*-
"""USDT P2P Mediator Telegram Bot.

Repaired version: restores the missing runtime/database layer, fixes the
corrupted SQL block, removes the accidental duplicate KYC handler, and keeps
the original user/admin/trade workflow intact.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import smtplib
import sqlite3
import string
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from email.message import EmailMessage

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode

try:
    from telegram import CopyTextButton
except ImportError:
    CopyTextButton = None

try:
    from txid_checker import (
        BSC_RPC_URL,
        USDT_BSC_CONTRACT,
        USDT_BSC_DECIMALS,
        verify_bep20_usdt_deposit,
        normalize_txid,
    )
except ImportError:
    # Kept explicit so deployment fails clearly if the companion file is missing.
    raise RuntimeError("txid_checker.py is required next to p2p_bot_updated.py")


def copy_value_button(label, value):
    """Create a real Telegram copy-to-clipboard button.

    Uses native copy_text when the installed PTB supports it. For older
    PTB versions, api_kwargs passes the Bot API field directly.
    """
    value = str(value or "").strip()
    if not value or len(value) > 256:
        return None
    if CopyTextButton is not None:
        try:
            return InlineKeyboardButton(label, copy_text=CopyTextButton(value))
        except (TypeError, ValueError):
            pass
    # Compatibility with older python-telegram-bot releases.
    try:
        return InlineKeyboardButton(
            label,
            api_kwargs={"copy_text": {"text": value}},
        )
    except Exception:
        # Last-resort button: the address is still shown plainly in the message.
        return InlineKeyboardButton(label, callback_data="noop_copy")

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION — عدّل هذه الأربعة فقط
# ============================================================

# 1) BotFather token
BOT_TOKEN = "7691026720:AAGbt7LAZQFGLyXkNGLZ5ltDgqBkvMVOo-E"

# 2) Telegram Admin ID
ADMIN_ID = 1441524960

# 3) Gmail address used to send OTP codes
GMAIL_ADDRESS = "ameralzman000@gmail.com"

# 4) Gmail App Password (16-character App Password)
GMAIL_APP_PASSWORD = "tlob cqvs ycah gvvi"


def _env_int(name, default=0):
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


# The four values above can also be overridden by environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN).strip()
ADMIN_ID = _env_int("ADMIN_ID", ADMIN_ID)
DATABASE = os.getenv("DATABASE", "p2p_bot.db").strip() or "p2p_bot.db"
NETWORK = os.getenv("NETWORK", "BEP20").strip() or "BEP20"
BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.bnbchain.org").strip()
USDT_BSC_CONTRACT = os.getenv(
    "USDT_BSC_CONTRACT",
    "0x55d398326f99059fF775485246999027B3197955",
).strip()
USDT_BSC_DECIMALS = 18
DEFAULT_MEDIATOR_ADDRESS = os.getenv("MEDIATOR_ADDRESS", "").strip()
COMMISSION_RATE = os.getenv("COMMISSION_RATE", "1")
TRADE_TIMEOUT_MINUTES = _env_int("TRADE_TIMEOUT_MINUTES", 60)
OTP_EXPIRY_MINUTES = _env_int("OTP_EXPIRY_MINUTES", 10)
# Password required before the administrator can permanently clear all database data.
# It can be overridden through the environment for safer deployment.
DATABASE_DELETE_PASSWORD = os.getenv("DATABASE_DELETE_PASSWORD", "ameralzman00")

# Gmail SMTP is fixed — no fifth setting is required.
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.getenv("SMTP_USER", GMAIL_ADDRESS).strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", GMAIL_APP_PASSWORD)
SMTP_FROM = SMTP_USER


# Conversation states. Integer values are intentionally stable.
(
    KYC_NAME,
    KYC_PHONE,
    KYC_DOB,
    KYC_COUNTRY,
    KYC_ID_TYPE,
    KYC_ID_PHOTO,
    KYC_SELFIE,
    KYC_SHAM_NAME,
    KYC_SHAM_CODE,
    KYC_SHAM_SCREENSHOT,
) = range(10)

(
    OTP_EMAIL,
    OTP_CODE,
) = range(10, 12)

# New lightweight KYC flow states. Keep legacy KYC/OTP values above stable
# for backward compatibility with existing conversations/database records.
(
    KYC_EMAIL,
    KYC_EMAIL_CODE,
    KYC_BEP20,
) = range(38, 41)

(
    AD_TYPE,
    AD_CURRENCY,
    AD_PRICE,
    AD_AMOUNT,
    AD_MIN,
    AD_MAX,
    AD_PAYMENT,
    AD_ACCOUNT,
    AD_NOTE,
) = range(20, 29)

SELLER_DEPOSIT_TXID = 30
PAYMENT_PROOF = 31
ADMIN_RELEASE_TXID = 32
DISPUTE_ADMIN_CHAT = 33
DISPUTE_USER_CHAT = 34
AD_EDIT_VALUE = 35
ADMIN_RECHECK_TRADE = 36
ADMIN_RECHECK_TXID = 37
ADMIN_DB_PASSWORD = 41
ADMIN_DB_CONFIRM = 42
ADMIN_DB_MODE = 43
ADMIN_QUEUE_SEARCH = 44
ADMIN_MARKET_VALUE = 45


# ============================================================
# DATABASE
# ============================================================

def db():
    # SQLite connection tuned for a Telegram bot where several handlers/jobs
    # may touch the database close together. WAL is enabled once in init_db();
    # busy_timeout here lets SQLite wait for short-lived locks instead of
    # immediately raising "database is locked".
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def db_commit(conn, retries=5):
    """Commit with a small retry window for transient SQLite lock contention."""
    delay = 0.25
    for attempt in range(retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= retries - 1:
                raise
            import time
            time.sleep(delay)
            delay = min(delay * 2, 2.0)


def _ensure_columns(conn, table, columns):
    """Add missing columns so upgrades do not destroy an existing database."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    conn = db()
    # WAL improves read/write concurrency and prevents readers from blocking
    # a writer in the common case. It does not permit two writers simultaneously.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0,
            kyc_status TEXT NOT NULL DEFAULT 'PENDING',
            sham_status TEXT NOT NULL DEFAULT 'PENDING',
            phone TEXT,
            dob TEXT,
            country TEXT,
            bep20_address TEXT,
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS kyc_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            dob TEXT,
            country TEXT,
            id_type TEXT,
            id_photo_file_id TEXT,
            selfie_file_id TEXT,
            sham_name TEXT,
            sham_code TEXT,
            sham_screenshot_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS advertisements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ad_type TEXT NOT NULL,
            currency TEXT NOT NULL,
            price TEXT NOT NULL,
            amount TEXT NOT NULL,
            min_amount TEXT NOT NULL,
            max_amount TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            payment_account TEXT NOT NULL,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_code TEXT UNIQUE NOT NULL,
            ad_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            requested_amount TEXT NOT NULL,
            buyer_receives TEXT NOT NULL,
            seller_sends TEXT NOT NULL,
            price TEXT NOT NULL,
            buyer_fee TEXT NOT NULL,
            seller_fee TEXT NOT NULL,
            fiat_amount TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            payment_account TEXT NOT NULL,
            mediator_address TEXT,
            buyer_bep20_address TEXT,
            state TEXT NOT NULL,
            seller_deposit_txid TEXT,
            seller_deposit_amount TEXT,
            payment_txid TEXT,
            payment_proof_file_id TEXT,
            release_txid TEXT,
            refund_txid TEXT,
            dispute_reason TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount TEXT NOT NULL,
            payment_method TEXT,
            account TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            txid TEXT,
            proof_file_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proofs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            proof_type TEXT NOT NULL,
            file_id TEXT,
            text TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS commissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount TEXT NOT NULL,
            side TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS disputes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            admin_decision TEXT,
            previous_trade_state TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS dispute_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispute_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            message_type TEXT NOT NULL DEFAULT 'TEXT',
            message_text TEXT,
            file_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            user_id INTEGER,
            trade_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    # Lightweight migrations for databases created by an older revision.
    _ensure_columns(conn, "users", {
        "email": "TEXT",
        "email_verified": "INTEGER NOT NULL DEFAULT 0",
        "kyc_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        "sham_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        "phone": "TEXT",
        "dob": "TEXT",
        "country": "TEXT",
        "bep20_address": "TEXT",
        "blocked": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(conn, "kyc_requests", {
        "reviewed_at": "TEXT",
    })
    _ensure_columns(conn, "trades", {
        "buyer_bep20_address": "TEXT",
        "seller_deposit_check_status": "TEXT",
        "seller_deposit_check_reason": "TEXT",
        "seller_deposit_check_block": "TEXT",
    })
    _ensure_columns(conn, "advertisements", {
        "expires_at": "TEXT",
    })
    _ensure_columns(conn, "disputes", {
        "previous_trade_state": "TEXT",
    })

    defaults = {
        "mediator_address": DEFAULT_MEDIATOR_ADDRESS,
        "commission_rate": str(COMMISSION_RATE),
        "trade_timeout_minutes": str(TRADE_TIMEOUT_MINUTES),
        "network": NETWORK,
        # Market controls — all editable by the manager from the bot.
        "market_open": "1",
        "ads_open": "1",
        "min_trade_amount": "10",
        "buy_price_min": "0",
        "buy_price_max": "0",
        "sell_price_min": "0",
        "sell_price_max": "0",
        "max_active_ads_per_user": "1",
        "min_ad_amount": "10",
        "max_ad_amount": "0",
        "ad_expiry_hours": "24",
    }
    for key, value in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )
    db_commit(conn)
    conn.close()


def clear_non_kyc_database_data():
    """Delete operational data while preserving user/KYC records and settings."""
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = [
            "advertisements",
            "otp_codes",
            "trades",
            "payments",
            "proofs",
            "commissions",
            "disputes",
            "dispute_messages",
            "notifications",
            "audit_logs",
        ]
        for table in tables:
            conn.execute(f'DELETE FROM "{table}"')
        db_commit(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_database():
    """Completely clear the database and recreate the schema."""
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            safe_table = table.replace(chr(34), chr(34) * 2)
            conn.execute(f'DROP TABLE IF EXISTS "{safe_table}"')
        db_commit(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    init_db()

    # Recreate all tables and default settings.
    init_db()


# ============================================================
# TIME
# ============================================================

def now():

    return datetime.now(
        timezone.utc
    ).isoformat()


def parse_time(value):

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return datetime.now(timezone.utc)


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=None):

    conn = db()

    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()

    conn.close()

    if not row:
        return default

    return row["value"]


def set_setting(key, value):

    conn = db()

    conn.execute(
        """
        INSERT INTO settings(key,value)
        VALUES(?,?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, str(value)),
    )

    db_commit(conn)
    conn.close()


def market_bool(key, default=True):
    return get_setting(key, "1" if default else "0") == "1"


def market_decimal(key, default="0"):
    return D(get_setting(key, default))


def expire_old_ads():
    """Mark expired active ads as CANCELLED without touching trades."""
    current = datetime.now(timezone.utc)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, expires_at FROM advertisements WHERE status='ACTIVE' AND expires_at IS NOT NULL"
        ).fetchall()
        changed = []
        for row in rows:
            try:
                expires = parse_time(row["expires_at"])
            except Exception:
                continue
            if current > expires:
                if conn.execute(
                    "UPDATE advertisements SET status='CANCELLED', updated_at=? WHERE id=? AND status='ACTIVE'",
                    (now(), row["id"]),
                ).rowcount:
                    changed.append(row["id"])
        db_commit(conn)
    finally:
        conn.close()
    for ad_id in changed:
        audit("ADVERTISEMENT_EXPIRED", details=f"ad_id={ad_id}")


def market_price_limits(ad_type):
    if ad_type == "BUY":
        return market_decimal("buy_price_min"), market_decimal("buy_price_max")
    return market_decimal("sell_price_min"), market_decimal("sell_price_max")


def active_ads_count(user_id):
    expire_old_ads()
    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) c FROM advertisements WHERE user_id=? AND status='ACTIVE'",
        (user_id,),
    ).fetchone()["c"]
    conn.close()
    return count


# ============================================================
# USER
# ============================================================

def ensure_user(tg_user):

    conn = db()

    timestamp = now()

    conn.execute(
        """
        INSERT INTO users(
            telegram_id,
            username,
            first_name,
            last_name,
            created_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?)

        ON CONFLICT(telegram_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            updated_at=excluded.updated_at
        """,
        (
            tg_user.id,
            tg_user.username,
            tg_user.first_name,
            tg_user.last_name,
            timestamp,
            timestamp,
        ),
    )

    db_commit(conn)
    conn.close()


def get_user(telegram_id):

    conn = db()

    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (telegram_id,),
    ).fetchone()

    conn.close()

    return row


def is_admin(user_id):

    return user_id == ADMIN_ID


def is_blocked(user_id):

    row = get_user(user_id)

    return bool(row and row["blocked"])


def is_verified(user_id):

    if is_admin(user_id):
        return True

    row = get_user(user_id)

    if not row:
        return False

    return (
        row["kyc_status"] == "APPROVED"
        and row["sham_status"] == "APPROVED"
        and row["email_verified"] == 1
    )


def get_user_identity(conn, user_id):
    """Return the real/KYC name plus Telegram identity for admin views."""
    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (user_id,),
    ).fetchone()
    kyc = conn.execute(
        """
        SELECT full_name, phone, country, sham_name, sham_code, status
        FROM kyc_requests
        WHERE user_id=? AND status='APPROVED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    return user, kyc


# ============================================================
# AUDIT
# ============================================================

def audit(
    action,
    user_id=None,
    trade_id=None,
    admin_id=None,
    details="",
):

    conn = db()

    conn.execute(
        """
        INSERT INTO audit_logs(
            admin_id,
            user_id,
            trade_id,
            action,
            details,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            admin_id,
            user_id,
            trade_id,
            action,
            details,
            now(),
        ),
    )

    db_commit(conn)
    conn.close()


# ============================================================
# NOTIFICATIONS
# ============================================================

def add_notification(user_id, message):

    conn = db()

    conn.execute(
        """
        INSERT INTO notifications(
            user_id,
            message,
            created_at
        )
        VALUES(?,?,?)
        """,
        (
            user_id,
            message,
            now(),
        ),
    )

    db_commit(conn)
    conn.close()


async def notify_user(
    context,
    user_id,
    message,
    reply_markup=None,
):

    try:

        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=reply_markup,
        )

    except Exception as exc:

        logger.warning(
            "Notification failed for %s: %s",
            user_id,
            exc,
        )

    add_notification(
        user_id,
        message,
    )


async def notify_admin(
    context,
    message,
    reply_markup=None,
):

    if not ADMIN_ID:
        return

    try:

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            reply_markup=reply_markup,
        )

    except Exception as exc:

        logger.warning(
            "Admin notification failed: %s",
            exc,
        )


# ============================================================
# FORMAT DECIMAL
# ============================================================

def D(value):

    try:
        return Decimal(str(value))
    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):
        return Decimal("0")


def clean_decimal(value):

    value = D(value)

    if value == value.to_integral():

        return str(value.quantize(Decimal("1")))

    text = format(
        value.normalize(),
        "f",
    )

    return text.rstrip("0").rstrip(".")


def calculate_fee(amount):

    rate = D(
        get_setting(
            "commission_rate",
            str(COMMISSION_RATE),
        )
    )

    # Commission is always rounded UP to 2 decimal places so the
    # rounding remainder stays with the mediator.
    exact_fee = amount * rate / Decimal("100")
    return exact_fee.quantize(
        Decimal("0.01"),
        rounding=ROUND_UP,
    )


# ============================================================
# TRADE CODE
# ============================================================

def generate_trade_code():

    while True:

        code = (
            "TRD-"
            + datetime.now().strftime("%Y%m%d")
            + "-"
            + "".join(
                random.choices(
                    string.digits,
                    k=5,
                )
            )
        )

        conn = db()

        row = conn.execute(
            """
            SELECT id
            FROM trades
            WHERE trade_code=?
            """,
            (code,),
        ).fetchone()

        conn.close()

        if not row:
            return code


# ============================================================
# EMAIL OTP
# ============================================================

def generate_otp():

    return "".join(
        random.choices(
            string.digits,
            k=6,
        )
    )


def send_otp_email(email, code):

    if not SMTP_HOST or not SMTP_USER:

        logger.warning(
            "SMTP is not configured."
        )

        return False

    try:

        msg = EmailMessage()

        msg["Subject"] = (
            "رمز التحقق - USDT P2P"
        )

        msg["From"] = SMTP_FROM
        msg["To"] = email

        msg.set_content(
            f"""
رمز التحقق الخاص بك:

{code}

صلاحية الرمز {OTP_EXPIRY_MINUTES} دقائق.

إذا لم تطلب هذا الرمز فتجاهل الرسالة.
"""
        )

        context = ssl_create_context()

        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30,
        ) as server:

            server.starttls(
                context=context
            )

            server.login(
                SMTP_USER,
                SMTP_PASSWORD,
            )

            server.send_message(msg)

        return True

    except Exception as exc:

        logger.exception(
            "Email error: %s",
            exc,
        )

        return False


def ssl_create_context():

    import ssl

    return ssl.create_default_context()


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard(user_id):

    rows = [
        [
            KeyboardButton("🟢 شراء USDT"),
            KeyboardButton("🔴 بيع USDT"),
        ],
        [
            KeyboardButton("📢 الإعلانات"),
            KeyboardButton("➕ إنشاء إعلان"),
        ],
        [
            KeyboardButton("📢 إعلاناتي"),
        ],
        [
            KeyboardButton("📋 صفقاتي"),
            KeyboardButton("🪪 التوثيق KYC"),
        ],
        [
            KeyboardButton("💳 Sham Cash"),
            KeyboardButton("ℹ️ التعليمات"),
        ],
        [
            KeyboardButton("🏠 الرئيسية"),
        ],
    ]

    if is_admin(user_id):

        rows.append(
            [
                KeyboardButton(
                    "⚙️ لوحة المدير"
                )
            ]
        )

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
    )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):

        await update.message.reply_text(
            "🚫 حسابك محظور من استخدام المنصة."
        )

        return

    text = (
        "مرحباً بك في منصة P2P\n\n"
        "🔐 الوساطة عبر المنصة\n"
        "💵 USDT\n"
        "🌐 الشبكة: BEP20\n\n"
        "اختر العملية من القائمة."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(user.id),
    )


# ============================================================
# HOME
# ============================================================

async def home(update, context):

    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        "🏠 الرئيسية\n\nاختر العملية:",
        reply_markup=main_keyboard(user.id),
    )


# ============================================================
# REQUIRE VERIFICATION
# ============================================================

async def require_verification(
    update,
):

    user_id = update.effective_user.id

    if is_verified(user_id):

        return True

    await update.message.reply_text(
        "🔐 يجب إكمال التوثيق قبل استخدام هذه الخدمة.\n\n"
        "اضغط 🪪 التوثيق KYC من القائمة."
    )

    return False


# ============================================================
# KYC START
# ============================================================

async def kyc_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Start the lightweight KYC flow with email verification first."""
    user_id = update.effective_user.id

    if is_admin(user_id):
        await update.message.reply_text("👑 المدير لا يحتاج إلى KYC.")
        return ConversationHandler.END

    row = get_user(user_id)

    if row and row["kyc_status"] == "APPROVED":
        await update.message.reply_text("✅ تم اعتماد KYC الخاص بك.")
        return ConversationHandler.END

    # Email verification is deliberately the FIRST step of KYC.
    if not row or not row["email_verified"]:
        await update.message.reply_text(
            "🪪 التوثيق KYC\n\n"
            "📧 الخطوة 1 من التوثيق: أرسل بريدك الإلكتروني:\n\n"
            "سيتم إرسال رمز تحقق إلى البريد، وبعد تأكيده نكمل التوثيق مباشرة."
        )
        return KYC_EMAIL

    await update.message.reply_text(
        "🪪 التوثيق KYC\n\n"
        "📧 البريد الإلكتروني: ✅ موثق\n\n"
        "👤 الخطوة التالية: أرسل الاسم الكامل:"
    )
    return KYC_NAME


async def kyc_email(update, context):
    email = update.message.text.strip()

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        await update.message.reply_text("❌ البريد الإلكتروني غير صحيح. أرسله مرة أخرى:")
        return KYC_EMAIL

    code = generate_otp()
    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    ).isoformat()

    conn = db()
    conn.execute(
        """
        INSERT INTO otp_codes(
            telegram_id, email, code, expires_at, created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            update.effective_user.id,
            email,
            hashlib.sha256(code.encode()).hexdigest(),
            expires,
            now(),
        ),
    )
    conn.execute(
        """
        UPDATE users
        SET email=?, updated_at=?
        WHERE telegram_id=?
        """,
        (email, now(), update.effective_user.id),
    )
    db_commit(conn)
    conn.close()

    sent = send_otp_email(email, code)
    context.user_data["kyc_email"] = email

    if not sent:
        await update.message.reply_text(
            "⚠️ SMTP غير مضبوط في إعدادات البوت.\n"
            "تم إنشاء الرمز في سجل تشغيل البوت."
        )
        logger.info("KYC OTP for %s = %s", update.effective_user.id, code)
    else:
        await update.message.reply_text(
            "📧 تم إرسال رمز التحقق إلى بريدك.\n"
            f"صلاحية الرمز {OTP_EXPIRY_MINUTES} دقائق.\n\n"
            "أرسل رمز التحقق:"
        )

    return KYC_EMAIL_CODE


async def kyc_email_code(update, context):
    code = update.message.text.strip()
    user_id = update.effective_user.id
    hashed = hashlib.sha256(code.encode()).hexdigest()

    conn = db()
    row = conn.execute(
        """
        SELECT * FROM otp_codes
        WHERE telegram_id=? AND used=0
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    if not row:
        conn.close()
        await update.message.reply_text("❌ لا يوجد رمز صالح. ابدأ التحقق من البريد مرة أخرى.")
        return ConversationHandler.END

    expires = parse_time(row["expires_at"])
    if datetime.now(timezone.utc) > expires:
        conn.close()
        await update.message.reply_text("❌ انتهت صلاحية الرمز. ابدأ التحقق من البريد مرة أخرى.")
        return ConversationHandler.END

    if hashed != row["code"]:
        conn.close()
        await update.message.reply_text("❌ رمز التحقق غير صحيح. أرسله مرة أخرى:")
        return KYC_EMAIL_CODE

    conn.execute("UPDATE otp_codes SET used=1 WHERE id=?", (row["id"],))
    conn.execute(
        """
        UPDATE users
        SET email_verified=1, updated_at=?
        WHERE telegram_id=?
        """,
        (now(), user_id),
    )
    db_commit(conn)
    conn.close()

    audit("EMAIL_VERIFIED", user_id=user_id, details="verified_during_kyc")

    await update.message.reply_text(
        "✅ تم التحقق من البريد الإلكتروني بنجاح.\n\n"
        "👤 الخطوة 2 من التوثيق: أرسل الاسم الكامل:"
    )
    return KYC_NAME


async def kyc_name(update, context):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("❌ أرسل الاسم الكامل:")
        return KYC_NAME
    context.user_data["kyc_name"] = value
    await update.message.reply_text("📱 أرسل رقم الهاتف المشترك/المستخدم للتواصل:")
    return KYC_PHONE


async def kyc_phone(update, context):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("❌ أرسل رقم الهاتف:")
        return KYC_PHONE
    context.user_data["kyc_phone"] = value
    await update.message.reply_text("🪪 أرسل صورة واضحة للهوية أو الوثيقة الشخصية:")
    return KYC_ID_PHOTO


async def kyc_id_photo(update, context):
    if not update.message.photo:
        await update.message.reply_text("❌ أرسل صورة الهوية/الوثيقة كصورة:")
        return KYC_ID_PHOTO

    context.user_data["kyc_id_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("🤳 الآن أرسل صورة Selfie واضحة لك:")
    return KYC_SELFIE


async def kyc_selfie(update, context):
    if not update.message.photo:
        await update.message.reply_text("❌ أرسل صورة Selfie كصورة:")
        return KYC_SELFIE

    context.user_data["kyc_selfie"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "📬 أرسل عنوان محفظتك BEP20:\n\n"
        "يجب أن يبدأ بـ 0x ويتكون من 40 خانة سداسية عشرية."
    )
    return KYC_BEP20


async def kyc_bep20(update, context):
    address = update.message.text.strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        await update.message.reply_text(
            "❌ عنوان BEP20 غير صحيح.\n"
            "يجب أن يبدأ بـ 0x ويتكون من 40 خانة سداسية عشرية.\n\n"
            "أرسل العنوان مرة أخرى:"
        )
        return KYC_BEP20

    context.user_data["kyc_bep20"] = address
    await update.message.reply_text(
        "💳 أرسل عنوان/معرّف حساب Sham Cash المستخدم لاستلام/إرسال الدفعات المحلية:"
    )
    return KYC_SHAM_CODE


async def kyc_sham_code(update, context):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text("❌ أرسل عنوان/معرّف حساب Sham Cash:")
        return KYC_SHAM_CODE

    # Keep the existing data model compatible: the single Sham Cash value is
    # stored in both legacy fields, while the screenshot is no longer required.
    context.user_data["sham_code"] = value
    context.user_data["sham_name"] = value

    user = update.effective_user
    data = context.user_data
    user_row = get_user(user.id)

    if not user_row:
        await update.message.reply_text("❌ لم يتم العثور على حسابك.")
        return ConversationHandler.END

    if not user_row["email_verified"]:
        # Defensive check; normal flow verifies email at the beginning.
        await update.message.reply_text(
            "❌ البريد الإلكتروني غير موثق. أعد بدء التوثيق ليتم التحقق منه أولاً."
        )
        return ConversationHandler.END

    conn = db()
    cursor = conn.execute(
        """
        INSERT INTO kyc_requests(
            user_id, full_name, email, phone,
            dob, country, id_type,
            id_photo_file_id, selfie_file_id,
            sham_name, sham_code, sham_screenshot_file_id,
            status, created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            user.id,
            data.get("kyc_name"),
            user_row["email"],
            data.get("kyc_phone"),
            None,  # legacy field: DOB no longer collected
            None,  # legacy field: country no longer collected
            None,  # legacy field: document type no longer collected
            data.get("kyc_id_photo"),
            data.get("kyc_selfie"),
            data.get("sham_name"),
            data.get("sham_code"),
            None,  # legacy field: Sham Cash screenshot no longer required
            "PENDING",
            now(),
        ),
    )

    kyc_request_id = cursor.lastrowid

    conn.execute(
        """
        UPDATE users
        SET
            kyc_status='PENDING',
            sham_status='PENDING',
            phone=?,
            bep20_address=?,
            updated_at=?
        WHERE telegram_id=?
        """,
        (
            data.get("kyc_phone"),
            data.get("kyc_bep20"),
            now(),
            user.id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "KYC_SUBMITTED",
        user_id=user.id,
        details=f"kyc_request_id={kyc_request_id}",
    )

    admin_text = (
        "🪪 <b>طلب توثيق جديد</b>\n\n"
        f"🆔 <b>رقم طلب KYC:</b> {kyc_request_id}\n"
        f"👤 <b>Telegram ID:</b> {user.id}\n"
        f"🔹 <b>Username:</b> @{user.username if user.username else '-'}\n"
        f"👨 <b>الاسم في Telegram:</b> {user.first_name or ''} {user.last_name or ''}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>بيانات التوثيق</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>الاسم الكامل:</b> {data.get('kyc_name') or '-'}\n\n"
        f"📧 <b>البريد الإلكتروني:</b> {user_row['email'] or '-'}\n"
        "📧 حالة البريد: ✅ موثق\n\n"
        f"📱 <b>رقم الهاتف:</b> {data.get('kyc_phone') or '-'}\n\n"
        f"📬 <b>عنوان BEP20:</b> <code>{data.get('kyc_bep20') or '-'}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💳 <b>بيانات Sham Cash</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 <b>عنوان / معرّف Sham Cash:</b> {data.get('sham_code') or '-'}\n\n"
        "📎 <b>المستندات المرفقة:</b>\n"
        "🪪 صورة الهوية/الوثيقة\n"
        "🤳 صورة السيلفي\n\n"
        "⚠️ راجع المعلومات والصور قبل اتخاذ القرار."
    )

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ قبول التوثيق",
                callback_data=f"kyc_approve:{kyc_request_id}",
            ),
            InlineKeyboardButton(
                "❌ رفض التوثيق",
                callback_data=f"kyc_reject:{kyc_request_id}",
            ),
        ]]
    )

    await notify_admin(context, admin_text, reply_markup=keyboard)

    id_photo = data.get("kyc_id_photo")
    selfie = data.get("kyc_selfie")

    if id_photo:
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=id_photo,
                caption=f"🪪 صورة الهوية — طلب KYC #{kyc_request_id}",
            )
        except Exception as exc:
            logger.warning("Failed to send KYC ID photo: %s", exc)

    if selfie:
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=selfie,
                caption=f"🤳 صورة السيلفي — طلب KYC #{kyc_request_id}",
            )
        except Exception as exc:
            logger.warning("Failed to send KYC selfie: %s", exc)

    await update.message.reply_text(
        "✅ تم إرسال طلب التوثيق كاملاً إلى المدير.\n\n"
        "📧 البريد الإلكتروني: تم التحقق منه أولاً.\n"
        "👤 الاسم الكامل: تم استلامه.\n"
        "📱 رقم الهاتف: تم استلامه.\n"
        "🪪 صورة الهوية: تم استلامها.\n"
        "🤳 صورة السيلفي: تم استلامها.\n"
        "📬 عنوان BEP20: تم استلامه.\n"
        "💳 عنوان Sham Cash: تم استلامه.\n\n"
        "⏳ انتظر نتيجة المراجعة.",
        reply_markup=main_keyboard(user.id),
    )

    context.user_data.clear()
    return ConversationHandler.END

# ============================================================
# KYC APPROVE / REJECT
# ============================================================

async def kyc_admin_callback(update, context):

    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("غير مصرح.", show_alert=True)
        return

    await query.answer()

    try:
        action, target_text = query.data.split(":", 1)
        target_id = int(target_text)
    except (ValueError, AttributeError):
        await query.message.reply_text("❌ بيانات طلب KYC غير صالحة.")
        return

    if action not in ("kyc_approve", "kyc_reject"):
        return

    conn = db()

    # The enhanced KYC submission uses the request ID in its buttons,
    # while older admin-list buttons used the Telegram user ID. Support both
    # so existing messages remain usable after an upgrade.
    request = conn.execute(
        "SELECT * FROM kyc_requests WHERE id=? AND status='PENDING'",
        (target_id,),
    ).fetchone()

    if request:
        user_id = request["user_id"]
        request_id = request["id"]
    else:
        user_id = target_id
        request = conn.execute(
            """
            SELECT * FROM kyc_requests
            WHERE user_id=? AND status='PENDING'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        request_id = request["id"] if request else None

    if not request:
        conn.close()
        await query.message.reply_text("⚠️ لا يوجد طلب KYC معلق لهذا المستخدم.")
        return

    new_status = "APPROVED" if action == "kyc_approve" else "REJECTED"

    # Update only the selected request, not every historical pending request.
    updated = conn.execute(
        """
        UPDATE kyc_requests
        SET status=?, reviewed_at=?
        WHERE id=? AND status='PENDING'
        """,
        (new_status, now(), request_id),
    ).rowcount

    if not updated:
        conn.close()
        await query.message.reply_text("⚠️ تم التعامل مع هذا الطلب مسبقاً.")
        return

    conn.execute(
        """
        UPDATE users
        SET
            kyc_status=?,
            sham_status=?,
            updated_at=?
        WHERE telegram_id=?
        """,
        (new_status, new_status, now(), user_id),
    )

    db_commit(conn)
    conn.close()

    if new_status == "APPROVED":
        audit("KYC_APPROVED", user_id=user_id, admin_id=ADMIN_ID,
              details=f"kyc_request_id={request_id}")
        await notify_user(
            context,
            user_id,
            "✅ تم قبول التوثيق الخاص بك.\nيمكنك الآن استخدام منصة P2P.",
        )
        await query.message.reply_text("✅ تم قبول KYC.")
    else:
        audit("KYC_REJECTED", user_id=user_id, admin_id=ADMIN_ID,
              details=f"kyc_request_id={request_id}")
        await notify_user(
            context,
            user_id,
            "❌ تم رفض طلب التوثيق.\nيمكنك التواصل مع الإدارة لمعرفة السبب.",
        )
        await query.message.reply_text("❌ تم رفض KYC.")

# ============================================================
# OTP START
# ============================================================

async def start_email_verification(
    update,
    context,
):

    user_id = update.effective_user.id

    if is_admin(user_id):

        await update.message.reply_text(
            "👑 المدير لا يحتاج إلى التحقق."
        )

        return ConversationHandler.END

    await update.message.reply_text(
        "📧 أرسل بريدك الإلكتروني:"
    )

    return OTP_EMAIL


async def otp_email(update, context):

    email = update.message.text.strip()

    if not re.match(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        email,
    ):

        await update.message.reply_text(
            "❌ البريد الإلكتروني غير صحيح."
        )

        return OTP_EMAIL

    code = generate_otp()

    expires = (
        datetime.now(timezone.utc)
        + timedelta(
            minutes=OTP_EXPIRY_MINUTES
        )
    ).isoformat()

    conn = db()

    conn.execute(
        """
        INSERT INTO otp_codes(
            telegram_id,
            email,
            code,
            expires_at,
            created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            update.effective_user.id,
            email,
            hashlib.sha256(
                code.encode()
            ).hexdigest(),
            expires,
            now(),
        ),
    )

    conn.execute(
        """
        UPDATE users
        SET
            email=?,
            updated_at=?
        WHERE telegram_id=?
        """,
        (
            email,
            now(),
            update.effective_user.id,
        ),
    )

    db_commit(conn)
    conn.close()

    sent = send_otp_email(
        email,
        code,
    )

    # Development fallback.
    # Remove this behavior in production if desired.
    if not sent:

        await update.message.reply_text(
            "⚠️ SMTP غير مضبوط في إعدادات البوت.\n"
            "تم إنشاء الرمز في سجل تشغيل البوت."
        )

        logger.info(
            "OTP for %s = %s",
            update.effective_user.id,
            code,
        )

    else:

        await update.message.reply_text(
            "📧 تم إرسال رمز التحقق إلى بريدك.\n"
            f"صلاحية الرمز {OTP_EXPIRY_MINUTES} دقائق.\n\n"
            "أرسل الرمز:"
        )

    context.user_data["otp_email"] = email

    return OTP_CODE


async def otp_code(update, context):

    code = update.message.text.strip()

    user_id = update.effective_user.id

    hashed = hashlib.sha256(
        code.encode()
    ).hexdigest()

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM otp_codes
        WHERE telegram_id=?
          AND used=0
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    if not row:

        conn.close()

        await update.message.reply_text(
            "❌ لا يوجد رمز صالح."
        )

        return ConversationHandler.END

    expires = parse_time(
        row["expires_at"]
    )

    if datetime.now(timezone.utc) > expires:

        conn.close()

        await update.message.reply_text(
            "❌ انتهت صلاحية الرمز."
        )

        return ConversationHandler.END

    if hashed != row["code"]:

        conn.close()

        await update.message.reply_text(
            "❌ رمز التحقق غير صحيح."
        )

        return OTP_CODE

    conn.execute(
        """
        UPDATE otp_codes
        SET used=1
        WHERE id=?
        """,
        (row["id"],),
    )

    conn.execute(
        """
        UPDATE users
        SET
            email_verified=1,
            updated_at=?
        WHERE telegram_id=?
        """,
        (
            now(),
            user_id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "EMAIL_VERIFIED",
        user_id=user_id,
    )

    await update.message.reply_text(
        "✅ تم التحقق من البريد الإلكتروني.",
        reply_markup=main_keyboard(user_id),
    )

    return ConversationHandler.END


# ============================================================
# ADVERTISEMENT START
# ============================================================

async def create_ad_start(update, context):

    if not await require_verification(update):

        return ConversationHandler.END

    if not market_bool("ads_open", True):
        await update.message.reply_text("🔴 إنشاء الإعلانات متوقف حالياً من قبل الإدارة.")
        return ConversationHandler.END

    max_ads = max(1, int(market_decimal("max_active_ads_per_user", "1")))
    current_ads = active_ads_count(update.effective_user.id)
    if current_ads >= max_ads:
        await update.message.reply_text(
            f"❌ وصلت إلى الحد المسموح للإعلانات النشطة: {max_ads}.\n\n"
            "أوقف أحد إعلاناتك الحالية أولاً إذا أردت إنشاء إعلان جديد."
        )
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟢 أشتري USDT",
                    callback_data="ad_buy",
                ),
                InlineKeyboardButton(
                    "🔴 أبيع USDT",
                    callback_data="ad_sell",
                ),
            ]
        ]
    )

    await update.message.reply_text(
        "➕ إنشاء إعلان\n\n"
        "اختر نوع الإعلان:",
        reply_markup=keyboard,
    )

    return AD_TYPE


async def ad_type_callback(update, context):

    query = update.callback_query

    await query.answer()

    if query.data == "ad_buy":

        context.user_data["ad_type"] = "BUY"

    else:

        context.user_data["ad_type"] = "SELL"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "SYP",
                    callback_data="ad_currency:SYP",
                ),
                InlineKeyboardButton(
                    "USD",
                    callback_data="ad_currency:USD",
                ),
            ]
        ]
    )

    await query.message.reply_text(
        "💱 اختر العملة المحلية:",
        reply_markup=keyboard,
    )

    return AD_CURRENCY


async def ad_currency_callback(update, context):

    query = update.callback_query

    await query.answer()

    context.user_data["ad_currency"] = (
        query.data.split(":")[1]
    )

    await query.message.reply_text(
        "💰 أرسل سعر 1 USDT:"
    )

    return AD_PRICE


async def ad_price(update, context):

    value = D(
        update.message.text.strip()
    )

    if value <= 0:

        await update.message.reply_text(
            "❌ السعر غير صحيح."
        )

        return AD_PRICE

    ad_type = context.user_data.get("ad_type")
    price_min, price_max = market_price_limits(ad_type)
    if price_min > 0 and value < price_min:
        await update.message.reply_text(
            f"❌ السعر أقل من الحد المسموح.\nالحد الأدنى: {clean_decimal(price_min)}"
        )
        return AD_PRICE
    if price_max > 0 and value > price_max:
        await update.message.reply_text(
            f"❌ السعر أعلى من الحد المسموح.\nالحد الأعلى: {clean_decimal(price_max)}"
        )
        return AD_PRICE

    context.user_data["ad_price"] = str(value)

    await update.message.reply_text(
        "💵 أرسل إجمالي كمية USDT في الإعلان:"
    )

    return AD_AMOUNT


async def ad_amount(update, context):

    value = D(
        update.message.text.strip()
    )

    min_ad = market_decimal("min_ad_amount", "10")
    max_ad = market_decimal("max_ad_amount", "0")
    if value <= 0 or value < min_ad:
        await update.message.reply_text(
            f"❌ كمية الإعلان أقل من الحد المسموح.\nالحد الأدنى: {clean_decimal(min_ad)} USDT"
        )
        return AD_AMOUNT
    if max_ad > 0 and value > max_ad:
        await update.message.reply_text(
            f"❌ كمية الإعلان أكبر من الحد المسموح.\nالحد الأعلى: {clean_decimal(max_ad)} USDT"
        )
        return AD_AMOUNT

    context.user_data["ad_amount"] = str(value)

    await update.message.reply_text(
        "📉 أرسل الحد الأدنى للصفقة:"
    )

    return AD_MIN


async def ad_min(update, context):

    value = D(
        update.message.text.strip()
    )

    amount = D(
        context.user_data["ad_amount"]
    )

    market_min = market_decimal("min_trade_amount", "10")
    if value < market_min or value <= 0 or value > amount:

        await update.message.reply_text(
            f"❌ الحد الأدنى غير صحيح. يجب ألا يقل عن {clean_decimal(market_min)} USDT وألا يتجاوز كمية الإعلان."
        )

        return AD_MIN

    context.user_data["ad_min"] = str(value)

    await update.message.reply_text(
        "📈 أرسل الحد الأعلى للصفقة:"
    )

    return AD_MAX


async def ad_max(update, context):

    value = D(
        update.message.text.strip()
    )

    amount = D(
        context.user_data["ad_amount"]
    )

    minimum = D(
        context.user_data["ad_min"]
    )

    market_min = market_decimal("min_trade_amount", "10")
    if value < market_min or value < minimum or value > amount:

        await update.message.reply_text(
            f"❌ الحد الأعلى غير صحيح. يجب ألا يقل عن {clean_decimal(market_min)} USDT وألا يتجاوز كمية الإعلان."
        )

        return AD_MAX

    context.user_data["ad_max"] = str(value)

    await update.message.reply_text(
        "💳 أرسل طريقة الدفع.\n"
        "مثال: Sham Cash"
    )

    return AD_PAYMENT


async def ad_payment(update, context):

    context.user_data["ad_payment"] = (
        update.message.text.strip()
    )

    await update.message.reply_text(
        "🏦 أرسل بيانات حساب الدفع التي ستظهر للمشتري:"
    )

    return AD_ACCOUNT


async def ad_account(update, context):

    context.user_data["ad_account"] = (
        update.message.text.strip()
    )

    await update.message.reply_text(
        "📝 أرسل ملاحظة للإعلان، أو اكتب - إذا لم توجد:"
    )

    return AD_NOTE


async def ad_note(update, context):

    note = update.message.text.strip()

    if note == "-":
        note = ""

    context.user_data["ad_note"] = note

    data = context.user_data

    conn = db()

    conn.execute(
        """
        INSERT INTO advertisements(
            user_id,
            ad_type,
            currency,
            price,
            amount,
            min_amount,
            max_amount,
            payment_method,
            payment_account,
            note,
            status,
            created_at,
            updated_at,
            expires_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            update.effective_user.id,
            data["ad_type"],
            data["ad_currency"],
            data["ad_price"],
            data["ad_amount"],
            data["ad_min"],
            data["ad_max"],
            data["ad_payment"],
            data["ad_account"],
            data["ad_note"],
            "ACTIVE",
            now(),
            now(),
            (datetime.now(timezone.utc) + timedelta(hours=max(1, int(market_decimal("ad_expiry_hours", "24"))))).isoformat(),
        ),
    )

    ad_id = conn.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    db_commit(conn)
    conn.close()

    audit(
        "ADVERTISEMENT_CREATED",
        user_id=update.effective_user.id,
        details=f"ad_id={ad_id}",
    )

    await update.message.reply_text(
        "✅ تم إنشاء الإعلان.\n\n"
        f"رقم الإعلان: #{ad_id}",
        reply_markup=main_keyboard(
            update.effective_user.id
        ),
    )

    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# AD MANAGEMENT
# ============================================================

async def my_ads(update, context):
    user_id = update.effective_user.id
    if not await require_verification(update):
        return
    conn = db()
    rows = conn.execute(
        "SELECT * FROM advertisements WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (user_id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📢 لا توجد إعلانات خاصة بك.")
        return
    for row in rows:
        title = "🟢 شراء USDT" if row["ad_type"] == "BUY" else "🔴 بيع USDT"
        await update.message.reply_text(
            f"{title}\n\nرقم الإعلان: #{row['id']}\n"
            f"السعر: {clean_decimal(row['price'])} {row['currency']}\n"
            f"الكمية: {clean_decimal(row['amount'])} USDT\n"
            f"الحالة: {row['status']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تعديل", callback_data=f"ad_manage:{row['id']}:edit")],
                [InlineKeyboardButton("❌ إلغاء الإعلان", callback_data=f"ad_manage:{row['id']}:cancel")],
            ]),
        )


async def admin_ads(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        return
    await query.answer()
    conn = db()
    rows = conn.execute(
        "SELECT * FROM advertisements ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    if not rows:
        await query.message.reply_text("📢 لا توجد إعلانات.")
        return
    for row in rows:
        title = "🟢 شراء USDT" if row["ad_type"] == "BUY" else "🔴 بيع USDT"
        await query.message.reply_text(
            f"{title}\n\nالإعلان: #{row['id']}\nصاحب الإعلان: {row['user_id']}\n"
            f"السعر: {clean_decimal(row['price'])} {row['currency']}\n"
            f"الكمية: {clean_decimal(row['amount'])} USDT\n"
            f"الحالة: {row['status']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ تعديل", callback_data=f"ad_manage:{row['id']}:edit")],
                [InlineKeyboardButton("❌ إلغاء", callback_data=f"ad_manage:{row['id']}:cancel")],
            ]),
        )


async def ad_manage_callback(update, context):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    ad_id = int(parts[1])
    action = parts[2]
    conn = db()
    ad = conn.execute("SELECT * FROM advertisements WHERE id=?", (ad_id,)).fetchone()
    conn.close()
    if not ad:
        await query.message.reply_text("❌ الإعلان غير موجود.")
        return
    if not is_admin(query.from_user.id) and ad["user_id"] != query.from_user.id:
        await query.message.reply_text("🚫 لا تملك صلاحية إدارة هذا الإعلان.")
        return
    if action == "cancel":
        if ad["status"] != "ACTIVE":
            await query.message.reply_text("⚠️ الإعلان ليس نشطًا.")
            return
        context.user_data["cancel_ad_id"] = ad_id
        await query.message.reply_text(
            f"⚠️ هل أنت متأكد من إلغاء الإعلان #{ad_id}؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، إلغاء الإعلان", callback_data=f"ad_cancel_confirm:{ad_id}")],
                [InlineKeyboardButton("↩️ رجوع", callback_data=f"ad_manage_back:{ad_id}")],
            ]),
        )
        return
    fields = [
        ("السعر", "price"),
        ("الكمية", "amount"),
        ("الحد الأدنى", "min_amount"),
        ("الحد الأعلى", "max_amount"),
        ("طريقة الدفع", "payment_method"),
        ("حساب الإعلان", "payment_account"),
        ("الملاحظة", "note"),
    ]
    buttons=[]
    for label, field in fields:
        buttons.append([InlineKeyboardButton(label, callback_data=f"ad_field:{ad_id}:{field}")])
    await query.message.reply_text(
        f"✏️ تعديل الإعلان #{ad_id}\nاختر الحقل الذي تريد تعديله:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def ad_field_callback(update, context):
    query = update.callback_query
    await query.answer()
    _, ad_id, field = query.data.split(":", 2)
    ad_id = int(ad_id)
    conn=db()
    ad=conn.execute("SELECT * FROM advertisements WHERE id=?", (ad_id,)).fetchone()
    conn.close()
    if not ad or (not is_admin(query.from_user.id) and ad["user_id"] != query.from_user.id):
        await query.message.reply_text("🚫 لا تملك صلاحية تعديل هذا الإعلان.")
        return
    if ad["status"] != "ACTIVE":
        await query.message.reply_text("⚠️ لا يمكن تعديل إعلان غير نشط.")
        return
    context.user_data["ad_edit_id"] = ad_id
    context.user_data["ad_edit_field"] = field
    labels={"price":"السعر","amount":"الكمية","min_amount":"الحد الأدنى","max_amount":"الحد الأعلى","payment_method":"طريقة الدفع","payment_account":"حساب الإعلان","note":"الملاحظة"}
    await query.message.reply_text(f"✏️ أرسل القيمة الجديدة لـ {labels.get(field, field)}:")


async def ad_edit_value(update, context):
    ad_id=context.user_data.get("ad_edit_id")
    field=context.user_data.get("ad_edit_field")
    if not ad_id or not field:
        return False
    value=(update.message.text or "").strip()
    if field in {"price","amount","min_amount","max_amount"}:
        try:
            number=D(value)
            if number <= 0:
                raise ValueError
            value=str(number)
        except Exception:
            await update.message.reply_text("❌ أدخل رقمًا صحيحًا وموجبًا.")
            return True
    if field == "note" and value == "-":
        value=""
    if len(value) > 1000:
        await update.message.reply_text("❌ القيمة طويلة جدًا.")
        return True
    conn=db()
    ad=conn.execute("SELECT * FROM advertisements WHERE id=?", (ad_id,)).fetchone()
    if not ad or (not is_admin(update.effective_user.id) and ad["user_id"] != update.effective_user.id):
        conn.close(); await update.message.reply_text("🚫 لا تملك صلاحية تعديل هذا الإعلان."); return True
    if ad["status"] != "ACTIVE":
        conn.close(); await update.message.reply_text("⚠️ الإعلان غير نشط."); return True
    conn.execute(f"UPDATE advertisements SET {field}=?, updated_at=? WHERE id=?", (value, now(), ad_id))
    db_commit(conn); conn.close()
    audit("ADVERTISEMENT_UPDATED", user_id=ad["user_id"], admin_id=update.effective_user.id if is_admin(update.effective_user.id) else None, details=f"ad_id={ad_id}, field={field}, value={value}")
    context.user_data.pop("ad_edit_id",None); context.user_data.pop("ad_edit_field",None)
    await update.message.reply_text(f"✅ تم تعديل الإعلان #{ad_id}.")
    return True


async def ad_cancel_confirm_callback(update, context):
    query=update.callback_query
    await query.answer()
    ad_id=int(query.data.split(":")[1])
    conn=db(); ad=conn.execute("SELECT * FROM advertisements WHERE id=?", (ad_id,)).fetchone()
    if not ad:
        conn.close(); await query.message.reply_text("❌ الإعلان غير موجود."); return
    if not is_admin(query.from_user.id) and ad["user_id"] != query.from_user.id:
        conn.close(); await query.message.reply_text("🚫 لا تملك صلاحية إلغاء هذا الإعلان."); return
    if ad["status"] != "ACTIVE":
        conn.close(); await query.message.reply_text("⚠️ الإعلان غير نشط."); return
    conn.execute("UPDATE advertisements SET status='CANCELLED', updated_at=? WHERE id=? AND status='ACTIVE'", (now(), ad_id))
    db_commit(conn); conn.close()
    audit("ADVERTISEMENT_CANCELLED", user_id=ad["user_id"], admin_id=query.from_user.id if is_admin(query.from_user.id) else None, details=f"ad_id={ad_id}")
    await query.message.reply_text(f"✅ تم إلغاء الإعلان #{ad_id}.")
    if ad["user_id"] != query.from_user.id:
        await notify_user(context, ad["user_id"], f"⚠️ قام المدير بإلغاء إعلانك #{ad_id}.")


async def ad_manage_back_callback(update, context):
    query=update.callback_query
    await query.answer()
    await ad_manage_callback(update, context) if False else query.message.reply_text("اختر الإجراء من إعلانك من جديد عبر 📢 إعلاناتي.")


# ============================================================
# ADS LIST
# ============================================================

async def ads(update, context):

    expire_old_ads()

    if not await require_verification(update):

        return

    conn = db()

    rows = conn.execute(
        """
        SELECT
            a.*,
            u.username
        FROM advertisements a
        JOIN users u
          ON u.telegram_id=a.user_id
        WHERE a.status='ACTIVE'
        ORDER BY a.id DESC
        LIMIT 30
        """
    ).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "📢 لا توجد إعلانات حالياً."
        )

        return

    for row in rows:

        if row["ad_type"] == "BUY":

            title = "🔴 بيع USDT"

        else:

            title = "🟢 شراء USDT"

        text = (
            f"{title}\n\n"
            f"رقم الإعلان: #{row['id']}\n"
            f"السعر: {clean_decimal(row['price'])} "
            f"{row['currency']}\n"
            f"الكمية: {clean_decimal(row['amount'])} USDT\n"
            f"الحد الأدنى: {clean_decimal(row['min_amount'])}\n"
            f"الحد الأعلى: {clean_decimal(row['max_amount'])}\n"
            f"الدفع: {row['payment_method']}\n"
        )

        if row["note"]:

            text += (
                f"📝 {row['note']}\n"
            )

        text += "\nاختر العملية:"

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "فتح الإعلان",
                        callback_data=f"ad_view:{row['id']}",
                    )
                ]
            ]
        )

        await update.message.reply_text(
            text,
            reply_markup=keyboard,
        )


# ============================================================
# VIEW AD
# ============================================================

async def view_ad(update, context):

    expire_old_ads()
    query = update.callback_query

    await query.answer()

    ad_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM advertisements
        WHERE id=?
          AND status='ACTIVE'
        """,
        (ad_id,),
    ).fetchone()

    conn.close()

    if not row:

        await query.message.reply_text(
            "❌ الإعلان غير موجود."
        )

        return

    if row["user_id"] == query.from_user.id:

        await query.message.reply_text(
            "❌ لا يمكنك التداول مع إعلانك الخاص."
        )

        return

    title = (
        "🔴 بيع USDT"
        if row["ad_type"] == "BUY"
        else "🟢 شراء USDT"
    )

    text = (
        f"{title}\n\n"
        f"السعر: {clean_decimal(row['price'])} "
        f"{row['currency']}\n"
        f"الكمية: {clean_decimal(row['amount'])} USDT\n"
        f"الحد الأدنى: {clean_decimal(row['min_amount'])}\n"
        f"الحد الأعلى: {clean_decimal(row['max_amount'])}\n"
        f"الدفع: {row['payment_method']}"
    )

    if row["note"]:

        text += f"\n📝 {row['note']}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "بدء صفقة",
                    callback_data=f"trade_start:{ad_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ رجوع",
                    callback_data="back_ads",
                )
            ],
        ]
    )

    await query.message.reply_text(
        text,
        reply_markup=keyboard,
    )


# ============================================================
# TRADE START
# ============================================================

async def trade_start_callback(update, context):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    expire_old_ads()
    if not market_bool("market_open", True):
        await query.message.reply_text("🔴 التداول متوقف حالياً من قبل الإدارة.")
        return

    if not is_verified(user_id):

        await query.message.reply_text(
            "🔐 يجب إكمال التوثيق أولاً."
        )

        return

    ad_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    ad = conn.execute(
        """
        SELECT *
        FROM advertisements
        WHERE id=?
          AND status='ACTIVE'
        """,
        (ad_id,),
    ).fetchone()

    conn.close()

    if not ad:

        await query.message.reply_text(
            "❌ الإعلان غير متاح."
        )

        return

    if ad["user_id"] == user_id:

        await query.message.reply_text(
            "❌ لا يمكنك التداول مع إعلانك الخاص."
        )

        return

    context.user_data["trade_ad_id"] = ad_id

    await query.message.reply_text(
        "💵 أرسل كمية USDT التي تريد تنفيذها:"
    )


# ============================================================
# TEXT TRADE AMOUNT
# ============================================================

async def process_trade_amount(update, context):

    if "trade_ad_id" not in context.user_data:

        return

    user_id = update.effective_user.id

    if not is_verified(user_id):

        await update.message.reply_text(
            "🔐 أكمل التوثيق أولاً."
        )

        return

    if not market_bool("market_open", True):
        await update.message.reply_text("🔴 التداول متوقف حالياً من قبل الإدارة.")
        return

    amount = D(
        update.message.text.strip()
    )

    if amount <= 0:

        await update.message.reply_text(
            "❌ الكمية غير صحيحة."
        )

        return

    ad_id = context.user_data[
        "trade_ad_id"
    ]

    conn = db()

    ad = conn.execute(
        """
        SELECT *
        FROM advertisements
        WHERE id=?
          AND status='ACTIVE'
        """,
        (ad_id,),
    ).fetchone()

    conn.close()

    if not ad:

        await update.message.reply_text(
            "❌ الإعلان لم يعد متاحاً."
        )

        context.user_data.pop(
            "trade_ad_id",
            None,
        )

        return

    market_min = market_decimal("min_trade_amount", "10")
    minimum = max(D(ad["min_amount"]), market_min)
    maximum = D(ad["max_amount"])
    available = D(ad["amount"])

    if amount < minimum:

        await update.message.reply_text(
            f"❌ الحد الأدنى هو {clean_decimal(minimum)} USDT."
        )

        return

    if amount > maximum:

        await update.message.reply_text(
            f"❌ الحد الأعلى هو {clean_decimal(maximum)} USDT."
        )

        return

    if amount > available:

        await update.message.reply_text(
            "❌ الكمية المطلوبة أكبر من المتاح."
        )

        return

    # --------------------------------------------------------
    # Determine buyer/seller according to advertisement.
    #
    # BUY advertisement:
    # advertiser wants to BUY USDT.
    # person opening the trade is SELLER.
    #
    # SELL advertisement:
    # advertiser wants to SELL USDT.
    # person opening the trade is BUYER.
    # --------------------------------------------------------

    if ad["ad_type"] == "BUY":

        seller_id = user_id
        buyer_id = ad["user_id"]

    else:

        buyer_id = user_id
        seller_id = ad["user_id"]

    base = amount

    fee = calculate_fee(base)

    # Buyer receives after buyer fee.
    buyer_receives = base - fee

    # Seller must send base + seller fee.
    seller_sends = base + fee

    fiat_amount = (
        base * D(ad["price"])
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )

    mediator_address = get_setting(
        "mediator_address",
        "",
    )

    buyer_row = get_user(buyer_id)
    buyer_bep20_address = (buyer_row["bep20_address"] or "").strip() if buyer_row else ""

    # The local-payment destination must ALWAYS belong to the USDT seller.
    # For a BUY ad, the seller is the user who accepted the ad.
    # For a SELL ad, the seller is the advertiser.
    conn = db()
    seller_sham = conn.execute(
        """
        SELECT sham_name, sham_code
        FROM kyc_requests
        WHERE user_id=?
          AND status='APPROVED'
        ORDER BY id DESC
        LIMIT 1
        """,
        (seller_id,),
    ).fetchone()
    conn.close()

    seller_payment_account = (seller_sham["sham_code"] or "").strip() if seller_sham else ""

    if not seller_payment_account:
        await update.message.reply_text(
            "⚠️ لا يوجد حساب Sham Cash موثّق للبائع.\n\n"
            "يجب على البائع إكمال توثيق Sham Cash أولاً ثم إعادة إنشاء الصفقة."
        )
        return

    if not buyer_bep20_address:
        await update.message.reply_text(
            "⚠️ المشتري لم يضع عنوان BEP20 الخاص به بعد.\n\n"
            "يجب على المشتري أولاً إرسال عنوان محفظته عبر الأمر:\n"
            "/setaddress 0x...\n\n"
            "ثم إعادة إنشاء الصفقة."
        )
        return

    if not mediator_address:

        await update.message.reply_text(
            "⚠️ عنوان الوسيط BEP20 غير مضبوط حالياً.\n"
            "يجب على المدير ضبطه من لوحة المدير."
        )

        return

    trade_code = generate_trade_code()

    expires = (
        datetime.now(timezone.utc)
        + timedelta(
            minutes=int(
                get_setting(
                    "trade_timeout_minutes",
                    TRADE_TIMEOUT_MINUTES,
                )
            )
        )
    ).isoformat()

    conn = db()

    conn.execute(
        """
        INSERT INTO trades(
            trade_code,
            ad_id,
            buyer_id,
            seller_id,
            currency,
            requested_amount,
            buyer_receives,
            seller_sends,
            price,
            buyer_fee,
            seller_fee,
            fiat_amount,
            payment_method,
            payment_account,
            mediator_address,
            buyer_bep20_address,
            state,
            expires_at,
            created_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_code,
            ad_id,
            buyer_id,
            seller_id,
            ad["currency"],
            str(base),
            str(buyer_receives),
            str(seller_sends),
            ad["price"],
            str(fee),
            str(fee),
            str(fiat_amount),
            ad["payment_method"],
            seller_payment_account,
            mediator_address,
            buyer_bep20_address,
            "WAITING_DEPOSIT",
            expires,
            now(),
            now(),
        ),
    )

    trade_id = conn.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    db_commit(conn)
    conn.close()

    audit(
        "TRADE_CREATED",
        user_id=user_id,
        trade_id=trade_id,
        details=trade_code,
    )

    context.user_data.pop(
        "trade_ad_id",
        None,
    )

    # --------------------------------------------------------
    # Notify buyer
    # --------------------------------------------------------

    buyer_text = (
        f"🔐 صفقة جديدة\n\n"
        f"رقم الصفقة: {trade_code}\n\n"
        f"المطلوب: {clean_decimal(base)} USDT\n"
        f"ستدفع: {clean_decimal(fiat_amount)} "
        f"{ad['currency']}\n"
        f"ستستلم: {clean_decimal(buyer_receives)} USDT\n"
        f"💳 طريقة الدفع المحلي: {ad['payment_method']}\n"
        f"🏦 حساب Sham Cash للبائع (مستلم المبلغ المحلي): `{seller_payment_account}`\n"
        f"عمولة المشتري: {clean_decimal(fee)} USDT\n\n"
        f"⏱ مدة الصفقة محدودة.\n\n"
        f"حالة الصفقة: انتظار إيداع USDT لدى الوسيط."
    )

    buyer_keyboard = [
        [InlineKeyboardButton("📋 تفاصيل الصفقة", callback_data=f"trade_view:{trade_id}")]
    ]
    copy_sham = copy_value_button("📋 نسخ حساب Sham Cash", seller_payment_account)
    if copy_sham:
        buyer_keyboard.insert(0, [copy_sham])

    await notify_user(
        context,
        buyer_id,
        buyer_text,
        reply_markup=InlineKeyboardMarkup(buyer_keyboard),
    )

    seller_text = (
        f"🔐 صفقة جديدة\n\n"
        f"رقم الصفقة: {trade_code}\n\n"
        f"يجب إرسال:\n"
        f"💵 {clean_decimal(seller_sends)} USDT\n\n"
        f"إلى عنوان الوسيط على شبكة {NETWORK}:\n"
        f"`{mediator_address}`\n\n"
        f"⚠️ تنبيه مهم جداً: يجب أن يصل إلى محفظة الوسيط نفس المبلغ المكتوب أعلاه بالضبط.\n"
        f"إذا وصل المبلغ ناقصاً أو زائداً، فلن يتمكن النظام من التحقق من المعاملة تلقائياً وقد تحتاج إلى مراجعة يدوية من الإدارة.\n\n"
        f"بعد الإرسال اضغط زر تأكيد الإيداع وأرسل TXID.\n\n"
        f"⚠️ لا ترسل USDT إلى المشتري مباشرة."
    )

    seller_keyboard = []
    copy_mediator = copy_value_button("📋 نسخ عنوان الوسيط BEP20", mediator_address)
    if copy_mediator:
        seller_keyboard.append([copy_mediator])
    seller_keyboard.append([
        InlineKeyboardButton(
            "📤 تم إرسال USDT",
            callback_data=f"deposit_sent:{trade_id}",
        )
    ])
    seller_keyboard.append([
        InlineKeyboardButton(
            "📋 تفاصيل الصفقة",
            callback_data=f"trade_view:{trade_id}",
        )
    ])

    await notify_user(
        context,
        seller_id,
        seller_text,
        reply_markup=InlineKeyboardMarkup(seller_keyboard),
    )

    # Admin's initial new-trade notification shows the real KYC names
    # together with the Telegram IDs, not only the numeric IDs.
    conn = db()
    buyer_user, buyer_kyc = get_user_identity(conn, buyer_id)
    seller_user, seller_kyc = get_user_identity(conn, seller_id)
    conn.close()

    buyer_real_name = (buyer_kyc["full_name"] or "غير موثق") if buyer_kyc else "غير موثق"
    seller_real_name = (seller_kyc["full_name"] or "غير موثق") if seller_kyc else "غير موثق"
    buyer_username = f"@{buyer_user['username']}" if buyer_user and buyer_user["username"] else "-"
    seller_username = f"@{seller_user['username']}" if seller_user and seller_user["username"] else "-"

    await notify_admin(
        context,
        f"🔔 صفقة جديدة\n\n"
        f"الصفقة: {trade_code}\n"
        f"👤 المشتري: {buyer_real_name}\n"
        f"معرف تلغرام: {buyer_username}\n"
        f"Telegram ID: {buyer_id}\n\n"
        f"👤 البائع: {seller_real_name}\n"
        f"معرف تلغرام: {seller_username}\n"
        f"Telegram ID: {seller_id}\n\n"
        f"USDT: {clean_decimal(base)}\n"
        f"إجمالي إرسال البائع: {clean_decimal(seller_sends)} USDT",
    )


# ============================================================
# TRADE VIEW
# ============================================================

async def trade_view(update, context):

    query = update.callback_query

    await query.answer()

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    conn.close()

    if not trade:

        await query.message.reply_text(
            "❌ الصفقة غير موجودة."
        )

        return

    if query.from_user.id not in (
        trade["buyer_id"],
        trade["seller_id"],
        ADMIN_ID,
    ):

        await query.message.reply_text(
            "🚫 غير مصرح لك."
        )

        return

    text = (
        f"🔐 الصفقة {trade['trade_code']}\n\n"
        f"الحالة: {trade['state']}\n"
        f"الكمية: {clean_decimal(trade['requested_amount'])} USDT\n"
        f"المشتري يستلم: {clean_decimal(trade['buyer_receives'])} USDT\n"
        f"البائع يرسل: {clean_decimal(trade['seller_sends'])} USDT\n"
        f"السعر: {clean_decimal(trade['price'])} {trade['currency']}\n"
        f"المبلغ المحلي: {clean_decimal(trade['fiat_amount'])} {trade['currency']}\n"
        f"عمولة المشتري: {clean_decimal(trade['buyer_fee'])} USDT\n"
        f"عمولة البائع: {clean_decimal(trade['seller_fee'])} USDT\n"
        f"الشبكة: {NETWORK}\n"
    )

    keyboard_rows = []

    if query.from_user.id == trade["seller_id"]:

        if trade["state"] == "WAITING_DEPOSIT":

            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        "📤 تم إرسال USDT",
                        callback_data=f"deposit_sent:{trade_id}",
                    )
                ]
            )

    if query.from_user.id == trade["buyer_id"]:

        if trade["state"] in (
            "USDT_CONFIRMED",
            "WAITING_LOCAL_PAYMENT",
        ):

            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        "💳 تم الدفع",
                        callback_data=f"payment_sent:{trade_id}",
                    )
                ]
            )

    if trade["state"] not in (
        "COMPLETED",
        "CANCELLED",
        "EXPIRED",
    ):

        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    "⚠️ فتح نزاع",
                    callback_data=f"dispute:{trade_id}",
                )
            ]
        )

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard_rows
        )
        if keyboard_rows
        else None,
    )


# ============================================================
# SELLER DEPOSIT SENT
# ============================================================

async def deposit_sent_callback(
    update,
    context,
):

    query = update.callback_query

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    conn.close()

    if not trade:

        return

    if query.from_user.id != trade["seller_id"]:

        await query.answer(
            "هذا الزر للبائع فقط.",
            show_alert=True,
        )

        return

    await query.answer()

    if trade["state"] != "WAITING_DEPOSIT":

        await query.message.reply_text(
            "⚠️ لا يمكن تنفيذ هذه الخطوة الآن."
        )

        return

    context.user_data[
        "deposit_trade_id"
    ] = trade_id

    await query.message.reply_text(
        "📤 أرسل TXID الخاص بتحويل USDT إلى عنوان الوسيط BEP20."
    )

    # Keep the seller inside the deposit conversation so the next
    # normal text message (the TXID) is handled by deposit_txid
    # instead of the global text router.
    return SELLER_DEPOSIT_TXID



async def deposit_txid(update, context):

    trade_id = context.user_data.get("deposit_trade_id")
    if not trade_id:
        return

    txid = update.message.text.strip()
    if not normalize_txid(txid):
        await update.message.reply_text("❌ TXID غير صحيح. يجب أن يكون TXID على شبكة BSC.")
        return SELLER_DEPOSIT_TXID

    conn = db()
    trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not trade:
        conn.close()
        return ConversationHandler.END

    if update.effective_user.id != trade["seller_id"]:
        conn.close()
        return ConversationHandler.END

    seller_row = conn.execute(
        "SELECT bep20_address FROM users WHERE telegram_id=?",
        (trade["seller_id"],),
    ).fetchone()
    seller_address = (seller_row["bep20_address"] or "").strip() if seller_row else ""

    check = await verify_bep20_usdt_deposit(
        txid,
        seller_address,
        trade["mediator_address"],
        trade["seller_sends"],
    )

    conn.execute(
        """
        UPDATE trades
        SET seller_deposit_txid=?,
            seller_deposit_check_status=?,
            seller_deposit_check_reason=?,
            seller_deposit_check_block=?,
            state='DEPOSIT_PENDING_CONFIRMATION',
            updated_at=?
        WHERE id=? AND state='WAITING_DEPOSIT'
        """,
        (
            txid,
            "VALID" if check["ok"] else "INVALID_OR_UNVERIFIED",
            check["reason"],
            str(check.get("block_number") or ""),
            now(),
            trade_id,
        ),
    )
    db_commit(conn)
    conn.close()

    audit(
        "SELLER_DEPOSIT_TXID_SUBMITTED",
        user_id=update.effective_user.id,
        trade_id=trade_id,
        details=f"{txid} | {check['reason']}",
    )

    if check["ok"]:
        await update.message.reply_text(
            "✅ تم فحص TXID تلقائياً ووجدنا تحويل USDT مطابقاً.\n"
            "⏳ بانتظار موافقة المدير النهائية."
        )
    else:
        await update.message.reply_text(
            "⚠️ تم تسجيل TXID، لكن الفحص التلقائي لم يؤكد الإيداع.\n\n"
            f"النتيجة: {check['reason']}\n\n"
            "سيقوم المدير بالمراجعة."
        )

    status_text = "🟢 الفحص التلقائي: ناجح" if check["ok"] else "🟠 الفحص التلقائي: غير مؤكد"
    confirmations_text = ""
    if check.get("confirmations") is not None:
        confirmations_text = f"\nالتأكيدات الحالية: {check['confirmations']}"

    await notify_admin(
        context,
        f"💰 إيداع USDT بانتظار موافقة المدير\n\n"
        f"الصفقة: {trade['trade_code']}\n"
        f"البائع: {trade['seller_id']}\n"
        f"المبلغ المتوقع: {clean_decimal(trade['seller_sends'])} USDT\n"
        f"TXID:\n{txid}\n\n"
        f"{status_text}\n"
        f"نتيجة الفحص: {check['reason']}"
        f"{confirmations_text}",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ موافقة المدير", callback_data=f"deposit_confirm:{trade_id}"),
                    InlineKeyboardButton("❌ رفض الإيداع", callback_data=f"deposit_reject:{trade_id}"),
                ]
            ]
        ),
    )

    context.user_data.pop("deposit_trade_id", None)
    return ConversationHandler.END


# ============================================================
# ADMIN CONFIRM DEPOSIT
# ============================================================

async def deposit_admin_callback(update, context):

    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    action, trade_id_text = query.data.split(":")
    trade_id = int(trade_id_text)

    conn = db()
    trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not trade:
        conn.close()
        return

    if action in ("deposit_confirm", "deposit_manual_confirm"):
        check = {"ok": True, "reason": "تمت الموافقة اليدوية من المدير."}
        if action == "deposit_confirm":
            # Re-check immediately before normal approval to prevent approving
            # a stale, replaced, or previously unconfirmed TXID.
            seller_row = conn.execute(
                "SELECT bep20_address FROM users WHERE telegram_id=?",
                (trade["seller_id"],),
            ).fetchone()
            seller_address = (seller_row["bep20_address"] or "").strip() if seller_row else ""

            check = await verify_bep20_usdt_deposit(
                trade["seller_deposit_txid"],
                seller_address,
                trade["mediator_address"],
                trade["seller_sends"],
            )

        if not check["ok"]:
            conn.execute(
                """
                UPDATE trades
                SET seller_deposit_check_status=?,
                    seller_deposit_check_reason=?,
                    updated_at=?
                WHERE id=?
                """,
                ("INVALID_OR_UNVERIFIED", check["reason"], now(), trade_id),
            )
            db_commit(conn)
            conn.close()
            await query.message.reply_text(
                "⚠️ الفحص التلقائي لم يؤكد الإيداع.\n\n"
                f"النتيجة: {check['reason']}\n\n"
                "القرار النهائي للمدير بعد المراجعة اليدوية.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⚠️ موافقة يدوية رغم التحذير",
                                callback_data=f"deposit_manual_confirm:{trade_id}",
                            )
                        ]
                    ]
                ),
            )
            return

        changed = conn.execute(
            """
            UPDATE trades
            SET seller_deposit_amount=?,
                seller_deposit_check_status=?,
                seller_deposit_check_reason=?,
                seller_deposit_check_block=?,
                state='WAITING_LOCAL_PAYMENT',
                updated_at=?
            WHERE id=?
              AND state='DEPOSIT_PENDING_CONFIRMATION'
            """,
            (
                trade["seller_sends"],
                "VALID" if action == "deposit_confirm" else "MANUAL_APPROVED",
                check["reason"],
                str(check.get("block_number") or ""),
                now(),
                trade_id,
            ),
        ).rowcount

        db_commit(conn)
        conn.close()

        if not changed:
            await query.message.reply_text("⚠️ تم التعامل مع هذه الصفقة مسبقاً أو لم تعد بانتظار التأكيد.")
            return

        audit(
            "USDT_DEPOSIT_CONFIRMED",
            trade_id=trade_id,
            admin_id=ADMIN_ID,
            details=trade["seller_deposit_txid"],
        )

        await notify_user(
            context,
            trade["seller_id"],
            f"✅ تم تأكيد استلام USDT للصفقة {trade['trade_code']}.\n"
            "بانتظار إتمام الدفع المحلي.",
        )

        await notify_user(
            context,
            trade["buyer_id"],
            f"✅ تم تأكيد إيداع USDT للصفقة {trade['trade_code']}.\n\n"
            f"💳 ادفع الآن:\n"
            f"{clean_decimal(trade['fiat_amount'])} {trade['currency']}\n\n"
            f"طريقة الدفع:\n"
            f"{trade['payment_method']}\n\n"
            f"الحساب:\n"
            f"{trade['payment_account']}\n\n"
            f"بعد الدفع استخدم زر «تم الدفع» وارفع الإثبات.",
            reply_markup=InlineKeyboardMarkup(
                ([ [copy_value_button("📋 نسخ حساب Sham Cash", trade["payment_account"])] ]
                 if copy_value_button("📋 نسخ حساب Sham Cash", trade["payment_account"])
                 else [])
                + [[InlineKeyboardButton("💳 تم الدفع", callback_data=f"payment_sent:{trade_id}")]]
            ),
        )

        await query.message.reply_text("✅ تم فحص TXID وتأكيد إيداع USDT بعد موافقة المدير.")

    else:
        conn.execute(
            """
            UPDATE trades
            SET state='WAITING_DEPOSIT',
                seller_deposit_txid=NULL,
                seller_deposit_check_status=NULL,
                seller_deposit_check_reason=NULL,
                seller_deposit_check_block=NULL,
                updated_at=?
            WHERE id=?
            """,
            (now(), trade_id),
        )
        db_commit(conn)
        conn.close()

        await notify_user(
            context,
            trade["seller_id"],
            f"❌ لم يتم تأكيد إيداع USDT للصفقة {trade['trade_code']}.\n"
            "تحقق من التحويل وأرسل TXID صحيحاً.",
        )
        await query.message.reply_text("❌ تم رفض الإيداع وإعادة الصفقة لانتظار TXID جديد.")


# ============================================================
# BUYER PAYMENT
# ============================================================

async def payment_sent_callback(
    update,
    context,
):

    query = update.callback_query

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    conn.close()

    if not trade:

        return

    if query.from_user.id != trade["buyer_id"]:

        await query.answer(
            "هذا الزر للمشتري فقط.",
            show_alert=True,
        )

        return

    await query.answer()

    if trade["state"] != "WAITING_LOCAL_PAYMENT":

        await query.message.reply_text(
            "⚠️ لا يمكن تنفيذ هذه الخطوة الآن."
        )

        return

    context.user_data[
        "payment_trade_id"
    ] = trade_id

    await query.message.reply_text(
        "📸 أرسل صورة إثبات الدفع من Sham Cash."
    )

    # Keep the buyer inside the payment conversation so the next
    # photo is handled by payment_proof instead of the global router.
    return PAYMENT_PROOF


async def payment_proof(update, context):

    trade_id = context.user_data.get(
        "payment_trade_id"
    )

    if not trade_id:

        return ConversationHandler.END

    if not update.message.photo:

        await update.message.reply_text(
            "❌ يجب إرسال صورة إثبات الدفع."
        )

        return PAYMENT_PROOF

    file_id = update.message.photo[-1].file_id

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    if not trade:

        conn.close()

        return ConversationHandler.END

    if update.effective_user.id != trade["buyer_id"]:

        conn.close()

        return ConversationHandler.END

    conn.execute(
        """
        UPDATE trades
        SET
            payment_proof_file_id=?,
            state='PAYMENT_PROOF_SENT',
            updated_at=?
        WHERE id=?
          AND state='WAITING_LOCAL_PAYMENT'
        """,
        (
            file_id,
            now(),
            trade_id,
        ),
    )

    conn.execute(
        """
        INSERT INTO proofs(
            trade_id,
            user_id,
            proof_type,
            file_id,
            created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            trade_id,
            update.effective_user.id,
            "LOCAL_PAYMENT",
            file_id,
            now(),
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "PAYMENT_PROOF_SUBMITTED",
        user_id=update.effective_user.id,
        trade_id=trade_id,
    )

    await update.message.reply_text(
        "✅ تم إرسال إثبات الدفع.\n"
        "بانتظار تأكيد الطرف الآخر/المدير حسب حالة الصفقة."
    )

    await notify_user(
        context,
        trade["seller_id"],
        f"💳 تم إرسال إثبات دفع للصفقة {trade['trade_code']}.\n\n"
        "تحقق من وصول المبلغ في حسابك Sham Cash.\n"
        "لا تؤكد الاستلام قبل التحقق الفعلي.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ أؤكد استلام المبلغ",
                        callback_data=f"payment_confirm:{trade_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚠️ فتح نزاع",
                        callback_data=f"dispute:{trade_id}",
                    )
                ]
            ]
        ),
    )

    # Send the actual payment-proof image to the mediator/admin,
    # not only a text notification. The release button is intentionally
    # NOT shown yet: the seller must first confirm receipt of the local
    # payment. After that confirmation, payment_confirm_callback sends
    # the mediator the release order.
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=file_id,
            caption=(
                f"💳 إثبات دفع جديد\n\n"
                f"الصفقة: {trade['trade_code']}\n"
                f"المبلغ: {clean_decimal(trade['fiat_amount'])} {trade['currency']}\n"
                f"المشتري: {trade['buyer_id']}\n"
                f"البائع: {trade['seller_id']}\n\n"
                "⏳ بانتظار تأكيد البائع لاستلام المبلغ المحلي."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📋 الصفقة",
                            callback_data=f"admin_trade:{trade_id}",
                        )
                    ]
                ]
            ),
        )
    except Exception:
        # Keep the normal admin notification as a fallback if Telegram
        # rejects the photo message for any reason.
        await notify_admin(
            context,
            f"💳 إثبات دفع جديد\n\n"
            f"الصفقة: {trade['trade_code']}\n"
            f"المبلغ: {clean_decimal(trade['fiat_amount'])} {trade['currency']}\n"
            f"المشتري: {trade['buyer_id']}\n"
            f"البائع: {trade['seller_id']}\n\n"
            "⚠️ تعذر إرسال صورة الإثبات، افتح الصفقة للمراجعة.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📋 الصفقة",
                            callback_data=f"admin_trade:{trade_id}",
                        )
                    ]
                ]
            ),
        )

    context.user_data.pop(
        "payment_trade_id",
        None,
    )

    return ConversationHandler.END


# ============================================================
# SELLER CONFIRMS PAYMENT
# ============================================================

async def payment_confirm_callback(
    update,
    context,
):

    query = update.callback_query

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    if not trade:

        conn.close()

        return

    if query.from_user.id != trade["seller_id"]:

        conn.close()

        await query.answer(
            "هذا الزر للبائع.",
            show_alert=True,
        )

        return

    await query.answer()

    if trade["state"] != "PAYMENT_PROOF_SENT":

        conn.close()

        await query.message.reply_text(
            "⚠️ لا يمكن تأكيد الدفع الآن."
        )

        return

    conn.execute(
        """
        UPDATE trades
        SET
            state='PAYMENT_CONFIRMED',
            updated_at=?
        WHERE id=?
          AND state='PAYMENT_PROOF_SENT'
        """,
        (
            now(),
            trade_id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "PAYMENT_CONFIRMED",
        user_id=query.from_user.id,
        trade_id=trade_id,
    )

    await query.message.reply_text(
        "✅ تم تسجيل تأكيد استلام المبلغ.\n"
        "الصفقة بانتظار تحرير USDT من المدير."
    )

    await notify_admin(
        context,
        f"🟡 الصفقة جاهزة لتحرير USDT\n\n"
        f"الصفقة: {trade['trade_code']}\n"
        f"USDT للمشتري: {clean_decimal(trade['buyer_receives'])}\n"
        f"المشتري: {trade['buyer_id']}\n"
        f"البائع: {trade['seller_id']}\n\n"
        "تحقق من كل شيء قبل التحرير.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💰 تحرير USDT",
                        callback_data=f"release_start:{trade_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚠️ نزاع",
                        callback_data=f"dispute:{trade_id}",
                    )
                ]
            ]
        ),
    )


# ============================================================
# ADMIN RELEASE
# ============================================================

async def admin_recheck_start(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 غير مصرح.")
        return ConversationHandler.END

    context.user_data.pop("recheck_trade_id", None)
    await update.message.reply_text(
        "🔄 إعادة فحص إيداع USDT\n\n"
        "أرسل رقم الصفقة أو كود الصفقة التي تريد إعادة فحص إيداعها."
    )
    return ADMIN_RECHECK_TRADE


async def admin_recheck_trade(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    value = (update.message.text or "").strip()
    conn = db()
    trade = conn.execute(
        "SELECT * FROM trades WHERE id=? OR trade_code=?",
        (int(value), value) if value.isdigit() else (-1, value),
    ).fetchone()
    conn.close()

    if not trade:
        await update.message.reply_text(
            "❌ لم أجد هذه الصفقة.\nأرسل رقم الصفقة أو كود الصفقة مرة أخرى."
        )
        return ADMIN_RECHECK_TRADE

    if not trade["seller_deposit_txid"]:
        await update.message.reply_text(
            f"⚠️ الصفقة {trade['trade_code']} لا يوجد لها TXID إيداع مسجل بعد."
        )
        return ConversationHandler.END

    context.user_data["recheck_trade_id"] = trade["id"]
    await update.message.reply_text(
        f"🔄 الصفقة: {trade['trade_code']}\n"
        f"💰 المبلغ المطلوب: {clean_decimal(trade['seller_sends'])} USDT\n\n"
        f"TXID المسجل حالياً:\n{trade['seller_deposit_txid']}\n\n"
        "📤 أرسل TXID الذي تريد فحصه الآن.\n"
        "يمكنك إرسال نفس TXID مرة أخرى إذا كنت تنتظر تأكيدات إضافية."
    )
    return ADMIN_RECHECK_TXID


async def admin_recheck_txid(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    trade_id = context.user_data.get("recheck_trade_id")
    txid = normalize_txid((update.message.text or "").strip())
    if not trade_id:
        return ConversationHandler.END

    if not txid:
        await update.message.reply_text(
            "❌ TXID غير صحيح. أرسل TXID صالحاً على شبكة BSC."
        )
        return ADMIN_RECHECK_TXID

    conn = db()
    trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not trade:
        conn.close()
        await update.message.reply_text("❌ الصفقة غير موجودة.")
        return ConversationHandler.END

    seller_row = conn.execute(
        "SELECT bep20_address FROM users WHERE telegram_id=?",
        (trade["seller_id"],),
    ).fetchone()
    seller_address = (seller_row["bep20_address"] or "").strip() if seller_row else ""
    conn.close()

    await update.message.reply_text("🔎 جارٍ فحص المعاملة على شبكة BSC...")
    check = await verify_bep20_usdt_deposit(
        txid,
        seller_address,
        trade["mediator_address"],
        trade["seller_sends"],
    )

    confirmations = check.get("confirmations")
    confirmations_text = (
        f"\n🔢 التأكيدات الحالية: {confirmations}"
        if confirmations is not None else ""
    )

    audit(
        "ADMIN_RECHECK_DEPOSIT_TXID",
        user_id=update.effective_user.id,
        trade_id=trade_id,
        admin_id=update.effective_user.id,
        details=f"{txid} | {check.get('reason', '')}",
    )

    if check["ok"]:
        # Keep the verified TXID so a later admin approval re-checks the
        # same transaction rather than an older/stale TXID.
        conn = db()
        conn.execute(
            "UPDATE trades SET seller_deposit_txid=?, seller_deposit_check_status=?, seller_deposit_check_reason=?, seller_deposit_check_block=?, updated_at=? WHERE id=?",
            (
                txid,
                "VALID",
                check.get("reason", ""),
                str(check.get("block_number") or ""),
                now(),
                trade_id,
            ),
        )
        db_commit(conn)
        conn.close()

        text = (
            "✅ نتيجة إعادة الفحص: الإيداع مطابق.\n\n"
            f"الصفقة: {trade['trade_code']}\n"
            f"المبلغ: {clean_decimal(trade['seller_sends'])} USDT\n"
            f"TXID: {txid}\n"
            f"النتيجة: {check['reason']}"
            f"{confirmations_text}"
        )
        keyboard = None
        if trade["state"] == "DEPOSIT_PENDING_CONFIRMATION":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ موافقة المدير على الإيداع",
                    callback_data=f"deposit_confirm:{trade_id}",
                )
            ]])
            text += "\n\nالصفقة ما زالت بانتظار موافقة المدير."
        else:
            text += f"\n\nالحالة الحالية للصفقة: {trade['state']}"
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "⚠️ نتيجة إعادة الفحص: لم يتم تأكيد الإيداع.\n\n"
            f"الصفقة: {trade['trade_code']}\n"
            f"TXID: {txid}\n"
            f"السبب: {check['reason']}"
            f"{confirmations_text}\n\n"
            "يمكنك إعادة الفحص لاحقاً بنفس الزر إذا تأخر ظهور الرصيد أو التأكيدات."
        )

    context.user_data.pop("recheck_trade_id", None)
    return ConversationHandler.END


async def release_start_callback(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):

        return

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    conn.close()

    if not trade:

        return

    if trade["state"] != "PAYMENT_CONFIRMED":

        await query.message.reply_text(
            "❌ الصفقة ليست جاهزة للتحرير."
        )

        return

    buyer_address = (trade["buyer_bep20_address"] or "").strip()
    if not buyer_address:
        await query.message.reply_text(
            "❌ لا يوجد عنوان BEP20 محفوظ للمشتري في هذه الصفقة.\n"
            "لا تقم بالتحويل قبل إضافة العنوان والتحقق منه."
        )
        return

    await query.message.reply_text(
        f"💰 تحرير USDT\n\n"
        f"الصفقة: {trade['trade_code']}\n"
        f"المبلغ الذي يجب إرساله للمشتري:\n"
        f"{clean_decimal(trade['buyer_receives'])} USDT\n\n"
        f"📬 عنوان المشتري BEP20:\n`{buyer_address}`\n\n"
        f"🌐 الشبكة: {NETWORK}\n\n"
        "تحقق من العنوان والمبلغ والشبكة، ثم قم بالتحويل من محفظة الوسيط.\n"
        "بعد إتمام التحويل أرسل TXID هنا.",
        reply_markup=release_keyboard,
    )

    release_copy = copy_value_button("📋 نسخ عنوان BEP20", buyer_address)
    release_keyboard = InlineKeyboardMarkup([[release_copy]]) if release_copy else None

    context.user_data[
        "release_trade_id"
    ] = trade_id

    # Keep the manager inside the release conversation so the next
    # text message (the TXID) is handled by release_txid rather than
    # the global text router.
    return ADMIN_RELEASE_TXID


async def release_txid(update, context):

    trade_id = context.user_data.get(
        "release_trade_id"
    )

    if not trade_id:

        return ConversationHandler.END

    if not is_admin(update.effective_user.id):

        return ConversationHandler.END

    txid = update.message.text.strip()

    if len(txid) < 10:

        await update.message.reply_text(
            "❌ TXID غير صحيح."
        )

        return ADMIN_RELEASE_TXID

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    if not trade:

        conn.close()

        return ConversationHandler.END

    if trade["state"] != "PAYMENT_CONFIRMED":

        conn.close()

        await update.message.reply_text(
            "❌ حالة الصفقة لا تسمح بالتحرير."
        )

        return ConversationHandler.END

    # Idempotency protection.
    if trade["release_txid"]:

        conn.close()

        await update.message.reply_text(
            "⚠️ تم تسجيل TXID للتحرير مسبقاً."
        )

        return ConversationHandler.END

    conn.execute(
        """
        UPDATE trades
        SET
            release_txid=?,
            state='COMPLETED',
            updated_at=?
        WHERE id=?
          AND state='PAYMENT_CONFIRMED'
          AND release_txid IS NULL
        """,
        (
            txid,
            now(),
            trade_id,
        ),
    )

    changed = conn.execute("SELECT changes()").fetchone()[0]
    if changed == 1:
        # The advertisement amount is reduced only when the trade is actually completed.
        consume_ad_amount(conn, trade["ad_id"], trade["requested_amount"])
    db_commit(conn)
    conn.close()

    if changed != 1:
        await update.message.reply_text(
            "⚠️ لم يتم تسجيل TXID لأن حالة الصفقة تغيرت. افتح الصفقة وتحقق منها."
        )
        context.user_data.pop("release_trade_id", None)
        return ConversationHandler.END

    # Commission records.
    conn = db()

    conn.execute(
        """
        INSERT INTO commissions(
            trade_id,
            user_id,
            currency,
            amount,
            side,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            trade_id,
            trade["buyer_id"],
            "USDT",
            trade["buyer_fee"],
            "BUYER",
            now(),
        ),
    )

    conn.execute(
        """
        INSERT INTO commissions(
            trade_id,
            user_id,
            currency,
            amount,
            side,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            trade_id,
            trade["seller_id"],
            "USDT",
            trade["seller_fee"],
            "SELLER",
            now(),
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "USDT_RELEASED",
        admin_id=ADMIN_ID,
        trade_id=trade_id,
        details=txid,
    )

    await update.message.reply_text(
        "✅ تم تسجيل تحرير USDT وإغلاق الصفقة."
    )

    await notify_user(
        context,
        trade["buyer_id"],
        f"🎉 اكتملت الصفقة {trade['trade_code']}.\n\n"
        f"تم إرسال {clean_decimal(trade['buyer_receives'])} USDT.\n"
        f"TXID:\n{txid}",
    )

    await notify_user(
        context,
        trade["seller_id"],
        f"✅ اكتملت الصفقة {trade['trade_code']}.\n\n"
        "تم تأكيد استلام المبلغ المحلي وتحرير USDT للمشتري.",
    )

    context.user_data.pop(
        "release_trade_id",
        None,
    )

    return ConversationHandler.END


# ============================================================
# DISPUTE CHAT
# ============================================================

def get_open_dispute_for_trade(trade_id):
    conn=db()
    row=conn.execute("SELECT * FROM disputes WHERE trade_id=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (trade_id,)).fetchone()
    conn.close()
    return row


def dispute_parties(dispute_id):
    conn=db()
    row=conn.execute("""
        SELECT d.*, t.buyer_id, t.seller_id, t.trade_code
        FROM disputes d JOIN trades t ON t.id=d.trade_id WHERE d.id=?
    """, (dispute_id,)).fetchone()
    conn.close()
    return row


async def dispute_admin_chat_callback(update, context):
    query=update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    dispute_id=int(query.data.split(":")[1]); side=query.data.split(":")[2]
    d=dispute_parties(dispute_id)
    if not d or d["status"] != "OPEN":
        await query.message.reply_text("⚠️ النزاع غير مفتوح."); return
    target=d["user_id"] if side=="opener" else (d["seller_id"] if d["user_id"]==d["buyer_id"] else d["buyer_id"])
    context.user_data["dispute_admin_id"]=dispute_id
    context.user_data["dispute_admin_target"]=target
    await query.message.reply_text(f"💬 أنت الآن تتحدث مع المستخدم {target}.\nأرسل رسالتك:")


async def dispute_user_chat_callback(update, context):
    query=update.callback_query
    await query.answer()
    dispute_id=int(query.data.split(":")[1])
    d=dispute_parties(dispute_id)
    if not d or d["status"] != "OPEN" or query.from_user.id not in (d["buyer_id"],d["seller_id"]):
        await query.message.reply_text("⚠️ لا يمكن الرد على هذا النزاع الآن."); return
    context.user_data["dispute_user_id"]=dispute_id
    await query.message.reply_text("💬 أرسل ردك للمدير. يمكنك إرسال رسالة أو صورة دليل.")


def save_dispute_message(dispute_id, sender_id, recipient_id, message_type="TEXT", message_text=None, file_id=None):
    conn=db(); conn.execute("INSERT INTO dispute_messages(dispute_id,sender_id,recipient_id,message_type,message_text,file_id,created_at) VALUES(?,?,?,?,?,?,?)", (dispute_id,sender_id,recipient_id,message_type,message_text,file_id,now())); db_commit(conn); conn.close()


async def dispute_admin_text(update, context):
    dispute_id=context.user_data.get("dispute_admin_id"); target=context.user_data.get("dispute_admin_target")
    if not dispute_id or not target: return False
    d=dispute_parties(dispute_id)
    if not d or d["status"]!="OPEN":
        context.user_data.pop("dispute_admin_id",None); context.user_data.pop("dispute_admin_target",None); return False
    text=(update.message.text or "").strip()
    if not text: return True
    save_dispute_message(dispute_id, update.effective_user.id, target, "TEXT", text)
    await notify_user(context,target,f"💬 رسالة من المدير بخصوص النزاع #{dispute_id}:\n\n{text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 الرد على المدير", callback_data=f"dispute_user_chat:{dispute_id}")]]))
    await update.message.reply_text("✅ تم إرسال الرسالة.")
    return True


async def dispute_user_text(update, context):
    dispute_id=context.user_data.get("dispute_user_id")
    if not dispute_id: return False
    d=dispute_parties(dispute_id)
    if not d or d["status"]!="OPEN" or update.effective_user.id not in (d["buyer_id"],d["seller_id"]):
        context.user_data.pop("dispute_user_id",None); return False
    text=(update.message.text or "").strip()
    if not text: return True
    save_dispute_message(dispute_id, update.effective_user.id, ADMIN_ID, "TEXT", text)
    await notify_admin(context,f"💬 رد من طرف في النزاع #{dispute_id}\nالمستخدم: {update.effective_user.id}\n\n{text}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 الرد على هذا الطرف", callback_data=f"dispute_admin_chat:{dispute_id}:opener")]]))
    await update.message.reply_text("✅ تم إرسال ردك للمدير.")
    return True


async def dispute_user_photo(update, context):
    dispute_id=context.user_data.get("dispute_user_id")
    if not dispute_id: return False
    d=dispute_parties(dispute_id)
    if not d or d["status"]!="OPEN" or update.effective_user.id not in (d["buyer_id"],d["seller_id"]):
        return False
    file_id=update.message.photo[-1].file_id
    save_dispute_message(dispute_id, update.effective_user.id, ADMIN_ID, "PHOTO", None, file_id)
    try:
        await context.bot.send_photo(chat_id=ADMIN_ID, photo=file_id, caption=f"💬 دليل/صورة في النزاع #{dispute_id}\nالمستخدم: {update.effective_user.id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 فتح محادثته", callback_data=f"dispute_admin_chat:{dispute_id}:opener")]]))
    except Exception: pass
    await update.message.reply_text("✅ تم إرسال الصورة للمدير.")
    return True


async def dispute_open_callback(update, context, dispute_id, reason, opener_id):
    d=dispute_parties(dispute_id)
    if not d: return
    other=d["seller_id"] if opener_id==d["buyer_id"] else d["buyer_id"]
    markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 محادثة مع صاحب النزاع", callback_data=f"dispute_admin_chat:{dispute_id}:opener")],
        [InlineKeyboardButton("💬 محادثة مع الطرف الآخر", callback_data=f"dispute_admin_chat:{dispute_id}:other")],
        [InlineKeyboardButton("📋 تفاصيل الصفقة", callback_data=f"admin_trade:{d['trade_id']}")],
        [InlineKeyboardButton("⚖️ الحكم لصالح المشتري", callback_data=f"dispute_resolve:{dispute_id}:buyer")],
        [InlineKeyboardButton("⚖️ الحكم لصالح البائع", callback_data=f"dispute_resolve:{dispute_id}:seller")],
        [InlineKeyboardButton("❌ رفض النزاع", callback_data=f"dispute_resolve:{dispute_id}:reject")],
    ])
    await notify_admin(context,f"⚠️ نزاع جديد\n\nالنزاع: #{dispute_id}\nالصفقة: {d['trade_code']}\nصاحب النزاع: {opener_id}\nالطرف الآخر: {other}\n\nالسبب:\n{reason}",reply_markup=markup)
    await notify_user(context,other,f"⚠️ تم فتح نزاع على الصفقة {d['trade_code']}.\n\nيمكنك إرسال ردك وأي أدلة للمدير.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 الرد على المدير", callback_data=f"dispute_user_chat:{dispute_id}")]]))


# ============================================================
# DISPUTE
# ============================================================

async def dispute_callback(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    conn.close()

    if not trade:

        return

    if query.from_user.id not in (
        trade["buyer_id"],
        trade["seller_id"],
        ADMIN_ID,
    ):

        return

    context.user_data[
        "dispute_trade_id"
    ] = trade_id

    await query.message.reply_text(
        "⚠️ اشرح سبب فتح النزاع:"
    )


async def dispute_reason(update, context):

    trade_id = context.user_data.get(
        "dispute_trade_id"
    )

    if not trade_id:

        return

    reason = update.message.text.strip()

    if len(reason) < 3:

        await update.message.reply_text(
            "❌ اكتب سبب النزاع."
        )

        return

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    if not trade:
        conn.close()
        context.user_data.pop("dispute_trade_id", None)
        await update.message.reply_text("⚠️ الصفقة غير موجودة.")
        return

    if update.effective_user.id not in (trade["buyer_id"], trade["seller_id"], ADMIN_ID):
        conn.close()
        context.user_data.pop("dispute_trade_id", None)
        await update.message.reply_text("⚠️ لا تملك صلاحية فتح نزاع على هذه الصفقة.")
        return

    if trade["state"] in ("COMPLETED", "CANCELLED", "EXPIRED", "DISPUTED"):
        conn.close()
        context.user_data.pop("dispute_trade_id", None)
        await update.message.reply_text("⚠️ لا يمكن فتح نزاع على هذه الصفقة في حالتها الحالية.")
        return

    existing = conn.execute(
        "SELECT id FROM disputes WHERE trade_id=? AND status IN ('OPEN','PENDING_SETTLEMENT') LIMIT 1",
        (trade_id,),
    ).fetchone()
    if existing:
        conn.close()
        context.user_data.pop("dispute_trade_id", None)
        await update.message.reply_text("⚠️ يوجد نزاع مفتوح لهذه الصفقة بالفعل.")
        return

    conn.execute(
        """
        INSERT INTO disputes(
            trade_id,
            user_id,
            reason,
            status,
            previous_trade_state,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            trade_id,
            update.effective_user.id,
            reason,
            "OPEN",
            trade["state"],
            now(),
        ),
    )

    conn.execute(
        """
        UPDATE trades
        SET
            state='DISPUTED',
            dispute_reason=?,
            updated_at=?
        WHERE id=?
          AND state NOT IN(
              'COMPLETED',
              'CANCELLED',
              'EXPIRED'
          )
        """,
        (
            reason,
            now(),
            trade_id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "DISPUTE_OPENED",
        user_id=update.effective_user.id,
        trade_id=trade_id,
        details=reason,
    )

    await update.message.reply_text(
        "⚠️ تم فتح النزاع.\n"
        "أوقفنا إجراءات التحرير حتى مراجعة المدير."
    )

    dispute = get_open_dispute_for_trade(trade_id)
    if dispute:
        await dispute_open_callback(context=context, update=update, dispute_id=dispute["id"], reason=reason, opener_id=update.effective_user.id)

    context.user_data.pop(
        "dispute_trade_id",
        None,
    )



# ============================================================
# DISPUTE RESOLUTION
# ============================================================

async def dispute_resolve_callback(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("🚫 غير مصرح.")
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return

    dispute_id = int(parts[1])
    decision = parts[2].upper()
    if decision not in ("BUYER", "SELLER", "REJECT"):
        return

    conn = db()
    dispute = conn.execute(
        "SELECT * FROM disputes WHERE id=? AND status='OPEN'",
        (dispute_id,),
    ).fetchone()
    if not dispute:
        conn.close()
        await query.message.reply_text("⚠️ هذا النزاع غير مفتوح أو تمت معالجته مسبقًا.")
        return

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (dispute["trade_id"],),
    ).fetchone()
    if not trade or trade["state"] != "DISPUTED":
        conn.close()
        await query.message.reply_text("⚠️ حالة الصفقة لا تسمح بحسم النزاع.")
        return

    # Rejecting a false/unproven dispute needs no blockchain transfer.
    if decision == "REJECT":
        previous_state = dispute["previous_trade_state"] or "PAYMENT_PROOF_SENT"
        conn.execute(
            "UPDATE disputes SET status='RESOLVED', admin_decision=?, resolved_at=? WHERE id=? AND status='OPEN'",
            ("REJECTED", now(), dispute_id),
        )
        conn.execute(
            "UPDATE trades SET state=?, updated_at=? WHERE id=? AND state='DISPUTED'",
            (previous_state, now(), trade["id"]),
        )
        db_commit(conn)
        conn.close()

        audit("DISPUTE_REJECTED", user_id=query.from_user.id, trade_id=trade["id"], details=f"dispute_id={dispute_id}")
        await query.message.reply_text("✅ تم رفض النزاع وإعادة الصفقة إلى حالتها السابقة.")
        await notify_user(context, trade["buyer_id"], f"⚖️ تم حسم النزاع #{dispute_id} ورفضه من المدير.")
        await notify_user(context, trade["seller_id"], f"⚖️ تم حسم النزاع #{dispute_id} ورفضه من المدير.")
        return

    winner_id = trade["buyer_id"] if decision == "BUYER" else trade["seller_id"]
    winner_label = "المشتري" if decision == "BUYER" else "البائع"
    address = ""
    if decision == "BUYER":
        address = (trade["buyer_bep20_address"] or "").strip()
    else:
        user_row = conn.execute(
            "SELECT bep20_address FROM users WHERE telegram_id=?",
            (trade["seller_id"],),
        ).fetchone()
        address = (user_row["bep20_address"] or "").strip() if user_row else ""

    if not address:
        conn.close()
        await query.message.reply_text(
            f"❌ لا يوجد عنوان BEP20 محفوظ لـ{winner_label}.\n"
            "يجب حفظ العنوان أولًا ثم إعادة محاولة حسم النزاع."
        )
        return

    conn.execute(
        "UPDATE disputes SET status='PENDING_SETTLEMENT', admin_decision=? WHERE id=? AND status='OPEN'",
        (f"WIN_{decision}", dispute_id),
    )
    db_commit(conn)
    conn.close()

    context.user_data["dispute_settlement_id"] = dispute_id
    context.user_data["dispute_settlement_decision"] = decision

    await query.message.reply_text(
        f"⚖️ تم اختيار الحكم لصالح {winner_label}.\n\n"
        f"💰 المبلغ: {clean_decimal(trade['buyer_receives'])} USDT\n"
        f"📬 عنوان المستلم BEP20:\n`{address}`\n\n"
        f"🌐 الشبكة: {NETWORK}\n\n"
        "قم بالتحويل يدويًا من محفظة الوسيط إلى العنوان أعلاه، ثم أرسل TXID هنا."
    )


async def dispute_settlement_txid(update, context):
    dispute_id = context.user_data.get("dispute_settlement_id")
    decision = context.user_data.get("dispute_settlement_decision")
    if not dispute_id or decision not in ("BUYER", "SELLER"):
        return False

    if not is_admin(update.effective_user.id):
        return False

    txid = (update.message.text or "").strip()
    if len(txid) < 10:
        await update.message.reply_text("❌ TXID غير صحيح. أرسل TXID صحيحًا.")
        return True

    conn = db()
    dispute = conn.execute(
        "SELECT * FROM disputes WHERE id=? AND status='PENDING_SETTLEMENT'",
        (dispute_id,),
    ).fetchone()
    if not dispute:
        conn.close()
        context.user_data.pop("dispute_settlement_id", None)
        context.user_data.pop("dispute_settlement_decision", None)
        await update.message.reply_text("⚠️ هذا النزاع لم يعد بانتظار TXID.")
        return True

    trade = conn.execute("SELECT * FROM trades WHERE id=?", (dispute["trade_id"],)).fetchone()
    if not trade or trade["state"] != "DISPUTED":
        conn.close()
        return True

    if decision == "BUYER":
        conn.execute(
            "UPDATE trades SET release_txid=?, state='COMPLETED', updated_at=? WHERE id=? AND state='DISPUTED' AND release_txid IS NULL",
            (txid, now(), trade["id"]),
        )
        decision_text = "لصالح المشتري"
    else:
        conn.execute(
            "UPDATE trades SET refund_txid=?, state='COMPLETED', updated_at=? WHERE id=? AND state='DISPUTED' AND refund_txid IS NULL",
            (txid, now(), trade["id"]),
        )
        decision_text = "لصالح البائع"

    changed = conn.execute("SELECT changes()").fetchone()[0]
    if changed != 1:
        conn.rollback()
        conn.close()
        await update.message.reply_text("⚠️ لم يتم تسجيل التسوية، ربما تمت معالجتها مسبقًا.")
        return True

    conn.execute(
        "UPDATE disputes SET status='RESOLVED', resolved_at=? WHERE id=? AND status='PENDING_SETTLEMENT'",
        (now(), dispute_id),
    )
    # A dispute settlement also completes the trade, so consume the ad amount.
    consume_ad_amount(conn, trade["ad_id"], trade["requested_amount"])
    db_commit(conn)
    conn.close()

    audit("DISPUTE_RESOLVED", user_id=update.effective_user.id, trade_id=trade["id"], details=f"dispute_id={dispute_id}; decision={decision_text}; txid={txid}")
    await update.message.reply_text(f"✅ تم حسم النزاع {decision_text} وتسجيل TXID وإغلاق الصفقة.")
    await notify_user(context, trade["buyer_id"], f"⚖️ تم حسم النزاع #{dispute_id}.\nالقرار: {decision_text}\nTXID: `{txid}`")
    await notify_user(context, trade["seller_id"], f"⚖️ تم حسم النزاع #{dispute_id}.\nالقرار: {decision_text}\nTXID: `{txid}`")

    context.user_data.pop("dispute_settlement_id", None)
    context.user_data.pop("dispute_settlement_decision", None)
    return True


def consume_ad_amount(conn, ad_id, amount):
    """Deduct the completed trade amount from its advertisement once."""
    if not ad_id:
        return False

    ad = conn.execute(
        "SELECT id, amount, status FROM advertisements WHERE id=?",
        (ad_id,),
    ).fetchone()

    if not ad:
        return False

    remaining = D(ad["amount"]) - D(amount)
    if remaining < 0:
        remaining = D("0")

    if remaining <= 0:
        conn.execute(
            "UPDATE advertisements SET amount=?, status='CANCELLED', updated_at=? WHERE id=?",
            (str(D("0")), now(), ad_id),
        )
    else:
        conn.execute(
            "UPDATE advertisements SET amount=?, updated_at=? WHERE id=?",
            (str(remaining), now(), ad_id),
        )

    return True


# ============================================================
# MY TRADES
# ============================================================

async def my_trades(update, context):

    user_id = update.effective_user.id

    if not await require_verification(update):

        return

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM trades
        WHERE buyer_id=?
           OR seller_id=?
        ORDER BY id DESC
        LIMIT 20
        """,
        (
            user_id,
            user_id,
        ),
    ).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "📋 لا توجد صفقات."
        )

        return

    for trade in rows:

        text = (
            f"🔐 {trade['trade_code']}\n"
            f"الحالة: {trade['state']}\n"
            f"USDT: {clean_decimal(trade['requested_amount'])}\n"
            f"المبلغ المحلي: {clean_decimal(trade['fiat_amount'])} "
            f"{trade['currency']}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "التفاصيل",
                        callback_data=f"trade_view:{trade['id']}",
                    )
                ]
            ]
        )

        await update.message.reply_text(
            text,
            reply_markup=keyboard,
        )


# ============================================================
# KYC STATUS
# ============================================================

async def kyc_status(update, context):

    user_id = update.effective_user.id

    if is_admin(user_id):

        await update.message.reply_text(
            "👑 أنت المدير."
        )

        return

    row = get_user(user_id)

    if not row:

        return

    await update.message.reply_text(
        "🪪 حالة التوثيق\n\n"
        f"البريد: "
        f"{'✅' if row['email_verified'] else '❌'}\n"
        f"KYC: {row['kyc_status']}\n"
        f"Sham Cash: {row['sham_status']}"
    )


# ============================================================
# INSTRUCTIONS
# ============================================================

async def instructions(update, context):

    text = (
        "ℹ️ تعليمات المنصة\n\n"
        "1️⃣ البائع يرسل USDT إلى الوسيط.\n"
        "2️⃣ المشتري يدفع المبلغ المحلي.\n"
        "3️⃣ المشتري يرفع إثبات الدفع.\n"
        "4️⃣ البائع يتحقق من وصول المبلغ.\n"
        "5️⃣ بعد التأكيد يقوم المدير بتحرير USDT.\n\n"
        f"🌐 شبكة USDT: {NETWORK}\n\n"
        "⚠️ لا ترسل USDT مباشرة إلى الطرف الآخر.\n"
        "⚠️ لا تعتمد على لقطة شاشة فقط لتأكيد استلام الأموال.\n"
        "⚠️ لا تتواصل خارج المنصة أثناء الصفقة."
    )

    await update.message.reply_text(
        text
    )


# ============================================================
# SHAM CASH
# ============================================================

async def sham_cash(update, context):

    user_id = update.effective_user.id

    if not is_verified(user_id):

        await update.message.reply_text(
            "🔐 أكمل التوثيق أولاً."
        )

        return

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM kyc_requests
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    if not row:

        await update.message.reply_text(
            "❌ لا توجد بيانات Sham Cash."
        )

        return

    await update.message.reply_text(
        "💳 بيانات Sham Cash\n\n"
        f"الاسم: {row['sham_name']}\n"
        f"العنوان/الكود: {row['sham_code']}\n"
        f"الحالة: {row['status']}"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):

    if not is_admin(update.effective_user.id):

        await update.message.reply_text(
            "🚫 غير مصرح."
        )

        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 الإحصائيات",
                    callback_data="admin_stats",
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 عنوان الوسيط",
                    callback_data="admin_wallet",
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ إعدادات السوق",
                    callback_data="admin_market_settings",
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ العمولة",
                    callback_data="admin_fee",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 إعادة فحص إيداع USDT",
                    callback_data="admin_recheck",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑️ حذف بيانات التشغيل (مع إبقاء التوثيقات)",
                    callback_data="admin_delete_db",
                )
            ],
            [
                InlineKeyboardButton(
                    "💣 حذف قاعدة البيانات بالكامل",
                    callback_data="admin_delete_db_full",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ مدة الصفقة",
                    callback_data="admin_timeout",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔐 الصفقات المفتوحة",
                    callback_data="admin_open_trades",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 إدارة المعاملات",
                    callback_data="admin_trade_queue",
                )
            ],
            [
                InlineKeyboardButton(
                    "🪪 طلبات KYC",
                    callback_data="admin_kyc",
                )
            ],
            [
                InlineKeyboardButton("📢 إدارة الإعلانات", callback_data="admin_ads"),
                InlineKeyboardButton("⚖️ النزاعات المفتوحة", callback_data="admin_disputes"),
            ],
            [
                InlineKeyboardButton("🚫 إدارة الحظر", callback_data="admin_blocked"),
                InlineKeyboardButton("🪪 إدارة التوثيق", callback_data="admin_verified"),
            ],
        ]
    )

    await update.message.reply_text(
        "⚙️ لوحة المدير",
        reply_markup=keyboard,
    )



# ============================================================
# ADMIN TRANSACTION QUEUE
# ============================================================

QUEUE_PAGE_SIZE = 8
QUEUE_ACTION_STATES = (
    "DISPUTED",
    "DEPOSIT_PENDING_CONFIRMATION",
    "PAYMENT_CONFIRMED",
)
QUEUE_FILTER_LABELS = {
    "needs": "🟠 تحتاج إجراء المدير",
    "disputed": "⚖️ النزاعات",
    "completed": "✅ المكتملة",
    "cancelled": "🚫 الملغاة/المنتهية",
    "all": "📋 كل المعاملات",
}


def _queue_state_label(state):
    return {
        "WAITING_DEPOSIT": "⏳ بانتظار إيداع USDT",
        "DEPOSIT_PENDING_CONFIRMATION": "💰 بانتظار موافقة الإيداع",
        "WAITING_LOCAL_PAYMENT": "💳 بانتظار الدفع المحلي",
        "PAYMENT_PROOF_SENT": "🧾 بانتظار تأكيد البائع",
        "PAYMENT_CONFIRMED": "💰 جاهزة لتحرير USDT",
        "DISPUTED": "⚖️ نزاع مفتوح",
        "COMPLETED": "✅ مكتملة",
        "CANCELLED": "🚫 ملغاة",
        "EXPIRED": "⌛ منتهية",
    }.get(state, state or "-")


def _queue_where(filter_name):
    if filter_name == "needs":
        return "state IN ('DISPUTED','DEPOSIT_PENDING_CONFIRMATION','PAYMENT_CONFIRMED')", []
    if filter_name == "disputed":
        return "state='DISPUTED'", []
    if filter_name == "completed":
        return "state='COMPLETED'", []
    if filter_name == "cancelled":
        return "state IN ('CANCELLED','EXPIRED')", []
    return "1=1", []


def _queue_order(filter_name):
    if filter_name == "needs":
        return "CASE state WHEN 'DISPUTED' THEN 1 WHEN 'PAYMENT_CONFIRMED' THEN 2 WHEN 'DEPOSIT_PENDING_CONFIRMATION' THEN 3 ELSE 9 END, created_at ASC, id ASC"
    if filter_name in ("completed", "cancelled"):
        return "updated_at DESC, id DESC"
    return "created_at ASC, id ASC"


async def admin_trade_queue_message(query, filter_name="needs", page=0):
    if filter_name not in QUEUE_FILTER_LABELS:
        filter_name = "needs"
    page = max(0, int(page))

    conn = db()
    where, params = _queue_where(filter_name)
    total = conn.execute(f"SELECT COUNT(*) c FROM trades WHERE {where}", params).fetchone()["c"]
    offset = page * QUEUE_PAGE_SIZE
    rows = conn.execute(
        f"SELECT * FROM trades WHERE {where} ORDER BY {_queue_order(filter_name)} LIMIT ? OFFSET ?",
        params + [QUEUE_PAGE_SIZE, offset],
    ).fetchall()

    counts = {}
    for key, clause in [
        ("needs", "state IN ('DISPUTED','DEPOSIT_PENDING_CONFIRMATION','PAYMENT_CONFIRMED')"),
        ("disputed", "state='DISPUTED'"),
        ("completed", "state='COMPLETED'"),
        ("cancelled", "state IN ('CANCELLED','EXPIRED')"),
    ]:
        counts[key] = conn.execute(f"SELECT COUNT(*) c FROM trades WHERE {clause}").fetchone()["c"]
    conn.close()

    text = (
        "📋 <b>إدارة المعاملات</b>\n\n"
        f"🟠 تحتاج إجراء: <b>{counts['needs']}</b>   "
        f"⚖️ نزاعات: <b>{counts['disputed']}</b>\n"
        f"✅ مكتملة: <b>{counts['completed']}</b>   "
        f"🚫 ملغاة/منتهية: <b>{counts['cancelled']}</b>\n\n"
        f"<b>{QUEUE_FILTER_LABELS[filter_name]}</b>\n"
    )

    if not rows:
        text += "\nلا توجد معاملات في هذا القسم."
    else:
        for row in rows:
            created = (row["created_at"] or "-").replace("T", " ")[:19]
            text += (
                "\n━━━━━━━━━━━━━━\n"
                f"🔹 <b>#{row['id']}</b> — <code>{row['trade_code']}</code>\n"
                f"💵 {clean_decimal(row['requested_amount'])} USDT\n"
                f"📌 {_queue_state_label(row['state'])}\n"
                f"🕒 {created}"
            )

    total_pages = max(1, (total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    text += f"\n\nصفحة <b>{page + 1}</b> من <b>{total_pages}</b>"

    keyboard = [
        [
            InlineKeyboardButton("🟠 تحتاج إجراء", callback_data="admin_queue:needs:0"),
            InlineKeyboardButton("⚖️ النزاعات", callback_data="admin_queue:disputed:0"),
        ],
        [
            InlineKeyboardButton("✅ مكتملة", callback_data="admin_queue:completed:0"),
            InlineKeyboardButton("🚫 ملغاة", callback_data="admin_queue:cancelled:0"),
        ],
        [InlineKeyboardButton("📋 الكل", callback_data="admin_queue:all:0")],
    ]

    if filter_name == "needs" and counts["needs"]:
        keyboard.append([InlineKeyboardButton("⏭️ المعاملة التالية", callback_data="admin_queue_next")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"admin_queue:{filter_name}:{page - 1}"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"admin_queue:{filter_name}:{page + 1}"))
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🔍 بحث عن معاملة", callback_data="admin_queue_search")])

    await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_queue_next_callback(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return
    await query.answer()
    conn = db()
    row = conn.execute(
        """SELECT * FROM trades
           WHERE state IN ('DISPUTED','PAYMENT_CONFIRMED','DEPOSIT_PENDING_CONFIRMATION')
           ORDER BY CASE state WHEN 'DISPUTED' THEN 1 WHEN 'PAYMENT_CONFIRMED' THEN 2 WHEN 'DEPOSIT_PENDING_CONFIRMATION' THEN 3 ELSE 9 END, created_at ASC, id ASC
           LIMIT 1"""
    ).fetchone()
    conn.close()
    if not row:
        await query.message.reply_text("✅ لا توجد حالياً معاملة تحتاج إجراء من المدير.")
        return
    await admin_trade_callback(update, context) if False else None
    # Reuse the existing trade-detail callback by updating callback data temporarily.
    original = query.data
    query.data = f"admin_trade:{row['id']}"
    try:
        await admin_trade_callback(update, context)
    finally:
        query.data = original


async def admin_queue_search_start(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.message.reply_text(
        "🔍 <b>البحث عن معاملة</b>\n\n"
        "أرسل رقم المعاملة أو كودها أو TXID أو Telegram ID أو username.\n"
        "مثال: <code>1042</code> أو <code>0x...</code> أو <code>@username</code>",
        parse_mode=ParseMode.HTML,
    )
    return ADMIN_QUEUE_SEARCH


async def admin_queue_search(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    term = (update.message.text or "").strip()
    if not term:
        await update.message.reply_text("❌ أرسل قيمة للبحث.")
        return ADMIN_QUEUE_SEARCH

    conn = db()
    like = f"%{term}%"
    numeric = None
    try:
        numeric = int(term)
    except ValueError:
        pass

    if numeric is not None:
        rows = conn.execute(
            """SELECT * FROM trades WHERE id=? OR buyer_id=? OR seller_id=?
               ORDER BY updated_at DESC, id DESC LIMIT 20""",
            (numeric, numeric, numeric),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT t.* FROM trades t
               LEFT JOIN users ub ON ub.telegram_id=t.buyer_id
               LEFT JOIN users us ON us.telegram_id=t.seller_id
               LEFT JOIN kyc_requests kb ON kb.id=(SELECT MAX(id) FROM kyc_requests WHERE user_id=t.buyer_id AND status='APPROVED')
               LEFT JOIN kyc_requests ks ON ks.id=(SELECT MAX(id) FROM kyc_requests WHERE user_id=t.seller_id AND status='APPROVED')
               WHERE t.trade_code LIKE ?
                  OR t.seller_deposit_txid LIKE ?
                  OR t.release_txid LIKE ?
                  OR ub.username LIKE ?
                  OR us.username LIKE ?
                  OR kb.full_name LIKE ?
                  OR ks.full_name LIKE ?
               ORDER BY t.updated_at DESC, t.id DESC LIMIT 20""",
            (like, like, like, like, like, like, like),
        ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("❌ لم أجد أي معاملة مطابقة.")
        return ConversationHandler.END

    text = f"🔎 نتائج البحث عن: <code>{term}</code>\n\n"
    keyboard = []
    for row in rows:
        text += (
            f"#{row['id']} — {row['trade_code']} — "
            f"{clean_decimal(row['requested_amount'])} USDT — "
            f"{_queue_state_label(row['state'])}\n"
        )
        keyboard.append([InlineKeyboardButton(
            f"📂 فتح #{row['id']}", callback_data=f"admin_trade:{row['id']}"
        )])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


def build_admin_queue_search_conversation():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_queue_search_start, pattern=r"^admin_queue_search$")],
        states={ADMIN_QUEUE_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_queue_search)]},
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )


# ============================================================
# ADMIN CALLBACKS
# ============================================================

async def admin_callback(update, context):

    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):

        return

    action = query.data

    if action == "admin_trade_queue":
        await admin_trade_queue_message(query, "needs", 0)
        return

    if action.startswith("admin_queue:"):
        parts = action.split(":")
        if len(parts) == 3:
            try:
                await admin_trade_queue_message(query, parts[1], int(parts[2]))
            except (ValueError, IndexError):
                await query.message.reply_text("❌ بيانات قائمة المعاملات غير صالحة.")
        return

    if action == "admin_queue_next":
        await admin_queue_next_callback(update, context)
        return

    if action == "admin_market_settings":
        await admin_market_settings_callback(update, context)
        return

    if action == "admin_market_toggle:market_open":
        current = market_bool("market_open")
        new_value = "0" if current else "1"
        set_setting("market_open", new_value)
        audit("MARKET_SETTING_CHANGED", admin_id=ADMIN_ID, details=f"market_open={new_value}")
        status_text = "🔴 تم إيقاف التداول بنجاح." if new_value == "0" else "🟢 تم فتح التداول بنجاح."
        await query.message.reply_text(status_text + "\n\n" + market_settings_text(), parse_mode=ParseMode.HTML, reply_markup=market_settings_keyboard())
        return

    if action == "admin_market_toggle:ads_open":
        current = market_bool("ads_open")
        new_value = "0" if current else "1"
        set_setting("ads_open", new_value)
        audit("MARKET_SETTING_CHANGED", admin_id=ADMIN_ID, details=f"ads_open={new_value}")
        status_text = "🔴 تم إيقاف إنشاء الإعلانات بنجاح." if new_value == "0" else "🟢 تم فتح إنشاء الإعلانات بنجاح."
        await query.message.reply_text(status_text + "\n\n" + market_settings_text(), parse_mode=ParseMode.HTML, reply_markup=market_settings_keyboard())
        return

    if action == "admin_stats":

        conn = db()

        users = conn.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        verified = conn.execute(
            """
            SELECT COUNT(*) c
            FROM users
            WHERE kyc_status='APPROVED'
            """
        ).fetchone()["c"]

        ads_count = conn.execute(
            """
            SELECT COUNT(*) c
            FROM advertisements
            WHERE status='ACTIVE'
            """
        ).fetchone()["c"]

        trades = conn.execute(
            "SELECT COUNT(*) c FROM trades"
        ).fetchone()["c"]

        open_trades = conn.execute(
            """
            SELECT COUNT(*) c
            FROM trades
            WHERE state NOT IN(
                'COMPLETED',
                'CANCELLED',
                'EXPIRED'
            )
            """
        ).fetchone()["c"]

        conn.close()

        await query.message.reply_text(
            "📊 الإحصائيات\n\n"
            f"المستخدمون: {users}\n"
            f"الموثقون: {verified}\n"
            f"الإعلانات النشطة: {ads_count}\n"
            f"إجمالي الصفقات: {trades}\n"
            f"الصفقات المفتوحة: {open_trades}"
        )

        return

    if action == "admin_wallet":

        address = get_setting(
            "mediator_address",
            "",
        )

        await query.message.reply_text(
            "💰 عنوان الوسيط الحالي:\n\n"
            f"{address or 'غير مضبوط'}\n\n"
            "لتغييره استخدم أمر المدير:\n"
            "/setwallet العنوان"
        )

        return

    if action == "admin_fee":

        fee = get_setting(
            "commission_rate",
            "1",
        )

        await query.message.reply_text(
            f"⚙️ العمولة الحالية: {fee}%\n\n"
            "لتغييرها:\n"
            "/setfee 1"
        )

        return

    if action == "admin_timeout":

        timeout = get_setting(
            "trade_timeout_minutes",
            "60",
        )

        await query.message.reply_text(
            f"⏱ مدة الصفقة الحالية: {timeout} دقيقة\n\n"
            "لتغييرها:\n"
            "/settimeout 60"
        )

        return

    if action == "admin_open_trades":

        conn = db()

        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE state NOT IN(
                'COMPLETED',
                'CANCELLED',
                'EXPIRED'
            )
            ORDER BY id DESC
            LIMIT 30
            """
        ).fetchall()

        conn.close()

        if not rows:

            await query.message.reply_text(
                "لا توجد صفقات مفتوحة."
            )

            return

        for trade in rows:

            await query.message.reply_text(
                f"🔐 {trade['trade_code']}\n"
                f"الحالة: {trade['state']}\n"
                f"USDT: {clean_decimal(trade['requested_amount'])}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "فتح",
                                callback_data=f"admin_trade:{trade['id']}",
                            )
                        ]
                    ]
                ),
            )

        return

    if action == "admin_blocked":
        conn = db()
        rows = conn.execute(
            "SELECT * FROM users ORDER BY blocked DESC, created_at DESC"
        ).fetchall()
        conn.close()
        if not rows:
            await query.message.reply_text("🚫 لا يوجد مستخدمون مسجلون.")
            return
        await query.message.reply_text("🚫 إدارة الحظر\n\nالمستخدمون المحظورون يظهرون أولاً. اختر المستخدم المطلوب، ثم اضغط 🔓 رفع الحظر لإلغاء الحظر:")
        for row in rows:
            name = " ".join(x for x in [row["first_name"], row["last_name"]] if x).strip() or "بدون اسم تلغرام"
            username = f"@{row['username']}" if row["username"] else "بدون معرف"
            status = "🚫 محظور" if row["blocked"] else "✅ غير محظور"
            await query.message.reply_text(
                f"👤 {name}\n{username}\n🆔 {row['telegram_id']}\n{status}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔓 رفع الحظر" if row["blocked"] else "🚫 حظر المستخدم",
                        callback_data=f"admin_user_block:{row['telegram_id']}:{'unblock' if row['blocked'] else 'block'}"
                    )],
                    [InlineKeyboardButton("📋 تفاصيل المستخدم", callback_data=f"admin_user_detail:{row['telegram_id']}")]
                ]),
            )
        return

    if action == "admin_verified":
        conn = db()
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        if not rows:
            await query.message.reply_text("🪪 لا يوجد مستخدمون مسجلون.")
            return
        await query.message.reply_text("🪪 إدارة التوثيق\n\nجميع المستخدمين المسجلين وحالة التوثيق:")
        for row in rows:
            name = " ".join(x for x in [row["first_name"], row["last_name"]] if x).strip() or "بدون اسم تلغرام"
            username = f"@{row['username']}" if row["username"] else "بدون معرف"
            status = row["kyc_status"] or "PENDING"
            await query.message.reply_text(
                f"👤 {name}\n{username}\n🆔 {row['telegram_id']}\n🪪 KYC: {status}\n💳 Sham Cash: {row['sham_status']}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 تفاصيل التوثيق", callback_data=f"admin_user_detail:{row['telegram_id']}")]
                ]),
            )
        return

    if action == "admin_ads":
        await admin_ads(update, context)
        return

    if action == "admin_disputes":
        conn=db()
        rows=conn.execute("SELECT d.*, t.trade_code, t.buyer_id, t.seller_id FROM disputes d JOIN trades t ON t.id=d.trade_id WHERE d.status='OPEN' ORDER BY d.id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            await query.message.reply_text("⚖️ لا توجد نزاعات مفتوحة.")
            return
        for d in rows:
            await query.message.reply_text(
                f"⚖️ نزاع #{d['id']}\nالصفقة: {d['trade_code']}\nصاحب النزاع: {d['user_id']}\nالطرف الآخر: {d['seller_id'] if d['user_id']==d['buyer_id'] else d['buyer_id']}\n\nالسبب:\n{d['reason']}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 صاحب النزاع", callback_data=f"dispute_admin_chat:{d['id']}:opener")],
                    [InlineKeyboardButton("💬 الطرف الآخر", callback_data=f"dispute_admin_chat:{d['id']}:other")],
                    [InlineKeyboardButton("📋 الصفقة", callback_data=f"admin_trade:{d['trade_id']}")],
                    [InlineKeyboardButton("⚖️ لصالح المشتري", callback_data=f"dispute_resolve:{d['id']}:buyer")],
                    [InlineKeyboardButton("⚖️ لصالح البائع", callback_data=f"dispute_resolve:{d['id']}:seller")],
                    [InlineKeyboardButton("❌ رفض النزاع", callback_data=f"dispute_resolve:{d['id']}:reject")],
                ]),
            )
        return

    if action == "admin_kyc":

        conn = db()

        rows = conn.execute(
            """
            SELECT *
            FROM kyc_requests
            WHERE status='PENDING'
            ORDER BY id ASC
            LIMIT 20
            """
        ).fetchall()

        conn.close()

        if not rows:

            await query.message.reply_text(
                "لا توجد طلبات KYC معلقة."
            )

            return

        for row in rows:

            await query.message.reply_text(
                f"🪪 طلب KYC\n\n"
                f"المستخدم: {row['user_id']}\n"
                f"الاسم: {row['full_name']}\n"
                f"الهاتف: {row['phone']}\n"
                f"الدولة: {row['country']}\n"
                f"Sham Cash: {row['sham_name']}\n"
                f"الكود: {row['sham_code']}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ قبول",
                                callback_data=f"kyc_approve:{row['id']}",
                            ),
                            InlineKeyboardButton(
                                "❌ رفض",
                                callback_data=f"kyc_reject:{row['id']}",
                            )
                        ]
                    ]
                ),
            )

        return


# ============================================================
# ADMIN USER MANAGEMENT
# ============================================================

async def admin_user_block_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        return
    action = parts[2]
    if user_id == ADMIN_ID and action == "block":
        await query.message.reply_text("❌ لا يمكن حظر حساب المدير الرئيسي.")
        return
    conn = db()
    updated = conn.execute(
        "UPDATE users SET blocked=?, updated_at=? WHERE telegram_id=?",
        (1 if action == "block" else 0, now(), user_id),
    ).rowcount
    db_commit(conn)
    conn.close()
    if not updated:
        await query.message.reply_text("❌ المستخدم غير موجود.")
        return
    audit("USER_BLOCKED" if action == "block" else "USER_UNBLOCKED", admin_id=ADMIN_ID, user_id=user_id)
    await query.message.reply_text("🚫 تم حظر المستخدم." if action == "block" else "✅ تم إلغاء الحظر.")


async def admin_user_detail_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    try:
        user_id = int((query.data or "").split(":")[1])
    except (ValueError, IndexError):
        return
    conn = db()
    user, kyc = get_user_identity(conn, user_id)
    conn.close()
    if not user:
        await query.message.reply_text("❌ المستخدم غير موجود.")
        return
    tg_name = " ".join(x for x in [user["first_name"], user["last_name"]] if x).strip() or "-"
    username = f"@{user['username']}" if user["username"] else "-"
    real_name = kyc["full_name"] if kyc and kyc["full_name"] else "غير موثق بالاسم الحقيقي"
    text = (
        "👤 تفاصيل المستخدم\n\n"
        f"الاسم الحقيقي: {real_name}\n"
        f"اسم تلغرام: {tg_name}\n"
        f"معرف تلغرام: {username}\n"
        f"Telegram ID: {user['telegram_id']}\n"
        f"KYC: {user['kyc_status']}\n"
        f"Sham Cash: {user['sham_status']}\n"
        f"الحظر: {'🚫 محظور' if user['blocked'] else '✅ غير محظور'}\n"
        f"الهاتف: {(kyc['phone'] if kyc else None) or user['phone'] or '-'}\n"
        f"الدولة: {(kyc['country'] if kyc else None) or user['country'] or '-'}\n"
        f"Sham Cash: {(kyc['sham_name'] if kyc else None) or '-'} / {(kyc['sham_code'] if kyc else None) or '-'}"
    )
    await query.message.reply_text(text)


# ============================================================
# ADMIN TRADE
# ============================================================

async def admin_trade_callback(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):

        return

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    if not trade:
        conn.close()
        await query.message.reply_text(
            "❌ غير موجود."
        )
        return

    buyer, buyer_kyc = get_user_identity(conn, trade["buyer_id"])
    seller, seller_kyc = get_user_identity(conn, trade["seller_id"])
    buyer_real_name = buyer_kyc["full_name"] if buyer_kyc and buyer_kyc["full_name"] else "غير موثق"
    seller_real_name = seller_kyc["full_name"] if seller_kyc and seller_kyc["full_name"] else "غير موثق"
    buyer_username = f"@{buyer['username']}" if buyer and buyer["username"] else "-"
    seller_username = f"@{seller['username']}" if seller and seller["username"] else "-"

    text = (
        f"🔐 {trade['trade_code']}\n\n"
        f"الحالة: {trade['state']}\n\n"
        f"👤 المشتري\n"
        f"الاسم الحقيقي: {buyer_real_name}\n"
        f"معرف تلغرام: {buyer_username}\n"
        f"Telegram ID: {trade['buyer_id']}\n\n"
        f"👤 البائع\n"
        f"الاسم الحقيقي: {seller_real_name}\n"
        f"معرف تلغرام: {seller_username}\n"
        f"Telegram ID: {trade['seller_id']}\n"
        f"📬 عنوان المشتري BEP20: {trade['buyer_bep20_address'] or '-'}\n"
        f"الكمية: {clean_decimal(trade['requested_amount'])} USDT\n"
        f"للمشتري: {clean_decimal(trade['buyer_receives'])} USDT\n"
        f"للبائع للإرسال: {clean_decimal(trade['seller_sends'])} USDT\n"
        f"المحلي: {clean_decimal(trade['fiat_amount'])} {trade['currency']}\n"
        f"إيداع TXID: {trade['seller_deposit_txid'] or '-'}\n"
        f"تحرير TXID: {trade['release_txid'] or '-'}"
    )
    conn.close()

    keyboard = []

    copy_buyer_address = copy_value_button("📋 نسخ عنوان المشتري BEP20", trade["buyer_bep20_address"] or "")
    if copy_buyer_address:
        keyboard.append([copy_buyer_address])

    if trade["state"] == "DEPOSIT_PENDING_CONFIRMATION":

        keyboard.append(
            [
                InlineKeyboardButton(
                    "✅ تأكيد الإيداع",
                    callback_data=f"deposit_confirm:{trade_id}",
                )
            ]
        )

    if trade["state"] == "PAYMENT_CONFIRMED":

        keyboard.append(
            [
                InlineKeyboardButton(
                    "💰 تحرير USDT",
                    callback_data=f"release_start:{trade_id}",
                )
            ]
        )

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        )
        if keyboard
        else None,
    )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def set_address(update, context):
    """Save the buyer's own BEP20 receiving address."""
    user = update.effective_user
    ensure_user(user)

    if not context.args:
        await update.message.reply_text(
            "الاستخدام:\n/setaddress BEP20_ADDRESS\n\n"
            "مثال: /setaddress 0x..."
        )
        return

    address = context.args[0].strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", address):
        await update.message.reply_text(
            "❌ عنوان BEP20 غير صحيح.\n"
            "يجب أن يكون عنواناً يبدأ بـ 0x ويتكون من 40 خانة سداسية عشرية."
        )
        return

    conn = db()
    conn.execute(
        "UPDATE users SET bep20_address=?, updated_at=? WHERE telegram_id=?",
        (address, now(), user.id),
    )
    db_commit(conn)
    conn.close()

    audit(
        "BEP20_ADDRESS_UPDATED",
        user_id=user.id,
        details=address,
    )

    await update.message.reply_text(
        "✅ تم حفظ عنوان BEP20 الخاص بك.\n\n"
        f"📬 العنوان:\n`{address}`\n\n"
        "سيتم استخدامه كعنوان استلام للمشتريات الجديدة."
    )


async def set_wallet(update, context):

    if not is_admin(update.effective_user.id):

        return

    if not context.args:

        await update.message.reply_text(
            "الاستخدام:\n/setwallet BEP20_ADDRESS"
        )

        return

    address = context.args[0].strip()

    # Basic BEP20 address validation.
    if not re.match(
        r"^0x[a-fA-F0-9]{40}$",
        address,
    ):

        await update.message.reply_text(
            "❌ عنوان BEP20 غير صحيح."
        )

        return

    set_setting(
        "mediator_address",
        address,
    )

    audit(
        "MEDIATOR_ADDRESS_CHANGED",
        admin_id=ADMIN_ID,
        details=address,
    )

    await update.message.reply_text(
        "✅ تم تحديث عنوان الوسيط."
    )


async def set_fee(update, context):

    if not is_admin(update.effective_user.id):

        return

    if not context.args:

        await update.message.reply_text(
            "الاستخدام:\n/setfee 1"
        )

        return

    fee = D(context.args[0])

    if fee < 0 or fee > 100:

        await update.message.reply_text(
            "❌ نسبة غير صحيحة."
        )

        return

    set_setting(
        "commission_rate",
        clean_decimal(fee),
    )

    audit(
        "COMMISSION_CHANGED",
        admin_id=ADMIN_ID,
        details=str(fee),
    )

    await update.message.reply_text(
        f"✅ العمولة أصبحت {clean_decimal(fee)}%."
    )


async def set_timeout(update, context):

    if not is_admin(update.effective_user.id):

        return

    if not context.args:

        await update.message.reply_text(
            "الاستخدام:\n/settimeout 60"
        )

        return

    try:

        minutes = int(
            context.args[0]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ أدخل رقماً صحيحاً."
        )

        return

    if minutes < 5 or minutes > 1440:

        await update.message.reply_text(
            "❌ المدة يجب أن تكون بين 5 و1440 دقيقة."
        )

        return

    set_setting(
        "trade_timeout_minutes",
        str(minutes),
    )

    await update.message.reply_text(
        f"✅ مدة الصفقة أصبحت {minutes} دقيقة."
    )


# ============================================================
# ADMIN BLOCK USER
# ============================================================

async def block_user(update, context):

    if not is_admin(update.effective_user.id):

        return

    if not context.args:

        await update.message.reply_text(
            "/block USER_ID"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        return

    conn = db()

    conn.execute(
        """
        UPDATE users
        SET blocked=1,
            updated_at=?
        WHERE telegram_id=?
        """,
        (
            now(),
            user_id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "USER_BLOCKED",
        admin_id=ADMIN_ID,
        user_id=user_id,
    )

    await update.message.reply_text(
        "🚫 تم حظر المستخدم."
    )


async def unblock_user(update, context):

    if not is_admin(update.effective_user.id):

        return

    if not context.args:

        await update.message.reply_text(
            "/unblock USER_ID"
        )

        return

    try:

        user_id = int(
            context.args[0]
        )

    except ValueError:

        return

    conn = db()

    conn.execute(
        """
        UPDATE users
        SET blocked=0,
            updated_at=?
        WHERE telegram_id=?
        """,
        (
            now(),
            user_id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "USER_UNBLOCKED",
        admin_id=ADMIN_ID,
        user_id=user_id,
    )

    await update.message.reply_text(
        "✅ تم إلغاء الحظر."
    )


# ============================================================
# CANCEL TRADE
# ============================================================

async def cancel_trade_callback(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    trade_id = int(
        query.data.split(":")[1]
    )

    conn = db()

    trade = conn.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()

    if not trade:

        conn.close()

        return

    if query.from_user.id not in (
        trade["buyer_id"],
        trade["seller_id"],
        ADMIN_ID,
    ):

        conn.close()

        return

    if trade["state"] in (
        "COMPLETED",
        "CANCELLED",
    ):

        conn.close()

        await query.message.reply_text(
            "❌ الصفقة مغلقة مسبقاً."
        )

        return

    conn.execute(
        """
        UPDATE trades
        SET
            state='CANCELLED',
            updated_at=?
        WHERE id=?
        """,
        (
            now(),
            trade_id,
        ),
    )

    db_commit(conn)
    conn.close()

    audit(
        "TRADE_CANCELLED",
        user_id=query.from_user.id,
        trade_id=trade_id,
    )

    await query.message.reply_text(
        "❌ تم إلغاء الصفقة."
    )


# ============================================================
# EXPIRED TRADES
# ============================================================

async def expire_old_trades(context):
    expire_old_ads()
    conn = db()
    expired_trades = []

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM trades
            WHERE state NOT IN(
                'COMPLETED',
                'CANCELLED',
                'EXPIRED'
            )
            """
        ).fetchall()

        for trade in rows:
            expires = parse_time(trade["expires_at"])

            if datetime.now(timezone.utc) > expires:
                changed = conn.execute(
                    """
                    UPDATE trades
                    SET state='EXPIRED', updated_at=?
                    WHERE id=?
                      AND state NOT IN(
                          'COMPLETED',
                          'CANCELLED',
                          'EXPIRED'
                      )
                    """,
                    (now(), trade["id"]),
                ).rowcount

                if changed:
                    expired_trades.append(trade)

        # IMPORTANT: release the SQLite write lock before calling audit() or
        # awaiting Telegram notifications. Those functions open their own DB
        # connections and must not compete with this still-open transaction.
        db_commit(conn)
    finally:
        conn.close()

    for trade in expired_trades:
        audit(
            "TRADE_EXPIRED",
            trade_id=trade["id"],
        )

        await notify_user(
            context,
            trade["buyer_id"],
            f"⏱ انتهت مهلة الصفقة {trade['trade_code']}."
        )

        await notify_user(
            context,
            trade["seller_id"],
            f"⏱ انتهت مهلة الصفقة {trade['trade_code']}."
        )


# ============================================================
# BACK BUTTON
# ============================================================

async def back_ads_callback(update, context):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "📢 الإعلانات:",
        reply_markup=main_keyboard(
            query.from_user.id
        ),
    )


# ============================================================
# UNKNOWN / SAFETY
# ============================================================

async def text_router(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):

        await update.message.reply_text(
            "🚫 حسابك محظور."
        )

        return

    text = (
        update.message.text or ""
    ).strip()

    if text == "🏠 الرئيسية":

        await home(
            update,
            context,
        )

        return

    if text == "📢 إعلاناتي":
        await my_ads(update, context)
        return

    if await dispute_settlement_txid(update, context):
        return

    if await dispute_admin_text(update, context):
        return

    if await dispute_user_text(update, context):
        return

    if await ad_edit_value(update, context):
        return

    if text == "📢 الإعلانات":

        await ads(
            update,
            context,
        )

        return

    if text == "➕ إنشاء إعلان":

        # Conversation handler normally handles this.
        return

    if text == "📋 صفقاتي":

        await my_trades(
            update,
            context,
        )

        return

    if text == "🪪 التوثيق KYC":

        await kyc_status(
            update,
            context,
        )

        return

    if text == "💳 Sham Cash":

        await sham_cash(
            update,
            context,
        )

        return

    if text == "ℹ️ التعليمات":

        await instructions(
            update,
            context,
        )

        return

    if text == "⚙️ لوحة المدير":

        await admin_panel(
            update,
            context,
        )

        return

    # If waiting for trade amount
    if "trade_ad_id" in context.user_data:

        await process_trade_amount(
            update,
            context,
        )

        return

    # If waiting for dispute reason
    if "dispute_trade_id" in context.user_data:

        await dispute_reason(
            update,
            context,
        )

        return

    await update.message.reply_text(
        "اختر عملية من القائمة.",
        reply_markup=main_keyboard(
            user.id
        ),
    )


# ============================================================
# CONVERSATION HANDLERS
# ============================================================

def build_kyc_conversation():
    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(r"^🪪 التوثيق KYC$"),
                kyc_start,
            )
        ],
        states={
            KYC_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, kyc_email)
            ],
            KYC_EMAIL_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, kyc_email_code)
            ],
            KYC_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, kyc_name)
            ],
            KYC_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, kyc_phone)
            ],
            KYC_ID_PHOTO: [
                MessageHandler(filters.PHOTO, kyc_id_photo)
            ],
            KYC_SELFIE: [
                MessageHandler(filters.PHOTO, kyc_selfie)
            ],
            KYC_BEP20: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, kyc_bep20)
            ],
            KYC_SHAM_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, kyc_sham_code)
            ],
        },
        fallbacks=[
            CommandHandler("start", start)
        ],
        allow_reentry=True,
    )

def build_otp_conversation():

    return ConversationHandler(
        entry_points=[
            CommandHandler(
                "verify_email",
                start_email_verification,
            )
        ],

        states={

            OTP_EMAIL: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    otp_email,
                )
            ],

            OTP_CODE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    otp_code,
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "start",
                start,
            )
        ],

        allow_reentry=True,
    )


def build_ad_conversation():

    return ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex(
                    r"^➕ إنشاء إعلان$"
                ),
                create_ad_start,
            )
        ],

        states={

            AD_TYPE: [
                CallbackQueryHandler(
                    ad_type_callback,
                    pattern=r"^ad_(buy|sell)$",
                )
            ],

            AD_CURRENCY: [
                CallbackQueryHandler(
                    ad_currency_callback,
                    pattern=r"^ad_currency:",
                )
            ],

            AD_PRICE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_price,
                )
            ],

            AD_AMOUNT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_amount,
                )
            ],

            AD_MIN: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_min,
                )
            ],

            AD_MAX: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_max,
                )
            ],

            AD_PAYMENT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_payment,
                )
            ],

            AD_ACCOUNT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_account,
                )
            ],

            AD_NOTE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    ad_note,
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "start",
                start,
            )
        ],

        allow_reentry=True,
    )


def build_deposit_conversation():

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                deposit_sent_callback,
                pattern=r"^deposit_sent:",
            )
        ],

        states={

            SELLER_DEPOSIT_TXID: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    deposit_txid,
                )
            ]
        },

        fallbacks=[
            CommandHandler(
                "start",
                start,
            )
        ],

        allow_reentry=True,
    )


def build_payment_conversation():

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                payment_sent_callback,
                pattern=r"^payment_sent:",
            )
        ],

        states={

            PAYMENT_PROOF: [
                MessageHandler(
                    filters.PHOTO,
                    payment_proof,
                )
            ]
        },

        fallbacks=[
            CommandHandler(
                "start",
                start,
            )
        ],

        allow_reentry=True,
    )


def market_settings_text():
    def v(key, default):
        return get_setting(key, default)
    return (
        "⚙️ <b>إعدادات السوق</b>\n\n"
        f"حالة التداول: {'🟢 مفتوح' if market_bool('market_open') else '🔴 متوقف'}\n"
        f"الإعلانات: {'🟢 مفتوحة' if market_bool('ads_open') else '🔴 متوقفة'}\n"
        f"الحد الأدنى للصفقة: <b>{v('min_trade_amount','10')}</b> USDT\n"
        f"شراء: <b>{v('buy_price_min','0')}</b> — <b>{v('buy_price_max','0')}</b>\n"
        f"بيع: <b>{v('sell_price_min','0')}</b> — <b>{v('sell_price_max','0')}</b>\n"
        f"الإعلانات النشطة لكل مستخدم: <b>{v('max_active_ads_per_user','1')}</b>\n"
        f"كمية الإعلان: <b>{v('min_ad_amount','10')}</b> — <b>{v('max_ad_amount','0') if v('max_ad_amount','0') != '0' else 'بدون حد'}</b> USDT\n"
        f"صلاحية الإعلان: <b>{v('ad_expiry_hours','24')}</b> ساعة\n\n"
        "اضغط على الإعداد الذي تريد تغييره."
    )


def market_settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 الحد الأدنى للصفقة", callback_data="admin_market_set:min_trade_amount")],
        [InlineKeyboardButton("🟢 نطاق سعر الشراء", callback_data="admin_market_set:buy_price")],
        [InlineKeyboardButton("🔴 نطاق سعر البيع", callback_data="admin_market_set:sell_price")],
        [InlineKeyboardButton("📢 عدد الإعلانات لكل مستخدم", callback_data="admin_market_set:max_active_ads_per_user")],
        [InlineKeyboardButton("📦 الحد الأدنى لكمية الإعلان", callback_data="admin_market_set:min_ad_amount")],
        [InlineKeyboardButton("📦 الحد الأقصى لكمية الإعلان", callback_data="admin_market_set:max_ad_amount")],
        [InlineKeyboardButton("⏱ صلاحية الإعلان", callback_data="admin_market_set:ad_expiry_hours")],
        [InlineKeyboardButton("🟢/🔴 حالة التداول", callback_data="admin_market_toggle:market_open")],
        [InlineKeyboardButton("📢 حالة الإعلانات", callback_data="admin_market_toggle:ads_open")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="admin_market_settings")],
    ])


async def admin_market_settings_callback(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return
    await query.answer()
    await query.message.reply_text(market_settings_text(), parse_mode=ParseMode.HTML, reply_markup=market_settings_keyboard())


async def admin_market_set_callback(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    key = query.data.split(":", 1)[1]
    context.user_data["market_setting_key"] = key
    prompts = {
        "min_trade_amount": "💰 أرسل الحد الأدنى للصفقة بالـ USDT. مثال: 10",
        "max_active_ads_per_user": "📢 أرسل عدد الإعلانات النشطة المسموحة لكل مستخدم. مثال: 1",
        "min_ad_amount": "📦 أرسل الحد الأدنى لكمية الإعلان بالـ USDT. مثال: 10",
        "max_ad_amount": "📦 أرسل الحد الأقصى لكمية الإعلان بالـ USDT. أرسل 0 إذا أردت بدون حد.",
        "ad_expiry_hours": "⏱ أرسل مدة صلاحية الإعلان بالساعات. مثال: 24",
        "buy_price": "🟢 أرسل نطاق سعر الشراء بهذا الشكل: الحد الأدنى - الحد الأعلى\nمثال: 11500 - 11800",
        "sell_price": "🔴 أرسل نطاق سعر البيع بهذا الشكل: الحد الأدنى - الحد الأعلى\nمثال: 11700 - 12000",
    }
    await query.message.reply_text(prompts.get(key, "أرسل القيمة الجديدة:"))
    return ADMIN_MARKET_VALUE


async def admin_market_value(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    key = context.user_data.get("market_setting_key")
    raw = (update.message.text or "").strip()
    try:
        if key in ("buy_price", "sell_price"):
            parts = re.split(r"\s*[-–—]\s*", raw)
            if len(parts) != 2:
                raise ValueError
            low, high = D(parts[0]), D(parts[1])
            if low <= 0 or high <= 0 or low > high:
                raise ValueError
            prefix = "buy_price" if key == "buy_price" else "sell_price"
            set_setting(prefix + "_min", clean_decimal(low))
            set_setting(prefix + "_max", clean_decimal(high))
            audit("MARKET_SETTING_CHANGED", admin_id=ADMIN_ID, details=f"{prefix}={low}-{high}")
        elif key == "max_active_ads_per_user":
            value = int(raw)
            if value < 1 or value > 100:
                raise ValueError
            set_setting(key, str(value))
            audit("MARKET_SETTING_CHANGED", admin_id=ADMIN_ID, details=f"{key}={value}")
        elif key == "ad_expiry_hours":
            value = int(raw)
            if value < 1 or value > 720:
                raise ValueError
            set_setting(key, str(value))
            audit("MARKET_SETTING_CHANGED", admin_id=ADMIN_ID, details=f"{key}={value}")
        else:
            value = D(raw)
            if value < 0:
                raise ValueError
            if key == "min_trade_amount" and value < Decimal("0.01"):
                raise ValueError
            if key == "min_ad_amount" and value < Decimal("0.01"):
                raise ValueError
            if key == "max_ad_amount" and value < 0:
                raise ValueError
            if key == "max_ad_amount" and value != 0 and value < market_decimal("min_ad_amount", "10"):
                raise ValueError
            if key == "min_ad_amount" and value < market_decimal("min_trade_amount", "10"):
                raise ValueError
            if key == "min_trade_amount" and value > market_decimal("min_ad_amount", str(value)):
                # Keep ad creation logically consistent: min ad quantity cannot be below trade minimum.
                set_setting("min_ad_amount", clean_decimal(value))
            set_setting(key, clean_decimal(value))
            audit("MARKET_SETTING_CHANGED", admin_id=ADMIN_ID, details=f"{key}={clean_decimal(value)}")
    except Exception:
        await update.message.reply_text("❌ القيمة غير صحيحة. أرسلها بالصيغة المطلوبة.")
        return ADMIN_MARKET_VALUE
    context.user_data.pop("market_setting_key", None)
    await update.message.reply_text("✅ تم حفظ إعداد السوق.")
    await update.message.reply_text(market_settings_text(), parse_mode=ParseMode.HTML, reply_markup=market_settings_keyboard())
    return ConversationHandler.END


def build_admin_market_conversation():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_market_set_callback, pattern=r"^admin_market_set:"),
        ],
        states={ADMIN_MARKET_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_market_value)]},
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )


def build_admin_delete_db_conversation():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_delete_db_start_callback,
                pattern=r"^admin_delete_db(?:_full)?$",
            )
        ],
        states={
            ADMIN_DB_PASSWORD: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    admin_delete_db_password,
                )
            ],
            ADMIN_DB_CONFIRM: [
                CallbackQueryHandler(
                    admin_delete_db_confirm_callback,
                    pattern=r"^admin_db_(confirm|confirm_full|cancel)$",
                )
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )


async def admin_delete_db_start_callback(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data["admin_db_delete_mode"] = (
        "full" if query.data == "admin_delete_db_full" else "non_kyc"
    )
    if context.user_data["admin_db_delete_mode"] == "full":
        description = (
            "💣 هذا الخيار سيحذف قاعدة البيانات بالكامل، بما فيها جميع بيانات المستخدمين والتوثيقات والإعدادات."
        )
    else:
        description = (
            "🗑️ هذا الخيار سيحذف بيانات التشغيل مثل الإعلانات والصفقات والمدفوعات والنزاعات والسجلات، "
            "مع الإبقاء على حسابات المستخدمين وطلبات KYC والتوثيقات."
        )
    await query.message.reply_text(
        f"⚠️ إجراء حذف حساس\n\n{description}\n\n"
        "🔐 للتأكد من صلاحية المدير، أرسل كلمة المرور:"
    )
    return ADMIN_DB_PASSWORD


async def admin_delete_db_password(update, context):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    password = (update.message.text or "").strip()
    if password != DATABASE_DELETE_PASSWORD:
        await update.message.reply_text(
            "❌ كلمة المرور غير صحيحة.\n\n"
            "أرسل كلمة المرور مرة أخرى، أو استخدم /start للإلغاء."
        )
        return ADMIN_DB_PASSWORD

    mode = context.user_data.get("admin_db_delete_mode", "non_kyc")
    if mode == "full":
        text = (
            "🚨 كلمة المرور صحيحة.\n\n"
            "💣 أنت على وشك حذف قاعدة البيانات بالكامل، بما فيها التوثيقات. "
            "لا يمكن التراجع عن هذا الإجراء.\n\nهل تريد المتابعة؟"
        )
        confirm_data = "admin_db_confirm_full"
    else:
        text = (
            "🚨 كلمة المرور صحيحة.\n\n"
            "🗑️ سيتم حذف بيانات التشغيل فقط، وستبقى بيانات المستخدمين وطلبات KYC والتوثيقات. "
            "لا يمكن التراجع عن بيانات التشغيل المحذوفة.\n\nهل تريد المتابعة؟"
        )
        confirm_data = "admin_db_confirm"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑️ نعم، تابع الحذف", callback_data=confirm_data),
                InlineKeyboardButton("❌ إلغاء", callback_data="admin_db_cancel"),
            ]
        ]),
    )
    return ADMIN_DB_CONFIRM


async def admin_delete_db_confirm_callback(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return ConversationHandler.END

    action = query.data
    await query.answer()

    if action == "admin_db_cancel":
        context.user_data.pop("admin_db_delete_mode", None)
        await query.message.reply_text("❌ تم إلغاء الحذف. لم يتم حذف أي بيانات.")
        return ConversationHandler.END

    try:
        if action == "admin_db_confirm_full":
            reset_database()
            message = (
                "✅ تم حذف قاعدة البيانات بالكامل وإعادة إنشاء الجداول فارغة.\n\n"
                "تم حذف التوثيقات وبيانات المستخدمين وجميع بيانات التشغيل."
            )
        else:
            clear_non_kyc_database_data()
            message = (
                "✅ تم حذف بيانات التشغيل بنجاح.\n\n"
                "🪪 بيانات المستخدمين وطلبات KYC والتوثيقات بقيت محفوظة."
            )
        await query.message.reply_text(message)
    except Exception:
        logger.exception("Failed to delete database data")
        await query.message.reply_text(
            "❌ فشل تنفيذ الحذف. لم يكتمل الإجراء، وتم تسجيل الخطأ لدى النظام."
        )
    finally:
        context.user_data.pop("admin_db_delete_mode", None)

    return ConversationHandler.END


def build_admin_recheck_conversation():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_recheck_start_callback,
                pattern=r"^admin_recheck$",
            )
        ],
        states={
            ADMIN_RECHECK_TRADE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_recheck_trade)
            ],
            ADMIN_RECHECK_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_recheck_txid)
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )


async def admin_recheck_start_callback(update, context):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("🚫 غير مصرح.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.pop("recheck_trade_id", None)
    await query.message.reply_text(
        "🔄 إعادة فحص إيداع USDT\n\n"
        "أرسل رقم الصفقة أو كود الصفقة التي تريد إعادة فحص إيداعها."
    )
    return ADMIN_RECHECK_TRADE


def build_release_conversation():

    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                release_start_callback,
                pattern=r"^release_start:",
            )
        ],

        states={

            ADMIN_RELEASE_TXID: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    release_txid,
                )
            ]
        },

        fallbacks=[
            CommandHandler(
                "start",
                start,
            )
        ],

        allow_reentry=True,
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context,
):

    query = update.callback_query

    if not query:

        return

    data = query.data or ""

    if is_blocked(query.from_user.id) and not is_admin(query.from_user.id):
        await query.answer("🚫 حسابك محظور.", show_alert=True)
        return

    if data.startswith("admin_user_block:"):
        await admin_user_block_callback(update, context)
        return

    if data.startswith("admin_user_detail:"):
        await admin_user_detail_callback(update, context)
        return

    # KYC
    if data.startswith("kyc_approve:") or data.startswith(
        "kyc_reject:"
    ):

        await kyc_admin_callback(
            update,
            context,
        )

        return

    # Dispute chat
    if data.startswith("dispute_admin_chat:"):
        await dispute_admin_chat_callback(update, context)
        return

    if data.startswith("dispute_user_chat:"):
        await dispute_user_chat_callback(update, context)
        return

    if data.startswith("dispute_resolve:"):
        await dispute_resolve_callback(update, context)
        return

    if data.startswith("ad_manage:"):
        await ad_manage_callback(update, context)
        return

    if data.startswith("ad_field:"):
        await ad_field_callback(update, context)
        return

    if data.startswith("ad_cancel_confirm:"):
        await ad_cancel_confirm_callback(update, context)
        return

    # Advertisement
    if data.startswith("ad_view:"):

        await view_ad(
            update,
            context,
        )

        return

    if data == "back_ads":

        await back_ads_callback(
            update,
            context,
        )

        return

    # Trade
    if data.startswith("trade_start:"):

        await trade_start_callback(
            update,
            context,
        )

        return

    if data.startswith("trade_view:"):

        await trade_view(
            update,
            context,
        )

        return

    # Deposit admin
    if data.startswith(
        "deposit_confirm:"
    ) or data.startswith(
        "deposit_reject:"
    ) or data.startswith(
        "deposit_manual_confirm:"
    ):

        await deposit_admin_callback(
            update,
            context,
        )

        return

    # Payment confirm
    if data.startswith(
        "payment_confirm:"
    ):

        await payment_confirm_callback(
            update,
            context,
        )

        return

    # Dispute
    if data.startswith("dispute:"):

        await dispute_callback(
            update,
            context,
        )

        return

    # Admin
    if data.startswith("admin_"):

        if data.startswith(
            "admin_trade:"
        ):

            await admin_trade_callback(
                update,
                context,
            )

        else:

            await admin_callback(
                update,
                context,
            )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )

    try:

        if update and update.effective_chat:

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ حدث خطأ غير متوقع.\n"
                    "تم تسجيل الخطأ لدى النظام."
                ),
            )

    except Exception:

        pass


# ============================================================
# APPLICATION
# ============================================================

def main():

    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("ضع BOT_TOKEN في القسم العلوي من الكود.")

    if not ADMIN_ID:
        raise RuntimeError("ضع ADMIN_ID في القسم العلوي من الكود.")

    if not GMAIL_ADDRESS or GMAIL_ADDRESS == "PUT_YOUR_GMAIL_HERE":
        raise RuntimeError("ضع GMAIL_ADDRESS في القسم العلوي من الكود.")

    if (
        not GMAIL_APP_PASSWORD
        or GMAIL_APP_PASSWORD == "PUT_YOUR_GMAIL_APP_PASSWORD_HERE"
    ):
        raise RuntimeError(
            "ضع GMAIL_APP_PASSWORD في القسم العلوي من الكود."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Conversations
    # --------------------------------------------------------

    application.add_handler(
        build_kyc_conversation()
    )

    application.add_handler(
        build_otp_conversation()
    )

    application.add_handler(
        build_ad_conversation()
    )

    application.add_handler(
        build_deposit_conversation()
    )

    application.add_handler(
        build_payment_conversation()
    )

    application.add_handler(
        build_admin_market_conversation()
    )

    application.add_handler(
        build_admin_delete_db_conversation()
    )

    application.add_handler(
        build_admin_recheck_conversation()
    )

    application.add_handler(
        build_admin_queue_search_conversation()
    )

    application.add_handler(
        build_release_conversation()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "setaddress",
            set_address,
        )
    )

    application.add_handler(
        CommandHandler(
            "setwallet",
            set_wallet,
        )
    )

    application.add_handler(
        CommandHandler(
            "setfee",
            set_fee,
        )
    )

    application.add_handler(
        CommandHandler(
            "settimeout",
            set_timeout,
        )
    )

    application.add_handler(
        CommandHandler(
            "block",
            block_user,
        )
    )

    application.add_handler(
        CommandHandler(
            "unblock",
            unblock_user,
        )
    )

    # --------------------------------------------------------
    # Callback router
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # --------------------------------------------------------
    # Text router
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router,
        )
    )

    # --------------------------------------------------------
    # Dispute photos
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(filters.PHOTO, dispute_user_photo)
    )

    # --------------------------------------------------------
    # Error
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Expiration job
    # --------------------------------------------------------

    application.job_queue.run_repeating(
        expire_old_trades,
        interval=60,
        first=30,
    )

    logger.info(
        "USDT P2P Mediator Bot starting..."
    )

    logger.info(
        "Network: %s",
        NETWORK,
    )

    logger.info(
        "Database: %s",
        DATABASE,
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()