import os
import time
import secrets
import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Set

import httpx
import feedparser

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV helpers
# =========================
def env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None else default

def need(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except Exception:
        return default

LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("bot")

# =========================
# REQUIRED
# =========================
BOT_TOKEN = need("BOT_TOKEN")
ADMIN_ID = int(need("ADMIN_ID"))

# =========================
# Language policy: replies only UA/EN (never RU)
# user may write any language
# =========================
DEFAULT_LANG = env("DEFAULT_LANG", "uk")  # uk|en
USER_LANG: Dict[int, str] = {}  # user_id -> "uk" | "en"

def get_lang(user_id: int) -> str:
    base = DEFAULT_LANG if DEFAULT_LANG in ("uk", "en") else "uk"
    return USER_LANG.get(user_id, base)

def t(user_id: int, uk: str, en: str) -> str:
    return uk if get_lang(user_id) == "uk" else en

# =========================
# AI (optional)
# =========================
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
AI_MODEL = env("AI_MODEL", "gpt-5")

_ai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        logger.exception("OpenAI client init failed")
        _ai_client = None

def ai_enabled() -> bool:
    return _ai_client is not None

def ai_instructions(user_id: int, mode: str) -> str:
    base_rules = (
        "HARD RULES:\n"
        "1) Answer ONLY in Ukrainian or English.\n"
        "2) NEVER answer in Russian.\n"
        "3) If user writes in Russian, answer in Ukrainian.\n"
        "4) Do NOT reveal technical details (frequencies, keys, QR, configs, onboarding steps).\n"
        "5) Keep it short, calm, factual.\n"
    )

    if mode == "admin":
        return (
            "You are an admin assistant for emergency access requests.\n"
            + base_rules +
            "Answer in Ukrainian.\n"
            "Format:\n"
            "Рішення: СХВАЛИТИ/ВІДХИЛИТИ\n"
            "Причина: 1 речення\n"
            "Ризик: низький/середній/високий\n"
            "Порада: 1 коротка дія\n"
        )

    # faq
    lang = get_lang(user_id)
    instr = (
        "You are a public FAQ assistant for an emergency communication access bot.\n"
        + base_rules +
        "If asked about access: say access is by request only inside the bot.\n"
        "If asked 'how to connect': say onboarding is provided after verification.\n"
    )
    instr += ("Answer in English." if lang == "en" else "Відповідай українською.")
    return instr

async def ask_ai(user_id: int, text: str, mode: str = "faq") -> str:
    if not ai_enabled():
        logger.warning("AI request ignored: client not configured")
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")
    try:
        resp = await asyncio.to_thread(
            _ai_client.responses.create,
            model=AI_MODEL,
            instructions=ai_instructions(user_id, mode),
            input=text,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out or t(user_id, "ℹ️ Немає відповіді.", "ℹ️ No answer.")
    except Exception:
        logger.exception("AI request failed (user_id=%s mode=%s)", user_id, mode)
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")

# =========================
# Content UA/EN
# =========================
CONTENT = {
    "uk": {
        "company": (
            "🏢 **УКРАВІАКОСТЕХ**\n\n"
            "УКРАВІАКОСТЕХ — інженерна компанія, що розробляє та підтримує автономні та інфраструктурні рішення.\n"
            "Цей бот — офіційний інтерфейс керованого доступу до резервної системи комунікації в екстрених сценаріях.\n\n"
            "🔐 Доступ надається **лише за запитом**."
        ),
        "products": (
            "🧩 **Рішення та продукти (публічно, без технічних деталей)**\n\n"
            "• Резервні автономні системи комунікації для екстрених сценаріїв\n"
            "• Інфраструктурні рішення для координації під час НС\n"
            "• Розгортання, інтеграція та підтримка мереж\n"
            "• Супровід підключення та підтримувані користувацькі комплекти"
        ),
        "system": (
            "📡 **Як працює система (загально)**\n\n"
            "Автономний канал обміну короткими повідомленнями для ситуацій, коли немає світла/інтернету/мобільного звʼязку.\n"
            "Технічні параметри, ключі та інструкції підключення публічно не розкриваються.\n"
            "Підключення — лише після підтвердження."
        ),
        "gear": (
            "📦 **Обладнання**\n\n"
            "Потрібен окремий автономний портативний пристрій із вбудованою батареєю.\n"
            "Телефон — лише для налаштування.\n\n"
            "Поширені варіанти:\n"
            "• ThinkNode M2\n• LILYGO T-Echo\n• Heltec Mesh Node (готовий)"
        ),
        "rules": (
            "📜 **Правила**\n\n"
            "• Лише екстрені/резервні сценарії\n"
            "• Без спаму\n"
            "• Не передавати доступ іншим\n"
            "• Використання лише за призначенням\n"
            "Порушення → відключення."
        ),
        "faq_hint": "💬 **Питання (AI)**\n\nНапишіть питання одним повідомленням.",
        "apply_intro": "🟢 **ЗАПИТ НА ДОСТУП**\n\nДля чого вам доступ? (1 рядок)",
        "ask_device": "📦 Який пристрій? (ThinkNode M2 / T-Echo / Heltec)",
        "confirm": "✅ Підтвердіть правила. Напишіть: **ПІДТВЕРДЖУЮ**",
        "sent": "✅ Заявку передано адміністратору. Очікуйте відповідь тут.",
        "sent_admin_fail": "⚠️ Заявку збережено, але не вдалося доставити адміністратору (адмін може не активував бота). Спробуйте пізніше.",
        "cancel": "❌ Запит скасовано.",
        "menu": "Меню:",
        "lang_saved": "✅ Мову збережено.",
        "choose_lang": "Оберіть мову / Choose language:",
        "cooldown": "⏳ Зачекайте {sec} сек і спробуйте ще раз.",
        "alerts_no_key": "⚠️ Тривоги: ключ не налаштовано.",
        "news_not_cfg": "⚠️ Новини не налаштовано (NEWS_CHANNEL_ID/RSS_FEEDS/KEYWORDS).",
        "no_rights": "⛔️ Недостатньо прав.",
        "already_done": "⚠️ Заявку вже оброблено або не знайдено.",
        "approved_user": "✅ Ваш запит схвалено. Інструкції надійдуть окремо.",
        "denied_user": "❌ Ваш запит відхилено.",
    },
    "en": {
        "company": (
            "🏢 **UkrAviaKosTech**\n\n"
            "Engineering company building autonomous and infrastructure-grade solutions.\n"
            "This bot provides managed access to a reserve emergency communication system.\n"
            "🔐 Access is **by request only**."
        ),
        "products": (
            "🧩 **Solutions (public, no technical details)**\n\n"
            "• Emergency reserve communication\n"
            "• Coordination infrastructure\n"
            "• Deployment, integration and support\n"
            "• Supported user kits and onboarding assistance"
        ),
        "system": (
            "📡 **How it works (high level)**\n\n"
            "An autonomous short-message channel designed for power/internet/mobile outages.\n"
            "Technical parameters and onboarding steps are not published.\n"
            "Access is provided after verification."
        ),
        "gear": (
            "📦 **Equipment**\n\n"
            "Standalone portable device with built-in battery. Phone is only for setup.\n"
            "Common options: ThinkNode M2 / T-Echo / Heltec."
        ),
        "rules": (
            "📜 **Rules**\n\n"
            "• Emergency/reserve scenarios only\n"
            "• No spam\n"
            "• Do not share access\n"
            "• Intended use only\n"
            "Violations → removal."
        ),
        "faq_hint": "💬 **Questions (AI)**\n\nSend one message.",
        "apply_intro": "🟢 **ACCESS REQUEST**\n\nPurpose? (one short line)",
        "ask_device": "📦 Which device? (ThinkNode M2 / T-Echo / Heltec)",
        "confirm": "✅ Confirm rules. Type: **CONFIRM**",
        "sent": "✅ Request sent to admin. Please wait here.",
        "sent_admin_fail": "⚠️ Request saved, but could not be delivered to admin (admin may not have started the bot). Please try later.",
        "cancel": "❌ Request cancelled.",
        "menu": "Menu:",
        "lang_saved": "✅ Language saved.",
        "choose_lang": "Choose language:",
        "cooldown": "⏳ Please wait {sec} seconds.",
        "alerts_no_key": "⚠️ Alerts: API key not configured.",
        "news_not_cfg": "⚠️ News not configured (NEWS_CHANNEL_ID/RSS_FEEDS/KEYWORDS).",
        "no_rights": "⛔️ Not authorized.",
        "already_done": "⚠️ Request already handled or not found.",
        "approved_user": "✅ Your request was approved. Instructions will follow separately.",
        "denied_user": "❌ Your request was denied.",
    }
}

def C(user_id: int, key: str) -> str:
    return CONTENT[get_lang(user_id)][key]

# =========================
# Menu UI
# =========================
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:set:uk"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:back")],
    ])

def menu_kb(user_id: int) -> InlineKeyboardMarkup:
    if get_lang(user_id) == "uk":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Запит на доступ", callback_data="apply:start")],
            [InlineKeyboardButton("🏢 Про компанію", callback_data="info:company"),
             InlineKeyboardButton("🧩 Продукти", callback_data="info:products")],
            [InlineKeyboardButton("📡 Як працює", callback_data="info:system")],
            [InlineKeyboardButton("📦 Обладнання", callback_data="info:gear"),
             InlineKeyboardButton("📜 Правила", callback_data="info:rules")],
            [InlineKeyboardButton("💬 Питання (AI)", callback_data="faq:start")],
            [InlineKeyboardButton("🚨 Тривоги On/Off", callback_data="alerts:toggle")],
            [InlineKeyboardButton("📰 Новини → канал (тест)", callback_data="news:test")],
            [InlineKeyboardButton("🌐 Мова / Language", callback_data="lang:menu")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Access request", callback_data="apply:start")],
        [InlineKeyboardButton("🏢 Company", callback_data="info:company"),
         InlineKeyboardButton("🧩 Products", callback_data="info:products")],
        [InlineKeyboardButton("📡 How it works", callback_data="info:system")],
        [InlineKeyboardButton("📦 Equipment", callback_data="info:gear"),
         InlineKeyboardButton("📜 Rules", callback_data="info:rules")],
        [InlineKeyboardButton("💬 Questions (AI)", callback_data="faq:start")],
        [InlineKeyboardButton("🚨 Alerts On/Off", callback_data="alerts:toggle")],
        [InlineKeyboardButton("📰 News → channel (test)", callback_data="news:test")],
        [InlineKeyboardButton("🌐 Language", callback_data="lang:menu")],
    ])

def admin_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{key}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"admin:deny:{key}"),
    ]])

# =========================
# State / anti-spam
# =========================
COOLDOWN_SEC = 45
AI_COOLDOWN_SEC = 10
_last_apply: Dict[int, float] = {}
_last_ai: Dict[int, float] = {}

# =========================
# Access requests
# =========================
@dataclass
class AccessRequest:
    key: str
    user_id: int
    chat_id: int
    who: str
    purpose: str
    device: str
    ts: float

PENDING: Dict[str, AccessRequest] = {}

def who(u) -> str:
    return f"@{u.username}" if u.username else f"id:{u.id}"

def clip(s: str, n: int = 300) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + "…"

# =========================
# Alerts (official) – configurable via env
# =========================
UA_ALARM_ENABLED = env_bool("UA_ALARM_ENABLED", False)
UA_ALARM_API_KEY = env("UA_ALARM_API_KEY", "")
UA_ALARM_POLL_SEC = env_int("UA_ALARM_POLL_SEC", 15)
UA_ALARM_BASE = env("UA_ALARM_BASE", "https://api.ukrainealarm.com")
UA_ALARM_REGIONS_PATH = env("UA_ALARM_REGIONS_PATH", "/api/v3/regions")
UA_ALARM_ALERT_PATH_TEMPLATE = env("UA_ALARM_ALERT_PATH_TEMPLATE", "/api/v3/alerts/{regionId}")
UA_ALARM_AUTH_HEADER = env("UA_ALARM_AUTH_HEADER", "Authorization")
UA_ALARM_AUTH_PREFIX = env("UA_ALARM_AUTH_PREFIX", "")
UA_ALARM_OBLAST_NAME = env("UA_ALARM_OBLAST_NAME", "Одеська область")

ALERTS_ENABLED: Dict[int, bool] = {}
ALERT_REGION: Dict[int, str] = {}
ALERT_LAST_STATE: Dict[str, bool] = {}
REGION_CACHE: Dict[str, str] = {}

def ua_alarm_enabled() -> bool:
    return UA_ALARM_ENABLED and bool(UA_ALARM_API_KEY)

def ua_headers() -> dict:
    return {UA_ALARM_AUTH_HEADER: f"{UA_ALARM_AUTH_PREFIX}{UA_ALARM_API_KEY}"}

async def ua_get_json(path: str, client: Optional[httpx.AsyncClient] = None):
    url = UA_ALARM_BASE.rstrip("/") + path
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers=ua_headers())
                r.raise_for_status()
                return r.json()
        r = await client.get(url, headers=ua_headers())
        r.raise_for_status()
        return r.json()
    except Exception:
        logger.exception("UA alarm request failed: %s", url)
        raise

async def ua_load_regions(client: Optional[httpx.AsyncClient] = None):
    if REGION_CACHE:
        return
    data = await ua_get_json(UA_ALARM_REGIONS_PATH, client=client)
    items = data if isinstance(data, list) else data.get("regions") or data.get("data") or []

    def norm(s: str) -> str:
        return (s or "").strip().lower()

    target = norm(UA_ALARM_OBLAST_NAME)
    for it in items:
        name = it.get("name") or it.get("title") or ""
        rid = it.get("regionId") or it.get("id") or it.get("region_id") or ""
        if name and rid and norm(name) == target:
            REGION_CACHE["oblast"] = str(rid)
            return

async def ua_region_oblast() -> str:
    await ua_load_regions()
    if "oblast" in REGION_CACHE:
        return REGION_CACHE["oblast"]
    logger.error("UA alarm region not found for oblast=%s", UA_ALARM_OBLAST_NAME)
    raise RuntimeError("Не знайдено regionId області (перевір /regions endpoint та UA_ALARM_OBLAST_NAME).")

def parse_is_alert(data: dict) -> Optional[bool]:
    for k in ("isAlert", "is_alert", "alert", "active"):
        if k in data:
            return bool(data[k])
    if isinstance(data.get("data"), dict):
        for k in ("isAlert", "is_alert", "alert", "active"):
            if k in data["data"]:
                return bool(data["data"][k])
    return None

async def alerts_job(context: ContextTypes.DEFAULT_TYPE):
    if not ua_alarm_enabled():
        return

    subs = [uid for uid, on in ALERTS_ENABLED.items() if on and uid in ALERT_REGION]
    if not subs:
        return

    region_ids = sorted({ALERT_REGION[uid] for uid in subs})

    async with httpx.AsyncClient(timeout=20) as client:
        for rid in region_ids:
            try:
                path = UA_ALARM_ALERT_PATH_TEMPLATE.replace("{regionId}", rid)
                data = await ua_get_json(path, client=client)
                is_alert = parse_is_alert(data if isinstance(data, dict) else {})
                if is_alert is None:
                    continue

                prev = ALERT_LAST_STATE.get(rid)
                if prev is None:
                    ALERT_LAST_STATE[rid] = is_alert
                    continue

                if prev != is_alert:
                    ALERT_LAST_STATE[rid] = is_alert
                    msg_uk = "🔴 ТРИВОГА" if is_alert else "🟢 ВІДБІЙ"
                    msg_en = "🔴 ALERT" if is_alert else "🟢 ALL CLEAR"

                    for uid in subs:
                        if ALERT_REGION.get(uid) == rid:
                            try:
                                await context.bot.send_message(chat_id=uid, text=t(uid, msg_uk, msg_en))
                            except Exception:
                                pass
            except Exception:
                logger.exception("alerts_job error for region %s", rid)
                continue

# =========================
# News -> Channel (urgent only)
# =========================
NEWS_ENABLED = env_bool("NEWS_ENABLED", False)
NEWS_CHANNEL_ID = env("NEWS_CHANNEL_ID", "")
NEWS_POLL_SEC = env_int("NEWS_POLL_SEC", 120)
RSS_FEEDS = [u.strip() for u in env("RSS_FEEDS", "").split(",") if u.strip()]
URGENT_KEYWORDS = [k.strip() for k in env("NEWS_URGENT_KEYWORDS", "").split(",") if k.strip()]

SEEN_MAX = env_int("NEWS_SEEN_MAX", 5000)
_seen_links: Set[str] = set()
_seen_order: deque[str] = deque()

def remember_link(link: str):
    if link in _seen_links:
        return
    _seen_links.add(link)
    _seen_order.append(link)
    while len(_seen_order) > SEEN_MAX:
        old = _seen_order.popleft()
        _seen_links.discard(old)

def news_config_ok() -> bool:
    return NEWS_ENABLED and bool(NEWS_CHANNEL_ID) and RSS_FEEDS and URGENT_KEYWORDS

def urgent_by_keywords(title: str, summary: str) -> bool:
    text = (title + "\n" + summary).lower()
    for kw in URGENT_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(chat_id=NEWS_CHANNEL_ID, text=text, disable_web_page_preview=False)

async def news_job(context: ContextTypes.DEFAULT_TYPE):
    if not news_config_ok():
        return
    async with httpx.AsyncClient(timeout=20) as client:
        for feed_url in RSS_FEEDS:
            try:
                r = await client.get(feed_url, headers={"User-Agent": "TelegramBot/1.0"})
                r.raise_for_status()
                feed = feedparser.parse(r.text)

                for entry in (feed.entries or [])[:10]:
                    title = getattr(entry, "title", "") or ""
                    link = getattr(entry, "link", "") or ""
                    summary = getattr(entry, "summary", "") or ""

                    if not link or link in _seen_links:
                        continue
                    if not urgent_by_keywords(title, summary):
                        continue

                    remember_link(link)

                    short = ""
                    if ai_enabled():
                        try:
                            resp = await asyncio.to_thread(
                                _ai_client.responses.create,
                                model=AI_MODEL,
                                instructions="Стисни до 2 речень українською без паніки. Без вигадок.",
                                input=f"{title}\n{summary}",
                            )
                            short = (getattr(resp, "output_text", "") or "").strip()
                        except Exception:
                            logger.exception("News AI summary failed for feed %s", feed_url)
                            short = ""

                    post = "🚨 ТЕРМІНОВО\n\n" + title + "\n\n"
                    if short:
                        post += "🤖 Коротко:\n" + short + "\n\n"
                    post += "🔗 Джерело: " + link

                    await post_to_channel(context, post)

            except Exception:
                logger.exception("news_job error for feed %s", feed_url)
                continue

# =========================
# Conversation states
# =========================
ASK_PURPOSE, ASK_DEVICE, ASK_CONFIRM, ASK_FAQ = range(4)

# =========================
# Command handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        t(uid, f"👋 Вітаю!\n\n{C(uid,'menu')}", f"👋 Hello!\n\n{C(uid,'menu')}"),
        reply_markup=menu_kb(uid),
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(C(uid, "cancel"))
    return ConversationHandler.END

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        f"OK\n"
        f"AI={'on' if ai_enabled() else 'off'}\n"
        f"NEWS={'on' if news_config_ok() else 'off'}\n"
        f"ALERTS={'on' if ua_alarm_enabled() else 'off'}"
    )
    await update.message.reply_text(txt)

async def test_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(t(uid, "⛔ Тільки адмін.", "⛔ Admin only."))
        return
    if not NEWS_CHANNEL_ID:
        await update.message.reply_text(t(uid, "NEWS_CHANNEL_ID не задан.", "NEWS_CHANNEL_ID is not set."))
        return
    try:
        await context.bot.send_message(chat_id=NEWS_CHANNEL_ID, text="✅ TEST: бот може писати в канал.")
        await update.message.reply_text(t(uid, "✅ Тестове повідомлення відправлено в канал.", "✅ Test message sent to channel."))
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

# =========================
# Menu callback handler (only menu/info/toggles)
# =========================
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "lang:menu":
        await q.message.reply_text(C(uid, "choose_lang"), reply_markup=lang_kb())
        return

    if data.startswith("lang:set:"):
        _, _, lng = data.split(":")
        USER_LANG[uid] = "en" if lng == "en" else "uk"
        await q.message.reply_text(C(uid, "lang_saved"))
        await q.message.reply_text(C(uid, "menu"), reply_markup=menu_kb(uid))
        return

    if data == "menu:back":
        await q.message.reply_text(C(uid, "menu"), reply_markup=menu_kb(uid))
        return

    if data == "info:company":
        await q.message.reply_text(C(uid, "company"), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "info:products":
        await q.message.reply_text(C(uid, "products"), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "info:system":
        await q.message.reply_text(C(uid, "system"), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "info:gear":
        await q.message.reply_text(C(uid, "gear"), parse_mode=ParseMode.MARKDOWN)
        return
    if data == "info:rules":
        await q.message.reply_text(C(uid, "rules"), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "alerts:toggle":
        if not ua_alarm_enabled():
            await q.message.reply_text(C(uid, "alerts_no_key"))
            return

        on = ALERTS_ENABLED.get(uid, False)
        if on:
            ALERTS_ENABLED[uid] = False
            await q.message.reply_text(t(uid, "✅ Тривоги вимкнено.", "✅ Alerts disabled."))
        else:
            try:
                rid = await ua_region_oblast()
                ALERT_REGION[uid] = rid
                ALERTS_ENABLED[uid] = True
                await q.message.reply_text(
                    t(uid, f"✅ Тривоги увімкнено ({UA_ALARM_OBLAST_NAME}).", f"✅ Alerts enabled ({UA_ALARM_OBLAST_NAME}).")
                )
            except Exception:
                await q.message.reply_text(
                    t(uid, "⚠️ Не вдалося увімкнути тривоги (помилка конфігурації/API).",
                       "⚠️ Could not enable alerts (config/API error).")
                )
        return

    if data == "news:test":
        if not news_config_ok():
            await q.message.reply_text(C(uid, "news_not_cfg"))
            return
        await q.message.reply_text(
            t(uid, "✅ News job активний. Чекайте публікацій у каналі.", "✅ News job active. Watch the channel.")
        )
        return

# =========================
# Apply conversation handlers
# =========================
async def apply_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    now = time.time()

    last = _last_apply.get(uid, 0.0)
    if now - last < COOLDOWN_SEC:
        remain = int(max(1, COOLDOWN_SEC - (now - last)))
        await q.message.reply_text(C(uid, "cooldown").format(sec=remain))
        return ConversationHandler.END

    _last_apply[uid] = now
    await q.message.reply_text(C(uid, "apply_intro"), parse_mode=ParseMode.MARKDOWN)
    return ASK_PURPOSE

async def apply_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    purpose = (update.message.text or "").strip()
    if not purpose:
        await update.message.reply_text(t(uid, "Напишіть мету одним рядком.", "Please write purpose in one short line."))
        return ASK_PURPOSE

    context.user_data["purpose"] = clip(purpose, 300)
    await update.message.reply_text(C(uid, "ask_device"))
    return ASK_DEVICE

async def apply_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    device = (update.message.text or "").strip()
    if not device:
        await update.message.reply_text(t(uid, "Вкажіть пристрій текстом.", "Please specify the device as text."))
        return ASK_DEVICE

    context.user_data["device"] = clip(device, 200)
    word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    msg = C(uid, "confirm").replace("ПІДТВЕРДЖУЮ", word).replace("CONFIRM", word)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return ASK_CONFIRM

async def apply_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip().upper()
    word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    if txt != word:
        await update.message.reply_text(C(uid, "cancel"))
        return ConversationHandler.END

    u = update.effective_user
    key = secrets.token_hex(8)
    req = AccessRequest(
        key=key,
        user_id=u.id,
        chat_id=update.effective_chat.id,
        who=who(u),
        purpose=context.user_data.get("purpose", ""),
        device=context.user_data.get("device", ""),
        ts=time.time(),
    )
    PENDING[key] = req

    reco = "AI: (disabled)"
    if ai_enabled():
        reco = await ask_ai(
            ADMIN_ID,
            f"Користувач: {req.who}\nМета: {req.purpose}\nПристрій: {req.device}",
            mode="admin",
        )

    # ВАЖНО: без Markdown/HTML, чтобы не ломалось от пользовательского ввода
    admin_text = (
        "🆕 ЗАЯВКА\n\n"
        f"👤 {req.who}\n"
        f"🎯 {req.purpose}\n"
        f"📦 {req.device}\n\n"
        f"🤖 AI\n{reco}\n\n"
        f"ID: {req.user_id}"
    )

    delivered = True
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_kb(key))
    except Exception:
        delivered = False

    await update.message.reply_text(C(uid, "sent") if delivered else C(uid, "sent_admin_fail"))
    return ConversationHandler.END

# =========================
# FAQ conversation handlers
# =========================
async def faq_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    now = time.time()

    last = _last_ai.get(uid, 0.0)
    if now - last < AI_COOLDOWN_SEC:
        remain = int(max(1, AI_COOLDOWN_SEC - (now - last)))
        await q.message.reply_text(C(uid, "cooldown").format(sec=remain))
        return ConversationHandler.END

    _last_ai[uid] = now
    await q.message.reply_text(C(uid, "faq_hint"), parse_mode=ParseMode.MARKDOWN)
    return ASK_FAQ

async def faq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text(t(uid, "Напишіть питання текстом.", "Please send a text question."))
        return ASK_FAQ
    ans = await ask_ai(uid, q, mode="faq")
    await update.message.reply_text(ans)
    return ConversationHandler.END

# =========================
# Admin callbacks
# =========================
async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.from_user.id != ADMIN_ID:
        uid = q.from_user.id
        await q.message.reply_text(t(uid, "⛔ Тільки адмін.", "⛔ Admin only."))
        return

    _, action, key = q.data.split(":", 2)
    req = PENDING.pop(key, None)
    if not req:
        await q.message.reply_text("ℹ️ Already processed / request not found.")
        return

    if action == "approve":
        await context.bot.send_message(
            chat_id=req.chat_id,
            text=t(
                req.user_id,
                "✅ Ваш запит **схвалено**. Інструкції/доступ надасть адміністратор.",
                "✅ Your request is **approved**. The admin will provide onboarding/access.",
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await q.message.reply_text(f"✅ Approved: {req.who}")
    else:
        await context.bot.send_message(
            chat_id=req.chat_id,
            text=t(
                req.user_id,
                "❌ Ваш запит **відхилено**. Спробуйте пізніше.",
                "❌ Your request is **denied**. Please try later.",
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await q.message.reply_text(f"❌ Denied: {req.who}")

# =========================
# Error handler (logs exceptions)
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        err = context.error
        logger.error("Unhandled error: %r", err)
    except Exception:
        pass

# =========================
# post_init: schedule jobs safely (PTB v21+)
# =========================
async def post_init(application):
    if news_config_ok():
        application.job_queue.run_repeating(news_job, interval=NEWS_POLL_SEC, first=15)
    if ua_alarm_enabled():
        application.job_queue.run_repeating(alerts_job, interval=UA_ALARM_POLL_SEC, first=5)

# =========================
# main
# =========================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("test_channel", test_channel_cmd))

    # menu/info callbacks only
    app.add_handler(CallbackQueryHandler(
        menu_cb,
        pattern=r"^(lang:menu|lang:set:(uk|en)|menu:back|info:company|info:products|info:system|info:gear|info:rules|alerts:toggle|news:test)$"
    ))

    # admin callbacks
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:(approve|deny):"))

    # apply conversation (entry is button only)
    apply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(apply_start_cb, pattern=r"^apply:start$")],
        states={
            ASK_PURPOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_purpose)],
            ASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_device)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        # per_message=False (default): важно, потому что после callback идут обычные сообщения
    )

    # faq conversation (entry is button only)
    faq_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(faq_start_cb, pattern=r"^faq:start$")],
        states={
            ASK_FAQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, faq_answer)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        # per_message=False (default)
    )

    app.add_handler(apply_conv)
    app.add_handler(faq_conv)

    app.add_error_handler(error_handler)

    # IMPORTANT: if you changed token or had conflicts, this helps after restart
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
