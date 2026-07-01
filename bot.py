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
    ReplyKeyboardRemove,
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
    cur.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
        amount DOUBLE PRECISION NOT NULL, category TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS incomes (
        id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
        amount DOUBLE PRECISION NOT NULL, source TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS limits (
        user_id BIGINT NOT NULL, category TEXT NOT NULL,
        limit_amount DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (user_id, category))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS categories (
        id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
        name TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE (user_id, name))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS recurring (
        id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
        name TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL,
        category TEXT NOT NULL, day_of_month INTEGER NOT NULL,
        last_added DATE)""")
    conn.commit()
    cur.close()
    conn.close()


def q(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall() if cur.description else None
    conn.commit()
    cur.close()
    conn.close()
    return rows


def q1(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


# ── Расходы ──
def add_expense(uid, amount, category):
    q("INSERT INTO expenses (user_id,amount,category,created_at) VALUES(%s,%s,%s,%s)",
      (uid, amount, category, datetime.now()))


def delete_last_expense(uid):
    row = q1("SELECT id,amount,category FROM expenses WHERE user_id=%s ORDER BY id DESC LIMIT 1", (uid,))
    if row:
        q("DELETE FROM expenses WHERE id=%s", (row[0],))
    return row


def get_expenses(uid, since):
    return q("SELECT amount,category,created_at FROM expenses WHERE user_id=%s AND created_at>=%s ORDER BY created_at DESC", (uid, since)) or []


def get_expenses_by_day(uid, since, until):
    return q("SELECT DATE(created_at),SUM(amount) FROM expenses WHERE user_id=%s AND created_at>=%s AND created_at<%s GROUP BY DATE(created_at) ORDER BY DATE(created_at)", (uid, since, until)) or []


def get_expenses_by_cat(uid, since):
    return q("SELECT category,SUM(amount) FROM expenses WHERE user_id=%s AND created_at>=%s GROUP BY category ORDER BY SUM(amount) DESC", (uid, since)) or []


def month_spent(uid, cat):
    since = datetime.combine(date.today().replace(day=1), datetime.min.time())
    row = q1("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=%s AND category=%s AND created_at>=%s", (uid, cat, since))
    return float(row[0]) if row else 0.0


# ── Доходы ──
def add_income(uid, amount, source):
    q("INSERT INTO incomes (user_id,amount,source,created_at) VALUES(%s,%s,%s,%s)",
      (uid, amount, source, datetime.now()))


def get_incomes(uid, since):
    return q("SELECT amount,source FROM incomes WHERE user_id=%s AND created_at>=%s", (uid, since)) or []


def get_balance(uid, since):
    inc = float((q1("SELECT COALESCE(SUM(amount),0) FROM incomes WHERE user_id=%s AND created_at>=%s", (uid, since)) or (0,))[0])
    exp = float((q1("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=%s AND created_at>=%s", (uid, since)) or (0,))[0])
    return inc, exp


# ── Лимиты ──
def set_limit(uid, cat, amount):
    q("INSERT INTO limits(user_id,category,limit_amount) VALUES(%s,%s,%s) ON CONFLICT(user_id,category) DO UPDATE SET limit_amount=EXCLUDED.limit_amount", (uid, cat, amount))


def get_limit(uid, cat):
    row = q1("SELECT limit_amount FROM limits WHERE user_id=%s AND category=%s", (uid, cat))
    return float(row[0]) if row else None


def get_all_limits(uid):
    return q("SELECT category,limit_amount FROM limits WHERE user_id=%s ORDER BY category", (uid,)) or []


def delete_limit(uid, cat):
    q("DELETE FROM limits WHERE user_id=%s AND category=%s", (uid, cat))


# ── Категории ──
DEFAULT_CATS = ["🍔 Еда","🚌 Транспорт","🏠 ЖКХ","🎉 Развлечения","👕 Одежда","💊 Здоровье","📱 Связь/интернет","🛒 Прочее"]


def ensure_cats(uid):
    row = q1("SELECT COUNT(*) FROM categories WHERE user_id=%s", (uid,))
    if row and row[0] == 0:
        for i, name in enumerate(DEFAULT_CATS):
            q("INSERT INTO categories(user_id,name,sort_order) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (uid, name, i))


def get_cats(uid):
    ensure_cats(uid)
    rows = q("SELECT name FROM categories WHERE user_id=%s ORDER BY sort_order,name", (uid,))
    return [r[0] for r in rows] if rows else []


def add_cat(uid, name):
    row = q1("SELECT COALESCE(MAX(sort_order),-1) FROM categories WHERE user_id=%s", (uid,))
    mx = row[0] if row else -1
    try:
        q("INSERT INTO categories(user_id,name,sort_order) VALUES(%s,%s,%s)", (uid, name, mx+1))
        return True
    except Exception:
        return False


def rename_cat(uid, old, new):
    try:
        q("UPDATE categories SET name=%s WHERE user_id=%s AND name=%s", (new, uid, old))
        q("UPDATE expenses SET category=%s WHERE user_id=%s AND category=%s", (new, uid, old))
        q("UPDATE limits SET category=%s WHERE user_id=%s AND category=%s AND NOT EXISTS(SELECT 1 FROM limits WHERE user_id=%s AND category=%s)", (new, uid, old, uid, new))
        return True
    except Exception:
        return False


def delete_cat(uid, name):
    q("DELETE FROM categories WHERE user_id=%s AND name=%s", (uid, name))
    q("DELETE FROM limits WHERE user_id=%s AND category=%s", (uid, name))


# ── Регулярные ──
def add_recurring(uid, name, amount, cat, day):
    q("INSERT INTO recurring(user_id,name,amount,category,day_of_month,last_added) VALUES(%s,%s,%s,%s,%s,NULL)", (uid, name, amount, cat, day))


def get_recurring(uid):
    return q("SELECT id,name,amount,category,day_of_month,last_added FROM recurring WHERE user_id=%s ORDER BY day_of_month,name", (uid,)) or []


def get_all_recurring():
    return q("SELECT id,user_id,name,amount,category,day_of_month,last_added FROM recurring") or []


def delete_recurring(rec_id, uid):
    q("DELETE FROM recurring WHERE id=%s AND user_id=%s", (rec_id, uid))


def mark_recurring(rec_id, d):
    q("UPDATE recurring SET last_added=%s WHERE id=%s", (d, rec_id))


# ──────────────────────────────────────────────
# СОСТОЯНИЯ ДИАЛОГОВ
# ──────────────────────────────────────────────
(
    EXP_AMOUNT, EXP_CATEGORY, EXP_CUSTOM_CAT,
    INC_AMOUNT, INC_SOURCE, INC_CUSTOM_SRC,
    LIM_CAT, LIM_AMOUNT,
    CAT_NEW, CAT_RENAME,
    REC_NAME, REC_AMOUNT, REC_CAT, REC_DAY,
    VOICE_CONFIRM,
    UNDO_CONFIRM,
) = range(16)

INCOME_SOURCES = ["💼 Зарплата","🎁 Подарок","📈 Инвестиции","🏠 Аренда","💻 Фриланс","🏦 Кешбэк","🛍 Продажа","💰 Прочее"]
CHART_COLORS = ["#FF6B6B","#4ECDC4","#45B7D1","#96CEB4","#FFEAA7","#DDA0DD","#98D8C8","#F7DC6F","#BB8FCE","#85C1E9"]

# ──────────────────────────────────────────────
# INLINE-КЛАВИАТУРЫ
# ──────────────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить расход", callback_data="menu:add_exp"),
         InlineKeyboardButton("💵 Добавить доход",  callback_data="menu:add_inc")],
        [InlineKeyboardButton("📊 За сегодня",  callback_data="menu:today"),
         InlineKeyboardButton("📅 За месяц",    callback_data="menu:month")],
        [InlineKeyboardButton("💰 Баланс",  callback_data="menu:balance"),
         InlineKeyboardButton("📈 Графики", callback_data="menu:charts")],
        [InlineKeyboardButton("🎯 Лимиты",     callback_data="menu:limits"),
         InlineKeyboardButton("⚙️ Категории", callback_data="menu:cats")],
        [InlineKeyboardButton("🔄 Регулярные платежи", callback_data="menu:recurring")],
        [InlineKeyboardButton("❌ Удалить последнюю запись", callback_data="menu:undo")],
    ])


def back_to_menu_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]])


def cats_inline_kb(uid, prefix, extra=None, back=True):
    cats = get_cats(uid)
    buttons, row = [], []
    for i, cat in enumerate(cats, 1):
        row.append(InlineKeyboardButton(cat, callback_data=f"{prefix}:{cat}"))
        if i % 2 == 0:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    if extra:
        buttons.append([extra])
    if back:
        buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


def sources_kb(prefix):
    buttons, row = [], []
    for i, src in enumerate(INCOME_SOURCES, 1):
        row.append(InlineKeyboardButton(src, callback_data=f"{prefix}:{src}"))
        if i % 2 == 0:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✏️ Свой источник", callback_data=f"{prefix}:__custom__")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


# ──────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ
# ──────────────────────────────────────────────
async def show_menu(update: Update, text="Главное меню:", edit=False):
    kb = main_menu_kb()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(text, reply_markup=kb)


async def warn_limit(uid, cat, send_fn):
    lim = get_limit(uid, cat)
    if not lim:
        return
    spent = month_spent(uid, cat)
    ratio = spent / lim
    if spent > lim:
        await send_fn(f"⚠️ Превышен лимит «{cat}»! {spent:.2f} из {lim:.2f}")
    elif ratio >= 0.8:
        await send_fn(f"🔔 {spent:.2f} из {lim:.2f} по «{cat}» ({ratio*100:.0f}%)")


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
    _, _, autotexts = ax.pie(
        values, labels=None, autopct=lambda p: f"{p:.1f}%" if p > 3 else "",
        colors=CHART_COLORS[:len(values)], startangle=140,
        wedgeprops={"edgecolor": "#1e1e2e", "linewidth": 1.5}, pctdistance=0.75,
    )
    for at in autotexts:
        at.set_color("white"); at.set_fontsize(9)
    patches = [mpatches.Patch(color=CHART_COLORS[i % len(CHART_COLORS)],
               label=f"{labels[i]}  —  {values[i]:.0f}") for i in range(len(labels))]
    ax.legend(handles=patches, loc="center left", bbox_to_anchor=(1, 0.5),
              fontsize=9, facecolor="#2e2e3e", labelcolor="white", edgecolor="#555")
    ax.set_title(f"{title}\nВсего: {sum(values):.0f}", color="white", fontsize=12, pad=15)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#1e1e2e")
    plt.close(fig); buf.seek(0)
    return buf


def make_bar(rows, title):
    if not rows:
        return None
    days = [str(r[0]) for r in rows]
    values = [float(r[1]) for r in rows]
    labels = [f"{d.split('-')[2]}.{d.split('-')[1]}" for d in days]
    fig, ax = plt.subplots(figsize=(max(7, len(days)*0.5+2), 4.5), facecolor="#1e1e2e")
    ax.set_facecolor("#2e2e3e")
    bars = ax.bar(range(len(values)), values, color="#4ECDC4", edgecolor="#1e1e2e")
    for bar, val in zip(bars, values):
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+max(values)*0.01,
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
    plt.close(fig); buf.seek(0)
    return buf


# ──────────────────────────────────────────────
# ГЛАВНЫЙ ОБРАБОТЧИК МЕНЮ
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_cats(update.effective_user.id)
    text = (
        "👋 Привет! Я бюджетный трекер.\n\n"
        "Выбери действие в меню ниже:"
    )
    await show_menu(update, text)
    # Убираем нижнюю клавиатуру если была
    if update.message:
        await update.message.reply_text(
            "Нижняя клавиатура скрыта — всё управление через кнопки выше.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диспетчер нажатий кнопок главного меню."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    uid = query.from_user.id

    if action == "main":
        await query.edit_message_text("Главное меню:", reply_markup=main_menu_kb())
        return ConversationHandler.END

    # ── Статистика ──
    if action == "today":
        since = datetime.combine(date.today(), datetime.min.time())
        rows = get_expenses(uid, since)
        text = fmt_summary(rows, "📊 Расходы за сегодня")
        await query.edit_message_text(text, reply_markup=back_to_menu_kb())

    elif action == "month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        rows = get_expenses(uid, since)
        text = fmt_summary(rows, "📅 Расходы за месяц")
        await query.edit_message_text(text, reply_markup=back_to_menu_kb())

    # ── Баланс ──
    elif action == "balance":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        inc_total, exp_total = get_balance(uid, since)
        inc_rows = get_incomes(uid, since)
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
            lines.append("  Доходов пока нет — добавь через «💵 Добавить доход»")
        lines.append(f"\n📤 Расходы: {exp_total:.2f}")
        remaining = inc_total - exp_total
        lines.append(f"\n{'✅' if remaining >= 0 else '🔴'} Остаток: {remaining:.2f}")
        await query.edit_message_text("\n".join(lines), reply_markup=back_to_menu_kb())

    # ── Графики ──
    elif action == "charts":
        await query.edit_message_text(
            "📈 Выбери тип графика:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🥧 По категориям (месяц)", callback_data="chart:pie_month")],
                [InlineKeyboardButton("📊 По дням (месяц)",       callback_data="chart:bar_month")],
                [InlineKeyboardButton("📊 По дням (7 дней)",      callback_data="chart:bar_week")],
                [InlineKeyboardButton("🏠 Главное меню",          callback_data="menu:main")],
            ]),
        )

    # ── Лимиты ──
    elif action == "limits":
        rows = get_all_limits(uid)
        if not rows:
            text = "🎯 Лимиты пока не установлены."
        else:
            lines = ["🎯 Лимиты на этот месяц:\n"]
            for cat, lim in rows:
                spent = month_spent(uid, cat)
                ratio = spent / lim if lim > 0 else 0
                mark = "🔴" if spent > lim else ("🟡" if ratio >= 0.8 else "🟢")
                lines.append(f"{mark} {cat}: {spent:.2f} / {lim:.2f}")
            text = "\n".join(lines)
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Установить лимит",   callback_data="lim:set")],
                [InlineKeyboardButton("🗑 Удалить лимит",      callback_data="lim:delete")],
                [InlineKeyboardButton("🏠 Главное меню",       callback_data="menu:main")],
            ]),
        )

    # ── Категории ──
    elif action == "cats":
        await _show_cats_menu(query, uid)

    # ── Регулярные ──
    elif action == "recurring":
        await _show_recurring_menu(query, uid)

    # ── Удалить последнюю ──
    elif action == "undo":
        row = q1("SELECT amount,category FROM expenses WHERE user_id=%s ORDER BY id DESC LIMIT 1", (uid,))
        if not row:
            await query.edit_message_text(
                "Записей пока нет.",
                reply_markup=back_to_menu_kb(),
            )
        else:
            await query.edit_message_text(
                f"Удалить последнюю запись?\n\n• {row[0]:.2f} — {row[1]}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Да, удалить", callback_data="undo:confirm"),
                     InlineKeyboardButton("❌ Отмена",      callback_data="menu:main")],
                ]),
            )

    # ── Диалоги (расход/доход/лимит) — передаём управление ConversationHandler ──
    elif action in ("add_exp", "add_inc"):
        pass  # обрабатывается ниже в ConversationHandler


async def _show_cats_menu(query, uid):
    cats = get_cats(uid)
    buttons = []
    for cat in cats:
        buttons.append([
            InlineKeyboardButton(cat, callback_data="catnoop"),
            InlineKeyboardButton("✏️", callback_data=f"catedit:{cat}"),
            InlineKeyboardButton("🗑", callback_data=f"catdel:{cat}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Новая категория", callback_data="catadd:new")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню",    callback_data="menu:main")])
    await query.edit_message_text(
        "⚙️ Категории:\n✏️ — переименовать,  🗑 — удалить",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_recurring_menu(query, uid):
    rows = get_recurring(uid)
    import calendar
    days_in_month = calendar.monthrange(date.today().year, date.today().month)[1]
    today_day = date.today().day
    if not rows:
        text = "🔄 Регулярных платежей пока нет."
    else:
        lines = ["🔄 Регулярные платежи:\n"]
        for _, name, amount, cat, day, _ in rows:
            dl = day - today_day
            if dl < 0:
                dl = days_in_month - today_day + day
            mark = "🔴 Сегодня!" if dl == 0 else (f"🟡 через {dl} дн." if dl <= 3 else f"🟢 {day}-го")
            lines.append(f"{mark}  {name} — {amount:.0f} ({cat})")
        text = "\n".join(lines)
    buttons = []
    for rec_id, name, *_ in rows:
        buttons.append([InlineKeyboardButton(f"🗑 {name}", callback_data=f"recdel:{rec_id}")])
    buttons.append([InlineKeyboardButton("➕ Добавить шаблон", callback_data="recadd:new")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню",    callback_data="menu:main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


# ──────────────────────────────────────────────
# ДИАЛОГ: РАСХОД
# ──────────────────────────────────────────────
async def exp_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ Добавить расход\n\nВведи сумму:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Отмена", callback_data="exp:cancel")
        ]]),
    )
    return EXP_AMOUNT


async def exp_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи положительное число, например: 350")
        return EXP_AMOUNT
    context.user_data["exp_amount"] = amount
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\n\nВыбери категорию:",
        reply_markup=cats_inline_kb(uid, "expcat",
            extra=InlineKeyboardButton("✏️ Своя категория", callback_data="expcat:__custom__")),
    )
    return EXP_CATEGORY


async def exp_got_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__" or query.data == "exp:cancel":
        await query.edit_message_text("Отменено.", reply_markup=back_to_menu_kb())
        context.user_data.pop("exp_amount", None)
        return ConversationHandler.END
    if choice == "__custom__":
        await query.edit_message_text("Введи название категории:")
        return EXP_CUSTOM_CAT
    amount = context.user_data.pop("exp_amount", None)
    if not amount:
        await query.edit_message_text("Что-то пошло не так.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    uid = query.from_user.id
    add_expense(uid, amount, choice)
    await query.edit_message_text(
        f"✅ Добавлено: {amount:.2f} — {choice}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ещё расход", callback_data="menu:add_exp"),
             InlineKeyboardButton("🏠 Меню",       callback_data="menu:main")],
        ]),
    )
    lim = get_limit(uid, choice)
    if lim:
        spent = month_spent(uid, choice)
        ratio = spent / lim
        warn = ""
        if spent > lim:
            warn = f"\n\n⚠️ Превышен лимит «{choice}»!\n{spent:.2f} из {lim:.2f}"
        elif ratio >= 0.8:
            warn = f"\n\n🔔 {spent:.2f} из {lim:.2f} по «{choice}» ({ratio*100:.0f}%)"
        if warn:
            await context.bot.send_message(chat_id=query.message.chat_id, text=warn)
    return ConversationHandler.END


async def exp_got_custom_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    amount = context.user_data.pop("exp_amount", None)
    if not cat or not amount:
        await update.message.reply_text("Что-то пошло не так. Начни заново.", reply_markup=back_to_menu_kb() if not amount else None)
        return ConversationHandler.END
    uid = update.effective_user.id
    add_expense(uid, amount, cat)
    await update.message.reply_text(
        f"✅ Добавлено: {amount:.2f} — {cat}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ещё расход", callback_data="menu:add_exp"),
             InlineKeyboardButton("🏠 Меню",       callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: ДОХОД
# ──────────────────────────────────────────────
async def inc_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💵 Добавить доход\n\nВведи сумму:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Отмена", callback_data="inc:cancel")
        ]]),
    )
    return INC_AMOUNT


async def inc_got_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи положительное число, например: 50000")
        return INC_AMOUNT
    context.user_data["inc_amount"] = amount
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\n\nВыбери источник дохода:",
        reply_markup=sources_kb("incsrc"),
    )
    return INC_SOURCE


async def inc_got_src(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__" or query.data == "inc:cancel":
        context.user_data.pop("inc_amount", None)
        await query.edit_message_text("Отменено.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    if choice == "__custom__":
        await query.edit_message_text("Введи название источника дохода:")
        return INC_CUSTOM_SRC
    amount = context.user_data.pop("inc_amount", None)
    if not amount:
        await query.edit_message_text("Что-то пошло не так.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    add_income(query.from_user.id, amount, choice)
    await query.edit_message_text(
        f"✅ Доход записан: +{amount:.2f} — {choice}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💵 Ещё доход", callback_data="menu:add_inc"),
             InlineKeyboardButton("🏠 Меню",      callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


async def inc_got_custom_src(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = update.message.text.strip()
    amount = context.user_data.pop("inc_amount", None)
    if not source or not amount:
        await update.message.reply_text("Что-то пошло не так.")
        return ConversationHandler.END
    add_income(update.effective_user.id, amount, source)
    await update.message.reply_text(
        f"✅ Доход записан: +{amount:.2f} — {source}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💵 Ещё доход", callback_data="menu:add_inc"),
             InlineKeyboardButton("🏠 Меню",      callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: ЛИМИТЫ
# ──────────────────────────────────────────────
async def lim_set_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await query.edit_message_text(
        "Выбери категорию для лимита:",
        reply_markup=cats_inline_kb(uid, "limcat"),
    )
    return LIM_CAT


async def lim_got_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__":
        await query.edit_message_text("Отменено.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    context.user_data["lim_cat"] = choice
    current = get_limit(query.from_user.id, choice)
    current_txt = f" (сейчас: {current:.2f})" if current else ""
    await query.edit_message_text(
        f"Категория: {choice}{current_txt}\n\nВведи новую сумму лимита на месяц:"
    )
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
        await update.message.reply_text("Что-то пошло не так.")
        return ConversationHandler.END
    set_limit(update.effective_user.id, cat, amount)
    await update.message.reply_text(
        f"✅ Лимит установлен: {cat} — {amount:.2f}/мес.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 К лимитам", callback_data="menu:limits"),
             InlineKeyboardButton("🏠 Меню",      callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


async def lim_delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    rows = get_all_limits(uid)
    if not rows:
        await query.edit_message_text("Лимитов нет.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(f"🗑 {cat} ({lim:.0f})", callback_data=f"limdel:{cat}")]
               for cat, lim in rows]
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    await query.edit_message_text("Выбери лимит для удаления:", reply_markup=InlineKeyboardMarkup(buttons))
    return ConversationHandler.END


async def lim_do_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.split(":", 1)[1]
    delete_limit(query.from_user.id, cat)
    await query.edit_message_text(
        f"✅ Лимит по «{cat}» удалён.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 К лимитам", callback_data="menu:limits"),
             InlineKeyboardButton("🏠 Меню",      callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: КАТЕГОРИИ
# ──────────────────────────────────────────────
async def cat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "catnoop":
        return None
    if data == "catmgr:close":
        await query.edit_message_text("Закрыто.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    if data == "catadd:new":
        await query.edit_message_text("Введи название новой категории (можно с эмодзи):")
        return CAT_NEW
    if data.startswith("catedit:"):
        old = data.split(":", 1)[1]
        context.user_data["cat_old"] = old
        await query.edit_message_text(f"Новое название для «{old}»:")
        return CAT_RENAME
    if data.startswith("catdel:"):
        name = data.split(":", 1)[1]
        delete_cat(uid, name)
        await _show_cats_menu(query, uid)
        return ConversationHandler.END
    return ConversationHandler.END


async def cat_got_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    uid = update.effective_user.id
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return CAT_NEW
    ok = add_cat(uid, name)
    if not ok:
        await update.message.reply_text("Такая категория уже есть. Введи другое название:")
        return CAT_NEW
    await update.message.reply_text(
        f"✅ Категория «{name}» добавлена.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ К категориям", callback_data="menu:cats"),
             InlineKeyboardButton("🏠 Меню",         callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


async def cat_got_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    old = context.user_data.pop("cat_old", None)
    if not old or not new_name:
        await update.message.reply_text("Что-то пошло не так.")
        return ConversationHandler.END
    ok = rename_cat(update.effective_user.id, old, new_name)
    text = f"✅ «{old}» → «{new_name}»" if ok else "Не удалось. Возможно, такое имя уже есть."
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ К категориям", callback_data="menu:cats"),
             InlineKeyboardButton("🏠 Меню",         callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: РЕГУЛЯРНЫЕ
# ──────────────────────────────────────────────
async def rec_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    if data == "recmgr:close":
        await query.edit_message_text("Закрыто.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    if data == "recadd:new":
        await query.edit_message_text("Введи название платежа (например: Netflix, Аренда):")
        return REC_NAME
    if data.startswith("recdel:"):
        rec_id = int(data.split(":")[1])
        delete_recurring(rec_id, uid)
        await _show_recurring_menu(query, uid)
        return ConversationHandler.END
    return ConversationHandler.END


async def rec_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Название не может быть пустым:")
        return REC_NAME
    context.user_data["rec_name"] = name
    await update.message.reply_text(f"Название: {name}\n\nВведи сумму платежа:")
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
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Сумма: {amount:.2f}\n\nВыбери категорию:",
        reply_markup=cats_inline_kb(uid, "reccat"),
    )
    return REC_CAT


async def rec_got_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__":
        context.user_data.pop("rec_name", None)
        context.user_data.pop("rec_amount", None)
        await query.edit_message_text("Отменено.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    context.user_data["rec_cat"] = choice
    await query.edit_message_text(f"Категория: {choice}\n\nВведи день месяца (от 1 до 28):")
    return REC_DAY


async def rec_got_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        day = int(text)
        if not (1 <= day <= 28):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число от 1 до 28:")
        return REC_DAY
    name = context.user_data.pop("rec_name", None)
    amount = context.user_data.pop("rec_amount", None)
    cat = context.user_data.pop("rec_cat", None)
    if not all([name, amount, cat]):
        await update.message.reply_text("Что-то пошло не так. Начни заново.")
        return ConversationHandler.END
    add_recurring(update.effective_user.id, name, amount, cat, day)
    await update.message.reply_text(
        f"✅ Шаблон создан:\n• {name}\n• {amount:.2f}\n• {cat}\n• Каждый месяц {day}-го числа\n\n"
        f"Бот автоматически добавит расход и пришлёт напоминание за 3 дня.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Регулярные", callback_data="menu:recurring"),
             InlineKeyboardButton("🏠 Меню",       callback_data="menu:main")],
        ]),
    )
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ДИАЛОГ: УДАЛИТЬ ПОСЛЕДНЮЮ (подтверждение)
# ──────────────────────────────────────────────
async def undo_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    row = delete_last_expense(query.from_user.id)
    if row:
        await query.edit_message_text(
            f"✅ Удалено: {row[1]:.2f} — {row[2]}",
            reply_markup=back_to_menu_kb(),
        )
    else:
        await query.edit_message_text("Записей нет.", reply_markup=back_to_menu_kb())
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ГРАФИКИ
# ──────────────────────────────────────────────
async def cb_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    uid = query.from_user.id
    chat_id = query.message.chat_id

    if action == "pie_month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        rows = get_expenses_by_cat(uid, since)
        if not rows:
            await query.edit_message_text("За этот месяц расходов нет.", reply_markup=back_to_menu_kb())
            return
        await query.edit_message_text("⏳ Строю график...")
        buf = make_pie(rows, f"Расходы по категориям\n{date.today().strftime('%B %Y')}")
        await context.bot.send_photo(chat_id=chat_id, photo=buf,
            reply_markup=back_to_menu_kb())

    elif action == "bar_month":
        since = datetime.combine(date.today().replace(day=1), datetime.min.time())
        rows = get_expenses_by_day(uid, since, datetime.now())
        if not rows:
            await query.edit_message_text("За этот месяц расходов нет.", reply_markup=back_to_menu_kb())
            return
        await query.edit_message_text("⏳ Строю график...")
        buf = make_bar(rows, f"Траты по дням — {date.today().strftime('%B %Y')}")
        await context.bot.send_photo(chat_id=chat_id, photo=buf, reply_markup=back_to_menu_kb())

    elif action == "bar_week":
        since = datetime.combine(date.today() - timedelta(days=6), datetime.min.time())
        rows = get_expenses_by_day(uid, since, datetime.now())
        if not rows:
            await query.edit_message_text("За 7 дней расходов нет.", reply_markup=back_to_menu_kb())
            return
        await query.edit_message_text("⏳ Строю график...")
        buf = make_bar(rows, "Траты за последние 7 дней")
        await context.bot.send_photo(chat_id=chat_id, photo=buf, reply_markup=back_to_menu_kb())


# ──────────────────────────────────────────────
# ГОЛОСОВОЙ ВВОД
# ──────────────────────────────────────────────
async def transcribe_voice(file_bytes: bytes) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": ("voice.ogg", file_bytes, "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "ru"},
        )
        response.raise_for_status()
        return response.json().get("text", "").strip()


def parse_expense(text, cats):
    text_lower = text.lower()
    amounts = re.findall(r"\b(\d+(?:[.,]\d+)?)\b", text)
    if not amounts:
        return None, None
    amount = float(amounts[0].replace(",", "."))
    matched = None
    for cat in cats:
        cat_name = re.sub(r"[^\w\s]", "", cat).strip().lower()
        if cat_name and cat_name in text_lower:
            matched = cat
            break
    if not matched:
        for pattern in [r"на\s+(\w+)", r"в\s+(\w+)", r"на\s+(\w+\s+\w+)"]:
            m = re.search(pattern, text_lower)
            if m:
                matched = m.group(1).capitalize()
                break
    return amount, matched


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "Голосовой ввод не настроен. Добавь OPENAI_API_KEY в Railway Variables.",
            reply_markup=back_to_menu_kb(),
        )
        return ConversationHandler.END

    msg = await update.message.reply_text("🎙 Распознаю речь...")
    try:
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        text = await transcribe_voice(bytes(file_bytes))
        if not text:
            await msg.edit_text("Не удалось распознать речь.", reply_markup=back_to_menu_kb())
            return ConversationHandler.END

        uid = update.effective_user.id
        cats = get_cats(uid)
        amount, cat = parse_expense(text, cats)

        if amount is None:
            await msg.edit_text(
                f"🎙 «{text}»\n\nНе нашёл сумму. Скажи например: «потратил 350 на кафе»",
                reply_markup=back_to_menu_kb(),
            )
            return ConversationHandler.END

        context.user_data["voice_amount"] = amount
        context.user_data["voice_cat"] = cat or "🛒 Прочее"
        cat_display = cat if cat else "🛒 Прочее (не распознана)"

        await msg.edit_text(
            f"🎙 Распознано: «{text}»\n\n"
            f"Добавить расход?\n• Сумма: {amount:.2f}\n• Категория: {cat_display}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить",         callback_data="voice:confirm"),
                 InlineKeyboardButton("✏️ Изменить категорию", callback_data="voice:change_cat")],
                [InlineKeyboardButton("❌ Отмена",              callback_data="voice:cancel")],
            ]),
        )
        return VOICE_CONFIRM

    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}", reply_markup=back_to_menu_kb())
        return ConversationHandler.END


async def voice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        context.user_data.pop("voice_amount", None)
        context.user_data.pop("voice_cat", None)
        await query.edit_message_text("Отменено.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END

    if action == "change_cat":
        uid = query.from_user.id
        await query.edit_message_text(
            "Выбери категорию:",
            reply_markup=cats_inline_kb(uid, "voicecat"),
        )
        return VOICE_CONFIRM

    if action == "confirm":
        amount = context.user_data.pop("voice_amount", None)
        cat = context.user_data.pop("voice_cat", None)
        if not amount:
            await query.edit_message_text("Что-то пошло не так.", reply_markup=back_to_menu_kb())
            return ConversationHandler.END
        add_expense(query.from_user.id, amount, cat)
        await query.edit_message_text(
            f"✅ Добавлено: {amount:.2f} — {cat}",
            reply_markup=back_to_menu_kb(),
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def voice_cat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    if choice == "__cancel__":
        context.user_data.pop("voice_amount", None)
        context.user_data.pop("voice_cat", None)
        await query.edit_message_text("Отменено.", reply_markup=back_to_menu_kb())
        return ConversationHandler.END
    context.user_data["voice_cat"] = choice
    amount = context.user_data.get("voice_amount", 0)
    await query.edit_message_text(
        f"Добавить расход?\n• Сумма: {amount:.2f}\n• Категория: {choice}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="voice:confirm"),
             InlineKeyboardButton("❌ Отмена",      callback_data="voice:cancel")],
        ]),
    )
    return VOICE_CONFIRM


# ──────────────────────────────────────────────
# БЫСТРЫЕ КОМАНДЫ
# ──────────────────────────────────────────────
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /add 350 кафе\nИли открой меню:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Добавить расход", callback_data="menu:add_exp")
            ]]),
        )
        return ConversationHandler.END
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return ConversationHandler.END
    cat = " ".join(args[1:])
    uid = update.effective_user.id
    add_expense(uid, amount, cat)
    await update.message.reply_text(
        f"✅ Добавлено: {amount:.2f} — {cat}",
        reply_markup=back_to_menu_kb(),
    )
    return ConversationHandler.END


async def cmd_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /income 50000 зарплата\nИли открой меню:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💵 Добавить доход", callback_data="menu:add_inc")
            ]]),
        )
        return ConversationHandler.END
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return ConversationHandler.END
    source = " ".join(args[1:])
    add_income(update.effective_user.id, amount, source)
    await update.message.reply_text(
        f"✅ Доход: +{amount:.2f} — {source}",
        reply_markup=back_to_menu_kb(),
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=back_to_menu_kb())
    return ConversationHandler.END


# ──────────────────────────────────────────────
# ПЛАНИРОВЩИК
# ──────────────────────────────────────────────
async def job_recurring(context):
    today = date.today()
    import calendar
    for rec_id, uid, name, amount, cat, day, last_added in get_all_recurring():
        if today.day == day:
            if last_added is None or (last_added.year, last_added.month) != (today.year, today.month):
                add_expense(uid, amount, cat)
                mark_recurring(rec_id, today)
                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=f"🔄 Автоплатёж: {name} — {amount:.2f} ({cat})",
                        reply_markup=back_to_menu_kb(),
                    )
                except Exception:
                    pass
        elif today.day == day - 3:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"🔔 Через 3 дня ({day}-го) платёж «{name}» — {amount:.2f} ({cat})",
                    reply_markup=back_to_menu_kb(),
                )
            except Exception:
                pass


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    MENU_BACK = [
        CallbackQueryHandler(lambda u, c: (c.user_data.clear(), show_menu(u, edit=True)) and ConversationHandler.END, pattern=r"^menu:main$"),
        CommandHandler("cancel", cmd_cancel),
        CommandHandler("start",  cmd_start),
    ]

    # Диалог: расход
    exp_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(exp_entry, pattern=r"^menu:add_exp$")],
        states={
            EXP_AMOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_got_amount)],
            EXP_CATEGORY:   [CallbackQueryHandler(exp_got_cat, pattern=r"^(expcat:|exp:cancel)")],
            EXP_CUSTOM_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_got_custom_cat)],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Диалог: доход
    inc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(inc_entry, pattern=r"^menu:add_inc$")],
        states={
            INC_AMOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_got_amount)],
            INC_SOURCE:     [CallbackQueryHandler(inc_got_src, pattern=r"^(incsrc:|inc:cancel)")],
            INC_CUSTOM_SRC: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_got_custom_src)],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Диалог: лимит установить
    lim_set_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lim_set_entry, pattern=r"^lim:set$")],
        states={
            LIM_CAT:    [CallbackQueryHandler(lim_got_cat, pattern=r"^limcat:")],
            LIM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lim_got_amount)],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Диалог: лимит удалить
    lim_del_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lim_delete_entry, pattern=r"^lim:delete$")],
        states={
            LIM_CAT: [CallbackQueryHandler(lim_do_delete, pattern=r"^limdel:")],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Диалог: категории
    cat_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cat_cb, pattern=r"^(catadd:|catedit:|catdel:|catnoop)")],
        states={
            CAT_NEW:    [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_got_new)],
            CAT_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cat_got_rename)],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Диалог: регулярные
    rec_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(rec_cb, pattern=r"^(recadd:|recdel:|recmgr:)")],
        states={
            REC_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_got_name)],
            REC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_got_amount)],
            REC_CAT:    [CallbackQueryHandler(rec_got_cat, pattern=r"^reccat:")],
            REC_DAY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rec_got_day)],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Диалог: голос
    voice_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.VOICE, handle_voice)],
        states={
            VOICE_CONFIRM: [
                CallbackQueryHandler(voice_cat_cb, pattern=r"^voicecat:"),
                CallbackQueryHandler(voice_cb,     pattern=r"^voice:"),
            ],
        },
        fallbacks=MENU_BACK, per_message=False, allow_reentry=True,
    )

    # Команды
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_start))
    app.add_handler(CommandHandler("add",      cmd_add))
    app.add_handler(CommandHandler("income",   cmd_income))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))

    # Диалоги
    app.add_handler(exp_conv)
    app.add_handler(inc_conv)
    app.add_handler(lim_set_conv)
    app.add_handler(lim_del_conv)
    app.add_handler(cat_conv)
    app.add_handler(rec_conv)
    app.add_handler(voice_conv)

    # Обработчики без диалога
    app.add_handler(CallbackQueryHandler(cb_chart,    pattern=r"^chart:"))
    app.add_handler(CallbackQueryHandler(undo_confirm, pattern=r"^undo:confirm$"))
    app.add_handler(CallbackQueryHandler(cb_menu,     pattern=r"^menu:"))

    # Планировщик
    app.job_queue.run_daily(
        job_recurring,
        time=datetime.strptime("09:00", "%H:%M").time(),
    )

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
