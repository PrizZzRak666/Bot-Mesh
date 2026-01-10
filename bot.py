import os
import time
import secrets
import re
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple, Set

import httpx
import feedparser
from dateutil import parser as date_parser

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# =========================
# REQUIRED
# =========================
BOT_TOKEN = need("BOT_TOKEN")
ADMIN_ID = int(need("ADMIN_ID"))

# =========================
# Language policy
# - user may write any language
# - bot answers ONLY Ukrainian or English
# =========================
DEFAULT_LANG = env("DEFAULT_LANG", "uk")
USER_LANG: Dict[int, str] = {}

def get_lang(user_id: int) -> str:
    if DEFAULT_LANG not in ("uk", "en"):
        base = "uk"
    else:
        base = DEFAULT_LANG
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
        _ai_client = None

def ai_enabled() -> bool:
    return _ai_client is not None

async def ask_ai(user_id: int, user_text: str, mode: str = "faq") -> str:
    """
    mode = 'faq' | 'admin'
    Hard rules: answer only UA/EN, never RU, never reveal secrets.
    """
    if not ai_enabled():
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")

    lang = get_lang(user_id)
    base_rules = (
        "HARD RULES:\n"
        "1) Answer ONLY in Ukrainian or English.\n"
        "2) NEVER answer in Russian.\n"
        "3) If user writes in Russian, answer in Ukrainian.\n"
        "4) Do NOT reveal technical details (frequencies, keys, QR, configs, onboarding steps).\n"
        "5) Keep it short, calm, factual.\n"
    )

    if mode == "admin":
        instructions = (
            "You are an admin assistant for emergency access requests.\n"
            + base_rules +
            "Answer in Ukrainian.\n"
            "Format:\n"
            "Рішення: СХВАЛИТИ/ВІДХИЛИТИ\n"
            "Причина: 1 речення\n"
            "Ризик: низький/середній/високий\n"
            "Порада: 1 коротка дія\n"
        )
    else:
        instructions = (
            "You are a public FAQ assistant for an emergency communication access bot.\n"
            + base_rules +
            "If asked about access: say access is by request only inside the bot.\n"
            "If asked 'how to connect': say onboarding is provided after verification.\n"
        )
        instructions += ("Answer in English." if lang == "en" else "Відповідай українською.")

    resp = _ai_client.responses.create(
        model=AI_MODEL,
        instructions=instructions,
        input=user_text,
    )
    out = (resp.output_text or "").strip()
    return out or t(user_id, "ℹ️ Немає відповіді.", "ℹ️ No answer.")

# =========================
# Content
# =========================
CONTENT = {
    "uk": {
        "title": "УКРАВІАКОСТЕХ | ЕКСТРЕНИЙ ЗВʼЯЗОК",
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
            "• Супровід підключення та підтримувані користувацькі комплекти\n\n"
            "Публічні кейси/опис — за запитом після підтвердження."
        ),
        "system": (
            "📡 **Як працює система (загально)**\n\n"
            "Це автономна система обміну короткими повідомленнями, яка може працювати, коли:\n"
            "• немає світла\n"
            "• немає інтернету\n"
            "• немає мобільного звʼязку\n\n"
            "🔒 Ми **не публікуємо** технічні параметри, ключі чи інструкції підключення у відкритому доступі.\n"
            "Інструкції надаються **після підтвердження**."
        ),
        "gear": (
            "📦 **Обладнання**\n\n"
            "Потрібен окремий автономний портативний пристрій із вбудованою батареєю.\n"
            "Телефон використовується лише для налаштування.\n\n"
            "Поширені варіанти:\n"
            "• ThinkNode M2\n"
            "• LILYGO T-Echo\n"
            "• Heltec Mesh Node (готовий)\n"
        ),
        "rules": (
            "📜 **Правила**\n\n"
            "• Мережа призначена для екстрених/резервних ситуацій\n"
            "• Спам/флуд заборонено\n"
            "• Заборонено передавати доступ іншим\n"
            "• Використання лише за призначенням\n\n"
            "Порушення → відключення."
        ),
        "help": (
            "ℹ️ **Допомога**\n\n"
            "1) Натисніть «Подати запит»\n"
            "2) Вкажіть мету\n"
            "3) Вкажіть пристрій\n"
            "4) Підтвердіть правила\n\n"
            "Після цього адміністратор отримає заявку."
        ),
        "faq_hint": "💬 **Питання (AI)**\n\nНапишіть питання одним повідомленням. Відповім коротко (без технічних деталей).",
        "menu": "Меню:",
        "choose_lang": "Оберіть мову / Choose language:",
        "lang_saved": "✅ Мову збережено.",
        "cooldown": "⏳ Зачекайте {sec} сек і спробуйте ще раз.",
        "apply_intro": "🟢 **ЗАПИТ НА ДОСТУП**\n\nДля чого вам доступ до мережі? Напишіть коротко (1 рядок).",
        "ask_device": "📦 Який пристрій ви плануєте використовувати? (ThinkNode M2 / T-Echo / Heltec)",
        "confirm": "✅ Підтвердіть правила. Напишіть: **ПІДТВЕРДЖУЮ**",
        "cancelled": "❌ Запит скасовано.",
        "sent_to_admin": "✅ Дякуємо! Заявку передано адміністратору. Очікуйте відповідь у цьому чаті.",
        "approved_user": "✅ Ваш запит **схвалено**. Інструкції/доступ буде надано адміністратором окремо.",
        "denied_user": "❌ Ваш запит **відхилено**. Ви можете подати запит повторно пізніше.",
        "no_rights": "⛔ Недостатньо прав.",
        "already_done": "⚠️ Заявка вже оброблена або не знайдена.",
        "alerts_no_key": "⚠️ Тривоги: ключ не налаштовано.",
        "alerts_on": "✅ Сповіщення про тривоги увімкнено.",
        "alerts_off": "✅ Сповіщення про тривоги вимкнено.",
        "alerts_choose_region": "Оберіть регіон для сповіщень:",
        "alerts_set_oblast": "✅ Регіон встановлено: Одеська область.",
        "alerts_set_city": "✅ Регіон встановлено: Одеса (місто).",
        "news_not_config": "⚠️ Новини не налаштовано (NEWS_CHANNEL_ID/RSS_FEEDS).",
        "news_enabled": "✅ Срочні новини: увімкнено.",
        "news_disabled": "✅ Срочні новини: вимкнено.",
    },
    "en": {
        "title": "UKRAVIAKOSTECH | EMERGENCY LINK",
        "company": (
            "🏢 **UkrAviaKosTech**\n\n"
            "UkrAviaKosTech is an engineering company developing autonomous and infrastructure-grade solutions.\n"
            "This bot is the official interface for managed access to a reserve communication system for emergency scenarios.\n\n"
            "🔐 Access is provided **by request only**."
        ),
        "products": (
            "🧩 **Solutions & products (public, no technical details)**\n\n"
            "• Emergency reserve communication solutions\n"
            "• Coordination infrastructure for crisis scenarios\n"
            "• Network deployment, integration and support\n"
            "• Supported user kits and onboarding assistance\n\n"
            "Public overview can be provided after verification."
        ),
        "system": (
            "📡 **How it works (high level)**\n\n"
            "An autonomous short-message communication system designed to work when:\n"
            "• power is down\n"
            "• internet is down\n"
            "• mobile networks are unavailable\n\n"
            "🔒 We do **not** publish technical parameters, keys or onboarding instructions publicly.\n"
            "Onboarding is provided **after verification**."
        ),
        "gear": (
            "📦 **Equipment**\n\n"
            "You need a standalone portable device with a built-in battery.\n"
            "A phone is used only for setup.\n\n"
            "Common supported options:\n"
            "• ThinkNode M2\n"
            "• LILYGO T-Echo\n"
            "• Heltec Mesh Node (ready unit)\n"
        ),
        "rules": (
            "📜 **Rules**\n\n"
            "• Intended for emergency/reserve scenarios\n"
            "• No spam/flood\n"
            "• Do not share access with others\n"
            "• Use for intended purpose only\n\n"
            "Violations → removal."
        ),
        "help": (
            "ℹ️ **Help**\n\n"
            "1) Tap “Request access”\n"
            "2) Provide purpose\n"
            "3) Provide device\n"
            "4) Confirm rules\n\n"
            "Admin will receive your request."
        ),
        "faq_hint": "💬 **Questions (AI)**\n\nSend your question in one message. I will reply briefly (no technical details).",
        "menu": "Menu:",
        "choose_lang": "Choose language:",
        "lang_saved": "✅ Language saved.",
        "cooldown": "⏳ Please wait {sec} seconds and try again.",
        "apply_intro": "🟢 **ACCESS REQUEST**\n\nWhat do you need access for? Write a short one-liner.",
        "ask_device": "📦 Which device will you use? (ThinkNode M2 / T-Echo / Heltec)",
        "confirm": "✅ Confirm rules. Type: **CONFIRM**",
        "cancelled": "❌ Request cancelled.",
        "sent_to_admin": "✅ Thanks! Your request was sent to the admin. Please wait for a response here.",
        "approved_user": "✅ Your request is **approved**. Onboarding/access will be provided by admin separately.",
        "denied_user": "❌ Your request is **denied**. You may apply again later.",
        "no_rights": "⛔ Not enough permissions.",
        "already_done": "⚠️ Request already processed or not found.",
        "alerts_no_key": "⚠️ Alerts: API key not configured.",
        "alerts_on": "✅ Alerts enabled.",
        "alerts_off": "✅ Alerts disabled.",
        "alerts_choose_region": "Choose a region for alerts:",
        "alerts_set_oblast": "✅ Region set: Odesa oblast.",
        "alerts_set_city": "✅ Region set: Odesa city.",
        "news_not_config": "⚠️ News not configured (NEWS_CHANNEL_ID/RSS_FEEDS).",
        "news_enabled": "✅ Urgent news: enabled.",
        "news_disabled": "✅ Urgent news: disabled.",
    }
}

def C(user_id: int, key: str) -> str:
    return CONTENT[get_lang(user_id)][key]

# =========================
# Buttons / UI
# =========================
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:set:uk"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:back")],
    ])

def menu_kb(user_id: int) -> InlineKeyboardMarkup:
    if get_lang(user_id) == "uk":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Подати запит на доступ", callback_data="apply:start")],
            [InlineKeyboardButton("🏢 Про компанію", callback_data="info:company"),
             InlineKeyboardButton("🧩 Продукти", callback_data="info:products")],
            [InlineKeyboardButton("📡 Як працює система", callback_data="info:system")],
            [InlineKeyboardButton("📦 Обладнання", callback_data="info:gear"),
             InlineKeyboardButton("📜 Правила", callback_data="info:rules")],
            [InlineKeyboardButton("💬 Питання (AI)", callback_data="faq:start")],
            [InlineKeyboardButton("🚨 Тривоги: On/Off", callback_data="alerts:toggle"),
             InlineKeyboardButton("📍 Регіон", callback_data="alerts:region")],
            [InlineKeyboardButton("📰 Срочні новини: On/Off", callback_data="news:toggle")],
            [InlineKeyboardButton("🌐 Мова / Language", callback_data="lang:menu")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Request access", callback_data="apply:start")],
        [InlineKeyboardButton("🏢 Company", callback_data="info:company"),
         InlineKeyboardButton("🧩 Products", callback_data="info:products")],
        [InlineKeyboardButton("📡 How it works", callback_data="info:system")],
        [InlineKeyboardButton("📦 Equipment", callback_data="info:gear"),
         InlineKeyboardButton("📜 Rules", callback_data="info:rules")],
        [InlineKeyboardButton("💬 Questions (AI)", callback_data="faq:start")],
        [InlineKeyboardButton("🚨 Alerts: On/Off", callback_data="alerts:toggle"),
         InlineKeyboardButton("📍 Region", callback_data="alerts:region")],
        [InlineKeyboardButton("📰 Urgent news: On/Off", callback_data="news:toggle")],
        [InlineKeyboardButton("🌐 Language", callback_data="lang:menu")],
    ])

def admin_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{key}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"admin:deny:{key}"),
    ]])

def who(u) -> str:
    return f"@{u.username}" if u.username else f"id:{u.id}"

# =========================
# Anti-spam
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

# =========================
# Alerts (official key) - minimal implementation
# IMPORTANT: confirm official endpoints in your email/docs
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
UA_ALARM_CITY_NAME = env("UA_ALARM_CITY_NAME", "Одеса")

ALERTS_ENABLED: Dict[int, bool] = {}
ALERT_REGION: Dict[int, str] = {}
ALERT_LAST_STATE: Dict[str, bool] = {}
REGION_CACHE: Dict[str, str] = {}

def ua_alarm_enabled() -> bool:
    return UA_ALARM_ENABLED and bool(UA_ALARM_API_KEY)

def ua_headers() -> dict:
    return {UA_ALARM_AUTH_HEADER: f"{UA_ALARM_AUTH_PREFIX}{UA_ALARM_API_KEY}"}

async def ua_get_json(path: str):
    url = UA_ALARM_BASE.rstrip("/") + path
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=ua_headers())
        r.raise_for_status()
        return r.json()

async def ua_load_regions() -> None:
    if REGION_CACHE:
        return
    data = await ua_get_json(UA_ALARM_REGIONS_PATH)
    items = data if isinstance(data, list) else data.get("regions") or data.get("data") or []

    def norm(s: str) -> str:
        return (s or "").strip().lower()

    for it in items:
        name = it.get("name") or it.get("title") or ""
        rid = it.get("regionId") or it.get("id") or it.get("region_id") or ""
        if not (name and rid):
            continue
        n = norm(name)
        if n == norm(UA_ALARM_OBLAST_NAME):
            REGION_CACHE["oblast"] = str(rid)
        if "одес" in n and (("місто" in n) or ("м." in n) or (n == norm(UA_ALARM_CITY_NAME))):
            REGION_CACHE["city"] = str(rid)

async def ua_region_oblast() -> str:
    await ua_load_regions()
    if "oblast" in REGION_CACHE:
        return REGION_CACHE["oblast"]
    raise RuntimeError("Не знайдено regionId для області (перевір /regions).")

async def ua_region_city() -> Optional[str]:
    await ua_load_regions()
    return REGION_CACHE.get("city")

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

    for rid in region_ids:
        try:
            path = UA_ALARM_ALERT_PATH_TEMPLATE.replace("{regionId}", rid)
            data = await ua_get_json(path)
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
            continue

# =========================
# News -> Channel (urgent only)
# =========================
NEWS_ENABLED = env_bool("NEWS_ENABLED", False)
NEWS_CHANNEL_ID = env("NEWS_CHANNEL_ID", "")
NEWS_POLL_SEC = env_int("NEWS_POLL_SEC", 120)
RSS_FEEDS = [u.strip() for u in env("RSS_FEEDS", "").split(",") if u.strip()]
URGENT_KEYWORDS = [k.strip() for k in env("NEWS_URGENT_KEYWORDS", "").split(",") if k.strip()]

_seen_links: Set[str] = set()

def news_config_ok() -> bool:
    return NEWS_ENABLED and bool(NEWS_CHANNEL_ID) and len(RSS_FEEDS) > 0 and len(URGENT_KEYWORDS) > 0

def urgent_by_keywords(title: str, summary: str) -> bool:
    text = f"{title}\n{summary}".lower()
    for kw in URGENT_KEYWORDS:
        if kw.lower() and kw.lower() in text:
            return True
    return False

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(chat_id=NEWS_CHANNEL_ID, text=text, disable_web_page_preview=False)

async def news_job(context: ContextTypes.DEFAULT_TYPE):
    if not news_config_ok():
        return
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in (feed.entries or [])[:10]:
                title = getattr(entry, "title", "") or ""
                link = getattr(entry, "link", "") or ""
                summary = getattr(entry, "summary", "") or ""
                if not link or link in _seen_links:
                    continue

                if not urgent_by_keywords(title, summary):
                    continue

                # optional AI verification (strict)
                if ai_enabled():
                    verdict = _ai_client.responses.create(
                        model=AI_MODEL,
                        instructions="Return ONLY one token: URGENT or NOT_URGENT. No extra text.",
                        input=f"TITLE: {title}\nTEXT: {summary}",
                    ).output_text.strip()
                    if not verdict.upper().startswith("URGENT"):
                        _seen_links.add(link)
                        continue

                _seen_links.add(link)

                short = ""
                if ai_enabled():
                    short = _ai_client.responses.create(
                        model=AI_MODEL,
                        instructions=(
                            "Стисни новину до 2 коротких речень українською без паніки. "
                            "Не вигадуй фактів. Якщо даних мало — скажи 'деталі уточнюються'."
                        ),
                        input=f"{title}\n{summary}",
                    ).output_text.strip()

                post = "🚨 ТЕРМІНОВО\n\n" + title + "\n\n"
                if short:
                    post += "🤖 Коротко:\n" + short + "\n\n"
                post += "🔗 Джерело: " + link

                await post_to_channel(context, post)
        except Exception:
            continue

# =========================
# Conversation states
# =========================
ASK_PURPOSE, ASK_DEVICE, ASK_CONFIRM, ASK_FAQ = range(4)

# =========================
# Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        t(uid,
          f"👋 Вітаю!\n\nЦе офіційний бот доступу до мережі екстреного звʼязку **УКРАВІАКОСТЕХ**.\n\n{C(uid,'menu')}",
          f"👋 Hello!\n\nThis is the official access bot for **UkrAviaKosTech** emergency communications.\n\n{C(uid,'menu')}"),
        parse_mode="Markdown",
        reply_markup=menu_kb(uid),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(C(uid, "help"), parse_mode="Markdown")

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(C(uid, "rules"), parse_mode="Markdown")

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await q.message.reply_text(C(uid, "company"), parse_mode="Markdown")
        return
    if data == "info:products":
        await q.message.reply_text(C(uid, "products"), parse_mode="Markdown")
        return
    if data == "info:system":
        await q.message.reply_text(C(uid, "system"), parse_mode="Markdown")
        return
    if data == "info:gear":
        await q.message.reply_text(C(uid, "gear"), parse_mode="Markdown")
        return
    if data == "info:rules":
        await q.message.reply_text(C(uid, "rules"), parse_mode="Markdown")
        return

    # FAQ start
    if data == "faq:start":
        now = time.time()
        last = _last_ai.get(uid, 0)
        if now - last < AI_COOLDOWN_SEC:
            await q.message.reply_text(C(uid, "cooldown").format(sec=int(AI_COOLDOWN_SEC - (now - last))))
            return
        _last_ai[uid] = now
        await q.message.reply_text(C(uid, "faq_hint"), parse_mode="Markdown")
        return ASK_FAQ

    # Apply start (conversation)
    if data == "apply:start":
        now = time.time()
        last = _last_apply.get(uid, 0)
        if now - last < COOLDOWN_SEC:
            await q.message.reply_text(C(uid, "cooldown").format(sec=int(COOLDOWN_SEC - (now - last))))
            return
        _last_apply[uid] = now
        await q.message.reply_text(C(uid, "apply_intro"), parse_mode="Markdown")
        return ASK_PURPOSE

    # Alerts toggle
    if data == "alerts:toggle":
        if not ua_alarm_enabled():
            await q.message.reply_text(C(uid, "alerts_no_key"))
            return
        on = ALERTS_ENABLED.get(uid, False)
        if on:
            ALERTS_ENABLED[uid] = False
            await q.message.reply_text(C(uid, "alerts_off"))
        else:
            rid = await ua_region_oblast()
            ALERT_REGION[uid] = rid
            ALERTS_ENABLED[uid] = True
            await q.message.reply_text(C(uid, "alerts_on"))
        return

    # Alerts region pick
    if data == "alerts:region":
        if not ua_alarm_enabled():
            await q.message.reply_text(C(uid, "alerts_no_key"))
            return
        await ua_load_regions()
        buttons = [[InlineKeyboardButton(t(uid, "Одеська область", "Odesa oblast"), callback_data="areg:set:oblast")]]
        if await ua_region_city():
            buttons.append([InlineKeyboardButton(t(uid, "Одеса (місто)", "Odesa city"), callback_data="areg:set:city")])
        await q.message.reply_text(C(uid, "alerts_choose_region"), reply_markup=InlineKeyboardMarkup(buttons))
        return

    # News toggle informational only (posting is global via env)
    if data == "news:toggle":
        if not news_config_ok():
            await q.message.reply_text(C(uid, "news_not_config"))
            return
        await q.message.reply_text(C(uid, "news_enabled") if get_lang(uid) == "uk" else C(uid, "news_enabled"))
        return

async def alerts_region_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not ua_alarm_enabled():
        await q.message.reply_text(C(uid, "alerts_no_key"))
        return
    await ua_load_regions()
    if q.data == "areg:set:oblast":
        ALERT_REGION[uid] = await ua_region_oblast()
        await q.message.reply_text(C(uid, "alerts_set_oblast"))
    elif q.data == "areg:set:city":
        city = await ua_region_city()
        if city:
            ALERT_REGION[uid] = city
            await q.message.reply_text(C(uid, "alerts_set_city"))

# ---- Apply conversation
async def ask_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data["purpose"] = (update.message.text or "").strip()
    await update.message.reply_text(C(uid, "ask_device"))
    return ASK_DEVICE

async def ask_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data["device"] = (update.message.text or "").strip()
    confirm_word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    msg = C(uid, "confirm").replace("ПІДТВЕРДЖУЮ", confirm_word).replace("CONFIRM", confirm_word)
    await update.message.reply_text(msg, parse_mode="Markdown")
    return ASK_CONFIRM

async def ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip().upper()
    confirm_word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    if txt != confirm_word:
        await update.message.reply_text(C(uid, "cancelled"))
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
        reco = await ask_ai(ADMIN_ID, f"Користувач: {req.who}\nМета: {req.purpose}\nПристрій: {req.device}", mode="admin")

    admin_text = (
        "🆕 **ЗАЯВКА НА ДОСТУП**\n\n"
        f"👤 {req.who}\n"
        f"🎯 Мета: {req.purpose}\n"
        f"📦 Пристрій: {req.device}\n\n"
        f"🤖 **AI**\n{reco}\n\n"
        f"ID: `{req.user_id}`"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_kb(key), parse_mode="Markdown")
    except Exception:
        # most common: admin never pressed /start
        pass

    await update.message.reply_text(C(uid, "sent_to_admin"))
    return ConversationHandler.END

# ---- FAQ conversation
async def faq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text(t(uid, "Напишіть питання текстом.", "Please send a text question."))
        return ASK_FAQ
    ans = await ask_ai(uid, q, mode="faq") if ai_enabled() else t(uid, "AI_toggle: вимкнено.", "AI is disabled.")
    await update.message.reply_text(ans)
    return ConversationHandler.END

# ---- Admin
async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.message.reply_text(C(ADMIN_ID, "no_rights"))
        return

    _, action, key = q.data.split(":", 2)
    req = PENDING.pop(key, None)
    if not req:
        await q.message.reply_text(C(ADMIN_ID, "already_done"))
        return

    if action == "approve":
        await context.bot.send_message(chat_id=req.chat_id, text=C(req.user_id, "approved_user"), parse_mode="Markdown")
        await q.message.reply_text(f"✅ Approved: {req.who}")
        return

    if action == "deny":
        await context.bot.send_message(chat_id=req.chat_id, text=C(req.user_id, "denied_user"), parse_mode="Markdown")
        await q.message.reply_text(f"❌ Denied: {req.who}")
        return

# =========================
# MAIN
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))

    # Menu callbacks (apply & faq are also conversation entry points; safe here because we route by pattern)
    app.add_handler(CallbackQueryHandler(
        menu_handler,
        pattern=r"^(info:company|info:products|info:system|info:gear|info:rules|faq:start|apply:start|alerts:toggle|alerts:region|news:toggle|lang:menu|lang:set:(uk|en)|menu:back)$"
    ))
    app.add_handler(CallbackQueryHandler(alerts_region_cb, pattern=r"^areg:set:(oblast|city)$"))

    # Admin callbacks
    app.add_handler(CallbackQueryHandler(admin_handler, pattern=r"^admin:(approve|deny):"))

    # Conversations
    apply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler, pattern=r"^apply:start$")],
        states={
            ASK_PURPOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_purpose)],
            ASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_device)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_confirm)],
        },
        fallbacks=[],
    )
    faq_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler, pattern=r"^faq:start$")],
        states={
            ASK_FAQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, faq_answer)],
        },
        fallbacks=[],
    )
    app.add_handler(apply_conv)
    app.add_handler(faq_conv)

    # Jobs
    if ua_alarm_enabled():
        app.job_queue.run_repeating(alerts_job, interval=UA_ALARM_POLL_SEC, first=5)

    if news_config_ok():
        app.job_queue.run_repeating(news_job, interval=NEWS_POLL_SEC, first=15)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
