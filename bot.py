import io
import os
import re
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

import pg8000.dbapi
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import httpx

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

# ──────────────────────────────────────────────
# КОНФИГУРАЦИЯ
# ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
DATABASE_URL = os.environ.get("DATABASE_URL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан. Добавь переменную в Railway Variables.")

_parsed = urlparse(DATABASE_URL)


def get_conn():
    return pg8000.dbapi.connect(
        user=_parsed.username,
        password=_parsed.password,
        host=_parsed.hostname,
        port=_parsed.port or 5432,
        database=_parsed.path.lstrip("/"),
    )


# ──────────────────────────────────────────────
# БАЗА ДАННЫХ
# ──────────────────────────────────────────────
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            category TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS incomes (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            source TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS limits (
            user_id BIGINT NOT NULL,
            category TEXT NOT NULL,
            limit_amount DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (user_id, category)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_id, name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recurring (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            name TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            category TEXT NOT NULL,
            day_of_month INTEGER NOT NULL,
            last_added DATE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Расходы ──
def add_expense(user_id, amount, category):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO expenses (user_id, amount, category, created_at) VALUES (%s,%s,%s,%s)",
        (user_id, amount, category, datetime.now()),
    )
    conn.commit()
    cur.close()
    conn.close()


def delete_last_expense(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM expenses WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM expenses WHERE id=%s", (row[0],))
    conn.commit()
    cur.close()
    conn.close()
    return row is not None


def get_expenses(user_id, since):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT amount, category, created_at FROM expenses "
        "WHERE user_id=%s AND created_at>=%s ORDER BY created_at DESC",
        (user_id, since),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_expenses_by_day(user_id, since, until):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT DATE(created_at), SUM(amount) FROM expenses "
        "WHERE user_id=%s AND created_at>=%s AND created_at<%s "
        "GROUP BY DATE(created_at) ORDER BY DATE(created_at)",
        (user_id, since, until),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_expenses_by_category(user_id, since):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT category, SUM(amount) FROM expenses "
        "WHERE user_id=%s AND created_at>=%s "
        "GROUP BY category ORDER BY SUM(amount) DESC",
        (user_id, since),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_month_spent(user_id, category):
    since = datetime.combine(date.today().replace(day=1), datetime.min.time())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) FROM expenses "
        "WHERE user_id=%s AND category=%s AND created_at>=%s",
        (user_id, category, since),
    )
    total = float(cur.fetchone()[0])
    cur.close()
    conn.close()
    return total


# ── Доходы ──
def add_income(user_id, amount, source):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO incomes (user_id, amount, source, created_at) VALUES (%s,%s,%s,%s)",
        (user_id, amount, source, datetime.now()),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_incomes(user_id, since):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT amount, source FROM incomes WHERE user_id=%s AND created_at>=%s",
        (user_id, since),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_balance(user_id, since):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) FROM incomes WHERE user_id=%s AND created_at>=%s",
        (user_id, since),
    )
    total_in = float(cur.fetchone()[0])
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=%s AND created_at>=%s",
        (user_id, since),
    )
    total_ex = float(cur.fetchone()[0])
    cur.close()
    conn.close()
    return total_in, total_ex


# ── Лимиты ──
def set_limit(user_id, category, amount):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO limits (user_id, category, limit_amount) VALUES (%s,%s,%s) "
        "ON CONFLICT (user_id, category) DO UPDATE SET limit_amount=EXCLUDED.limit_amount",
        (user_id, category, amount),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_limit(user_id, category):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT limit_amount FROM limits WHERE user_id=%s AND category=%s",
        (user_id, category),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return float(row[0]) if row else None


def get_all_limits(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT category, limit_amount FROM limits WHERE user_id=%s ORDER BY category",
        (user_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ── Категории ──
DEFAULT_CATEGORIES = [
    "🍔 Еда", "🚌 Транспорт", "🏠 ЖКХ", "🎉 Развлечения",
    "👕 Одежда", "💊 Здоровье", "📱 Связь/интернет", "🛒 Прочее",
]


def ensure_default_categories(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM categories WHERE user_id=%s", (user_id,))
    if cur.fetchone()[0] == 0:
        for i, name in enumerate(DEFAULT_CATEGORIES):
            cur.execute(
                "INSERT INTO categories (user_id, name, sort_order) VALUES (%s,%s,%s) "
                "ON CONFLICT (user_id, name) DO NOTHING",
                (user_id, name, i),
            )
        conn.commit()
    cur.close()
    conn.close()


def get_user_categories(user_id):
    ensure_default_categories(user_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM categories WHERE user_id=%s ORDER BY sort_order, name",
        (user_id,),
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def add_category(user_id, name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(sort_order),-1) FROM categories WHERE user_id=%s", (user_id,))
    max_order = cur.fetchone()[0]
    try:
        cur.execute(
            "INSERT INTO categories (user_id, name, sort_order) VALUES (%s,%s,%s)",
            (user_id, name, max_order + 1),
        )
        conn.commit()
        ok = True
    except Exception:
        conn.rollback()
        ok = False
    cur.close()
    conn.close()
    return ok


def rename_category(user_id, old_name, new_name):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE categories SET name=%s WHERE user_id=%s AND name=%s",
            (new_name, user_id, old_name),
        )
        cur.execute(
            "UPDATE expenses SET category=%s WHERE user_id=%s AND category=%s",
            (new_name, user_id, old_name),
        )
        cur.execute(
            "UPDATE limits SET category=%s WHERE user_id=%s AND category=%s "
            "AND NOT EXISTS (SELECT 1 FROM limits WHERE user_id=%s AND category=%s)",
            (new_name, user_id, old_name, user_id, new_name),
        )
        conn.commit()
        ok = True
    except Exception:
        conn.rollback()
        ok = False
    cur.close()
    conn.close()
    return ok


def delete_category(user_id, name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM categories WHERE user_id=%s AND name=%s", (user_id, name))
    cur.execute("DELETE FROM limits WHERE user_id=%s AND category=%s", (user_id, name))
    conn.commit()
    cur.close()
    conn.close()


# ── Регулярные платежи ──
def add_recurring(user_id, name, amount, category, day_of_month):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO recurring (user_id, name, amount, category, day_of_month, last_added) "
        "VALUES (%s,%s,%s,%s,%s,NULL)",
        (user_id, name, amount, category, day_of_month),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_recurring(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, amount, category, day_of_month, last_added "
        "FROM recurring WHERE user_id=%s ORDER BY day_of_month, name",
        (user_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_all_recurring():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, name, amount, category, day_of_month, last_added FROM recurring"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def delete_recurring(rec_id, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM recurring WHERE id=%s AND user_id=%s", (rec_id, user_id))
    conn.commit()
    cur.close()
    conn.close()


def mark_recurring_added(rec_id, added_date):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE recurring SET last_added=%s WHERE id=%s", (added_date, rec_id))
    conn.commit()
    cur.close()
    conn.close()


# ──────────────────────────────────────────────
# СОСТОЯНИЯ ДИАЛОГОВ
# ──────────────────────────────────────────────
(
    EXP_AMOUNT, EXP_CATEGORY, EXP_CUSTOM_CAT,
    INC_AMOUNT, INC_SOURCE, INC_CUSTOM_SRC,
    LIM_CATEGORY, LIM_AMOUNT,
    CAT_NEW_NAME, CAT_RENAME,
    REC_NAME, REC_AMOUNT, REC_CATEGORY, REC_DAY,
    VOICE_CONFIRM,
) = range(15)

# ──────────────────────────────────────────────
# КЛАВИАТУРЫ
# ──────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Добавить расход", "💵 Добавить доход"],
        ["📊 За сегодня", "📅 За месяц"],
        ["💰 Баланс", "📈 Графики"],
        ["🎯 Лимиты", "⚙️ Категории"],
        ["🔄 Регулярные платежи", "❌ Удалить последнюю"],
        ["ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)

INCOME_SOURCES = [
    "💼 Зарплата", "🎁 Подарок", "📈 Инвестиции", "🏠 Аренда",
    "💻 Фриланс", "🏦 Кешбэк/проценты", "🛍 Продажа", "💰 Прочее",
]

CHART_COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
    "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9",
]


def make_kb(items, prefix, extra_buttons=None, cancel=True):
    """Универсальный конструктор inline-клавиатуры."""
    buttons, row = [], []
    for i, item in enumerate(items, 1):
        row.append(InlineKeyboardButton(item, callback_data=f"{prefix}:{item}"))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if extra_buttons:
        for btn in extra_buttons:
            buttons.append([btn])
    if cancel:
        buttons.append([InlineKeyboardButton("🚫 Отмена", callback_data=f"{prefix}:__cancel__")])
    return InlineKeyboardMarkup(buttons)


# ──────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────
async def warn_limit(user_id, category, reply_func):
    limit = get_limit(user_id, category)
    if not limit:
        return
    spent = get_month_spent(user_id, category)
    ratio = spent / limit
    if spent > limit:
        await reply_func(f"⚠️ Превышен лимит «{category}»! {spent:.2f} из {limit:.2f}")
    elif ratio >= 0.8:
        await reply_func(f"🔔 {spent:.2f} из {limit:.2f} по «{category}» ({ratio*100:.0f}%)")


def fmt_summary(rows, title):
    if not rows:
        return f"{title}: записей пока нет."
    total = sum(r[0] for r in rows)
    by_cat = {}
    for amt, cat, *_ in rows:
        by_cat[cat] = by_cat.get(cat, 0) + amt
    lines = [title, f"Всего: {total:.2f}", ""]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"• {cat}: {amt:.2f}")
    return "\n".join(lines)


def make_pie(rows, title):
    labels = [r[0] for r in rows]
    values = [float(r[1]) for r in rows]
    fig, ax = plt.subplots(figsize=(7, 5), facecolor="#1e1e2e")
    ax.set_facecolor("#1e1e2e")
    wedges, _, autotexts = ax.pie(
        values, labels=None, autopct=lambda p: f"{p:.1f}%" if p > 3 else "",
        colors=CHART_COLORS[:len(values)], startangle=140,
        wedgeprops={"edgecolor": "#1e1e2e", "linewidth": 1.5}, pctdistance=0.75,
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontsize(9)
    patches = [mpatches.Patch(color=CHART_COLORS[i % len(CHART_COLORS)],
               label=f"{labels[i]}  —  {values[i]:.0f}") for i in range(len(labels))]
    ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1, 0.5),
              fontsize=9, facecolor="#2e2e3e", labelcolor="white", edgecolor="#555")
    ax.set_title(f"{title}\nВсего: {sum(values):.0f}", color="white", fontsize=12, pad=15)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#1e1e2e")
    plt.close(fig)
    buf.seek(0)
    return buf


def make_bar(rows, title):
    if not rows:
        return None
    days = [str(r[0]) for r in rows]
    values = [float(r[1]) for r in rows]
    labels = [f"{d.split('-')[2]}.{d.split('-')[1]}" for d in days]
    fig, ax = plt.subplots(figsize=(max(7, len(days) * 0.5 + 2), 4.5), facecolor="#1e1e2e")
    ax.set_facecolor("#2e2e3e")
    bars = ax.bar(range(len(values)), values, color="#4ECDC4", edgecolor="#1e1e2e", linewidth=0.8)
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.01,
                f"{val:.0f}", ha="center", va="bottom", color="white", fontsize=8)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", color="white", fontsize=8)
    ax.tick_params(axis="y", colors="white")
    ax.spines[:].set_color("#555")
    ax.set_title(title, color="white", fontsize=12, pad=10)
    ax.set_ylabel("Сумма", color="white", fontsize=9)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#1e1e2e")
    plt.close(fig)
    buf.seek(0)
    return buf


# ──────────────────────────────────────────────
# КОМАНДЫ БЕЗ ДИАЛОГА
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_default_categories(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я бюджетный трекер.\n\n"
        "➕ Добавить расход / 💵 Добавить доход — записать трату или доход\n"
        "💰 Баланс — остаток за месяц\n"
        "📈 Графики — диаграммы расходов\n"
        "🎯 Лимиты — лимиты по категориям\n"
        "⚙️ Категории — управление категориями\n"
        "🔄 Регулярные платежи — автоматические ежемесячные траты\n"
        "/add 500 еда — быстрое добавление расхода\n"
        "/income 50000 зарплата — быстрое добавление дохода\n",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    since = datetime.combine(date.today(), datetime.min.time())
    rows = get_expenses(update.effective_user.id, since)
    await update.message.reply_text(fmt_summary(rows, "Расходы за сегодня"))


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    since = datetime.combine(date.today().replace(day=1), datetime.min.time())
    rows = get_expenses(update.effective_user.id, since)
    await update.message.reply_text(fmt_summary(rows, "Расходы за месяц"))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok = delete_last_expense(update.effective_user.id)
    await update.message.reply_text(
        "Последняя запись удалена." if ok else "Записей пока нет."
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    since = datetime.combine(date.today().replace(day=1), datetime.min.time())
    inc_total, exp_total = get_balance(user_id, since)
    inc_rows = get_incomes(user_id, since)
    by_src = {}
    for amt, src in inc_rows:
        by_src[src] = by_src.get(src, 0) + amt
    month_name = date.today().strftime("%B %Y")
    lines = [f"💰 Баланс за {month_name}", ""]
    lines.append("📥 Доходы:")
    if by_src:
        for src, amt in sorted(by_src.items(), key=lambda x: -x[1]):
            lines.append(f"  • {src}: +{amt:.2f}")
        lines.append(f"  Итого: {inc_total:.2f}")
    else:
        lines.append("  Доходов пока нет")
    lines.append(f"\n📤 Расходы: {exp_total:.2f}")
    remaining = inc_total - exp_total
    if inc_total == 0:
        lines.append(f"\n💳 Остаток: {remaining:.2f}")
        lines.append("(Добавь доходы через 💵 Добавить доход)")
    elif remaining >= 0:
        lines.append(f"\n✅ Остаток: {remaining:.2f}")
    else:
        lines.append(f"\n🔴 Перерасход: {remaining:.2f}")
    await update.message.reply_text("\n".join(lines))


async def cmd_charts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выбери тип графика:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🥧 По категориям (месяц)", callback_data="chart:pie_month")],
            [InlineKeyboardButton("📊 По дням (месяц)", callback_data="chart:bar_month")],
            [InlineKeyboardButton("📊 По дням (7 дней)", callback_data="chart:bar_week")],
            [InlineKeyboardButton("🚫 Закрыть", callback_data="chart:close")],
        ]),
    )


async def cb_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if action == "close":
        await query.edit_message_text("Закрыто.")
        return

    await query.edit_message_text("⏳ Строю график...")

    if action == "pie_month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        rows = get_expenses_by_category(user_id, since)
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="За этот месяц расходов нет.")
            return
        buf = make_pie(rows, f"Расходы по категориям\n{date.today().strftime('%B %Y')}")
        await context.bot.send_photo(chat_id=chat_id, photo=buf)

    elif action == "bar_month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        rows = get_expenses_by_day(user_id, since, datetime.now())
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="За этот месяц расходов нет.")
            return
        buf = make_bar(rows, f"Траты по дням — {date.today().strftime('%B %Y')}")
        await context.bot.send_photo(chat_id=chat_id, photo=buf)

    elif action == "bar_week":
        since = datetime.combine(date.today() - timedelta(days=6), datetime.min.time())
        rows = get_expenses_by_day(user_id, since, datetime.now())
        if not rows:
            await context.bot.send_message(chat_id=chat_id, text="За 7 дней расходов нет.")
            return
        buf = make_bar(rows, "Траты за последние 7 дней")
        await context.bot.send_photo(chat_id=chat_id, photo=buf)


async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_all_limits(user_id)
    if not rows:
        text = "Лимиты пока не установлены."
    else:
        lines = ["Лимиты на этот месяц:\n"]
        for cat, lim in rows:
            spent = get_month_spent(user_id, cat)
            ratio = spent / lim if lim > 0 else 0
            mark = "🔴" if spent > lim else ("🟡" if ratio >= 0.8 else "🟢")
            lines.append(f"{mark} {cat}: {spent:.2f} / {lim:.2f}")
        text = "\n".join(lines)
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Установить лимит", callback_data="limitmenu:set")],
            [InlineKeyboardButton("🚫 Закрыть", callback_data="limitmenu:close")],
        ]),
    )


async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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
    await update.message.reply_text(
        "Твои категории:\n✏️ — переименовать, 🗑 — удалить",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_recurring(user_id)
    today_day = date.today().day

    if not rows:
        text = "Регулярных платежей пока нет."
    else:
        import calendar
        days_in_month = calendar.monthrange(date.today().year, date.today().month)[1]
        lines = ["🔄 Регулярные платежи:\n"]
        for rec_id, name, amount, category, day, last_added in rows:
            days_left = day - today_day
            if days_left < 0:
                days_left = days_in_month - today_day + day
            mark = "🔴 Сегодня!" if days_left == 0 else (
                f"🟡 через {days_left} дн." if days_left <= 3 else f"🟢 {day}-го числа"
            )
            lines.append(f"{mark}  {name} — {amount:.0f} ({category})")
        text = "\n".join(lines)

    buttons = []
    for rec_id, name, amount, category, day, last_added in rows:
        buttons.append([InlineKeyboardButton(f"🗑 Удалить: {name}", callback_data=f"recdel:{rec_id}")])
    buttons.append([InlineKeyboardButton("➕ Добавить шаблон", callback_data="recadd:new")])
    buttons.append([InlineKeyboardButton("🚫 Закрыть", callback_data="recmgr:close")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


# ──────────────────────────────────────────────
# ДИАЛОГ: ДОБАВИТЬ РАСХОД
# ──────────────────────────────────────────────
async def exp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи сумму расхода (например: 350):")
    return EXP_AMOUNT


async def exp_add_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        return await exp_start(update, context)
    if len(args) < 2:
        await update.message.reply_text("Формат: /add 350 кафе")
        return ConversationHandler.END
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return ConversationHandler.END
    category = " ".join(args[1:])
    user_id = update.effective_user.id
    add_expense(user_id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")
    await warn_limit(user_id, category, update.message.reply_text)
    return ConversationHandler.END


async def exp_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например: 350")
        return EXP_AMOUNT
    context.user_data["exp_amount"] = amount
    cats = get_user_categories(update.effective_user.id)
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\nВыбери категорию:",
        reply_markup=make_kb(
            cats, "expcat",
            extra_buttons=[InlineKeyboardButton("✏️ Своя категория", callback_data="expcat:__custom__")],
        ),
    )
    return EXP_CATEGORY


async def exp_got_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "__cancel__":
        context.user_data.pop("exp_amount", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    if choice == "__custom__":
        await query.edit_message_text("Введи название категории:")
        return EXP_CUSTOM_CAT

    amount = context.user_data.pop("exp_amount", None)
    if amount is None:
        await query.edit_message_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END

    user_id = query.from_user.id
    add_expense(user_id, amount, choice)
    await query.edit_message_text(f"Добавлено: {amount:.2f} — {choice}")
    await warn_limit(user_id, choice,
                     lambda t: context.bot.send_message(chat_id=query.message.chat_id, text=t))
    return ConversationHandler.END


async def exp_got_custom_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    amount = context.user_data.pop("exp_amount", None)
    if not category or amount is None:
        await update.message.reply_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END
    user_id = update.effective_user.id
    add_expense(user_id, amount, category)
    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")
    await warn_limit(user_id, category, update.message.reply_text)
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: ДОБАВИТЬ ДОХОД
# ──────────────────────────────────────────────
async def inc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи сумму дохода (например: 50000):")
    return INC_AMOUNT


async def inc_add_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        return await inc_start(update, context)
    if len(args) < 2:
        await update.message.reply_text("Формат: /income 50000 зарплата")
        return ConversationHandler.END
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return ConversationHandler.END
    source = " ".join(args[1:])
    add_income(update.effective_user.id, amount, source)
    await update.message.reply_text(f"Доход записан: +{amount:.2f} — {source}")
    return ConversationHandler.END


async def inc_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например: 50000")
        return INC_AMOUNT
    context.user_data["inc_amount"] = amount
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\nВыбери источник дохода:",
        reply_markup=make_kb(
            INCOME_SOURCES, "incsrc",
            extra_buttons=[InlineKeyboardButton("✏️ Свой источник", callback_data="incsrc:__custom__")],
        ),
    )
    return INC_SOURCE


async def inc_got_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "__cancel__":
        context.user_data.pop("inc_amount", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    if choice == "__custom__":
        await query.edit_message_text("Введи название источника дохода:")
        return INC_CUSTOM_SRC

    amount = context.user_data.pop("inc_amount", None)
    if amount is None:
        await query.edit_message_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END
    add_income(query.from_user.id, amount, choice)
    await query.edit_message_text(f"Доход записан: +{amount:.2f} — {choice}")
    return ConversationHandler.END


async def inc_got_custom_src(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = update.message.text.strip()
    amount = context.user_data.pop("inc_amount", None)
    if not source or amount is None:
        await update.message.reply_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END
    add_income(update.effective_user.id, amount, source)
    await update.message.reply_text(f"Доход записан: +{amount:.2f} — {source}")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: ЛИМИТЫ
# ──────────────────────────────────────────────
async def cb_limitmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "close":
        await query.edit_message_text("Закрыто.")
        return ConversationHandler.END
    cats = get_user_categories(query.from_user.id)
    await query.edit_message_text(
        "Выбери категорию для лимита:",
        reply_markup=make_kb(cats, "limcat"),
    )
    return LIM_CATEGORY


async def lim_got_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__":
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END
    context.user_data["lim_cat"] = choice
    await query.edit_message_text(f"Категория: {choice}\nВведи сумму лимита на месяц:")
    return LIM_AMOUNT


async def lim_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи положительное число:")
        return LIM_AMOUNT
    cat = context.user_data.pop("lim_cat", None)
    if not cat:
        await update.message.reply_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END
    set_limit(update.effective_user.id, cat, amount)
    await update.message.reply_text(f"Лимит установлен: {cat} — {amount:.2f}/мес.")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: УПРАВЛЕНИЕ КАТЕГОРИЯМИ
# ──────────────────────────────────────────────
async def cb_catmgr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "catmgr:close" or data == "catnoop":
        if data == "catmgr:close":
            await query.edit_message_text("Закрыто.")
        return ConversationHandler.END

    if data == "catadd:new":
        await query.edit_message_text("Введи название новой категории:")
        return CAT_NEW_NAME

    if data.startswith("catedit:"):
        old = data.split(":", 1)[1]
        context.user_data["cat_rename_old"] = old
        await query.edit_message_text(f"Новое название для «{old}»:")
        return CAT_RENAME

    if data.startswith("catdel:"):
        name = data.split(":", 1)[1]
        delete_category(user_id, name)
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
        await query.edit_message_text(
            f"Категория «{name}» удалена.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def cat_got_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return CAT_NEW_NAME
    user_id = update.effective_user.id
    ok = add_category(user_id, name)
    if ok:
        await update.message.reply_text(f"Категория «{name}» добавлена.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text("Такая категория уже есть. Введи другое название:")
        return CAT_NEW_NAME
    return ConversationHandler.END


async def cat_got_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    old_name = context.user_data.pop("cat_rename_old", None)
    if not old_name or not new_name:
        await update.message.reply_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END
    user_id = update.effective_user.id
    ok = rename_category(user_id, old_name, new_name)
    if ok:
        await update.message.reply_text(
            f"«{old_name}» переименовано в «{new_name}».", reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text("Не вышло. Попробуй другое название:")
        context.user_data["cat_rename_old"] = old_name
        return CAT_RENAME
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: РЕГУЛЯРНЫЕ ПЛАТЕЖИ
# ──────────────────────────────────────────────
async def cb_recmgr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "recmgr:close":
        await query.edit_message_text("Закрыто.")
        return ConversationHandler.END

    if data == "recadd:new":
        await query.edit_message_text("Введи название платежа (например: Аренда, Netflix):")
        return REC_NAME

    if data.startswith("recdel:"):
        rec_id = int(data.split(":")[1])
        delete_recurring(rec_id, query.from_user.id)
        await query.edit_message_text("Шаблон удалён.")
        return ConversationHandler.END

    return ConversationHandler.END


async def rec_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return REC_NAME
    context.user_data["rec_name"] = name
    await update.message.reply_text(f"Название: {name}\nВведи сумму платежа:")
    return REC_AMOUNT


async def rec_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи положительное число:")
        return REC_AMOUNT
    context.user_data["rec_amount"] = amount
    cats = get_user_categories(update.effective_user.id)
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\nВыбери категорию:",
        reply_markup=make_kb(cats, "reccat"),
    )
    return REC_CATEGORY


async def rec_got_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__":
        context.user_data.pop("rec_name", None)
        context.user_data.pop("rec_amount", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END
    context.user_data["rec_cat"] = choice
    await query.edit_message_text(
        f"Категория: {choice}\nВведи день месяца для платежа (от 1 до 28):"
    )
    return REC_DAY


async def rec_got_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        day = int(text)
        if day < 1 or day > 28:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число от 1 до 28:")
        return REC_DAY

    name = context.user_data.pop("rec_name", None)
    amount = context.user_data.pop("rec_amount", None)
    category = context.user_data.pop("rec_cat", None)
    if not all([name, amount, category]):
        await update.message.reply_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END

    add_recurring(update.effective_user.id, name, amount, category, day)
    await update.message.reply_text(
        f"✅ Шаблон создан:\n• {name}\n• {amount:.2f}\n• {category}\n• Каждый месяц {day}-го числа\n\n"
        f"Бот автоматически добавит расход и пришлёт напоминание за 3 дня.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ПЛАНИРОВЩИК
# ──────────────────────────────────────────────
async def job_check_recurring(context):
    today = date.today()
    import calendar
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    for rec_id, user_id, name, amount, category, day, last_added in get_all_recurring():
        if today.day == day:
            if last_added is None or (last_added.year, last_added.month) != (today.year, today.month):
                add_expense(user_id, amount, category)
                mark_recurring_added(rec_id, today)
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🔄 Автоматически добавлен платёж:\n• {name} — {amount:.2f} ({category})",
                    )
                except Exception:
                    pass
        elif today.day == day - 3:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🔔 Через 3 дня ({day}-го) платёж «{name}» — {amount:.2f} ({category}).",
                )
            except Exception:
                pass


# ──────────────────────────────────────────────
# ГОЛОСОВОЙ ВВОД
# ──────────────────────────────────────────────

async def transcribe_voice(file_bytes: bytes) -> str:
    """Отправляет аудио в Groq Whisper API и возвращает текст."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": ("voice.ogg", file_bytes, "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "ru"},
        )
        response.raise_for_status()
        return response.json().get("text", "").strip()


def parse_expense_from_text(text: str, categories: list):
    """
    Пытается извлечь сумму и категорию из распознанного текста.
    Примеры: "потратил 350 на кафе", "500 рублей еда", "купил кофе за 200".
    Возвращает (amount, category) или (None, None).
    """
    text_lower = text.lower()

    # Ищем число (сумму)
    amounts = re.findall(r"\b(\d+(?:[.,]\d+)?)\b", text)
    if not amounts:
        return None, None
    amount = float(amounts[0].replace(",", "."))

    # Пробуем найти совпадение с известной категорией (без эмодзи)
    matched_cat = None
    for cat in categories:
        cat_name = re.sub(r"[^\w\s]", "", cat).strip().lower()
        if cat_name and cat_name in text_lower:
            matched_cat = cat
            break

    # Если категорию не нашли — пробуем вытащить слово после "на", "в", "за"
    if not matched_cat:
        for pretext in [r"на\s+(\w+)", r"в\s+(\w+)", r"за\s+\w+\s+(\w+)", r"на\s+(\w+\s+\w+)"]:
            m = re.search(pretext, text_lower)
            if m:
                matched_cat = m.group(1).capitalize()
                break

    return amount, matched_cat


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает голосовое сообщение."""
    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "Голосовой ввод не настроен. Добавь переменную OPENAI_API_KEY в Railway Variables."
        )
        return ConversationHandler.END

    msg = await update.message.reply_text("🎙 Распознаю речь...")

    try:
        # Скачиваем голосовое сообщение
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        file_bytes = await tg_file.download_as_bytearray()

        # Распознаём текст
        text = await transcribe_voice(bytes(file_bytes))
        if not text:
            await msg.edit_text("Не удалось распознать речь. Попробуй ещё раз.")
            return ConversationHandler.END

        await msg.edit_text(f"🎙 Распознано: «{text}»\n\nПарсю сумму и категорию...")

        # Парсим сумму и категорию
        user_id = update.effective_user.id
        cats = get_user_categories(user_id)
        amount, category = parse_expense_from_text(text, cats)

        if amount is None:
            await msg.edit_text(
                f"🎙 Распознано: «{text}»\n\n"
                "Не нашёл сумму в тексте. Попробуй сказать чётче, например:\n"
                "«Потратил 350 рублей на кафе»"
            )
            return ConversationHandler.END

        # Сохраняем для подтверждения
        context.user_data["voice_amount"] = amount
        context.user_data["voice_category"] = category or "🛒 Прочее"
        context.user_data["voice_text"] = text

        cat_display = category if category else "🛒 Прочее (не распознана)"

        await msg.edit_text(
            f"🎙 Распознано: «{text}»\n\n"
            f"Добавить расход?\n"
            f"• Сумма: {amount:.2f}\n"
            f"• Категория: {cat_display}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да", callback_data="voice:confirm"),
                    InlineKeyboardButton("✏️ Изменить категорию", callback_data="voice:change_cat"),
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data="voice:cancel")],
            ]),
        )
        return VOICE_CONFIRM

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            await msg.edit_text("Ошибка: неверный OPENAI_API_KEY. Проверь переменную в Railway.")
        else:
            await msg.edit_text(f"Ошибка API: {e.response.status_code}. Попробуй позже.")
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"Произошла ошибка: {e}")
        return ConversationHandler.END


async def voice_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        context.user_data.pop("voice_amount", None)
        context.user_data.pop("voice_category", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    if action == "change_cat":
        user_id = query.from_user.id
        cats = get_user_categories(user_id)
        await query.edit_message_text(
            "Выбери категорию:",
            reply_markup=make_kb(cats, "voicecat"),
        )
        return VOICE_CONFIRM

    if action == "confirm":
        amount = context.user_data.pop("voice_amount", None)
        category = context.user_data.pop("voice_category", None)
        context.user_data.pop("voice_text", None)

        if amount is None:
            await query.edit_message_text("Что-то пошло не так. Начни заново.")
            return ConversationHandler.END

        user_id = query.from_user.id
        add_expense(user_id, amount, category)
        await query.edit_message_text(f"✅ Добавлено: {amount:.2f} — {category}")
        await warn_limit(
            user_id, category,
            lambda t: context.bot.send_message(chat_id=query.message.chat_id, text=t),
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def voice_change_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор новой категории после голосового ввода."""
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "__cancel__":
        context.user_data.pop("voice_amount", None)
        context.user_data.pop("voice_category", None)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    context.user_data["voice_category"] = choice
    amount = context.user_data.get("voice_amount", 0)

    await query.edit_message_text(
        f"Добавить расход?\n• Сумма: {amount:.2f}\n• Категория: {choice}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да", callback_data="voice:confirm"),
                InlineKeyboardButton("✏️ Изменить категорию", callback_data="voice:change_cat"),
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="voice:cancel")],
        ]),
    )
    return VOICE_CONFIRM


# ──────────────────────────────────────────────
# ОБЩИЙ ОБРАБОТЧИК КНОПОК МЕНЮ
# ──────────────────────────────────────────────
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 За сегодня":
        await cmd_today(update, context)
    elif text == "📅 За месяц":
        await cmd_month(update, context)
    elif text == "❌ Удалить последнюю":
        await cmd_undo(update, context)
    elif text == "ℹ️ Помощь":
        await cmd_start(update, context)
    elif text == "💰 Баланс":
        await cmd_balance(update, context)
    elif text == "📈 Графики":
        await cmd_charts(update, context)
    elif text == "🎯 Лимиты":
        await cmd_limits(update, context)
    elif text == "⚙️ Категории":
        await cmd_categories(update, context)
    elif text == "🔄 Регулярные платежи":
        await cmd_recurring(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Паттерн для кнопок меню — используется как fallback в диалогах
    MENU_PAT = (
        "^(📊 За сегодня|📅 За месяц|❌ Удалить последнюю|ℹ️ Помощь|"
        "💰 Баланс|📈 Графики|🎯 Лимиты|⚙️ Категории|"
        "🔄 Регулярные платежи|💵 Добавить доход|➕ Добавить расход)$"
    )

    def make_fallbacks():
        return [
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.Regex(MENU_PAT), cmd_cancel),
        ]

    # Диалог: расход
    exp_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", exp_add_quick),
            MessageHandler(filters.Regex("^➕ Добавить расход$"), exp_start),
        ],
        states={
            EXP_AMOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_got_amount)],
            EXP_CATEGORY:   [CallbackQueryHandler(exp_got_category, pattern=r"^expcat:")],
            EXP_CUSTOM_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_got_custom_cat)],
        },
        fallbacks=make_fallbacks(),
        allow_reentry=True,
    )

    # Диалог: доход
    inc_conv = ConversationHandler(
        entry_points=[
            CommandHandler("income", inc_add_quick),
            MessageHandler(filters.Regex("^💵 Добавить доход$"), inc_start),
        ],
        states={
            INC_AMOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_got_amount)],
            INC_SOURCE:     [CallbackQueryHandler(inc_got_source, pattern=r"^incsrc:")],
            INC_CUSTOM_SRC: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_got_custom_src)],
        },
        fallbacks=make_fallbacks(),
        allow_reentry=True,
    )

    # Диалог: лимиты
    lim_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_limitmenu, pattern=r"^limitmenu:")],
        states={
            LIM_CATEGORY: [CallbackQueryHandler(lim_got_category, pattern=r"^limcat:")],
            LIM_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, lim_got_amount)],
        },
        fallbacks=make_fallbacks(),
        per_message=False,
        allow_reentry=True,
    )

    # Диалог: категории
    cat_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_catmgr, pattern=r"^(catmgr:|catadd:|catedit:|catdel:|catnoop)"),
        ],
        states={
            CAT_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_got_new_name)],
            CAT_RENAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_got_rename)],
        },
        fallbacks=make_fallbacks(),
        per_message=False,
        allow_reentry=True,
    )

    # Диалог: регулярные платежи
    rec_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_recmgr, pattern=r"^(recadd:|recdel:|recmgr:)"),
        ],
        states={
            REC_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_got_name)],
            REC_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_got_amount)],
            REC_CATEGORY: [CallbackQueryHandler(rec_got_category, pattern=r"^reccat:")],
            REC_DAY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_got_day)],
        },
        fallbacks=make_fallbacks(),
        per_message=False,
        allow_reentry=True,
    )

    # Диалог: голосовой ввод
    voice_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.VOICE, handle_voice),
        ],
        states={
            VOICE_CONFIRM: [
                CallbackQueryHandler(voice_change_cat, pattern=r"^voicecat:"),
                CallbackQueryHandler(voice_confirm, pattern=r"^voice:"),
            ],
        },
        fallbacks=make_fallbacks(),
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("charts", cmd_charts))
    app.add_handler(CommandHandler("limits", cmd_limits))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("recurring", cmd_recurring))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(exp_conv)
    app.add_handler(inc_conv)
    app.add_handler(lim_conv)
    app.add_handler(cat_conv)
    app.add_handler(rec_conv)
    app.add_handler(voice_conv)

    app.add_handler(CallbackQueryHandler(cb_chart, pattern=r"^chart:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))

    app.job_queue.run_daily(
        job_check_recurring,
        time=datetime.strptime("09:00", "%H:%M").time(),
    )

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
