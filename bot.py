import os
import time
import secrets
import asyncio
import logging
import json
import random
import re
import calendar
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import httpx
import feedparser
from dateutil import parser as date_parser

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.error import Forbidden
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
AI_TEMP_DISABLE_SEC = env_int("AI_TEMP_DISABLE_SEC", 900)
AI_TIMEOUT_SEC = env_int("AI_TIMEOUT_SEC", 20)
AI_INPUT_MAX_CHARS = env_int("AI_INPUT_MAX_CHARS", 3000)

_ai_client = None
_ai_disabled_until = 0.0
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _ai_client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)
    except Exception:
        logger.exception("OpenAI client init failed")
        _ai_client = None

def ai_enabled() -> bool:
    if _ai_client is None:
        return False
    return time.time() >= _ai_disabled_until

def ai_configured() -> bool:
    return _ai_client is not None

def _ai_should_backoff(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "insufficient_quota" in msg or "quota" in msg or "429" in msg

def _ai_disable_temporarily(reason: str):
    global _ai_disabled_until
    if AI_TEMP_DISABLE_SEC <= 0:
        return
    _ai_disabled_until = max(_ai_disabled_until, time.time() + AI_TEMP_DISABLE_SEC)
    logger.warning("AI temporarily disabled for %ss: %s", AI_TEMP_DISABLE_SEC, reason)

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
        safe_text = (text or "").strip()[:AI_INPUT_MAX_CHARS]
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=ai_instructions(user_id, mode),
                input=safe_text,
            ),
            timeout=AI_TIMEOUT_SEC,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out or t(user_id, "ℹ️ Немає відповіді.", "ℹ️ No answer.")
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("AI request failed (user_id=%s mode=%s)", user_id, mode)
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")

# =========================
# Content UA/EN
# =========================
CONTENT = {
    "uk": {
        "company": (
            "🏢 **УкрАвіаКосТех**\n\n"
            "Вітаємо у світі інновацій Українських Авіаційних Технологій, де майбутнє БПЛА стає реальністю. "
            "Засновані ентузіастами авіації, ми лідируємо на ринку, надаючи першокласні рішення для цивільних та військових потреб.\n\n"
            "З початку повномасштабної війни, ми пройшли шлях від стартапу до визнаного лідера у сфері БПЛА, "
            "неодноразово змінюючи галузеві стандарти своїми інноваціями.\n\n"
            "Наша місія — використовувати потенціал БПЛА для створення безпечнішого і ефективнішого майбутнього, "
            "а бачення — стати визначальним голосом у технологіях БПЛА на світовій арені.\n\n"
            "Постійні дослідження та розробки ведуть нас вперед, дозволяючи створювати БПЛА, які випереджають свій час "
            "та встановлюють нові стандарти в індустрії.\n\n"
            "Контакти:\n"
            "• Email: ukrainskiaviacijni@gmail.com\n"
            "• Графік: пн-пт 9:00-18:00\n"
            "• Сайт: https://www.ukrainianaviation.com\n"
            "• Facebook: https://www.facebook.com/ukr.avia.kos.tech\n"
            "• Instagram: https://www.instagram.com/ukr.avia.kos.tech\n"
            "• TikTok: https://www.tiktok.com/@ukr.avia.kos.tech"
        ),
        "contact": (
            "📞 **Звʼязок**\n\n"
            "Напишіть нам зручним способом:\n"
            "• Email: ukrainskiaviacijni@gmail.com\n"
            "• Сайт: https://www.ukrainianaviation.com\n"
            "• Facebook: https://www.facebook.com/ukr.avia.kos.tech\n"
            "• Instagram: https://www.instagram.com/ukr.avia.kos.tech\n"
            "• TikTok: https://www.tiktok.com/@ukr.avia.kos.tech\n\n"
            "Графік: пн-пт 9:00-18:00\n\n"
            "Або натисніть кнопку нижче, щоб залишити запит."
        ),
        "products": (
            "🧩 **Каталог продуктів**\n\n"
            "Оберіть продукт, щоб отримати фото та ключові характеристики.\n"
            "ℹ️ Детальні конфігурації та частотні діапазони — за запитом."
        ),
        "products_not_found": "⚠️ Продукт не знайдено.",
        "system": (
            "📡 **Як працює автономна мережа (Meshtastic)**\n\n"
            "Meshtastic — це відкритий проєкт автономного звʼязку на mesh-мережі портативних вузлів.\n"
            "Простіше: багато невеликих пристроїв утворюють «ланцюжок», який передає повідомлення далі.\n\n"
            "Як відбувається обмін:\n"
            "• користувач пише повідомлення у додатку\n"
            "• пристрій передає його в мережу\n"
            "• інші вузли підхоплюють нове повідомлення і ретранслюють далі\n"
            "• мережа уникає дублювання: повторно почуте повідомлення не пересилається\n"
            "• є обмеження на кількість «стрибків», щоб повідомлення не ходили по колу\n"
            "• коли додаток тимчасово недоступний, пристрій тримає невеликий буфер останніх повідомлень\n\n"
            "Чому це працює під час відключень:\n"
            "• не потрібен інтернет або мобільна мережа\n"
            "• кожен вузол може підсилювати покриття\n"
            "• якщо один вузол зник — маршрут відновлюється через інші\n\n"
            "Безпека:\n"
            "• повідомлення захищені шифруванням\n"
            "• обмін без публікації технічних ключів чи налаштувань\n\n"
            "💙 Проєкт безкоштовний для користувачів і фінансується як волонтерська ініціатива.\n\n"
            "Публічно не розкриваємо технічні параметри й інструкції підключення.\n"
            "Підключення — лише після підтвердження.\n\n"
            "📦 **Обладнання**\n\n"
            "Потрібен сумісний пристрій Meshtastic (портативний вузол), джерело живлення та клієнт для керування.\n\n"
            "Необхідне:\n"
            "• сумісний вузол Meshtastic (готовий девайс або модуль)\n"
            "• зарядка/кабель та батарея\n"
            "• телефон або ПК для роботи з клієнтом (iOS/Android/Web)\n\n"
            "Поширені лінійки з офіційного списку:\n"
            "• RAK (WisBlock/WisMesh)\n"
            "• LILYGO (T-Echo, T-Beam, T-Deck)\n"
            "• HELTEC (Mesh Node, LoRa 32)\n"
            "• Seeed Studio (SenseCAP, Wio)\n"
            "• Elecrow (ThinkNode)\n"
            "• B&Q Consulting (Nano/Station)\n"
            "• muzi works (R1 Neo)\n"
            "• Raspberry Pi (Linux native)\n\n"
            "Опційно:\n"
            "• зовнішня антена\n"
            "• запасна батарея/повербанк або сонячне живлення\n"
            "• GPS (за потреби)\n\n"
            "Офіційний список: https://meshtastic.org/docs/hardware/devices/"
        ),
        "service": (
            "🛠️ **Сервісне обслуговування продуктів**\n\n"
            "Для звернення щодо сервісу вкажіть:\n"
            "• модель продукту\n"
            "• короткий опис проблеми\n"
            "• бажаний спосіб звʼязку\n\n"
            "Контакт для звернень: ukrainskiaviacijni@gmail.com\n\n"
            "Або натисніть кнопку нижче, щоб залишити заявку."
        ),
        "contact_form_question": "❓ Яке у вас питання? (1-2 речення)",
        "contact_form_name": "🧑 Ваше імʼя? (1 рядок)",
        "contact_form_contact": "📞 Контактний номер / Telegram / Email",
        "contact_sent": "✅ Запит передано адміністратору. Очікуйте відповідь тут.",
        "contact_sent_admin_fail": "⚠️ Запит збережено, але не вдалося доставити адміністратору. Спробуйте пізніше.",
        "service_form_product": "🛠️ Який продукт/модель? (1 рядок)",
        "service_form_serial": "🔢 Серійний номер?",
        "service_form_contact": "📞 Контактний номер / Telegram / Email",
        "service_sent": "✅ Заявку на сервіс передано адміністратору. Очікуйте відповідь тут.",
        "service_sent_admin_fail": "⚠️ Заявку збережено, але не вдалося доставити адміністратору. Спробуйте пізніше.",
        "rules": (
            "📜 **Правила**\n\n"
            "• Лише екстрені/резервні сценарії\n"
            "• Дотримуйтесь законів і вимог адміністраторів\n"
            "• Без спаму, реклами та дезінформації\n"
            "• Не передавати доступ третім особам\n"
            "• Не публікувати технічні параметри, ключі чи інструкції\n"
            "• Поважайте приватність інших користувачів\n"
            "• Не здійснюйте дій, що можуть порушити роботу мережі\n\n"
            "Посилання на закони України:\n"
            "• «Про електронні комунікації»: https://zakon.rada.gov.ua/laws/show/1089-IX#Text\n"
            "• «Про інформацію»: https://zakon.rada.gov.ua/laws/show/2657-12#Text\n"
            "• «Про захист персональних даних»: https://zakon.rada.gov.ua/laws/show/2297-17#Text\n"
            "Порушення → відключення."
        ),
        "faq_hint": (
            "💬 **Питання та відповіді**\n\n"
            "Напиши своє питання — як людині 😊\n"
            "Якщо потрібно, додай один рядок контексту.\n"
            "Відповім коротко і без технічних деталей."
        ),
        "apply_intro": "🟢 **ЗАПИТ НА ДОСТУП**\n\nВаше імʼя? (1 рядок)",
        "ask_contact": "📞 Контакт для зворотного звʼязку (Telegram/телефон/Email)",
        "ask_purpose": "🎯 Для чого вам доступ? (1 рядок)",
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
        "news_not_cfg": "⚠️ Новини не налаштовано (NEWS_CHANNEL_ID/RSS_FEEDS та NEWS_URGENT_KEYWORDS або NEWS_AI_FILTER_ENABLED).",
        "no_rights": "⛔️ Недостатньо прав.",
        "already_done": "⚠️ Заявку вже оброблено або не знайдено.",
        "approved_user": "✅ Ваш запит схвалено. Інструкції надійдуть окремо.",
        "denied_user": "❌ Ваш запит відхилено.",
    },
    "en": {
        "company": (
            "🏢 **UkrAviaKosTech**\n\n"
            "Welcome to the world of innovations in Ukrainian Aviation Technologies, where the future of UAVs "
            "becomes reality. Founded by aviation enthusiasts, we lead the market by delivering first-class "
            "solutions for civil and military needs.\n\n"
            "Since the start of the full-scale war, we have grown from a startup into a recognized UAV leader, "
            "repeatedly reshaping industry standards through innovation.\n\n"
            "Our mission is to use the potential of UAVs to create a safer and more efficient future, and our vision "
            "is to become a defining voice in UAV technologies worldwide.\n\n"
            "Ongoing research and development keeps us moving forward, enabling UAVs that set new standards "
            "in the industry.\n\n"
            "Contacts:\n"
            "• Email: ukrainskiaviacijni@gmail.com\n"
            "• Hours: Mon-Fri 9:00-18:00\n"
            "• Website: https://www.ukrainianaviation.com\n"
            "• Facebook: https://www.facebook.com/ukr.avia.kos.tech\n"
            "• Instagram: https://www.instagram.com/ukr.avia.kos.tech\n"
            "• TikTok: https://www.tiktok.com/@ukr.avia.kos.tech"
        ),
        "contact": (
            "📞 **Contact**\n\n"
            "Reach us via:\n"
            "• Email: ukrainskiaviacijni@gmail.com\n"
            "• Website: https://www.ukrainianaviation.com\n"
            "• Facebook: https://www.facebook.com/ukr.avia.kos.tech\n"
            "• Instagram: https://www.instagram.com/ukr.avia.kos.tech\n"
            "• TikTok: https://www.tiktok.com/@ukr.avia.kos.tech\n\n"
            "Hours: Mon-Fri 9:00-18:00\n\n"
            "Or use the button below to leave a request."
        ),
        "products": (
            "🧩 **Product catalog**\n\n"
            "Choose a product to see photos and key characteristics.\n"
            "ℹ️ Detailed configurations and frequency ranges are available on request."
        ),
        "products_not_found": "⚠️ Product not found.",
        "system": (
            "📡 **How the autonomous network works (Meshtastic)**\n\n"
            "Meshtastic is an open-source, off-grid mesh network of portable nodes.\n"
            "Simply put: many small devices form a chain that forwards messages onward.\n\n"
            "How messages move:\n"
            "• a user writes a message in the companion app\n"
            "• the device sends it into the mesh\n"
            "• other nodes relay new messages to extend coverage\n"
            "• duplicates are ignored so the network doesn’t resend the same message\n"
            "• there is a hop limit to prevent loops\n"
            "• if the app is temporarily offline, the device keeps a small buffer of recent messages\n\n"
            "Why it works during outages:\n"
            "• no internet or cellular network needed\n"
            "• each node can extend coverage\n"
            "• if one node drops, routes recover through others\n\n"
            "Security:\n"
            "• messages are protected with encryption\n"
            "• no public disclosure of technical keys or settings\n\n"
            "💙 The project is free for users and funded as a volunteer initiative.\n\n"
            "Technical parameters and onboarding steps are not published.\n"
            "Access is provided after verification.\n\n"
            "📦 **Equipment**\n\n"
            "You need a Meshtastic-compatible device (portable node), power, and a client to manage it.\n\n"
            "Required:\n"
            "• a Meshtastic-compatible node (ready-made device or module)\n"
            "• charging cable and battery\n"
            "• phone or PC client (iOS/Android/Web)\n\n"
            "Common device families from the official list:\n"
            "• RAK (WisBlock/WisMesh)\n"
            "• LILYGO (T-Echo, T-Beam, T-Deck)\n"
            "• HELTEC (Mesh Node, LoRa 32)\n"
            "• Seeed Studio (SenseCAP, Wio)\n"
            "• Elecrow (ThinkNode)\n"
            "• B&Q Consulting (Nano/Station)\n"
            "• muzi works (R1 Neo)\n"
            "• Raspberry Pi (Linux native)\n\n"
            "Optional:\n"
            "• external antenna\n"
            "• spare battery/power bank or solar\n"
            "• GPS (if needed)\n\n"
            "Official list: https://meshtastic.org/docs/hardware/devices/"
        ),
        "service": (
            "🛠️ **Product Service**\n\n"
            "For service requests, include:\n"
            "• product model\n"
            "• short description of the issue\n"
            "• preferred contact method\n\n"
            "Service contact: ukrainskiaviacijni@gmail.com\n\n"
            "Or use the button below to submit a request."
        ),
        "contact_form_question": "❓ What is your question? (1-2 sentences)",
        "contact_form_name": "🧑 Your name? (one short line)",
        "contact_form_contact": "📞 Contact number / Telegram / Email",
        "contact_sent": "✅ Request sent to admin. Please wait here.",
        "contact_sent_admin_fail": "⚠️ Request saved, but could not be delivered to admin. Please try later.",
        "service_form_product": "🛠️ Which product/model? (one short line)",
        "service_form_serial": "🔢 Serial number?",
        "service_form_contact": "📞 Contact number / Telegram / Email",
        "service_sent": "✅ Service request sent to admin. Please wait here.",
        "service_sent_admin_fail": "⚠️ Request saved, but could not be delivered to admin. Please try later.",
        "rules": (
            "📜 **Rules**\n\n"
            "• Emergency/reserve scenarios only\n"
            "• Follow laws and admin guidance\n"
            "• No spam, ads, or disinformation\n"
            "• Do not share access with others\n"
            "• Do not publish technical parameters, keys, or instructions\n"
            "• Respect other users' privacy\n"
            "• Do not take actions that may disrupt the network\n\n"
            "Ukrainian legal references:\n"
            "• “On Electronic Communications”: https://zakon.rada.gov.ua/laws/show/1089-IX#Text\n"
            "• “On Information”: https://zakon.rada.gov.ua/laws/show/2657-12#Text\n"
            "• “On Personal Data Protection”: https://zakon.rada.gov.ua/laws/show/2297-17#Text\n"
            "Violations → removal."
        ),
        "faq_hint": (
            "💬 **Questions & Answers**\n\n"
            "Just ask your question — human to human 😊\n"
            "If needed, add one short line of context.\n"
            "I’ll answer briefly and without technical details."
        ),
        "apply_intro": "🟢 **ACCESS REQUEST**\n\nYour name? (one short line)",
        "ask_contact": "📞 Contact for follow-up (Telegram/phone/email)",
        "ask_purpose": "🎯 Purpose? (one short line)",
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
        "news_not_cfg": "⚠️ News not configured (NEWS_CHANNEL_ID/RSS_FEEDS and NEWS_URGENT_KEYWORDS or NEWS_AI_FILTER_ENABLED).",
        "no_rights": "⛔️ Not authorized.",
        "already_done": "⚠️ Request already handled or not found.",
        "approved_user": "✅ Your request was approved. Instructions will follow separately.",
        "denied_user": "❌ Your request was denied.",
    }
}

def C(user_id: int, key: str) -> str:
    return CONTENT[get_lang(user_id)][key]

# =========================
# Greetings
# =========================
GREETINGS = {
    "uk": {
        "plain": [
            "👋 Вітаємо в офіційному боті УкрАвіаКосТех.",
            "Привіт! Це бот УкрАвіаКосТех — ваш канал для інформації та запитів.",
            "👋 Ласкаво просимо до УкрАвіаКосТех.",
            "Вітаємо! УкрАвіаКосТех на зв’язку.",
            "👋 Дякуємо, що з нами. Це офіційний бот УкрАвіаКосТех.",
        ],
        "named": [
            "👋 Вітаємо, {name}, в офіційному боті УкрАвіаКосТех.",
            "Привіт, {name}! Це бот УкрАвіаКосТех — ваш канал для інформації та запитів.",
            "👋 Ласкаво просимо, {name}, до УкрАвіаКосТех.",
            "Вітаємо, {name}! УкрАвіаКосТех на зв’язку.",
            "👋 Дякуємо, що з нами, {name}. Це офіційний бот УкрАвіаКосТех.",
        ],
    },
    "en": {
        "plain": [
            "👋 Welcome to the official UkrAviaKosTech bot.",
            "Hi! This is the UkrAviaKosTech bot — your channel for info and requests.",
            "👋 Welcome to UkrAviaKosTech.",
            "Hello! UkrAviaKosTech is here.",
            "👋 Thanks for joining. This is the official UkrAviaKosTech bot.",
        ],
        "named": [
            "👋 Welcome, {name}, to the official UkrAviaKosTech bot.",
            "Hi, {name}! This is the UkrAviaKosTech bot — your channel for info and requests.",
            "👋 Welcome to UkrAviaKosTech, {name}.",
            "Hello, {name}! UkrAviaKosTech is here.",
            "👋 Thanks for joining, {name}. This is the official UkrAviaKosTech bot.",
        ],
    },
}

def display_name(user) -> str:
    if not user:
        return ""
    name = (user.first_name or "").strip()
    if not name and user.username:
        name = f"@{user.username}"
    name = " ".join(name.split())
    if len(name) > 40:
        name = name[:40].rstrip()
    # Escape braces to avoid .format() errors in greeting templates.
    name = name.replace("{", "{{").replace("}", "}}")
    return name

def greeting_text(user_id: int, name: str) -> str:
    lang = get_lang(user_id)
    group = GREETINGS.get(lang) or GREETINGS.get("uk") or {}
    pool = group.get("named") if name else group.get("plain")
    if not pool:
        return t(user_id, "👋 Вітаю!", "👋 Hello!")
    template = random.choice(pool)
    return template.format(name=name) if name else template

# =========================
# Menu UI
# =========================
@dataclass
class ProductInfo:
    key: str
    menu_uk: str
    menu_en: str
    title_uk: str
    title_en: str
    specs_uk: List[str]
    specs_en: List[str]
    image_name: str

PRODUCT_IMAGES_DIR = Path(__file__).resolve().parent / "data" / "product_images"

PRODUCTS_ORDER = [
    "apostol_intelligent",
    "apostol_extended",
    "vizor",
    "apostol_backpack",
    "apostol_rifle",
    "manul_2b",
    "manul_2r",
    "alligator",
    "hydra_10_opt",
    "hydra_8_opt",
    "hydra_7_opt",
    "hydra_10",
    "hydra_8",
    "hydra_7",
    "hydra_10_fold",
    "bee",
]

PRODUCTS: Dict[str, ProductInfo] = {
    "apostol_intelligent": ProductInfo(
        key="apostol_intelligent",
        menu_uk="🛡️ Апостол Intelligent",
        menu_en="🛡️ Apostol Intelligent",
        title_uk="Апостол Intelligent",
        title_en="Apostol Intelligent",
        specs_uk=[
            "Всенаправлена система протидії БПЛА, 6-канальна",
            "Дальність впливу: до 250 м",
            "Потужність модульної системи (10 модулів): >=500 Вт",
            "Час розгортання: ~3 хв",
            "Вага (макс. комплектація): ~25 кг",
            "Температура експлуатації: -40..+50 C; охолодження імерсійне",
        ],
        specs_en=[
            "Omnidirectional counter-UAV system, 6-channel",
            "Effective range: up to 250 m",
            "Module system power (10 modules): >=500 W",
            "Deployment time: ~3 min",
            "Weight (max config): ~25 kg",
            "Operating temp: -40..+50 C; immersion cooling",
        ],
        image_name="apostol_intelligent.png",
    ),
    "apostol_extended": ProductInfo(
        key="apostol_extended",
        menu_uk="🛡️ Апостол (розширений)",
        menu_en="🛡️ Apostol (extended)",
        title_uk="Апостол (розширений діапазон)",
        title_en="Apostol (extended range)",
        specs_uk=[
            "Всенаправлена система з розширеним діапазоном (для авто/позиції)",
            "Дальність впливу: до 250 м",
            "Потужність: >=300 Вт (базова) або >=600 Вт (посилена)",
            "Час розгортання: ~3 хв",
            "Вага: ~12 кг",
            "Температура експлуатації: -40..+50 C",
        ],
        specs_en=[
            "Omnidirectional system with extended range (auto/position variants)",
            "Effective range: up to 250 m",
            "Power: >=300 W (base) or >=600 W (enhanced)",
            "Deployment time: ~3 min",
            "Weight: ~12 kg",
            "Operating temp: -40..+50 C",
        ],
        image_name="apostol_extended.png",
    ),
    "vizor": ProductInfo(
        key="vizor",
        menu_uk="🛰️ Vizor (детектор)",
        menu_en="🛰️ Vizor detector",
        title_uk="Детектор «Vizor»",
        title_en="“Vizor” detector",
        specs_uk=[
            "Детектор автоматизації для системи «Апостол»",
            "Дальність виявлення: до 5 км",
            "Швидкість сканування: 5 с",
            "Живлення: 9-12.6 В (3S), робота 6-8 год",
            "Габарити: 160x190x50 мм; вага до 850 г",
            "Екрани: інформаційний + відео",
        ],
        specs_en=[
            "Automation detector for the “Apostol” system",
            "Detection range: up to 5 km",
            "Scan speed: 5 s",
            "Power: 9-12.6 V (3S), 6-8 h operation",
            "Size: 160x190x50 mm; weight up to 850 g",
            "Displays: info + video",
        ],
        image_name="vizor.png",
    ),
    "apostol_backpack": ProductInfo(
        key="apostol_backpack",
        menu_uk="🎒 РЕБ-рюкзак «Апостол»",
        menu_en="🎒 EW backpack “Apostol”",
        title_uk="РЕБ-рюкзак «Апостол»",
        title_en="EW backpack “Apostol”",
        specs_uk=[
            "Мобільний двохдіапазонний комплекс купольного захисту",
            "Потужність: 100 або 200 Вт (варіанти)",
            "Дальність впливу: до 250 м",
            "Охолодження: захист від перегріву",
            "Температура експлуатації: -40..+50 C",
            "Комплект: антени, пульт, кабелі, акумулятор, рюкзак",
        ],
        specs_en=[
            "Mobile dual-band omnidirectional protection system",
            "Power: 100 or 200 W (variants)",
            "Effective range: up to 250 m",
            "Cooling: overheat protection",
            "Operating temp: -40..+50 C",
            "Kit: antennas, controller, cables, battery, backpack",
        ],
        image_name="apostol_backpack.png",
    ),
    "apostol_rifle": ProductInfo(
        key="apostol_rifle",
        menu_uk="🔫 РЕБ-рушниця «Апостол»",
        menu_en="🔫 EW rifle “Apostol”",
        title_uk="РЕБ-рушниця «Апостол»",
        title_en="EW rifle “Apostol”",
        specs_uk=[
            "Мобільний двохдіапазонний комплекс направленої дії",
            "Потужність: 200 або 400 Вт",
            "Дальність впливу: до 500 м",
            "Час роботи: до 40 хв",
            "Охолодження: захист від перегріву",
            "Температура експлуатації: -40..+50 C",
        ],
        specs_en=[
            "Mobile dual-band directional system",
            "Power: 200 or 400 W",
            "Effective range: up to 500 m",
            "Operating time: up to 40 min",
            "Cooling: overheat protection",
            "Operating temp: -40..+50 C",
        ],
        image_name="apostol_rifle.png",
    ),
    "manul_2b": ProductInfo(
        key="manul_2b",
        menu_uk="✈️ БПЛА «Манул 2Б»",
        menu_en="✈️ UAV “Manul 2B”",
        title_uk="БПЛА літакового типу «Манул 2Б»",
        title_en="Fixed-wing UAV “Manul 2B”",
        specs_uk=[
            "Макс дальність: 300 км; тактичний радіус 150 км",
            "Тривалість польоту: до 3 год",
            "Висота: до 2000 м (робоча ~300 м)",
            "Швидкість: крейсерська 22 м/с, макс 30 м/с",
            "Оптико-електронні засоби: відео та ІЧ",
        ],
        specs_en=[
            "Max range: 300 km; tactical radius 150 km",
            "Endurance: up to 3 h",
            "Altitude: up to 2000 m (working ~300 m)",
            "Speed: cruise 22 m/s, max 30 m/s",
            "Sensors: video + IR",
        ],
        image_name="manul_2b.png",
    ),
    "manul_2r": ProductInfo(
        key="manul_2r",
        menu_uk="✈️ БПЛА «Манул 2Р»",
        menu_en="✈️ UAV “Manul 2R”",
        title_uk="БПЛА літакового типу «Манул 2Р»",
        title_en="Fixed-wing UAV “Manul 2R”",
        specs_uk=[
            "Макс дальність: 550 км; тактичний радіус 300 км",
            "Тривалість польоту: до 5 год",
            "Висота: до 2000 м (робоча ~800 м)",
            "Швидкість: крейсерська 22 м/с, макс 30 м/с",
            "Оптика: відео, ІЧ, 4K відео, фото",
        ],
        specs_en=[
            "Max range: 550 km; tactical radius 300 km",
            "Endurance: up to 5 h",
            "Altitude: up to 2000 m (working ~800 m)",
            "Speed: cruise 22 m/s, max 30 m/s",
            "Sensors: video, IR, 4K video, photo",
        ],
        image_name="manul_2r.png",
    ),
    "alligator": ProductInfo(
        key="alligator",
        menu_uk="✈️ БПЛА «Алігатор»",
        menu_en="✈️ UAV “Alligator”",
        title_uk="БПЛА літакового типу «Алігатор»",
        title_en="Fixed-wing UAV “Alligator”",
        specs_uk=[
            "Макс дальність: 32 км; тактичний радіус 32 км",
            "Тривалість польоту: до 30 хв",
            "Висота: до 3000 м (робоча ~1000 м)",
            "Швидкість: крейсерська 22 м/с, макс 25 м/с",
            "Оптика: відео, ІЧ, аналогове FPV",
        ],
        specs_en=[
            "Max range: 32 km; tactical radius 32 km",
            "Endurance: up to 30 min",
            "Altitude: up to 3000 m (working ~1000 m)",
            "Speed: cruise 22 m/s, max 25 m/s",
            "Sensors: video, IR, analog FPV",
        ],
        image_name="alligator.png",
    ),
    "hydra_10_opt": ProductInfo(
        key="hydra_10_opt",
        menu_uk="🛰️ «Гідра 10 PRO Optical»",
        menu_en="🛰️ “Hydra 10 PRO Optical”",
        title_uk="БПЛА «Гідра 10 PRO Optical»",
        title_en="UAV “Hydra 10 PRO Optical”",
        specs_uk=[
            "Дальність: 5/10/15 км (за потребою)",
            "Тактичний радіус: 4-5 / 8-10 / 12-15 км",
            "Тривалість польоту: 15-30 хв",
            "Висота: до 100 м (робоча 15 м)",
            "Швидкість: макс 33 м/с, крейсерська 17 м/с",
            "Оптика: відео + тепловізор; оптоволоконний зв'язок",
        ],
        specs_en=[
            "Range: 5/10/15 km (as required)",
            "Tactical radius: 4-5 / 8-10 / 12-15 km",
            "Endurance: 15-30 min",
            "Altitude: up to 100 m (working 15 m)",
            "Speed: max 33 m/s, cruise 17 m/s",
            "Sensors: video + thermal; fiber link",
        ],
        image_name="hydra_10_opt.png",
    ),
    "hydra_8_opt": ProductInfo(
        key="hydra_8_opt",
        menu_uk="🛰️ «Гідра 8 PRO Optical»",
        menu_en="🛰️ “Hydra 8 PRO Optical”",
        title_uk="БПЛА «Гідра 8 PRO Optical»",
        title_en="UAV “Hydra 8 PRO Optical”",
        specs_uk=[
            "Дальність: 5/10/15 км (за потребою)",
            "Тактичний радіус: 4-5 / 8-10 / 12-15 км",
            "Тривалість польоту: 15-30 хв",
            "Висота: до 100 м (робоча 15 м)",
            "Швидкість: макс 33 м/с",
            "Оптика: відео + тепловізор; оптоволоконний зв'язок",
        ],
        specs_en=[
            "Range: 5/10/15 km (as required)",
            "Tactical radius: 4-5 / 8-10 / 12-15 km",
            "Endurance: 15-30 min",
            "Altitude: up to 100 m (working 15 m)",
            "Speed: max 33 m/s",
            "Sensors: video + thermal; fiber link",
        ],
        image_name="hydra_8_opt.png",
    ),
    "hydra_7_opt": ProductInfo(
        key="hydra_7_opt",
        menu_uk="🛰️ «Гідра 7 PRO Optical»",
        menu_en="🛰️ “Hydra 7 PRO Optical”",
        title_uk="БПЛА «Гідра 7 PRO Optical»",
        title_en="UAV “Hydra 7 PRO Optical”",
        specs_uk=[
            "Дальність: 5/10/15 км (за потребою)",
            "Тактичний радіус: 4-5 / 8-10 / 12-15 км",
            "Тривалість польоту: 10-30 хв",
            "Висота: до 100 м (робоча 15 м)",
            "Швидкість: макс 33 м/с, крейсерська 17 м/с",
            "Оптика: відео + тепловізор; оптоволоконний зв'язок",
        ],
        specs_en=[
            "Range: 5/10/15 km (as required)",
            "Tactical radius: 4-5 / 8-10 / 12-15 km",
            "Endurance: 10-30 min",
            "Altitude: up to 100 m (working 15 m)",
            "Speed: max 33 m/s, cruise 17 m/s",
            "Sensors: video + thermal; fiber link",
        ],
        image_name="hydra_7_opt.png",
    ),
    "hydra_10": ProductInfo(
        key="hydra_10",
        menu_uk="🛰️ «Гідра 10 PRO»",
        menu_en="🛰️ “Hydra 10 PRO”",
        title_uk="БПЛА «Гідра 10 PRO»",
        title_en="UAV “Hydra 10 PRO”",
        specs_uk=[
            "Макс дальність: 22 км; тактичний радіус 12-15 км",
            "Тривалість польоту: 15-30 хв",
            "Висота: до 3000 м (робоча 800 м)",
            "Швидкість: макс 33 м/с, крейсерська 17 м/с",
            "Оптика: відео, світлочутлива камера",
            "Виявлення типових цілей: до 1000 м",
        ],
        specs_en=[
            "Max range: 22 km; tactical radius 12-15 km",
            "Endurance: 15-30 min",
            "Altitude: up to 3000 m (working 800 m)",
            "Speed: max 33 m/s, cruise 17 m/s",
            "Sensors: video, low-light camera",
            "Target detection: up to 1000 m",
        ],
        image_name="hydra_10.png",
    ),
    "hydra_8": ProductInfo(
        key="hydra_8",
        menu_uk="🛰️ «Гідра 8 PRO»",
        menu_en="🛰️ “Hydra 8 PRO”",
        title_uk="БПЛА «Гідра 8 PRO»",
        title_en="UAV “Hydra 8 PRO”",
        specs_uk=[
            "Макс дальність: 22 км; тактичний радіус 12-15 км",
            "Тривалість польоту: 15-30 хв",
            "Висота: до 3000 м (робоча 800 м)",
            "Швидкість: макс 33 м/с, крейсерська 17 м/с",
            "Оптика: відео, світлочутлива камера",
        ],
        specs_en=[
            "Max range: 22 km; tactical radius 12-15 km",
            "Endurance: 15-30 min",
            "Altitude: up to 3000 m (working 800 m)",
            "Speed: max 33 m/s, cruise 17 m/s",
            "Sensors: video, low-light camera",
        ],
        image_name="hydra_8.png",
    ),
    "hydra_7": ProductInfo(
        key="hydra_7",
        menu_uk="🛰️ «Гідра 7 PRO»",
        menu_en="🛰️ “Hydra 7 PRO”",
        title_uk="БПЛА «Гідра 7 PRO»",
        title_en="UAV “Hydra 7 PRO”",
        specs_uk=[
            "Макс дальність: 22 км; тактичний радіус 12-15 км",
            "Тривалість польоту: 15-30 хв",
            "Висота: до 3000 м (робоча 800 м)",
            "Швидкість: макс 33 м/с, крейсерська 17 м/с",
            "Оптика: відео, світлочутлива камера",
            "Виявлення типових цілей: до 1000 м",
        ],
        specs_en=[
            "Max range: 22 km; tactical radius 12-15 km",
            "Endurance: 15-30 min",
            "Altitude: up to 3000 m (working 800 m)",
            "Speed: max 33 m/s, cruise 17 m/s",
            "Sensors: video, low-light camera",
            "Target detection: up to 1000 m",
        ],
        image_name="hydra_7.png",
    ),
    "hydra_10_fold": ProductInfo(
        key="hydra_10_fold",
        menu_uk="🛰️ «Гідра 10 PRO» (складна)",
        menu_en="🛰️ “Hydra 10 PRO” (folding)",
        title_uk="БПЛА «Гідра 10 PRO» (складна рама)",
        title_en="UAV “Hydra 10 PRO” (folding frame)",
        specs_uk=[
            "Складна рама",
            "Макс дальність: 22 км; тактичний радіус 12-15 км",
            "Тривалість польоту: 15-30 хв",
            "Висота: до 3000 м (робоча 800 м)",
            "Швидкість: макс 33 м/с, крейсерська 17 м/с",
            "Оптика: відео, світлочутлива камера",
        ],
        specs_en=[
            "Folding frame",
            "Max range: 22 km; tactical radius 12-15 km",
            "Endurance: 15-30 min",
            "Altitude: up to 3000 m (working 800 m)",
            "Speed: max 33 m/s, cruise 17 m/s",
            "Sensors: video, low-light camera",
        ],
        image_name="hydra_10_fold.png",
    ),
    "bee": ProductInfo(
        key="bee",
        menu_uk="🛸 БПЛА «Бджілка»",
        menu_en="🛸 UAV “Bee”",
        title_uk="БПЛА «Бджілка»",
        title_en="UAV “Bee”",
        specs_uk=[
            "Макс дальність: до 5 км; тактичний радіус 4-5 км",
            "Тривалість польоту: 5-8 хв",
            "Висота: до 3000 м (робоча 800 м)",
            "Швидкість: макс 17 м/с",
            "Розгортання/згортання: ~1 хв; підготовка до польоту 1 хв",
            "Виявлення типових цілей: до 1000 м",
        ],
        specs_en=[
            "Max range: up to 5 km; tactical radius 4-5 km",
            "Endurance: 5-8 min",
            "Altitude: up to 3000 m (working 800 m)",
            "Speed: max 17 m/s",
            "Deploy/pack: ~1 min; prep for flight 1 min",
            "Target detection: up to 1000 m",
        ],
        image_name="bee.png",
    ),
}

def products_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for key in PRODUCTS_ORDER:
        prod = PRODUCTS.get(key)
        if not prod:
            continue
        label = prod.menu_uk if get_lang(user_id) == "uk" else prod.menu_en
        rows.append([InlineKeyboardButton(label, callback_data=f"prod:{key}")])
    back_label = "⬅️ Назад" if get_lang(user_id) == "uk" else "⬅️ Back"
    rows.append([InlineKeyboardButton(back_label, callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)

def product_detail_kb(user_id: int) -> InlineKeyboardMarkup:
    if get_lang(user_id) == "uk":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Каталог", callback_data="products:menu")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:back")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Catalog", callback_data="products:menu")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu:back")],
    ])

def product_caption(user_id: int, prod: ProductInfo) -> str:
    title = prod.title_uk if get_lang(user_id) == "uk" else prod.title_en
    specs = prod.specs_uk if get_lang(user_id) == "uk" else prod.specs_en
    lines = [f"**{title}**", ""]
    lines.extend([f"• {s}" for s in specs])
    return "\n".join(lines)

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
            [InlineKeyboardButton("🟢 Запит на доступ до автономної мережі звʼязку", callback_data="apply:start")],
            [InlineKeyboardButton("🏢 Про компанію", callback_data="info:company"),
             InlineKeyboardButton("🧩 Продукти", callback_data="info:products")],
            [InlineKeyboardButton("📞 Звʼязок", callback_data="info:contact"),
             InlineKeyboardButton("🛠️ Сервісне обслуговування", callback_data="info:service")],
            [InlineKeyboardButton("📡 Як працює автономна мережа", callback_data="info:system"),
             InlineKeyboardButton("📜 Правила", callback_data="info:rules")],
            [InlineKeyboardButton("💬 Питання та відповіді", callback_data="faq:start")],
            [InlineKeyboardButton("🚨 Сповіщення про тривогу", callback_data="alerts:menu")],
            [InlineKeyboardButton("🌐 Мова / Language", callback_data="lang:menu")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Access to autonomous comms network", callback_data="apply:start")],
        [InlineKeyboardButton("🏢 Company", callback_data="info:company"),
         InlineKeyboardButton("🧩 Products", callback_data="info:products")],
        [InlineKeyboardButton("📞 Contact", callback_data="info:contact"),
         InlineKeyboardButton("🛠️ Product service", callback_data="info:service")],
        [InlineKeyboardButton("📡 How the autonomous network works", callback_data="info:system"),
         InlineKeyboardButton("📜 Rules", callback_data="info:rules")],
        [InlineKeyboardButton("💬 Questions & Answers", callback_data="faq:start")],
        [InlineKeyboardButton("🚨 Air Alert Notifications", callback_data="alerts:menu")],
        [InlineKeyboardButton("🌐 Language", callback_data="lang:menu")],
    ])

def menu_only_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(user_id, "🏠 Меню", "🏠 Menu"), callback_data="menu:back")],
    ])

def contact_kb(user_id: int) -> InlineKeyboardMarkup:
    label = t(user_id, "📝 Контактна форма", "📝 Contact form")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="contact:form")],
        [InlineKeyboardButton(t(user_id, "🏠 Меню", "🏠 Menu"), callback_data="menu:back")],
    ])

def service_kb(user_id: int) -> InlineKeyboardMarkup:
    label = t(user_id, "📝 Сервісна заявка", "📝 Service request")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="service:form")],
        [InlineKeyboardButton(t(user_id, "🏠 Меню", "🏠 Menu"), callback_data="menu:back")],
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
    name: str
    contact: str
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
# User tracking (for summary DMs)
# =========================
USERS_FILE = Path("data/users.json")
KNOWN_USERS: Set[int] = set()

def _load_known_users() -> None:
    if not USERS_FILE.exists():
        return
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load users list")
        return
    if isinstance(data, list):
        for item in data:
            try:
                KNOWN_USERS.add(int(item))
            except Exception:
                continue

def _save_known_users() -> None:
    try:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        USERS_FILE.write_text(json.dumps(sorted(KNOWN_USERS)), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save users list")

def remember_user_id(user_id: int) -> None:
    if user_id in KNOWN_USERS:
        return
    KNOWN_USERS.add(user_id)
    _save_known_users()

def track_user(update: Update) -> None:
    if not update:
        return
    user = update.effective_user
    if not user:
        return
    remember_user_id(user.id)

_load_known_users()

# =========================
# Message cleanup (private chats only)
# =========================
LAST_BOT_MESSAGE_ID: Dict[int, int] = {}

def _is_private_chat_id(chat_id: object) -> bool:
    return isinstance(chat_id, int) and chat_id > 0

async def cleanup_after_send(bot, chat_id: object, message) -> None:
    if not _is_private_chat_id(chat_id):
        return
    if chat_id == ADMIN_ID:
        return
    msg_id = getattr(message, "message_id", None)
    if not msg_id:
        return
    prev_id = LAST_BOT_MESSAGE_ID.get(chat_id)
    if prev_id and prev_id != msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prev_id)
        except Exception:
            pass
    LAST_BOT_MESSAGE_ID[chat_id] = msg_id

async def send_with_cleanup(bot, cleanup_chat_id: object, send_callable, *args, **kwargs):
    msg = await send_callable(*args, **kwargs)
    await cleanup_after_send(bot, cleanup_chat_id, msg)
    return msg

# =========================
# Alerts (official) – configurable via env
# =========================
UA_ALARM_ENABLED = env_bool("UA_ALARM_ENABLED", False)
UA_ALARM_API_KEY = env("UA_ALARM_API_KEY", "")
UA_ALARM_POLL_SEC = env_int("UA_ALARM_POLL_SEC", 15)
UA_ALARM_BASE = env("UA_ALARM_BASE", "https://api.ukrainealarm.com")
UA_ALARM_REGIONS_PATH = env("UA_ALARM_REGIONS_PATH", "/api/v3/regions")
UA_ALARM_ALERT_PATH_TEMPLATE = env("UA_ALARM_ALERT_PATH_TEMPLATE", "/api/v3/alerts/{regionId}")
UA_ALARM_ALERTS_PATH = env("UA_ALARM_ALERTS_PATH", "/api/v3/alerts")
UA_ALARM_AUTH_HEADER = env("UA_ALARM_AUTH_HEADER", "Authorization")
UA_ALARM_AUTH_PREFIX = env("UA_ALARM_AUTH_PREFIX", "")
UA_ALARM_OBLAST_NAME = env("UA_ALARM_OBLAST_NAME", "Одеська область")

def parse_region_ids(value: str) -> List[str]:
    items: List[str] = []
    for raw in value.replace(";", ",").split(","):
        rid = raw.strip()
        if rid:
            items.append(rid)
    seen = set()
    unique: List[str] = []
    for rid in items:
        if rid in seen:
            continue
        seen.add(rid)
        unique.append(rid)
    return unique

UA_ALARM_REGION_IDS = parse_region_ids(env("UA_ALARM_REGION_ID", ""))

ALERTS_ENABLED: Dict[int, bool] = {}
ALERT_OBLAST: Dict[int, str] = {}
ALERT_REGION: Dict[int, List[str]] = {}
ALERT_LAST_STATE: Dict[str, bool] = {}
ALERT_LAST_USER_STATE: Dict[int, bool] = {}
REGION_CACHE: Dict[str, str] = {}
REGION_DATA_LOADED = False
REGION_NAME_UA_BY_ID: Dict[str, str] = {}
REGION_NAME_EN_BY_ID: Dict[str, str] = {}
REGION_TYPE_BY_ID: Dict[str, str] = {}
REGIONS_PER_PAGE = 10

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

def _pick_str(item: dict, keys: List[str]) -> str:
    for k in keys:
        v = item.get(k)
        if v:
            return str(v).strip()
    return ""

def _region_id(item: dict) -> str:
    return _pick_str(item, ["regionId", "id", "region_id"])

def _norm_region_name(name: str) -> str:
    s = (name or "").strip().lower()
    for token in ("область", "обл.", "обл", "oblast"):
        s = s.replace(token, "")
    return " ".join(s.split())

def _guess_region_type(item: dict, name_ua: str, name_en: str) -> str:
    for k in ("regionType", "region_type", "type", "regionTypeName"):
        v = item.get(k)
        if isinstance(v, str):
            val = v.strip().lower()
            if "state" in val or "oblast" in val or "область" in val:
                return "oblast"
            if "city" in val or "місто" in val or "город" in val:
                return "city"
            if "district" in val or "район" in val:
                return "district"
    for k in ("regionTypeId", "region_type_id", "typeId", "type_id"):
        v = item.get(k)
        if v is None:
            continue
        try:
            val = int(v)
        except Exception:
            continue
        if val == 1:
            return "oblast"
        if val == 2:
            return "city"
        if val == 3:
            return "district"
    for name in (name_ua, name_en):
        if not name:
            continue
        low = name.lower()
        if "область" in low or "oblast" in low:
            return "oblast"
        if "місто" in low or "city" in low:
            return "city"
        if "район" in low or "district" in low:
            return "district"
    return ""

async def ua_load_regions(client: Optional[httpx.AsyncClient] = None):
    global REGION_DATA_LOADED
    if REGION_DATA_LOADED:
        return
    data = await ua_get_json(UA_ALARM_REGIONS_PATH, client=client)
    items = data if isinstance(data, list) else data.get("regions") or data.get("states") or data.get("data") or []

    target = _norm_region_name(UA_ALARM_OBLAST_NAME)
    fallback_rid = ""
    fallback_name = ""

    for it in items:
        if not isinstance(it, dict):
            continue
        rid = _region_id(it)
        if not rid:
            continue
        name_ua = _pick_str(it, ["name", "title", "regionName", "regionUkName", "regionUaName"])
        name_en = _pick_str(it, ["regionEngName", "regionEnName", "name_en", "title_en"])
        if not name_ua:
            name_ua = name_en
        if not name_en:
            name_en = name_ua
        if not name_ua and not name_en:
            name_ua = rid
            name_en = rid
        region_type = _guess_region_type(it, name_ua, name_en)

        REGION_NAME_UA_BY_ID[rid] = name_ua
        REGION_NAME_EN_BY_ID[rid] = name_en
        if region_type:
            REGION_TYPE_BY_ID[rid] = region_type

        if not REGION_CACHE.get("oblast") and target:
            name_norm = _norm_region_name(name_ua or name_en)
            if name_norm == target:
                REGION_CACHE["oblast"] = rid
            elif target in name_norm or name_norm in target:
                fallback_rid = rid
                fallback_name = name_ua or name_en

    if not REGION_CACHE.get("oblast") and fallback_rid:
        REGION_CACHE["oblast"] = fallback_rid
        logger.warning("UA alarm region matched by partial name: %s", fallback_name)
    if not REGION_CACHE.get("oblast") and items:
        sample = []
        for it in items[:10]:
            if isinstance(it, dict):
                sample.append(it.get("name") or it.get("title") or "")
        logger.error("UA alarm region not found. Sample regions: %s", sample)
    REGION_DATA_LOADED = True

async def ua_region_oblast() -> str:
    await ua_load_regions()
    if "oblast" in REGION_CACHE:
        return REGION_CACHE["oblast"]
    logger.error("UA alarm region not found for oblast=%s", UA_ALARM_OBLAST_NAME)
    raise RuntimeError("Не знайдено regionId області (перевір /regions endpoint та UA_ALARM_OBLAST_NAME).")

def _coerce_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    return None

def _parse_alert_list(value: object) -> Optional[bool]:
    if not isinstance(value, list):
        return None
    if not value:
        return False
    any_known = False
    for item in value:
        parsed = parse_is_alert(item)
        if parsed is True:
            return True
        if parsed is False:
            any_known = True
    return False if any_known else None

def _parse_alert_item(data: dict) -> Optional[bool]:
    for k in ("isAlert", "is_alert", "isActive", "is_active", "alert", "active", "isContinue", "is_continue"):
        if k in data:
            parsed = _coerce_bool(data.get(k))
            if parsed is not None:
                return parsed
    if "endDate" in data:
        end = data.get("endDate")
        return True if end in (None, "", 0) else False
    return None

def parse_is_alert(data: object) -> Optional[bool]:
    if isinstance(data, dict):
        parsed = _parse_alert_item(data)
        if parsed is not None:
            return parsed
        if "activeAlerts" in data:
            active = data.get("activeAlerts")
            if isinstance(active, list):
                return True if active else False
            parsed = _parse_alert_list(active)
            if parsed is not None:
                return parsed
        for k in ("alerts", "alarms"):
            if k in data:
                parsed = _parse_alert_list(data.get(k))
                if parsed is not None:
                    return parsed
        nested = data.get("data")
        if nested is not None:
            return parse_is_alert(nested)
        return None
    if isinstance(data, list):
        parsed = _parse_alert_list(data)
        if parsed is not None:
            return parsed
    return None

async def fetch_alert_state(client: httpx.AsyncClient, region_id: str) -> Optional[bool]:
    path = UA_ALARM_ALERT_PATH_TEMPLATE.replace("{regionId}", region_id)
    data = await ua_get_json(path, client=client)
    return parse_is_alert(data)

async def ua_region_ids() -> List[str]:
    if UA_ALARM_REGION_IDS:
        return UA_ALARM_REGION_IDS
    rid = await ua_region_oblast()
    return [rid]

def region_display_name(user_id: int, rid: str) -> str:
    if not rid:
        return ""
    if get_lang(user_id) == "en":
        return REGION_NAME_EN_BY_ID.get(rid) or REGION_NAME_UA_BY_ID.get(rid) or rid
    return REGION_NAME_UA_BY_ID.get(rid) or REGION_NAME_EN_BY_ID.get(rid) or rid

def region_type_for_id(rid: str) -> str:
    rtype = REGION_TYPE_BY_ID.get(rid)
    if rtype:
        return rtype
    return _guess_region_type({}, REGION_NAME_UA_BY_ID.get(rid, ""), REGION_NAME_EN_BY_ID.get(rid, ""))

def effective_region_ids(user_id: int) -> List[str]:
    oblast_id = ALERT_OBLAST.get(user_id)
    if oblast_id:
        return [oblast_id]
    return ALERT_REGION.get(user_id, [])

def sync_alert_regions(user_id: int):
    rids = effective_region_ids(user_id)
    if rids:
        ALERT_REGION[user_id] = list(dict.fromkeys(rids))
    else:
        ALERT_REGION.pop(user_id, None)

def region_label_for_message(region_ids: List[str]) -> str:
    if len(region_ids) == 1 and UA_ALARM_OBLAST_NAME:
        return UA_ALARM_OBLAST_NAME
    return ", ".join(region_ids)

async def fetch_active_region_ids(client: httpx.AsyncClient) -> Optional[Set[str]]:
    try:
        data = await ua_get_json(UA_ALARM_ALERTS_PATH, client=client)
    except Exception:
        logger.exception("UA alarm alerts list failed: %s", UA_ALARM_ALERTS_PATH)
        return None
    if not isinstance(data, list):
        return None
    ids: Set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        rid = item.get("regionId") or item.get("region_id") or ""
        rid = str(rid).strip()
        if rid:
            ids.add(rid)
    return ids

def region_items_by_type(user_id: int, region_type: str) -> List[tuple[str, str]]:
    items: List[tuple[str, str]] = []
    for rid in REGION_NAME_UA_BY_ID.keys():
        rtype = region_type_for_id(rid)
        if rtype == region_type:
            items.append((rid, region_display_name(user_id, rid)))
    items.sort(key=lambda x: x[1].lower())
    return items

def paginate_items(items: List[tuple[str, str]], page: int) -> tuple[List[tuple[str, str]], int, int]:
    if not items:
        return [], 0, 0
    total_pages = (len(items) + REGIONS_PER_PAGE - 1) // REGIONS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * REGIONS_PER_PAGE
    return items[start:start + REGIONS_PER_PAGE], page, total_pages

def regions_list_kb(user_id: int, items: List[tuple[str, str]], page: int, prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    page_items, page, total_pages = paginate_items(items, page)
    rows = []
    for rid, name in page_items:
        rows.append([InlineKeyboardButton(name, callback_data=f"{prefix}:set:{rid}")])
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}:page:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}:page:{page + 1}"))
        if nav:
            rows.append(nav)
    back_label = "⬅️ Назад" if get_lang(user_id) == "uk" else "⬅️ Back"
    rows.append([InlineKeyboardButton(back_label, callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)

async def fetch_current_alert_state(region_ids: List[str]) -> Optional[bool]:
    if not region_ids:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            active_ids = await fetch_active_region_ids(client)
            if active_ids is not None:
                for rid in region_ids:
                    ALERT_LAST_STATE[rid] = rid in active_ids
                return any(rid in active_ids for rid in region_ids)
            states: List[bool] = []
            for rid in region_ids:
                state = await fetch_alert_state(client, rid)
                if state is None:
                    continue
                ALERT_LAST_STATE[rid] = state
                states.append(state)
            if not states:
                return None
            return any(states)
    except Exception:
        logger.exception("alerts status fetch failed for regions %s", region_ids)
        return None

async def alerts_status_text(user_id: int) -> str:
    try:
        await ua_load_regions()
    except Exception:
        pass
    def esc(text: str) -> str:
        return escape_markdown(text or "", version=1)
    on = ALERTS_ENABLED.get(user_id, False)
    oblast_id = ALERT_OBLAST.get(user_id, "")
    oblast_name = region_display_name(user_id, oblast_id) if oblast_id else t(user_id, "не обрано", "not set")
    oblast_name = esc(oblast_name)

    lines = [
        t(user_id, "🚨 **Тривоги**", "🚨 **Alerts**"),
        t(user_id, f"Статус: {'увімкнено' if on else 'вимкнено'}",
          f"Status: {'enabled' if on else 'disabled'}"),
        t(user_id, f"Область: {oblast_name}", f"Oblast: {oblast_name}"),
    ]
    if not oblast_id and ALERT_REGION.get(user_id):
        names = [esc(region_display_name(user_id, rid)) for rid in ALERT_REGION.get(user_id, [])]
        if names:
            lines.append(t(user_id, f"Регіони: {', '.join(names)}", f"Regions: {', '.join(names)}"))

    region_ids = effective_region_ids(user_id)
    if on and not region_ids:
        lines.append(t(user_id, "ℹ️ Оберіть область, щоб отримувати сповіщення.",
                        "ℹ️ Choose an oblast to receive alerts."))
    if region_ids:
        state = await fetch_current_alert_state(region_ids)
        if state is True:
            lines.append(t(user_id, "Поточний стан: 🔴 ТРИВОГА", "Current: 🔴 ALERT"))
        elif state is False:
            lines.append(t(user_id, "Поточний стан: 🟢 ВІДБІЙ", "Current: 🟢 ALL CLEAR"))
        else:
            lines.append(t(user_id, "Поточний стан: невідомо", "Current: unknown"))
    return "\n".join(lines)

def alerts_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    on = ALERTS_ENABLED.get(user_id, False)
    toggle_label = t(user_id, "🔔 Увімкнути сповіщення", "🔔 Enable alerts")
    if on:
        toggle_label = t(user_id, "🔕 Вимкнути сповіщення", "🔕 Disable alerts")
    rows = [
        [InlineKeyboardButton(toggle_label, callback_data="alerts:toggle")],
        [InlineKeyboardButton(t(user_id, "🏙️ Область", "🏙️ Oblast"), callback_data="alerts:oblast:menu")],
        [InlineKeyboardButton(t(user_id, "⬅️ Назад", "⬅️ Back"), callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)
async def alerts_job(context: ContextTypes.DEFAULT_TYPE):
    if not ua_alarm_enabled():
        return

    user_regions: Dict[int, List[str]] = {}
    for uid, on in ALERTS_ENABLED.items():
        if not on:
            continue
        rids = effective_region_ids(uid)
        if rids:
            user_regions[uid] = rids
    if not user_regions:
        return

    region_ids = sorted({rid for rids in user_regions.values() for rid in rids})
    region_state: Dict[str, bool] = {}

    async with httpx.AsyncClient(timeout=20) as client:
        active_ids = await fetch_active_region_ids(client)
        if active_ids is not None:
            for rid in region_ids:
                is_alert = rid in active_ids
                region_state[rid] = is_alert
                ALERT_LAST_STATE[rid] = is_alert
        else:
            for rid in region_ids:
                try:
                    is_alert = await fetch_alert_state(client, rid)
                except Exception:
                    logger.exception("alerts_job error for region %s", rid)
                    continue
                if is_alert is None:
                    continue
                region_state[rid] = is_alert
                ALERT_LAST_STATE[rid] = is_alert

    for uid, rids in user_regions.items():
        states = [region_state.get(rid) for rid in rids if rid in region_state]
        if not states:
            continue
        is_alert = any(states)
        prev = ALERT_LAST_USER_STATE.get(uid)
        if prev is None:
            ALERT_LAST_USER_STATE[uid] = is_alert
            continue
        if prev != is_alert:
            ALERT_LAST_USER_STATE[uid] = is_alert
            msg_uk = "🔴 ТРИВОГА" if is_alert else "🟢 ВІДБІЙ"
            msg_en = "🔴 ALERT" if is_alert else "🟢 ALL CLEAR"
            try:
                await send_with_cleanup(
                    context.bot,
                    uid,
                    context.bot.send_message,
                    chat_id=uid,
                    text=t(uid, msg_uk, msg_en),
                    reply_markup=menu_only_kb(uid),
                )
            except Exception:
                pass

# =========================
# News -> Channel (urgent only)
# =========================
NEWS_ENABLED = env_bool("NEWS_ENABLED", False)
NEWS_CHANNEL_ID = env("NEWS_CHANNEL_ID", "")
NEWS_POLL_SEC = env_int("NEWS_POLL_SEC", 120)
RSS_FEEDS = [u.strip() for u in env("RSS_FEEDS", "").split(",") if u.strip()]
URGENT_KEYWORDS = [k.strip() for k in env("NEWS_URGENT_KEYWORDS", "").split(",") if k.strip()]
NEWS_SUMMARY_MAX_CHARS = env_int("NEWS_SUMMARY_MAX_CHARS", 2000)
NEWS_AI_TIMEOUT_SEC = env_int("NEWS_AI_TIMEOUT_SEC", 8)
NEWS_USE_KEYWORDS = env_bool("NEWS_USE_KEYWORDS", False)
NEWS_AI_FILTER_ENABLED = env_bool("NEWS_AI_FILTER_ENABLED", False)
NEWS_AI_STRICT = env_bool("NEWS_AI_STRICT", False)
NEWS_AI_MIN_CRITICALITY = env_int("NEWS_AI_MIN_CRITICALITY", 3)
NEWS_AI_MIN_IMPORTANCE = env_int("NEWS_AI_MIN_IMPORTANCE", 3)
NEWS_AI_SCORE_SCALE = env_int("NEWS_AI_SCORE_SCALE", 5)

NEWS_SEEN_MAX = env_int("NEWS_SEEN_MAX", 5000)
NEWS_MIN_KEYWORDS = env_int("NEWS_MIN_KEYWORDS", 2)
NEWS_REQUIRE_TITLE_KEYWORD = env_bool("NEWS_REQUIRE_TITLE_KEYWORD", True)
NEWS_MAX_POSTS_PER_RUN = env_int("NEWS_MAX_POSTS_PER_RUN", 3)
NEWS_MAX_POSTS_PER_HOUR = env_int("NEWS_MAX_POSTS_PER_HOUR", 12)
_seen_links: Set[str] = set()
_seen_order: deque[str] = deque()
_feed_redirects: Dict[str, str] = {}
_seen_titles: Set[str] = set()
_seen_titles_order: deque[str] = deque()
_news_sent_times: deque[float] = deque()

NEWS_SUMMARY_ENABLED = env_bool("NEWS_SUMMARY_ENABLED", False)
NEWS_SUMMARY_TIMES = env("NEWS_SUMMARY_TIMES", "08:00,14:00,20:00")
NEWS_SUMMARY_TZ = env("NEWS_SUMMARY_TZ", "Europe/Kyiv")
NEWS_SUMMARY_LOOKBACK_HOURS = env_int("NEWS_SUMMARY_LOOKBACK_HOURS", 8)
NEWS_SUMMARY_MAX_ITEMS = env_int("NEWS_SUMMARY_MAX_ITEMS", 12)
NEWS_SUMMARY_SEEN_MAX = env_int("NEWS_SUMMARY_SEEN_MAX", 2000)
NEWS_SUMMARY_SEND_TO_USERS = env_bool("NEWS_SUMMARY_SEND_TO_USERS", True)
NEWS_SUMMARY_SEND_TO_CHANNEL = env_bool("NEWS_SUMMARY_SEND_TO_CHANNEL", True)
NEWS_SUMMARY_CHANNEL_LINK = env("NEWS_SUMMARY_CHANNEL_LINK", "")

def remember_link(link: str):
    if link in _seen_links:
        return
    _seen_links.add(link)
    _seen_order.append(link)
    while len(_seen_order) > NEWS_SEEN_MAX:
        old = _seen_order.popleft()
        _seen_links.discard(old)

def normalize_title(title: str) -> str:
    return " ".join((title or "").lower().split())

def remember_title(title: str):
    if title in _seen_titles:
        return
    _seen_titles.add(title)
    _seen_titles_order.append(title)
    while len(_seen_titles_order) > NEWS_SEEN_MAX:
        old = _seen_titles_order.popleft()
        _seen_titles.discard(old)

def news_config_ok() -> bool:
    if not (NEWS_ENABLED and bool(NEWS_CHANNEL_ID) and RSS_FEEDS):
        return False
    if NEWS_USE_KEYWORDS and not URGENT_KEYWORDS:
        return False
    if not NEWS_USE_KEYWORDS and not NEWS_AI_FILTER_ENABLED:
        return False
    if NEWS_AI_FILTER_ENABLED and not NEWS_USE_KEYWORDS and not ai_configured():
        return False
    return True

def keyword_hits(text: str) -> int:
    text = (text or "").lower()
    hits = 0
    for kw in URGENT_KEYWORDS:
        if kw.lower() in text:
            hits += 1
    return hits

def urgent_by_keywords(title: str, summary: str) -> bool:
    title_hits = keyword_hits(title)
    summary_hits = keyword_hits(summary)
    if NEWS_REQUIRE_TITLE_KEYWORD and title_hits == 0:
        return False
    return (title_hits + summary_hits) >= NEWS_MIN_KEYWORDS

def _extract_json_object(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    snippet = text[start:end + 1]
    try:
        return json.loads(snippet)
    except Exception:
        return None

async def ai_contact_triage(user_id: int, question: str) -> Optional[Dict[str, object]]:
    if not ai_enabled():
        return None
    lang = get_lang(user_id)
    instructions = (
        "You are a support triage assistant.\n"
        "Decide if you can answer the user's question safely and briefly.\n"
        "If you cannot answer, set can_answer=false.\n"
        "Rules:\n"
        "1) Answer ONLY in Ukrainian or English.\n"
        "2) NEVER answer in Russian.\n"
        "3) If the user writes in Russian, answer in Ukrainian.\n"
        "4) Do NOT reveal technical details (frequencies, keys, QR, configs, onboarding steps).\n"
        "Return ONLY JSON: {\"can_answer\":true|false,\"answer\":\"...\"}\n"
        "If can_answer=false, answer must be empty string.\n"
    )
    if lang == "en":
        instructions += "Answer in English."
    else:
        instructions += "Відповідай українською."
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=instructions,
                input=(question or "").strip()[:AI_INPUT_MAX_CHARS],
            ),
            timeout=AI_TIMEOUT_SEC,
        )
        raw = (getattr(resp, "output_text", "") or "").strip()
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            return None
        can_answer = _coerce_bool(data.get("can_answer"))
        answer = str(data.get("answer") or "").strip()
        if can_answer is None:
            return None
        if not can_answer:
            return {"can_answer": False, "answer": ""}
        if not answer:
            return None
        return {"can_answer": True, "answer": answer}
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("Contact AI triage failed")
        return None

def _to_int(value) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(value.strip())
        except Exception:
            try:
                return int(round(float(value.strip())))
            except Exception:
                return None
    return None

async def ai_news_scores(title: str, summary: str) -> Optional[Dict[str, int]]:
    if not ai_enabled():
        return None
    safe_title = (title or "").strip()[:300]
    safe_summary = (summary or "").strip()[:NEWS_SUMMARY_MAX_CHARS]
    input_text = f"TITLE:\n{safe_title}\n\nSUMMARY:\n{safe_summary}"
    max_score = NEWS_AI_SCORE_SCALE if NEWS_AI_SCORE_SCALE > 0 else 5
    instructions = (
        "You are a news triage assistant.\n"
        "Rate criticality and importance on a 0-"
        f"{max_score} scale using integers.\n"
        "Return ONLY a JSON object: "
        "{\"criticality\":0,\"importance\":0}\n"
        "criticality: immediate harm/emergency risk.\n"
        "importance: broad impact/relevance/scale.\n"
        "No extra text."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=instructions,
                input=input_text,
            ),
            timeout=NEWS_AI_TIMEOUT_SEC,
        )
        raw = (getattr(resp, "output_text", "") or "").strip()
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            return None
        crit = _to_int(data.get("criticality"))
        imp = _to_int(data.get("importance"))
        if crit is None or imp is None:
            return None
        crit = max(0, min(max_score, crit))
        imp = max(0, min(max_score, imp))
        return {"criticality": crit, "importance": imp}
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("News AI scoring failed")
        return None

def news_rate_ok() -> bool:
    if NEWS_MAX_POSTS_PER_HOUR <= 0:
        return True
    now = time.time()
    while _news_sent_times and now - _news_sent_times[0] > 3600:
        _news_sent_times.popleft()
    return len(_news_sent_times) < NEWS_MAX_POSTS_PER_HOUR

def mark_news_sent():
    _news_sent_times.append(time.time())

def _summary_tzinfo():
    try:
        return ZoneInfo(NEWS_SUMMARY_TZ)
    except Exception:
        return timezone.utc

def _parse_summary_times(value: str) -> List[dt_time]:
    times: List[dt_time] = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            hh, mm = raw.split(":", 1)
            times.append(dt_time(hour=int(hh), minute=int(mm)))
        except Exception:
            continue
    if not times:
        times = [dt_time(8, 0), dt_time(14, 0), dt_time(20, 0)]
    tz = _summary_tzinfo()
    return [t.replace(tzinfo=tz) for t in times]

def _summary_channel_link() -> str:
    if NEWS_SUMMARY_CHANNEL_LINK:
        return NEWS_SUMMARY_CHANNEL_LINK
    if NEWS_CHANNEL_ID.startswith("@"):
        return f"https://t.me/{NEWS_CHANNEL_ID[1:]}"
    return ""

def _clean_html(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())

def _entry_datetime(entry) -> Optional[datetime]:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            ts = calendar.timegm(parsed)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            pass
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None) or ""
    if raw:
        try:
            dt = date_parser.parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

SUMMARY_SEEN_FILE = Path("data/summary_seen.json")
_summary_seen: Set[str] = set()
_summary_seen_order: deque[str] = deque()

def _load_summary_seen() -> None:
    if not SUMMARY_SEEN_FILE.exists():
        return
    try:
        data = json.loads(SUMMARY_SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load summary seen list")
        return
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, str):
                continue
            if item in _summary_seen:
                continue
            _summary_seen.add(item)
            _summary_seen_order.append(item)

def _save_summary_seen() -> None:
    try:
        SUMMARY_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_SEEN_FILE.write_text(json.dumps(list(_summary_seen_order)), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save summary seen list")

def _mark_summary_links(links: List[str]) -> None:
    changed = False
    for link in links:
        if link in _summary_seen:
            continue
        _summary_seen.add(link)
        _summary_seen_order.append(link)
        changed = True
        while len(_summary_seen_order) > NEWS_SUMMARY_SEEN_MAX:
            old = _summary_seen_order.popleft()
            _summary_seen.discard(old)
    if changed:
        _save_summary_seen()

_load_summary_seen()

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(chat_id=NEWS_CHANNEL_ID, text=text, disable_web_page_preview=False)

async def fetch_feed_text(client: httpx.AsyncClient, feed_url: str) -> str:
    headers = {"User-Agent": "TelegramBot/1.0"}
    url = _feed_redirects.get(feed_url, feed_url)
    r = await client.get(url, headers=headers, follow_redirects=True)
    if r.is_redirect:
        location = r.headers.get("location")
        if not location:
            r.raise_for_status()
        redirect_url = str(r.url.join(location))
        logger.info("RSS redirect: %s -> %s", url, redirect_url)
        _feed_redirects[feed_url] = redirect_url
        r = await client.get(redirect_url, headers=headers, follow_redirects=True)
    if r.status_code >= 400:
        r.raise_for_status()
    return r.text

async def news_job(context: ContextTypes.DEFAULT_TYPE):
    if not news_config_ok():
        return
    if NEWS_AI_FILTER_ENABLED and not ai_enabled() and not NEWS_USE_KEYWORDS:
        logger.warning("News AI filter enabled but AI unavailable; skipping run")
        return
    posted = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for feed_url in RSS_FEEDS:
            try:
                feed_text = await fetch_feed_text(client, feed_url)
                feed = feedparser.parse(feed_text)

                for entry in (feed.entries or [])[:10]:
                    if NEWS_MAX_POSTS_PER_RUN > 0 and posted >= NEWS_MAX_POSTS_PER_RUN:
                        return
                    if not news_rate_ok():
                        return
                    title = getattr(entry, "title", "") or ""
                    link = getattr(entry, "link", "") or ""
                    summary = getattr(entry, "summary", "") or ""
                    title_norm = normalize_title(title)

                    if not link or link in _seen_links:
                        continue
                    if not title_norm or title_norm in _seen_titles:
                        continue
                    if NEWS_USE_KEYWORDS and not urgent_by_keywords(title, summary):
                        continue

                    ai_scores = None
                    if NEWS_AI_FILTER_ENABLED:
                        ai_scores = await ai_news_scores(title, summary)
                        if ai_scores is None:
                            if NEWS_AI_STRICT or not NEWS_USE_KEYWORDS:
                                continue
                        else:
                            if (ai_scores["criticality"] < NEWS_AI_MIN_CRITICALITY or
                                    ai_scores["importance"] < NEWS_AI_MIN_IMPORTANCE):
                                continue

                    short = ""
                    if ai_enabled():
                        try:
                            safe_summary = (summary or "")[:NEWS_SUMMARY_MAX_CHARS]
                            resp = await asyncio.wait_for(
                                asyncio.to_thread(
                                    _ai_client.responses.create,
                                    model=AI_MODEL,
                                    instructions="Стисни до 2 речень українською без паніки. Без вигадок.",
                                    input=f"{title}\n{safe_summary}",
                                ),
                                timeout=NEWS_AI_TIMEOUT_SEC,
                            )
                            short = (getattr(resp, "output_text", "") or "").strip()
                        except Exception as exc:
                            if _ai_should_backoff(exc):
                                _ai_disable_temporarily("rate limit or quota")
                            logger.exception("News AI summary failed for feed %s", feed_url)
                            short = ""

                    post = "🚨 ТЕРМІНОВО\n\n" + title + "\n\n"
                    if ai_scores:
                        max_score = NEWS_AI_SCORE_SCALE if NEWS_AI_SCORE_SCALE > 0 else 5
                        post += (
                            f"🎯 Оцінка AI: критичність {ai_scores['criticality']}/{max_score}, "
                            f"важливість {ai_scores['importance']}/{max_score}\n\n"
                        )
                    if short:
                        post += "🤖 Коротко:\n" + short + "\n\n"
                    post += "🔗 Джерело: " + link

                    try:
                        await post_to_channel(context, post)
                    except Exception:
                        logger.exception("News post failed: %s", link)
                        continue
                    remember_link(link)
                    remember_title(title_norm)
                    mark_news_sent()
                    posted += 1

            except Exception:
                logger.exception("news_job error for feed %s", feed_url)
                continue

async def _collect_summary_items() -> List[Dict[str, object]]:
    if not RSS_FEEDS:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, NEWS_SUMMARY_LOOKBACK_HOURS))
    items: List[Dict[str, object]] = []
    seen: Set[str] = set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for feed_url in RSS_FEEDS:
            try:
                feed_text = await fetch_feed_text(client, feed_url)
                feed = feedparser.parse(feed_text)
                for entry in (feed.entries or [])[:30]:
                    title = getattr(entry, "title", "") or ""
                    link = getattr(entry, "link", "") or ""
                    if not title or not link or link in seen:
                        continue
                    if link in _summary_seen:
                        continue
                    published = _entry_datetime(entry)
                    if published and published < cutoff:
                        continue
                    summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                    items.append({
                        "title": title.strip(),
                        "link": link.strip(),
                        "summary": _clean_html(summary),
                        "published": published,
                    })
                    seen.add(link)
            except Exception:
                logger.exception("summary feed error for %s", feed_url)
                continue
    items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items[:max(1, NEWS_SUMMARY_MAX_ITEMS)]

async def _ai_news_summary(items: List[Dict[str, object]]) -> str:
    if not ai_enabled():
        return ""
    lines: List[str] = []
    for idx, item in enumerate(items, 1):
        title = item.get("title", "")
        summary = clip(item.get("summary", ""), 220)
        if summary:
            lines.append(f"{idx}. {title} — {summary}")
        else:
            lines.append(f"{idx}. {title}")
    input_text = "\n".join(lines)[:AI_INPUT_MAX_CHARS]
    instructions = (
        "Стисла аналітична зведення новин України.\n"
        "Поверни ТІЛЬКИ український текст у такому форматі:\n"
        "Ключові тези:\n"
        "• 4-6 коротких пунктів\n"
        "Коротка аналітика:\n"
        "2-3 короткі речення\n"
        "Без посилань і без вигадок. До 900 символів."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=instructions,
                input=input_text,
            ),
            timeout=NEWS_AI_TIMEOUT_SEC,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("AI summary failed")
        return ""

async def _build_news_summary_text() -> tuple[str, List[str]]:
    items = await _collect_summary_items()
    if not items:
        return "", []
    ai_text = await _ai_news_summary(items)
    if not ai_text:
        bullets = "\n".join([f"• {it['title']}" for it in items[:6]])
        ai_text = "Ключові тези:\n" + bullets

    header = "🗞️ Зведення новин України"
    sources: List[str] = []
    max_len = 3800
    base = header + "\n\n" + ai_text + "\n\nДжерела:\n"
    for idx, it in enumerate(items, 1):
        line = f"{idx}) {clip(it['title'], 140)} — {it['link']}"
        if len(base) + sum(len(s) + 1 for s in sources) + len(line) > max_len:
            break
        sources.append(line)
    body = base + "\n".join(sources)
    channel_link = _summary_channel_link()
    if channel_link:
        body += f"\n\n🔗 Канал: {channel_link}"
    links = [it["link"] for it in items if it.get("link")]
    return body, links

async def news_summary_job(context: ContextTypes.DEFAULT_TYPE):
    if not NEWS_SUMMARY_ENABLED:
        return
    text, links = await _build_news_summary_text()
    if not text:
        return
    delivered = False
    if NEWS_SUMMARY_SEND_TO_CHANNEL and NEWS_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=NEWS_CHANNEL_ID,
                text=text,
                disable_web_page_preview=True,
            )
            delivered = True
        except Exception:
            logger.exception("Summary post failed")
    if NEWS_SUMMARY_SEND_TO_USERS and KNOWN_USERS:
        for uid in list(KNOWN_USERS):
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    disable_web_page_preview=True,
                )
                delivered = True
            except Forbidden:
                KNOWN_USERS.discard(uid)
                _save_known_users()
            except Exception:
                logger.exception("Summary DM failed for %s", uid)
    if delivered and links:
        _mark_summary_links(links)

# =========================
# Conversation states
# =========================
(
    ASK_NAME,
    ASK_CONTACT,
    ASK_PURPOSE,
    ASK_DEVICE,
    ASK_CONFIRM,
    ASK_FAQ,
    CONTACT_QUESTION,
    CONTACT_NAME,
    CONTACT_CONTACT,
    SERVICE_PRODUCT,
    SERVICE_SERIAL,
    SERVICE_CONTACT,
) = range(12)

# =========================
# Command handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    uid = update.effective_user.id
    name = display_name(update.effective_user)
    greet = greeting_text(uid, name)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        f"{greet}\n\n{C(uid,'menu')}",
        reply_markup=menu_kb(uid),
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "cancel"),
        reply_markup=menu_only_kb(uid),
    )
    return ConversationHandler.END

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (
        f"OK\n"
        f"AI={'on' if ai_enabled() else 'off'}\n"
        f"NEWS={'on' if news_config_ok() else 'off'}\n"
        f"NEWS_SUMMARY={'on' if NEWS_SUMMARY_ENABLED else 'off'}\n"
        f"ALERTS={'on' if ua_alarm_enabled() else 'off'}"
    )
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        txt,
        reply_markup=menu_only_kb(uid),
    )

async def test_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_user.id != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not NEWS_CHANNEL_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "NEWS_CHANNEL_ID не задан.", "NEWS_CHANNEL_ID is not set."),
            reply_markup=menu_only_kb(uid),
        )
        return
    try:
        await context.bot.send_message(chat_id=NEWS_CHANNEL_ID, text="✅ TEST: бот може писати в канал.")
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "✅ Тестове повідомлення відправлено в канал.", "✅ Test message sent to channel."),
            reply_markup=menu_only_kb(uid),
        )
    except Exception as e:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            f"❌ {e}",
            reply_markup=menu_only_kb(uid),
        )

async def regions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
        )
        return
    query = " ".join(context.args or []).strip().lower()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            data = await ua_get_json(UA_ALARM_REGIONS_PATH, client=client)
    except Exception:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "❌ Не вдалося отримати список регіонів.", "❌ Failed to fetch regions list."),
        )
        return

    items = data if isinstance(data, list) else data.get("regions") or data.get("data") or []
    lines = []
    for it in items:
        name = (it.get("name") or it.get("title") or "").strip()
        rid = str(it.get("regionId") or it.get("id") or it.get("region_id") or "").strip()
        if not name and not rid:
            continue
        if query and query not in name.lower():
            continue
        if rid and name:
            lines.append(f"{rid} — {name}")
        else:
            lines.append(name or rid)

    if not lines:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "ℹ️ Регіони не знайдені за запитом.", "ℹ️ No regions found for query."),
        )
        return

    prefix = "Регіони:\n" if get_lang(uid) == "uk" else "Regions:\n"
    max_len = 3500
    chunk = prefix
    chunks: List[str] = []
    for line in lines:
        if len(chunk) + len(line) + 1 > max_len:
            chunks.append(chunk)
            chunk = prefix + line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        chunks.append(chunk)

    for idx, out in enumerate(chunks):
        kwargs = {}
        if idx == len(chunks) - 1:
            kwargs["reply_markup"] = menu_only_kb(uid)
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            out,
            **kwargs,
        )

# =========================
# Menu callback handler (menu/info/news)
# =========================
async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "lang:menu":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "choose_lang"),
            reply_markup=lang_kb(),
        )
        return

    if data.startswith("lang:set:"):
        _, _, lng = data.split(":")
        USER_LANG[uid] = "en" if lng == "en" else "uk"
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "lang_saved"),
        )
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "menu"),
            reply_markup=menu_kb(uid),
        )
        return

    if data == "menu:back":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "menu"),
            reply_markup=menu_kb(uid),
        )
        return

    if data == "info:company":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "company"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=menu_only_kb(uid),
        )
        return
    if data == "info:contact":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "contact"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=contact_kb(uid),
        )
        return
    if data == "info:service":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "service"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=service_kb(uid),
        )
        return
    if data in ("info:products", "products:menu"):
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "products"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=products_kb(uid),
        )
        return
    if data == "info:system":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "system"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=menu_only_kb(uid),
        )
        return
    if data == "info:rules":
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "rules"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=menu_only_kb(uid),
        )
        return


# =========================
# Alerts callbacks
# =========================
async def alerts_show_menu(q, uid: int, bot):
    if not ua_alarm_enabled():
        await send_with_cleanup(
            bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "alerts_no_key"),
        )
        return
    txt = await alerts_status_text(uid)
    await send_with_cleanup(
        bot,
        q.message.chat_id,
        q.message.reply_text,
        txt,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=alerts_menu_kb(uid),
    )

async def alerts_oblast_menu(q, uid: int, bot, page: int = 0):
    if not ua_alarm_enabled():
        await send_with_cleanup(
            bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "alerts_no_key"),
        )
        return
    try:
        await ua_load_regions()
    except Exception:
        await send_with_cleanup(
            bot,
            q.message.chat_id,
            q.message.reply_text,
            t(uid, "❌ Не вдалося отримати список областей.", "❌ Failed to fetch oblasts."),
        )
        return
    items = region_items_by_type(uid, "oblast")
    if not items:
        fallback: List[tuple[str, str]] = []
        for rid in REGION_NAME_UA_BY_ID.keys():
            name = (REGION_NAME_UA_BY_ID.get(rid, "") + " " + REGION_NAME_EN_BY_ID.get(rid, "")).lower()
            if "область" in name or "oblast" in name:
                fallback.append((rid, region_display_name(uid, rid)))
        fallback.sort(key=lambda x: x[1].lower())
        items = fallback
    if not items:
        await send_with_cleanup(
            bot,
            q.message.chat_id,
            q.message.reply_text,
            t(uid, "❌ Не знайдено областей.", "❌ No oblasts found."),
        )
        return
    kb = regions_list_kb(uid, items, page, "alerts:oblast", "alerts:menu")
    await send_with_cleanup(
        bot,
        q.message.chat_id,
        q.message.reply_text,
        t(uid, "Оберіть область:", "Choose an oblast:"),
        reply_markup=kb,
    )

async def alerts_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "alerts:menu":
        await alerts_show_menu(q, uid, context.bot)
        return

    if data == "alerts:toggle":
        if not ua_alarm_enabled():
            await send_with_cleanup(
                context.bot,
                q.message.chat_id,
                q.message.reply_text,
                C(uid, "alerts_no_key"),
            )
            return
        on = ALERTS_ENABLED.get(uid, False)
        if on:
            ALERTS_ENABLED[uid] = False
            ALERT_LAST_USER_STATE.pop(uid, None)
            await send_with_cleanup(
                context.bot,
                q.message.chat_id,
                q.message.reply_text,
                t(uid, "✅ Сповіщення вимкнено.", "✅ Alerts disabled."),
            )
            await alerts_show_menu(q, uid, context.bot)
            return
        if not ALERT_OBLAST.get(uid) and not ALERT_REGION.get(uid):
            try:
                rids = await ua_region_ids()
            except Exception:
                await send_with_cleanup(
                    context.bot,
                    q.message.chat_id,
                    q.message.reply_text,
                    t(uid, "⚠️ Не вдалося увімкнути тривоги (помилка конфігурації/API).",
                       "⚠️ Could not enable alerts (config/API error)."),
                )
                return
            if rids:
                ALERT_REGION[uid] = rids
                if len(rids) == 1:
                    ALERT_OBLAST[uid] = rids[0]
        ALERTS_ENABLED[uid] = True
        ALERT_LAST_USER_STATE.pop(uid, None)
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            t(uid, "✅ Сповіщення увімкнено.", "✅ Alerts enabled."),
        )
        if not ALERT_OBLAST.get(uid):
            await alerts_oblast_menu(q, uid, context.bot, 0)
            return
        sync_alert_regions(uid)
        await alerts_show_menu(q, uid, context.bot)
        return

    if data.startswith("alerts:oblast:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        action = parts[2]
        if action == "menu":
            await alerts_oblast_menu(q, uid, context.bot, 0)
            return
        if action == "page" and len(parts) == 4:
            try:
                page = int(parts[3])
            except Exception:
                page = 0
            await alerts_oblast_menu(q, uid, context.bot, page)
            return
        if action == "set" and len(parts) == 4:
            rid = parts[3]
            ALERT_OBLAST[uid] = rid
            sync_alert_regions(uid)
            ALERT_LAST_USER_STATE.pop(uid, None)
            name = region_display_name(uid, rid) or rid
            await send_with_cleanup(
                context.bot,
                q.message.chat_id,
                q.message.reply_text,
                t(uid, f"✅ Область встановлено: {name}.", f"✅ Oblast set: {name}."),
            )
            await alerts_show_menu(q, uid, context.bot)
            return

# =========================
# Product callbacks
# =========================
async def product_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    _, key = data.split(":", 1)
    prod = PRODUCTS.get(key)
    if not prod:
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "products_not_found"),
            reply_markup=products_kb(uid),
        )
        return

    caption = product_caption(uid, prod)
    image_path = PRODUCT_IMAGES_DIR / prod.image_name
    if image_path.is_file():
        with open(image_path, "rb") as photo:
            await send_with_cleanup(
                context.bot,
                q.message.chat_id,
                q.message.reply_photo,
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=product_detail_kb(uid),
            )
        return

    await send_with_cleanup(
        context.bot,
        q.message.chat_id,
        q.message.reply_text,
        caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=product_detail_kb(uid),
    )

# =========================
# Apply conversation handlers
# =========================
async def apply_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    now = time.time()

    last = _last_apply.get(uid, 0.0)
    if now - last < COOLDOWN_SEC:
        remain = int(max(1, COOLDOWN_SEC - (now - last)))
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "cooldown").format(sec=remain),
        )
        return ConversationHandler.END

    _last_apply[uid] = now
    await send_with_cleanup(
        context.bot,
        q.message.chat_id,
        q.message.reply_text,
        C(uid, "apply_intro"),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_NAME

async def apply_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = (update.message.text or "").strip()
    if not name:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Вкажіть імʼя текстом.", "Please provide your name as text."),
        )
        return ASK_NAME

    context.user_data["name"] = clip(name, 100)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "ask_contact"),
    )
    return ASK_CONTACT

async def apply_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    contact = (update.message.text or "").strip()
    if not contact:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Вкажіть контакт текстом.", "Please provide a contact as text."),
        )
        return ASK_CONTACT

    context.user_data["contact"] = clip(contact, 120)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "ask_purpose"),
    )
    return ASK_PURPOSE

async def apply_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    purpose = (update.message.text or "").strip()
    if not purpose:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Напишіть мету одним рядком.", "Please write purpose in one short line."),
        )
        return ASK_PURPOSE

    context.user_data["purpose"] = clip(purpose, 300)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "ask_device"),
    )
    return ASK_DEVICE

async def apply_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    device = (update.message.text or "").strip()
    if not device:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Вкажіть пристрій текстом.", "Please specify the device as text."),
        )
        return ASK_DEVICE

    context.user_data["device"] = clip(device, 200)
    word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    msg = C(uid, "confirm").replace("ПІДТВЕРДЖУЮ", word).replace("CONFIRM", word)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        msg,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_CONFIRM

async def apply_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip().upper()
    word = "ПІДТВЕРДЖУЮ" if get_lang(uid) == "uk" else "CONFIRM"
    if txt != word:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "cancel"),
            reply_markup=menu_only_kb(uid),
        )
        return ConversationHandler.END

    u = update.effective_user
    key = secrets.token_hex(8)
    req = AccessRequest(
        key=key,
        user_id=u.id,
        chat_id=update.effective_chat.id,
        who=who(u),
        name=context.user_data.get("name", ""),
        contact=context.user_data.get("contact", ""),
        purpose=context.user_data.get("purpose", ""),
        device=context.user_data.get("device", ""),
        ts=time.time(),
    )
    PENDING[key] = req

    reco = "AI: (disabled)"
    if ai_enabled():
        reco = await ask_ai(
            ADMIN_ID,
            f"Користувач: {req.who}\nІмʼя: {req.name}\nКонтакт: {req.contact}\nМета: {req.purpose}\nПристрій: {req.device}",
            mode="admin",
        )

    # ВАЖНО: без Markdown/HTML, чтобы не ломалось от пользовательского ввода
    admin_text = (
        "🆕 ЗАЯВКА\n\n"
        f"👤 {req.who}\n"
        f"🧑 {req.name}\n"
        f"📞 {req.contact}\n"
        f"🎯 {req.purpose}\n"
        f"📦 {req.device}\n\n"
        f"🤖 AI\n{reco}\n\n"
        f"ID: {req.user_id}"
    )

    delivered = True
    try:
        await send_with_cleanup(
            context.bot,
            ADMIN_ID,
            context.bot.send_message,
            chat_id=ADMIN_ID,
            text=admin_text,
            reply_markup=admin_kb(key),
        )
    except Exception:
        delivered = False

    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "sent") if delivered else C(uid, "sent_admin_fail"),
        reply_markup=menu_only_kb(uid),
    )
    return ConversationHandler.END

# =========================
# Contact form handlers (AI triage)
# =========================
async def contact_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    await send_with_cleanup(
        context.bot,
        q.message.chat_id,
        q.message.reply_text,
        C(uid, "contact_form_question"),
    )
    return CONTACT_QUESTION

async def contact_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    question = (update.message.text or "").strip()
    if not question:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "contact_form_question"),
        )
        return CONTACT_QUESTION
    context.user_data["contact_question"] = clip(question, 800)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "contact_form_name"),
    )
    return CONTACT_NAME

async def contact_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = (update.message.text or "").strip()
    if not name:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "contact_form_name"),
        )
        return CONTACT_NAME
    context.user_data["contact_name"] = clip(name, 100)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "contact_form_contact"),
    )
    return CONTACT_CONTACT

async def contact_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    contact_info = (update.message.text or "").strip()
    if not contact_info:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "contact_form_contact"),
        )
        return CONTACT_CONTACT

    context.user_data["contact_info"] = clip(contact_info, 120)
    question = context.user_data.get("contact_question", "")
    name = context.user_data.get("contact_name", "")

    ai_result = await ai_contact_triage(uid, question)
    if ai_result and ai_result.get("can_answer") and ai_result.get("answer"):
        answer = clip(str(ai_result["answer"]), 1500)
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            answer,
            reply_markup=menu_only_kb(uid),
        )
        return ConversationHandler.END

    admin_text = (
        "📩 КОНТАКТ ЗАПИТ\n\n"
        f"👤 {who(update.effective_user)}\n"
        f"🧑 {name}\n"
        f"📞 {context.user_data.get('contact_info', '')}\n"
        f"❓ {question}\n\n"
        f"ID: {uid}"
    )
    delivered = True
    try:
        await send_with_cleanup(
            context.bot,
            ADMIN_ID,
            context.bot.send_message,
            chat_id=ADMIN_ID,
            text=admin_text,
        )
    except Exception:
        delivered = False

    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "contact_sent") if delivered else C(uid, "contact_sent_admin_fail"),
        reply_markup=menu_only_kb(uid),
    )
    return ConversationHandler.END

# =========================
# Service form handlers
# =========================
async def service_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    await send_with_cleanup(
        context.bot,
        q.message.chat_id,
        q.message.reply_text,
        C(uid, "service_form_product"),
    )
    return SERVICE_PRODUCT

async def service_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    product = (update.message.text or "").strip()
    if not product:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "service_form_product"),
        )
        return SERVICE_PRODUCT
    context.user_data["service_product"] = clip(product, 120)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "service_form_serial"),
    )
    return SERVICE_SERIAL

async def service_serial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    serial = (update.message.text or "").strip()
    if not serial:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "service_form_serial"),
        )
        return SERVICE_SERIAL
    context.user_data["service_serial"] = clip(serial, 120)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "service_form_contact"),
    )
    return SERVICE_CONTACT

async def service_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    contact_info = (update.message.text or "").strip()
    if not contact_info:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            C(uid, "service_form_contact"),
        )
        return SERVICE_CONTACT

    context.user_data["service_contact"] = clip(contact_info, 120)
    admin_text = (
        "🛠️ СЕРВІСНА ЗАЯВКА\n\n"
        f"👤 {who(update.effective_user)}\n"
        f"🛠️ {context.user_data.get('service_product', '')}\n"
        f"🔢 {context.user_data.get('service_serial', '')}\n"
        f"📞 {context.user_data.get('service_contact', '')}\n\n"
        f"ID: {uid}"
    )
    delivered = True
    try:
        await send_with_cleanup(
            context.bot,
            ADMIN_ID,
            context.bot.send_message,
            chat_id=ADMIN_ID,
            text=admin_text,
        )
    except Exception:
        delivered = False

    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        C(uid, "service_sent") if delivered else C(uid, "service_sent_admin_fail"),
        reply_markup=menu_only_kb(uid),
    )
    return ConversationHandler.END

# =========================
# FAQ conversation handlers
# =========================
async def faq_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update)
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    now = time.time()

    last = _last_ai.get(uid, 0.0)
    if now - last < AI_COOLDOWN_SEC:
        remain = int(max(1, AI_COOLDOWN_SEC - (now - last)))
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            C(uid, "cooldown").format(sec=remain),
        )
        return ConversationHandler.END

    _last_ai[uid] = now
    await send_with_cleanup(
        context.bot,
        q.message.chat_id,
        q.message.reply_text,
        C(uid, "faq_hint"),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_FAQ

async def faq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    q = (update.message.text or "").strip()
    if not q:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Напишіть питання текстом.", "Please send a text question."),
        )
        return ASK_FAQ
    ans = await ask_ai(uid, q, mode="faq")
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        ans,
        reply_markup=menu_only_kb(uid),
    )
    return ConversationHandler.END

# =========================
# Admin callbacks
# =========================
async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.from_user.id != ADMIN_ID:
        uid = q.from_user.id
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
        )
        return

    _, action, key = q.data.split(":", 2)
    req = PENDING.pop(key, None)
    if not req:
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            "ℹ️ Already processed / request not found.",
        )
        return

    if action == "approve":
        await send_with_cleanup(
            context.bot,
            req.chat_id,
            context.bot.send_message,
            chat_id=req.chat_id,
            text=t(
                req.user_id,
                "✅ Ваш запит **схвалено**. Інструкції/доступ надасть адміністратор.",
                "✅ Your request is **approved**. The admin will provide onboarding/access.",
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=menu_only_kb(req.user_id),
        )
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            f"✅ Approved: {req.who}",
        )
    else:
        await send_with_cleanup(
            context.bot,
            req.chat_id,
            context.bot.send_message,
            chat_id=req.chat_id,
            text=t(
                req.user_id,
                "❌ Ваш запит **відхилено**. Спробуйте пізніше.",
                "❌ Your request is **denied**. Please try later.",
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=menu_only_kb(req.user_id),
        )
        await send_with_cleanup(
            context.bot,
            q.message.chat_id,
            q.message.reply_text,
            f"❌ Denied: {req.who}",
        )

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
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Failed to delete webhook")
    if news_config_ok():
        application.job_queue.run_repeating(news_job, interval=NEWS_POLL_SEC, first=15)
    if NEWS_SUMMARY_ENABLED:
        for t in _parse_summary_times(NEWS_SUMMARY_TIMES):
            application.job_queue.run_daily(news_summary_job, time=t)
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
    app.add_handler(CommandHandler("regions", regions_cmd))

    # menu/info callbacks only
    app.add_handler(CallbackQueryHandler(
        menu_cb,
        pattern=r"^(lang:menu|lang:set:(uk|en)|menu:back|info:company|info:contact|info:service|info:products|products:menu|info:system|info:rules)$"
    ))

    app.add_handler(CallbackQueryHandler(alerts_cb, pattern=r"^alerts:"))

    app.add_handler(CallbackQueryHandler(product_cb, pattern=r"^prod:"))

    # admin callbacks
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:(approve|deny):"))

    # apply conversation (entry is button only)
    apply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(apply_start_cb, pattern=r"^apply:start$")],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_name)],
            ASK_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_contact)],
            ASK_PURPOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_purpose)],
            ASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_device)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, apply_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        # per_message=False (default): важно, потому что после callback идут обычные сообщения
    )

    # contact form conversation (entry is button only)
    contact_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(contact_start_cb, pattern=r"^contact:form$")],
        states={
            CONTACT_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_question)],
            CONTACT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_name)],
            CONTACT_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        # per_message=False (default)
    )

    # service form conversation (entry is button only)
    service_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(service_start_cb, pattern=r"^service:form$")],
        states={
            SERVICE_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, service_product)],
            SERVICE_SERIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, service_serial)],
            SERVICE_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, service_contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        # per_message=False (default)
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
    app.add_handler(contact_conv)
    app.add_handler(service_conv)
    app.add_handler(faq_conv)

    app.add_error_handler(error_handler)

    # IMPORTANT: if you changed token or had conflicts, this helps after restart
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
