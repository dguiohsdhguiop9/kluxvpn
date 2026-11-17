"""
KlyxVPN — Telegram-бот + API для мини-апки.

Что умеет:
  • Меню: купить/продлить, мой ключ, инструкции, пробный период, рефералка, поддержка
  • Оплата в Telegram Stars (можно через ОТДЕЛЬНЫЙ платёжный бот — PAY_BOT_TOKEN)
  • После оплаты сам создаёт/продлевает ключ в панели 3X-UI и присылает его
  • Админ-панель /admin: статистика, оплаты, выдать дни, рассылка, проверка сервера
  • Напоминание за 3 дня до конца подписки
  • API для мини-апки (данные подписки, ключ, оплата, пробный период)

Переменные окружения (Railway → Variables) — см. README в чате.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from aiohttp import web

import db
from xui import XUI

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("klyxvpn")

# ---------------- настройки ----------------
env = lambda k, d="": (os.getenv(k) or d).strip()

BOT_TOKEN = env("BOT_TOKEN")
PAY_BOT_TOKEN = env("PAY_BOT_TOKEN")  # токен второго бота, куда будут приходить звёзды (необязательно)
ADMIN_ID = int(env("ADMIN_ID", "8949720653"))
WEBAPP_URL = env("WEBAPP_URL")
SUPPORT = env("SUPPORT_USERNAME").lstrip("@")
PUBLIC_URL = env("PUBLIC_URL") or (f"https://{env('RAILWAY_PUBLIC_DOMAIN')}" if env("RAILWAY_PUBLIC_DOMAIN") else "")
PORT = int(env("PORT", "8080"))

TRIAL_DAYS = int(env("TRIAL_DAYS", "1"))
REF_BONUS_DAYS = int(env("REF_BONUS_DAYS", "7"))
DEVICES = int(env("DEVICES", "3"))
SERVER_NAME = env("SERVER_NAME", "🇫🇷 Франция")

xui = XUI(
    env("XUI_URL"),
    env("XUI_USERNAME"),
    env("XUI_PASSWORD"),
    int(env("XUI_INBOUND_ID", "1")),
    env("SERVER_HOST"),
    env("XUI_TOKEN"),  # API-токен панели (3X-UI 3.x), можно вместо логина/пароля
)

if not BOT_TOKEN:
    raise SystemExit("❌ Не задан BOT_TOKEN. Добавь его в Railway → Variables.")

# Тарифы: ключ -> (название, цена в Stars, дней)
PLANS = {
    "m1": ("1 месяц", 129, 30),
    "m3": ("3 месяца", 324, 90),
    "m12": ("12 месяцев", 1039, 365),
}

main_bot = Bot(BOT_TOKEN)
pay_bot = Bot(PAY_BOT_TOKEN) if PAY_BOT_TOKEN and PAY_BOT_TOKEN != BOT_TOKEN else main_bot
MAIN_USERNAME = ""
PAY_USERNAME = ""

dp = Dispatcher()
main_r = Router(name="main")
admin_r = Router(name="admin")
pay_r = Router(name="payments")
paybot_r = Router(name="paybot")

MSK = timezone(timedelta(hours=3))


# ---------------- фильтры ----------------
class IsMainBot(BaseFilter):
    async def __call__(self, event, bot: Bot) -> bool:
        return bot.id == main_bot.id


class IsPayBot(BaseFilter):
    async def __call__(self, event, bot: Bot) -> bool:
        return pay_bot is not main_bot and bot.id == pay_bot.id


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return event.from_user and event.from_user.id == ADMIN_ID


for r in (main_r, admin_r):
    r.message.filter(IsMainBot())
    r.callback_query.filter(IsMainBot())
admin_r.message.filter(IsAdmin())
admin_r.callback_query.filter(IsAdmin())
paybot_r.message.filter(IsPayBot())


# ---------------- помощники ----------------
def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m.%Y")


def days_left(u) -> int:
    if not u or u["expires_at"] <= time.time():
        return 0
    return max(1, int((u["expires_at"] - time.time() + 86399) // 86400))


def is_active(u) -> bool:
    return bool(u and u["expires_at"] > time.time())


def webapp_url() -> str:
    if not WEBAPP_URL:
        return ""
    if not PUBLIC_URL:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}api={quote(PUBLIC_URL, safe='')}"


def ref_link(uid: int) -> str:
    return f"https://t.me/{MAIN_USERNAME}?start=ref{uid}"


def support_text() -> str:
    return f"@{SUPPORT}" if SUPPORT else "напиши администратору"


async def notify_admin(text: str):
    try:
        await main_bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось написать админу: %s", e)


async def key_for(u) -> str | None:
    if not (u and u["uuid"] and xui.configured):
        return None
    try:
        return await xui.link(u["uuid"], u["id"], f"KlyxVPN {SERVER_NAME}")
    except Exception as e:
        log.error("Не удалось собрать ключ: %s", e)
        return None


async def sync_user(uid: int):
    """Отправляет в панель 3X-UI текущий срок подписки пользователя (создаёт ключ, если его нет)."""
    if not xui.configured:
        raise RuntimeError("Панель 3X-UI не настроена (XUI_URL / XUI_USERNAME / XUI_PASSWORD)")
    u = db.get_user(uid)
    if u["uuid"]:
        await xui.update_client(u["uuid"], uid, u["expires_at"], DEVICES)
    else:
        new_uuid = await xui.create_client(uid, u["expires_at"], DEVICES)
        db.set_uuid(uid, new_uuid)


async def add_days(uid: int, days: int) -> tuple[bool, str | None]:
    """Продлевает подписку на N дней. Возвращает (успех_в_панели, ключ)."""
    u = db.get_user(uid)
    base = max(int(time.time()), u["expires_at"] or 0)
    db.set_expires(uid, base + days * 86400)
    try:
        await sync_user(uid)
    except Exception as e:
        log.error("Ошибка панели для %s: %s", uid, e)
        await notify_admin(
            f"⚠️ Не удалось выдать ключ пользователю <code>{uid}</code>: {esc(e)}\n"
            f"Срок в базе продлён. Когда починишь панель — отправь <code>/sync {uid}</code>"
        )
        return False, None
    return True, await key_for(db.get_user(uid))


async def make_invoice_link(plan_key: str, uid: int) -> str:
    name, stars, days = PLANS[plan_key]
    return await pay_bot.create_invoice_link(
        title=f"KlyxVPN — {name}",
        description=f"Доступ к VPN на {name}. Безлимитный трафик, до {DEVICES} устройств.",
        payload=f"{plan_key}:{uid}",
        currency="XTR",
        prices=[LabeledPrice(label=name, amount=stars)],
    )


# ---------------- тексты и клавиатуры ----------------
def home_text(u) -> str:
    if is_active(u):
        status = f"🟢 Подписка активна до <b>{fmt_date(u['expires_at'])}</b> (осталось {days_left(u)} дн.)"
    else:
        status = "⚪️ Подписки нет"
    return (
        "⚡️ <b>KlyxVPN</b> — быстрый и надёжный VPN\n\n"
        "🔒 Протокол VLESS Reality — работает там, где другие блокируют\n"
        f"♾ Безлимитный трафик · 📱 до {DEVICES} устройств\n"
        f"🌍 Сервер: {SERVER_NAME}\n\n"
        f"{status}"
    )


def home_kb(u) -> InlineKeyboardMarkup:
    rows = []
    if webapp_url():
        rows.append([InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=webapp_url()))])
    rows.append([InlineKeyboardButton(text="💎 Продлить подписку" if is_active(u) else "💎 Купить подписку", callback_data="buy")])
    rows.append([
        InlineKeyboardButton(text="🔑 Мой ключ", callback_data="key"),
        InlineKeyboardButton(text="📲 Как подключить", callback_data="guide"),
    ])
    if TRIAL_DAYS and u and not u["trial_used"] and not is_active(u):
        rows.append([InlineKeyboardButton(text=f"🎁 Попробовать бесплатно ({TRIAL_DAYS} дн.)", callback_data="trial")])
    rows.append([
        InlineKeyboardButton(text="👥 Пригласить друга", callback_data="ref"),
        InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(*extra) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*extra, [InlineKeyboardButton(text="« В меню", callback_data="home")]])


def plans_text() -> str:
    lines = ["💎 <b>Выбери тариф</b>\n"]
    base = PLANS["m1"][1]
    for key, (name, stars, days) in PLANS.items():
        months = days / 30
        per = round(stars / months)
        save = round((1 - per / base) * 100)
        extra = f" · выгода {save}%" if save > 0 else ""
        lines.append(f"• <b>{name}</b> — {stars} ⭐ ({per} ⭐/мес{extra})")
    lines.append(f"\n♾ Безлимит · 📱 до {DEVICES} устройств · 🌍 {SERVER_NAME}")
    return "\n".join(lines)


def plans_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{name} — {stars} ⭐", callback_data=f"plan:{k}")] for k, (name, stars, _) in PLANS.items()]
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


GUIDES = {
    "ios": ("🍏 iPhone / iPad", [
        "Установи <b>v2RayTun</b> или <b>Streisand</b> из App Store",
        "В боте нажми «🔑 Мой ключ» и скопируй ключ",
        "В приложении нажми «+» → «Добавить из буфера»",
        "Включи VPN и разреши добавить конфигурацию",
    ]),
    "android": ("🤖 Android", [
        "Установи <b>v2rayNG</b> или <b>Hiddify</b> из Google Play",
        "В боте нажми «🔑 Мой ключ» и скопируй ключ",
        "В приложении нажми «+» → «Импорт из буфера обмена»",
        "Нажми кнопку подключения",
    ]),
    "pc": ("💻 Windows / macOS", [
        "Скачай <b>Hiddify</b> с сайта hiddify.com",
        "В боте нажми «🔑 Мой ключ» и скопируй ключ",
        "В Hiddify: «Новый профиль» → «Добавить из буфера»",
        "Нажми «Подключиться»",
    ]),
}


def guide_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=v[0], callback_data=f"guide:{k}")] for k, v in GUIDES.items()
    ] + [[InlineKeyboardButton(text="« В меню", callback_data="home")]])


async def key_text(u) -> tuple[str, InlineKeyboardMarkup]:
    if not is_active(u):
        return (
            "🔑 У тебя пока нет активной подписки.\n\nКупи тариф или попробуй бесплатно — ключ придёт сразу.",
            back_kb([InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy")]),
        )
    link = await key_for(u)
    if not link:
        return (
            f"🔑 Подписка активна до <b>{fmt_date(u['expires_at'])}</b>, но ключ ещё готовится.\n"
            f"Если через пару минут не появится — {support_text()}",
            back_kb(),
        )
    return (
        f"🔑 <b>Твой ключ</b> (нажми, чтобы скопировать):\n\n<code>{esc(link)}</code>\n\n"
        f"📅 Действует до <b>{fmt_date(u['expires_at'])}</b> (осталось {days_left(u)} дн.)\n"
        f"📱 До {DEVICES} устройств · ⚠️ никому не передавай ключ",
        back_kb([InlineKeyboardButton(text="📲 Как подключить", callback_data="guide")]),
    )


async def edit_or_send(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


def ensure_user(tg_user, referrer: int | None = None) -> tuple:
    is_new = db.upsert_user(tg_user.id, tg_user.username, tg_user.full_name, referrer)
    return db.get_user(tg_user.id), is_new


# ---------------- меню пользователя ----------------
@main_r.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    referrer = None
    if command.args and command.args.startswith("ref") and command.args[3:].isdigit():
        referrer = int(command.args[3:])
    u, is_new = ensure_user(message.from_user, referrer)
    if is_new and u["referrer"]:
        try:
            await main_bot.send_message(
                u["referrer"],
                f"👥 По твоей ссылке пришёл новый пользователь! Когда он оплатит подписку, ты получишь +{REF_BONUS_DAYS} дн.",
            )
        except Exception:
            pass
    await message.answer(home_text(u), reply_markup=home_kb(u), parse_mode="HTML")


@main_r.message(Command("menu"))
async def cmd_menu(message: Message):
    u, _ = ensure_user(message.from_user)
    await message.answer(home_text(u), reply_markup=home_kb(u), parse_mode="HTML")


@main_r.message(Command("buy"))
async def cmd_buy(message: Message):
    ensure_user(message.from_user)
    await message.answer(plans_text(), reply_markup=plans_kb(), parse_mode="HTML")


@main_r.message(Command("key"))
async def cmd_key(message: Message):
    u, _ = ensure_user(message.from_user)
    text, kb = await key_text(u)
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@main_r.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📄 <b>Условия использования KlyxVPN</b>\n\n"
        "1. Подписка даёт доступ к VPN на выбранный срок.\n"
        f"2. Один ключ — до {DEVICES} устройств, передавать ключ третьим лицам нельзя.\n"
        "3. Запрещено использовать сервис для незаконной деятельности.\n"
        "4. Если сервис не заработал — напиши в поддержку, разберёмся или вернём оплату.",
        parse_mode="HTML",
    )


@main_r.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    await message.answer(f"💬 По вопросам оплаты и возвратов: {support_text()}. Ответим в течение суток.")


@main_r.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@main_r.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    u, _ = ensure_user(call.from_user)
    await edit_or_send(call, home_text(u), home_kb(u))
    await call.answer()


@main_r.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery):
    await edit_or_send(call, plans_text(), plans_kb())
    await call.answer()


@main_r.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in PLANS:
        return await call.answer("Тариф не найден", show_alert=True)
    ensure_user(call.from_user)
    name, stars, days = PLANS[key]
    try:
        link = await make_invoice_link(key, call.from_user.id)
    except Exception as e:
        log.error("Ошибка создания счёта: %s", e)
        return await call.answer("Не получилось создать счёт, попробуй позже", show_alert=True)
    await call.answer()
    await call.message.answer(
        f"🧾 <b>Тариф: {name}</b> — {stars} ⭐\n\nНажми кнопку ниже, чтобы оплатить. Ключ придёт сюда сразу после оплаты.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Оплатить {stars} Stars", url=link)],
            [InlineKeyboardButton(text="« Другой тариф", callback_data="buy")],
        ]),
        parse_mode="HTML",
    )


@main_r.callback_query(F.data == "key")
async def cb_key(call: CallbackQuery):
    u, _ = ensure_user(call.from_user)
    await call.answer()
    text, kb = await key_text(u)
    await edit_or_send(call, text, kb)


@main_r.callback_query(F.data == "guide")
async def cb_guide(call: CallbackQuery):
    await edit_or_send(call, "📲 <b>Как подключиться</b>\n\nВыбери своё устройство:", guide_kb())
    await call.answer()


@main_r.callback_query(F.data.startswith("guide:"))
async def cb_guide_os(call: CallbackQuery):
    title, steps = GUIDES.get(call.data.split(":", 1)[1], GUIDES["ios"])
    text = f"<b>{title}</b>\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мой ключ", callback_data="key")],
        [InlineKeyboardButton(text="« Другое устройство", callback_data="guide")],
    ])
    await edit_or_send(call, text, kb)
    await call.answer()


@main_r.callback_query(F.data == "trial")
async def cb_trial(call: CallbackQuery):
    u, _ = ensure_user(call.from_user)
    if u["trial_used"] or not TRIAL_DAYS:
        return await call.answer("Пробный период уже использован 🙂", show_alert=True)
    db.mark(u["id"], "trial_used")
    await call.answer("Активируем…")
    ok, link = await add_days(u["id"], TRIAL_DAYS)
    u = db.get_user(u["id"])
    if ok and link:
        text = (
            f"🎁 Пробный период на {TRIAL_DAYS} дн. активирован!\n\n"
            f"🔑 Твой ключ (нажми, чтобы скопировать):\n<code>{esc(link)}</code>\n\n"
            "Не знаешь, куда вставить? Жми «Как подключить» 👇"
        )
    else:
        text = f"🎁 Пробный период активирован, но ключ ещё готовится. Загляни в «🔑 Мой ключ» через пару минут или {support_text()}."
    await edit_or_send(call, text, back_kb([InlineKeyboardButton(text="📲 Как подключить", callback_data="guide")]))


@main_r.callback_query(F.data == "ref")
async def cb_ref(call: CallbackQuery):
    u, _ = ensure_user(call.from_user)
    text = (
        "👥 <b>Приглашай друзей — получай дни бесплатно</b>\n\n"
        f"За каждого друга, который оплатит подписку, ты получишь <b>+{REF_BONUS_DAYS} дн.</b>\n\n"
        f"Твоя ссылка:\n<code>{ref_link(u['id'])}</code>\n\n"
        f"Приглашено: <b>{db.count_invited(u['id'])}</b> · бонусных дней: <b>{u['bonus_days']}</b>"
    )
    share = f"https://t.me/share/url?url={quote(ref_link(u['id']))}&text={quote('Быстрый VPN, которым пользуюсь сам 👇')}"
    await edit_or_send(call, text, back_kb([InlineKeyboardButton(text="📤 Поделиться", url=share)]))
    await call.answer()


@main_r.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    await call.answer()
    await edit_or_send(
        call,
        f"🆘 <b>Поддержка</b>\n\nПиши: {support_text()}\nОбычно отвечаем в течение пары часов.\n\n"
        "Перед вопросом проверь:\n• подписка активна (🔑 Мой ключ)\n• ключ вставлен целиком\n• в приложении включён VPN",
        back_kb(),
    )


# ---------------- оплата (работает в любом из ботов) ----------------
@pay_r.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    key = query.invoice_payload.split(":", 1)[0]
    await query.answer(ok=key in PLANS, error_message=None if key in PLANS else "Тариф не найден, попробуй ещё раз")


@pay_r.message(F.successful_payment)
async def on_paid(message: Message, bot: Bot):
    sp = message.successful_payment
    if db.payment_exists(sp.telegram_payment_charge_id):
        return
    key, _, uid_s = sp.invoice_payload.partition(":")
    uid = int(uid_s) if uid_s.isdigit() else message.from_user.id
    name, _, days = PLANS.get(key, ("?", 0, 30))

    db.upsert_user(uid, message.from_user.username, message.from_user.full_name)
    first_payment = db.paid_count(uid) == 0
    db.add_payment(uid, key, sp.total_amount, sp.currency, sp.telegram_payment_charge_id)
    ok, link = await add_days(uid, days)
    u = db.get_user(uid)

    if ok and link:
        text = (
            f"✅ <b>Оплата прошла!</b> Тариф: {name}\n"
            f"📅 Подписка до <b>{fmt_date(u['expires_at'])}</b>\n\n"
            f"🔑 Твой ключ (нажми, чтобы скопировать):\n<code>{esc(link)}</code>\n\n"
            "Не знаешь, куда вставить? Жми «Как подключить» 👇"
        )
    else:
        text = (
            f"✅ <b>Оплата прошла!</b> Подписка до <b>{fmt_date(u['expires_at'])}</b>.\n\n"
            f"Ключ готовится — он появится в «🔑 Мой ключ» через пару минут. Если нет — {support_text()}"
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Как подключить", callback_data="guide")],
        [InlineKeyboardButton(text="« В меню", callback_data="home")],
    ])
    try:
        await main_bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.warning("Не удалось отправить ключ %s: %s", uid, e)

    if bot is not main_bot:  # оплатили через платёжного бота — подскажем, куда идти
        await message.answer(f"✅ Оплата получена, спасибо! Ключ отправлен в @{MAIN_USERNAME}")

    # бонус пригласившему
    if first_payment and u["referrer"] and not u["ref_paid"] and REF_BONUS_DAYS:
        db.mark(uid, "ref_paid")
        db.add_bonus_days(u["referrer"], REF_BONUS_DAYS)
        await add_days(u["referrer"], REF_BONUS_DAYS)
        try:
            await main_bot.send_message(u["referrer"], f"🎉 Твой друг оплатил подписку — тебе +{REF_BONUS_DAYS} дн. KlyxVPN!")
        except Exception:
            pass

    await notify_admin(
        f"💰 <b>Оплата: {sp.total_amount} ⭐</b> — {name}\n"
        f"От: {esc(message.from_user.full_name)} (@{esc(message.from_user.username or '—')}, <code>{uid}</code>)\n"
        f"Возврат: <code>/refund {uid} {sp.telegram_payment_charge_id}</code>"
    )


# ---------------- платёжный бот: всё остальное ----------------
@paybot_r.message()
async def paybot_any(message: Message):
    await message.answer(f"Это платёжный бот KlyxVPN 💳\nВсё управление подпиской — в @{MAIN_USERNAME}")


# ---------------- админка ----------------
class AdminSG(StatesGroup):
    give = State()
    broadcast = State()


def admin_kb() -> InlineKeyboardMarkup:
    b = lambda t, d: InlineKeyboardButton(text=t, callback_data=d)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("📊 Статистика", "adm:stats"), b("💰 Оплаты", "adm:pays")],
        [b("🎁 Выдать дни", "adm:give"), b("📢 Рассылка", "adm:bc")],
        [b("🖥 Проверить сервер", "adm:srv")],
    ])


ADMIN_HELP = (
    "🛠 <b>Админ-панель KlyxVPN</b>\n\n"
    "Команды:\n"
    "<code>/user ID</code> — инфо о пользователе\n"
    "<code>/give ID ДНИ</code> — выдать/продлить подписку\n"
    "<code>/sync ID</code> — повторно отправить ключ в панель\n"
    "<code>/refund ID CHARGE_ID</code> — вернуть звёзды"
)


@admin_r.message(Command("admin"))
async def adm_menu(message: Message, state: FSMContext):
    await state.clear()
    warn = "" if db.is_persistent() else "\n\n⚠️ <b>База не на Volume — данные сотрутся при перезапуске!</b>"
    await message.answer(ADMIN_HELP + warn, reply_markup=admin_kb(), parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery):
    s = db.stats()
    await call.answer()
    await call.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: <b>{s['users']}</b> (+{s['users_today']} за сутки)\n"
        f"🟢 Активных подписок: <b>{s['active']}</b>\n"
        f"🎁 Взяли пробный: {s['trials']}\n"
        f"💰 Оплат: <b>{s['payments']}</b>\n"
        f"⭐ Звёзд всего: <b>{s['stars']}</b> (за сутки: {s['stars_today']})",
        parse_mode="HTML",
    )


@admin_r.callback_query(F.data == "adm:pays")
async def adm_pays(call: CallbackQuery):
    rows = db.last_payments(10)
    await call.answer()
    if not rows:
        return await call.message.answer("Оплат пока нет")
    lines = ["💰 <b>Последние оплаты</b>\n"]
    for p in rows:
        who = f"@{p['username']}" if p["username"] else esc(p["name"] or p["user_id"])
        ref = " ↩️ возврат" if p["refunded"] else ""
        lines.append(f"{fmt_date(p['created_at'])} · {p['amount']} ⭐ · {PLANS.get(p['plan'], ('?',))[0]} · {who} (<code>{p['user_id']}</code>){ref}")
    await call.message.answer("\n".join(lines), parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:srv")
async def adm_srv(call: CallbackQuery):
    await call.answer("Проверяю…")
    if not xui.configured:
        return await call.message.answer("❌ Панель не настроена: добавь XUI_URL, XUI_USERNAME, XUI_PASSWORD (или XUI_TOKEN) в Railway")
    try:
        await call.message.answer(await xui.ping())
    except Exception as e:
        await call.message.answer(f"❌ Панель недоступна: {esc(e)}", parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:give")
async def adm_give(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSG.give)
    await call.answer()
    await call.message.answer("🎁 Отправь: <code>ID ДНИ</code>, например <code>123456789 30</code>\n/cancel — отмена", parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:bc")
async def adm_bc(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSG.broadcast)
    await call.answer()
    await call.message.answer(
        f"📢 Пришли сообщение для рассылки (текст, фото, видео — что угодно). Получат все {len(db.all_user_ids())} пользователей.\n/cancel — отмена"
    )


@admin_r.message(Command("cancel"))
async def adm_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено", reply_markup=admin_kb())


async def do_give(message: Message, args: str):
    parts = (args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
        return await message.answer("Формат: <code>ID ДНИ</code>", parse_mode="HTML")
    uid, days = int(parts[0]), int(parts[1])
    if not db.get_user(uid):
        db.upsert_user(uid, None, None)
    ok, link = await add_days(uid, days)
    u = db.get_user(uid)
    await message.answer(
        f"{'✅' if ok else '⚠️'} Пользователю <code>{uid}</code> {'+' if days >= 0 else ''}{days} дн. Подписка до {fmt_date(u['expires_at'])}"
        + ("" if ok else "\nВ панель не отправлено — см. ошибку выше"),
        parse_mode="HTML",
    )
    if days > 0:
        try:
            msg = f"🎁 Тебе начислено <b>+{days} дн.</b> KlyxVPN! Подписка до {fmt_date(u['expires_at'])}"
            if link:
                msg += f"\n\n🔑 Ключ:\n<code>{esc(link)}</code>"
            await main_bot.send_message(uid, msg, parse_mode="HTML")
        except Exception:
            pass


@admin_r.message(AdminSG.give)
async def adm_give_input(message: Message, state: FSMContext):
    await state.clear()
    await do_give(message, message.text)


@admin_r.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    await do_give(message, command.args)


@admin_r.message(AdminSG.broadcast)
async def adm_bc_input(message: Message, state: FSMContext):
    await state.clear()
    ids = db.all_user_ids()
    await message.answer(f"📢 Рассылка запущена на {len(ids)} пользователей…")
    ok = fail = 0
    for uid in ids:
        try:
            await main_bot.copy_message(uid, message.chat.id, message.message_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Рассылка завершена. Доставлено: {ok}, не доставлено: {fail}")


@admin_r.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await message.answer("Формат: /user ID")
    u = db.get_user(int(command.args))
    if not u:
        return await message.answer("Пользователь не найден")
    await message.answer(
        f"👤 <b>{esc(u['name'])}</b> @{esc(u['username'] or '—')} (<code>{u['id']}</code>)\n"
        f"Подписка: {'до ' + fmt_date(u['expires_at']) if is_active(u) else 'нет'}\n"
        f"Ключ в панели: {'есть' if u['uuid'] else 'нет'} · пробный: {'да' if u['trial_used'] else 'нет'}\n"
        f"Оплат: {db.paid_count(u['id'])} · пригласил: {db.count_invited(u['id'])} · пришёл от: {u['referrer'] or '—'}",
        parse_mode="HTML",
    )


@admin_r.message(Command("sync"))
async def cmd_sync(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit() or not db.get_user(int(command.args)):
        return await message.answer("Формат: /sync ID (пользователь должен быть в базе)")
    uid = int(command.args)
    try:
        await sync_user(uid)
        link = await key_for(db.get_user(uid))
        await message.answer("✅ Отправлено в панель")
        if link:
            await main_bot.send_message(uid, f"🔑 Твой ключ KlyxVPN:\n<code>{esc(link)}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ {esc(e)}", parse_mode="HTML")


@admin_r.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        return await message.answer("Формат: /refund ID CHARGE_ID")
    try:
        await pay_bot.refund_star_payment(user_id=int(parts[0]), telegram_payment_charge_id=parts[1])
        db.mark_refunded(parts[1])
        await message.answer(
            "✅ Звёзды возвращены. Подписка не урезана — если нужно, убавь дни: "
            f"<code>/give {parts[0]} -30</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка возврата: {esc(e)}", parse_mode="HTML")


# ---------------- напоминания ----------------
async def reminders_loop():
    while True:
        try:
            for u in db.expiring_soon(3 * 86400):
                db.mark(u["id"], "notified")
                try:
                    await main_bot.send_message(
                        u["id"],
                        f"⏳ Подписка KlyxVPN закончится {fmt_date(u['expires_at'])} (через {days_left(u)} дн.).\nПродли, чтобы VPN не отключился 👇",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Продлить", callback_data="buy")]]),
                    )
                except Exception:
                    pass
        except Exception as e:
            log.error("reminders: %s", e)
        await asyncio.sleep(1800)


# ---------------- API для мини-апки ----------------
def check_init_data(init_data: str) -> dict | None:
    """Проверяет подпись данных Telegram WebApp. Возвращает пользователя или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 7 * 86400:
            return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = web.Response(status=e.status, text=e.text or "{}", content_type="application/json")
        except Exception as e:
            log.exception("API error: %s", e)
            resp = web.json_response({"error": "server error"}, status=500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def api_user(request):
    tg = check_init_data(request.headers.get("X-Init-Data", ""))
    if not tg or "id" not in tg:
        raise web.HTTPUnauthorized(text='{"error":"bad init data"}')
    name = " ".join(x for x in [tg.get("first_name"), tg.get("last_name")] if x)
    db.upsert_user(tg["id"], tg.get("username"), name)
    return db.get_user(tg["id"])


async def me_payload(u) -> dict:
    active = is_active(u)
    return {
        "id": u["id"],
        "name": u["name"],
        "active": active,
        "expires_at": u["expires_at"] if active else 0,
        "expires_text": fmt_date(u["expires_at"]) if active else "",
        "days_left": days_left(u),
        "key": await key_for(u) if active else None,
        "trial_available": bool(TRIAL_DAYS and not u["trial_used"] and not active),
        "trial_days": TRIAL_DAYS,
        "devices": DEVICES,
        "plans": [{"key": k, "name": n, "stars": s, "days": d} for k, (n, s, d) in PLANS.items()],
        "servers": [{"name": SERVER_NAME, "protocol": "VLESS Reality"}],
        "ref_link": ref_link(u["id"]),
        "ref_bonus": REF_BONUS_DAYS,
        "invited": db.count_invited(u["id"]),
        "bonus_days": u["bonus_days"],
        "support": SUPPORT,
        "bot": MAIN_USERNAME,
    }


async def api_me(request):
    return web.json_response(await me_payload(api_user(request)))


async def api_invoice(request):
    u = api_user(request)
    body = await request.json()
    plan = body.get("plan")
    if plan not in PLANS:
        return web.json_response({"error": "unknown plan"}, status=400)
    return web.json_response({"link": await make_invoice_link(plan, u["id"])})


async def api_trial(request):
    u = api_user(request)
    if u["trial_used"] or not TRIAL_DAYS:
        return web.json_response({"error": "trial used"}, status=400)
    db.mark(u["id"], "trial_used")
    await add_days(u["id"], TRIAL_DAYS)
    return web.json_response(await me_payload(db.get_user(u["id"])))


async def api_root(request):
    return web.Response(text="KlyxVPN API ok")


async def start_api():
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", api_root)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/invoice", api_invoice)
    app.router.add_post("/api/trial", api_trial)
    app.router.add_route("OPTIONS", "/{tail:.*}", api_root)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("🌐 API запущен на порту %s (публичный адрес: %s)", PORT, PUBLIC_URL or "не задан")


# ---------------- запуск ----------------
async def main():
    global MAIN_USERNAME, PAY_USERNAME
    MAIN_USERNAME = (await main_bot.get_me()).username
    PAY_USERNAME = (await pay_bot.get_me()).username
    log.info("✅ Бот запущен: @%s", MAIN_USERNAME)
    log.info("💳 Платёжный бот: @%s%s", PAY_USERNAME, "" if pay_bot is not main_bot else " (тот же)")
    log.info("🗄 База: %s%s", db.DB_PATH, "" if db.is_persistent() else "  ⚠️ НЕ на Volume — данные сотрутся при перезапуске!")
    log.info("🖥 Панель 3X-UI: %s", "настроена" if xui.configured else "НЕ настроена (ключи выдаваться не будут)")

    if xui.configured:
        try:
            log.info(await xui.ping())
        except Exception as e:
            log.error("❌ Панель 3X-UI недоступна: %s", e)

    if webapp_url():
        try:
            await main_bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="KlyxVPN", web_app=WebAppInfo(url=webapp_url())))
            log.info("✅ Кнопка мини-апки: %s", webapp_url())
        except Exception as e:
            log.warning("Не удалось поставить кнопку меню: %s", e)

    bots = [main_bot] if pay_bot is main_bot else [main_bot, pay_bot]
    for b in bots:
        await b.delete_webhook(drop_pending_updates=False)

    dp.include_routers(admin_r, pay_r, main_r, paybot_r)
    await start_api()
    asyncio.create_task(reminders_loop())
    await dp.start_polling(*bots)


if __name__ == "__main__":
    asyncio.run(main())
