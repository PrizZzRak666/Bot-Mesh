import os
import time
import secrets
import asyncio
import logging
import json
import random
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

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
            "• Телефон (Telegram): 0 755 05 35 18\n"
            "• Графік: пн-пт 9:00-18:00\n"
            "• Сайт: https://www.ukrainianaviation.com\n"
            "• Facebook: https://www.facebook.com/ukr.avia.kos.tech\n"
            "• Instagram: https://www.instagram.com/ukr.avia.kos.tech\n"
            "• TikTok: https://www.tiktok.com/@ukr.avia.kos.tech"
        ),
        "products": (
            "🧩 **Каталог продуктів**\n\n"
            "Оберіть продукт, щоб отримати фото та ключові характеристики.\n"
            "ℹ️ Детальні конфігурації та частотні діапазони — за запитом."
        ),
        "products_not_found": "⚠️ Продукт не знайдено.",
        "system": (
            "📡 **Як працює система (Meshtastic)**\n\n"
            "Це автономна mesh-мережа на портативних вузлах, де кожен вузол може ретранслювати повідомлення далі.\n"
            "Система працює без інтернету й мобільного звʼязку, з низьким енергоспоживанням та короткими повідомленнями.\n\n"
            "Навіщо потрібна:\n"
            "• резервний звʼязок під час відключень і перевантажень мереж\n"
            "• координація в польових/кризових умовах\n"
            "• стійкість до втрати окремих вузлів (мережа самовідновлюється)\n\n"
            "Безпека:\n"
            "• повідомлення захищені шифруванням\n"
            "• обмін без публікації технічних ключів чи налаштувань\n\n"
            "💙 Проєкт безкоштовний для користувачів і фінансується як волонтерська ініціатива.\n\n"
            "Публічно не розкриваємо технічні параметри й інструкції підключення.\n"
            "Підключення — лише після підтвердження."
        ),
        "gear": (
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
            "Офіційний список: https://meshtastic.org/docs/hardware/devices/\n"
            "Технічні параметри та інструкції підключення публічно не розкриваємо."
        ),
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
            "• Phone (Telegram): 0 755 05 35 18\n"
            "• Hours: Mon-Fri 9:00-18:00\n"
            "• Website: https://www.ukrainianaviation.com\n"
            "• Facebook: https://www.facebook.com/ukr.avia.kos.tech\n"
            "• Instagram: https://www.instagram.com/ukr.avia.kos.tech\n"
            "• TikTok: https://www.tiktok.com/@ukr.avia.kos.tech"
        ),
        "products": (
            "🧩 **Product catalog**\n\n"
            "Choose a product to see photos and key characteristics.\n"
            "ℹ️ Detailed configurations and frequency ranges are available on request."
        ),
        "products_not_found": "⚠️ Product not found.",
        "system": (
            "📡 **How it works (Meshtastic)**\n\n"
            "An autonomous mesh network of portable nodes where each node can relay messages.\n"
            "It works without internet or cellular coverage, optimized for low power and short messages.\n\n"
            "Why it matters:\n"
            "• backup communications during outages and network congestion\n"
            "• coordination in field or crisis conditions\n"
            "• resilient topology that self-heals if some nodes drop\n\n"
            "Security:\n"
            "• messages are protected with encryption\n"
            "• no public disclosure of technical keys or settings\n\n"
            "💙 The project is free for users and funded as a volunteer initiative.\n\n"
            "Technical parameters and onboarding steps are not published.\n"
            "Access is provided after verification."
        ),
        "gear": (
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
            "Official list: https://meshtastic.org/docs/hardware/devices/\n"
            "Technical parameters and onboarding instructions are not published."
        ),
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
            [InlineKeyboardButton("🟢 Запит на доступ", callback_data="apply:start")],
            [InlineKeyboardButton("🏢 Про компанію", callback_data="info:company"),
             InlineKeyboardButton("🧩 Продукти", callback_data="info:products")],
            [InlineKeyboardButton("📡 Як працює", callback_data="info:system")],
            [InlineKeyboardButton("📦 Обладнання", callback_data="info:gear"),
             InlineKeyboardButton("📜 Правила", callback_data="info:rules")],
            [InlineKeyboardButton("💬 Питання та відповіді", callback_data="faq:start")],
            [InlineKeyboardButton("🚨 Тривоги у регіоні On/Off", callback_data="alerts:toggle")],
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
        [InlineKeyboardButton("💬 Questions & Answers", callback_data="faq:start")],
        [InlineKeyboardButton("🚨 Regional Air Alerts On/Off", callback_data="alerts:toggle")],
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
ALERT_REGION: Dict[int, List[str]] = {}
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
    items = data if isinstance(data, list) else data.get("regions") or data.get("states") or data.get("data") or []

    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        for token in ("область", "обл.", "обл"):
            s = s.replace(token, "")
        return " ".join(s.split())

    target = norm(UA_ALARM_OBLAST_NAME)
    fallback_rid = ""
    fallback_name = ""
    for it in items:
        name = it.get("name") or it.get("title") or it.get("regionName") or it.get("regionEngName") or ""
        rid = it.get("regionId") or it.get("id") or it.get("region_id") or ""
        if not name or not rid:
            continue
        name_norm = norm(name)
        if name_norm == target:
            REGION_CACHE["oblast"] = str(rid)
            return
        if target and (target in name_norm or name_norm in target):
            fallback_rid = str(rid)
            fallback_name = name

    if fallback_rid:
        REGION_CACHE["oblast"] = fallback_rid
        logger.warning("UA alarm region matched by partial name: %s", fallback_name)
        return
    if items:
        sample = []
        for it in items[:10]:
            sample.append(it.get("name") or it.get("title") or "")
        logger.error("UA alarm region not found. Sample regions: %s", sample)

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

async def alerts_job(context: ContextTypes.DEFAULT_TYPE):
    if not ua_alarm_enabled():
        return

    subs = [uid for uid, on in ALERTS_ENABLED.items() if on and ALERT_REGION.get(uid)]
    if not subs:
        return

    region_ids = sorted({rid for uid in subs for rid in ALERT_REGION.get(uid, [])})

    async with httpx.AsyncClient(timeout=20) as client:
        active_ids = await fetch_active_region_ids(client)
        for rid in region_ids:
            try:
                is_alert = rid in active_ids if active_ids is not None else await fetch_alert_state(client, rid)
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
                        if rid in ALERT_REGION.get(uid, []):
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

                    remember_link(link)
                    remember_title(title_norm)

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

                    await post_to_channel(context, post)
                    mark_news_sent()
                    posted += 1

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
    name = display_name(update.effective_user)
    greet = greeting_text(uid, name)
    await update.message.reply_text(
        f"{greet}\n\n{C(uid,'menu')}",
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

async def regions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text(t(uid, "⛔ Тільки адмін.", "⛔ Admin only."))
        return
    query = " ".join(context.args or []).strip().lower()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            data = await ua_get_json(UA_ALARM_REGIONS_PATH, client=client)
    except Exception:
        await update.message.reply_text(
            t(uid, "❌ Не вдалося отримати список регіонів.", "❌ Failed to fetch regions list.")
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
        await update.message.reply_text(
            t(uid, "ℹ️ Регіони не знайдені за запитом.", "ℹ️ No regions found for query.")
        )
        return

    prefix = "Регіони:\n" if get_lang(uid) == "uk" else "Regions:\n"
    max_len = 3500
    chunk = prefix
    for line in lines:
        if len(chunk) + len(line) + 1 > max_len:
            await update.message.reply_text(chunk)
            chunk = prefix + line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk)

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
    if data in ("info:products", "products:menu"):
        await q.message.reply_text(
            C(uid, "products"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=products_kb(uid),
        )
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
                rids = await ua_region_ids()
            except Exception:
                await q.message.reply_text(
                    t(uid, "⚠️ Не вдалося увімкнути тривоги (помилка конфігурації/API).",
                       "⚠️ Could not enable alerts (config/API error).")
                )
                return
            ALERT_REGION[uid] = rids
            ALERTS_ENABLED[uid] = True
            label = region_label_for_message(rids)
            if label:
                await q.message.reply_text(
                    t(uid, f"✅ Тривоги увімкнено ({label}).", f"✅ Alerts enabled ({label}).")
                )
            else:
                await q.message.reply_text(t(uid, "✅ Тривоги увімкнено.", "✅ Alerts enabled."))
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    active_ids = await fetch_active_region_ids(client)
                if active_ids is None:
                    active_ids = set()
                any_alert = any(rid in active_ids for rid in rids)
                for rid in rids:
                    ALERT_LAST_STATE[rid] = rid in active_ids
                msg_uk = "🔴 ТРИВОГА" if any_alert else "🟢 ВІДБІЙ"
                msg_en = "🔴 ALERT" if any_alert else "🟢 ALL CLEAR"
                await q.message.reply_text(t(uid, msg_uk, msg_en))
            except Exception:
                logger.exception("alerts status fetch failed for regions %s", rids)
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
        await q.message.reply_text(C(uid, "products_not_found"))
        return

    caption = product_caption(uid, prod)
    image_path = PRODUCT_IMAGES_DIR / prod.image_name
    if image_path.is_file():
        with open(image_path, "rb") as photo:
            await q.message.reply_photo(
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=product_detail_kb(uid),
            )
        return

    await q.message.reply_text(
        caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=product_detail_kb(uid),
    )

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
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("Failed to delete webhook")
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
    app.add_handler(CommandHandler("regions", regions_cmd))

    # menu/info callbacks only
    app.add_handler(CallbackQueryHandler(
        menu_cb,
        pattern=r"^(lang:menu|lang:set:(uk|en)|menu:back|info:company|info:products|products:menu|info:system|info:gear|info:rules|alerts:toggle|news:test)$"
    ))

    app.add_handler(CallbackQueryHandler(product_cb, pattern=r"^prod:"))

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
