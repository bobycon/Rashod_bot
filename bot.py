import os
from datetime import datetime, date
from urllib.parse import urlparse

import pg8000.dbapi

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "Переменная окружения DATABASE_URL не найдена. "
        "Подключи PostgreSQL в Railway и добавь ссылку на DATABASE_URL в Variables бота."
    )

_parsed = urlparse(DATABASE_URL)


def get_conn():
    return pg8000.dbapi.connect(
        user=_parsed.username,
        password=_parsed.password,
        host=_parsed.hostname,
        port=_parsed.port or 5432,
        database=_parsed.path.lstrip("/"),
    )


# ---------- База данных ----------

def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                category TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def add_expense(user_id: int, amount: float, category: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO expenses (user_id, amount, category, created_at) VALUES (%s, %s, %s, %s)",
            (user_id, amount, category, datetime.now()),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def delete_last_expense(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM expenses WHERE user_id = %s ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM expenses WHERE id = %s", (row[0],))
        conn.commit()
        cur.close()
        return row is not None
    finally:
        conn.close()


def get_expenses(user_id: int, since):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT amount, category, created_at FROM expenses "
            "WHERE user_id = %s AND created_at >= %s ORDER BY created_at DESC",
            (user_id, since),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


# ---------- Категории ----------

CATEGORIES = [
    "🍔 Еда",
    "🚌 Транспорт",
    "🏠 ЖКХ",
    "🎉 Развлечения",
    "👕 Одежда",
    "💊 Здоровье",
    "📱 Связь/интернет",
    "🛒 Прочее",
]

WAITING_AMOUNT, WAITING_CATEGORY, WAITING_CUSTOM_CATEGORY = range(3)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Добавить расход"],
        ["📊 За сегодня", "📅 За месяц"],
        ["❌ Удалить последнюю", "ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)


def categories_inline_keyboard():
    buttons = []
    row = []
    for i, cat in enumerate(CATEGORIES, 1):
        row.append(InlineKeyboardButton(cat, callback_data=f"cat:{cat}"))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ Своя категория", callback_data="cat:custom")])
    buttons.append([InlineKeyboardButton("🚫 Отмена", callback_data="cat:cancel")])
    return InlineKeyboardMarkup(buttons)


# ---------- Базовые команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу вести учёт расходов.\n\n"
        "Нажми «➕ Добавить расход», чтобы быстро записать трату, "
        "или используй команды:\n"
        "/add 500 еда — быстрое добавление одной строкой\n"
        "/today — расходы за сегодня\n"
        "/month — расходы за месяц\n"
        "/undo — удалить последнюю запись\n",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ---------- Пошаговое добавление расхода ----------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи сумму расхода (например: 350 или 199.50):"
    )
    return WAITING_AMOUNT


async def add_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        return await add_start(update, context)

    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /add <сумма> <категория>, например /add 350 кафе\n"
            "Либо просто отправь /add без аргументов для пошагового ввода."
        )
        return ConversationHandler.END

    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом. Например: /add 350 кафе")
        return ConversationHandler.END

    category = " ".join(args[1:])
    add_expense(update.effective_user.id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")
    return ConversationHandler.END


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Не похоже на сумму. Введи число, например: 350"
        )
        return WAITING_AMOUNT

    context.user_data["pending_amount"] = amount
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\nТеперь выбери категорию:",
        reply_markup=categories_inline_keyboard(),
    )
    return WAITING_CATEGORY


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    choice = data.split(":", 1)[1]

    if choice == "cancel":
        context.user_data.pop("pending_amount", None)
        await query.edit_message_text("Добавление отменено.")
        return ConversationHandler.END

    if choice == "custom":
        await query.edit_message_text("Напиши название своей категории текстом:")
        return WAITING_CUSTOM_CATEGORY

    amount = context.user_data.pop("pending_amount", None)
    if amount is None:
        await query.edit_message_text("Что-то пошло не так, начни заново через ➕ Добавить расход.")
        return ConversationHandler.END

    add_expense(query.from_user.id, amount, choice)
    await query.edit_message_text(f"Добавлено: {amount:.2f} — {choice}")
    return ConversationHandler.END


async def custom_category_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    amount = context.user_data.pop("pending_amount", None)
    if amount is None or not category:
        await update.message.reply_text("Что-то пошло не так, начни заново через ➕ Добавить расход.")
        return ConversationHandler.END

    add_expense(update.effective_user.id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_amount", None)
    await update.message.reply_text("Добавление отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------- Прочие команды ----------

async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ok = delete_last_expense(user_id)
    if ok:
        await update.message.reply_text("Последняя запись удалена.")
    else:
        await update.message.reply_text("У тебя ещё нет записей.")


def format_summary(rows, title):
    if not rows:
        return f"{title}: записей пока нет."

    total = sum(r[0] for r in rows)
    by_category = {}
    for amount, category, _ in rows:
        by_category[category] = by_category.get(category, 0) + amount

    lines = [f"{title}", f"Всего: {total:.2f}", ""]
    for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
        lines.append(f"• {cat}: {amt:.2f}")
    return "\n".join(lines)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = datetime.combine(date.today(), datetime.min.time())
    rows = get_expenses(user_id, since)
    await update.message.reply_text(format_summary(rows, "Расходы за сегодня"))


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = datetime.combine(date.today().replace(day=1), datetime.min.time())
    rows = get_expenses(user_id, since)
    await update.message.reply_text(format_summary(rows, "Расходы за месяц"))


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 За сегодня":
        await today(update, context)
    elif text == "📅 За месяц":
        await month(update, context)
    elif text == "❌ Удалить последнюю":
        await undo(update, context)
    elif text == "ℹ️ Помощь":
        await help_cmd(update, context)


def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    add_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_quick),
            MessageHandler(filters.Regex("^➕ Добавить расход$"), add_start),
        ],
        states={
            WAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)
            ],
            WAITING_CATEGORY: [
                CallbackQueryHandler(category_chosen, pattern=r"^cat:")
            ],
            WAITING_CUSTOM_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_category_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(add_conversation)
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
