import asyncio
import html
import json
import logging
import os
import random
import time
from typing import Any
from urllib.parse import quote

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ═══════════════════════ КОНФИГ (только это правишь в коде) ═══════════════════════
BOT_TOKEN = "8720749233:AAETdVr2eXh7HCQpF6BUJRQTXxdNLFO1Hhs"
ADMIN_IDS = {8709632020}  # id админов, можно несколько: {111, 222}
# ═════════════════════════════════════════════════════════════════════════════════

DB_PATH = "shop.db"

# Готовые категории одежды
CATEGORIES = [
    ("футболки", "👕 Футболки"),
    ("штаны", "👖 Штаны"),
    ("шорты", "🩳 Шорты"),
    ("толстовки", "🧥 Толстовки"),
    ("худи", "🧢 Худи"),
    ("кепки", "🎩 Кепки"),
    ("куртки", "🧥 Куртки"),
    ("обувь", "👟 Обувь"),
    ("носки", "🧦 Носки"),
    ("сумки", "🎒 Сумки"),
    ("аксессуары", "⌚ Аксессуары"),
    ("другое", "📦 Другое"),
]
CAT_LABEL = {k: v for k, v in CATEGORIES}

PRESET_SIZES = [
    "XS", "S", "M", "L", "XL", "XXL", "3XL",
    "36", "37", "38", "39", "40", "41", "42", "43", "44", "45", "46",
]
DELIVERY_PRESETS = ["3–5 дней", "7–10 дней", "10–14 дней", "14–21 день", "21–30 дней"]
STATUS = {
    "new": "⏳ ждёт оплату",
    "paid": "✅ оплачен",
    "shipped": "📦 отправлен",
    "canceled": "❌ отменён",
}
DEFAULT_SETTINGS = {
    "shop_name": "Shop",
    "kaspi_phone": "+7 000 000 00 00",
    "kaspi_name": "",
    "support": "",
}

esc = html.escape

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS brands(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id INTEGER NOT NULL,
                category TEXT DEFAULT 'другое',
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price INTEGER NOT NULL,
                sizes TEXT DEFAULT '',
                photos TEXT DEFAULT '[]',
                delivery TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                created INTEGER
            );
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                user_id INTEGER,
                user_name TEXT,
                username TEXT,
                product_id INTEGER,
                title TEXT,
                size TEXT,
                price INTEGER,
                delivery TEXT,
                address TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                created INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        for col, default in (
            ("address", "''"),
            ("category", "'другое'"),
        ):
            try:
                table = "orders" if col == "address" else "products"
                await db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT {default}"
                )
            except Exception:
                pass
        for key, value in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()


async def db_exec(sql: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid or 0


async def db_all(sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def db_one(sql: str, params: tuple = ()) -> dict | None:
    rows = await db_all(sql, params)
    return rows[0] if rows else None


async def get_setting(key: str) -> str:
    row = await db_one("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        return row["value"]
    return DEFAULT_SETTINGS.get(key, "")


async def set_setting(key: str, value: str) -> None:
    await db_exec(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def get_all_settings() -> dict[str, str]:
    rows = await db_all("SELECT key, value FROM settings")
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


PRODUCT_SQL = (
    "SELECT p.*, b.name AS brand FROM products p "
    "JOIN brands b ON b.id=p.brand_id"
)


async def get_product(pid: int) -> dict | None:
    return await db_one(PRODUCT_SQL + " WHERE p.id=?", (pid,))


def money(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₸"


def split_sizes(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def parse_price(text: str) -> int | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def gis_link(address: str) -> str:
    q = quote(f"Караганда, {address}")
    return f"https://2gis.kz/karaganda/search/{q}"


def cat_label(key: str | None) -> str:
    if not key:
        return CAT_LABEL["другое"]
    return CAT_LABEL.get(key, key)


# ───────────────────────── ВСПОМОГАТЕЛЬНОЕ ─────────────────────────
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _safe_delete(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest:
        pass
    except Exception:
        pass


async def present(
    msg: Message,
    text: str,
    kb: Any = None,
    photo: str | None = None,
    edit: bool = True,
    state: FSMContext | None = None,
) -> Message | None:
    """Показать экран. Удаляет старое сообщение бота, чтобы не копить дубли."""
    bot = msg.bot
    chat_id = msg.chat.id

    # удалить ранее сохранённое сообщение бота
    if state is not None:
        d = await state.get_data()
        old_id = d.get("bot_msg_id")
        if old_id and (not edit or old_id != msg.message_id):
            await _safe_delete(bot, chat_id, old_id)

    if edit:
        try:
            if photo and msg.photo:
                await msg.edit_media(
                    InputMediaPhoto(media=photo, caption=text),
                    reply_markup=kb,
                )
                if state is not None:
                    await state.update_data(bot_msg_id=msg.message_id)
                return msg
            if not photo and not msg.photo:
                await msg.edit_text(text, reply_markup=kb)
                if state is not None:
                    await state.update_data(bot_msg_id=msg.message_id)
                return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return msg
        # не получилось отредактировать — удаляем текущее и шлём новое
        await _safe_delete(bot, chat_id, msg.message_id)
    else:
        # не edit: удаляем сообщение-источник (callback), если это сообщение бота
        if msg.from_user and msg.from_user.is_bot:
            await _safe_delete(bot, chat_id, msg.message_id)

    if photo:
        sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=kb)

    if state is not None:
        await state.update_data(bot_msg_id=sent.message_id)
    return sent


async def send_clean(
    msg: Message,
    text: str,
    kb: Any = None,
    state: FSMContext | None = None,
    photo: str | None = None,
) -> Message:
    """Отправить новое сообщение и удалить предыдущее бота + (опционально) сообщение юзера."""
    bot = msg.bot
    chat_id = msg.chat.id
    if state is not None:
        d = await state.get_data()
        await _safe_delete(bot, chat_id, d.get("bot_msg_id"))
    # удаляем сообщение пользователя (текст/фото), чтобы чат не захламлялся
    if not (msg.from_user and msg.from_user.is_bot):
        await _safe_delete(bot, chat_id, msg.message_id)

    if photo:
        sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=kb)
    if state is not None:
        await state.update_data(bot_msg_id=sent.message_id)
    return sent


async def main_menu_kb(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🛍 Каталог", callback_data="cat")
    b.button(text="🔍 Поиск", callback_data="search")
    b.button(text="📦 Мои заказы", callback_data="my")
    b.button(text="💬 Связаться", callback_data="contact")
    b.adjust(2)
    if user_id in ADMIN_IDS:
        b.row(btn("⚙️ Админ-панель", "adm"))
    return b.as_markup()


async def menu_text() -> str:
    name = await get_setting("shop_name")
    return f"👋 Добро пожаловать в <b>{esc(name)}</b>!\nВыбери, что хочешь сделать 👇"


def card_text(p: dict) -> str:
    brand = (p.get("brand") or "").strip()
    if brand and brand not in ("—", "-", "."):
        title = f"<b>{esc(brand)}</b>\n{esc(p['name'])}"
    else:
        title = f"<b>{esc(p['name'])}</b>"
    t = (
        f"{cat_label(p.get('category'))}\n"
        f"{title}\n\n"
        f"💰 Цена: <b>{money(p['price'])}</b>\n"
        f"📏 Размеры: {esc(p['sizes'] or '—')}\n"
        f"🚚 Доставка: ~{esc(p['delivery'] or 'уточняйте')}"
    )
    if p["description"]:
        d = p["description"]
        t += "\n\n" + esc(d if len(d) < 600 else d[:600] + "…")
    if not p["active"]:
        t += "\n\n🙈 <i>скрыт от покупателей</i>"
    return t


# ───────────────────────── ПРОСМОТР ТОВАРОВ ─────────────────────────
async def show_product(
    msg: Message, state: FSMContext, idx: int, edit: bool = True
) -> None:
    d = await state.get_data()
    ids = d.get("view_ids") or []
    admin_view = bool(d.get("admin_view"))
    back = d.get("view_back", "cat")
    if not ids:
        await present(
            msg,
            "Список устарел, открой каталог заново 👇",
            await main_menu_kb(msg.chat.id),
            edit=edit,
            state=state,
        )
        return
    p = None
    for _ in range(len(ids)):
        idx %= len(ids)
        p = await get_product(ids[idx])
        if p:
            break
        idx += 1
    if not p:
        await present(
            msg,
            "Товары не найдены.",
            await main_menu_kb(msg.chat.id),
            edit=edit,
            state=state,
        )
        return
    await state.update_data(view_idx=idx)
    photos = json.loads(p["photos"] or "[]")
    b = InlineKeyboardBuilder()
    n = len(ids)
    if n > 1:
        b.row(
            btn("◀️", f"v:{idx - 1}"),
            btn(f"{idx + 1}/{n}", "noop"),
            btn("▶️", f"v:{idx + 1}"),
        )
    if admin_view:
        b.row(
            btn("💰 Цена", f"ae_price:{p['id']}"),
            btn("🚚 Срок", f"ae_del:{p['id']}"),
        )
        b.row(
            btn("📏 Размеры", f"ae_sz:{p['id']}"),
            btn("📝 Описание", f"ae_desc:{p['id']}"),
        )
        b.row(
            btn(
                "👁 Показать" if not p["active"] else "🙈 Скрыть",
                f"ae_tog:{p['id']}",
            ),
            btn("🗑 Удалить", f"ae_rm:{p['id']}"),
        )
        b.row(btn("➕ Ещё товар", "adm_add"), btn("⚙️ Панель", "adm"))
    else:
        b.row(btn("🛒 Купить", f"buy:{p['id']}"))
        if len(photos) > 1:
            b.row(btn(f"📷 Все фото ({len(photos)})", f"photos:{p['id']}"))
        b.row(btn("🔙 Назад", back), btn("🏠 Меню", "menu"))
    await present(
        msg,
        card_text(p),
        b.as_markup(),
        photo=photos[0] if photos else None,
        edit=edit,
        state=state,
    )


async def open_list(
    msg: Message,
    state: FSMContext,
    ids: list[int],
    back: str,
    admin: bool = False,
    edit: bool = True,
) -> None:
    await state.update_data(
        view_ids=ids, view_idx=0, view_back=back, admin_view=admin
    )
    await show_product(msg, state, 0, edit=edit)


# ───────────────────────── РОУТЕРЫ ─────────────────────────
user_router = Router()
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddP(StatesGroup):
    category = State()
    name = State()
    photos = State()
    price = State()
    sizes = State()
    custom_size = State()
    delivery = State()
    custom_delivery = State()
    desc = State()


class Search(StatesGroup):
    name = State()


class Buy(StatesGroup):
    address = State()


class SettingsEdit(StatesGroup):
    shop_name = State()
    kaspi_phone = State()
    kaspi_name = State()
    support = State()


# ═════════════════════════ ПОКУПАТЕЛЬ ═════════════════════════
@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext) -> None:
    await state.clear()
    sent = await m.answer(
        await menu_text(), reply_markup=await main_menu_kb(m.from_user.id)
    )
    await state.update_data(bot_msg_id=sent.message_id)


@user_router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await present(
        c.message, await menu_text(), await main_menu_kb(c.from_user.id), state=state
    )
    await c.answer()


@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery) -> None:
    await c.answer()


@user_router.callback_query(F.data == "contact")
async def cb_contact(c: CallbackQuery, state: FSMContext) -> None:
    support = await get_setting("support")
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Меню", callback_data="menu")
    await present(
        c.message,
        f"💬 По любым вопросам пиши: {esc(support) or 'администратору'}",
        kb.as_markup(),
        state=state,
    )
    await c.answer()


# ---- каталог: категория → бренд → товары ----
@user_router.callback_query(F.data == "cat")
async def cb_catalog(c: CallbackQuery, state: FSMContext) -> None:
    rows = await db_all(
        "SELECT category, COUNT(*) cnt FROM products WHERE active=1 "
        "GROUP BY category"
    )
    counts = {r["category"] or "другое": r["cnt"] for r in rows}
    b = InlineKeyboardBuilder()
    for key, label in CATEGORIES:
        cnt = counts.get(key, 0)
        if cnt:
            b.button(text=f"{label} ({cnt})", callback_data=f"cg:{key}")
    b.adjust(2)
    b.row(btn("🏠 Меню", "menu"))
    text = (
        "🛍 <b>Каталог</b>\nВыбери категорию:"
        if any(counts.values())
        else "Каталог пока пуст 🙈"
    )
    await present(c.message, text, b.as_markup(), state=state)
    await c.answer()


@user_router.callback_query(F.data.startswith("cg:"))
async def cb_category(c: CallbackQuery, state: FSMContext) -> None:
    cat = c.data.split(":", 1)[1]
    rows = await db_all(
        "SELECT id FROM products WHERE category=? AND active=1 ORDER BY id DESC",
        (cat,),
    )
    if not rows:
        await c.answer("Тут пока пусто", show_alert=True)
        return
    # сразу товары категории, без выбора бренда
    await open_list(c.message, state, [r["id"] for r in rows], "cat")
    await c.answer()


@user_router.callback_query(F.data.startswith("v:"))
async def cb_view(c: CallbackQuery, state: FSMContext) -> None:
    await show_product(c.message, state, int(c.data.split(":")[1]))
    await c.answer()


@user_router.callback_query(F.data.startswith("photos:"))
async def cb_photos(c: CallbackQuery) -> None:
    p = await get_product(int(c.data.split(":")[1]))
    if p:
        photos = json.loads(p["photos"] or "[]")[:10]
        if len(photos) > 1:
            await c.message.answer_media_group(
                [InputMediaPhoto(media=x) for x in photos]
            )
    await c.answer()


# ---- поиск ----
@user_router.callback_query(F.data == "search")
async def cb_search(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    b = InlineKeyboardBuilder()
    b.button(text="🔤 По названию / бренду", callback_data="sn")
    b.button(text="📏 По размеру", callback_data="ss")
    b.button(text="🏠 Меню", callback_data="menu")
    b.adjust(1)
    await present(
        c.message, "🔍 <b>Поиск</b>\nКак будем искать?", b.as_markup(), state=state
    )
    await c.answer()


@user_router.callback_query(F.data == "sn")
async def cb_search_name(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Search.name)
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад", callback_data="search")
    await present(
        c.message,
        "Напиши название или бренд (например: <i>nike</i> или <i>худи</i>):",
        b.as_markup(),
        state=state,
    )
    await c.answer()


@user_router.message(Search.name, F.text)
async def search_name_input(m: Message, state: FSMContext) -> None:
    q = m.text.strip().lower()
    rows = await db_all(PRODUCT_SQL + " WHERE p.active=1 ORDER BY p.id DESC")
    ids = [
        r["id"]
        for r in rows
        if q in r["name"].lower()
        or q in (r.get("brand") or "").lower()
        or q in (r.get("category") or "").lower()
        or q in (r["description"] or "").lower()
    ]
    await state.set_state(None)
    await _safe_delete(m.bot, m.chat.id, m.message_id)
    if not ids:
        b = InlineKeyboardBuilder()
        b.button(text="🔍 Искать ещё", callback_data="sn")
        b.button(text="🏠 Меню", callback_data="menu")
        b.adjust(1)
        sent = await m.answer("Ничего не нашлось 😕", reply_markup=b.as_markup())
        await state.update_data(bot_msg_id=sent.message_id)
        return
    # удалить старое сообщение бота перед показом списка
    d = await state.get_data()
    await _safe_delete(m.bot, m.chat.id, d.get("bot_msg_id"))
    await open_list(m, state, ids, "search", edit=False)


@user_router.callback_query(F.data == "ss")
async def cb_search_size(c: CallbackQuery, state: FSMContext) -> None:
    rows = await db_all("SELECT sizes FROM products WHERE active=1")
    found = {s for r in rows for s in split_sizes(r["sizes"])}
    opts = [s for s in PRESET_SIZES if s in found] + sorted(found - set(PRESET_SIZES))
    await state.update_data(size_opts=opts)
    b = InlineKeyboardBuilder()
    for i, s in enumerate(opts):
        b.button(text=s, callback_data=f"sf:{i}")
    b.adjust(4)
    b.row(btn("🔙 Назад", "search"))
    await present(
        c.message,
        "📏 Выбери размер:" if opts else "Пока нет товаров 🙈",
        b.as_markup(),
        state=state,
    )
    await c.answer()


@user_router.callback_query(F.data.startswith("sf:"))
async def cb_size_filter(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    opts = d.get("size_opts") or []
    i = int(c.data.split(":")[1])
    if i >= len(opts):
        await c.answer("Открой поиск заново", show_alert=True)
        return
    rows = await db_all(PRODUCT_SQL + " WHERE p.active=1 ORDER BY p.id DESC")
    ids = [r["id"] for r in rows if opts[i] in split_sizes(r["sizes"])]
    if not ids:
        await c.answer("Ничего нет", show_alert=True)
        return
    await open_list(c.message, state, ids, "ss")
    await c.answer()


# ---- покупка ----
@user_router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery, state: FSMContext) -> None:
    p = await get_product(int(c.data.split(":")[1]))
    if not p or not p["active"]:
        await c.answer("Товар недоступен", show_alert=True)
        return
    sizes = split_sizes(p["sizes"]) or ["Один размер"]
    b = InlineKeyboardBuilder()
    for i, s in enumerate(sizes):
        b.button(text=s, callback_data=f"sz:{p['id']}:{i}")
    b.adjust(4)
    b.row(btn("🔙 Назад", "v_back"))
    await present(
        c.message,
        f"<b>{esc(p['name'])}</b>\n"
        f"💰 {money(p['price'])}\n\n📏 Выбери размер:",
        b.as_markup(),
        photo=None if not c.message.photo else None,
        state=state,
    )
    await c.answer()


@user_router.callback_query(F.data == "v_back")
async def cb_v_back(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await show_product(c.message, state, d.get("view_idx", 0))
    await c.answer()


def order_text(o: dict, header: str) -> str:
    buyer = f'<a href="tg://user?id={o["user_id"]}">{esc(o["user_name"])}</a>'
    if o["username"]:
        buyer += f" (@{esc(o['username'])})"
    addr = (o.get("address") or "").strip()
    if addr:
        link = gis_link(addr)
        addr_line = f'📍 Адрес: <a href="{link}">{esc(addr)}</a>\n'
    else:
        addr_line = "📍 Адрес: —\n"
    return (
        f"{header} <code>{o['code']}</code>\n\n"
        f"👤 {buyer}\n"
        f"🛍 {esc(o['title'])}\n"
        f"📏 Размер: {esc(o['size'])}\n"
        f"{addr_line}"
        f"💰 Сумма: <b>{money(o['price'])}</b>\n"
        f"🚚 Доставка: ~{esc(o['delivery'] or '—')}\n"
        f"Статус: {STATUS[o['status']]}"
    )


def admin_order_kb(o: dict):
    b = InlineKeyboardBuilder()
    if o["status"] == "new":
        b.row(
            btn("✅ Подтвердить оплату", f"o_ok:{o['id']}"),
            btn("❌ Отклонить", f"o_no:{o['id']}"),
        )
    elif o["status"] == "paid":
        b.row(btn("📦 Отметить отправленным", f"o_sent:{o['id']}"))
    return b.as_markup()


async def notify_admins(bot: Bot, text: str, kb) -> None:
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logging.warning("admin %s notify failed: %s", aid, e)


@user_router.callback_query(F.data.startswith("sz:"))
async def cb_size_chosen(c: CallbackQuery, state: FSMContext) -> None:
    _, pid, i = c.data.split(":")
    p = await get_product(int(pid))
    if not p or not p["active"]:
        await c.answer("Товар недоступен", show_alert=True)
        return
    sizes = split_sizes(p["sizes"]) or ["Один размер"]
    size = sizes[int(i)] if int(i) < len(sizes) else sizes[0]
    brand = (p.get("brand") or "").strip()
    title = (
        p["name"]
        if (not brand or brand in ("—", "-", "."))
        else f"{brand} {p['name']}"
    )
    await state.update_data(
        buy_pid=p["id"],
        buy_size=size,
        buy_title=title,
        buy_price=p["price"],
        buy_delivery=p["delivery"] or "",
    )
    await state.set_state(Buy.address)
    b = InlineKeyboardBuilder()
    b.row(btn("❌ Отмена", "buy_cancel"))
    await present(
        c.message,
        f"<b>{esc(p['name'])}</b>\n"
        f"📏 Размер: {esc(size)}\n"
        f"💰 {money(p['price'])}\n\n"
        f"📍 Напиши <b>адрес доставки по Караганде</b>\n"
        f"(улица, дом, квартира / подъезд):\n\n"
        f"<i>Например: ул. Бухар Жырау 15, кв. 42</i>",
        b.as_markup(),
        state=state,
    )
    await c.answer()


@user_router.callback_query(F.data == "buy_cancel", Buy.address)
async def cb_buy_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await present(
        c.message,
        "Заказ отменён.",
        await main_menu_kb(c.from_user.id),
        state=state,
    )
    await c.answer()


@user_router.message(Buy.address, F.text)
async def buy_address_input(m: Message, state: FSMContext, bot: Bot) -> None:
    address = m.text.strip()[:200]
    if len(address) < 5:
        await m.answer(
            "Адрес слишком короткий. Напиши улицу, дом и квартиру по Караганде."
        )
        return
    d = await state.get_data()
    pid = d.get("buy_pid")
    if not pid:
        await state.clear()
        await m.answer("Сессия устарела, начни заново из каталога.")
        return
    p = await get_product(pid)
    if not p or not p["active"]:
        await state.clear()
        await m.answer("Товар больше недоступен.")
        return

    while True:
        code = str(random.randint(10000, 99999))
        if not await db_one("SELECT 1 FROM orders WHERE code=?", (code,)):
            break

    oid = await db_exec(
        "INSERT INTO orders(code,user_id,user_name,username,product_id,title,"
        "size,price,delivery,address,status,created) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'new',?)",
        (
            code,
            m.from_user.id,
            m.from_user.full_name,
            m.from_user.username or "",
            p["id"],
            d["buy_title"],
            d["buy_size"],
            d["buy_price"],
            d["buy_delivery"],
            address,
            int(time.time()),
        ),
    )
    o = await db_one("SELECT * FROM orders WHERE id=?", (oid,))
    s = await get_all_settings()
    text = (
        f"✅ <b>Заказ создан!</b>\n\n"
        f"🛍 {esc(o['title'])}\n"
        f"📏 Размер: {esc(o['size'])}\n"
        f"📍 Адрес: {esc(address)}\n"
        f"🚚 Доставка: ~{esc(o['delivery'] or '—')}\n\n"
        f"💳 <b>Оплата через Kaspi</b>\n"
        f"Номер: <code>{esc(s['kaspi_phone'])}</code>\n"
    )
    if s["kaspi_name"]:
        text += f"Получатель: {esc(s['kaspi_name'])}\n"
    text += (
        f"Сумма: <b>{money(o['price'])}</b>\n\n"
        f"⚠️ В комментарии к переводу <b>обязательно</b> напиши код заказа:\n"
        f"<code>{code}</code>\n\n"
        f"После перевода нажми «Я оплатил» — мы проверим и подтвердим заказ."
    )
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Я оплатил", f"pay:{oid}"))
    b.row(btn("❌ Отменить заказ", f"cancel:{oid}"), btn("🏠 Меню", "menu"))
    await state.clear()
    await send_clean(m, text, b.as_markup(), state=state)
    await notify_admins(
        bot,
        order_text(o, "🆕 <b>Новый заказ</b>")
        + "\n\n⏳ Ожидай поступления денег на Kaspi.",
        admin_order_kb(o),
    )


@user_router.callback_query(F.data.startswith("pay:"))
async def cb_paid(c: CallbackQuery, bot: Bot) -> None:
    o = await db_one(
        "SELECT * FROM orders WHERE id=? AND user_id=?",
        (int(c.data.split(":")[1]), c.from_user.id),
    )
    if not o or o["status"] != "new":
        await c.answer("Заказ уже обработан", show_alert=True)
        return
    await notify_admins(
        bot,
        order_text(o, "💬 <b>Покупатель нажал «Я оплатил»</b>")
        + "\n\nПроверь поступление в Kaspi.",
        admin_order_kb(o),
    )
    await c.answer("Отправили админу, ждите подтверждения ✅", show_alert=True)


@user_router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel_order(c: CallbackQuery, state: FSMContext) -> None:
    o = await db_one(
        "SELECT * FROM orders WHERE id=? AND user_id=?",
        (int(c.data.split(":")[1]), c.from_user.id),
    )
    if not o or o["status"] != "new":
        await c.answer("Нельзя отменить", show_alert=True)
        return
    await db_exec("UPDATE orders SET status='canceled' WHERE id=?", (o["id"],))
    await present(
        c.message,
        f"❌ Заказ <code>{o['code']}</code> отменён.",
        await main_menu_kb(c.from_user.id),
        state=state,
    )
    await c.answer()


@user_router.callback_query(F.data == "my")
async def cb_my_orders(c: CallbackQuery, state: FSMContext) -> None:
    rows = await db_all(
        "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (c.from_user.id,),
    )
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Меню", callback_data="menu")
    if not rows:
        await present(c.message, "У тебя пока нет заказов.", b.as_markup(), state=state)
    else:
        t = "📦 <b>Твои заказы</b>\n\n" + "\n\n".join(
            f"<code>{o['code']}</code> — {esc(o['title'])} ({esc(o['size'])})\n"
            f"📍 {esc(o.get('address') or '—')}\n"
            f"{money(o['price'])} • {STATUS[o['status']]}"
            for o in rows
        )
        await present(c.message, t, b.as_markup(), state=state)
    await c.answer()


# ═════════════════════════ АДМИНКА ═════════════════════════
def admin_panel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить товар", callback_data="adm_add")
    b.button(text="📋 Товары / редактировать", callback_data="adm_list")
    b.button(text="📨 Заказы", callback_data="adm_orders")
    b.button(text="⚙️ Настройки магазина", callback_data="adm_settings")
    b.button(text="🏠 Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def cancel_row():
    return [btn("✖️ Отмена", "adm_cancel")]


def cancel_kb():
    b = InlineKeyboardBuilder()
    b.row(*cancel_row())
    return b.as_markup()


@admin_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext) -> None:
    await state.clear()
    sent = await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_panel_kb())
    await state.update_data(bot_msg_id=sent.message_id)


@admin_router.callback_query(F.data == "adm")
async def cb_adm(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await present(c.message, "⚙️ <b>Админ-панель</b>", admin_panel_kb(), state=state)
    await c.answer()


@admin_router.callback_query(F.data == "adm_cancel")
async def cb_adm_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await present(
        c.message,
        "Отменено.\n\n⚙️ <b>Админ-панель</b>",
        admin_panel_kb(),
        state=state,
    )
    await c.answer()


# ---- настройки магазина ----
async def settings_text() -> str:
    s = await get_all_settings()
    return (
        "⚙️ <b>Настройки магазина</b>\n\n"
        f"🏪 Название: <b>{esc(s['shop_name'])}</b>\n"
        f"💳 Kaspi номер: <code>{esc(s['kaspi_phone'])}</code>\n"
        f"👤 Получатель: {esc(s['kaspi_name']) or '—'}\n"
        f"💬 Поддержка: {esc(s['support']) or '—'}\n\n"
        "Нажми, чтобы изменить:"
    )


def settings_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🏪 Название магазина", callback_data="set_shop_name")
    b.button(text="💳 Kaspi номер", callback_data="set_kaspi_phone")
    b.button(text="👤 Получатель Kaspi", callback_data="set_kaspi_name")
    b.button(text="💬 Контакт поддержки", callback_data="set_support")
    b.button(text="⚙️ Панель", callback_data="adm")
    b.adjust(1)
    return b.as_markup()


@admin_router.callback_query(F.data == "adm_settings")
async def cb_adm_settings(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await present(c.message, await settings_text(), settings_kb(), state=state)
    await c.answer()


@admin_router.callback_query(F.data == "set_shop_name")
async def cb_set_shop_name(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsEdit.shop_name)
    await present(c.message, "🏪 Напиши новое название магазина:", cancel_kb(), state=state)
    await c.answer()


@admin_router.message(SettingsEdit.shop_name, F.text)
async def in_shop_name(m: Message, state: FSMContext) -> None:
    name = m.text.strip()[:60]
    if not name:
        await m.answer("Название не может быть пустым.")
        return
    await set_setting("shop_name", name)
    await state.set_state(None)
    await send_clean(
        m,
        f"✅ Название изменено на: <b>{esc(name)}</b>\n\n" + await settings_text(),
        settings_kb(),
        state=state,
    )


@admin_router.callback_query(F.data == "set_kaspi_phone")
async def cb_set_kaspi_phone(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsEdit.kaspi_phone)
    await present(
        c.message,
        "💳 Напиши номер Kaspi (например: <code>+7 700 123 45 67</code>):",
        cancel_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(SettingsEdit.kaspi_phone, F.text)
async def in_kaspi_phone(m: Message, state: FSMContext) -> None:
    phone = m.text.strip()[:30]
    if not phone:
        await m.answer("Номер не может быть пустым.")
        return
    await set_setting("kaspi_phone", phone)
    await state.set_state(None)
    await send_clean(
        m,
        f"✅ Номер Kaspi: <code>{esc(phone)}</code>\n\n" + await settings_text(),
        settings_kb(),
        state=state,
    )


@admin_router.callback_query(F.data == "set_kaspi_name")
async def cb_set_kaspi_name(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsEdit.kaspi_name)
    b = InlineKeyboardBuilder()
    b.row(btn("🧹 Очистить", "set_kaspi_name_clear"))
    b.row(*cancel_row())
    await present(
        c.message,
        "👤 Напиши имя получателя Kaspi (или очисти):",
        b.as_markup(),
        state=state,
    )
    await c.answer()


@admin_router.callback_query(F.data == "set_kaspi_name_clear")
async def cb_kaspi_name_clear(c: CallbackQuery, state: FSMContext) -> None:
    await set_setting("kaspi_name", "")
    await state.clear()
    await present(
        c.message,
        "✅ Получатель очищен.\n\n" + await settings_text(),
        settings_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(SettingsEdit.kaspi_name, F.text)
async def in_kaspi_name(m: Message, state: FSMContext) -> None:
    name = m.text.strip()[:60]
    await set_setting("kaspi_name", name)
    await state.set_state(None)
    await send_clean(
        m,
        f"✅ Получатель: {esc(name) or '—'}\n\n" + await settings_text(),
        settings_kb(),
        state=state,
    )


@admin_router.callback_query(F.data == "set_support")
async def cb_set_support(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsEdit.support)
    b = InlineKeyboardBuilder()
    b.row(btn("🧹 Очистить", "set_support_clear"))
    b.row(*cancel_row())
    await present(
        c.message,
        "💬 Напиши контакт поддержки (username, телефон и т.д.):",
        b.as_markup(),
        state=state,
    )
    await c.answer()


@admin_router.callback_query(F.data == "set_support_clear")
async def cb_support_clear(c: CallbackQuery, state: FSMContext) -> None:
    await set_setting("support", "")
    await state.clear()
    await present(
        c.message,
        "✅ Контакт очищен.\n\n" + await settings_text(),
        settings_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(SettingsEdit.support, F.text)
async def in_support(m: Message, state: FSMContext) -> None:
    contact = m.text.strip()[:100]
    await set_setting("support", contact)
    await state.set_state(None)
    await send_clean(
        m,
        f"✅ Поддержка: {esc(contact) or '—'}\n\n" + await settings_text(),
        settings_kb(),
        state=state,
    )


# ---- добавление товара: категория → бренд → ... ----
async def ensure_default_brand() -> int:
    """Служебный бренд-заглушка (бренды убрали из меню)."""
    await db_exec("INSERT OR IGNORE INTO brands(name) VALUES (?)", ("—",))
    br = await db_one("SELECT id FROM brands WHERE name=?", ("—",))
    return br["id"]


@admin_router.callback_query(F.data == "adm_add")
async def cb_add_start(c: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddP.category)
    b = InlineKeyboardBuilder()
    for key, label in CATEGORIES:
        b.button(text=label, callback_data=f"ac:{key}")
    b.adjust(2)
    b.row(*cancel_row())
    await present(
        c.message,
        "➕ <b>Новый товар</b>\nШаг 1: выбери категорию",
        b.as_markup(),
        state=state,
    )
    await c.answer()


@admin_router.callback_query(F.data.startswith("ac:"), AddP.category)
async def cb_add_category(c: CallbackQuery, state: FSMContext) -> None:
    cat = c.data.split(":", 1)[1]
    brand_id = await ensure_default_brand()
    await state.update_data(category=cat, brand_id=brand_id)
    await state.set_state(AddP.name)
    await present(
        c.message,
        f"Категория: {cat_label(cat)}\n\n"
        f"Шаг 2: напиши название товара (например: <i>Худи Essentials</i>)",
        cancel_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(AddP.name, F.text)
async def in_name(m: Message, state: FSMContext) -> None:
    name = m.text.strip()[:100]
    if not name:
        await m.answer("Название не может быть пустым.")
        return
    await state.update_data(name=name, photos=[])
    await state.set_state(AddP.photos)
    await send_clean(
        m,
        "Шаг 3: отправь фото товара (можно несколько, до 10). "
        "Когда закончишь — нажми «Готово».",
        cancel_kb(),
        state=state,
    )


@admin_router.message(AddP.photos, F.photo)
async def in_photo(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    photos = d.get("photos", [])
    if len(photos) >= 10:
        await m.answer("Максимум 10 фото.")
        return
    photos.append(m.photo[-1].file_id)
    await state.update_data(photos=photos)
    await _safe_delete(m.bot, m.chat.id, m.message_id)
    # удалить предыдущее «фото принято»
    old = d.get("bot_msg_id")
    await _safe_delete(m.bot, m.chat.id, old)
    b = InlineKeyboardBuilder()
    b.row(btn(f"✅ Готово ({len(photos)} фото)", "ph_done"))
    b.row(*cancel_row())
    sent = await m.answer(
        f"📷 Фото {len(photos)} принято. Шли ещё или жми «Готово».",
        reply_markup=b.as_markup(),
    )
    await state.update_data(bot_msg_id=sent.message_id)


@admin_router.message(AddP.photos)
async def in_photo_wrong(m: Message) -> None:
    await m.answer("Нужно отправить именно фото 📷")


@admin_router.callback_query(F.data == "ph_done", AddP.photos)
async def cb_photos_done(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if not d.get("photos"):
        await c.answer("Сначала отправь хотя бы одно фото", show_alert=True)
        return
    await state.set_state(AddP.price)
    await present(
        c.message,
        "Шаг 4: напиши цену в тенге цифрами (например: <code>25000</code>)",
        cancel_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(AddP.price, F.text)
async def in_price(m: Message, state: FSMContext) -> None:
    price = parse_price(m.text)
    if not price:
        await m.answer(
            "Не понял цену, напиши цифрами, например <code>25000</code>"
        )
        return
    d = await state.get_data()
    if d.get("edit_pid"):
        await db_exec(
            "UPDATE products SET price=? WHERE id=?", (price, d["edit_pid"])
        )
        await state.set_state(None)
        await _safe_delete(m.bot, m.chat.id, m.message_id)
        await finish_edit(m, state, d["edit_pid"], edit=False)
        return
    await state.update_data(price=price, sizes=[])
    await state.set_state(AddP.sizes)
    await send_clean(
        m, "Шаг 5: выбери доступные размеры:", sizes_kb([]), state=state
    )


# ---- размеры ----
def size_options(selected: list[str]) -> list[str]:
    return PRESET_SIZES + [s for s in selected if s not in PRESET_SIZES]


def sizes_kb(selected: list[str]):
    opts = size_options(selected)
    b = InlineKeyboardBuilder()
    for i, s in enumerate(opts):
        b.button(
            text=("✅ " if s in selected else "") + s,
            callback_data=f"sz_t:{i}",
        )
    b.adjust(4)
    b.row(btn("✍️ Свой размер", "sz_custom"), btn("Без размера", "sz_one"))
    b.row(btn("➡️ Готово", "sz_done"))
    b.row(*cancel_row())
    return b.as_markup()


@admin_router.callback_query(F.data.startswith("sz_t:"), AddP.sizes)
async def cb_size_toggle(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    sel = list(d.get("sizes", []))
    opts = size_options(sel)
    i = int(c.data.split(":")[1])
    if i < len(opts):
        s = opts[i]
        sel = [x for x in sel if x != s] if s in sel else sel + [s]
        await state.update_data(sizes=sel)
    await c.message.edit_reply_markup(reply_markup=sizes_kb(sel))
    await c.answer()


@admin_router.callback_query(F.data == "sz_one", AddP.sizes)
async def cb_size_one(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(sizes=["Один размер"])
    await c.message.edit_reply_markup(reply_markup=sizes_kb(["Один размер"]))
    await c.answer()


@admin_router.callback_query(F.data == "sz_custom", AddP.sizes)
async def cb_size_custom(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddP.custom_size)
    await present(
        c.message,
        "Напиши свои размеры через запятую (например: <code>46, 48, 50</code>):",
        cancel_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(AddP.custom_size, F.text)
async def in_custom_size(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    sel = list(d.get("sizes", []))
    for s in m.text.split(","):
        s = s.strip()[:15]
        if s and s not in sel:
            sel.append(s)
    await state.update_data(sizes=sel)
    await state.set_state(AddP.sizes)
    await send_clean(m, "Выбери размеры:", sizes_kb(sel), state=state)


@admin_router.callback_query(F.data == "sz_done", AddP.sizes)
async def cb_size_done(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    sel = d.get("sizes", [])
    if not sel:
        await c.answer("Выбери хотя бы один размер", show_alert=True)
        return
    opts = size_options(sel)
    sizes = ", ".join(
        sorted(sel, key=lambda s: opts.index(s) if s in opts else 999)
    )
    if d.get("edit_pid"):
        await db_exec(
            "UPDATE products SET sizes=? WHERE id=?", (sizes, d["edit_pid"])
        )
        await finish_edit(c.message, state, d["edit_pid"])
    else:
        await state.update_data(sizes_str=sizes)
        await ask_delivery(c.message, state)
    await c.answer()


# ---- срок доставки ----
async def ask_delivery(msg: Message, state: FSMContext, edit: bool = True) -> None:
    await state.set_state(AddP.delivery)
    b = InlineKeyboardBuilder()
    for i, t in enumerate(DELIVERY_PRESETS):
        b.button(text=t, callback_data=f"dl:{i}")
    b.adjust(2)
    b.row(btn("✍️ Свой срок", "dl_custom"))
    b.row(*cancel_row())
    await present(
        msg,
        "🚚 Сколько примерно ехать до покупателя?",
        b.as_markup(),
        edit=edit,
        state=state,
    )


async def set_delivery(
    msg: Message, state: FSMContext, value: str, edit: bool = True
) -> None:
    d = await state.get_data()
    if d.get("edit_pid"):
        await db_exec(
            "UPDATE products SET delivery=? WHERE id=?", (value, d["edit_pid"])
        )
        await finish_edit(msg, state, d["edit_pid"], edit=edit)
    else:
        await state.update_data(delivery=value)
        await state.set_state(AddP.desc)
        b = InlineKeyboardBuilder()
        b.row(btn("⏭ Пропустить", "ds_skip"))
        b.row(*cancel_row())
        await present(
            msg,
            "Последний шаг: напиши описание товара (состав, посадка и т.д.) "
            "или пропусти.",
            b.as_markup(),
            edit=edit,
            state=state,
        )


@admin_router.callback_query(F.data.startswith("dl:"), AddP.delivery)
async def cb_delivery(c: CallbackQuery, state: FSMContext) -> None:
    await set_delivery(
        c.message, state, DELIVERY_PRESETS[int(c.data.split(":")[1])]
    )
    await c.answer()


@admin_router.callback_query(F.data == "dl_custom", AddP.delivery)
async def cb_delivery_custom(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddP.custom_delivery)
    await present(
        c.message,
        "Напиши срок (например: <code>5–7 дней</code>):",
        cancel_kb(),
        state=state,
    )
    await c.answer()


@admin_router.message(AddP.custom_delivery, F.text)
async def in_custom_delivery(m: Message, state: FSMContext) -> None:
    await _safe_delete(m.bot, m.chat.id, m.message_id)
    await set_delivery(m, state, m.text.strip()[:40], edit=False)


# ---- описание и сохранение ----
async def save_new_product(msg: Message, state: FSMContext, desc: str) -> None:
    d = await state.get_data()
    pid = await db_exec(
        "INSERT INTO products(brand_id,category,name,description,price,sizes,"
        "photos,delivery,active,created) VALUES (?,?,?,?,?,?,?,?,1,?)",
        (
            d["brand_id"],
            d.get("category") or "другое",
            d["name"],
            desc,
            d["price"],
            d["sizes_str"],
            json.dumps(d["photos"]),
            d["delivery"],
            int(time.time()),
        ),
    )
    await state.clear()
    await state.update_data(view_ids=[pid], view_idx=0, admin_view=True)
    await msg.answer("✅ Товар добавлен!")
    await show_product(msg, state, 0, edit=False)


@admin_router.callback_query(F.data == "ds_skip", AddP.desc)
async def cb_desc_skip(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if d.get("edit_pid"):
        await db_exec(
            "UPDATE products SET description='' WHERE id=?", (d["edit_pid"],)
        )
        await finish_edit(c.message, state, d["edit_pid"])
    else:
        await save_new_product(c.message, state, "")
    await c.answer()


@admin_router.message(AddP.desc, F.text)
async def in_desc(m: Message, state: FSMContext) -> None:
    d = await state.get_data()
    desc = m.text.strip()[:1500]
    await _safe_delete(m.bot, m.chat.id, m.message_id)
    if d.get("edit_pid"):
        await db_exec(
            "UPDATE products SET description=? WHERE id=?", (desc, d["edit_pid"])
        )
        await finish_edit(m, state, d["edit_pid"], edit=False)
    else:
        await save_new_product(m, state, desc)


# ---- список / редактирование ----
@admin_router.callback_query(F.data == "adm_list")
async def cb_adm_list(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    rows = await db_all(
        "SELECT category, COUNT(*) cnt FROM products GROUP BY category"
    )
    counts = {r["category"] or "другое": r["cnt"] for r in rows}
    b = InlineKeyboardBuilder()
    for key, label in CATEGORIES:
        cnt = counts.get(key, 0)
        if cnt:
            b.button(text=f"{label} ({cnt})", callback_data=f"alc:{key}")
    b.adjust(2)
    b.row(btn("⚙️ Панель", "adm"))
    text = "📋 Выбери категорию:" if any(counts.values()) else "Товаров пока нет."
    await present(c.message, text, b.as_markup(), state=state)
    await c.answer()


@admin_router.callback_query(F.data.startswith("alc:"))
async def cb_adm_list_cat(c: CallbackQuery, state: FSMContext) -> None:
    cat = c.data.split(":", 1)[1]
    rows = await db_all(
        "SELECT id FROM products WHERE category=? ORDER BY id DESC",
        (cat,),
    )
    if not rows:
        await c.answer("Пусто", show_alert=True)
        return
    await open_list(
        c.message, state, [r["id"] for r in rows], "adm_list", admin=True
    )
    await c.answer()


async def finish_edit(
    msg: Message, state: FSMContext, pid: int, edit: bool = True
) -> None:
    await state.set_state(None)
    d = await state.get_data()
    ids = d.get("view_ids") or []
    if pid in ids:
        idx = ids.index(pid)
    else:
        ids, idx = [pid], 0
    await state.update_data(
        edit_pid=None, view_ids=ids, view_idx=idx, admin_view=True
    )
    await show_product(msg, state, idx, edit=edit)


@admin_router.callback_query(F.data.startswith("ae_price:"))
async def cb_edit_price(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(edit_pid=int(c.data.split(":")[1]))
    await state.set_state(AddP.price)
    await present(c.message, "💰 Напиши новую цену цифрами:", cancel_kb(), state=state)
    await c.answer()


@admin_router.callback_query(F.data.startswith("ae_desc:"))
async def cb_edit_desc(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(edit_pid=int(c.data.split(":")[1]))
    await state.set_state(AddP.desc)
    b = InlineKeyboardBuilder()
    b.row(btn("🧹 Очистить описание", "ds_skip"))
    b.row(*cancel_row())
    await present(c.message, "📝 Напиши новое описание:", b.as_markup(), state=state)
    await c.answer()


@admin_router.callback_query(F.data.startswith("ae_del:"))
async def cb_edit_delivery(c: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(edit_pid=int(c.data.split(":")[1]))
    await ask_delivery(c.message, state)
    await c.answer()


@admin_router.callback_query(F.data.startswith("ae_sz:"))
async def cb_edit_sizes(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    p = await get_product(pid)
    sel = split_sizes(p["sizes"]) if p else []
    await state.update_data(edit_pid=pid, sizes=sel)
    await state.set_state(AddP.sizes)
    await present(c.message, "📏 Выбери размеры:", sizes_kb(sel), state=state)
    await c.answer()


@admin_router.callback_query(F.data.startswith("ae_tog:"))
async def cb_toggle(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    await db_exec("UPDATE products SET active=1-active WHERE id=?", (pid,))
    d = await state.get_data()
    await show_product(c.message, state, d.get("view_idx", 0))
    await c.answer("Готово")


@admin_router.callback_query(F.data.startswith("ae_rm:"))
async def cb_remove_ask(c: CallbackQuery, state: FSMContext) -> None:
    pid = c.data.split(":")[1]
    b = InlineKeyboardBuilder()
    b.row(btn("🗑 Да, удалить", f"ae_rm_y:{pid}"), btn("Нет", "v_back"))
    await present(c.message, "Точно удалить этот товар?", b.as_markup(), state=state)
    await c.answer()


@admin_router.callback_query(F.data.startswith("ae_rm_y:"))
async def cb_remove_yes(c: CallbackQuery, state: FSMContext) -> None:
    pid = int(c.data.split(":")[1])
    await db_exec("DELETE FROM products WHERE id=?", (pid,))
    d = await state.get_data()
    ids = [i for i in (d.get("view_ids") or []) if i != pid]
    await state.update_data(view_ids=ids)
    if ids:
        await c.answer("Удалено")
        await show_product(c.message, state, d.get("view_idx", 0))
    else:
        await present(
            c.message,
            "Удалено.\n\n⚙️ <b>Админ-панель</b>",
            admin_panel_kb(),
            state=state,
        )
        await c.answer()


# ---- заказы ----
@admin_router.callback_query(F.data == "adm_orders")
async def cb_orders(c: CallbackQuery) -> None:
    rows = await db_all(
        "SELECT * FROM orders WHERE status IN ('new','paid') "
        "ORDER BY id DESC LIMIT 15"
    )
    if not rows:
        await c.answer("Активных заказов нет", show_alert=True)
        return
    await c.answer()
    for o in reversed(rows):
        await c.message.answer(
            order_text(o, "📨 <b>Заказ</b>"), reply_markup=admin_order_kb(o)
        )


async def refresh_order_msg(c: CallbackQuery, o: dict, extra: str = "") -> None:
    try:
        await c.message.edit_text(
            order_text(o, "📨 <b>Заказ</b>") + extra,
            reply_markup=admin_order_kb(o),
        )
    except TelegramBadRequest:
        pass


@admin_router.callback_query(F.data.startswith("o_ok:"))
async def cb_order_ok(c: CallbackQuery, bot: Bot) -> None:
    o = await db_one("SELECT * FROM orders WHERE id=?", (int(c.data.split(":")[1]),))
    if not o or o["status"] != "new":
        await c.answer("Заказ уже обработан", show_alert=True)
        return
    await db_exec("UPDATE orders SET status='paid' WHERE id=?", (o["id"],))
    o["status"] = "paid"
    await refresh_order_msg(c, o)
    await c.answer("Оплата подтверждена ✅")
    try:
        await bot.send_message(
            o["user_id"],
            f"✅ Оплата по заказу <code>{o['code']}</code> подтверждена!\n"
            f"🛍 {esc(o['title'])} ({esc(o['size'])})\n"
            f"🚚 Ожидай доставку: ~{esc(o['delivery'] or '—')}",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("o_no:"))
async def cb_order_no(c: CallbackQuery, bot: Bot) -> None:
    o = await db_one("SELECT * FROM orders WHERE id=?", (int(c.data.split(":")[1]),))
    if not o or o["status"] != "new":
        await c.answer("Заказ уже обработан", show_alert=True)
        return
    await db_exec("UPDATE orders SET status='canceled' WHERE id=?", (o["id"],))
    o["status"] = "canceled"
    await refresh_order_msg(c, o)
    await c.answer("Отклонено")
    support = await get_setting("support")
    try:
        await bot.send_message(
            o["user_id"],
            f"❌ Заказ <code>{o['code']}</code> отклонён: оплата не найдена.\n"
            f"Если ты оплатил — напиши {esc(support) or 'администратору'}.",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("o_sent:"))
async def cb_order_sent(c: CallbackQuery, bot: Bot) -> None:
    o = await db_one("SELECT * FROM orders WHERE id=?", (int(c.data.split(":")[1]),))
    if not o or o["status"] != "paid":
        await c.answer("Нельзя", show_alert=True)
        return
    await db_exec("UPDATE orders SET status='shipped' WHERE id=?", (o["id"],))
    o["status"] = "shipped"
    await refresh_order_msg(c, o)
    await c.answer("Отмечено 📦")
    try:
        await bot.send_message(
            o["user_id"], f"📦 Заказ <code>{o['code']}</code> отправлен!"
        )
    except Exception:
        pass


# ───────────────────────── KEEP-ALIVE (чтобы Render не засыпал) ─────────────────────────
async def health_handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


async def start_web_server() -> web.AppRunner:
    """HTTP-сервер для health-check. Render / UptimeRobot пингуют / и бот не спит."""
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ping", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health server on 0.0.0.0:%s  (/  /health  /ping)", port)
    return runner


async def self_ping_loop() -> None:
    """Доп. самопинг раз в 10 мин (на Render без внешней ссылки мало помогает,
    но если задан RENDER_EXTERNAL_URL — держит сервис живым)."""
    import aiohttp

    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
    if not url:
        return
    url = url.rstrip("/") + "/ping"
    logging.info("Self-ping enabled: %s", url)
    while True:
        await asyncio.sleep(600)  # каждые 10 минут
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    logging.info("Self-ping %s -> %s", url, r.status)
        except Exception as e:
            logging.warning("Self-ping failed: %s", e)


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not BOT_TOKEN or BOT_TOKEN == "ВАШ_ТОКЕН_БОТА":
        raise SystemExit("Укажи BOT_TOKEN в начале файла")
    if not ADMIN_IDS:
        raise SystemExit("Укажи хотя бы один ADMIN_ID в начале файла")

    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(user_router)
    await bot.delete_webhook(drop_pending_updates=True)

    # health-сервер (нужен Render'у) + polling бота параллельно
    await start_web_server()
    asyncio.create_task(self_ping_loop())

    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
