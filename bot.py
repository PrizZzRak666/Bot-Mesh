import os
import atexit
import time
import secrets
import asyncio
import logging
import json
import random
import re
import calendar
import base64
import io
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import feedparser
from dateutil import parser as date_parser

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
BASE_DIR = Path(__file__).resolve().parent

def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else BASE_DIR / path

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

def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
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
# Instance lock (avoid multiple pollers)
# =========================
INSTANCE_LOCK_PATH = env("INSTANCE_LOCK_PATH", "data/bot.lock")
INSTANCE_LOCK_ENABLED = env_bool("INSTANCE_LOCK_ENABLED", True)
_instance_lock_handle = None

def _acquire_instance_lock() -> None:
    global _instance_lock_handle
    if not INSTANCE_LOCK_ENABLED:
        logger.info("Instance lock disabled")
        return
    lock_path = _resolve_path(INSTANCE_LOCK_PATH)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to prepare lock directory: %s", lock_path.parent)
        return
    try:
        import fcntl
    except Exception:
        logger.warning("Instance lock unavailable (fcntl not supported)")
        return
    try:
        handle = open(lock_path, "a+")
    except Exception:
        logger.exception("Failed to open lock file: %s", lock_path)
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another bot instance is already running (lock: %s)", lock_path)
        raise SystemExit(1)
    except Exception:
        logger.exception("Failed to acquire instance lock: %s", lock_path)
        handle.close()
        return
    _instance_lock_handle = handle

    def _release_lock() -> None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass

    atexit.register(_release_lock)

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
AI_TIMEOUT_SEC = env_int("AI_TIMEOUT_SEC", 30)
AI_INPUT_MAX_CHARS = env_int("AI_INPUT_MAX_CHARS", 3000)
AI_TIMEOUT_BACKOFF_COUNT = env_int("AI_TIMEOUT_BACKOFF_COUNT", 3)
AI_TIMEOUT_BACKOFF_WINDOW_SEC = env_int("AI_TIMEOUT_BACKOFF_WINDOW_SEC", 300)

_ai_client = None
_ai_disabled_until = 0.0
_ai_timeout_hits: deque[float] = deque()
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

def _ai_status_reason() -> str:
    if _ai_client is None:
        return "not configured"
    if time.time() < _ai_disabled_until:
        return "temporarily disabled"
    return ""

def _ai_should_backoff(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "insufficient_quota" in msg or "quota" in msg or "429" in msg

def _ai_disable_temporarily(reason: str):
    global _ai_disabled_until
    if AI_TEMP_DISABLE_SEC <= 0:
        return
    _ai_disabled_until = max(_ai_disabled_until, time.time() + AI_TEMP_DISABLE_SEC)
    logger.warning("AI temporarily disabled for %ss: %s", AI_TEMP_DISABLE_SEC, reason)

def _ai_register_timeout(context: str) -> None:
    if AI_TIMEOUT_BACKOFF_COUNT <= 0 or AI_TIMEOUT_BACKOFF_WINDOW_SEC <= 0:
        return
    now = time.time()
    _ai_timeout_hits.append(now)
    while _ai_timeout_hits and now - _ai_timeout_hits[0] > AI_TIMEOUT_BACKOFF_WINDOW_SEC:
        _ai_timeout_hits.popleft()
    if len(_ai_timeout_hits) >= AI_TIMEOUT_BACKOFF_COUNT:
        _ai_disable_temporarily(f"timeouts in {context}")
        _ai_timeout_hits.clear()

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
        logger.warning("AI request ignored: %s", _ai_status_reason())
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")
    try:
        safe_text = (text or "").strip()[:AI_INPUT_MAX_CHARS]
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=ai_instructions(user_id, mode),
                input=safe_text,
                timeout=AI_TIMEOUT_SEC,
            ),
            timeout=AI_TIMEOUT_SEC,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out or t(user_id, "ℹ️ Немає відповіді.", "ℹ️ No answer.")
    except asyncio.TimeoutError:
        _ai_register_timeout("ask_ai")
        logger.warning("AI request timed out (user_id=%s mode=%s)", user_id, mode)
        return t(user_id, "ℹ️ AI тимчасово недоступний.", "ℹ️ AI is currently unavailable.")
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
    if "text" in kwargs and isinstance(kwargs["text"], str):
        kwargs["text"] = _append_footer(kwargs["text"], cleanup_chat_id)
    elif args and isinstance(args[0], str):
        args = list(args)
        args[0] = _append_footer(args[0], cleanup_chat_id)
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
NEWS_AI_TIMEOUT_SEC = env_int("NEWS_AI_TIMEOUT_SEC", 20)
NEWS_AI_SCORE_BUDGET_SEC = env_int("NEWS_AI_SCORE_BUDGET_SEC", max(10, NEWS_POLL_SEC - 60))
NEWS_USE_KEYWORDS = env_bool("NEWS_USE_KEYWORDS", False)
NEWS_FALLBACK_KEYWORDS = env_bool("NEWS_FALLBACK_KEYWORDS", True)
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
NEWS_AI_MAX_CANDIDATES = env_int("NEWS_AI_MAX_CANDIDATES", 30)
NEWS_MAX_AGE_HOURS = env_int("NEWS_MAX_AGE_HOURS", 24)
NEWS_INTEREST_KEYWORDS = [k.strip() for k in env("NEWS_INTEREST_KEYWORDS", "").split(",") if k.strip()]
NEWS_INTEREST_BOOST = env_int("NEWS_INTEREST_BOOST", 2)
NEWS_RECENCY_BOOST = env_int("NEWS_RECENCY_BOOST", 6)
NEWS_TITLE_SIM_THRESHOLD = env_float("NEWS_TITLE_SIM_THRESHOLD", 0.82)
NEWS_TITLE_SIM_MIN_TOKENS = env_int("NEWS_TITLE_SIM_MIN_TOKENS", 4)
NEWS_TITLE_SIM_SEEN_MAX = env_int("NEWS_TITLE_SIM_SEEN_MAX", 800)
NEWS_WHITELIST_KEYWORDS = [k.strip() for k in env("NEWS_WHITELIST_KEYWORDS", "").split(",") if k.strip()]
NEWS_WHITELIST_BOOST = env_int("NEWS_WHITELIST_BOOST", 3)
NEWS_BLOCK_KEYWORDS = [k.strip() for k in env("NEWS_BLOCK_KEYWORDS", "").split(",") if k.strip()]
NEWS_ALLOW_FEEDS = [u.strip() for u in env("NEWS_ALLOW_FEEDS", "").split(",") if u.strip()]
NEWS_BLOCK_FEEDS = [u.strip() for u in env("NEWS_BLOCK_FEEDS", "").split(",") if u.strip()]
NEWS_PRIORITY_FEEDS = [u.strip() for u in env("NEWS_PRIORITY_FEEDS", "").split(",") if u.strip()]
NEWS_PRIORITY_FEED_BOOST = env_int("NEWS_PRIORITY_FEED_BOOST", 2)
NEWS_DEPRIORITY_KEYWORDS = [
    k.strip() for k in env(
        "NEWS_DEPRIORITY_KEYWORDS",
        "відео,відеорепортаж,відеоогляд,фото,фоторепортаж,галерея,стрім,онлайн,live,stream,video,photo,infographic"
    ).split(",") if k.strip()
]
NEWS_DEPRIORITY_PENALTY = env_int("NEWS_DEPRIORITY_PENALTY", 4)
NEWS_MAX_URGENT_PER_HOUR = env_int("NEWS_MAX_URGENT_PER_HOUR", 8)
NEWS_FEED_FAIL_THRESHOLD = env_int("NEWS_FEED_FAIL_THRESHOLD", 3)
NEWS_FEED_EMPTY_THRESHOLD = env_int("NEWS_FEED_EMPTY_THRESHOLD", 4)
NEWS_FEED_DISABLE_SEC = env_int("NEWS_FEED_DISABLE_SEC", 900)
NEWS_BLAST_KEYWORDS = [k.strip() for k in env("NEWS_BLAST_KEYWORDS", "вибух,вибухи").split(",") if k.strip()]
NEWS_BLAST_CONTEXT_KEYWORDS = [
    k.strip() for k in env(
        "NEWS_BLAST_CONTEXT_KEYWORDS",
        "атака,атаки,удар,удари,обстріл,обстрел,ракета,ракети,ракеты,дрон,дрони,дроны,"
        "ППО,ПВО,тривога,тревога,відбій,отбой,приліт,прилет"
    ).split(",") if k.strip()
]
_seen_links: Set[str] = set()
_seen_order: deque[str] = deque()
_feed_redirects: Dict[str, str] = {}
_feed_cache: Dict[str, Dict[str, str]] = {}
_feed_health: Dict[str, Dict[str, object]] = {}
_seen_titles: Set[str] = set()
_seen_titles_order: deque[str] = deque()
_seen_title_tokens_sigs: Set[str] = set()
_seen_title_tokens_order: deque[str] = deque()
_seen_title_tokens_sets: deque[Set[str]] = deque()
_news_sent_times: deque[float] = deque()
_news_urgent_sent_times: deque[float] = deque()
_news_job_lock = asyncio.Lock()

NEWS_SUMMARY_ENABLED = env_bool("NEWS_SUMMARY_ENABLED", False)
NEWS_SUMMARY_TIMES = env("NEWS_SUMMARY_TIMES", "08:00,14:00,20:00")
NEWS_SUMMARY_TZ = env("NEWS_SUMMARY_TZ", "Europe/Kyiv")
NEWS_SUMMARY_LOOKBACK_HOURS = env_int("NEWS_SUMMARY_LOOKBACK_HOURS", 8)
NEWS_SUMMARY_MAX_ITEMS = env_int("NEWS_SUMMARY_MAX_ITEMS", 12)
NEWS_SUMMARY_SEEN_MAX = env_int("NEWS_SUMMARY_SEEN_MAX", 2000)
NEWS_SUMMARY_SEND_TO_CHANNEL = env_bool("NEWS_SUMMARY_SEND_TO_CHANNEL", True)
NEWS_SUMMARY_CHANNEL_LINK = env("NEWS_SUMMARY_CHANNEL_LINK", "")

NEWS_STATS_ENABLED = env_bool("NEWS_STATS_ENABLED", True)
NEWS_STATS_RETENTION_HOURS = env_int("NEWS_STATS_RETENTION_HOURS", 168)
NEWS_SKIP_STATS_ENABLED = env_bool("NEWS_SKIP_STATS_ENABLED", True)
NEWS_SKIP_STATS_RETENTION_HOURS = env_int("NEWS_SKIP_STATS_RETENTION_HOURS", 168)
NEWS_KEYWORD_SUGGEST_MIN_FREQ = env_int("NEWS_KEYWORD_SUGGEST_MIN_FREQ", 3)
NEWS_KEYWORD_SUGGEST_MAX = env_int("NEWS_KEYWORD_SUGGEST_MAX", 20)

FOOTER_ENABLED = env_bool("FOOTER_ENABLED", False)
FOOTER_BOT_LINK = env("FOOTER_BOT_LINK", "")
FOOTER_CHANNEL_LINK = env("FOOTER_CHANNEL_LINK", "")
FOOTER_SITE_LINK = env("FOOTER_SITE_LINK", "https://www.ukrainianaviation.com")
BOT_PUBLIC_LINK = FOOTER_BOT_LINK

CHANNEL_POSTS_ENABLED = env_bool("CHANNEL_POSTS_ENABLED", False)
CHANNEL_POSTS_INTERVAL_SEC = env_int("CHANNEL_POSTS_INTERVAL_SEC", 3600)
CHANNEL_POSTS_LANG = env("CHANNEL_POSTS_LANG", "uk").strip().lower()
CHANNEL_POSTS_TZ = env("CHANNEL_POSTS_TZ", NEWS_SUMMARY_TZ)
CHANNEL_POSTS_TIMES = env("CHANNEL_POSTS_TIMES", "").strip()
CHANNEL_POSTS_USE_WEEKLY_PLAN = env_bool("CHANNEL_POSTS_USE_WEEKLY_PLAN", True)
CHANNEL_POSTS_TOPICS_RAW = env("CHANNEL_POSTS_TOPICS", "").strip()
CHANNEL_POSTS_TOPICS_FILE = env("CHANNEL_POSTS_TOPICS_FILE", "channel_topics.txt").strip()
CHANNEL_POSTS_IMAGE_ENABLED = env_bool(
    "CHANNEL_POSTS_IMAGE_ENABLED",
    env_bool("NEWS_IMAGE_ENABLED", False),
)
CHANNEL_POSTS_IMAGE_MODEL = env("CHANNEL_POSTS_IMAGE_MODEL", "gpt-image-1")
CHANNEL_POSTS_IMAGE_SIZE = env("CHANNEL_POSTS_IMAGE_SIZE", "1024x1024")
CHANNEL_POSTS_IMAGE_TIMEOUT_SEC = env_int("CHANNEL_POSTS_IMAGE_TIMEOUT_SEC", 20)
CHANNEL_POSTS_IMAGE_TEMP_DISABLE_SEC = env_int("CHANNEL_POSTS_IMAGE_TEMP_DISABLE_SEC", 900)
CHANNEL_POSTS_IMAGE_STYLES = env(
    "CHANNEL_POSTS_IMAGE_STYLES",
    "flat-vector,soft-gradient,paper-cut,monoline,grainy-duotone",
)
CHANNEL_POSTS_ACTION_HISTORY_MAX = env_int("CHANNEL_POSTS_ACTION_HISTORY_MAX", 120)
CHANNEL_POSTS_ACTION_REPEAT_MAX = env_int("CHANNEL_POSTS_ACTION_REPEAT_MAX", 0)
CHANNEL_POSTS_ACTION_MIN_WORDS = env_int("CHANNEL_POSTS_ACTION_MIN_WORDS", 2)
CHANNEL_POSTS_ACTION_MAX_WORDS = env_int("CHANNEL_POSTS_ACTION_MAX_WORDS", 10)
CHANNEL_POSTS_TOPIC_REPEAT_HOURS = env_int("CHANNEL_POSTS_TOPIC_REPEAT_HOURS", 48)
CHANNEL_POSTS_TOPIC_HISTORY_MAX = env_int("CHANNEL_POSTS_TOPIC_HISTORY_MAX", 300)

MEME_POSTS_ENABLED = env_bool("MEME_POSTS_ENABLED", False)
MEME_POSTS_INTERVAL_SEC = env_int("MEME_POSTS_INTERVAL_SEC", 7200)
MEME_IMAGE_MODEL = env("MEME_IMAGE_MODEL", "gpt-image-1")
MEME_IMAGE_SIZE = env("MEME_IMAGE_SIZE", "1024x1024")
MEME_IMAGE_TIMEOUT_SEC = env_int("MEME_IMAGE_TIMEOUT_SEC", 25)
MEME_IMAGE_TEMP_DISABLE_SEC = env_int("MEME_IMAGE_TEMP_DISABLE_SEC", 900)
MEME_IMAGE_STYLES = env(
    "MEME_IMAGE_STYLES",
    "comic-pop,sticker-collage,flat-vector,soft-gradient,grainy-duotone,paper-cut",
)

NEWS_IMAGE_ENABLED = env_bool("NEWS_IMAGE_ENABLED", False)
NEWS_IMAGE_MODEL = env("NEWS_IMAGE_MODEL", "gpt-image-1")
NEWS_IMAGE_SIZE = env("NEWS_IMAGE_SIZE", "1024x1024")
NEWS_IMAGE_TIMEOUT_SEC = env_int("NEWS_IMAGE_TIMEOUT_SEC", 20)
NEWS_IMAGE_TEMP_DISABLE_SEC = env_int("NEWS_IMAGE_TEMP_DISABLE_SEC", 900)
NEWS_IMAGE_STYLES = env(
    "NEWS_IMAGE_STYLES",
    "editorial-ink,cinematic-glow,comic-pop,poster-pop,soft-gradient,grainy-duotone,monoline",
)
NEWS_SUMMARY_IMAGE_STYLES = env("NEWS_SUMMARY_IMAGE_STYLES", "")
_news_images_disabled_until = 0.0
_meme_images_disabled_until = 0.0

def _news_image_skip_reason() -> str:
    if not NEWS_IMAGE_ENABLED:
        return "NEWS_IMAGE_ENABLED=false"
    if time.time() < _news_images_disabled_until:
        return "temporarily disabled"
    if not ai_enabled():
        return "AI unavailable"
    return ""

def _keyword_fallback_enabled() -> bool:
    return bool(URGENT_KEYWORDS) and (NEWS_USE_KEYWORDS or NEWS_FALLBACK_KEYWORDS)

def _disable_news_images_temporarily(reason: str) -> None:
    global _news_images_disabled_until
    if NEWS_IMAGE_TEMP_DISABLE_SEC <= 0:
        return
    _news_images_disabled_until = max(_news_images_disabled_until, time.time() + NEWS_IMAGE_TEMP_DISABLE_SEC)
    logger.warning("News images temporarily disabled for %ss: %s", NEWS_IMAGE_TEMP_DISABLE_SEC, reason)

def _meme_image_skip_reason() -> str:
    if not MEME_POSTS_ENABLED:
        return "MEME_POSTS_ENABLED=false"
    if time.time() < _meme_images_disabled_until:
        return "temporarily disabled"
    if not ai_enabled():
        return "AI unavailable"
    return ""

def _disable_meme_images_temporarily(reason: str) -> None:
    global _meme_images_disabled_until
    if MEME_IMAGE_TEMP_DISABLE_SEC <= 0:
        return
    _meme_images_disabled_until = max(_meme_images_disabled_until, time.time() + MEME_IMAGE_TEMP_DISABLE_SEC)
    logger.warning("Meme images temporarily disabled for %ss: %s", MEME_IMAGE_TEMP_DISABLE_SEC, reason)

def _is_permission_denied(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 403:
        return True
    name = exc.__class__.__name__.lower()
    if "permissiondenied" in name:
        return True
    msg = str(exc).lower()
    return "organization must be verified" in msg or "permission denied" in msg

def news_images_enabled() -> bool:
    return _news_image_skip_reason() == ""

def remember_link(link: str):
    link_key = normalize_link(link)
    if not link_key:
        return
    if link_key in _seen_links:
        return
    _seen_links.add(link_key)
    _seen_order.append(link_key)
    while len(_seen_order) > NEWS_SEEN_MAX:
        old = _seen_order.popleft()
        _seen_links.discard(old)
    _save_news_seen()

def normalize_title(title: str) -> str:
    text = re.sub(r"[^\w]+", " ", (title or "").lower())
    return " ".join(text.split())

_TITLE_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "over", "after",
    "про", "для", "між", "під", "перед", "через", "щодо", "які", "який", "яка",
    "та", "і", "й", "але", "що", "це", "на", "у", "з", "до", "від", "без", "по",
    "про", "над", "при", "або", "чи", "за", "як", "не", "є",
    "по", "из", "об", "на", "в", "с", "у", "за", "без", "для", "про", "над",
    "при", "или", "что", "это", "как", "не", "есть",
}

def _title_tokens(text: str) -> Set[str]:
    if not text:
        return set()
    tokens = re.findall(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+", text.lower())
    out: Set[str] = set()
    for tok in tokens:
        if tok in _TITLE_STOPWORDS:
            continue
        if len(tok) < 3 and not tok.isdigit():
            continue
        out.add(tok)
    return out

def _content_tokens(title: str, summary: str) -> List[str]:
    tokens = set()
    tokens.update(_title_tokens(title))
    if summary:
        tokens.update(_title_tokens(summary))
    return list(sorted(tokens))[:20]

def _title_signature(tokens: Set[str]) -> str:
    if not tokens:
        return ""
    return " ".join(sorted(tokens))

def _title_similarity(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a.intersection(b)
    union = a.union(b)
    return len(inter) / max(1, len(union))

def _is_similar_title(tokens: Set[str], seen_tokens: List[Set[str]]) -> bool:
    if NEWS_TITLE_SIM_THRESHOLD <= 0 or len(tokens) < max(1, NEWS_TITLE_SIM_MIN_TOKENS):
        return False
    for prev in seen_tokens:
        if len(prev) < max(1, NEWS_TITLE_SIM_MIN_TOKENS):
            continue
        if _title_similarity(tokens, prev) >= NEWS_TITLE_SIM_THRESHOLD:
            return True
    return False

_TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_name",
    "gclid",
    "fbclid",
    "yclid",
    "igshid",
    "ref",
    "source",
}

def normalize_link(link: str) -> str:
    link = (link or "").strip()
    if not link:
        return ""
    try:
        parts = urlsplit(link)
        query = parse_qsl(parts.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in query if k.lower() not in _TRACKING_QUERY_KEYS]
        new_query = urlencode(filtered, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))
    except Exception:
        return link

def _feed_matches(feed_url: str, patterns: List[str]) -> bool:
    if not patterns:
        return False
    for pat in patterns:
        if not pat:
            continue
        if feed_url == pat or pat in feed_url:
            return True
    return False

def _feed_allowed(feed_url: str) -> bool:
    if NEWS_ALLOW_FEEDS and not _feed_matches(feed_url, NEWS_ALLOW_FEEDS):
        return False
    if NEWS_BLOCK_FEEDS and _feed_matches(feed_url, NEWS_BLOCK_FEEDS):
        return False
    return True

def _feed_disabled(feed_url: str) -> bool:
    data = _feed_health.get(feed_url)
    if not data:
        return False
    disabled_until = float(data.get("disabled_until") or 0)
    return time.time() < disabled_until

def _feed_record_failure(feed_url: str, reason: str) -> None:
    data = _feed_health.setdefault(feed_url, {"failures": 0, "empty": 0, "disabled_until": 0.0})
    data["failures"] = int(data.get("failures") or 0) + 1
    if NEWS_FEED_FAIL_THRESHOLD > 0 and data["failures"] >= NEWS_FEED_FAIL_THRESHOLD:
        data["disabled_until"] = time.time() + max(0, NEWS_FEED_DISABLE_SEC)
        data["failures"] = 0
        data["empty"] = 0
        logger.warning("Feed disabled: %s (%s)", feed_url, reason)

def _feed_record_empty(feed_url: str) -> None:
    data = _feed_health.setdefault(feed_url, {"failures": 0, "empty": 0, "disabled_until": 0.0})
    data["empty"] = int(data.get("empty") or 0) + 1
    if NEWS_FEED_EMPTY_THRESHOLD > 0 and data["empty"] >= NEWS_FEED_EMPTY_THRESHOLD:
        data["disabled_until"] = time.time() + max(0, NEWS_FEED_DISABLE_SEC)
        data["failures"] = 0
        data["empty"] = 0
        logger.warning("Feed disabled: %s (empty)", feed_url)

def _feed_record_success(feed_url: str) -> None:
    data = _feed_health.setdefault(feed_url, {"failures": 0, "empty": 0, "disabled_until": 0.0})
    data["failures"] = 0
    data["empty"] = 0

def _feed_cache_headers(feed_url: str) -> Dict[str, str]:
    headers = {"User-Agent": "TelegramBot/1.0"}
    meta = _feed_cache.get(feed_url) or {}
    etag = meta.get("etag")
    last_modified = meta.get("last_modified")
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers

NEWS_SEEN_FILE = Path("data/news_seen.json")

def _load_news_seen() -> None:
    if not NEWS_SEEN_FILE.exists():
        return
    try:
        data = json.loads(NEWS_SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load news seen list")
        return
    links = data.get("links") if isinstance(data, dict) else None
    titles = data.get("titles") if isinstance(data, dict) else None
    title_tokens = data.get("title_tokens") if isinstance(data, dict) else None
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, str):
                continue
            link_key = normalize_link(link)
            if not link_key or link_key in _seen_links:
                continue
            _seen_links.add(link_key)
            _seen_order.append(link_key)
    if isinstance(titles, list):
        for title in titles:
            if not isinstance(title, str) or title in _seen_titles:
                continue
            _seen_titles.add(title)
            _seen_titles_order.append(title)
    if isinstance(title_tokens, list):
        for item in title_tokens:
            tokens: Set[str] = set()
            if isinstance(item, list):
                tokens = {str(x) for x in item if str(x)}
            elif isinstance(item, str):
                tokens = {t for t in item.split() if t}
            if not tokens:
                continue
            sig = _title_signature(tokens)
            if not sig or sig in _seen_title_tokens_sigs:
                continue
            _seen_title_tokens_sigs.add(sig)
            _seen_title_tokens_order.append(sig)
            _seen_title_tokens_sets.append(tokens)
            while len(_seen_title_tokens_order) > NEWS_TITLE_SIM_SEEN_MAX:
                old_sig = _seen_title_tokens_order.popleft()
                _seen_title_tokens_sigs.discard(old_sig)
                if _seen_title_tokens_sets:
                    _seen_title_tokens_sets.popleft()
    elif _seen_titles_order:
        for title in _seen_titles_order:
            tokens = _title_tokens(title)
            if len(tokens) < max(1, NEWS_TITLE_SIM_MIN_TOKENS):
                continue
            sig = _title_signature(tokens)
            if not sig or sig in _seen_title_tokens_sigs:
                continue
            _seen_title_tokens_sigs.add(sig)
            _seen_title_tokens_order.append(sig)
            _seen_title_tokens_sets.append(tokens)
            while len(_seen_title_tokens_order) > NEWS_TITLE_SIM_SEEN_MAX:
                old_sig = _seen_title_tokens_order.popleft()
                _seen_title_tokens_sigs.discard(old_sig)
                if _seen_title_tokens_sets:
                    _seen_title_tokens_sets.popleft()

def _save_news_seen() -> None:
    try:
        NEWS_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "links": list(_seen_order),
            "titles": list(_seen_titles_order),
            "title_tokens": list(_seen_title_tokens_order),
        }
        NEWS_SEEN_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save news seen list")

def remember_title(title: str):
    if title in _seen_titles:
        return
    _seen_titles.add(title)
    _seen_titles_order.append(title)
    while len(_seen_titles_order) > NEWS_SEEN_MAX:
        old = _seen_titles_order.popleft()
        _seen_titles.discard(old)
    _save_news_seen()

def remember_title_tokens(title: str) -> None:
    tokens = _title_tokens(title)
    if len(tokens) < max(1, NEWS_TITLE_SIM_MIN_TOKENS):
        return
    sig = _title_signature(tokens)
    if not sig or sig in _seen_title_tokens_sigs:
        return
    _seen_title_tokens_sigs.add(sig)
    _seen_title_tokens_order.append(sig)
    _seen_title_tokens_sets.append(tokens)
    while len(_seen_title_tokens_order) > NEWS_TITLE_SIM_SEEN_MAX:
        old_sig = _seen_title_tokens_order.popleft()
        _seen_title_tokens_sigs.discard(old_sig)
        if _seen_title_tokens_sets:
            _seen_title_tokens_sets.popleft()
    _save_news_seen()

_load_news_seen()

def news_config_ok() -> bool:
    if not (NEWS_ENABLED and bool(NEWS_CHANNEL_ID) and RSS_FEEDS):
        return False
    if NEWS_USE_KEYWORDS and not URGENT_KEYWORDS:
        return False
    if not NEWS_USE_KEYWORDS and not NEWS_AI_FILTER_ENABLED:
        return _keyword_fallback_enabled()
    if NEWS_AI_FILTER_ENABLED and not NEWS_USE_KEYWORDS and not ai_configured():
        return _keyword_fallback_enabled()
    return True

def keyword_hits(text: str) -> int:
    text = (text or "").lower()
    hits = 0
    for kw in URGENT_KEYWORDS:
        if kw.lower() in text:
            hits += 1
    return hits

def _interest_hits(text: str) -> int:
    text = (text or "").lower()
    if not text:
        return 0
    hits = 0
    for kw in NEWS_INTEREST_KEYWORDS:
        if kw.lower() in text:
            hits += 1
    if NEWS_USE_KEYWORDS and not NEWS_INTEREST_KEYWORDS:
        for kw in URGENT_KEYWORDS:
            if kw.lower() in text:
                hits += 1
    return hits

def _whitelist_hits(text: str) -> int:
    text = (text or "").lower()
    if not text or not NEWS_WHITELIST_KEYWORDS:
        return 0
    hits = 0
    for kw in NEWS_WHITELIST_KEYWORDS:
        if kw.lower() in text:
            hits += 1
    return hits

def _block_hits(text: str) -> int:
    text = (text or "").lower()
    if not text or not NEWS_BLOCK_KEYWORDS:
        return 0
    hits = 0
    for kw in NEWS_BLOCK_KEYWORDS:
        if kw.lower() in text:
            hits += 1
    return hits

def _deprioritize_hits(text: str) -> int:
    text = (text or "").lower()
    if not text or not NEWS_DEPRIORITY_KEYWORDS:
        return 0
    hits = 0
    for kw in NEWS_DEPRIORITY_KEYWORDS:
        if kw.lower() in text:
            hits += 1
    return hits

def _text_has_any(text: str, keywords: List[str]) -> bool:
    for kw in keywords:
        if kw.lower() in text:
            return True
    return False

def _blast_hits(text: str) -> bool:
    text = (text or "").lower()
    if not text:
        return False
    if not _text_has_any(text, NEWS_BLAST_KEYWORDS):
        return False
    if not NEWS_BLAST_CONTEXT_KEYWORDS:
        return True
    return _text_has_any(text, NEWS_BLAST_CONTEXT_KEYWORDS)

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
        logger.debug("Contact AI triage skipped: %s", _ai_status_reason())
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
                timeout=AI_TIMEOUT_SEC,
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
    except asyncio.TimeoutError:
        _ai_register_timeout("contact_triage")
        logger.warning("Contact AI triage timed out")
        return None
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
        logger.debug("News AI scoring skipped: %s", _ai_status_reason())
        return None
    safe_title = (title or "").strip()[:300]
    safe_summary = (summary or "").strip()[:NEWS_SUMMARY_MAX_CHARS]
    input_text = f"TITLE:\n{safe_title}\n\nSUMMARY:\n{safe_summary}"
    max_score = NEWS_AI_SCORE_SCALE if NEWS_AI_SCORE_SCALE > 0 else 5
    instructions = (
        "You are a news triage assistant.\n"
        "Rate criticality and public resonance on a 0-"
        f"{max_score} scale using integers.\n"
        "Return ONLY a JSON object: "
        "{\"criticality\":0,\"importance\":0}\n"
        "criticality: immediate harm/emergency risk.\n"
        "importance: public resonance, broad impact, relevance.\n"
        "No extra text."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=instructions,
                input=input_text,
                timeout=NEWS_AI_TIMEOUT_SEC,
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
    except asyncio.TimeoutError:
        _ai_register_timeout("news_scoring")
        logger.warning("News AI scoring timed out")
        return None
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("News AI scoring failed")
        return None

async def ai_news_bullets(title: str, summary: str) -> str:
    if not ai_enabled():
        logger.debug("News AI bullets skipped: %s", _ai_status_reason())
        return ""
    safe_title = (title or "").strip()[:300]
    safe_summary = (summary or "").strip()[:NEWS_SUMMARY_MAX_CHARS]
    instructions = (
        "Склади 3–5 коротких тез українською.\n"
        "Формат: кожен рядок починається з '• '.\n"
        "Без паніки, без вигадок, без прямих закликів.\n"
        "Не використовуй емодзі."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=instructions,
                input=f"{safe_title}\n{safe_summary}",
                timeout=NEWS_AI_TIMEOUT_SEC,
            ),
            timeout=NEWS_AI_TIMEOUT_SEC,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out
    except asyncio.TimeoutError:
        _ai_register_timeout("news_bullets")
        logger.warning("News AI bullets timed out")
        return ""
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("News AI bullets failed")
        return ""

def news_rate_ok() -> bool:
    if NEWS_MAX_POSTS_PER_HOUR <= 0:
        return True
    now = time.time()
    while _news_sent_times and now - _news_sent_times[0] > 3600:
        _news_sent_times.popleft()
    return len(_news_sent_times) < NEWS_MAX_POSTS_PER_HOUR

def mark_news_sent():
    _news_sent_times.append(time.time())

def urgent_rate_ok() -> bool:
    if NEWS_MAX_URGENT_PER_HOUR <= 0:
        return True
    now = time.time()
    while _news_urgent_sent_times and now - _news_urgent_sent_times[0] > 3600:
        _news_urgent_sent_times.popleft()
    return len(_news_urgent_sent_times) < NEWS_MAX_URGENT_PER_HOUR

def mark_urgent_sent():
    _news_urgent_sent_times.append(time.time())

def _summary_tzinfo():
    try:
        return ZoneInfo(NEWS_SUMMARY_TZ)
    except Exception:
        return timezone.utc

def _channel_tzinfo():
    try:
        return ZoneInfo(CHANNEL_POSTS_TZ)
    except Exception:
        return timezone.utc

def _channel_now() -> datetime:
    return datetime.now(_channel_tzinfo())

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

def _parse_channel_times(value: str) -> List[dt_time]:
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
        return []
    tz = _channel_tzinfo()
    return [t.replace(tzinfo=tz) for t in times]

def _footer_channel_link() -> str:
    if FOOTER_CHANNEL_LINK:
        return FOOTER_CHANNEL_LINK
    if NEWS_SUMMARY_CHANNEL_LINK:
        return NEWS_SUMMARY_CHANNEL_LINK
    if NEWS_CHANNEL_ID.startswith("@"):
        return f"https://t.me/{NEWS_CHANNEL_ID[1:]}"
    return ""

def _footer_bot_link() -> str:
    return FOOTER_BOT_LINK or BOT_PUBLIC_LINK

def _footer_site_link() -> str:
    return FOOTER_SITE_LINK

FOOTER_MARKER = "\n\nLinks:\n"

def _footer_text(chat_id: object = None) -> str:
    if not FOOTER_ENABLED:
        return ""
    parts: List[str] = []
    bot_link = _footer_bot_link()
    if bot_link:
        parts.append(f"Bot: {bot_link}")
    channel_link = _footer_channel_link()
    if channel_link:
        parts.append(f"Channel: {channel_link}")
    site_link = _footer_site_link()
    if site_link:
        parts.append(f"Site: {site_link}")
    if not parts:
        return ""
    return FOOTER_MARKER + "\n".join(parts)

def _append_footer(text: str, chat_id: object = None) -> str:
    if not text:
        return text
    footer = _footer_text(chat_id)
    if not footer:
        return text
    if FOOTER_MARKER in text:
        return text
    if len(text) + len(footer) > 4096:
        return text
    return text + footer

async def _init_bot_public_link(application) -> None:
    global BOT_PUBLIC_LINK
    if BOT_PUBLIC_LINK:
        return
    try:
        me = await application.bot.get_me()
        username = getattr(me, "username", "") or ""
        if username:
            BOT_PUBLIC_LINK = f"https://t.me/{username}"
    except Exception:
        logger.exception("Failed to resolve bot public link")

IMAGE_STYLE_PRESETS = {
    "editorial-ink": "editorial ink illustration, high contrast, clean lines",
    "soft-gradient": "soft gradient poster, minimal shapes",
    "paper-cut": "layered paper-cut collage, tactile depth",
    "isometric": "isometric scene, simplified geometry",
    "flat-vector": "flat vector illustration, bold shapes",
    "grainy-duotone": "duotone with subtle grain, calm palette",
    "monoline": "monoline illustration, minimal linework",
    "watercolor": "watercolor wash, soft edges",
    "comic-pop": "comic pop art, bold outlines, punchy shapes",
    "sticker-collage": "sticker collage, cutout feel, playful layering",
    "cinematic-glow": "cinematic lighting, moody glow, depth and contrast",
    "poster-pop": "bold poster design, high contrast, striking silhouette",
}

_last_news_image_style = ""
_last_summary_image_style = ""
_last_channel_image_style = ""
_last_meme_image_style = ""

def _normalize_style_key(value: str) -> str:
    return (value or "").strip().lower()

def _parse_style_pool(value: str, fallback: List[str]) -> List[str]:
    raw = _normalize_style_key(value)
    if raw in ("none", "off", "false", "0", "disable", "disabled"):
        return []
    if raw:
        items = [_normalize_style_key(x) for x in value.split(",") if x.strip()]
        items = [i for i in items if i in IMAGE_STYLE_PRESETS]
        if items:
            return items
    return [i for i in fallback if i in IMAGE_STYLE_PRESETS]

def _style_pool_for(kind: str) -> List[str]:
    if kind == "summary":
        base_pool = _parse_style_pool(NEWS_IMAGE_STYLES, list(IMAGE_STYLE_PRESETS.keys()))
        return _parse_style_pool(NEWS_SUMMARY_IMAGE_STYLES, base_pool)
    if kind == "channel":
        return _parse_style_pool(CHANNEL_POSTS_IMAGE_STYLES, list(IMAGE_STYLE_PRESETS.keys()))
    if kind == "meme":
        return _parse_style_pool(MEME_IMAGE_STYLES, list(IMAGE_STYLE_PRESETS.keys()))
    return _parse_style_pool(NEWS_IMAGE_STYLES, list(IMAGE_STYLE_PRESETS.keys()))

def _pick_style(pool: List[str], last: str) -> str:
    if not pool:
        return ""
    if len(pool) == 1:
        return pool[0]
    if last and last in pool:
        choices = [p for p in pool if p != last]
    else:
        choices = pool
    return random.choice(choices) if choices else pool[0]

def _image_style_line(kind: str) -> str:
    global _last_news_image_style, _last_summary_image_style, _last_channel_image_style
    global _last_meme_image_style
    pool = _style_pool_for(kind)
    if not pool:
        return ""
    if kind == "summary":
        choice = _pick_style(pool, _last_summary_image_style)
        _last_summary_image_style = choice
    elif kind == "channel":
        choice = _pick_style(pool, _last_channel_image_style)
        _last_channel_image_style = choice
    elif kind == "meme":
        choice = _pick_style(pool, _last_meme_image_style)
        _last_meme_image_style = choice
    else:
        choice = _pick_style(pool, _last_news_image_style)
        _last_news_image_style = choice
    style = IMAGE_STYLE_PRESETS.get(choice, "")
    return f"Style: {style}." if style else ""

def _news_image_prompt(title: str, summary: str) -> str:
    title = (title or "").strip()
    summary = (summary or "").strip()
    base = (
        "Create a single, safe-for-work illustration for a news post about Ukraine. "
        "Make it visually bold and shareable with dynamic composition, strong contrast, "
        "and clear visual metaphor. "
        "If the news is light/positive, make it playful and meme-leaning without text. "
        "If the news is serious/tragic, make it cinematic and respectful, not graphic. "
        "No graphic violence, no gore, no text overlays, no logos."
    )
    style_line = _image_style_line("news")
    if style_line:
        base = base + " " + style_line
    return f"{base}\n\nTITLE: {title}\nSUMMARY: {summary}"

def _caption_with_footer(text: str, chat_id: object = None, max_len: int = 1024) -> str:
    if not text:
        return text
    footer = _footer_text(chat_id)
    body = text
    if footer:
        body = text
        if text.endswith(footer):
            body = text[:-len(footer)]
    if not footer:
        return text[:max_len] if len(text) > max_len else text
    max_body = max_len - len(footer)
    if max_body <= 0:
        return text[:max_len]
    if len(body) > max_body:
        body = body[:max_body - 1].rstrip() + "…"
    return body + footer

async def _generate_news_image(title: str, summary: str) -> Optional[bytes]:
    reason = _news_image_skip_reason()
    if reason:
        logger.info("News image skipped: %s", reason)
        return None
    prompt = _news_image_prompt(title, summary)
    logger.debug("News image request: model=%s size=%s prompt_len=%s", NEWS_IMAGE_MODEL, NEWS_IMAGE_SIZE, len(prompt))
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.images.generate,
                model=NEWS_IMAGE_MODEL,
                prompt=prompt,
                size=NEWS_IMAGE_SIZE,
            ),
            timeout=NEWS_IMAGE_TIMEOUT_SEC,
        )
        data = getattr(resp, "data", None) or []
        if not data:
            logger.warning("News image generation returned empty data")
            return None
        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(item, "url", None)
        if url:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url)
                if r.status_code < 400:
                    return r.content
                logger.warning("News image download failed: %s", r.status_code)
        return None
    except asyncio.TimeoutError:
        _ai_register_timeout("news_image")
        logger.warning("News image generation timed out")
        return None
    except Exception as exc:
        if _is_permission_denied(exc):
            _disable_news_images_temporarily("permission denied")
            return None
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("News image generation failed")
        return None

def _summary_image_prompt(headlines: List[str], ai_text: str) -> str:
    lines = [h for h in headlines if h]
    bullets = "\n".join([f"- {h}" for h in lines[:6]])
    base = (
        "Create a single cover illustration for a Ukraine news digest. "
        "Make it visually bold and cinematic with a clear central metaphor and strong contrast. "
        "If the overall tone is positive/light, make it playful and meme-leaning without text. "
        "If the tone is grim/serious, make it cinematic and respectful, not graphic. "
        "No graphic violence, no gore, no text overlays, no logos."
    )
    style_line = _image_style_line("summary")
    if style_line:
        base = base + " " + style_line
    extra = (ai_text or "").strip()
    if extra:
        extra = clip(extra, 800)
        return f"{base}\n\nDIGEST NOTES:\n{extra}\n\nHEADLINES:\n{bullets}"
    return f"{base}\n\nHEADLINES:\n{bullets}"

async def _generate_summary_image(items: List[Dict[str, object]], ai_text: str) -> Optional[bytes]:
    reason = _news_image_skip_reason()
    if reason:
        logger.info("Summary image skipped: %s", reason)
        return None
    headlines = [str(it.get("title") or "") for it in items[:8]]
    prompt = _summary_image_prompt(headlines, ai_text)
    logger.debug("Summary image request: model=%s size=%s prompt_len=%s", NEWS_IMAGE_MODEL, NEWS_IMAGE_SIZE, len(prompt))
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.images.generate,
                model=NEWS_IMAGE_MODEL,
                prompt=prompt,
                size=NEWS_IMAGE_SIZE,
            ),
            timeout=NEWS_IMAGE_TIMEOUT_SEC,
        )
        data = getattr(resp, "data", None) or []
        if not data:
            logger.warning("Summary image generation returned empty data")
            return None
        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(item, "url", None)
        if url:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url)
                if r.status_code < 400:
                    return r.content
                logger.warning("Summary image download failed: %s", r.status_code)
        return None
    except asyncio.TimeoutError:
        _ai_register_timeout("summary_image")
        logger.warning("Summary image generation timed out")
        return None
    except Exception as exc:
        if _is_permission_denied(exc):
            _disable_news_images_temporarily("permission denied")
            return None
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("Summary image generation failed")
        return None

def _meme_image_prompt(headlines: List[str]) -> str:
    lines = [clip(h or "", 180) for h in headlines if h]
    bullets = "\n".join([f"- {h}" for h in lines[:6]])
    base = (
        "Create a single, safe-for-work meme-style illustration inspired by current Ukraine news headlines. "
        "Make it clever, visually bold, and easy to share. "
        "Humor must be respectful and never mock victims or tragedy. "
        "No text overlays, no logos, no real people."
    )
    style_line = _image_style_line("meme")
    if style_line:
        base = base + " " + style_line
    return f"{base}\n\nHEADLINES:\n{bullets}"

async def _generate_meme_image(items: List[Dict[str, object]]) -> Optional[bytes]:
    reason = _meme_image_skip_reason()
    if reason:
        logger.info("Meme image skipped: %s", reason)
        return None
    headlines = [str(it.get("title") or "") for it in items[:8]]
    if not headlines:
        logger.info("Meme image skipped: no headlines")
        return None
    prompt = _meme_image_prompt(headlines)
    logger.debug(
        "Meme image request: model=%s size=%s prompt_len=%s",
        MEME_IMAGE_MODEL,
        MEME_IMAGE_SIZE,
        len(prompt),
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.images.generate,
                model=MEME_IMAGE_MODEL,
                prompt=prompt,
                size=MEME_IMAGE_SIZE,
            ),
            timeout=MEME_IMAGE_TIMEOUT_SEC,
        )
        data = getattr(resp, "data", None) or []
        if not data:
            logger.warning("Meme image generation returned empty data")
            return None
        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(item, "url", None)
        if url:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url)
                if r.status_code < 400:
                    return r.content
                logger.warning("Meme image download failed: %s", r.status_code)
        return None
    except asyncio.TimeoutError:
        _ai_register_timeout("meme_image")
        logger.warning("Meme image generation timed out")
        return None
    except Exception as exc:
        if _is_permission_denied(exc):
            _disable_meme_images_temporarily("permission denied")
            return None
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("Meme image generation failed")
        return None

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
            key = normalize_link(item)
            if not key or key in _summary_seen:
                continue
            _summary_seen.add(key)
            _summary_seen_order.append(key)

def _save_summary_seen() -> None:
    try:
        SUMMARY_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_SEEN_FILE.write_text(json.dumps(list(_summary_seen_order)), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save summary seen list")

def _mark_summary_links(links: List[str]) -> None:
    changed = False
    for link in links:
        key = normalize_link(link)
        if not key or key in _summary_seen:
            continue
        _summary_seen.add(key)
        _summary_seen_order.append(key)
        changed = True
        while len(_summary_seen_order) > NEWS_SUMMARY_SEEN_MAX:
            old = _summary_seen_order.popleft()
            _summary_seen.discard(old)
    if changed:
        _save_summary_seen()

_load_summary_seen()

NEWS_STATS_FILE = Path("data/news_stats.json")
_news_stats: deque[Dict[str, object]] = deque()
NEWS_SKIP_STATS_FILE = Path("data/news_skip_stats.json")
_news_skip_stats: deque[Dict[str, object]] = deque()

def _trim_news_stats() -> None:
    if NEWS_STATS_RETENTION_HOURS <= 0:
        _news_stats.clear()
        return
    cutoff = time.time() - NEWS_STATS_RETENTION_HOURS * 3600
    while _news_stats and float(_news_stats[0].get("ts") or 0) < cutoff:
        _news_stats.popleft()

def _load_news_stats() -> None:
    if not NEWS_STATS_FILE.exists():
        return
    try:
        data = json.loads(NEWS_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load news stats")
        return
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            _news_stats.append(item)
    _trim_news_stats()

def _save_news_stats() -> None:
    try:
        NEWS_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        NEWS_STATS_FILE.write_text(json.dumps(list(_news_stats)), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save news stats")

def _record_news_stat(feed_url: str, title: str, urgent: bool) -> None:
    if not NEWS_STATS_ENABLED:
        return
    tokens = list(_title_tokens(title))[:20]
    _news_stats.append({
        "ts": time.time(),
        "feed": feed_url,
        "urgent": bool(urgent),
        "tokens": tokens,
    })
    _trim_news_stats()
    _save_news_stats()

def _stats_window(hours: int) -> List[Dict[str, object]]:
    if hours <= 0:
        return []
    cutoff = time.time() - hours * 3600
    return [it for it in _news_stats if float(it.get("ts") or 0) >= cutoff]

def _top_counts(items: List[Dict[str, object]], key: str, limit: int) -> List[tuple[str, int]]:
    counts: Dict[str, int] = {}
    for it in items:
        value = it.get(key)
        if not value:
            continue
        if isinstance(value, list):
            for v in value:
                if not v:
                    continue
                counts[str(v)] = counts.get(str(v), 0) + 1
        else:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]

_load_news_stats()

def _trim_news_skip_stats() -> None:
    if NEWS_SKIP_STATS_RETENTION_HOURS <= 0:
        _news_skip_stats.clear()
        return
    cutoff = time.time() - NEWS_SKIP_STATS_RETENTION_HOURS * 3600
    while _news_skip_stats and float(_news_skip_stats[0].get("ts") or 0) < cutoff:
        _news_skip_stats.popleft()

def _load_news_skip_stats() -> None:
    if not NEWS_SKIP_STATS_FILE.exists():
        return
    try:
        data = json.loads(NEWS_SKIP_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load news skip stats")
        return
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            _news_skip_stats.append(item)
    _trim_news_skip_stats()

def _save_news_skip_stats() -> None:
    try:
        NEWS_SKIP_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        NEWS_SKIP_STATS_FILE.write_text(json.dumps(list(_news_skip_stats)), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save news skip stats")

def _record_news_skip(reason: str, title: str, summary: str, feed_url: str = "", meta: Optional[Dict[str, object]] = None) -> None:
    if not NEWS_SKIP_STATS_ENABLED:
        return
    tokens = _content_tokens(title, summary)
    _news_skip_stats.append({
        "ts": time.time(),
        "reason": reason,
        "feed": feed_url,
        "tokens": tokens,
        "meta": meta or {},
    })
    _trim_news_skip_stats()
    _save_news_skip_stats()

def _skip_window(hours: int) -> List[Dict[str, object]]:
    if hours <= 0:
        return []
    cutoff = time.time() - hours * 3600
    return [it for it in _news_skip_stats if float(it.get("ts") or 0) >= cutoff]

def _suggest_keywords(hours: int, reasons: Optional[Set[str]] = None) -> List[tuple[str, int]]:
    items = _skip_window(hours)
    if reasons:
        items = [it for it in items if it.get("reason") in reasons]
    existing = set()
    for pool in (URGENT_KEYWORDS, NEWS_INTEREST_KEYWORDS, NEWS_WHITELIST_KEYWORDS, NEWS_DEPRIORITY_KEYWORDS, NEWS_BLOCK_KEYWORDS):
        existing.update([k.lower() for k in pool])
    counts: Dict[str, int] = {}
    for it in items:
        tokens = it.get("tokens") or []
        for tok in tokens:
            t = str(tok).lower()
            if not t or t in existing:
                continue
            if t.isdigit():
                continue
            counts[t] = counts.get(t, 0) + 1
    min_freq = max(1, NEWS_KEYWORD_SUGGEST_MIN_FREQ)
    pairs = [(tok, cnt) for tok, cnt in counts.items() if cnt >= min_freq]
    pairs.sort(key=lambda x: x[1], reverse=True)
    return pairs[:max(1, NEWS_KEYWORD_SUGGEST_MAX)]

_load_news_skip_stats()

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    await context.bot.send_message(
        chat_id=NEWS_CHANNEL_ID,
        text=_append_footer(text, NEWS_CHANNEL_ID),
        disable_web_page_preview=False,
    )

async def fetch_feed_text(client: httpx.AsyncClient, feed_url: str) -> str:
    headers = _feed_cache_headers(feed_url)
    url = _feed_redirects.get(feed_url, feed_url)
    r = await client.get(url, headers=headers, follow_redirects=True)
    if r.status_code == 304:
        return ""
    if r.is_redirect:
        location = r.headers.get("location")
        if not location:
            r.raise_for_status()
        redirect_url = str(r.url.join(location))
        logger.info("RSS redirect: %s -> %s", url, redirect_url)
        _feed_redirects[feed_url] = redirect_url
        r = await client.get(redirect_url, headers=headers, follow_redirects=True)
        if r.status_code == 304:
            return ""
    if r.status_code >= 400:
        r.raise_for_status()
    etag = r.headers.get("etag")
    last_modified = r.headers.get("last-modified")
    if etag or last_modified:
        meta = _feed_cache.setdefault(feed_url, {})
        if etag:
            meta["etag"] = etag
        if last_modified:
            meta["last_modified"] = last_modified
    return r.text

def _news_age_hours(published: Optional[datetime]) -> float:
    if not published:
        return 0.0
    now = datetime.now(timezone.utc)
    delta = now - published
    return max(0.0, delta.total_seconds() / 3600.0)

def _fallback_bullets(summary: str) -> str:
    summary = _clean_html(summary or "")
    if not summary:
        return "• Немає короткого опису у джерелі."
    parts = [p.strip() for p in re.split(r"[.!?]+", summary) if p.strip()]
    if not parts:
        return "• Деталі у джерелі."
    keywords = URGENT_KEYWORDS + NEWS_WHITELIST_KEYWORDS + NEWS_INTEREST_KEYWORDS

    def score_sentence(text: str) -> int:
        score = 0
        length = len(text)
        if 40 <= length <= 180:
            score += 2
        elif length < 25 or length > 220:
            score -= 1
        if re.search(r"\d", text):
            score += 1
        if _text_has_any(text, keywords):
            score += 1
        return score

    scored = [(score_sentence(p), idx, p) for idx, p in enumerate(parts)]
    scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    chosen = []
    seen = set()
    for _, _, sent in scored:
        if sent in seen:
            continue
        seen.add(sent)
        chosen.append(sent)
        if len(chosen) >= 4:
            break
    if not chosen:
        return "• Деталі у джерелі."
    return "\n".join([f"• {s}" for s in chosen])

def _normalize_bullets(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return ""
    bullets: List[str] = []
    for line in lines:
        if line.startswith("•"):
            bullets.append(line)
        else:
            bullets.append("• " + line.lstrip("-•* ").strip())
        if len(bullets) >= 5:
            break
    return "\n".join(bullets)

def _news_header_text(title: str, bullets: str, urgent: bool) -> str:
    prefix = "🚨 ТЕРМІНОВО\n" if urgent else "📰 Новини\n"
    return f"{prefix}🧭 Тема: {title}\n\nТези:\n{bullets}"

def _news_body_text(title: str, summary: str, link: str) -> str:
    summary = _clean_html(summary or "")
    if summary:
        summary = clip(summary, 1200)
        body = f"🗞️ Основна новина:\n{title}\n\n{summary}"
    else:
        body = f"🗞️ Основна новина:\n{title}"
    body += f"\n\n🔗 Джерело: {link}"
    return body

def _clip_to_len(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[:max_len - 1].rstrip() + "…"

def _news_full_text(
    title: str,
    summary: str,
    link: str,
    bullets: str,
    urgent: bool,
    max_len: Optional[int] = None,
) -> str:
    prefix = "🚨 ТЕРМІНОВО" if urgent else "📰 Новини"
    title = (title or "").strip()
    summary_clean = _clean_html(summary or "")
    if not summary_clean:
        summary_clean = "Немає короткого опису у джерелі."

    bullets_text = _normalize_bullets(bullets)
    if not bullets_text:
        bullets_text = _fallback_bullets(summary)
    bullet_lines = [l for l in bullets_text.splitlines() if l.strip()]

    link_text = normalize_link(link) or (link or "")

    if max_len:
        title = clip(title, 160)
        bullet_lines = [_clip_to_len(l, 140) for l in bullet_lines[:4]]
        link_text = _clip_to_len(link_text, 220)
    else:
        bullet_lines = bullet_lines[:5]

    lines_before = [
        prefix,
        f"🧭 Тема: {title}",
        "",
        "Тези:",
    ] + bullet_lines + [
        "",
        "🗞️ Деталі:",
    ]
    lines_after = [
        "",
        f"🔗 Джерело: {link_text}",
    ]

    if not max_len:
        summary_line = clip(summary_clean, 1200)
        return "\n".join(lines_before + [summary_line] + lines_after)

    def base_len() -> int:
        return len("\n".join(lines_before + [""] + lines_after))

    while bullet_lines and base_len() > max_len:
        bullet_lines.pop()
        lines_before = [
            prefix,
            f"🧭 Тема: {title}",
            "",
            "Тези:",
        ] + bullet_lines + [
            "",
            "🗞️ Деталі:",
        ]

    if base_len() > max_len:
        title = _clip_to_len(title, 80)
        lines_before = [
            prefix,
            f"🧭 Тема: {title}",
            "",
            "Тези:",
        ] + bullet_lines + [
            "",
            "🗞️ Деталі:",
        ]

    available = max_len - base_len()
    summary_line = _clip_to_len(summary_clean, available)
    return "\n".join(lines_before + [summary_line] + lines_after)

async def _publish_news_entry(
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    summary: str,
    link: str,
    urgent: bool,
    feed_url: str,
) -> None:
    bullets = _normalize_bullets(await ai_news_bullets(title, summary))
    if not bullets:
        bullets = _fallback_bullets(summary)

    try:
        image_bytes = await _generate_news_image(title, summary)
        if image_bytes:
            footer = _footer_text(NEWS_CHANNEL_ID)
            max_body = 1024 - len(footer) if footer else 1024
            text = _news_full_text(title, summary, link, bullets, urgent, max_len=max_body)
            caption = _caption_with_footer(text, NEWS_CHANNEL_ID, max_len=1024)
            await context.bot.send_photo(
                chat_id=NEWS_CHANNEL_ID,
                photo=InputFile(io.BytesIO(image_bytes), filename="news.png"),
                caption=caption,
            )
        else:
            text = _news_full_text(title, summary, link, bullets, urgent)
            await context.bot.send_message(
                chat_id=NEWS_CHANNEL_ID,
                text=_append_footer(text, NEWS_CHANNEL_ID),
                disable_web_page_preview=True,
            )
        _record_news_stat(feed_url, title, urgent)
        logger.info("News post delivered: urgent=%s title=%s link=%s", urgent, clip(title, 120), link)
    except Exception:
        logger.exception("News post failed: %s", link)
        raise

async def _news_job_inner(context: ContextTypes.DEFAULT_TYPE):
    if not news_config_ok():
        logger.info("News job skipped: config not OK")
        return
    if not RSS_FEEDS or not NEWS_CHANNEL_ID:
        logger.info("News job skipped: RSS_FEEDS or NEWS_CHANNEL_ID empty")
        return

    urgent_items: List[Dict[str, object]] = []
    candidates: List[Dict[str, object]] = []
    run_seen_links: Set[str] = set()
    run_seen_titles: Set[str] = set()
    run_seen_title_tokens: List[Set[str]] = []
    stat_keys = [
        "feeds_ok",
        "entries",
        "skipped_feed_blocked",
        "skipped_feed_disabled",
        "not_modified",
        "empty",
        "skipped_no_link",
        "skipped_seen_link",
        "skipped_run_link",
        "skipped_no_title",
        "skipped_seen_title",
        "skipped_run_title",
        "skipped_block_kw",
        "skipped_similar_title",
        "skipped_old",
        "urgent",
        "candidate",
    ]
    stats = {k: 0 for k in stat_keys}
    feed_stats: Dict[str, Dict[str, int]] = {}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for feed_url in RSS_FEEDS:
            fs = feed_stats.setdefault(feed_url, {k: 0 for k in stat_keys})
            if not _feed_allowed(feed_url):
                stats["skipped_feed_blocked"] += 1
                fs["skipped_feed_blocked"] += 1
                continue
            if _feed_disabled(feed_url):
                stats["skipped_feed_disabled"] += 1
                fs["skipped_feed_disabled"] += 1
                continue
            try:
                feed_text = await fetch_feed_text(client, feed_url)
                if not feed_text:
                    stats["not_modified"] += 1
                    fs["not_modified"] += 1
                    continue
                feed = feedparser.parse(feed_text)
                stats["feeds_ok"] += 1
                fs["feeds_ok"] += 1
                for entry in (feed.entries or [])[:20]:
                    stats["entries"] += 1
                    fs["entries"] += 1
                    title = getattr(entry, "title", "") or ""
                    link = getattr(entry, "link", "") or ""
                    summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                    title_norm = normalize_title(title)
                    link_key = normalize_link(link)
                    title_tokens = _title_tokens(title)

                    if not link_key:
                        stats["skipped_no_link"] += 1
                        fs["skipped_no_link"] += 1
                        continue
                    if link_key in _seen_links:
                        stats["skipped_seen_link"] += 1
                        fs["skipped_seen_link"] += 1
                        continue
                    if link_key in run_seen_links:
                        stats["skipped_run_link"] += 1
                        fs["skipped_run_link"] += 1
                        continue
                    if not title_norm:
                        stats["skipped_no_title"] += 1
                        fs["skipped_no_title"] += 1
                        continue
                    if title_norm in _seen_titles:
                        stats["skipped_seen_title"] += 1
                        fs["skipped_seen_title"] += 1
                        continue
                    if title_norm in run_seen_titles:
                        stats["skipped_run_title"] += 1
                        fs["skipped_run_title"] += 1
                        continue
                    if _is_similar_title(title_tokens, run_seen_title_tokens) or _is_similar_title(title_tokens, _seen_title_tokens_sets):
                        stats["skipped_similar_title"] += 1
                        fs["skipped_similar_title"] += 1
                        logger.debug("Skip similar title: %s", clip(title, 120))
                        _record_news_skip("similar_title", title, summary, feed_url)
                        continue

                    published = _entry_datetime(entry)
                    age_hours = _news_age_hours(published)

                    text = f"{title}\n{summary}"
                    block_hits = _block_hits(text)
                    if block_hits > 0:
                        stats["skipped_block_kw"] += 1
                        fs["skipped_block_kw"] += 1
                        _record_news_skip("blocked_kw", title, summary, feed_url)
                        continue
                    deprior_hits = _deprioritize_hits(text)
                    whitelist_hits = _whitelist_hits(text)
                    interest_hits = _interest_hits(text)
                    item = {
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "title_norm": title_norm,
                        "feed_url": feed_url,
                        "published": published,
                        "age_hours": age_hours,
                        "text": text,
                        "deprior_hits": deprior_hits,
                        "whitelist_hits": whitelist_hits,
                        "interest_hits": interest_hits,
                    }

                    if _blast_hits(text):
                        urgent_items.append(item)
                        stats["urgent"] += 1
                        fs["urgent"] += 1
                    else:
                        if NEWS_MAX_AGE_HOURS > 0 and age_hours > NEWS_MAX_AGE_HOURS:
                            stats["skipped_old"] += 1
                            fs["skipped_old"] += 1
                            _record_news_skip("old", title, summary, feed_url, {"age_hours": age_hours})
                            continue
                        candidates.append(item)
                        stats["candidate"] += 1
                        fs["candidate"] += 1
                    run_seen_links.add(link_key)
                    run_seen_titles.add(title_norm)
                    if title_tokens:
                        run_seen_title_tokens.append(title_tokens)
                if feed.entries:
                    _feed_record_success(feed_url)
                else:
                    stats["empty"] += 1
                    fs["empty"] += 1
                    _feed_record_empty(feed_url)
            except Exception:
                _feed_record_failure(feed_url, "news fetch")
                logger.exception("news_job error for feed %s", feed_url)
                continue
    logger.info(
        "News poll stats: feeds=%s ok=%s entries=%s urgent=%s candidates=%s "
        "skipped_feed_blocked=%s skipped_feed_disabled=%s not_modified=%s empty=%s "
        "skipped_no_link=%s skipped_no_title=%s skipped_seen_link=%s skipped_seen_title=%s "
        "skipped_run_link=%s skipped_run_title=%s skipped_block_kw=%s skipped_similar_title=%s skipped_old=%s",
        len(RSS_FEEDS),
        stats["feeds_ok"],
        stats["entries"],
        stats["urgent"],
        stats["candidate"],
        stats["skipped_feed_blocked"],
        stats["skipped_feed_disabled"],
        stats["not_modified"],
        stats["empty"],
        stats["skipped_no_link"],
        stats["skipped_no_title"],
        stats["skipped_seen_link"],
        stats["skipped_seen_title"],
        stats["skipped_run_link"],
        stats["skipped_run_title"],
        stats["skipped_block_kw"],
        stats["skipped_similar_title"],
        stats["skipped_old"],
    )
    for feed_url, fs in feed_stats.items():
        logger.info(
            "News feed stats: url=%s entries=%s urgent=%s candidates=%s "
            "skipped_feed_blocked=%s skipped_feed_disabled=%s not_modified=%s empty=%s "
            "skipped_no_link=%s skipped_no_title=%s skipped_seen_link=%s skipped_seen_title=%s "
            "skipped_run_link=%s skipped_run_title=%s skipped_block_kw=%s skipped_similar_title=%s skipped_old=%s",
            feed_url,
            fs["entries"],
            fs["urgent"],
            fs["candidate"],
            fs["skipped_feed_blocked"],
            fs["skipped_feed_disabled"],
            fs["not_modified"],
            fs["empty"],
            fs["skipped_no_link"],
            fs["skipped_no_title"],
            fs["skipped_seen_link"],
            fs["skipped_seen_title"],
            fs["skipped_run_link"],
            fs["skipped_run_title"],
            fs["skipped_block_kw"],
            fs["skipped_similar_title"],
            fs["skipped_old"],
        )

    # publish urgent blast news immediately (no hourly limit)
    urgent_items.sort(
        key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for idx, item in enumerate(urgent_items):
        if not urgent_rate_ok():
            logger.info("Urgent rate limit reached")
            for rest in urgent_items[idx:]:
                _record_news_skip("urgent_rate_limited", rest["title"], rest["summary"], rest.get("feed_url") or "")
            break
        try:
            await _publish_news_entry(
                context,
                item["title"],
                item["summary"],
                item["link"],
                urgent=True,
                feed_url=item.get("feed_url") or "",
            )
        except Exception:
            continue
        remember_link(item["link"])
        remember_title(item["title_norm"])
        remember_title_tokens(item["title"])
        mark_urgent_sent()

    # AI-selected posts (rate limited)
    if NEWS_AI_FILTER_ENABLED and ai_enabled():
        # prioritize newest items for scoring
        candidates.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if NEWS_AI_MAX_CANDIDATES > 0:
            candidates = candidates[:NEWS_AI_MAX_CANDIDATES]

        scored: List[tuple[float, Dict[str, object], Dict[str, int]]] = []
        ai_start = time.monotonic()
        for idx, item in enumerate(candidates):
            if NEWS_AI_SCORE_BUDGET_SEC > 0 and (time.monotonic() - ai_start) > NEWS_AI_SCORE_BUDGET_SEC:
                logger.warning(
                    "AI scoring budget exceeded; skipped %s candidates",
                    len(candidates) - idx,
                )
                for rest in candidates[idx:]:
                    _record_news_skip("ai_budget", rest["title"], rest["summary"], rest.get("feed_url") or "")
                break
            ai_scores = await ai_news_scores(item["title"], item["summary"])
            if not ai_scores:
                if NEWS_AI_STRICT:
                    _record_news_skip("ai_no_score", item["title"], item["summary"], item.get("feed_url") or "")
                    continue
            else:
                if (ai_scores["criticality"] < NEWS_AI_MIN_CRITICALITY or
                        ai_scores["importance"] < NEWS_AI_MIN_IMPORTANCE):
                    _record_news_skip(
                        "ai_below_threshold",
                        item["title"],
                        item["summary"],
                        item.get("feed_url") or "",
                        {"criticality": ai_scores["criticality"], "importance": ai_scores["importance"]},
                    )
                    continue
            interest_hits = item.get("interest_hits", 0) or 0
            recency = max(0.0, NEWS_RECENCY_BOOST - item["age_hours"])
            deprior_hits = item.get("deprior_hits", 0) or 0
            whitelist_hits = item.get("whitelist_hits", 0) or 0
            feed_boost = NEWS_PRIORITY_FEED_BOOST if _feed_matches(str(item.get("feed_url") or ""), NEWS_PRIORITY_FEEDS) else 0
            score = (
                (ai_scores["importance"] if ai_scores else 0) * 2 +
                (ai_scores["criticality"] if ai_scores else 0) * 2 +
                interest_hits * NEWS_INTEREST_BOOST +
                whitelist_hits * NEWS_WHITELIST_BOOST +
                feed_boost +
                recency -
                deprior_hits * NEWS_DEPRIORITY_PENALTY
            )
            scored.append((score, item, ai_scores or {"criticality": 0, "importance": 0}))

        scored.sort(key=lambda x: x[0], reverse=True)
        posted = 0
        for idx, (_, item, _) in enumerate(scored):
            if NEWS_MAX_POSTS_PER_RUN > 0 and posted >= NEWS_MAX_POSTS_PER_RUN:
                for _, rest, _ in scored[idx:]:
                    _record_news_skip("max_per_run", rest["title"], rest["summary"], rest.get("feed_url") or "")
                break
            if not news_rate_ok():
                for _, rest, _ in scored[idx:]:
                    _record_news_skip("rate_limited", rest["title"], rest["summary"], rest.get("feed_url") or "")
                break
            try:
                await _publish_news_entry(
                    context,
                    item["title"],
                    item["summary"],
                    item["link"],
                    urgent=False,
                    feed_url=item.get("feed_url") or "",
                )
            except Exception:
                continue
            remember_link(item["link"])
            remember_title(item["title_norm"])
            remember_title_tokens(item["title"])
            mark_news_sent()
            posted += 1
        return

    if NEWS_AI_FILTER_ENABLED and not ai_enabled() and candidates:
        logger.warning("AI unavailable; falling back to keyword filter")
    if not _keyword_fallback_enabled():
        return

    keyword_items: List[Dict[str, object]] = []
    for item in candidates:
        if urgent_by_keywords(item["title"], item["summary"]):
            hits = keyword_hits(item["title"]) + keyword_hits(item["summary"])
            item["keyword_hits"] = hits
            deprior_hits = item.get("deprior_hits", 0) or 0
            whitelist_hits = item.get("whitelist_hits", 0) or 0
            recency = max(0.0, NEWS_RECENCY_BOOST - item["age_hours"])
            feed_boost = NEWS_PRIORITY_FEED_BOOST if _feed_matches(str(item.get("feed_url") or ""), NEWS_PRIORITY_FEEDS) else 0
            item["score"] = (
                hits * 10 +
                recency +
                whitelist_hits * NEWS_WHITELIST_BOOST -
                deprior_hits * NEWS_DEPRIORITY_PENALTY +
                feed_boost
            )
            keyword_items.append(item)
        else:
            if (item.get("whitelist_hits", 0) or item.get("interest_hits", 0)):
                _record_news_skip("keyword_no_match", item["title"], item["summary"], item.get("feed_url") or "")

    if not keyword_items:
        return

    keyword_items.sort(
        key=lambda x: (
            x.get("score", 0),
            x.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    posted = 0
    for idx, item in enumerate(keyword_items):
        if NEWS_MAX_POSTS_PER_RUN > 0 and posted >= NEWS_MAX_POSTS_PER_RUN:
            for rest in keyword_items[idx:]:
                _record_news_skip("max_per_run", rest["title"], rest["summary"], rest.get("feed_url") or "")
            break
        if not news_rate_ok():
            for rest in keyword_items[idx:]:
                _record_news_skip("rate_limited", rest["title"], rest["summary"], rest.get("feed_url") or "")
            break
        try:
            await _publish_news_entry(
                context,
                item["title"],
                item["summary"],
                item["link"],
                urgent=False,
                feed_url=item.get("feed_url") or "",
            )
        except Exception:
            continue
        remember_link(item["link"])
        remember_title(item["title_norm"])
        remember_title_tokens(item["title"])
        mark_news_sent()
        posted += 1

async def news_job(context: ContextTypes.DEFAULT_TYPE):
    if _news_job_lock.locked():
        logger.info("news_job skipped: previous run still in progress")
        return
    async with _news_job_lock:
        await _news_job_inner(context)

async def _collect_summary_items() -> List[Dict[str, object]]:
    if not RSS_FEEDS:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, NEWS_SUMMARY_LOOKBACK_HOURS))
    items: List[Dict[str, object]] = []
    seen: Set[str] = set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for feed_url in RSS_FEEDS:
            if not _feed_allowed(feed_url) or _feed_disabled(feed_url):
                continue
            try:
                feed_text = await fetch_feed_text(client, feed_url)
                if not feed_text:
                    continue
                feed = feedparser.parse(feed_text)
                for entry in (feed.entries or [])[:30]:
                    title = getattr(entry, "title", "") or ""
                    link = getattr(entry, "link", "") or ""
                    link_key = normalize_link(link)
                    if not title or not link_key or link_key in seen:
                        continue
                    if link_key in _summary_seen:
                        continue
                    published = _entry_datetime(entry)
                    if published and published < cutoff:
                        continue
                    summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                    if _block_hits(f"{title}\n{summary}") > 0:
                        continue
                    items.append({
                        "title": title.strip(),
                        "link": link_key.strip(),
                        "summary": _clean_html(summary),
                        "published": published,
                    })
                    seen.add(link_key)
                if feed.entries:
                    _feed_record_success(feed_url)
                else:
                    _feed_record_empty(feed_url)
            except Exception:
                _feed_record_failure(feed_url, "summary fetch")
                logger.exception("summary feed error for %s", feed_url)
                continue
    items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items[:max(1, NEWS_SUMMARY_MAX_ITEMS)]

async def _ai_news_summary(items: List[Dict[str, object]]) -> str:
    if not ai_enabled():
        logger.debug("News AI summary skipped: %s", _ai_status_reason())
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
                timeout=NEWS_AI_TIMEOUT_SEC,
            ),
            timeout=NEWS_AI_TIMEOUT_SEC,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out
    except asyncio.TimeoutError:
        _ai_register_timeout("news_summary")
        logger.warning("AI summary timed out")
        return ""
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("AI summary failed")
        return ""

async def _build_news_summary_text() -> tuple[str, List[str], str, List[Dict[str, object]]]:
    items = await _collect_summary_items()
    if not items:
        return "", [], "", []
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
    links = [it["link"] for it in items if it.get("link")]
    return body, links, ai_text, items

async def _post_news_summary(context: ContextTypes.DEFAULT_TYPE) -> bool:
    text, links, ai_text, items = await _build_news_summary_text()
    if not text:
        logger.info("News summary skipped: no items")
        return False
    delivered = False
    image_bytes = await _generate_summary_image(items, ai_text)
    if NEWS_SUMMARY_SEND_TO_CHANNEL and NEWS_CHANNEL_ID:
        try:
            if image_bytes:
                await context.bot.send_photo(
                    chat_id=NEWS_CHANNEL_ID,
                    photo=InputFile(io.BytesIO(image_bytes), filename="digest.png"),
                    caption="🗞️ Зведення новин України",
                )
            await context.bot.send_message(
                chat_id=NEWS_CHANNEL_ID,
                text=_append_footer(text, NEWS_CHANNEL_ID),
                disable_web_page_preview=True,
            )
            delivered = True
        except Exception:
            logger.exception("Summary post failed")
    if delivered and links:
        _mark_summary_links(links)
    if delivered:
        logger.info("News summary delivered: items=%s image=%s", len(items), bool(image_bytes))
    return delivered

async def news_summary_job(context: ContextTypes.DEFAULT_TYPE):
    if not NEWS_SUMMARY_ENABLED:
        return
    await _post_news_summary(context)

# =========================
# Channel posts (hourly content)
# =========================
CHANNEL_DEFAULT_TOPICS = [
    "if_no_internet",
    "value_simple",
    "if_no_mobile",
    "forward_close",
    "save_checklist",
    "quiet",
    "if_not_home",
    "not_news",
    "if_family",
    "pinned",
    "prepare",
]

CHANNEL_DEFAULT_TOPICS_BY_BLOCK = {
    "1": ["save_checklist", "prepare", "value_simple"],
    "2": ["if_no_internet", "if_no_mobile", "if_not_home", "if_family"],
    "3": ["forward_close"],
    "4": ["not_news"],
    "5": ["pinned"],
    "6": ["value_simple"],
    "7": ["prepare"],
    "8": ["save_checklist"],
    "9": ["if_family"],
}

CHANNEL_TOPIC_ALIASES = {
    "if_no_internet": "if_no_internet",
    "no_internet": "if_no_internet",
    "if_no_mobile": "if_no_mobile",
    "no_mobile": "if_no_mobile",
    "if_not_home": "if_not_home",
    "not_home": "if_not_home",
    "if_family": "if_family",
    "family": "if_family",
    "prepare": "prepare",
    "prep": "prepare",
    "value_simple": "value_simple",
    "value": "value_simple",
    "forward_close": "forward_close",
    "forward": "forward_close",
    "quiet": "quiet",
    "weekly_summary": "weekly_summary",
    "not_news": "not_news",
    "pinned": "pinned",
    "save_checklist": "save_checklist",
    "checklist": "save_checklist",
}

CHANNEL_CTA_TEXT = {
    "uk": "🔁 Збережи собі та перешли близьким.",
    "en": "🔁 Save this and forward to close ones.",
}

CHANNEL_LOW_VALUE_PHRASES = [
    "заспокойся",
    "слідкуй за інструкціями",
    "дій за інструкціями",
    "follow the channel instructions",
    "follow instructions",
    "stay calm",
    "keep calm",
    "be ready",
    "будь готов",
    "ми на звʼязку",
    "we are here",
]

CHANNEL_ACTION_KEYWORDS = {
    "uk": {
        "power": ["заряд", "енергозбереж", "павербанк"],
        "offline_contacts": ["контакт", "офлайн"],
        "short_message": ["повідом", "смс", "коротк", "одне повідом"],
        "backup_plan": ["план", "резерв", "спосіб зв", "місце зустріч", "точка зустріч", "зустріч"],
        "connections_off": ["wi-fi", "wifi", "bluetooth", "фонов", "оновлен", "підключенн"],
        "travel": ["дороз", "маршрут", "адрес", "локац", "поїзд"],
        "children": ["діт", "дитин"],
        "power_outage": ["світл", "електр", "блекаут", "ламп", "ліхтар", "свіч"],
        "old_phone": ["стар", "повільн", "памʼят", "пам'ят"],
        "phone_dead": ["розряд", "кабель", "заряд", "павербанк", "акум"],
        "unstable": ["нестаб", "перерив", "зника"],
        "long_outage": ["6 год", "6год", "доб", "доба", "сут"],
        "only_one": ["лише у одного", "одна людина", "один телефон", "координатор"],
        "not_tech": ["не розбира", "не в темі"],
    },
    "en": {
        "power": ["battery", "charge", "power saving", "power bank"],
        "offline_contacts": ["contact", "offline"],
        "short_message": ["message", "text", "short"],
        "backup_plan": ["plan", "backup", "meeting", "meet", "check-in", "way to connect"],
        "connections_off": ["wifi", "wi-fi", "bluetooth", "background"],
        "travel": ["travel", "road", "route", "address", "location"],
        "children": ["child", "kids"],
        "power_outage": ["power outage", "blackout", "electricity", "lamp", "flashlight", "candle"],
        "old_phone": ["old phone", "slow", "storage"],
        "phone_dead": ["dead", "charger", "cable", "power bank", "battery"],
        "unstable": ["unstable", "drops", "weak signal"],
        "long_outage": ["6 hours", "24 hours", "one day", "a day"],
        "only_one": ["only one", "one person", "coordinator"],
        "not_tech": ["not technical", "not tech", "not into"],
    },
}

CHANNEL_REQUIRED_ACTION_HINTS = {
    "uk": [
        "заряд/енергозбереження",
        "офлайн-контакти",
        "коротке повідомлення близьким",
        "резервний спосіб звʼязку або план",
    ],
    "en": [
        "battery/power saving",
        "offline contacts",
        "one short message to close ones",
        "backup way to connect or meeting plan",
    ],
}

CHANNEL_POSTS_STATE_FILE = _resolve_path(env("CHANNEL_POSTS_STATE_FILE", "data/channel_posts_state.json"))
_channel_posts_index = 0
_channel_posts_images_disabled_until = 0.0
CHANNEL_POSTS_ACTIONS_FILE = _resolve_path(env("CHANNEL_POSTS_ACTIONS_FILE", "data/channel_posts_actions.json"))
_channel_recent_actions: deque[str] = deque()
CHANNEL_POSTS_TOPICS_HISTORY_FILE = _resolve_path(env("CHANNEL_POSTS_TOPICS_HISTORY_FILE", "data/channel_posts_topics.json"))
_channel_recent_topics: deque[Dict[str, object]] = deque()

def _channel_post_lang() -> str:
    return CHANNEL_POSTS_LANG if CHANNEL_POSTS_LANG in ("uk", "en") else "uk"

def _channel_topics_from_file() -> List[str]:
    if not CHANNEL_POSTS_TOPICS_FILE:
        return []
    path = _resolve_path(CHANNEL_POSTS_TOPICS_FILE)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.exception("Failed to load channel topics file")
        return []
    topics: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("//") or line.startswith(";"):
            continue
        topics.append(line)
    return topics

_BLOCK_HEADER_RE = re.compile(r"блок\s*(\d+)", re.IGNORECASE)

def _channel_topics_from_file_by_block() -> Dict[str, List[str]]:
    if not CHANNEL_POSTS_TOPICS_FILE:
        return {}
    path = _resolve_path(CHANNEL_POSTS_TOPICS_FILE)
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.exception("Failed to load channel topics file")
        return {}
    block = None
    topics_by_block: Dict[str, List[str]] = {}
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#") or raw.startswith("//") or raw.startswith(";"):
            match = _BLOCK_HEADER_RE.search(raw)
            if match:
                block = match.group(1)
            continue
        if block:
            topics_by_block.setdefault(block, []).append(raw)
        else:
            topics_by_block.setdefault("0", []).append(raw)
    return topics_by_block

def _channel_topics() -> List[str]:
    if CHANNEL_POSTS_TOPICS_RAW:
        items = [t.strip() for t in CHANNEL_POSTS_TOPICS_RAW.split(",") if t.strip()]
        if items:
            return items
    file_topics = _channel_topics_from_file()
    if file_topics:
        return file_topics
    return CHANNEL_DEFAULT_TOPICS

def _channel_topics_by_block() -> Dict[str, List[str]]:
    topics_by_block = _channel_topics_from_file_by_block()
    if topics_by_block:
        return topics_by_block
    return CHANNEL_DEFAULT_TOPICS_BY_BLOCK

def _weekly_plan_slot(now: datetime) -> str:
    morning = now.hour < 14
    weekday = now.weekday()
    schedule = {
        0: ("7", "2"),  # Mon
        1: ("4", "3"),  # Tue
        2: ("6", "8"),  # Wed
        3: ("1", "9"),  # Thu
        4: ("5", "7"),  # Fri
        5: ("2", "8"),  # Sat
        6: ("quiet", "weekly_summary"),  # Sun
    }
    slot = schedule.get(weekday, ("1", "2"))
    return slot[0] if morning else slot[1]

def _pick_topic_from_list(topics: List[str]) -> str:
    if not topics:
        return "status"
    global _channel_posts_index
    start_idx = _channel_posts_index % len(topics)
    for offset in range(len(topics)):
        idx = (start_idx + offset) % len(topics)
        candidate = topics[idx]
        if not _topic_recently_used(candidate):
            _channel_posts_index = start_idx + offset + 1
            _save_channel_posts_state()
            return candidate
    chosen = topics[start_idx]
    _channel_posts_index = start_idx + 1
    _save_channel_posts_state()
    return chosen

def _normalize_channel_topic(raw: str) -> str:
    key = (raw or "").strip().lower()
    return CHANNEL_TOPIC_ALIASES.get(key, key)

def _load_channel_posts_state() -> None:
    global _channel_posts_index
    if not CHANNEL_POSTS_STATE_FILE.exists():
        return
    try:
        data = json.loads(CHANNEL_POSTS_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load channel post state")
        return
    if isinstance(data, dict):
        idx = data.get("idx")
        if isinstance(idx, int) and idx >= 0:
            _channel_posts_index = idx

def _save_channel_posts_state() -> None:
    try:
        CHANNEL_POSTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHANNEL_POSTS_STATE_FILE.write_text(json.dumps({"idx": _channel_posts_index}), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save channel post state")

def _load_channel_topics_history() -> None:
    if not CHANNEL_POSTS_TOPICS_HISTORY_FILE.exists():
        return
    try:
        data = json.loads(CHANNEL_POSTS_TOPICS_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load channel topics history")
        return
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic")
            ts = item.get("ts")
            if not isinstance(topic, str) or not topic:
                continue
            try:
                ts_val = float(ts)
            except Exception:
                continue
            _channel_recent_topics.append({"topic": topic, "ts": ts_val})
    _prune_topic_history()

def _save_channel_topics_history() -> None:
    try:
        CHANNEL_POSTS_TOPICS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHANNEL_POSTS_TOPICS_HISTORY_FILE.write_text(
            json.dumps(list(_channel_recent_topics)), encoding="utf-8"
        )
    except Exception:
        logger.exception("Failed to save channel topics history")

def _normalize_topic_name(topic: str) -> str:
    return " ".join((topic or "").strip().lower().split())

def _prune_topic_history() -> None:
    if CHANNEL_POSTS_TOPIC_HISTORY_MAX <= 0:
        _channel_recent_topics.clear()
        return
    while len(_channel_recent_topics) > CHANNEL_POSTS_TOPIC_HISTORY_MAX:
        _channel_recent_topics.popleft()

def _topic_recently_used(topic: str) -> bool:
    if CHANNEL_POSTS_TOPIC_REPEAT_HOURS <= 0:
        return False
    norm = _normalize_topic_name(topic)
    if not norm:
        return False
    cutoff = time.time() - CHANNEL_POSTS_TOPIC_REPEAT_HOURS * 3600
    for item in reversed(_channel_recent_topics):
        try:
            ts = float(item.get("ts") or 0)
        except Exception:
            continue
        if ts < cutoff:
            break
        if _normalize_topic_name(str(item.get("topic") or "")) == norm:
            return True
    return False

def _remember_channel_topic(topic: str) -> None:
    if not topic:
        return
    _channel_recent_topics.append({"topic": topic, "ts": time.time()})
    _prune_topic_history()
    _save_channel_topics_history()

def _load_channel_actions() -> None:
    if not CHANNEL_POSTS_ACTIONS_FILE.exists():
        return
    try:
        data = json.loads(CHANNEL_POSTS_ACTIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load channel actions")
        return
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, str):
                continue
            if item:
                _channel_recent_actions.append(item)
    while len(_channel_recent_actions) > CHANNEL_POSTS_ACTION_HISTORY_MAX:
        _channel_recent_actions.popleft()

def _save_channel_actions() -> None:
    try:
        CHANNEL_POSTS_ACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHANNEL_POSTS_ACTIONS_FILE.write_text(
            json.dumps(list(_channel_recent_actions)), encoding="utf-8"
        )
    except Exception:
        logger.exception("Failed to save channel actions")

def _next_channel_topic() -> str:
    if CHANNEL_POSTS_USE_WEEKLY_PLAN:
        slot = _weekly_plan_slot(_channel_now())
        if slot in ("quiet", "weekly_summary"):
            return slot
        topics_by_block = _channel_topics_by_block()
        block_topics = topics_by_block.get(slot, [])
        if block_topics:
            return _pick_topic_from_list(block_topics)
    return _pick_topic_from_list(_channel_topics())

_load_channel_posts_state()
_load_channel_topics_history()
_load_channel_actions()

def _channel_bot_line(lang: str) -> str:
    label = "Бот:" if lang == "uk" else "Bot:"
    link = _footer_bot_link()
    if link:
        return f"{label} {link}"
    hint = "посилання у профілі каналу" if lang == "uk" else "link in the channel profile"
    return f"{label} {hint}"

def _channel_ai_instructions(lang: str) -> str:
    language = "Ukrainian" if lang == "uk" else "English"
    return (
        "You write short official posts for a Telegram channel.\n"
        "HARD RULES:\n"
        "1) Write ONLY in Ukrainian or English as requested.\n"
        "2) NEVER use Russian.\n"
        "3) No technical details (frequencies, keys, configs, onboarding steps).\n"
        "4) No news, no analysis, no dates.\n"
        "5) Keep it short, calm, factual.\n"
        "6) Use simple words. Do not mention AI.\n"
        "7) Every line must be a concrete, practical action or a short lifehack tip. No vague phrases.\n"
        "8) Avoid repeats and filler like 'follow instructions' or 'stay calm'.\n"
        "9) If relevant, include 1-2 lifehacks (charging without grid, long-lasting light) but avoid impossible claims.\n"
        f"10) Each action line should be {CHANNEL_POSTS_ACTION_MIN_WORDS}-{CHANNEL_POSTS_ACTION_MAX_WORDS} words.\n"
        f"Language: {language}.\n"
    )

def _channel_post_image_skip_reason() -> str:
    if not CHANNEL_POSTS_IMAGE_ENABLED:
        return "CHANNEL_POSTS_IMAGE_ENABLED=false"
    if time.time() < _channel_posts_images_disabled_until:
        return "temporarily disabled"
    if not ai_enabled():
        return "AI unavailable"
    return ""

def _disable_channel_post_images_temporarily(reason: str) -> None:
    global _channel_posts_images_disabled_until
    if CHANNEL_POSTS_IMAGE_TEMP_DISABLE_SEC <= 0:
        return
    _channel_posts_images_disabled_until = max(
        _channel_posts_images_disabled_until,
        time.time() + CHANNEL_POSTS_IMAGE_TEMP_DISABLE_SEC,
    )
    logger.warning(
        "Channel post images temporarily disabled for %ss: %s",
        CHANNEL_POSTS_IMAGE_TEMP_DISABLE_SEC,
        reason,
    )

def _channel_post_image_prompt(topic: str, text: str) -> str:
    topic = clip(topic or "", 140)
    summary = clip(_clean_html(text or ""), 600)
    base = (
        "Create a single, safe-for-work illustration for a Telegram channel post about emergency "
        "communication readiness. Use a calm, clear visual style. No text overlays, no logos, no "
        "technical diagrams, no maps, no violence."
    )
    style_line = _image_style_line("channel")
    if style_line:
        base = base + " " + style_line
    return f"{base}\n\nTOPIC: {topic}\nPOST: {summary}"

async def _generate_channel_post_image(topic: str, text: str) -> Optional[bytes]:
    reason = _channel_post_image_skip_reason()
    if reason:
        logger.info("Channel post image skipped: %s", reason)
        return None
    prompt = _channel_post_image_prompt(topic, text)
    logger.debug(
        "Channel post image request: model=%s size=%s prompt_len=%s",
        CHANNEL_POSTS_IMAGE_MODEL,
        CHANNEL_POSTS_IMAGE_SIZE,
        len(prompt),
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.images.generate,
                model=CHANNEL_POSTS_IMAGE_MODEL,
                prompt=prompt,
                size=CHANNEL_POSTS_IMAGE_SIZE,
            ),
            timeout=CHANNEL_POSTS_IMAGE_TIMEOUT_SEC,
        )
        data = getattr(resp, "data", None) or []
        if not data:
            logger.warning("Channel post image generation returned empty data")
            return None
        item = data[0]
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(item, "url", None)
        if url:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url)
                if r.status_code < 400:
                    return r.content
                logger.warning("Channel post image download failed: %s", r.status_code)
        return None
    except asyncio.TimeoutError:
        _ai_register_timeout("channel_post_image")
        logger.warning("Channel post image generation timed out")
        return None
    except Exception as exc:
        if _is_permission_denied(exc):
            _disable_channel_post_images_temporarily("permission denied")
            return None
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("Channel post image generation failed")
        return None

def _channel_checklist_prompt(title_uk: str, title_en: str, lang: str) -> str:
    title = title_uk if lang == "uk" else title_en
    cta = CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"])
    if lang == "uk":
        return (
            "Напиши короткий пост-інструкцію.\n"
            f"Заголовок: {title}\n"
            "Формат:\n"
            "ЗАГОЛОВОК\n\n"
            "1) ...\n"
            "2) ...\n"
            "... (5–7 коротких лайфхаків/дій)\n"
            "Кожен пункт = конкретна дія, починається з дієслова.\n"
            "Додай 1–2 короткі лайфхаки або важливі дані, якщо доречно.\n"
            "Без загальних фраз типу «слідкуй за інструкціями».\n"
            f"\n{cta}\n\n{_channel_prompt_requirements(title, lang)}"
        )
    return (
        "Write a short checklist post.\n"
        f"Title: {title}\n"
        "Format:\n"
        "TITLE\n\n"
        "1) ...\n"
        "2) ...\n"
        "... (5–7 lifehacks/actions)\n"
        "Each point must be a concrete action starting with a verb.\n"
        "Add 1–2 short lifehacks or key tips if relevant.\n"
        "Avoid vague lines like 'follow instructions'.\n"
        f"\n{cta}\n\n{_channel_prompt_requirements(title, lang)}"
    )

def _topic_lower(text: str) -> str:
    return (text or "").strip().lower()

def _channel_auto_mode(topic: str) -> str:
    t = _topic_lower(topic)
    if not t:
        return "checklist"
    if t.startswith(("перешли", "передай", "forward")):
        return "forward"
    if "нічого робити не потрібно" in t or "ничего делать не нужно" in t or "no action is needed" in t:
        return "quiet"
    if t.startswith(("якщо", "если", "if ")):
        return "scenario"
    if t.startswith(("чому", "почему", "why", "міф", "миф", "myth")):
        return "myth"
    if t.startswith(("що означає", "что значит", "what does", "як працює", "как работает", "how ",
                     "що відбувається", "что происходит", "what happens", "чим цей канал", "чем этот канал", "для кого",
                     "who this", "what we check", "що ми перевіряємо", "что мы проверяем", "чому ми", "почему мы",
                     "система активна", "system active")):
        return "explain"
    if t.startswith((
        "мінімальний набір", "минимальный набор", "що перевірити", "что проверить", "що важливо зарядити",
        "что важно зарядить", "які застосунки", "какие приложения", "як підготувати", "как подготовить",
        "як зберегти", "как сохранить", "що робити", "что делать", "what to do"
    )):
        return "checklist"
    return "checklist"

def _channel_scenario_prompt(title: str, lang: str) -> str:
    cta = CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"])
    if lang == "uk":
        return (
            "Напиши сценарний пост.\n"
            f"Заголовок: {title}\n"
            "Формат:\n"
            "ЗАГОЛОВОК\n\n"
            "A → B → C → D → E (5–7 коротких практичних кроків)\n"
            "Кожен крок = конкретна дія.\n"
            f"\n{cta}\n\n{_channel_prompt_requirements(title, lang)}"
        )
    return (
        "Write a scenario post.\n"
        f"Title: {title}\n"
        "Format:\n"
        "TITLE\n\n"
        "A → B → C → D → E (5–7 short practical steps)\n"
        "Each step must be a concrete action.\n"
        f"\n{cta}\n\n{_channel_prompt_requirements(title, lang)}"
    )

def _channel_explain_prompt(title: str, lang: str) -> str:
    cta = CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"])
    if lang == "uk":
        return (
            "Напиши короткий пояснювальний пост.\n"
            f"Заголовок: {title}\n"
            "Формат:\n"
            "ЗАГОЛОВОК\n"
            "• 5–7 коротких пунктів простими словами\n"
            "Кожен пункт має конкретну практичну дію.\n"
            "Без технічних деталей, без дат, без новин.\n"
            f"{cta}\n\n{_channel_prompt_requirements(title, lang)}"
        )
    return (
        "Write a short explanatory post.\n"
        f"Title: {title}\n"
        "Format:\n"
        "TITLE\n"
        "• 5–7 short bullet points in simple words\n"
        "Each point must be a concrete practical action.\n"
        "No technical details, no dates, no news.\n"
        f"{cta}\n\n{_channel_prompt_requirements(title, lang)}"
    )

def _channel_myth_prompt(title: str, lang: str) -> str:
    cta = CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"])
    if lang == "uk":
        return (
            "Напиши пост «розвінчання міфу» як інструкцію.\n"
            f"Заголовок: {title}\n"
            "Формат:\n"
            "ЗАГОЛОВОК\n"
            "• 5–7 коротких пунктів\n"
            "Кожен пункт = практична дія, яка знімає міф.\n"
            f"{cta}\n\n{_channel_prompt_requirements(title, lang)}"
        )
    return (
        "Write a myth-busting post as instructions.\n"
        f"Title: {title}\n"
        "Format:\n"
        "TITLE\n"
        "• 5–7 short points\n"
        "Each point is a practical action that removes the myth.\n"
        f"{cta}\n\n{_channel_prompt_requirements(title, lang)}"
    )

def _channel_forward_prompt(title: str, lang: str) -> str:
    cta = CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"])
    if lang == "uk":
        return (
            f"Заголовок: {title}\n"
            "Далі 5–7 коротких рядків простими словами.\n"
            "Кожен рядок = конкретна дія.\n"
            "Без складних термінів.\n"
            f"{cta}\n\n{_channel_prompt_requirements(title, lang)}"
        )
    return (
        f"Title: {title}\n"
        "Then 5–7 short lines in simple words.\n"
        "Each line must be a concrete action.\n"
        "No complex terms.\n"
        f"{cta}\n\n{_channel_prompt_requirements(title, lang)}"
    )

def _channel_quiet_prompt(lang: str) -> str:
    if lang == "uk":
        return (
            "Пост у стилі «тихе присутність».\n"
            "Почни рядком: Нічого робити не потрібно. Просто збережи.\n"
            "Додай 5–7 коротких ПРАКТИЧНИХ дій (наприклад: перевір заряд).\n"
            "Без емоцій і без загальних фраз.\n\n"
            + _channel_prompt_requirements("тихе присутність", lang)
        )
    return (
        "A quiet-presence post.\n"
        "Start with: No action is needed. Just save this.\n"
        "Add 5–7 short PRACTICAL actions (e.g., check battery).\n"
        "No emotions and no vague phrases.\n\n"
        + _channel_prompt_requirements("quiet presence", lang)
    )

def _channel_is_low_value(text: str) -> bool:
    if not text:
        return True
    t = text.strip().lower()
    return any(phrase in t for phrase in CHANNEL_LOW_VALUE_PHRASES)

_ACTION_ITEM_RE = re.compile(r"^\s*(?:\d+[\).]|[•*\-])\s+")

def _extract_action_lines(text: str) -> List[str]:
    if not text:
        return []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    actions: List[str] = []
    for line in lines:
        if _ACTION_ITEM_RE.match(line):
            actions.append(_ACTION_ITEM_RE.sub("", line).strip())
    if actions:
        return actions
    for line in lines:
        if "→" in line or "->" in line:
            sep = "→" if "→" in line else "->"
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts
    return []

def _count_action_items(text: str) -> int:
    return len(_extract_action_lines(text))

def _action_word_count(text: str) -> int:
    return len(re.findall(r"[0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+", text or ""))

def _normalize_action_line(text: str) -> str:
    text = _ACTION_ITEM_RE.sub("", (text or ""))
    text = re.sub(r"[^\w\s]+", " ", text.lower())
    return " ".join(text.split())

def _channel_action_has_keywords(action: str, keywords: List[str]) -> bool:
    if not action:
        return False
    a = action.lower()
    return any(k in a for k in keywords if k)

def _channel_required_action_templates(lang: str) -> Dict[str, List[str]]:
    if lang == "uk":
        return {
            "power": [
                "Увімкни енергозбереження",
                "Заряди телефон і павербанк",
                "Перевір заряд телефону",
                "Заряджай телефон від авто",
                "Заряджай від ноутбука через USB",
            ],
            "offline_contacts": [
                "Збережи контакти офлайн",
                "Запиши важливі номери на папері",
            ],
            "short_message": [
                "Підготуй одне коротке повідомлення",
                "Надішли одне коротке повідомлення",
            ],
            "backup_plan": [
                "Домовся про резервний спосіб звʼязку",
                "Узгодь час короткої перевірки",
                "Признач точку зустрічі",
            ],
        }
    return {
        "power": [
            "Enable power saving",
            "Charge your phone and power bank",
            "Check your phone battery",
            "Charge your phone from a car",
            "Charge via a laptop USB port",
        ],
        "offline_contacts": [
            "Save contacts offline",
            "Write key numbers on paper",
        ],
        "short_message": [
            "Prepare one short message",
            "Send one short message",
        ],
        "backup_plan": [
            "Agree on a backup way to connect",
            "Set a short check-in time",
            "Set a meeting point",
        ],
    }

def _channel_generic_action_templates(lang: str) -> List[str]:
    if lang == "uk":
        return [
            "Вимкни Wi-Fi і Bluetooth",
            "Увімкни енергозбереження",
            "Збережи контакти офлайн",
            "Підготуй одне коротке повідомлення",
            "Домовся про резервний спосіб звʼязку",
            "Перевір заряд телефону",
            "Обмеж фонові оновлення",
            "Підготуй ліхтарик на батарейках",
            "Використай LED-лампу замість екрана",
        ]
    return [
        "Disable Wi-Fi and Bluetooth",
        "Enable power saving",
        "Save contacts offline",
        "Prepare one short message",
        "Agree on a backup way to connect",
        "Check your phone battery",
        "Limit background updates",
        "Keep a battery flashlight ready",
        "Use an LED lamp instead of the screen",
    ]

def _channel_lifehack_templates(lang: str) -> List[str]:
    if lang == "uk":
        return [
            "Зменш яскравість екрана",
            "Увімкни режим польоту без мережі",
            "Тримай запасний кабель у сумці",
            "Заряджай телефон від авто",
            "Заряджай від ноутбука через USB",
            "Перемкни мережу на 2G/3G",
            "Вимкни геолокацію",
            "Постав нічний режим екрана",
        ]
    return [
        "Lower screen brightness",
        "Use airplane mode without signal",
        "Keep a spare cable in your bag",
        "Charge your phone from a car",
        "Charge via a laptop USB port",
        "Switch to 2G/3G network",
        "Turn off location services",
        "Use night mode on the screen",
    ]

def _channel_fallback_actions(topic: str, lang: str) -> List[str]:
    required_groups = _channel_required_groups(topic)
    required_map = _channel_required_action_templates(lang)
    recent = set(_channel_recent_actions)
    used: Set[str] = set()
    actions: List[str] = []

    for group in required_groups:
        options = list(required_map.get(group, []))
        if not options:
            continue
        random.shuffle(options)
        picked = _pick_action(options, used, recent)
        if not picked:
            picked = _pick_action(options, used, set())
        if picked:
            actions.append(picked)
            used.add(_normalize_action_line(picked))

    lifehacks = _channel_lifehack_templates(lang)
    random.shuffle(lifehacks)
    lifehack_added = 0
    for hack in lifehacks:
        if lifehack_added >= 2 or len(actions) >= 7:
            break
        norm = _normalize_action_line(hack)
        if not norm or norm in used or norm in recent:
            continue
        words = _action_word_count(hack)
        if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
            continue
        actions.append(hack)
        used.add(norm)
        lifehack_added += 1

    extras = _channel_topic_extra_action_templates(topic, lang)
    random.shuffle(extras)
    for extra in extras:
        if len(actions) >= 7:
            break
        norm = _normalize_action_line(extra)
        if not norm or norm in used or norm in recent:
            continue
        words = _action_word_count(extra)
        if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
            continue
        actions.append(extra)
        used.add(norm)

    pool = _channel_generic_action_templates(lang)
    random.shuffle(pool)
    for act in pool:
        if len(actions) >= 7:
            break
        norm = _normalize_action_line(act)
        if not norm or norm in used or norm in recent:
            continue
        words = _action_word_count(act)
        if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
            continue
        actions.append(act)
        used.add(norm)

    if len(actions) < 5:
        for act in extras + pool:
            if len(actions) >= 5:
                break
            norm = _normalize_action_line(act)
            if not norm or norm in used:
                continue
            words = _action_word_count(act)
            if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
                continue
            actions.append(act)
            used.add(norm)

    if len(actions) > 7:
        actions = actions[:7]
    return actions

def _channel_topic_extra_action_templates(topic: str, lang: str) -> List[str]:
    hints = _channel_topic_extra_hints(topic, lang)
    if not hints:
        return []
    mapping_uk = {
        "підготуй павербанк і кабель": "Підготуй павербанк і кабель",
        "повідом маршрут близьким": "Повідом маршрут близьким",
        "запиши контакти для дітей": "Запиши контакти для дітей",
        "очисти памʼять і вимкни фонові оновлення": "Очисти памʼять і вимкни фонові оновлення",
        "підключи павербанк або знайди заряд": "Підключи павербанк або знайди заряд",
        "надсилай дуже короткі повідомлення": "Надсилай дуже короткі повідомлення",
        "узгодь графік коротких перевірок": "Узгодь графік коротких перевірок",
        "признач координатора звʼязку": "Признач координатора звʼязку",
        "попроси допомогу однією фразою": "Попроси допомогу однією фразою",
        "заряджай телефон від авто": "Заряджай телефон від авто",
        "заряджай від ноутбука через usb": "Заряджай від ноутбука через USB",
        "підготуй ліхтарик на батарейках": "Підготуй ліхтарик на батарейках",
        "використай led-лампу замість екрана": "Використай LED-лампу замість екрана",
    }
    mapping_en = {
        "prepare a power bank and cable": "Prepare a power bank and cable",
        "share your route with close ones": "Share your route with close ones",
        "write key contacts for kids": "Write key contacts for kids",
        "clear storage and disable background updates": "Clear storage and disable background updates",
        "use a power bank or find charging": "Use a power bank or find charging",
        "send very short messages": "Send very short messages",
        "set a short check-in schedule": "Set a short check-in schedule",
        "assign one coordinator": "Assign one coordinator",
        "ask for help in one sentence": "Ask for help in one sentence",
        "charge your phone from a car": "Charge your phone from a car",
        "charge via a laptop usb port": "Charge via a laptop USB port",
        "keep a battery flashlight ready": "Keep a battery flashlight ready",
        "use an led lamp instead of the screen": "Use an LED lamp instead of the screen",
    }
    mapping = mapping_uk if lang == "uk" else mapping_en
    return [mapping.get(h, h) for h in hints]

def _channel_required_groups(topic: str) -> List[str]:
    return ["power", "offline_contacts", "short_message", "backup_plan"]

def _channel_required_action_hints(lang: str) -> str:
    hints = CHANNEL_REQUIRED_ACTION_HINTS.get(lang, CHANNEL_REQUIRED_ACTION_HINTS["uk"])
    return ", ".join(hints)

def _channel_topic_extra_hints(topic: str, lang: str) -> List[str]:
    t = _topic_lower(topic)
    hints: List[str] = []
    kw = CHANNEL_ACTION_KEYWORDS.get(lang, CHANNEL_ACTION_KEYWORDS["uk"])
    if any(k in t for k in kw.get("power_outage", [])):
        hints.append("підготуй павербанк і кабель" if lang == "uk" else "prepare a power bank and cable")
        hints.append("підготуй ліхтарик на батарейках" if lang == "uk" else "keep a battery flashlight ready")
        hints.append("використай led-лампу замість екрана" if lang == "uk" else "use an led lamp instead of the screen")
    if any(k in t for k in kw.get("travel", [])):
        hints.append("повідом маршрут близьким" if lang == "uk" else "share your route with close ones")
    if any(k in t for k in kw.get("children", [])):
        hints.append("запиши контакти для дітей" if lang == "uk" else "write key contacts for kids")
    if any(k in t for k in kw.get("old_phone", [])):
        hints.append("очисти памʼять і вимкни фонові оновлення" if lang == "uk" else "clear storage and disable background updates")
    if any(k in t for k in kw.get("phone_dead", [])):
        hints.append("підключи павербанк або знайди заряд" if lang == "uk" else "use a power bank or find charging")
        hints.append("заряджай телефон від авто" if lang == "uk" else "charge your phone from a car")
        hints.append("заряджай від ноутбука через usb" if lang == "uk" else "charge via a laptop usb port")
    if any(k in t for k in kw.get("unstable", [])):
        hints.append("надсилай дуже короткі повідомлення" if lang == "uk" else "send very short messages")
    if any(k in t for k in kw.get("long_outage", [])):
        hints.append("узгодь графік коротких перевірок" if lang == "uk" else "set a short check-in schedule")
    if any(k in t for k in kw.get("only_one", [])):
        hints.append("признач координатора звʼязку" if lang == "uk" else "assign one coordinator")
    if any(k in t for k in kw.get("not_tech", [])):
        hints.append("попроси допомогу однією фразою" if lang == "uk" else "ask for help in one sentence")
    return hints

def _channel_prompt_requirements(topic: str, lang: str) -> str:
    required = _channel_required_action_hints(lang)
    extra = _channel_topic_extra_hints(topic, lang)
    recent = "; ".join(list(_channel_recent_actions)[-6:])
    word_min = max(1, CHANNEL_POSTS_ACTION_MIN_WORDS)
    word_max = max(word_min, CHANNEL_POSTS_ACTION_MAX_WORDS)
    if lang == "uk":
        lines = [
            f"Обовʼязково включи дії про: {required}.",
            f"Кожен рядок = дія, починай дієсловом, {word_min}-{word_max} слів.",
        ]
        if recent:
            lines.append("Не повторюй такі дії: " + recent + ".")
        if extra:
            lines.append("Додай хоча б одну дію про: " + "; ".join(extra) + ".")
    else:
        lines = [
            f"Mandatory actions: {required}.",
            f"Each line must start with a verb and be {word_min}-{word_max} words.",
        ]
        if recent:
            lines.append("Avoid repeating these actions: " + recent + ".")
        if extra:
            lines.append("Include at least one action about: " + "; ".join(extra) + ".")
    return "\n".join(lines)

def _channel_actions_cover_required(actions: List[str], topic: str, lang: str) -> bool:
    return not _channel_missing_required_groups(actions, topic, lang)

def _channel_missing_required_groups(actions: List[str], topic: str, lang: str) -> List[str]:
    groups = _channel_required_groups(topic)
    kw = CHANNEL_ACTION_KEYWORDS.get(lang, CHANNEL_ACTION_KEYWORDS["uk"])
    missing: List[str] = []
    for group in groups:
        keywords = kw.get(group, [])
        if keywords and not any(_channel_action_has_keywords(action, keywords) for action in actions):
            missing.append(group)
    return missing

def _channel_action_overlap_count(actions: List[str]) -> int:
    if not actions:
        return 0
    recent = set(_channel_recent_actions)
    overlaps = [a for a in actions if a in recent]
    return len(overlaps)

def _remember_channel_actions(actions: List[str]) -> None:
    if not actions:
        return
    for action in actions:
        if not action:
            continue
        _channel_recent_actions.append(action)
        while len(_channel_recent_actions) > CHANNEL_POSTS_ACTION_HISTORY_MAX:
            _channel_recent_actions.popleft()
    _save_channel_actions()

def _channel_validate_actions(actions: List[str], topic: str, lang: str) -> bool:
    if not actions:
        logger.debug("Channel post validation failed: no actions topic=%s lang=%s", topic, lang)
        return False
    if not (5 <= len(actions) <= 7):
        logger.debug(
            "Channel post validation failed: bad action count=%s topic=%s lang=%s",
            len(actions),
            topic,
            lang,
        )
        return False
    missing = _channel_missing_required_groups(actions, topic, lang)
    if missing:
        logger.debug(
            "Channel post validation failed: missing required groups=%s topic=%s lang=%s",
            ",".join(missing),
            topic,
            lang,
        )
        return False
    seen: Set[str] = set()
    for action in actions:
        if _channel_is_low_value(action):
            logger.debug(
                "Channel post validation failed: low value action=%s topic=%s lang=%s",
                clip(action, 80),
                topic,
                lang,
            )
            return False
        words = _action_word_count(action)
        if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
            logger.debug(
                "Channel post validation failed: action word count=%s action=%s topic=%s lang=%s",
                words,
                clip(action, 80),
                topic,
                lang,
            )
            return False
        norm = _normalize_action_line(action)
        if not norm or norm in seen:
            logger.debug(
                "Channel post validation failed: duplicate or empty action=%s topic=%s lang=%s",
                clip(action, 80),
                topic,
                lang,
            )
            return False
        seen.add(norm)
    overlap_count = _channel_action_overlap_count(list(seen))
    if overlap_count > CHANNEL_POSTS_ACTION_REPEAT_MAX:
        logger.debug(
            "Channel post validation failed: repeated actions overlap=%s max=%s topic=%s lang=%s",
            overlap_count,
            CHANNEL_POSTS_ACTION_REPEAT_MAX,
            topic,
            lang,
        )
        return False
    return True

def _pick_action(actions: List[str], used: Set[str], recent: Set[str]) -> Optional[str]:
    for act in actions:
        norm = _normalize_action_line(act)
        if not norm or norm in used or norm in recent:
            continue
        words = _action_word_count(act)
        if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
            continue
        return act
    return None

def _channel_fill_actions(source_actions: List[str], topic: str, lang: str) -> List[str]:
    cleaned: List[str] = []
    seen: Set[str] = set()
    recent = set(_channel_recent_actions)
    for action in source_actions:
        action = (action or "").strip()
        if not action or _channel_is_low_value(action):
            continue
        words = _action_word_count(action)
        if words < CHANNEL_POSTS_ACTION_MIN_WORDS or words > CHANNEL_POSTS_ACTION_MAX_WORDS:
            continue
        norm = _normalize_action_line(action)
        if not norm or norm in seen:
            continue
        if norm in recent:
            continue
        cleaned.append(action)
        seen.add(norm)

    templates = _channel_required_action_templates(lang)
    keywords = CHANNEL_ACTION_KEYWORDS.get(lang, CHANNEL_ACTION_KEYWORDS["uk"])
    for group in _channel_required_groups(topic):
        keys = keywords.get(group, [])
        if keys and any(_channel_action_has_keywords(a, keys) for a in cleaned):
            continue
        pick = _pick_action(templates.get(group, []), seen, recent)
        if pick:
            cleaned.append(pick)
            seen.add(_normalize_action_line(pick))

    for extra in _channel_topic_extra_action_templates(topic, lang):
        if len(cleaned) >= 7:
            break
        norm = _normalize_action_line(extra)
        if not norm or norm in seen or norm in recent:
            continue
        cleaned.append(extra)
        seen.add(norm)

    if len(cleaned) < 5:
        for filler in _channel_generic_action_templates(lang):
            if len(cleaned) >= 5:
                break
            norm = _normalize_action_line(filler)
            if not norm or norm in seen or norm in recent:
                continue
            cleaned.append(filler)
            seen.add(norm)

    if len(cleaned) > 7:
        required_groups = _channel_required_groups(topic)
        prioritized: List[str] = []
        remaining = list(cleaned)
        for group in required_groups:
            keys = keywords.get(group, [])
            for action in remaining:
                if keys and _channel_action_has_keywords(action, keys):
                    prioritized.append(action)
                    remaining.remove(action)
                    break
        for action in remaining:
            if len(prioritized) >= 7:
                break
            prioritized.append(action)
        cleaned = prioritized[:7]

    return cleaned

def _channel_format_actions_post(topic: str, actions: List[str], lang: str) -> str:
    title = (topic or "").strip()
    if not title:
        title = "Коротка інструкція" if lang == "uk" else "Quick checklist"
    badge = _channel_badge_for_topic(title, lang)
    hint = "Лайфхаки та важливі деталі:" if lang == "uk" else "Lifehacks and key tips:"
    lines = [badge, title, "", hint] if badge else [title, "", hint]
    for idx, action in enumerate(actions, 1):
        lines.append(f"{idx}) {action}")
    lines.append("")
    lines.append(CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"]))
    t = _topic_lower(topic)
    if _normalize_channel_topic(topic) == "pinned" or any(k in t for k in ("вхідна", "вход", "закреп", "entry point", "pinned")):
        lines.append(_channel_bot_line(lang))
    return "\n".join(lines).strip()

def _channel_badge_for_topic(topic: str, lang: str) -> str:
    t = (topic or "").strip().lower()
    if not t:
        return ""
    if "лайфхак" in t or "lifehack" in t:
        return "⚡️ Лайфхак дня" if lang == "uk" else "⚡️ Lifehack of the day"
    if t.startswith(("якщо", "если", "if ")):
        return "🧭 Сценарій" if lang == "uk" else "🧭 Scenario"
    if t.startswith(("чому", "почему", "why", "міф", "миф", "myth")):
        return "🧪 Міф/Факт" if lang == "uk" else "🧪 Myth/Fact"
    if "перешли" in t or "forward" in t:
        return "📣 Перешли близьким" if lang == "uk" else "📣 Forward to close ones"
    if "офлайн" in t or "offline" in t:
        return "🧰 Офлайн-безпека" if lang == "uk" else "🧰 Offline safety"
    if "координац" in t or "coordination" in t:
        return "🧭 Координація" if lang == "uk" else "🧭 Coordination"
    if "підсумок тижня" in t or "weekly recap" in t:
        return "🗓️ Підсумок тижня" if lang == "uk" else "🗓️ Weekly recap"
    if "система активна" in t or "system active" in t:
        return "✅ Статус" if lang == "uk" else "✅ Status"
    return ""

def _channel_validate_ai_post(text: str, topic: str, lang: str) -> bool:
    if not text or _channel_is_low_value(text):
        return False
    actions = _extract_action_lines(text)
    return _channel_validate_actions(actions, topic, lang)

def _channel_titled_post(title: str, lines: List[str], cta: Optional[str] = None) -> str:
    parts = [title, ""]
    parts.extend(lines)
    if cta:
        parts.extend(["", cta])
    return "\n".join([p for p in parts if p]).strip()

def _channel_topic_prompt(topic: str, lang: str) -> str:
    key = _normalize_channel_topic(topic)
    if key == "weekly_summary":
        return _channel_checklist_prompt(
            "Підсумок тижня: одна ключова дія",
            "Weekly recap: one key action",
            lang,
        )
    if key == "if_no_internet":
        return _channel_checklist_prompt(
            "Як діяти, якщо немає інтернету",
            "What to do if there is no internet",
            lang,
        )
    if key == "if_no_mobile":
        return _channel_checklist_prompt(
            "Як діяти, якщо немає мобільного звʼязку",
            "What to do if there is no mobile network",
            lang,
        )
    if key == "if_not_home":
        return _channel_checklist_prompt(
            "Як діяти, якщо ти не вдома",
            "What to do if you are not at home",
            lang,
        )
    if key == "if_family":
        return _channel_checklist_prompt(
            "Як діяти, якщо відповідаєш за сімʼю",
            "What to do if you are responsible for family",
            lang,
        )
    if key == "prepare":
        return _channel_checklist_prompt(
            "Підготовка заздалегідь (5 кроків)",
            "Prepare in advance (5 steps)",
            lang,
        )
    if key == "save_checklist":
        return _channel_checklist_prompt(
            "Коротка інструкція на випадок збою звʼязку",
            "Quick checklist for a comms outage",
            lang,
        )
    if key == "value_simple":
        if lang == "uk":
            return (
                "Напиши короткий пост за формулою:\n"
                "❌ Інтернет\n"
                "❌ Мобільний звʼязок\n"
                "✅ Telegram + інструкція\n"
                "Потім 5–7 коротких дій у форматі списку.\n"
                "Кожен пункт = конкретна дія.\n"
                f"Закінчи рядком: {CHANNEL_CTA_TEXT['uk']}\n\n{_channel_prompt_requirements('цінність', lang)}"
            )
        return (
            "Write a short post using this formula:\n"
            "❌ Internet\n"
            "❌ Mobile network\n"
            "✅ Telegram + instructions\n"
            "Then add 5–7 short actions as a list.\n"
            "Each point must be a concrete action.\n"
            f"End with the line: {CHANNEL_CTA_TEXT['en']}\n\n{_channel_prompt_requirements('value', lang)}"
        )
    if key == "forward_close":
        if lang == "uk":
            return (
                "Заголовок: Перешли близьким\n"
                "Далі 3–4 короткі рядки простими словами.\n"
                "Без складних термінів.\n"
                f"Закінчи рядком: {CHANNEL_CTA_TEXT['uk']}"
            )
        return (
            "Title: Forward to close ones\n"
            "Then 3–4 short lines in simple words.\n"
            "No complex terms.\n"
            f"End with the line: {CHANNEL_CTA_TEXT['en']}"
        )
    if key == "quiet":
        return _channel_quiet_prompt(lang)
    if key == "not_news":
        if lang == "uk":
            return (
                "Заголовок: Це не новини\n"
                "2–3 короткі рядки: тут лише короткі інструкції.\n"
                f"Закінчи рядком: {CHANNEL_CTA_TEXT['uk']}"
            )
        return (
            "Title: This is not news\n"
            "2–3 short lines: only short instructions here.\n"
            f"End with the line: {CHANNEL_CTA_TEXT['en']}"
        )
    if key == "pinned":
        bot_line = _channel_bot_line(lang)
        if lang == "uk":
            return (
                "Зроби «вхідний» пост з розділами:\n"
                "Що це / Коли потрібно / Як підключитися.\n"
                "Кожен розділ — 2 короткі ПРАКТИЧНІ дії.\n"
                f"Останній рядок: {bot_line}\n\n{_channel_prompt_requirements('вхідна точка', lang)}"
            )
        return (
            "Create an entry-point post with sections:\n"
            "What this is / When to use / How to connect.\n"
            "Each section: 2 short PRACTICAL actions.\n"
            f"Last line: {bot_line}\n\n{_channel_prompt_requirements('entry point', lang)}"
        )
    theme = topic.strip() or ("Коротка інструкція" if lang == "uk" else "Short checklist")
    mode = _channel_auto_mode(theme)
    if mode == "scenario":
        return _channel_scenario_prompt(theme, lang)
    if mode == "myth":
        return _channel_myth_prompt(theme, lang)
    if mode == "forward":
        return _channel_forward_prompt(theme, lang)
    if mode == "quiet":
        return _channel_quiet_prompt(lang)
    if mode == "explain":
        return _channel_explain_prompt(theme, lang)
    return _channel_checklist_prompt(theme, theme, lang)

def _channel_post_fallback(topic: str, lang: str) -> str:
    key = _normalize_channel_topic(topic)
    cta = CHANNEL_CTA_TEXT.get(lang, CHANNEL_CTA_TEXT["uk"])
    if key == "if_no_internet":
        if lang == "uk":
            return (
                "🧭 Якщо немає інтернету\n\n"
                "1) Увімкни режим енергозбереження\n"
                "2) Вимкни Wi-Fi, Bluetooth і фонові оновлення\n"
                "3) Надішли близьким одне коротке повідомлення\n"
                "4) Домовся про час і спосіб звʼязку\n"
                f"5) Збережи важливі номери офлайн\n\n{cta}"
            )
        return (
            "🧭 If there is no internet\n\n"
            "1) Turn on power saving mode\n"
            "2) Disable Wi-Fi, Bluetooth, and background updates\n"
            "3) Send one short message to close ones\n"
            "4) Agree on time and way to connect\n"
            f"5) Save important numbers offline\n\n{cta}"
        )
    if key == "if_no_mobile":
        if lang == "uk":
            return (
                "🧭 Якщо немає мобільного звʼязку\n\n"
                "1) Перейди ближче до вікна або на відкрите місце\n"
                "2) Увімкни режим енергозбереження\n"
                "3) Надішли коротке повідомлення, коли зʼявиться сигнал\n"
                "4) Домовся про резервний спосіб звʼязку\n"
                "5) Збережи контакти офлайн\n"
                f"6) Тримай телефон зарядженим\n\n{cta}"
            )
        return (
            "🧭 If there is no mobile network\n\n"
            "1) Move closer to a window or open area\n"
            "2) Turn on power saving mode\n"
            "3) Send a short message once signal appears\n"
            "4) Agree on a backup way to connect\n"
            "5) Save contacts offline\n"
            f"6) Keep your phone charged\n\n{cta}"
        )
    if key == "if_not_home":
        if lang == "uk":
            return (
                "🧭 Якщо ти не вдома\n\n"
                "1) Знайди безпечне місце і залишайся там\n"
                "2) Напиши близьким, де ти і що з тобою все гаразд\n"
                "3) Увімкни енергозбереження\n"
                "4) Домовся про місце зустрічі\n"
                "5) Збережи контакти офлайн\n"
                f"6) Плануй короткий маршрут додому\n\n{cta}"
            )
        return (
            "🧭 If you are not at home\n\n"
            "1) Find a safe place and stay there\n"
            "2) Tell close ones where you are and that you are safe\n"
            "3) Turn on power saving mode\n"
            "4) Agree on a meeting point\n"
            "5) Save contacts offline\n"
            f"6) Plan a short route home\n\n{cta}"
        )
    if key == "if_family":
        if lang == "uk":
            return (
                "🧭 Якщо відповідаєш за сімʼю\n\n"
                "1) Перевір, де всі знаходяться\n"
                "2) Узгодь одне коротке правило звʼязку\n"
                "3) Признач спільну точку зустрічі\n"
                "4) Розподіли задачі (заряд, вода, документи)\n"
                "5) Збережи контакти офлайн\n"
                f"6) Надсилай короткі підтвердження\n\n{cta}"
            )
        return (
            "🧭 If you are responsible for family\n\n"
            "1) Check where everyone is\n"
            "2) Agree on one short contact rule\n"
            "3) Set a shared meeting point\n"
            "4) Split tasks (charge, water, documents)\n"
            "5) Save contacts offline\n"
            f"6) Send brief confirmations\n\n{cta}"
        )
    if key == "prepare":
        if lang == "uk":
            return (
                "🧭 Підготовка заздалегідь (5 кроків)\n\n"
                "1) Збережи цей канал і бота\n"
                "2) Домовся з близькими про простий план\n"
                "3) Заряди телефон і павербанк\n"
                "4) Збережи важливі контакти офлайн\n"
                "5) Підготуй одне коротке повідомлення\n"
                f"6) Перевір, що бот відкривається\n\n{cta}"
            )
        return (
            "🧭 Prepare in advance (5 steps)\n\n"
            "1) Save this channel and the bot\n"
            "2) Agree on a simple plan with close ones\n"
            "3) Charge your phone and power bank\n"
            "4) Save important contacts offline\n"
            "5) Prepare one short message\n"
            f"6) Make sure the bot opens\n\n{cta}"
        )
    if key == "save_checklist":
        if lang == "uk":
            return (
                "🧭 Коротка інструкція на випадок збою звʼязку\n\n"
                "1) Увімкни режим енергозбереження\n"
                "2) Вимкни непотрібні підключення (Wi-Fi, Bluetooth)\n"
                "3) Підготуй один короткий шаблон повідомлення\n"
                "4) Повідом близьких одним реченням\n"
                "5) Домовся про резервний спосіб звʼязку\n"
                f"6) Збережи контакти офлайн\n\n{cta}"
            )
        return (
            "🧭 Quick checklist for a comms outage\n\n"
            "1) Turn on power saving mode\n"
            "2) Disable unused connections (Wi-Fi, Bluetooth)\n"
            "3) Prepare one short message template\n"
            "4) Send one short message to close ones\n"
            "5) Agree on a backup way to connect\n"
            f"6) Save contacts offline\n\n{cta}"
        )
    if key == "value_simple":
        if lang == "uk":
            return (
                "❌ Інтернет\n"
                "❌ Мобільний звʼязок\n"
                "✅ Telegram + інструкція\n\n"
                "1) Збережи цей пост\n"
                "2) Домовся про короткий план\n"
                "3) Заряди телефон і павербанк\n"
                "4) Збережи контакти офлайн\n"
                "5) Підготуй одне коротке повідомлення\n\n"
                f"{cta}"
            )
        return (
            "❌ Internet\n"
            "❌ Mobile network\n"
            "✅ Telegram + instructions\n\n"
            "1) Save this post\n"
            "2) Agree on a short plan\n"
            "3) Charge your phone and power bank\n"
            "4) Save contacts offline\n"
            "5) Prepare one short message\n\n"
            f"{cta}"
        )
    if key == "forward_close":
        if lang == "uk":
            return (
                "Перешли близьким\n\n"
                "1) Поясни, що тут короткі інструкції\n"
                "2) Попроси зберегти цей пост\n"
                "3) Перевір разом заряд телефону\n"
                "4) Домовся про один спосіб звʼязку\n"
                "5) Збережи контакти офлайн\n"
                "6) Підготуй одне коротке повідомлення\n"
                f"7) Узгодь час для короткої перевірки\n\n{cta}"
            )
        return (
            "Forward to close ones\n\n"
            "1) Explain this is a short-instruction channel\n"
            "2) Ask them to save this post\n"
            "3) Check their phone battery together\n"
            "4) Agree on one way to connect\n"
            "5) Save contacts offline\n"
            "6) Prepare one short message\n"
            f"7) Set a time for a short check-in\n\n{cta}"
        )
    if key == "quiet":
        if lang == "uk":
            return (
                "Нічого робити не потрібно. Просто збережи.\n\n"
                "1) Перевір заряд\n"
                "2) Увімкни енергозбереження\n"
                "3) Вимкни зайві підключення\n"
                "4) Збережи контакти офлайн\n"
                "5) Підготуй коротке повідомлення\n"
                "6) Домовся про один спосіб звʼязку\n"
                "7) Тримай цей пост під рукою"
            )
        return (
            "No action is needed. Just save this.\n\n"
            "1) Check your battery\n"
            "2) Enable power saving\n"
            "3) Disable extra connections\n"
            "4) Save contacts offline\n"
            "5) Prepare one short message\n"
            "6) Agree on one way to connect\n"
            "7) Keep this post handy"
        )
    if key == "not_news":
        if lang == "uk":
            return (
                "Це не новини\n\n"
                "1) Це короткі інструкції, не новини\n"
                "2) Збережи цей пост\n"
                "3) Домовся про короткий план\n"
                "4) Підготуй одне коротке повідомлення\n"
                "5) Перевір заряд телефону\n"
                "6) Збережи контакти офлайн\n"
                f"7) Використай його, коли потрібно діяти\n\n{cta}"
            )
        return (
            "This is not news\n\n"
            "1) Only short instructions here\n"
            "2) Save this post\n"
            "3) Agree on a short plan\n"
            "4) Prepare one short message\n"
            "5) Check your phone battery\n"
            "6) Save contacts offline\n"
            f"7) Use it when action is needed\n\n{cta}"
        )
    if key == "pinned":
        bot_line = _channel_bot_line(lang)
        if lang == "uk":
            return (
                "📌 Вхідна точка\n\n"
                "1) Збережи канал як інструкцію\n"
                "2) Використовуй для коротких дій\n"
                "3) Дій, якщо немає інтернету\n"
                "4) Дій, якщо немає мобільного звʼязку\n"
                "5) Подай запит у боті\n"
                "6) Дочекайся підтвердження\n\n"
                f"{bot_line}"
            )
        return (
            "📌 Entry point\n\n"
            "1) Save the channel as your instruction\n"
            "2) Use it for short actions\n"
            "3) Use it if there is no internet\n"
            "4) Use it if there is no mobile network\n"
            "5) Submit a request in the bot\n"
            "6) Wait for approval\n\n"
            f"{bot_line}"
        )
    theme = topic.strip() or ("Коротка інструкція" if lang == "uk" else "Short checklist")
    mode = _channel_auto_mode(theme)
    if mode == "forward":
        if lang == "uk":
            lines = [
                "Це коротка інструкція для швидких дій.",
                "Попроси зберегти та перевірити заряд.",
                "Домовся про один спосіб звʼязку.",
                "Узгодь час короткої перевірки.",
                "Підготуй одне коротке повідомлення.",
                "Збережи контакти офлайн.",
            ]
        else:
            lines = [
                "This is a short guide for quick action.",
                "Ask them to save it and check battery.",
                "Agree on one way to connect.",
                "Set a time for a short check-in.",
                "Prepare one short message.",
                "Save contacts offline.",
            ]
        return _channel_titled_post(theme, lines, cta)
    if mode == "quiet":
        return _channel_post_fallback("quiet", lang)
    if mode == "explain":
        if lang == "uk":
            lines = [
                "Пояснюємо просто і коротко.",
                "Збережи цей пост як інструкцію.",
                "Домовся з близькими про короткий план.",
                "Підготуй одне коротке повідомлення.",
                "Перевір заряд телефону.",
                "Збережи контакти офлайн.",
            ]
        else:
            lines = [
                "Explained simply and briefly.",
                "Save this post as your instruction.",
                "Agree on a short plan with close ones.",
                "Prepare one short message.",
                "Check your phone battery.",
                "Save contacts offline.",
            ]
        return _channel_titled_post(theme, lines, cta)
    if mode == "scenario":
        if lang == "uk":
            steps = [
                "Увімкни енергозбереження",
                "Вимкни зайві підключення",
                "Надішли одне коротке повідомлення",
                "Домовся про час/місце звʼязку",
                "Збережи контакти офлайн",
            ]
        else:
            steps = [
                "Enable power saving",
                "Disable extra connections",
                "Send one short message",
                "Agree on time/place to connect",
                "Save contacts offline",
            ]
        arrow = " → ".join(steps)
        return _channel_titled_post(f"🧭 {theme}", [arrow], cta)
    actions = _channel_fallback_actions(theme, lang)
    if actions and _channel_validate_actions(actions, theme, lang):
        return _channel_format_actions_post(theme, actions, lang)
    return _channel_post_fallback("save_checklist", lang).replace(
        "Коротка інструкція на випадок збою звʼязку", theme
    ).replace(
        "Quick checklist for a comms outage", theme
    )

async def _ai_channel_post(prompt: str, lang: str) -> str:
    if _ai_client is None:
        return ""
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                _ai_client.responses.create,
                model=AI_MODEL,
                instructions=_channel_ai_instructions(lang),
                input=prompt,
                timeout=AI_TIMEOUT_SEC,
            ),
            timeout=AI_TIMEOUT_SEC,
        )
        out = (getattr(resp, "output_text", "") or "").strip()
        return out
    except asyncio.TimeoutError:
        _ai_register_timeout("channel_post")
        logger.warning("Channel post AI timed out")
        return ""
    except Exception as exc:
        if _ai_should_backoff(exc):
            _ai_disable_temporarily("rate limit or quota")
        logger.exception("Channel post AI failed")
        return ""

async def _build_channel_post(topic: str, lang: str) -> str:
    prompt = _channel_topic_prompt(topic, lang)
    text = ""
    if ai_enabled():
        text = await _ai_channel_post(prompt, lang)
    else:
        logger.info("Channel post AI unavailable: %s", _ai_status_reason())
    if text:
        text = text.strip()
        actions = _channel_fill_actions(_extract_action_lines(text), topic, lang)
        if _channel_validate_actions(actions, topic, lang):
            return _channel_format_actions_post(topic, actions, lang)
        logger.info("Channel post AI rejected; fallback used: topic=%s", topic)
    fallback = _channel_post_fallback(topic, lang)
    actions = _channel_fill_actions(_extract_action_lines(fallback), topic, lang)
    if _channel_validate_actions(actions, topic, lang):
        return _channel_format_actions_post(topic, actions, lang)
    return fallback

async def _send_channel_post(context: ContextTypes.DEFAULT_TYPE, topic: str, text: str, lang: str) -> bool:
    image_bytes = await _generate_channel_post_image(topic, text)
    if image_bytes:
        caption = _caption_with_footer(clip(text, 3000), NEWS_CHANNEL_ID, max_len=1024)
        await context.bot.send_photo(
            chat_id=NEWS_CHANNEL_ID,
            photo=InputFile(io.BytesIO(image_bytes), filename="channel_post.png"),
            caption=caption,
        )
        logger.info("Channel post delivered with image: topic=%s lang=%s", topic, lang)
        actions = [_normalize_action_line(a) for a in _extract_action_lines(text) if a]
        _remember_channel_actions(actions)
        _remember_channel_topic(topic)
        return True
    await context.bot.send_message(
        chat_id=NEWS_CHANNEL_ID,
        text=_append_footer(clip(text, 3800), NEWS_CHANNEL_ID),
        disable_web_page_preview=True,
    )
    logger.info("Channel post delivered: topic=%s lang=%s", topic, lang)
    actions = [_normalize_action_line(a) for a in _extract_action_lines(text) if a]
    _remember_channel_actions(actions)
    _remember_channel_topic(topic)
    return False

async def channel_posts_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_POSTS_ENABLED:
        return
    if not NEWS_CHANNEL_ID:
        logger.info("Channel posts skipped: NEWS_CHANNEL_ID empty")
        return
    lang = _channel_post_lang()
    topic = _next_channel_topic()
    text = await _build_channel_post(topic, lang)
    if not text:
        return
    await _send_channel_post(context, topic, text, lang)

async def meme_job(context: ContextTypes.DEFAULT_TYPE):
    if not MEME_POSTS_ENABLED:
        return
    if not NEWS_CHANNEL_ID:
        logger.info("Meme posts skipped: NEWS_CHANNEL_ID empty")
        return
    if not RSS_FEEDS:
        logger.info("Meme posts skipped: RSS_FEEDS empty")
        return
    if not ai_enabled():
        logger.info("Meme posts skipped: %s", _ai_status_reason())
        return
    items = await _collect_summary_items()
    if not items:
        logger.info("Meme posts skipped: no recent items")
        return
    image_bytes = await _generate_meme_image(items)
    if not image_bytes:
        return
    try:
        await context.bot.send_photo(
            chat_id=NEWS_CHANNEL_ID,
            photo=InputFile(io.BytesIO(image_bytes), filename="meme.png"),
        )
        logger.info("Meme post delivered")
    except Exception:
        logger.exception("Meme post failed")
        raise

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
        f"NEWS_IMAGE={'on' if NEWS_IMAGE_ENABLED else 'off'}\n"
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
        await context.bot.send_message(
            chat_id=NEWS_CHANNEL_ID,
            text=_append_footer("✅ TEST: бот може писати в канал.", NEWS_CHANNEL_ID),
        )
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

async def news_image_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
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
    mode = (context.args[0] if context.args else "news").strip().lower()
    if mode not in ("news", "summary"):
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Використання: /news_image_test [news|summary]", "Usage: /news_image_test [news|summary]"),
            reply_markup=menu_only_kb(uid),
        )
        return
    reason = _news_image_skip_reason()
    if reason:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, f"⚠️ Картинки вимкнені: {reason}.", f"⚠️ Images are disabled: {reason}."),
            reply_markup=menu_only_kb(uid),
        )
        return
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        t(uid, "⏳ Генерую тестову картинку...", "⏳ Generating test image..."),
    )
    try:
        if mode == "summary":
            items = [
                {"title": "Тестова добірка: ситуація на фронті"},
                {"title": "Тестова добірка: дипломатичні заяви"},
                {"title": "Тестова добірка: інфраструктура та енергетика"},
            ]
            ai_text = "Ключові тези:\n• Тестові пункти зведення\nКоротка аналітика:\nТестова аналітика."
            image_bytes = await _generate_summary_image(items, ai_text)
            caption = "🧪 TEST: Зведення новин України"
            filename = "summary_test.png"
        else:
            title = "Тестова новина: перевірка генерації зображень"
            summary = "Це тест для перевірки, що генерація картинок працює."
            image_bytes = await _generate_news_image(title, summary)
            caption = "🧪 TEST: Новинне зображення"
            filename = "news_test.png"
        if not image_bytes:
            reason = _news_image_skip_reason()
            msg = (
                t(uid, "❌ Картинку не згенеровано.", "❌ Image was not generated.")
                if not reason
                else t(uid, f"⚠️ Картинки вимкнені: {reason}.", f"⚠️ Images are disabled: {reason}.")
            )
            await send_with_cleanup(
                context.bot,
                update.effective_chat.id,
                update.message.reply_text,
                msg,
                reply_markup=menu_only_kb(uid),
            )
            return
        await context.bot.send_photo(
            chat_id=NEWS_CHANNEL_ID,
            photo=InputFile(io.BytesIO(image_bytes), filename=filename),
            caption=caption,
        )
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "✅ Тестову картинку відправлено в канал.", "✅ Test image sent to channel."),
            reply_markup=menu_only_kb(uid),
        )
    except Exception:
        logger.exception("News image test failed")
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "❌ Помилка під час генерації.", "❌ Failed to generate image."),
            reply_markup=menu_only_kb(uid),
        )

def _broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if context.args:
        return " ".join(context.args).strip()
    msg = getattr(update, "message", None)
    if msg and msg.reply_to_message:
        reply = msg.reply_to_message
        return (reply.text or reply.caption or "").strip()
    return ""

async def news_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not news_config_ok():
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⚠️ Новини не налаштовано.", "⚠️ News not configured."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if _news_job_lock.locked():
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⏳ Новини вже оновлюються.", "⏳ News already running."),
            reply_markup=menu_only_kb(uid),
        )
        return
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        t(uid, "⏳ Запускаю оновлення новин...", "⏳ Running news update..."),
    )
    try:
        async with _news_job_lock:
            await _news_job_inner(context)
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "✅ Готово.", "✅ Done."),
            reply_markup=menu_only_kb(uid),
        )
    except Exception:
        logger.exception("Manual news run failed")
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "❌ Помилка під час запуску.", "❌ Failed to run news job."),
            reply_markup=menu_only_kb(uid),
        )

async def summary_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not NEWS_SUMMARY_ENABLED:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⚠️ NEWS_SUMMARY_ENABLED вимкнено.", "⚠️ NEWS_SUMMARY_ENABLED is off."),
            reply_markup=menu_only_kb(uid),
        )
        return
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        t(uid, "⏳ Запускаю зведення...", "⏳ Running summary..."),
    )
    try:
        delivered = await _post_news_summary(context)
        msg = (
            t(uid, "✅ Зведення відправлено.", "✅ Summary sent.")
            if delivered
            else t(uid, "ℹ️ Немає новин для зведення.", "ℹ️ No news for summary.")
        )
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            msg,
            reply_markup=menu_only_kb(uid),
        )
    except Exception:
        logger.exception("Manual summary run failed")
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "❌ Помилка під час запуску.", "❌ Failed to run summary."),
            reply_markup=menu_only_kb(uid),
        )

async def channel_post_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
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
    topic_arg = " ".join(context.args or []).strip()
    topic = topic_arg if topic_arg else _next_channel_topic()
    lang = _channel_post_lang()
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        t(uid, "⏳ Генерую пост...", "⏳ Generating post..."),
    )
    try:
        text = await _build_channel_post(topic, lang)
        if not text:
            await send_with_cleanup(
                context.bot,
                update.effective_chat.id,
                update.message.reply_text,
                t(uid, "❌ Не вдалося згенерувати пост.", "❌ Failed to generate post."),
                reply_markup=menu_only_kb(uid),
            )
            return
        await _send_channel_post(context, topic, text, lang)
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "✅ Пост відправлено в канал.", "✅ Post sent to channel."),
            reply_markup=menu_only_kb(uid),
        )
    except Exception:
        logger.exception("Manual channel post failed")
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "❌ Помилка під час відправки.", "❌ Failed to send post."),
            reply_markup=menu_only_kb(uid),
        )

async def meme_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
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
    if not MEME_POSTS_ENABLED:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⚠️ MEME_POSTS_ENABLED вимкнено.", "⚠️ MEME_POSTS_ENABLED is off."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not RSS_FEEDS:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⚠️ RSS_FEEDS порожній.", "⚠️ RSS_FEEDS is empty."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not ai_enabled():
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⚠️ AI недоступний.", "⚠️ AI is unavailable."),
            reply_markup=menu_only_kb(uid),
        )
        return
    reason = _meme_image_skip_reason()
    if reason:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, f"⚠️ Картинки вимкнені: {reason}.", f"⚠️ Images are disabled: {reason}."),
            reply_markup=menu_only_kb(uid),
        )
        return

    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        t(uid, "⏳ Генерую мем...", "⏳ Generating meme..."),
    )
    try:
        items = await _collect_summary_items()
        if not items:
            await send_with_cleanup(
                context.bot,
                update.effective_chat.id,
                update.message.reply_text,
                t(uid, "ℹ️ Немає новин для мема.", "ℹ️ No news for a meme."),
                reply_markup=menu_only_kb(uid),
            )
            return
        image_bytes = await _generate_meme_image(items)
        if not image_bytes:
            reason = _meme_image_skip_reason()
            msg = t(uid, "❌ Мем не згенеровано.", "❌ Meme was not generated.")
            if reason:
                msg = t(uid, f"⚠️ Картинки вимкнені: {reason}.", f"⚠️ Images are disabled: {reason}.")
            await send_with_cleanup(
                context.bot,
                update.effective_chat.id,
                update.message.reply_text,
                msg,
                reply_markup=menu_only_kb(uid),
            )
            return
        await context.bot.send_photo(
            chat_id=NEWS_CHANNEL_ID,
            photo=InputFile(io.BytesIO(image_bytes), filename="meme.png"),
        )
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "✅ Мем відправлено в канал.", "✅ Meme sent to channel."),
            reply_markup=menu_only_kb(uid),
        )
    except Exception:
        logger.exception("Manual meme run failed")
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "❌ Помилка під час відправки.", "❌ Failed to send meme."),
            reply_markup=menu_only_kb(uid),
        )

async def news_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not NEWS_STATS_ENABLED:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "ℹ️ Статистика вимкнена.", "ℹ️ Stats are disabled."),
            reply_markup=menu_only_kb(uid),
        )
        return
    raw = (context.args[0] if context.args else "24").strip().lower()
    hours = 24
    if raw.endswith("d"):
        try:
            hours = max(1, int(raw[:-1])) * 24
        except Exception:
            hours = 24
    else:
        try:
            hours = max(1, int(raw))
        except Exception:
            hours = 24
    items = _stats_window(hours)
    if not items:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "ℹ️ Немає даних за цей період.", "ℹ️ No data for this period."),
            reply_markup=menu_only_kb(uid),
        )
        return
    feeds = _top_counts(items, "feed", 5)
    topics = _top_counts(items, "tokens", 8)
    urgent = sum(1 for it in items if it.get("urgent"))
    total = len(items)
    header = f"📊 News stats ({hours}h)\nTotal: {total} | Urgent: {urgent}\n"
    feed_lines = "\n".join([f"• {cnt} — {feed}" for feed, cnt in feeds]) or "• —"
    topic_lines = "\n".join([f"• {cnt} — {tok}" for tok, cnt in topics]) or "• —"
    text = header + "\nSources:\n" + feed_lines + "\n\nTopics:\n" + topic_lines
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        clip(text, 3800),
        reply_markup=menu_only_kb(uid),
    )

async def news_keywords_suggest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
            reply_markup=menu_only_kb(uid),
        )
        return
    if not NEWS_SKIP_STATS_ENABLED:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "ℹ️ Лог пропусків вимкнено.", "ℹ️ Skip stats are disabled."),
            reply_markup=menu_only_kb(uid),
        )
        return
    raw_hours = (context.args[0] if context.args else "24").strip().lower()
    reason_arg = (context.args[1] if len(context.args) > 1 else "").strip().lower()
    hours = 24
    if raw_hours.endswith("d"):
        try:
            hours = max(1, int(raw_hours[:-1])) * 24
        except Exception:
            hours = 24
    else:
        try:
            hours = max(1, int(raw_hours))
        except Exception:
            hours = 24

    reason_map = {
        "ai": {"ai_below_threshold", "ai_no_score"},
        "rate": {"rate_limited", "max_per_run", "urgent_rate_limited"},
        "keyword": {"keyword_no_match"},
        "all": set(),
    }
    default_reasons = {"ai_below_threshold", "ai_no_score", "keyword_no_match", "rate_limited", "max_per_run", "urgent_rate_limited"}
    if reason_arg:
        reasons = reason_map.get(reason_arg, {reason_arg})
        if reason_arg == "all":
            reasons = set()
    else:
        reasons = default_reasons

    items = _skip_window(hours)
    if not items:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "ℹ️ Немає даних за цей період.", "ℹ️ No data for this period."),
            reply_markup=menu_only_kb(uid),
        )
        return
    reason_counts = _top_counts(items, "reason", 8)
    suggestions = _suggest_keywords(hours, reasons if reasons else None)
    reasons_line = ", ".join(sorted(reasons)) if reasons else "all"
    lines = [f"🧪 Keyword suggestions ({hours}h, reasons: {reasons_line})"]
    if reason_counts:
        lines.append("Reasons:")
        lines.extend([f"• {cnt} — {reason}" for reason, cnt in reason_counts])
    if suggestions:
        lines.append("")
        lines.append("Suggestions:")
        lines.extend([f"• {cnt} — {tok}" for tok, cnt in suggestions])
    else:
        lines.append("")
        lines.append("Suggestions: —")
    text = "\n".join(lines)
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        clip(text, 3800),
        reply_markup=menu_only_kb(uid),
    )

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "⛔ Тільки адмін.", "⛔ Admin only."),
            reply_markup=menu_only_kb(uid),
        )
        return
    text = _broadcast_text(update, context)
    if not text:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "Використання: /broadcast <текст> або відповідь на повідомлення.",
              "Usage: /broadcast <text> or reply to a message."),
            reply_markup=menu_only_kb(uid),
        )
        return
    text = clip(text, 4000)
    if not KNOWN_USERS:
        await send_with_cleanup(
            context.bot,
            update.effective_chat.id,
            update.message.reply_text,
            t(uid, "ℹ️ Немає користувачів для розсилки.", "ℹ️ No users to broadcast."),
            reply_markup=menu_only_kb(uid),
        )
        return
    total = len(KNOWN_USERS)
    sent = 0
    removed = 0
    failed = 0
    for target_id in list(KNOWN_USERS):
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=_append_footer(text, target_id),
                disable_web_page_preview=True,
            )
            sent += 1
        except Forbidden:
            KNOWN_USERS.discard(target_id)
            _save_known_users()
            removed += 1
        except Exception:
            failed += 1
    result = (
        f"✅ Sent: {sent}/{total} | Removed: {removed} | Failed: {failed}"
    )
    await send_with_cleanup(
        context.bot,
        update.effective_chat.id,
        update.message.reply_text,
        result,
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
    await _init_bot_public_link(application)
    if news_config_ok():
        application.job_queue.run_repeating(
            news_job,
            interval=NEWS_POLL_SEC,
            first=15,
            job_kwargs={"max_instances": 2, "coalesce": True},
        )
    if NEWS_SUMMARY_ENABLED:
        for t in _parse_summary_times(NEWS_SUMMARY_TIMES):
            application.job_queue.run_daily(news_summary_job, time=t)
    if ua_alarm_enabled():
        application.job_queue.run_repeating(alerts_job, interval=UA_ALARM_POLL_SEC, first=5)
    if CHANNEL_POSTS_ENABLED:
        channel_times = _parse_channel_times(CHANNEL_POSTS_TIMES)
        if channel_times:
            for t in channel_times:
                application.job_queue.run_daily(channel_posts_job, time=t)
        elif CHANNEL_POSTS_INTERVAL_SEC > 0:
            application.job_queue.run_repeating(channel_posts_job, interval=CHANNEL_POSTS_INTERVAL_SEC, first=30)
    if MEME_POSTS_ENABLED and MEME_POSTS_INTERVAL_SEC > 0:
        application.job_queue.run_repeating(meme_job, interval=MEME_POSTS_INTERVAL_SEC, first=60)

# =========================
# main
# =========================
def main():
    _acquire_instance_lock()
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
    app.add_handler(CommandHandler("news_image_test", news_image_test_cmd))
    app.add_handler(CommandHandler("news_now", news_now_cmd))
    app.add_handler(CommandHandler("summary_now", summary_now_cmd))
    app.add_handler(CommandHandler("channel_post_now", channel_post_now_cmd))
    app.add_handler(CommandHandler("meme_now", meme_now_cmd))
    app.add_handler(CommandHandler("news_stats", news_stats_cmd))
    app.add_handler(CommandHandler("news_keywords_suggest", news_keywords_suggest_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
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
