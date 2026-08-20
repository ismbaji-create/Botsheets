"""
Telegram Results Bot — one ID, one account, one lookup, ever.

How it works
------------
- Runs a Telegram bot (long-polling) in a background thread.
- Runs a tiny Flask web server on the side, only so Render (or any free
  host) has an HTTP port to bind to and so a free "keep-alive" pinger
  (e.g. cron-job.org) has something to hit every few minutes.
- Reads/writes a Google Sheet directly via the Sheets API (gspread).
  No database needed — two extra columns in the same sheet ("Used" and
  "TelegramUserID") act as the lock.

Required environment variables (set these in Render's dashboard):
  BOT_TOKEN              - from @BotFather
  GOOGLE_SHEET_ID         - the long ID in your sheet's URL
  GOOGLE_CREDENTIALS_JSON - the full contents of your service-account
                            JSON key, pasted as one string
  ADMIN_CHAT_ID           - (optional) your own Telegram numeric ID,
                            lets you run /unlock to fix mistakes
"""

import os
import json
import logging
import threading

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask
from waitress import serve

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("results-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
PORT = int(os.environ.get("PORT", 10000))

ID_COL_NAME = "ID Number"
USED_COL_NAME = "Used"
TG_COL_NAME = "TelegramUserID"

# ---------------------------------------------------------------------------
# Google Sheets setup
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
_creds = Credentials.from_service_account_info(_creds_info, scopes=SCOPES)
_gc = gspread.authorize(_creds)
_sheet = _gc.open_by_key(GOOGLE_SHEET_ID)
_ws = _sheet.sheet1  # first tab

# Column index cache: {header_name: 1-based column number}
_col_index = {}


def _refresh_header():
    """Read row 1, add Used/TelegramUserID columns if they're missing."""
    header = _ws.row_values(1)
    changed = False
    if USED_COL_NAME not in header:
        header.append(USED_COL_NAME)
        changed = True
    if TG_COL_NAME not in header:
        header.append(TG_COL_NAME)
        changed = True
    if changed:
        _ws.update("A1", [header])
        log.info("Added missing tracking columns to sheet header.")
    _col_index.clear()
    for i, name in enumerate(header, start=1):
        _col_index[name.strip()] = i
    if ID_COL_NAME not in _col_index:
        raise RuntimeError(
            f"Sheet header must contain a column called exactly '{ID_COL_NAME}'."
        )


_refresh_header()

# Subjects = every header column except ID/Used/TelegramUserID, in sheet order
def _subject_columns():
    header = _ws.row_values(1)
    return [h for h in header if h.strip() not in (ID_COL_NAME, USED_COL_NAME, TG_COL_NAME)]


# ---------------------------------------------------------------------------
# Core lookup / lock logic (runs sequentially — PTB processes one update at
# a time by default, so there is no race condition on the sheet writes).
# ---------------------------------------------------------------------------
def lookup_and_claim(student_id: str, telegram_user_id: int):
    """
    Returns a tuple (status, payload):
      status = "ok"            -> payload is dict of {subject: score}
      status = "not_found"     -> payload is None
      status = "id_used"       -> payload is None (this ID already claimed)
      status = "account_used"  -> payload is the ID this account already claimed
    """
    rows = _ws.get_all_values()
    header = rows[0]
    id_col = _col_index[ID_COL_NAME]
    used_col = _col_index[USED_COL_NAME]
    tg_col = _col_index[TG_COL_NAME]

    tg_str = str(telegram_user_id)
    target = student_id.strip().upper()

    # 1) has this Telegram account already claimed a (possibly different) ID?
    for row in rows[1:]:
        row = row + [""] * (len(header) - len(row))  # pad short rows
        if row[tg_col - 1].strip() == tg_str:
            return "account_used", row[id_col - 1]

    # 2) find the requested ID
    for r_idx, row in enumerate(rows[1:], start=2):
        row = row + [""] * (len(header) - len(row))
        if row[id_col - 1].strip().upper() == target:
            if row[used_col - 1].strip().lower() in ("yes", "true", "1"):
                return "id_used", None
            # claim it
            _ws.update_cell(r_idx, used_col, "yes")
            _ws.update_cell(r_idx, tg_col, tg_str)
            scores = {}
            for subj in _subject_columns():
                scores[subj] = row[_col_index[subj] - 1]
            return "ok", scores

    return "not_found", None


def unlock_id(student_id: str):
    """Admin helper: clear the lock on one ID so it can be re-checked."""
    rows = _ws.get_all_values()
    id_col = _col_index[ID_COL_NAME]
    used_col = _col_index[USED_COL_NAME]
    tg_col = _col_index[TG_COL_NAME]
    target = student_id.strip().upper()
    for r_idx, row in enumerate(rows[1:], start=2):
        if len(row) >= id_col and row[id_col - 1].strip().upper() == target:
            _ws.update_cell(r_idx, used_col, "")
            _ws.update_cell(r_idx, tg_col, "")
            return True
    return False


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome. Send your ID number exactly as issued (e.g. RU0001/15) "
        "to receive your result.\n\n"
        "Each ID can be checked once, and each Telegram account can check one ID."
    )


async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_CHAT_ID or str(update.effective_chat.id) != ADMIN_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unlock <ID Number>")
        return
    ok = unlock_id(" ".join(context.args))
    await update.message.reply_text("Unlocked." if ok else "ID not found.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    student_id = update.message.text.strip()
    user_id = update.effective_user.id

    status, payload = lookup_and_claim(student_id, user_id)

    if status == "account_used":
        await update.message.reply_text(
            "This Telegram account has already been used to check a result "
            f"(ID: {payload}). Each account can only check one result."
        )
    elif status == "id_used":
        await update.message.reply_text(
            "This ID has already been used to view a result. If this is a "
            "mistake, contact the exam office."
        )
    elif status == "not_found":
        await update.message.reply_text(
            "No matching ID was found. Please check it and send it again "
            "exactly as issued."
        )
    elif status == "ok":
        lines = [f"Result for {student_id}:\n"]
        for subject, score in payload.items():
            lines.append(f"{subject}: {score}")
        await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Flask health endpoint (for Render port binding + keep-alive pings)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)


@flask_app.get("/")
def health():
    return "OK", 200


def run_flask():
    serve(flask_app, host="0.0.0.0", port=PORT)


def run_bot():
    import asyncio

def run_bot():
    # Set explicit event loop for Python 3.10+ / 3.14 background thread compatibility
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("unlock", unlock))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
