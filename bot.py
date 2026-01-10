import os
import time
import secrets
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
# ENV
# =========================
def need_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v

BOT_TOKEN = need_env("BOT_TOKEN")
ADMIN_ID = int(need_env("ADMIN_ID"))

# Optional AI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-5")

_ai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _ai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        _ai_client = None

# Optional official alarms
UA_ALARM_API_KEY = os.getenv("UA_ALARM_API_KEY", "")
UA_ALARM_POLL_SEC = int(os.getenv("UA_ALARM_POLL_SEC", "15"))
UA_ALARM_BASE = os.getenv("UA_ALARM_BASE", "https://api.ukrainealarm.com")  # leave default

# =========================
# Language policy
# - User can write any language
# - Bot replies ONLY in Ukrainian or English (never Russian)
# =========================
USER_LANG: Dict[int, str] = {}  # user_id -> "uk" | "en"

def get_lang(user_id: int) -> str:
    return USER_LANG.get(user_id, "uk")

def t(user_id: int, uk: str, en: str) -> str:
    return uk if get_lang(user_id) == "uk" else en

# =========================
# Content (filled)
# =========================
CONTENT = {
    "uk": {
        "title": "УКРАВІАКОСТЕХ | ЕКСТРЕНИЙ ЗВʼЯЗОК",
        "company": (
            "🏢 **УКРАВІАКОСТЕХ**\n\n"
            "УКРАВІАКОСТЕХ — інженерна компанія, що розробляє та підтримує автономні та інфраструктурні рішення.\n"
            "Цей бот — офіційний інтерфейс для керованого доступу до резервної системи комунікації в екстрених сценаріях.\n\n"
            "🔐 Доступ надається **лише за запитом**."
        ),
        "products": (
            "🧩 **Рішення та продукти (публічно, без технічних деталей)**\n\n"
            "• Резервні автономні системи комунікації для екстрених сценаріїв\n"
            "• Інфраструктурні рішення для координації під час НС\n"
            "• Розгортання, інтеграція та підтримка мереж\n"
            "• Підтримувані користувацькі комплекти та супровід підключення\n\n"
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
            "📦 **Обладнання для користувачів**\n\n"
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
            "1) Натисніть «Подати запит на доступ»\n"
            "2) Дайте відповіді на 2 питання\n"
            "3) Підтвердіть правила\n\n"
            "Після цього адміністратор отримає заявку."
        ),
        "faq_hint": (
            "💬 **Питання**\n\n"
            "Напишіть питання одним повідомленням.\n"
            "Я відповім коротко (без технічних деталей)."
        ),
        "lang_saved": "✅ Мову збережено.",
        "menu": "Меню:",
        "cooldown": "⏳ Зачекайте {sec} сек і спробуйте ще раз.",
        "apply_intro": (
            "🟢 **ЗАПИТ НА ДОСТУП**\n\n"
            "Для чого вам доступ до мережі?\n"
            "Напишіть коротко (1 рядок)."
        ),
        "ask_device": "📦 Який пристрій ви плануєте використовувати? (ThinkNode M2 / T-Echo / Heltec)",
        "confirm": "✅ Підтвердіть правила. Напишіть: **ПІДТВЕРДЖУЮ**",
        "cancelled": "❌ Запит скасовано.",
        "sent_to_admin": "✅ Дякуємо! Заявку передано адміністратору. Очікуйте відповідь у цьому чаті.",
        "ai_off": "ℹ️ AI-відповіді тимчасово недоступні.",
        "no_rights": "⛔ Недостатньо прав.",
        "already_done": "⚠️ Заявка вже оброблена або не знайдена.",
        "approved_user": "✅ Ваш запит **схвалено**. Інструкції/доступ буде надано адміністратором окремо.",
        "denied_user": "❌ Ваш запит **відхилено**. Ви можете подати запит повторно пізніше.",
        "approved_admin": "✅ Схвалено для {who}",
        "denied_admin": "❌ Відхилено для {who}",
        "choose_lang": "Оберіть мову / Choose language:",
        "alerts_no_key": "⚠️ Модуль тривог: ключ не налаштовано.",
        "alerts_on_ok": "✅ Сповіщення про тривоги увімкнено (Одеська область).",
        "alerts_off_ok": "✅ Сповіщення про тривоги вимкнено.",
        "alerts_choose_region": "Оберіть регіон для сповіщень:",
        "alerts_set_oblast": "✅ Регіон встановлено: Одеська область.",
        "alerts_set_city": "✅ Регіон встановлено: Одеса (місто).",
    },
    "en": {
        "title": "UKRAVIAKOSTECH | EMERGENCY LINK",
        "company": (
            "🏢 **UkrAviaKosTech**\n\n"
            "UkrAviaKosTech is an engineering company building autonomous and infrastructure-grade solutions.\n"
            "This bot is the official interface for managed access to a reserve communication system for emergency scenarios.\n\n"
            "🔐 Access is provided **by request only**."
        ),
        "products": (
            "🧩 **Solutions & products (public, no technical details)**\n\n"
            "• Emergency reserve communication solutions\n"
            "• Infrastructure coordination tools for crisis scenarios\n"
            "• Network deployment, integration and support\n"
            "• Supported user kits and onboarding assistance\n\n"
            "Public cases/overview can be provided after verification."
        ),
        "system": (
            "📡 **How the system works (high level)**\n\n"
            "This is an автономous short-message communication system that can work when:\n"
            "• power is down\n"
            "• internet is down\n"
            "• mobile networks are unavailable\n\n"
            "🔒 We do **not** publish technical parameters, keys or onboarding instructions publicly.\n"
            "Onboarding instructions are provided **after verification**."
        ),
        "gear": (
            "📦 **User equipment**\n\n"
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
            "2) Answer 2 questions\n"
            "3) Confirm the rules\n\n"
            "The admin will receive your request."
        ),
        "faq_hint": (
            "💬 **Questions**\n\n"
            "Send your question in one message.\n"
            "I will answer briefly (no technical details)."
        ),
        "lang_saved": "✅ Language saved.",
        "menu": "Menu:",
        "cooldown": "⏳ Please wait {sec} seconds and try again.",
        "apply_intro": (
            "🟢 **ACCESS REQUEST**\n\n"
            "What do you need access for?\n"
            "Write a short one-liner."
        ),
        "ask_device": "📦 Which device will you use? (ThinkNode M2 / T-Echo / Heltec)",
        "confirm": "✅ Confirm the rules. Type: **CONFIRM**",
        "cancelled": "❌ Request cancelled.",
        "sent_to_admin": "✅ Thanks! Your request was sent to the admin. Please wait for a response here.",
        "ai_off": "ℹ️ AI replies are currently unavailable.",
        "no_rights": "⛔ Not enough permissions.",
        "already_done": "⚠️ This request is already processed or not found.",
        "approved_user": "✅ Your request is **approved**. Onboarding/access will be provided by the admin separately.",
        "denied_user": "❌ Your request is **denied**. You may apply again later.",
        "approved_admin": "✅ Approved for {who}",
        "denied_admin": "❌ Denied for {who}",
        "choose_lang": "Choose language:",
        "alerts_no_key": "⚠️ Alerts module: API key is not configured.",
        "alerts_on_ok": "✅ Air-raid alerts enabled (Odesa oblast).",
        "alerts_off_ok": "✅ Air-raid alerts disabled.",
        "alerts_choose_region": "Choose a region for alerts:",
        "alerts_set_oblast": "✅ Region set: Odesa oblast.",
        "alerts_set_city": "✅ Region set: Odesa city.",
    }
}

# =========================
# Anti-spam
# =========================
COOLDOWN_SEC = 45
AI_COOLDOWN_SEC = 10
_last_apply: Dict[int, float] = {}
_last_ai: Dict[int, float] = {}

# =========================
# Access requests (in memory)
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
# Alerts subscriptions (in memory)
# =========================
ALERTS_ENABLED: Dict[int, bool] = {}  # user_id -> on/off
ALERT_REGION: Dict[int, str] = {}    # user_id -> regionId
ALERT_LAST_STATE: Dict[str, bool] = {}  # regionId -> last state
REGION_CACHE: Dict[str, str] = {}    # name -> regionId

# =========================
# Conversation states
# =========================
ASK_PURPOSE, ASK_DEVICE, ASK_CONFIRM, ASK_FAQ = range(4)

# =========================
# UI
# =========================
def menu_kb(user_id: int) -> InlineKeyboardMarkup:
    lang = get_lang(user_id)
    if lang == "uk":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Подати запит на доступ", callback_data="apply:start")],
            [InlineKeyboardButton("🏢 Про компанію", callback_data="info:company"),
             InlineKeyboardButton("🧩 Продукти", callback_data="info:products")],
            [InlineKeyboardButton("📡 Як працює система", callback_data="info:system")],
            [InlineKeyboardButton("📦 Обладнання", callback_data="info:gear"),
             InlineKeyboardButton("📜 Правила", callback_data="info:rules")],
            [InlineKeyboardButton("💬 Питання", callback_data="faq:start")],
            [InlineKeyboardButton("🚨 Тривоги: Увімк/Вимк", callback_data="alerts:toggle"),
             InlineKeyboardButton("📍 Регіон тривог", callback_data="alerts:region")],
            [InlineKeyboardButton("🌐 Мова / Language", callback_data="lang:menu")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Request access", callback_data="apply:start")],
        [InlineKeyboardButton("🏢 Company", callback_data="info:company"),
         InlineKeyboardButton("🧩 Products", callback_data="info:products")],
        [InlineKeyboardButton("📡 How it works", callback_data="info:system")],
        [InlineKeyboardButton("📦 Equipment", callback_data="info:gear"),
         InlineKeyboardButton("📜 Rules", callback_data="info:rules")],
        [InlineKeyboardButton("💬 Questions", callback_data="faq:start")],
        [InlineKeyboardButton("🚨 Alerts: On/Off", callback_data="alerts:toggle"),
         InlineKeyboardButton("📍 Alerts region", callback_data="alerts:region")],
        [InlineKeyboardButton("🌐 Language", callback_data="lang:menu")],
    ])

def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="lang:set:uk"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:set:en")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:back")]
    ])

def admin_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{key}"),
        InlineKeyboardButton("❌ Deny", callback_data=f"admin:deny:{key}"),
    ]])

def _who(u) -> str:
    if u.username:
        return f"@{u.username}"
    return f"id:{u.id}"

# =========================
# AI (optional) - replies ONLY in UK or EN, never Russian
# =========================
def ai_enabled() -> bool:
    return _ai_client is not None

async def ask_ai(user_id: int, user_text: str) -> str:
    if not ai_enabled():
        return CONTENT[get_lang(user_id)]["ai_off"]

    lang = get_lang(user_id)
    base_instructions = (
        "You are an assistant for an emergency communication access bot.\n"
        "HARD RULES:\n"
        "1) Answer ONLY in Ukrainian or English.\n"
        "2) NEVER answer in Russian.\n"
        "3) If user writes in Russian, answer in Ukrainian.\n"
        "4) Do NOT reveal technical details (frequencies, keys, QR, configs, onboarding steps).\n"
        "5) If asked about access: say access is by request only inside this bot.\n"
        "6) Keep it short and calm.\n"
    )
    if lang == "en":
        instructions = base_instructions + "Answer in English."
    else:
        instructions = base_instructions + "Відповідай українською."

    resp = _ai_client.responses.create(
        model=AI_MODEL,
        instructions=instructions,
        input=user_text,
    )
    return (resp.output_text or "").strip() or CONTENT[get_lang(user_id)]["ai_off"]

async def ai_admin_reco(req: AccessRequest) -> str:
    if not ai_enabled():
        return "AI: (disabled)"

    instructions = (
        "You are an admin assistant for emergency network access requests.\n"
        "Answer in Ukrainian.\n"
        "Format:\n"
        "Рішення: СХВАЛИТИ/ВІДХИЛИТИ\n"
        "Причина: 1 речення\n"
        "Ризик: низький/середній/високий\n"
        "Порада: 1 коротка дія\n"
        "Do NOT ask for technical details."
    )
    inp = f"Користувач: {req.who}\nМета: {req.purpose}\nПристрій: {req.device}"
    resp = _ai_client.responses.create(model=AI_MODEL, instructions=instructions, input=inp)
    return (resp.output_text or "").strip() or "AI: (no recommendation)"

# =========================
# Official alarms helpers (official key)
# NOTE: endpoints/fields must be verified with your official docs/email.
# =========================
def ua_alarm_enabled() -> bool:
    return bool(UA_ALARM_API_KEY)

def ua_alarm_headers() -> dict:
    # Official docs commonly use Authorization: <API_KEY>
    return {"Authorization": UA_ALARM_API_KEY}

async def ua_get_json(path: str):
    url = UA_ALARM_BASE.rstrip("/") + path
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=ua_alarm_headers())
        r.raise_for_status()
        return r.json()

async def ua_load_regions_cache() -> None:
    """
    Loads regions, finds:
    - 'Одеська область'
    - optional 'Одеса' if available as separate item
    """
    if REGION_CACHE:
        return

    # TODO: confirm exact endpoint with official docs.
    data = await ua_get_json("/api/v3/regions")
    items = data if isinstance(data, list) else data.get("regions") or data.get("data") or []

    def norm(s: str) -> str:
        return (s or "").strip().lower()

    for it in items:
        name = it.get("name") or it.get("title") or ""
        rid = it.get("regionId") or it.get("id") or it.get("region_id") or ""
        if not (name and rid):
            continue
        n = norm(name)
        if n in (norm("Одеська область"), norm("Одеська обл.")):
            REGION_CACHE["Одеська область"] = str(rid)
        if "одес" in n and ("місто" in n or "м." in n or n == "одеса"):
            REGION_CACHE["Одеса"] = str(rid)

async def ua_region_id_oblast() -> str:
    await ua_load_regions_cache()
    if "Одеська область" in REGION_CACHE:
        return REGION_CACHE["Одеська область"]
    raise RuntimeError("Не знайдено 'Одеська область' у /regions.")

async def ua_region_id_city() -> Optional[str]:
    await ua_load_regions_cache()
    return REGION_CACHE.get("Одеса")

async def alerts_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Polls alert state for subscribed regions and pushes only on change.
    """
    if not ua_alarm_enabled():
        return

    subs = [uid for uid, on in ALERTS_ENABLED.items() if on and uid in ALERT_REGION]
    if not subs:
        return

    region_ids = sorted({ALERT_REGION[uid] for uid in subs})

    for rid in region_ids:
        try:
            # TODO: confirm endpoint & field names with official docs.
            data = await ua_get_json(f"/api/v3/alerts/{rid}")

            # best-effort parse of "isAlert"
            is_alert = None
            if isinstance(data, dict):
                for k in ("isAlert", "is_alert", "alert", "active"):
                    if k in data:
                        is_alert = bool(data[k])
                        break
                if is_alert is None and isinstance(data.get("data"), dict):
                    for k in ("isAlert", "is_alert", "alert", "active"):
                        if k in data["data"]:
                            is_alert = bool(data["data"][k])
                            break

            if is_alert is None:
                continue

            prev = ALERT_LAST_STATE.get(rid)
            if prev is None:
                ALERT_LAST_STATE[rid] = is_alert
                continue

            if prev != is_alert:
                ALERT_LAST_STATE[rid] = is_alert
                text_uk = "🔴 ТРИВОГА" if is_alert else "🟢 ВІДБІЙ"
                text_en = "🔴 ALERT" if is_alert else "🟢 ALL CLEAR"

                for uid in subs:
                    if ALERT_REGION.get(uid) == rid:
                        try:
                            await context.bot.send_message(chat_id=uid, text=t(uid, text_uk, text_en))
                        except Exception:
                            pass
        except Exception:
            continue

# =========================
# Commands
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        t(uid,
          f"👋 Вітаю!\n\nЦе офіційний бот доступу до мережі екстреного звʼязку **УКРАВІАКОСТЕХ**.\n\n{CONTENT['uk']['menu']}",
          f"👋 Hello!\n\nThis is the official access bot for **UkrAviaKosTech** emergency communication.\n\n{CONTENT['en']['menu']}"),
        parse_mode="Markdown",
        reply_markup=menu_kb(uid),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(CONTENT[get_lang(uid)]["help"], parse_mode="Markdown")

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(CONTENT[get_lang(uid)]["rules"], parse_mode="Markdown")

async def alerts_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ua_alarm_enabled():
        await update.message.reply_text(CONTENT[get_lang(uid)]["alerts_no_key"])
        return
    rid = await ua_region_id_oblast()
    ALERT_REGION[uid] = rid
    ALERTS_ENABLED[uid] = True
    await update.message.reply_text(CONTENT[get_lang(uid)]["alerts_on_ok"])

async def alerts_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ALERTS_ENABLED[uid] = False
    await update.message.reply_text(CONTENT[get_lang(uid)]["alerts_off_ok"])

# =========================
# Menu handler (callbacks)
# =========================
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "lang:menu":
        await q.message.reply_text(CONTENT[get_lang(uid)]["choose_lang"], reply_markup=lang_kb())
        return

    if data.startswith("lang:set:"):
        _, _, lng = data.split(":")
        if lng not in ("uk", "en"):
            lng = "uk"
        USER_LANG[uid] = lng
        await q.message.reply_text(CONTENT[get_lang(uid)]["lang_saved"])
        await q.message.reply_text(CONTENT[get_lang(uid)]["menu"], reply_markup=menu_kb(uid))
        return

    if data == "menu:back":
        await q.message.reply_text(CONTENT[get_lang(uid)]["menu"], reply_markup=menu_kb(uid))
        return

    if data == "info:company":
        await q.message.reply_text(CONTENT[get_lang(uid)]["company"], parse_mode="Markdown")
        return

    if data == "info:products":
        await q.message.reply_text(CONTENT[get_lang(uid)]["products"], parse_mode="Markdown")
        return

    if data == "info:system":
        await q.message.reply_text(CONTENT[get_lang(uid)]["system"], parse_mode="Markdown")
        return

    if data == "info:gear":
        await q.message.reply_text(CONTENT[get_lang(uid)]["gear"], parse_mode="Markdown")
        return

    if data == "info:rules":
        await q.message.reply_text(CONTENT[get_lang(uid)]["rules"], parse_mode="Markdown")
        return

    if data == "faq:start":
        now = time.time()
        last = _last_ai.get(uid, 0)
        if now - last < AI_COOLDOWN_SEC:
            await q.message.reply_text(CONTENT[get_lang(uid)]["cooldown"].format(sec=int(AI_COOLDOWN_SEC - (now - last))))
            return
        _last_ai[uid] = now
        await q.message.reply_text(CONTENT[get_lang(uid)]["faq_hint"], parse_mode="Markdown")
        return ASK_FAQ

    if data == "apply:start":
        now = time.time()
        last = _last_apply.get(uid, 0)
        if now - last < COOLDOWN_SEC:
            await q.message.reply_text(CONTENT[get_lang(uid)]["cooldown"].format(sec=int(COOLDOWN_SEC - (now - last))))
            return
        _last_apply[uid] = now
        await q.message.reply_text(CONTENT[get_lang(uid)]["apply_intro"], parse_mode="Markdown")
        return ASK_PURPOSE

    if data == "alerts:toggle":
        if not ua_alarm_enabled():
            await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_no_key"])
            return
        on = ALERTS_ENABLED.get(uid, False)
        if on:
            ALERTS_ENABLED[uid] = False
            await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_off_ok"])
        else:
            rid = await ua_region_id_oblast()
            ALERT_REGION[uid] = rid
            ALERTS_ENABLED[uid] = True
            await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_on_ok"])
        return

    if data == "alerts:region":
        if not ua_alarm_enabled():
            await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_no_key"])
            return
        await ua_load_regions_cache()
        buttons = [[InlineKeyboardButton("Одеська область" if get_lang(uid) == "uk" else "Odesa oblast", callback_data="areg:set:oblast")]]
        city_id = await ua_region_id_city()
        if city_id:
            buttons.append([InlineKeyboardButton("Одеса (місто)" if get_lang(uid) == "uk" else "Odesa city", callback_data="areg:set:city")])
        await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_choose_region"], reply_markup=InlineKeyboardMarkup(buttons))
        return

# =========================
# Alerts region selection callback
# =========================
async def alerts_region_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not ua_alarm_enabled():
        await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_no_key"])
        return

    try:
        await ua_load_regions_cache()
        if q.data == "areg:set:oblast":
            ALERT_REGION[uid] = REGION_CACHE["Одеська область"]
            await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_set_oblast"])
        elif q.data == "areg:set:city":
            city_id = await ua_region_id_city()
            if city_id:
                ALERT_REGION[uid] = city_id
                await q.message.reply_text(CONTENT[get_lang(uid)]["alerts_set_city"])
    except Exception as e:
        await q.message.reply_text(f"⚠️ {e}")

# =========================
# Conversation: apply flow
# =========================
async def ask_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data["purpose"] = (update.message.text or "").strip()
    await update.message.reply_text(CONTENT[get_lang(uid)]["ask_device"])
    return ASK_DEVICE

async def ask_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data["device"] = (update.message.text or "").strip()
    # confirm word differs by lang
    confirm_word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    await update.message.reply_text(CONTENT[get_lang(uid)]["confirm"].replace("ПІДТВЕРДЖУЮ", confirm_word).replace("CONFIRM", confirm_word),
                                    parse_mode="Markdown")
    return ASK_CONFIRM

async def ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip().upper()
    confirm_word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    if txt != confirm_word:
        await update.message.reply_text(CONTENT[get_lang(uid)]["cancelled"])
        return ConversationHandler.END

    u = update.effective_user
    key = secrets.token_hex(8)

    req = AccessRequest(
        key=key,
        user_id=u.id,
        chat_id=update.effective_chat.id,
        who=_who(u),
        purpose=context.user_data.get("purpose", ""),
        device=context.user_data.get("device", ""),
        ts=time.time(),
    )
    PENDING[key] = req

    reco = await ai_admin_reco(req)

    admin_text = (
        "🆕 **ЗАЯВКА НА ДОСТУП**\n\n"
        f"👤 {req.who}\n"
        f"🎯 Мета: {req.purpose}\n"
        f"📦 Пристрій: {req.device}\n\n"
        f"🤖 **AI**\n{reco}\n\n"
        f"ID: `{req.user_id}`"
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=admin_text,
        reply_markup=admin_kb(key),
        parse_mode="Markdown",
    )

    await update.message.reply_text(CONTENT[get_lang(uid)]["sent_to_admin"])
    return ConversationHandler.END

# =========================
# Conversation: FAQ (AI)
# =========================
async def faq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    if not txt:
        await update.message.reply_text(t(uid, "Напишіть питання текстом.", "Please send your question as text."))
        return ASK_FAQ
    ans = await ask_ai(uid, txt)
    await update.message.reply_text(ans)
    return ConversationHandler.END

# =========================
# Admin callbacks
# =========================
async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID:
        await q.message.reply_text(CONTENT["uk"]["no_rights"])
        return

    _, action, key = q.data.split(":", 2)
    req = PENDING.pop(key, None)
    if not req:
        await q.message.reply_text(CONTENT["uk"]["already_done"])
        return

    if action == "approve":
        await context.bot.send_message(chat_id=req.chat_id, text=CONTENT[get_lang(req.user_id)]["approved_user"], parse_mode="Markdown")
        await q.message.reply_text(CONTENT["uk"]["approved_admin"].format(who=req.who))
        return

    if action == "deny":
        await context.bot.send_message(chat_id=req.chat_id, text=CONTENT[get_lang(req.user_id)]["denied_user"], parse_mode="Markdown")
        await q.message.reply_text(CONTENT["uk"]["denied_admin"].format(who=req.who))
        return

# =========================
# Main
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("alerts_on", alerts_on_cmd))
    app.add_handler(CommandHandler("alerts_off", alerts_off_cmd))

    # Menu/info callbacks (apply:start handled ONLY by Conversation entry point to avoid double-start bugs)
    app.add_handler(CallbackQueryHandler(
        menu_handler,
        pattern=r"^(info:company|info:products|info:system|info:gear|info:rules|faq:start|lang:menu|lang:set:(uk|en)|menu:back|alerts:toggle|alerts:region)$"
    ))

    # Alerts region callback
    app.add_handler(CallbackQueryHandler(alerts_region_cb, pattern=r"^areg:set:(oblast|city)$"))

    # Admin callbacks
    app.add_handler(CallbackQueryHandler(admin_handler, pattern=r"^admin:(approve|deny):"))

    # Conversation: apply + faq
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler, pattern=r"^apply:start$")],
        states={
            ASK_PURPOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_purpose)],
            ASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_device)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_confirm)],
            ASK_FAQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, faq_answer)],
        },
        fallbacks=[],
    )
    app.add_handler(conv)

    # Alerts polling job (only if key is set)
    if ua_alarm_enabled():
        app.job_queue.run_repeating(alerts_job, interval=UA_ALARM_POLL_SEC, first=5)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
