import os
import time
import secrets
from dataclasses import dataclass
from typing import Dict

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

# ====== ENV ======
def need_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Відсутня змінна середовища: {name}")
    return v

BOT_TOKEN = need_env("BOT_TOKEN")
ADMIN_ID = int(need_env("ADMIN_ID"))

# ====== Anti-spam ======
COOLDOWN_SEC = 60
_last_apply: Dict[int, float] = {}

# ====== Storage (in-memory) ======
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

# ====== Conversation states ======
ASK_PURPOSE, ASK_DEVICE, ASK_CONFIRM = range(3)

# ====== UI ======
def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Подати запит на доступ", callback_data="apply:start")],
        [InlineKeyboardButton("📦 Обладнання", callback_data="info:gear")],
        [InlineKeyboardButton("📜 Правила", callback_data="info:rules")],
        [InlineKeyboardButton("ℹ️ Допомога", callback_data="info:help")],
    ])

def admin_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Схвалити", callback_data=f"admin:approve:{key}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"admin:deny:{key}"),
    ]])

def _who(u) -> str:
    if u.username:
        return f"@{u.username}"
    return f"id:{u.id}"

# ====== Commands ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю!\n\n"
        "Це офіційний бот доступу до мережі екстреного звʼязку УКРАВІАКОСТЕХ.\n"
        "Доступ надається **лише за запитом**.\n\n"
        "Оберіть дію нижче:",
        reply_markup=menu_kb(),
        parse_mode="Markdown",
    )

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 **ПРАВИЛА**\n\n"
        "• Мережа призначена для екстрених ситуацій\n"
        "• Спам/флуд заборонено\n"
        "• Заборонено передавати доступ іншим\n"
        "• Використання лише за призначенням\n\n"
        "Порушення → відключення.",
        parse_mode="Markdown",
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ **ДОПОМОГА**\n\n"
        "1) Натисніть «Подати запит на доступ»\n"
        "2) Дайте відповіді на 2 питання\n"
        "3) Підтвердіть правила\n\n"
        "Після цього адміністратор отримає заявку.",
        parse_mode="Markdown",
    )

# ====== Menu callbacks ======
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "info:gear":
        await q.message.reply_text(
            "📦 **Обладнання**\n\n"
            "Потрібен окремий автономний портативний пристрій із вбудованою батареєю.\n"
            "Телефон використовується лише для налаштування.\n\n"
            "Поширені варіанти:\n"
            "• ThinkNode M2\n"
            "• LILYGO T-Echo\n"
            "• Heltec Mesh Node (готовий)\n",
            parse_mode="Markdown",
        )
        return

    if q.data == "info:rules":
        await rules_cmd(update, context)
        return

    if q.data == "info:help":
        await help_cmd(update, context)
        return

    if q.data == "apply:start":
        # anti-spam cooldown
        uid = q.from_user.id
        now = time.time()
        last = _last_apply.get(uid, 0)
        if now - last < COOLDOWN_SEC:
            await q.message.reply_text(
                f"⏳ Зачекайте {int(COOLDOWN_SEC - (now - last))} сек і спробуйте ще раз."
            )
            return

        _last_apply[uid] = now
        await q.message.reply_text(
            "🟢 **ЗАПИТ НА ДОСТУП**\n\n"
            "Для чого вам доступ до мережі?\n"
            "Напишіть коротко (1 рядок).",
            parse_mode="Markdown",
        )
        return ASK_PURPOSE

# ====== Conversation ======
async def ask_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["purpose"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "📦 Який пристрій ви плануєте використовувати?\n\n"
        "Наприклад: ThinkNode M2 / T-Echo / Heltec",
    )
    return ASK_DEVICE

async def ask_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["device"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "✅ Підтвердіть правила.\n\n"
        "Напишіть: **ПІДТВЕРДЖУЮ**",
        parse_mode="Markdown",
    )
    return ASK_CONFIRM

async def ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip().upper()
    if txt != "ПІДТВЕРДЖУЮ":
        await update.message.reply_text("❌ Запит скасовано.")
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

    admin_text = (
        "🆕 **ЗАЯВКА НА ДОСТУП**\n\n"
        f"👤 {req.who}\n"
        f"🎯 Мета: {req.purpose}\n"
        f"📦 Пристрій: {req.device}\n\n"
        f"ID: `{req.user_id}`"
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=admin_text,
        reply_markup=admin_kb(key),
        parse_mode="Markdown",
    )

    await update.message.reply_text(
        "✅ Дякуємо! Заявку передано адміністратору.\n"
        "Очікуйте відповідь у цьому чаті."
    )
    return ConversationHandler.END

# ====== Admin callbacks ======
async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # тільки адмін
    if q.from_user.id != ADMIN_ID:
        await q.message.reply_text("⛔ Недостатньо прав.")
        return

    parts = q.data.split(":")
    action = parts[1]
    key = parts[2]

    req = PENDING.pop(key, None)
    if not req:
        await q.message.reply_text("⚠️ Ця заявка вже оброблена або не знайдена.")
        return

    if action == "approve":
        await context.bot.send_message(
            chat_id=req.chat_id,
            text="✅ Ваш запит **схвалено**. Інструкції/доступ буде надано окремо адміністратором.",
            parse_mode="Markdown",
        )
        await q.message.reply_text(f"✅ Схвалено для {req.who}")
        return

    if action == "deny":
        await context.bot.send_message(
            chat_id=req.chat_id,
            text="❌ Ваш запит **відхилено**. Ви можете подати запит повторно пізніше.",
            parse_mode="Markdown",
        )
        await q.message.reply_text(f"❌ Відхилено для {req.who}")
        return

# ====== Main ======
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # menu callbacks
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^(apply:start|info:gear|info:rules|info:help)$"))

    # admin callbacks
    app.add_handler(CallbackQueryHandler(admin_handler, pattern=r"^admin:(approve|deny):"))

    # conversation
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler, pattern=r"^apply:start$")],
        states={
            ASK_PURPOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_purpose)],
            ASK_DEVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_device)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_confirm)],
        },
        fallbacks=[],
    )
    app.add_handler(conv)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
