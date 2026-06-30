import io
import os
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

import pg8000.dbapi
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

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


DEFAULT_CATEGORIES = [
    "🍔 Еда",
    "🚌 Транспорт",
    "🏠 ЖКХ",
    "🎉 Развлечения",
    "👕 Одежда",
    "💊 Здоровье",
    "📱 Связь/интернет",
    "🛒 Прочее",
]


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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS limits (
                user_id BIGINT NOT NULL,
                category TEXT NOT NULL,
                limit_amount DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (user_id, category)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE (user_id, name)
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


def get_expenses_by_day(user_id: int, since, until):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DATE(created_at), SUM(amount) FROM expenses "
            "WHERE user_id = %s AND created_at >= %s AND created_at < %s "
            "GROUP BY DATE(created_at) ORDER BY DATE(created_at)",
            (user_id, since, until),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def get_expenses_by_category(user_id: int, since):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, SUM(amount) FROM expenses "
            "WHERE user_id = %s AND created_at >= %s "
            "GROUP BY category ORDER BY SUM(amount) DESC",
            (user_id, since),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()
    since = datetime.combine(date.today().replace(day=1), datetime.min.time())
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses "
            "WHERE user_id = %s AND category = %s AND created_at >= %s",
            (user_id, category, since),
        )
        total = cur.fetchone()[0]
        cur.close()
        return float(total)
    finally:
        conn.close()


def set_limit(user_id: int, category: str, amount: float):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO limits (user_id, category, limit_amount)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, category)
            DO UPDATE SET limit_amount = EXCLUDED.limit_amount
            """,
            (user_id, category, amount),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def get_limit(user_id: int, category: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT limit_amount FROM limits WHERE user_id = %s AND category = %s",
            (user_id, category),
        )
        row = cur.fetchone()
        cur.close()
        return float(row[0]) if row else None
    finally:
        conn.close()


def get_all_limits(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, limit_amount FROM limits WHERE user_id = %s ORDER BY category",
            (user_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


# ---------- Категории (хранятся в базе, свои у каждого пользователя) ----------

def ensure_default_categories(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM categories WHERE user_id = %s", (user_id,))
        count = cur.fetchone()[0]
        if count == 0:
            for i, name in enumerate(DEFAULT_CATEGORIES):
                cur.execute(
                    "INSERT INTO categories (user_id, name, sort_order) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, name) DO NOTHING",
                    (user_id, name, i),
                )
            conn.commit()
        cur.close()
    finally:
        conn.close()


def get_user_categories(user_id: int):
    ensure_default_categories(user_id)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM categories WHERE user_id = %s ORDER BY sort_order, name",
            (user_id,),
        )
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
        return rows
    finally:
        conn.close()


def add_category(user_id: int, name: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) FROM categories WHERE user_id = %s", (user_id,))
        max_order = cur.fetchone()[0]
        try:
            cur.execute(
                "INSERT INTO categories (user_id, name, sort_order) VALUES (%s, %s, %s)",
                (user_id, name, max_order + 1),
            )
            conn.commit()
            ok = True
        except Exception:
            conn.rollback()
            ok = False
        cur.close()
        return ok
    finally:
        conn.close()


def rename_category(user_id: int, old_name: str, new_name: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE categories SET name = %s WHERE user_id = %s AND name = %s",
                (new_name, user_id, old_name),
            )
            # Обновляем категорию и в уже существующих расходах/лимитах для согласованности
            cur.execute(
                "UPDATE expenses SET category = %s WHERE user_id = %s AND category = %s",
                (new_name, user_id, old_name),
            )
            cur.execute(
                "UPDATE limits SET category = %s WHERE user_id = %s AND category = %s "
                "AND NOT EXISTS (SELECT 1 FROM limits WHERE user_id = %s AND category = %s)",
                (new_name, user_id, old_name, user_id, new_name),
            )
            conn.commit()
            ok = True
        except Exception:
            conn.rollback()
            ok = False
        cur.close()
        return ok
    finally:
        conn.close()


def delete_category(user_id: int, name: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM categories WHERE user_id = %s AND name = %s",
            (user_id, name),
        )
        cur.execute(
            "DELETE FROM limits WHERE user_id = %s AND category = %s",
            (user_id, name),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ---------- Состояния диалогов ----------

WAITING_AMOUNT, WAITING_CATEGORY, WAITING_CUSTOM_CATEGORY = range(3)
LIMIT_WAITING_CATEGORY, LIMIT_WAITING_AMOUNT = range(3, 5)
CATMGR_WAITING_NEW_NAME, CATMGR_WAITING_RENAME = range(5, 7)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Добавить расход"],
        ["📊 За сегодня", "📅 За месяц"],
        ["📈 Графики", "🎯 Лимиты"],
        ["⚙️ Категории", "❌ Удалить последнюю"],
        ["ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)


def categories_inline_keyboard(user_id: int, prefix="cat"):
    cats = get_user_categories(user_id)
    buttons = []
    row = []
    for i, cat in enumerate(cats, 1):
        row.append(InlineKeyboardButton(cat, callback_data=f"{prefix}:{cat}"))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if prefix == "cat":
        buttons.append([InlineKeyboardButton("✏️ Своя категория (разово)", callback_data="cat:custom")])
    buttons.append([InlineKeyboardButton("🚫 Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(buttons)


def limits_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Установить / изменить лимит", callback_data="limitmenu:set")],
            [InlineKeyboardButton("🚫 Закрыть", callback_data="limitmenu:close")],
        ]
    )


def categories_manage_keyboard(user_id: int):
    cats = get_user_categories(user_id)
    buttons = []
    for cat in cats:
        buttons.append([
            InlineKeyboardButton(cat, callback_data="catnoop"),
            InlineKeyboardButton("✏️", callback_data=f"catedit:{cat}"),
            InlineKeyboardButton("🗑", callback_data=f"catdel:{cat}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Добавить категорию", callback_data="catadd:new")])
    buttons.append([InlineKeyboardButton("🚫 Закрыть", callback_data="catmgr:close")])
    return InlineKeyboardMarkup(buttons)


# ---------- Проверка лимита после добавления расхода ----------

async def check_and_warn_limit(user_id: int, category: str, send_func):
    limit = get_limit(user_id, category)
    if limit is None:
        return
    spent = get_month_spent_for_category(user_id, category)
    ratio = spent / limit if limit > 0 else 0

    if spent > limit:
        await send_func(
            f"⚠️ Превышен лимит по категории «{category}»!\n"
            f"Потрачено в этом месяце: {spent:.2f} из {limit:.2f}"
        )
    elif ratio >= 0.8:
        await send_func(
            f"🔔 Внимание: потрачено {spent:.2f} из {limit:.2f} по категории «{category}» "
            f"({ratio*100:.0f}% лимита за месяц)"
        )


COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
    "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
]


def make_pie_chart(rows, title):
    labels = [r[0] for r in rows]
    values = [float(r[1]) for r in rows]
    total = sum(values)

    fig, ax = plt.subplots(figsize=(7, 5), facecolor="#1e1e2e")
    ax.set_facecolor("#1e1e2e")

    wedges, texts, autotexts = ax.pie(
        values,
        labels=None,
        autopct=lambda p: f"{p:.1f}%" if p > 3 else "",
        colors=COLORS[:len(values)],
        startangle=140,
        wedgeprops={"edgecolor": "#1e1e2e", "linewidth": 1.5},
        pctdistance=0.75,
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontsize(9)

    legend_labels = [f"{l}  —  {v:.0f}" for l, v in zip(labels, values)]
    patches = [mpatches.Patch(color=COLORS[i % len(COLORS)], label=legend_labels[i])
               for i in range(len(labels))]
    ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1, 0.5),
              fontsize=9, facecolor="#2e2e3e", labelcolor="white", edgecolor="#555")

    ax.set_title(f"{title}\nВсего: {total:.0f}", color="white", fontsize=12, pad=15)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#1e1e2e")
    plt.close(fig)
    buf.seek(0)
    return buf


def make_bar_chart(rows, title):
    if not rows:
        return None

    days = [str(r[0]) for r in rows]
    values = [float(r[1]) for r in rows]

    # Короткие метки: день.месяц
    short_labels = []
    for d in days:
        parts = d.split("-")
        short_labels.append(f"{parts[2]}.{parts[1]}")

    fig, ax = plt.subplots(figsize=(max(7, len(days) * 0.5 + 2), 4.5), facecolor="#1e1e2e")
    ax.set_facecolor("#2e2e3e")

    bars = ax.bar(range(len(values)), values, color="#4ECDC4", edgecolor="#1e1e2e", linewidth=0.8)

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.01,
                f"{val:.0f}", ha="center", va="bottom", color="white", fontsize=8)

    ax.set_xticks(range(len(short_labels)))
    ax.set_xticklabels(short_labels, rotation=45, ha="right", color="white", fontsize=8)
    ax.tick_params(axis="y", colors="white")
    ax.spines[:].set_color("#555")
    ax.set_title(title, color="white", fontsize=12, pad=10)
    ax.set_ylabel("Сумма", color="white", fontsize=9)
    ax.yaxis.label.set_color("white")

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#1e1e2e")
    plt.close(fig)
    buf.seek(0)
    return buf


def charts_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🥧 Расходы по категориям (месяц)", callback_data="chart:pie_month")],
            [InlineKeyboardButton("📊 Траты по дням (месяц)", callback_data="chart:bar_month")],
            [InlineKeyboardButton("📊 Траты по дням (7 дней)", callback_data="chart:bar_week")],
            [InlineKeyboardButton("🚫 Закрыть", callback_data="chart:close")],
        ]
    )


async def charts_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери тип графика:",
        reply_markup=charts_menu_keyboard(),
    )


async def chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if action == "close":
        await query.edit_message_text("Закрыто.")
        return

    await query.edit_message_text("⏳ Строю график, подожди секунду...")

    if action == "pie_month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        rows = get_expenses_by_category(user_id, since)
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="За этот месяц расходов пока нет.")
            return
        month_name = date.today().strftime("%B %Y")
        buf = make_pie_chart(rows, f"Расходы по категориям\n{month_name}")
        await context.bot.send_photo(chat_id=chat_id, photo=buf)

    elif action == "bar_month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        until = datetime.now()
        rows = get_expenses_by_day(user_id, since, until)
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="За этот месяц расходов пока нет.")
            return
        month_name = date.today().strftime("%B %Y")
        buf = make_bar_chart(rows, f"Траты по дням — {month_name}")
        await context.bot.send_photo(chat_id=chat_id, photo=buf)

    elif action == "bar_week":
        since = datetime.combine(date.today() - timedelta(days=6), datetime.min.time())
        until = datetime.now()
        rows = get_expenses_by_day(user_id, since, until)
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="За последние 7 дней расходов пока нет.")
            return
        buf = make_bar_chart(rows, "Траты за последние 7 дней")
        await context.bot.send_photo(chat_id=chat_id, photo=buf)


# ---------- Базовые команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_default_categories(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я помогу вести учёт расходов.\n\n"
        "➕ Добавить расход — записать трату по шагам\n"
        "📈 Графики — круговая диаграмма и график по дням\n"
        "🎯 Лимиты — установить лимит по категории и следить за ним\n"
        "⚙️ Категории — добавить, переименовать или удалить категории\n"
        "📊 За сегодня / 📅 За месяц — посмотреть статистику\n"
        "/add 500 еда — быстрое добавление одной строкой\n",
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
    user_id = update.effective_user.id
    add_expense(user_id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")
    await check_and_warn_limit(user_id, category, update.message.reply_text)
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
        reply_markup=categories_inline_keyboard(update.effective_user.id, "cat"),
    )
    return WAITING_CATEGORY


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "cancel":
        context.user_data.pop("pending_amount", None)
        await query.edit_message_text("Добавление отменено.")
        return ConversationHandler.END

    if choice == "custom":
        await query.edit_message_text("Напиши название категории текстом (разово, не сохранится в списке):")
        return WAITING_CUSTOM_CATEGORY

    amount = context.user_data.pop("pending_amount", None)
    if amount is None:
        await query.edit_message_text("Что-то пошло не так, начни заново через ➕ Добавить расход.")
        return ConversationHandler.END

    user_id = query.from_user.id
    add_expense(user_id, amount, choice)
    await query.edit_message_text(f"Добавлено: {amount:.2f} — {choice}")

    chat_id = query.message.chat_id

    async def send_warning(txt):
        await context.bot.send_message(chat_id=chat_id, text=txt)

    await check_and_warn_limit(user_id, choice, send_warning)
    return ConversationHandler.END


async def custom_category_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    amount = context.user_data.pop("pending_amount", None)
    if amount is None or not category:
        await update.message.reply_text("Что-то пошло не так, начни заново через ➕ Добавить расход.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    add_expense(user_id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")
    await check_and_warn_limit(user_id, category, update.message.reply_text)
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_amount", None)
    context.user_data.pop("pending_limit_category", None)
    context.user_data.pop("pending_rename_category", None)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------- Лимиты ----------

async def limits_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_all_limits(user_id)

    if not rows:
        text = "Лимиты пока не установлены."
    else:
        lines = ["Текущие лимиты на этот месяц:\n"]
        for category, limit_amount in rows:
            spent = get_month_spent_for_category(user_id, category)
            ratio = spent / limit_amount if limit_amount > 0 else 0
            mark = "🔴" if spent > limit_amount else ("🟡" if ratio >= 0.8 else "🟢")
            lines.append(f"{mark} {category}: {spent:.2f} / {limit_amount:.2f}")
        text = "\n".join(lines)

    await update.message.reply_text(text, reply_markup=limits_menu_keyboard())


async def limits_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "close":
        await query.edit_message_text("Закрыто.")
        return ConversationHandler.END

    if action == "set":
        await query.edit_message_text(
            "Выбери категорию, для которой хочешь задать месячный лимит:",
            reply_markup=categories_inline_keyboard(query.from_user.id, "limitcat"),
        )
        return LIMIT_WAITING_CATEGORY

    return ConversationHandler.END


async def limit_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "cancel":
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    context.user_data["pending_limit_category"] = choice
    await query.edit_message_text(
        f"Категория: {choice}\nВведи сумму месячного лимита (например: 15000):"
    )
    return LIMIT_WAITING_AMOUNT


async def limit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нужно ввести положительное число, например: 15000")
        return LIMIT_WAITING_AMOUNT

    category = context.user_data.pop("pending_limit_category", None)
    if not category:
        await update.message.reply_text("Что-то пошло не так, начни заново через 🎯 Лимиты.")
        return ConversationHandler.END

    set_limit(update.effective_user.id, category, amount)
    await update.message.reply_text(
        f"Лимит установлен: {category} — {amount:.2f} в месяц.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ---------- Управление категориями ----------

async def categories_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Твои категории:\n✏️ — переименовать, 🗑 — удалить",
        reply_markup=categories_manage_keyboard(user_id),
    )


async def categories_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "catmgr:close":
        await query.edit_message_text("Закрыто.")
        return ConversationHandler.END

    if data == "catnoop":
        return None

    if data == "catadd:new":
        await query.edit_message_text("Введи название новой категории (можно с эмодзи):")
        return CATMGR_WAITING_NEW_NAME

    if data.startswith("catedit:"):
        old_name = data.split(":", 1)[1]
        context.user_data["pending_rename_category"] = old_name
        await query.edit_message_text(f"Введи новое название для категории «{old_name}»:")
        return CATMGR_WAITING_RENAME

    if data.startswith("catdel:"):
        name = data.split(":", 1)[1]
        delete_category(query.from_user.id, name)
        await query.edit_message_text(
            f"Категория «{name}» удалена. Уже добавленные расходы в этой категории остаются в истории.",
            reply_markup=categories_manage_keyboard(query.from_user.id),
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def category_new_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым, попробуй ещё раз:")
        return CATMGR_WAITING_NEW_NAME

    user_id = update.effective_user.id
    ok = add_category(user_id, name)
    if ok:
        await update.message.reply_text(
            f"Категория «{name}» добавлена.",
            reply_markup=categories_manage_keyboard(user_id),
        )
    else:
        await update.message.reply_text(
            f"Такая категория уже есть. Попробуй другое название:",
        )
        return CATMGR_WAITING_NEW_NAME
    return ConversationHandler.END


async def category_rename_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    old_name = context.user_data.pop("pending_rename_category", None)
    if not old_name or not new_name:
        await update.message.reply_text("Что-то пошло не так, начни заново через ⚙️ Категории.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    ok = rename_category(user_id, old_name, new_name)
    if ok:
        await update.message.reply_text(
            f"Категория «{old_name}» переименована в «{new_name}». "
            f"История расходов и лимит по этой категории сохранены.",
            reply_markup=categories_manage_keyboard(user_id),
        )
    else:
        await update.message.reply_text(
            "Не получилось переименовать (возможно, такое название уже есть). Попробуй другое:"
        )
        context.user_data["pending_rename_category"] = old_name
        return CATMGR_WAITING_RENAME
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
    elif text == "📈 Графики":
        await charts_open(update, context)
    elif text == "🎯 Лимиты":
        await limits_open(update, context)
    elif text == "⚙️ Категории":
        await categories_open(update, context)


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

    limit_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(limits_menu_callback, pattern=r"^limitmenu:"),
        ],
        states={
            LIMIT_WAITING_CATEGORY: [
                CallbackQueryHandler(limit_category_chosen, pattern=r"^limitcat:")
            ],
            LIMIT_WAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, limit_amount_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )

    category_manage_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                categories_manage_callback,
                pattern=r"^(catmgr:|catadd:|catedit:|catdel:|catnoop)",
            ),
        ],
        states={
            CATMGR_WAITING_NEW_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, category_new_name_received)
            ],
            CATMGR_WAITING_RENAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, category_rename_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(add_conversation)
    app.add_handler(limit_conversation)
    app.add_handler(category_manage_conversation)
    app.add_handler(CallbackQueryHandler(chart_callback, pattern=r"^chart:"))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("limits", limits_open))
    app.add_handler(CommandHandler("categories", categories_open))
    app.add_handler(CommandHandler("charts", charts_open))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
