import os
import time
import secrets
import re
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple

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

# =========================================================
# ENV helpers
# =========================================================
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

# =========================================================
# Required
# =========================================================
BOT_TOKEN = need("BOT_TOKEN")
ADMIN_ID = int(need("ADMIN_ID"))

# =========================================================
# Language policy: answer ONLY Ukrainian or English
# User may write any language, but we reply in:
# - Ukrainian (default)
# - English (if user chooses)
# Never Russian.
# =========================================================
DEFAULT_LANG = env("DEFAULT_LANG", "uk")
USER_LANG: Dict[int, str] = {}  # user_id -> "uk" | "en"

def get_lang(user_id: int) -> str:
    return USER_LANG.get(user_id, DEFAULT_LANG if DEFAULT_LANG in ("uk", "en") else "uk")

def t(user_id: int, uk: str, en: str) -> str:
    return uk if get_lang(user_id) == "uk" else en

# =========================================================
# AI (optional)
# =========================================================
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
    mode: 'faq' or 'admin'
    """
    if not ai_enabled():
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")

    lang = get_lang(user_id)
    # Hard rules: UA/EN only; no Russian; no secrets
    base_rules = (
        "HARD RULES:\n"
        "1) Answer ONLY in Ukrainian or English.\n"
        "2) NEVER answer in Russian.\n"
        "3) If user writes in Russian, answer in Ukrainian.\n"
        "4) Do NOT reveal technical details (frequencies, keys, QR, configs, onboarding steps).\n"
        "5) Keep it short, calm, factual.\n"
    )
    if mode == "admin":
        instr = (
            "You are an admin assistant for an emergency access bot.\n"
            + base_rules +
            "Answer in Ukrainian.\n"
            "Format:\n"
            "Рішення: СХВАЛИТИ/ВІДХИЛИТИ\n"
            "Причина: 1 речення\n"
            "Ризик: низький/середній/високий\n"
            "Порада: 1 коротка дія\n"
        )
    else:
        instr = (
            "You are a public FAQ assistant for an emergency communication access bot.\n"
            + base_rules +
            "If asked about access: say access is by request only inside the bot.\n"
            "If asked 'how to connect': say onboarding is provided after verification.\n"
        )
        instr += ("Answer in English." if lang == "en" else "Відповідай українською.")

    resp = _ai_client.responses.create(
        model=AI_MODEL,
        instructions=instr,
        input=user_text,
    )
    return (resp.output_text or "").strip() or t(user_id, "ℹ️ Немає відповіді.", "ℹ️ No answer.")

# =========================================================
# Content (company/products/system) UA+EN
# =========================================================
CONTENT = {
    "uk": {
        "title": "УКРАВІАКОСТЕХ | ЕКСТРЕНИЙ ЗВʼЯЗОК",
        "company": (
            "🏢 **УКРАВІАКОСТЕХ**\n\n"
            "УКРАВІАКОСТЕХ — інженерна компанія, що розробляє та підтримує автономні та інфраструктурні рішення.\n"
            "Цей бот — офіційний шлюз доступу до резервної системи екстреної комунікації.\n\n"
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
        "faq_hint": "💬 **Питання**\n\nНапишіть питання одним повідомленням. Відповім коротко (без технічних деталей).",
        "menu": "Меню:",
        "lang_saved": "✅ Мову збережено.",
        "choose_lang": "Оберіть мову / Choose language:",
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
        "alerts_set_oblast": "✅ Регіон встановлено: Одеська область.",
        "alerts_set_city": "✅ Регіон встановлено: Одеса (місто).",
        "news_on": "✅ Срочні новини: увімкнено.",
        "news_off": "✅ Срочні новини: вимкнено.",
        "posted": "📰 Опубліковано в канал.",
        "not_configured": "⚠️ Не налаштовано.",
    },
    "en": {
        "title": "UKRAVIAKOSTECH | EMERGENCY LINK",
        "company": (
            "🏢 **UkrAviaKosTech**\n\n"
            "UkrAviaKosTech is an engineering company developing autonomous and infrastructure-grade solutions.\n"
            "This bot is the official gate for managed access to a reserve emergency communication system.\n\n"
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
            "This is an autonomous short-message communication system that can work when:\n"
            "• power is down\n"
            "• internet is down\n"
            "• mobile networks are unavailable\n\n"
            "🔒 We do **not** publish technical parameters, keys, or onboarding instructions publicly.\n"
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
        "faq_hint": "💬 **Questions**\n\nSend your question in one message. I will reply briefly (no technical details).",
        "menu": "Menu:",
        "lang_saved": "✅ Language saved.",
        "choose_lang": "Choose language:",
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
        "alerts_set_oblast": "✅ Region set: Odesa oblast.",
        "alerts_set_city": "✅ Region set: Odesa city.",
        "news_on": "✅ Urgent news: enabled.",
        "news_off": "✅ Urgent news: disabled.",
        "posted": "📰 Posted to channel.",
        "not_configured": "⚠️ Not configured.",
    }
}

def C(user_id: int, key: str) -> str:
    return CONTENT[get_lang(user_id)][key]

# =========================================================
# UI
# =========================================================
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
            [InlineKeyboardButton("📰 Новини: On/Off", callback_data="news:toggle")],
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
        [InlineKeyboardButton("📰 News: On/Off", callback_data="news:toggle")],
        [InlineKeyboardButton("🌐 Language", callback_data="lang:menu")],
    ])

def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:set:uk"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:back")],
    ])

def admin_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{key}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"admin:deny:{key}"),
    ]])

# =========================================================
# Anti-spam / state
# =========================================================
COOLDOWN_SEC = 45
AI_COOLDOWN_SEC = 10
_last_apply: Dict[int, float] = {}
_last_ai: Dict[int, float] = {}

# =========================================================
# Access requests
# =========================================================
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

# =========================================================
# Alerts (official key) – subscriptions
# =========================================================
UA_ALARM_ENABLED = env_bool("UA_ALARM_ENABLED", False)
UA_ALARM_OBLAST_NAME = env("UA_ALARM_OBLAST_NAME", "Одеська область")
UA_ALARM_CITY_NAME = env("UA_ALARM_CITY_NAME", "Одеса")
UA_ALARM_AUTH_HEADER = env("UA_ALARM_AUTH_HEADER", "Authorization")
UA_ALARM_AUTH_PREFIX = env("UA_ALARM_AUTH_PREFIX", "")
UA_ALARM_BASE = env("UA_ALARM_BASE", "https://api.ukrainealarm.com")
UA_ALARM_REGIONS_PATH = env("UA_ALARM_REGIONS_PATH", "/api/v3/regions")
UA_ALARM_ALERT_PATH_TEMPLATE = env("UA_ALARM_ALERT_PATH_TEMPLATE", "/api/v3/alerts/{regionId}")

def ua_alarm_enabled() -> bool:
    return UA_ALARM_ENABLED and bool(UA_ALARM_API_KEY)

ALERTS_ENABLED: Dict[int, bool] = {}
ALERT_REGION: Dict[int, str] = {}
ALERT_LAST_STATE: Dict[str, bool] = {}
REGION_CACHE: Dict[str, str] = {}

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
        if ("одес" in n) and (("місто" in n) or ("м." in n) or (n == norm(UA_ALARM_CITY_NAME))):
            REGION_CACHE["city"] = str(rid)

async def ua_region_oblast() -> str:
    await ua_load_regions()
    if "oblast" in REGION_CACHE:
        return REGION_CACHE["oblast"]
    raise RuntimeError("Не знайдено regionId для області. Перевір /regions структуру в оф. доках.")

async def ua_region_city() -> Optional[str]:
    await ua_load_regions()
    return REGION_CACHE.get("city")

def parse_is_alert(data: dict) -> Optional[bool]:
    # best effort parse
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
                msg_en = "
