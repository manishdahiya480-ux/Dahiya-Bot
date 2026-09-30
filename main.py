import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Set
import difflib

from dotenv import load_dotenv
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, BigInteger, Text, Float,
    select, update, delete, func, or_, and_
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

import telegram
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ChatMember
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8988983689:AAG41_mlwkhxNuUafeLCT-tJnObIFcYgpcc")
OWNER_ID = int(os.getenv("OWNER_ID", "91460524"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///dahiya_movie_bot.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger("DahiyaMovieBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    role = Column(String, default="USER")  # OWNER, ADMIN, MODERATOR, USER
    search_count = Column(Integer, default=0)
    request_count = Column(Integer, default=0)
    is_banned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)

class MediaSource(Base):
    __tablename__ = 'media_sources'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    is_active = Column(Boolean, default=True)
    indexing_status = Column(String, default="IDLE")  # IDLE, INDEXING, COMPLETED
    indexed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Media(Base):
    __tablename__ = 'media'
    id = Column(Integer, primary_key=True, autoincrement=True)
    caption = Column(Text, nullable=False, index=True)
    file_id = Column(String, nullable=False)
    file_type = Column(String, default="document") # document, video, audio
    title_norm = Column(String, nullable=False, index=True)
    language = Column(String, nullable=True, index=True)
    year = Column(Integer, nullable=True, index=True)
    quality = Column(String, nullable=True, index=True)
    season = Column(Integer, nullable=True, index=True)
    episode = Column(Integer, nullable=True, index=True)
    size_mb = Column(String, nullable=True)
    source_id = Column(Integer, nullable=True)
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ForceJoin(Base):
    __tablename__ = 'force_joins'
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False)
    title = Column(String, nullable=False)
    link = Column(String, nullable=False)
    button_text = Column(String, default="📢 Join Channel")
    is_active = Column(Boolean, default=True)
    order_num = Column(Integer, default=0)

class Request(Base):
    __tablename__ = 'requests'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    display_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    title = Column(String, nullable=False)
    status = Column(String, default="PENDING")  # PENDING, COMPLETED, REJECTED
    admin_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Setting(Base):
    __tablename__ = 'settings'
    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)

class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(BigInteger, nullable=False)
    action = Column(String, nullable=False)
    target = Column(String, nullable=True)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

class SearchSession:
    def __init__(self, query: str, user_id: int):
        self.query = query
        self.user_id = user_id
        self.results: List[Media] = []
        self.page: int = 1
        self.filters: Dict[str, Optional[Any]] = {
            "lang": None,
            "year": None,
            "quality": None,
            "season": None,
            "episode": None
        }
        self.start_time: float = time.time()
        self.exec_time: float = 0.0
        self.suggestion: Optional[str] = None

ACTIVE_SEARCHES: Dict[int, SearchSession] = {}
MESSAGES_TO_DELETE: List[Dict[str, Any]] = []  # Format: {"chat_id": int, "msg_id": int, "delete_at": datetime}

class MetadataParser:
    """Extracts and normalizes metadata from caption strings."""
    
    @staticmethod
    def parse(caption: str) -> dict:
        data = {
            "title": "", "language": None, "year": None,
            "quality": None, "season": None, "episode": None,
            "size_mb": None
        }
        
        # File Size
        size_match = re.search(r'\[([\d\.]+)\s*(MB|GB)\]', caption, re.IGNORECASE)
        if size_match:
            data["size_mb"] = f"{size_match.group(1)} {size_match.group(2).upper()}"
            
        # Resolution / Quality
        qual_match = re.search(r'(360p|480p|576p|720p|1080p|1440p|2160p|4k)', caption, re.IGNORECASE)
        if qual_match:
            data["quality"] = qual_match.group(1).upper()
            
        # Release Year
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', caption)
        if year_match:
            data["year"] = int(year_match.group(1))
            
        # Season Normalization (S01, S-01, S_01, Season 01, SEASON1 -> Season 1)
        season_match = re.search(r'(?:S|Season)\s*[-_]?\s*(\d+)', caption, re.IGNORECASE)
        if season_match:
            data["season"] = int(season_match.group(1))
            
        # Episode Normalization (E01, Ep01, Episode 01 -> Episode 1)
        ep_match = re.search(r'(?:E|Ep|Episode)\s*[-_]?\s*(\d+)', caption, re.IGNORECASE)
        if ep_match:
            data["episode"] = int(ep_match.group(1))
            
        # Languages
        langs = [
            "Hindi", "English", "Tamil", "Telugu", "Malayalam", 
            "Kannada", "Bengali", "Punjabi", "Marathi", "Gujarati"
        ]
        found_langs = []
        for l in langs:
            if re.search(rf'\b{l}\b', caption, re.IGNORECASE):
                found_langs.append(l)
        if found_langs:
            data["language"] = found_langs[0]
            
        # Clean title normalization
        clean = re.sub(r'\[.*?\]|\{.*?\}|\(.*?\)', ' ', caption)
        clean = re.sub(r'(360p|480p|576p|720p|1080p|1440p|2160p|4k|AMZN|WEB-DL|HDRip|AAC2|x264|x265|HEVC)', ' ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'(?:S|Season)\s*[-_]?\s*\d+', ' ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'(?:E|Ep|Episode)\s*[-_]?\s*\d+', ' ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\b(19\d{2}|20\d{2})\b', ' ', clean)
        clean = " ".join(clean.split()).replace("❗", "").replace("📗", "").strip()
        data["title"] = clean.lower()
        
        return data

class SearchEngine:
    @staticmethod
    async def search(query: str) -> Tuple[List[Media], float, Optional[str]]:
        start_t = time.time()
        query_norm = query.strip().lower()
        words = query_norm.split()
        
        async with AsyncSessionLocal() as session:
            # Step 1: SQL Pattern Search (Exact/Partial/Word-anywhere)
            conditions = [
                Media.caption.ilike(f"%{query_norm}%"),
                Media.title_norm.ilike(f"%{query_norm}%")
            ]
            for word in words:
                if len(word) > 2:
                    conditions.append(Media.caption.ilike(f"%{word}%"))

            stmt = select(Media).where(or_(*conditions)).limit(300)
            result = await session.execute(stmt)
            matches = list(result.scalars().all())
            
            suggestion = None
            
            # Step 2: Fuzzy Matching fallback if few matches found
            if len(matches) < 5:
                all_stmt = select(Media).order_by(Media.id.desc()).limit(2000)
                all_res = await session.execute(all_stmt)
                all_media = all_res.scalars().all()
                
                fuzzy_matches = []
                best_suggestion_ratio = 0.0
                best_suggested_title = None

                for m in all_media:
                    ratio = difflib.SequenceMatcher(None, query_norm, m.title_norm).ratio()
                    if ratio > 0.65:
                        fuzzy_matches.append((ratio, m))
                    if ratio > best_suggestion_ratio and ratio > 0.55:
                        best_suggestion_ratio = ratio
                        best_suggested_title = m.title_norm.title()

                fuzzy_matches.sort(key=lambda x: x[0], reverse=True)
                for r, m in fuzzy_matches:
                    if m not in matches:
                        matches.append(m)
                        
                if not matches and best_suggested_title:
                    suggestion = best_suggested_title

            exec_time = time.time() - start_t
            return matches, exec_time, suggestion

class DB:
    @staticmethod
    async def get_user(user_id: int) -> Optional[User]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            return result.scalar_one_or_none()

    @staticmethod
    async def create_or_update_user(user: telegram.User) -> User:
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User).where(User.id == user.id))
            db_user = result.scalar_one_or_none()
            if not db_user:
                role = "OWNER" if user.id == OWNER_ID else "USER"
                db_user = User(
                    id=user.id, 
                    username=user.username, 
                    first_name=user.first_name, 
                    role=role
                )
                db_session.add(db_user)
            else:
                db_user.username = user.username
                db_user.first_name = user.first_name
                db_user.last_active = datetime.utcnow()
            await db_session.commit()
            return db_user

    @staticmethod
    async def get_setting(key: str, default: str) -> str:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Setting).where(Setting.key == key))
            row = result.scalar_one_or_none()
            return row.value if row else default

    @staticmethod
    async def set_setting(key: str, value: str):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Setting).where(Setting.key == key))
            row = result.scalar_one_or_none()
            if row:
                row.value = value
            else:
                session.add(Setting(key=key, value=value))
            await session.commit()

    @staticmethod
    async def get_force_joins() -> List[ForceJoin]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ForceJoin).where(ForceJoin.is_active == True).order_by(ForceJoin.order_num)
            )
            return list(result.scalars().all())

    @staticmethod
    async def log_audit(admin_id: int, action: str, target: str = None, details: str = None):
        async with AsyncSessionLocal() as session:
            log = AuditLog(admin_id=admin_id, action=action, target=target, details=details)
            session.add(log)
            await session.commit()

class UIBuilder:
    @staticmethod
    def get_main_keyboard(session: SearchSession) -> InlineKeyboardMarkup:
        keyboard = []
        
        # Navigation Row
        nav_row = []
        if session.page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ PREVIOUS", callback_data="nav_prev"))
        
        total_pages = max(1, (len(session.results) + 9) // 10)
        nav_row.append(InlineKeyboardButton(f"[{session.page}/{total_pages}]", callback_data="ignore"))
        
        if session.page < total_pages:
            nav_row.append(InlineKeyboardButton("NEXT ➡️️", callback_data="nav_next"))
            
        keyboard.append(nav_row)
        
        # Dynamic Filter Rows
        keyboard.append([
            InlineKeyboardButton("🌐 LANGUAGE", callback_data="filter_menu_lang"),
            InlineKeyboardButton("📅 YEAR", callback_data="filter_menu_year")
        ])
        keyboard.append([
            InlineKeyboardButton("🎞 QUALITY", callback_data="filter_menu_quality"),
            InlineKeyboardButton("📺 SEASON", callback_data="filter_menu_season")
        ])
        keyboard.append([InlineKeyboardButton("🏠 HOME", callback_data="home")])
        
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def generate_result_text(session: SearchSession, user_name: str, paged_results: List[Media], auto_delete_sec: int) -> str:
        text = f"📢 THE RESULTS FOR 👉 \"{session.query}\"\n\n"
        text += f"👤 REQUESTED BY 👉 {user_name}\n"
        text += f"⏱ RESULT SHOW IN 👉 {int(session.exec_time * 1000)} ms\n\n"
        text += f"✨ POWERED BY 👉 Dahiya Movie Bot\n\n"
        text += f"⚠️ THIS MESSAGE WILL AUTO DELETE AFTER {auto_delete_sec} SECONDS 🗑️\n\n"
        text += f"🍿 YOUR RESULTS 👇\n\n"
        
        if not paged_results:
            text += "❌ NO RESULT FOUND matching your active filters."
        else:
            for m in paged_results:
                size_str = f"[{m.size_mb}] " if m.size_mb else ""
                text += f"📗 ❗ {size_str}❗ {m.caption}\n\n"
        return text

    @staticmethod
    def get_clickable_result_buttons(paged_results: List[Media]) -> List[List[InlineKeyboardButton]]:
        buttons = []
        for m in paged_results:
            label = f"📗 {m.caption[:42]}..." if len(m.caption) > 45 else f"📗 {m.caption}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"getfile_{m.id}")])
        return buttons

    @staticmethod
    def get_dynamic_filter_keyboard(filter_type: str, session: SearchSession) -> InlineKeyboardMarkup:
        keyboard = []
        unique_vals: Set[str] = set()
        
        for m in session.results:
            if filter_type == "lang" and m.language:
                unique_vals.add(m.language)
            elif filter_type == "year" and m.year:
                unique_vals.add(str(m.year))
            elif filter_type == "quality" and m.quality:
                unique_vals.add(m.quality)
            elif filter_type == "season" and m.season is not None:
                unique_vals.add(f"Season {m.season}")

        vals_list = sorted(list(unique_vals))
        
        # 2 per row layout
        row = []
        for v in vals_list:
            row.append(InlineKeyboardButton(str(v), callback_data=f"set_{filter_type}_{v}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
            
        all_text_map = {
            "lang": "🌐 ALL LANGUAGES", 
            "year": "📅 ALL YEARS",
            "quality": "🎞 ALL QUALITY", 
            "season": "📺 ALL SEASONS"
        }
        
        keyboard.append([InlineKeyboardButton(all_text_map[filter_type], callback_data=f"set_{filter_type}_ALL")])
        keyboard.append([InlineKeyboardButton("↩️ BACK TO FILES", callback_data="back_to_results")])
        
        return InlineKeyboardMarkup(keyboard)

async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        return True
        
    force_joins = await DB.get_force_joins()
    if not force_joins:
        return True

    not_joined = []
    for fj in force_joins:
        try:
            member = await context.bot.get_chat_member(chat_id=fj.chat_id, user_id=user_id)
            if member.status in [ChatMember.LEFT, ChatMember.BANNED]:
                not_joined.append(fj)
        except Exception as e:
            logger.warning(f"Could not check force join channel {fj.chat_id}: {e}")
            
    if not_joined:
        keyboard = []
        for fj in not_joined:
            keyboard.append([InlineKeyboardButton(fj.button_text or fj.title, url=fj.link)])
        keyboard.append([InlineKeyboardButton("🔄 CHECK AGAIN", callback_data="check_join")])
        
        text = "🔐 **JOIN REQUIRED**\n\nPlease join our required channels to use this bot."
        if update.callback_query:
            await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return False
        
    return True

def apply_filters(session: SearchSession) -> List[Media]:
    filtered = session.results
    if session.filters["lang"]:
        filtered = [m for m in filtered if m.language == session.filters["lang"]]
    if session.filters["year"]:
        filtered = [m for m in filtered if str(m.year) == session.filters["year"]]
    if session.filters["quality"]:
        filtered = [m for m in filtered if m.quality == session.filters["quality"]]
    if session.filters["season"]:
        s_val = int(session.filters["season"].replace("Season ", ""))
        filtered = [m for m in filtered if m.season == s_val]
    return filtered

async def process_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    user = update.effective_user
    if not await check_force_join(update, context):
        return
        
    db_user = await DB.create_or_update_user(user)
    if db_user.is_banned:
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    async with AsyncSessionLocal() as db_session:
        db_user.search_count += 1
        db_session.add(db_user)
        await db_session.commit()

    session = SearchSession(query, user.id)
    ACTIVE_SEARCHES[user.id] = session
    
    results, exec_time, suggestion = await SearchEngine.search(query)
    session.results = results
    session.exec_time = exec_time
    session.suggestion = suggestion
    
    await render_search_page(update, context, session)

async def render_search_page(update: Update, context: ContextTypes.DEFAULT_TYPE, session: SearchSession):
    filtered_results = apply_filters(session)
    
    page_size = 10
    start_idx = (session.page - 1) * page_size
    end_idx = start_idx + page_size
    paged = filtered_results[start_idx:end_idx]
    
    auto_delete_sec = int(await DB.get_setting("auto_delete", "60"))
    text = UIBuilder.generate_result_text(session, update.effective_user.first_name, paged, auto_delete_sec)
    
    if not paged:
        keyboard = []
        if session.suggestion:
            text += f"\n\n❓ Did you mean: **{session.suggestion}**?"
            keyboard.append([InlineKeyboardButton(f"🔎 SEARCH {session.suggestion}", callback_data=f"suggest_{session.suggestion}")])
            
        keyboard.append([InlineKeyboardButton("📥 REQUEST THIS CONTENT", callback_data="request_movie")])
        keyboard.append([InlineKeyboardButton("🏠 HOME", callback_data="home")])
        markup = InlineKeyboardMarkup(keyboard)
    else:
        # Combine clickable media item buttons with navigation controls
        media_buttons = UIBuilder.get_clickable_result_buttons(paged)
        nav_markup = UIBuilder.get_main_keyboard(session)
        combined_keyboard = media_buttons + nav_markup.inline_keyboard
        markup = InlineKeyboardMarkup(combined_keyboard)

    if update.callback_query:
        msg = await update.callback_query.message.edit_text(text, reply_markup=markup)
    else:
        msg = await update.message.reply_text(text, reply_markup=markup)
        
    delete_at = datetime.utcnow() + timedelta(seconds=auto_delete_sec)
    MESSAGES_TO_DELETE.append({"chat_id": msg.chat_id, "msg_id": msg.message_id, "delete_at": delete_at})

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = update.effective_user.id
    
    if data == "home":
        await start_cmd(update, context)
        return
        
    if data == "check_join":
        if await check_force_join(update, context):
            await query.message.edit_text("✅ Thank you for joining!")
            await asyncio.sleep(1.5)
            await start_cmd(update, context)
        return

    if data.startswith("suggest_"):
        sug_query = data.replace("suggest_", "")
        await process_search(update, context, sug_query)
        return

    if data.startswith("getfile_"):
        media_id = int(data.split("_")[1])
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Media).where(Media.id == media_id))
            media_item = res.scalar_one_or_none()
            if media_item:
                try:
                    sent_msg = await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=media_item.file_id,
                        caption=f"✨ **{media_item.caption}**\n\n✨ POWERED BY 👉 Dahiya Movie Bot",
                        parse_mode="Markdown"
                    )
                    auto_del = int(await DB.get_setting("auto_delete", "60"))
                    MESSAGES_TO_DELETE.append({
                        "chat_id": sent_msg.chat_id, 
                        "msg_id": sent_msg.message_id, 
                        "delete_at": datetime.utcnow() + timedelta(seconds=auto_del)
                    })
                except Exception as e:
                    await query.message.reply_text(f"⚠️ Error sending file: {e}")
            else:
                await query.message.reply_text("❌ File no longer exists in database.")
        return

    if data == "request_movie":
        session = ACTIVE_SEARCHES.get(user_id)
        req_title = session.query if session else "Unknown Title"
        user = update.effective_user
        
        async with AsyncSessionLocal() as db:
            new_req = Request(
                user_id=user_id, 
                display_name=user.first_name,
                username=user.username,
                title=req_title
            )
            db.add(new_req)
            await db.commit()
            
        text = f"📥 **REQUEST RECEIVED**\n\n"
        text += f"🎬 **Title:** {req_title}\n"
        text += f"👤 **REQUESTED BY:** {user.first_name}\n"
        text += f"🔗 @{user.username or 'N/A'}\n"
        text += f"🆔 **Telegram ID:** `{user_id}`\n\n"
        text += f"⏳ THIS MESSAGE WILL AUTO DELETE AFTER 60 SECONDS"
        
        msg = await query.message.edit_text(text, parse_mode="Markdown")
        MESSAGES_TO_DELETE.append({
            "chat_id": msg.chat_id, 
            "msg_id": msg.message_id, 
            "delete_at": datetime.utcnow() + timedelta(seconds=60)
        })
        return

    # Search Navigation & Filter Handlers
    session = ACTIVE_SEARCHES.get(user_id)
    if not session:
        await query.message.edit_text("⚠️️ Search session expired. Please search again.")
        return

    if data == "nav_prev" and session.page > 1:
        session.page -= 1
        await render_search_page(update, context, session)
    elif data == "nav_next":
        session.page += 1
        await render_search_page(update, context, session)
    elif data.startswith("filter_menu_"):
        f_type = data.split("_")[2]
        markup = UIBuilder.get_dynamic_filter_keyboard(f_type, session)
        title_map = {"lang": "LANGUAGE", "year": "YEAR", "quality": "QUALITY", "season": "SEASON"}
        await query.message.edit_text(f"🌐 SELECT YOUR {title_map[f_type]}", reply_markup=markup)
    elif data.startswith("set_"):
        parts = data.split("_")
        f_type = parts[1]
        val = parts[2]
        
        if val == "ALL":
            session.filters[f_type] = None
        else:
            session.filters[f_type] = val
        session.page = 1
        await render_search_page(update, context, session)
    elif data == "back_to_results":
        await render_search_page(update, context, session)
    elif data == "admin_panel" and user_id == OWNER_ID:
        await render_admin_dashboard(update, context)
    elif data.startswith("adm_"):
        await handle_admin_callbacks(update, context, data)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await DB.create_or_update_user(user)
    
    if not await check_force_join(update, context):
        return

    text = "🎬 **DAHIYA MOVIE BOT**\n\n🔎 Send me any Movie or Web Series name to search our database."
    keyboard = [
        [InlineKeyboardButton("🔍 SEARCH", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("📥 REQUEST", callback_data="request_movie"), InlineKeyboardButton("ℹ️ HELP", callback_data="help")]
    ]
    if user.id == OWNER_ID:
        keyboard.append([InlineKeyboardButton("👑 OWNER CONTROL CENTER", callback_data="admin_panel")])
        
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "ℹ️ **HELP & INSTRUCTIONS**\n\n"
    text += "1. Type any title to search (e.g. `Raaz`, `Shinchan`, `Daldal S01`).\n"
    text += "2. Use dynamic filters for Language, Quality, Year, and Season.\n"
    text += "3. Results auto-delete after 60 seconds to clean up chat.\n\n"
    text += "✨ POWERED BY 👉 Dahiya Movie Bot"
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 HOME", callback_data="home")]]), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def render_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "👑 **DAHIYA MOVIE BOT**\nOWNER CONTROL CENTER\n\nSelect an option to manage the bot:"
    keyboard = [
        [InlineKeyboardButton("📊 DASHBOARD", callback_data="adm_dashboard"), InlineKeyboardButton("🗄️ DATABASE", callback_data="adm_db")],
        [InlineKeyboardButton("📥 REQUEST CENTER", callback_data="adm_req"), InlineKeyboardButton("🔐 FORCE JOIN", callback_data="adm_fj")],
        [InlineKeyboardButton("👥 USERS", callback_data="adm_users"), InlineKeyboardButton("📢 BROADCAST", callback_data="adm_broad")],
        [InlineKeyboardButton("📈 ANALYTICS", callback_data="adm_stat"), InlineKeyboardButton("⚙️ SETTINGS", callback_data="adm_settings")],
        [InlineKeyboardButton("🛠️ SYSTEM HEALTH", callback_data="adm_health"), InlineKeyboardButton("📋 LOGS", callback_data="adm_logs")],
        [InlineKeyboardButton("🏠 HOME", callback_data="home")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")

async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    if update.effective_user.id != OWNER_ID:
        return
        
    query = update.callback_query
    
    if data == "adm_dashboard" or data == "admin_panel":
        await render_admin_dashboard(update, context)
    elif data == "adm_db":
        async with AsyncSessionLocal() as session:
            count = (await session.execute(select(func.count(Media.id)))).scalar_one()
            sources = (await session.execute(select(func.count(MediaSource.id)))).scalar_one()
            
        text = f"🗄️ **DATABASE MANAGEMENT**\n\nTotal Indexed Items: `{count}`\nActive Sources: `{sources}`"
        keyboard = [
            [InlineKeyboardButton("➕ ADD SOURCE", callback_data="adm_add_src"), InlineKeyboardButton("📋 VIEW SOURCES", callback_data="adm_view_src")],
            [InlineKeyboardButton("↩️ ADMIN PANEL", callback_data="adm_dashboard")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif data == "adm_req":
        async with AsyncSessionLocal() as session:
            reqs = (await session.execute(select(Request).order_by(Request.id.desc()).limit(10))).scalars().all()
            
        text = "📥 **REQUEST CENTER (RECENT 10)**\n\n"
        if not reqs:
            text += "No pending requests."
        else:
            for r in reqs:
                text += f"🆔 #{r.id} | **{r.title}** | Status: `{r.status}`\nUser: {r.display_name} (@{r.username or 'N/A'})\n\n"
        keyboard = [[InlineKeyboardButton("↩️ ADMIN PANEL", callback_data="adm_dashboard")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif data == "adm_fj":
        force_joins = await DB.get_force_joins()
        text = "🔐 **FORCE JOIN CHANNELS**\n\n"
        for fj in force_joins:
            text += f"📢 {fj.title} (`{fj.chat_id}`)\nLink: {fj.link}\n\n"
        keyboard = [
            [InlineKeyboardButton("➕ ADD CHANNEL", callback_data="adm_add_fj")],
            [InlineKeyboardButton("↩️ ADMIN PANEL", callback_data="adm_dashboard")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif data == "adm_stat":
        async with AsyncSessionLocal() as session:
            u_count = (await session.execute(select(func.count(User.id)))).scalar_one()
            m_count = (await session.execute(select(func.count(Media.id)))).scalar_one()
            r_count = (await session.execute(select(func.count(Request.id)))).scalar_one()
            
        text = f"📈 **ANALYTICS & STATS**\n\n👥 Total Users: `{u_count}`\n🎬 Total Indexed Files: `{m_count}`\n📥 Total Requests: `{r_count}`\n\n✨ POWERED BY 👉 Dahiya Movie Bot"
        keyboard = [[InlineKeyboardButton("↩ ADMIN PANEL", callback_data="adm_dashboard")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif data == "adm_health":
        text = "🛠️ **SYSTEM HEALTH**\n\n🟢 BOT: Running\n🟢 DATABASE: Connected\n🟢 SCHEDULER: Active\n🟢 TELEGRAM CONNECTION: Normal"
        keyboard = [[InlineKeyboardButton("↩️ ADMIN PANEL", callback_data="adm_dashboard")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def add_media_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to index media via forward/reply."""
    if update.effective_user.id != OWNER_ID:
        return
        
    msg = update.message.reply_to_message
    if not msg or not (msg.document or msg.video):
        await update.message.reply_text("⚠ Reply to a file/document/video to index it.")
        return
        
    doc = msg.document or msg.video
    caption = msg.caption or getattr(doc, 'file_name', 'Untitled Media')
    meta = MetadataParser.parse(caption)
    
    async with AsyncSessionLocal() as db_session:
        new_media = Media(
            caption=caption,
            file_id=doc.file_id,
            title_norm=meta["title"],
            language=meta["language"],
            year=meta["year"],
            quality=meta["quality"],
            season=meta["season"],
            episode=meta["episode"],
            size_mb=meta["size_mb"],
            added_by=OWNER_ID
        )
        db_session.add(new_media)
        await db_session.commit()
        
    await update.message.reply_text(
        f"✅ **Media Indexed Successfully!**\n\n"
        f"📌 Title: `{meta['title']}`\n"
        f"🌐 Lang: `{meta['language']}` | 📅 Year: `{meta['year']}`\n"
        f"🎞 Quality: `{meta['quality']}` | 📺 Season: `{meta['season']}`",
        parse_mode="Markdown"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.startswith("/"):
        return
    await process_search(update, context, update.message.text)

async def auto_delete_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    to_remove = []
    for item in MESSAGES_TO_DELETE:
        if now >= item["delete_at"]:
            try:
                await context.bot.delete_message(chat_id=item["chat_id"], message_id=item["msg_id"])
            except Exception:
                pass
            to_remove.append(item)
            
    for item in to_remove:
        if item in MESSAGES_TO_DELETE:
            MESSAGES_TO_DELETE.remove(item)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def main():
    logger.info("Initializing Dahiya Movie Bot...")
    
    # Run async database initialization
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("addmedia", add_media_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    # Background Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_delete_job, 'interval', seconds=4, args=[app])
    scheduler.start()
    
    logger.info("Dahiya Movie Bot is online and polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
