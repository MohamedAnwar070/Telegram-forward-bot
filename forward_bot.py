import os
import re
import asyncio
import logging
from datetime import datetime
from cryptography.fernet import Fernet

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import FloodWait, MessageIdInvalid

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# ================== CONFIG & LOGGING ================== #

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

class Config:
    API_ID = int(os.getenv("API_ID") or 0)
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/forward_engine")
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not cls.SECRET_KEY:
            missing.append("SECRET_KEY")
        if not cls.ADMIN_ID:
            missing.append("ADMIN_ID")
        if missing:
            logger.error("❌ Missing required environment variables: %s", ", ".join(missing))
            logger.error("Please configure your .env file (see .env.example).")
            raise SystemExit(1)

fernet = Fernet(Config.SECRET_KEY.encode()) if Config.SECRET_KEY else None

# ================== FORWARD TYPE FILTERS ================== #

# Global toggles for which message types are forwarded.
# You can change True/False here to control forwarding behavior.
# All types default to True (forward everything supported).
FORWARD_FILTERS = {
    "text": True,        # plain text messages (no media)
    "link": True,        # messages where main content is a link
    "photo": True,
    "video": True,
    "document": True,
    "audio": True,
    "voice": True,
    "animation": True,   # GIFs, etc.
    "sticker": True,
    "video_note": True,
    "other": True,       # polls, locations, etc.
}

# ================== DATABASE ================== #

mongo = AsyncIOMotorClient(Config.MONGO_URI)
db = mongo.forward_engine

mappings = db.mappings        # source -> target
sessions = db.sessions        # encrypted/admin sessions & states
duplicates = db.duplicates    # forwarded messages
stats = db.stats              # statistics
accounts = db.accounts        # encrypted user sessions

# ================== STATE MANAGEMENT ================== #

class States:
    HOME = "HOME"
    ADD_SOURCE = "ADD_SOURCE"
    ADD_TARGET = "ADD_TARGET"
    ADD_CONFIRM = "ADD_CONFIRM"
    EDIT_CAPTION = "EDIT_CAPTION"
    ADD_SESSION = "ADD_SESSION"
    SET_SKIP = "SET_SKIP"
    EDIT_FILTERS = "EDIT_FILTERS"


def is_admin_user(user_id: int) -> bool:
    return user_id == Config.ADMIN_ID

async def get_state(user_id):
    doc = await sessions.find_one({"_id": user_id})
    return doc if doc else {"_id": user_id, "state": States.HOME, "data": {}}

async def update_state(user_id, state, data=None):
    update = {"state": state}
    if data is not None:
        update["data"] = data
    await sessions.update_one({"_id": user_id}, {"$set": update}, upsert=True)

async def clear_state(user_id):
    await sessions.delete_one({"_id": user_id})

# ================== CAPTION ENGINE ================== #

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"http\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"#\w+", "", text)
    return text.strip()

def build_caption(template: str, msg):
    meta = {
        "caption": clean_text(msg.caption or ""),
        "filename": getattr(msg.document, "file_name", ""),
        "size": getattr(msg.document, "file_size", ""),
        "duration": getattr(msg.video, "duration", ""),
    }
    for k, v in meta.items():
        template = template.replace(f"{{{k}}}", str(v))
    return template.strip()


def get_message_kind(msg: Message) -> str:
    """Rough classification of a message's main type for filtering."""
    # Text-only or link-only messages (no media attached)
    if msg.text and not msg.media:
        if re.search(r"https?://", msg.text):
            return "link"
        return "text"

    # Media types
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.animation:
        return "animation"
    if msg.sticker:
        return "sticker"
    if msg.video_note:
        return "video_note"

    return "other"


def is_allowed_to_forward(msg: Message, rule: dict | None = None) -> bool:
    """Decide if a message is allowed to be forwarded, considering per-rule and global filters."""
    kind = get_message_kind(msg)

    # Per-rule override: mapping document may specify an explicit list of allowed types
    if rule is not None:
        allowed_types = rule.get("allowed_types")
        if isinstance(allowed_types, list) and allowed_types:
            return kind in allowed_types

    # Fallback to global filters
    return FORWARD_FILTERS.get(kind, True)


async def run_history_forward(source_chat_id: int, target_chat_id: int, last_message_id: int, notify_msg: Message, caption_template: str, start_from: int = 1):
    forwarded = 0
    errors = 0
    missing = 0

    status_msg = await notify_msg.reply(
        f"⏳ Starting history forward from `{source_chat_id}` to `{target_chat_id}` up to message `{last_message_id}`. This may take a while..."
    )

    # Load rule config once for per-rule filters, if present
    rule_cfg = await mappings.find_one({"source": source_chat_id}) or {}

    msg_id = max(1, int(start_from))
    while msg_id <= last_message_id:
        dk = f"{source_chat_id}:{msg_id}"
        if await duplicates.find_one({"_id": dk}):
            msg_id += 1
            continue
        try:
            src_msg = await bot.get_messages(source_chat_id, msg_id)
        except MessageIdInvalid as e:
            # Message was deleted / does not exist – treat as missing, not a hard error
            logger.info(f"[BOT] Source message {msg_id} missing in {source_chat_id}: {e}")
            missing += 1
            await stats.update_one({"_id": "global"}, {"$inc": {"missing": 1}}, upsert=True)
            msg_id += 1
            continue
        except FloodWait as e:
            logger.warning(f"FloodWait while fetching source message {msg_id}: {e.value}s")
            await asyncio.sleep(e.value)
            continue
        except Exception as e:
            logger.error(f"Error fetching source message {msg_id}: {e}")
            errors += 1
            msg_id += 1
            continue

        if not src_msg:
            missing += 1
            msg_id += 1
            continue

        # Type-based filtering (per-rule, then global toggles)
        if not is_allowed_to_forward(src_msg, rule_cfg):
            msg_id += 1
            continue

        cap = build_caption(caption_template, src_msg)
        try:
            await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=src_msg.id,
                caption=cap,
            )
            await duplicates.insert_one({"_id": dk, "time": datetime.utcnow()})
            await stats.update_one({"_id": "global"}, {"$inc": {"forwarded": 1}}, upsert=True)
            forwarded += 1
        except MessageIdInvalid as e:
            # Source message vanished between fetch and copy
            logger.info(f"[BOT] Source message {msg_id} vanished while copying in {source_chat_id}: {e}")
            missing += 1
            await stats.update_one({"_id": "global"}, {"$inc": {"missing": 1}}, upsert=True)
        except FloodWait as e:
            logger.warning(f"FloodWait while forwarding message {msg_id}: {e.value}s")
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.error(f"Error forwarding message {msg_id}: {e}")
            errors += 1

        if forwarded and forwarded % 50 == 0:
            try:
                await status_msg.edit_text(
                    f"⏳ History forward in progress...\nForwarded: {forwarded}\nErrors: {errors}"
                )
            except Exception:
                pass

        msg_id += 1

    try:
        await status_msg.edit_text(
            f"✅ History forward completed.\nForwarded: {forwarded}\nMissing/Deleted: {missing}\nErrors: {errors}"
        )
    except Exception:
        await notify_msg.reply(
            f"✅ History forward completed.\nForwarded: {forwarded}\nMissing/Deleted: {missing}\nErrors: {errors}"
        )


async def run_history_forward_user(source_chat_id: int, target_chat_id: int, last_message_id: int, notify_msg: Message, caption_template: str, start_from: int = 1):
    acc = await accounts.find_one({"_id": "main"})
    if not acc or "enc_session" not in acc:
        await notify_msg.reply("❌ No user session configured. Use /addsession first.")
        return

    try:
        session_str = fernet.decrypt(acc["enc_session"].encode()).decode()
    except Exception as e:
        logger.error(f"Failed to decrypt user session: {e}")
        await notify_msg.reply("❌ Stored session is invalid. Please configure it again with /addsession.")
        return

    user_app = Client(
        "user_forwarder",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=session_str,
        in_memory=True,
    )

    forwarded = 0
    errors = 0

    status_msg = await notify_msg.reply(
        f"⏳ Starting history forward (user session) from `{source_chat_id}` to `{target_chat_id}` up to message `{last_message_id}`. This may take a while..."
    )

    await user_app.start()
    try:
        # Load rule config once for per-rule filters, if present
        rule_cfg = await mappings.find_one({"source": source_chat_id}) or {}

        # Preload dialogs so this in-memory session learns peers for source/target
        try:
            async for _ in user_app.get_dialogs(limit=200):
                pass
        except FloodWait as e:
            logger.warning(f"[USER] FloodWait while preloading dialogs: {e.value}s")
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.warning(f"[USER] Failed to preload dialogs: {e}")

        # Ensure we can resolve source and target chats before looping
        try:
            src_chat = await user_app.get_chat(source_chat_id)
        except Exception as e:
            logger.error(f"[USER] Cannot resolve source chat {source_chat_id}: {e}")
            try:
                await status_msg.edit_text(
                    f"❌ User session cannot access source chat `{source_chat_id}`.\nError: {e}"
                )
            except Exception:
                await notify_msg.reply(
                    f"❌ User session cannot access source chat `{source_chat_id}`.\nError: {e}"
                )
            return

        try:
            tgt_chat = await user_app.get_chat(target_chat_id)
        except Exception as e:
            logger.error(f"[USER] Cannot resolve target chat {target_chat_id}: {e}")
            try:
                await status_msg.edit_text(
                    f"❌ User session cannot access target chat `{target_chat_id}`.\n"
                    f"Make sure this account is a member/admin there.\nError: {e}"
                )
            except Exception:
                await notify_msg.reply(
                    f"❌ User session cannot access target chat `{target_chat_id}`.\n"
                    f"Make sure this account is a member/admin there.\nError: {e}"
                )
            return

        src_name = getattr(src_chat, "title", None) or getattr(src_chat, "first_name", "")
        tgt_name = getattr(tgt_chat, "title", None) or getattr(tgt_chat, "first_name", "")
        logger.info(
            f"[USER] Using user session to forward from {src_chat.id} ('{src_name}') "
            f"to {tgt_chat.id} ('{tgt_name}')"
        )

        msg_id = max(1, int(start_from))
        while msg_id <= last_message_id:
            dk = f"{source_chat_id}:{msg_id}"
            if await duplicates.find_one({"_id": dk}):
                msg_id += 1
                continue
            try:
                src_msg = await user_app.get_messages(source_chat_id, msg_id)
            except MessageIdInvalid as e:
                logger.info(f"[USER] Source message {msg_id} missing in {source_chat_id}: {e}")
                missing += 1
                await stats.update_one({"_id": "global"}, {"$inc": {"missing": 1}}, upsert=True)
                msg_id += 1
                continue
            except FloodWait as e:
                logger.warning(f"[USER] FloodWait while fetching source message {msg_id}: {e.value}s")
                await asyncio.sleep(e.value)
                continue
            except Exception as e:
                logger.error(f"[USER] Error fetching source message {msg_id}: {e}")
                errors += 1
                msg_id += 1
                continue

            if not src_msg:
                missing += 1
                msg_id += 1
                continue

            # Type-based filtering (per-rule, then global toggles)
            if not is_allowed_to_forward(src_msg, rule_cfg):
                msg_id += 1
                continue

            cap = build_caption(caption_template, src_msg)
            try:
                await user_app.copy_message(
                    chat_id=target_chat_id,
                    from_chat_id=source_chat_id,
                    message_id=src_msg.id,
                    caption=cap,
                )
                await duplicates.insert_one({"_id": dk, "time": datetime.utcnow()})
                await stats.update_one({"_id": "global"}, {"$inc": {"forwarded": 1}}, upsert=True)
                forwarded += 1
            except MessageIdInvalid as e:
                logger.info(f"[USER] Source message {msg_id} vanished while copying in {source_chat_id}: {e}")
                missing += 1
                await stats.update_one({"_id": "global"}, {"$inc": {"missing": 1}}, upsert=True)
            except FloodWait as e:
                logger.warning(f"[USER] FloodWait while forwarding message {msg_id}: {e.value}s")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"[USER] Error forwarding message {msg_id}: {e}")
                errors += 1

            if forwarded and forwarded % 50 == 0:
                try:
                    await status_msg.edit_text(
                        f"⏳ History forward in progress (user session)...\nForwarded: {forwarded}\nErrors: {errors}"
                    )
                except Exception:
                    pass

            msg_id += 1
    finally:
        await user_app.stop()

    try:
        await status_msg.edit_text(
            f"✅ History forward completed (user session).\nForwarded: {forwarded}\nMissing/Deleted: {missing}\nErrors: {errors}"
        )
    except Exception:
        await notify_msg.reply(
            f"✅ History forward completed (user session).\nForwarded: {forwarded}\nMissing/Deleted: {missing}\nErrors: {errors}"
        )


async def handle_history_link(msg: Message):
    if not msg.text:
        return

    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return

    text = msg.text.strip()
    m = re.match(r"https?://t\.me/(c/)?([^/]+)/([0-9]+)", text)
    if not m:
        await msg.reply("❌ This does not look like a valid Telegram message link.")
        return

    is_c = m.group(1) is not None
    chat_part = m.group(2)
    message_id = int(m.group(3))

    try:
        if is_c:
            source_id = int("-100" + chat_part)
        else:
            chat = await bot.get_chat(chat_part)
            source_id = chat.id
    except Exception as e:
        logger.error(f"Failed to resolve chat from link '{text}': {e}")
        await msg.reply("❌ Could not resolve the chat from this link. Make sure the bot is a member/admin there.")
        return

    cfg = await mappings.find_one({"source": source_id})
    if not cfg:
        await msg.reply(
            "⚠️ No forwarding rule found for this source chat. Add a rule first via 'Manage Rules → Add New Rule'."
        )
        return

    await msg.reply(
        f"🚀 Starting one-time history forward from `{source_id}` to `{cfg['target']}` up to message `{message_id}`."
    )

    # If a skip point is configured for this source, start from the next message
    skip_until = int(cfg.get("skip_until", 0) or 0)
    start_from = skip_until + 1 if skip_until > 0 else 1

    acc = await accounts.find_one({"_id": "main"})
    if acc and "enc_session" in acc:
        asyncio.create_task(
            run_history_forward_user(
                source_id,
                cfg["target"],
                message_id,
                msg,
                cfg.get("caption_template", "{caption}"),
                start_from=start_from,
            )
        )
    else:
        asyncio.create_task(
            run_history_forward(
                source_id,
                cfg["target"],
                message_id,
                msg,
                cfg.get("caption_template", "{caption}"),
                start_from=start_from,
            )
        )

# ================== KEYBOARDS ================== #

def get_home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Manage Rules", callback_data="nav_manage")],
        [InlineKeyboardButton("📊 Stats", callback_data="nav_stats"),
         InlineKeyboardButton("❓ Help", callback_data="nav_help")]
    ])

def get_manage_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add New Rule", callback_data="act_add")],
        [InlineKeyboardButton("📋 List Rules", callback_data="act_list")],
        [InlineKeyboardButton("🔙 Back to Home", callback_data="nav_home")]
    ])

def get_cancel_kb(back_to="nav_manage"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=back_to)]])

# ================== BOT INITIALIZATION ================== #

bot = Client(
    "forward_bot_prod",
    api_id=Config.API_ID or 1,
    api_hash=Config.API_HASH or "dummy_hash",
    bot_token=Config.BOT_TOKEN or "dummy_token",
    workers=20,
    sleep_threshold=1000
)

# ================== HANDLERS ================== #

@bot.on_message(filters.all, group=-200)
async def global_logger(_, msg: Message):
    chat = msg.chat
    uid = msg.from_user.id if msg.from_user else "N/A"
    text = (msg.text or msg.caption or "Media")[:50]
    logger.info(
        f"🌍 [UPDATE] chat_id={chat.id} type={chat.type} from={uid} text='{text}'"
    )
    msg.continue_propagation()

@bot.on_message(filters.private, group=-100)
async def diagnostic_logger(_, msg: Message):
    uid = msg.from_user.id if msg.from_user else "N/A"
    text = (msg.text or "Media")[:50]
    logger.info(f"🚨 [MSG RECEIVED] From {uid}: {text}")
    msg.continue_propagation()

@bot.on_message(filters.command("id") & filters.private)
async def cmd_id_handler(_, msg: Message):
    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return
    uid = msg.from_user.id
    await msg.reply(f"🆔 **Your ID**: `{uid}`\n👑 **Admin**: `{Config.ADMIN_ID}`")

@bot.on_message(filters.command("help") & filters.private)
async def cmd_help_handler(_, msg: Message):
    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return
    await msg.reply(
        "❓ **Help**\nUse the buttons to manage forwarding rules.\n\n"
        "- ⚙️ *Manage Rules*: Add or list forward rules.\n"
        "- 📊 *Stats*: View forwarded message counts.\n"
        "- ❓ *Help*: Show this message.",
        reply_markup=get_home_kb(),
    )

@bot.on_message(filters.command("start") & filters.private)
async def cmd_start_handler(_, msg: Message):
    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return
    await clear_state(msg.from_user.id)
    await msg.reply(
        "👋 **Welcome to the Command Center!**\nUse buttons to manage your bot.",
        reply_markup=get_home_kb()
    )


@bot.on_message(filters.command("addsession") & filters.private)
async def cmd_addsession_handler(_, msg: Message):
    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return
    user_id = msg.from_user.id
    await update_state(user_id, States.ADD_SESSION)
    await msg.reply(
        "🔐 **Add User Session**\nSend your Pyrogram `SESSION_STRING` for your account.\n"
        "It will be encrypted with `SECRET_KEY` and stored only in MongoDB.\n\n"
        "Never share this string with anyone else.",
        reply_markup=get_cancel_kb("nav_home"),
    )


@bot.on_message(filters.command("setskip") & filters.private)
async def cmd_setskip_handler(_, msg: Message):
    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return
    user_id = msg.from_user.id
    await update_state(user_id, States.SET_SKIP)
    await msg.reply(
        "⏭ **Set Skip Point**\nSend a Telegram *message link* from the **source** channel/group.\n"
        "History jobs will then skip all messages up to that ID and start from the next one.",
        reply_markup=get_cancel_kb("nav_home"),
    )

@bot.on_callback_query()
async def cb_dispatcher(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if not is_admin_user(user_id):
        return
    data = query.data
    logger.info(f"🔘 Callback: {data} from {user_id}")

    try:
        if data == "nav_home":
            await update_state(user_id, States.HOME)
            await query.message.edit("👋 **Main Menu**", reply_markup=get_home_kb())
        elif data == "nav_manage":
            await query.message.edit("⚙️ **Rule Management**", reply_markup=get_manage_kb())
        elif data == "nav_stats":
            s = await stats.find_one({"_id": "global"}) or {}
            text = f"📊 **Stats**\n\n✅ Forwarded: {s.get('forwarded', 0)}\n🔄 Dups: {s.get('duplicates', 0)}\n❌ Errors: {s.get('errors', 0)}"
            await query.message.edit(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="nav_home")]]))
        elif data == "nav_help":
            await query.message.edit("📖 **Help**\nManage rules and captions via buttons.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="nav_home")]]))
        elif data == "act_add":
            await update_state(user_id, States.ADD_SOURCE)
            await query.message.edit("🔢 **Step 1: Source ID**\nProvide Source Channel/Group ID.", reply_markup=get_cancel_kb())
        elif data == "act_list":
            rules = await mappings.find().to_list(length=50)
            if not rules: return await query.answer("📭 No rules!", show_alert=True)
            kb = [[InlineKeyboardButton(f"{r['source']} ➡️ {r['target']}", callback_data=f"rule_{r['source']}")] for r in rules]
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="nav_manage")])
            await query.message.edit("📋 **Rules**", reply_markup=InlineKeyboardMarkup(kb))
        elif data.startswith("rule_"):
            sid = int(data.split("_")[1])
            rule = await mappings.find_one({"source": sid})
            if not rule: return await query.answer("Gone!")
            filters_desc = ", ".join(rule.get("allowed_types", [])) if rule.get("allowed_types") else "default (all enabled)"
            kb = [
                [InlineKeyboardButton("✏️ Caption", callback_data=f"edit_cap_{sid}"),
                 InlineKeyboardButton("🎚 Filters", callback_data=f"edit_filt_{sid}")],
                [InlineKeyboardButton("🗑️ Delete", callback_data=f"del_rule_{sid}")],
                [InlineKeyboardButton("🔙 Back", callback_data="act_list")],
            ]
            await query.message.edit(
                f"📍 Rule: {sid}\nTarget: {rule['target']}\nFilters: {filters_desc}",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        elif data.startswith("edit_cap_"):
            sid = int(data.split("_")[2])
            await update_state(user_id, States.EDIT_CAPTION, {"source": sid})
            await query.message.edit("📝 Send new template:", reply_markup=get_cancel_kb(f"rule_{sid}"))
        elif data.startswith("edit_filt_"):
            sid = int(data.split("_")[2])
            rule = await mappings.find_one({"source": sid}) or {}
            current = rule.get("allowed_types")
            if isinstance(current, list) and current:
                allowed = set(current)
            else:
                allowed = {k for k, v in FORWARD_FILTERS.items() if v}

            kinds = [
                "text", "link", "photo", "video", "document",
                "audio", "voice", "animation", "sticker", "video_note", "other",
            ]
            labels = {
                "text": "Text",
                "link": "Links",
                "photo": "Photos",
                "video": "Videos",
                "document": "Files",
                "audio": "Audio",
                "voice": "Voice",
                "animation": "GIFs",
                "sticker": "Stickers",
                "video_note": "Video Notes",
                "other": "Other",
            }

            rows = []
            row = []
            for kind in kinds:
                prefix = "✅" if kind in allowed else "🚫"
                row.append(InlineKeyboardButton(f"{prefix} {labels[kind]}", callback_data=f"filt_toggle_{sid}_{kind}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("🔄 Reset to default", callback_data=f"filt_reset_{sid}")])
            rows.append([InlineKeyboardButton("✅ Done", callback_data=f"filt_done_{sid}")])

            desc = ", ".join(sorted(allowed)) if allowed else "none"
            await query.message.edit(
                f"🎚 **Edit Filters for {sid}**\nCurrent: {desc}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        elif data.startswith("del_rule_"):
            sid = int(data.split("_")[2])
            await mappings.delete_one({"source": sid})
            await query.message.edit("🗑️ Deleted.", reply_markup=get_manage_kb())
        elif data.startswith("filt_toggle_"):
            parts = data.split("_")
            sid = int(parts[2])
            kind = parts[3]
            if kind not in FORWARD_FILTERS:
                return
            rule = await mappings.find_one({"source": sid}) or {}
            current = rule.get("allowed_types")
            if isinstance(current, list) and current:
                allowed = set(current)
            else:
                allowed = {k for k, v in FORWARD_FILTERS.items() if v}
            if kind in allowed:
                allowed.remove(kind)
            else:
                allowed.add(kind)
            await mappings.update_one({"source": sid}, {"$set": {"allowed_types": sorted(allowed)}}, upsert=True)

            # Re-open editor with updated state (no recursion)
            rule = await mappings.find_one({"source": sid}) or {}
            current = rule.get("allowed_types")
            if isinstance(current, list) and current:
                allowed = set(current)
            else:
                allowed = {k for k, v in FORWARD_FILTERS.items() if v}

            kinds = [
                "text", "link", "photo", "video", "document",
                "audio", "voice", "animation", "sticker", "video_note", "other",
            ]
            labels = {
                "text": "Text",
                "link": "Links",
                "photo": "Photos",
                "video": "Videos",
                "document": "Files",
                "audio": "Audio",
                "voice": "Voice",
                "animation": "GIFs",
                "sticker": "Stickers",
                "video_note": "Video Notes",
                "other": "Other",
            }

            rows = []
            row = []
            for k in kinds:
                prefix = "✅" if k in allowed else "🚫"
                row.append(InlineKeyboardButton(f"{prefix} {labels[k]}", callback_data=f"filt_toggle_{sid}_{k}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("🔄 Reset to default", callback_data=f"filt_reset_{sid}")])
            rows.append([InlineKeyboardButton("✅ Done", callback_data=f"filt_done_{sid}")])

            desc = ", ".join(sorted(allowed)) if allowed else "none"
            await query.message.edit(
                f"🎚 **Edit Filters for {sid}**\nCurrent: {desc}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        elif data.startswith("filt_reset_"):
            sid = int(data.split("_")[2])
            await mappings.update_one({"source": sid}, {"$unset": {"allowed_types": ""}})
            # After reset, show editor again with defaults
            rule = await mappings.find_one({"source": sid}) or {}
            current = rule.get("allowed_types")
            if isinstance(current, list) and current:
                allowed = set(current)
            else:
                allowed = {k for k, v in FORWARD_FILTERS.items() if v}

            kinds = [
                "text", "link", "photo", "video", "document",
                "audio", "voice", "animation", "sticker", "video_note", "other",
            ]
            labels = {
                "text": "Text",
                "link": "Links",
                "photo": "Photos",
                "video": "Videos",
                "document": "Files",
                "audio": "Audio",
                "voice": "Voice",
                "animation": "GIFs",
                "sticker": "Stickers",
                "video_note": "Video Notes",
                "other": "Other",
            }

            rows = []
            row = []
            for k in kinds:
                prefix = "✅" if k in allowed else "🚫"
                row.append(InlineKeyboardButton(f"{prefix} {labels[k]}", callback_data=f"filt_toggle_{sid}_{k}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("🔄 Reset to default", callback_data=f"filt_reset_{sid}")])
            rows.append([InlineKeyboardButton("✅ Done", callback_data=f"filt_done_{sid}")])

            desc = ", ".join(sorted(allowed)) if allowed else "none"
            await query.message.edit(
                f"🎚 **Edit Filters for {sid}**\nCurrent: {desc}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        elif data.startswith("filt_done_"):
            sid = int(data.split("_")[2])
            rule = await mappings.find_one({"source": sid})
            if not rule:
                await query.answer("Gone!", show_alert=True)
                return
            filters_desc = ", ".join(rule.get("allowed_types", [])) if rule.get("allowed_types") else "default (all enabled)"
            kb = [
                [InlineKeyboardButton("✏️ Caption", callback_data=f"edit_cap_{sid}"),
                 InlineKeyboardButton("🎚 Filters", callback_data=f"edit_filt_{sid}")],
                [InlineKeyboardButton("🗑️ Delete", callback_data=f"del_rule_{sid}")],
                [InlineKeyboardButton("🔙 Back", callback_data="act_list")],
            ]
            await query.message.edit(
                f"📍 Rule: {sid}\nTarget: {rule['target']}\nFilters: {filters_desc}",
                reply_markup=InlineKeyboardMarkup(kb),
            )
            return
        elif data == "conf_add":
            sd = await get_state(user_id)
            d = sd.get("data", {})
            await mappings.update_one({"source": d["source"]}, {"$set": {"source": d["source"], "target": d["target"], "caption_template": "{caption}", "created_at": datetime.utcnow()}}, upsert=True)
            await clear_state(user_id)
            await query.message.edit("✅ Rule Added!", reply_markup=get_manage_kb())
    except Exception as e:
        logger.error(f"CB Error: {e}")

@bot.on_message(filters.private & ~filters.command(["start", "id", "help", "addsession", "setskip"]))
async def text_input_handler(_, msg: Message):
    if not msg.from_user or not is_admin_user(msg.from_user.id):
        return

    user_id = msg.from_user.id
    sd = await get_state(user_id)
    state, data = sd["state"], sd.get("data", {})
    if state == States.ADD_SOURCE:
        raw = msg.text.strip()
        try:
            # Accept numeric IDs, @usernames, and t.me links
            text = raw
            if re.match(r"^-?\d+$", text):
                cid = int(text)
            elif text.startswith("https://t.me/") or text.startswith("http://t.me/"):
                m = re.match(r"https?://t\.me/(c/)?([^/]+)", text)
                if not m:
                    raise ValueError
                is_c = m.group(1) is not None
                chat_part = m.group(2)
                if is_c and chat_part.isdigit():
                    cid = int("-100" + chat_part)
                else:
                    chat = await _.get_chat(chat_part)
                    cid = chat.id
            elif raw.startswith("@"):
                chat = await _.get_chat(raw[1:])
                cid = chat.id
            else:
                raise ValueError

            data["source"] = cid
            await update_state(user_id, States.ADD_TARGET, data)
            await msg.reply("✅ Source Set. Send Target ID (ID, @link, or t.me link):", reply_markup=get_cancel_kb())
        except Exception:
            await msg.reply("❌ Invalid source. Send channel/group ID, @username, or t.me link.")
    elif state == States.ADD_TARGET:
        raw = msg.text.strip()
        try:
            text = raw
            if re.match(r"^-?\d+$", text):
                cid = int(text)
            elif text.startswith("https://t.me/") or text.startswith("http://t.me/"):
                m = re.match(r"https?://t\.me/(c/)?([^/]+)", text)
                if not m:
                    raise ValueError
                is_c = m.group(1) is not None
                chat_part = m.group(2)
                if is_c and chat_part.isdigit():
                    cid = int("-100" + chat_part)
                else:
                    chat = await _.get_chat(chat_part)
                    cid = chat.id
            elif raw.startswith("@"):
                chat = await _.get_chat(raw[1:])
                cid = chat.id
            else:
                raise ValueError

            data["target"] = cid
            await update_state(user_id, States.ADD_CONFIRM, data)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="conf_add")], [InlineKeyboardButton("❌ Cancel", callback_data="nav_manage")]])
            await msg.reply(f"🧐 Confirm: {data['source']} ➡ {data['target']}?", reply_markup=kb)
        except Exception:
            await msg.reply("❌ Invalid target. Send channel/group ID, @username, or t.me link.")
    elif state == States.EDIT_CAPTION:
        sid = data.get("source")
        if sid:
            await mappings.update_one({"source": sid}, {"$set": {"caption_template": msg.text}})
            await clear_state(user_id)
            await msg.reply("✅ Caption Updated!", reply_markup=get_home_kb())
    elif state == States.EDIT_FILTERS:
        sid = data.get("source")
        if not sid:
            await clear_state(user_id)
            await msg.reply("⚠️ Rule context lost. Open the rule again.", reply_markup=get_home_kb())
            return

        raw = (msg.text or "").strip().lower()
        valid_kinds = {"text", "link", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "other"}

        if raw in {"*", "default", "all"}:
            await mappings.update_one({"source": sid}, {"$unset": {"allowed_types": ""}})
            await clear_state(user_id)
            await msg.reply("✅ Filters reset to default (global) for this rule.", reply_markup=get_home_kb())
            return

        parts = [p for p in re.split(r"[,\s]+", raw) if p]
        if not parts:
            await msg.reply("❌ Send one or more types (e.g. `video,document`) or `*` for default.")
            return

        invalid = [p for p in parts if p not in valid_kinds]
        if invalid:
            await msg.reply(
                "❌ Invalid types: " + ", ".join(invalid) + "\nValid: " + ", ".join(sorted(valid_kinds))
            )
            return

        await mappings.update_one({"source": sid}, {"$set": {"allowed_types": parts}})
        await clear_state(user_id)
        await msg.reply(
            "✅ Filters updated for this rule. Allowed types: " + ", ".join(parts),
            reply_markup=get_home_kb(),
        )
    elif state == States.SET_SKIP:
        text = (msg.text or "").strip()
        # Expect a Telegram message link, reuse the same pattern as handle_history_link
        m = re.match(r"https?://t\.me/(c/)?([^/]+)/([0-9]+)", text)
        if not m:
            await msg.reply("❌ Send a valid Telegram message link from the *source* channel/group.")
            return

        is_c = m.group(1) is not None
        chat_part = m.group(2)
        message_id = int(m.group(3))

        try:
            if is_c:
                source_id = int("-100" + chat_part)
            else:
                chat = await _.get_chat(chat_part)
                source_id = chat.id
        except Exception:
            await msg.reply("❌ Could not resolve the chat from this link. Make sure the bot can see that chat.")
            return

        cfg = await mappings.find_one({"source": source_id})
        if not cfg:
            await msg.reply("⚠️ No forwarding rule found for this source. Add a rule first in *Manage Rules*.")
            return

        await mappings.update_one(
            {"source": source_id},
            {"$set": {"skip_until": message_id}},
        )
        await clear_state(user_id)
        await msg.reply(
            f"✅ Skip point set for source `{source_id}` at message `{message_id}`.\n"
            f"Future history jobs will start from `{message_id + 1}`.",
            reply_markup=get_home_kb(),
        )
    elif state == States.ADD_SESSION:
        session_raw = msg.text.strip()
        try:
            enc = fernet.encrypt(session_raw.encode()).decode()
            await accounts.update_one({"_id": "main"}, {"$set": {"enc_session": enc}}, upsert=True)
            await clear_state(user_id)
            await msg.reply("✅ User session stored securely.", reply_markup=get_home_kb())
        except Exception:
            await msg.reply("❌ Failed to store session. Please try again.")
    else:
        await handle_history_link(msg)

@bot.on_message(filters.channel | filters.group)
async def forward_handler(_, msg: Message):
    cfg = await mappings.find_one({"source": msg.chat.id})
    if not cfg:
        return

    # Per-rule / global type-based filtering for live forwards
    if not is_allowed_to_forward(msg, cfg):
        return

    dk = f"{msg.chat.id}:{msg.id}"
    if await duplicates.find_one({"_id": dk}):
        return
    cap = build_caption(cfg["caption_template"], msg)
    try:
        await bot.copy_message(chat_id=cfg["target"], from_chat_id=msg.chat.id, message_id=msg.id, caption=cap)
        await duplicates.insert_one({"_id": dk, "time": datetime.utcnow()})
        await stats.update_one({"_id": "global"}, {"$inc": {"forwarded": 1}}, upsert=True)
    except Exception:
        await stats.update_one({"_id": "global"}, {"$inc": {"errors": 1}}, upsert=True)

if __name__ == "__main__":
    Config.validate()
    logger.info("🎬 Initializing Command Center...")
    bot.run()
