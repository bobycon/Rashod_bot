import os
import sqlite3
from datetime import datetime, date

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")


# ---------- База данных ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def add_expense(user_id: int, amount: float, category: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO expenses (user_id, amount, category, created_at) VALUES (?, ?, ?, ?)",
        (user_id, amount, category, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def delete_last_expense(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT id FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    if row:
        conn.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
        conn.commit()
    conn.close()
    return row is not None


def get_expenses(user_id: int, since: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT amount, category, created_at FROM expenses "
        "WHERE user_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (user_id, since),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------- Команды бота ----------

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["📊 За сегодня", "📅 За месяц"], ["❌ Удалить последнюю", "ℹ️ Помощь"]],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу вести учёт расходов.\n\n"
        "Добавить расход: /add 500 еда\n"
        "Статистика за сегодня: /today\n"
        "Статистика за месяц: /month\n"
        "Удалить последнюю запись: /undo\n",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Используй формат: /add <сумма> <категория>\nНапример: /add 350 кафе"
        )
        return
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом. Например: /add 350 кафе")
        return

    category = " ".join(args[1:])
    add_expense(user_id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")


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
    since = date.today().isoformat()
    rows = get_expenses(user_id, since)
    await update.message.reply_text(format_summary(rows, "Расходы за сегодня"))


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = date.today().replace(day=1).isoformat()
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("undo", undo))

    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
