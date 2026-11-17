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
    FSInputFile,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputSticker,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from aiohttp import web

import db
from payments import CryptoPay, YooMoney
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

ADMIN_PASSWORD = env("ADMIN_PASSWORD")  # пароль для входа в /admin (необязательно, но очень желательно)
ADMIN_IDS = {ADMIN_ID} | {int(x) for x in env("ADMIN_IDS").replace(" ", "").split(",") if x.isdigit()}
crypto = CryptoPay(env("CRYPTOBOT_TOKEN"), env("CRYPTOBOT_TESTNET") == "1")
yoomoney = YooMoney(env("YOOMONEY_WALLET"), env("YOOMONEY_SECRET"))

if not BOT_TOKEN:
    raise SystemExit("❌ Не задан BOT_TOKEN. Добавь его в Railway → Variables.")

# Тарифы: ключ -> (название, цена в Stars, дней)
PLANS = {
    "m1": ("1 месяц", 129, 30),
    "m3": ("3 месяца", 324, 90),
    "m12": ("12 месяцев", 1039, 365),
}

# Цены в рублях (для карты и крипты — крипта считается по курсу от рублёвой цены)
RUB_PRICES = {"m1": 195, "m3": 495, "m12": 1560}

METHOD_NAMES = {"stars": "⭐ Telegram Stars", "crypto": "🪙 Криптовалюта", "card": "💳 Картой (рубли)"}


def methods() -> list[str]:
    m = ["stars"]
    if crypto.enabled:
        m.append("crypto")
    if yoomoney.enabled:
        m.append("card")
    return m


main_bot = Bot(BOT_TOKEN)
pay_bot = Bot(PAY_BOT_TOKEN) if PAY_BOT_TOKEN and PAY_BOT_TOKEN != BOT_TOKEN else main_bot
MAIN_USERNAME = ""
PAY_USERNAME = ""

dp = Dispatcher()
main_r = Router(name="main")
admin_r = Router(name="admin")
pay_r = Router(name="payments")
paybot_r = Router(name="paybot")
admin_login_r = Router(name="admin_login")

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
        return bool(event.from_user and event.from_user.id in ADMIN_IDS)


for r in (main_r, admin_r, admin_login_r):
    r.message.filter(IsMainBot())
    r.callback_query.filter(IsMainBot())
for r in (admin_r, admin_login_r):
    r.message.filter(IsAdmin())
    r.callback_query.filter(IsAdmin())
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
        status = f"🟠 Подписка активна до <b>{fmt_date(u['expires_at'])}</b> (осталось {days_left(u)} дн.)"
    else:
        status = "⚫️ Подписки нет"
    return (
        "⚡️ <b>KlyxVPN</b> — быстрый и надёжный VPN\n\n"
        "🔸 Протокол VLESS Reality — работает там, где другие блокируют\n"
        f"🔸 Безлимитный трафик · до {DEVICES} устройств\n"
        f"🔸 Сервер: {SERVER_NAME}\n\n"
        f"{status}"
    )


def home_kb(u) -> InlineKeyboardMarkup:
    rows = []
    if webapp_url():
        rows.append([InlineKeyboardButton(text="⚡️ Открыть приложение", web_app=WebAppInfo(url=webapp_url()))])
    rows.append([InlineKeyboardButton(text="🔥 Продлить подписку" if is_active(u) else "🔥 Купить подписку", callback_data="buy")])
    rows.append([
        InlineKeyboardButton(text="🔑 Мой ключ", callback_data="key"),
        InlineKeyboardButton(text="📱 Как подключить", callback_data="guide"),
    ])
    if TRIAL_DAYS and u and not u["trial_used"] and not is_active(u):
        rows.append([InlineKeyboardButton(text=f"🎁 Попробовать бесплатно ({TRIAL_DAYS} дн.)", callback_data="trial")])
    rows.append([
        InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="ref"),
        InlineKeyboardButton(text="💬 Поддержка", callback_data="support"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb(*extra) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*extra, [InlineKeyboardButton(text="« В меню", callback_data="home")]])


def plans_text() -> str:
    lines = ["🔥 <b>Выбери тариф</b>\n"]
    base = PLANS["m1"][1]
    for key, (name, stars, days) in PLANS.items():
        months = days / 30
        per = round(stars / months)
        save = round((1 - per / base) * 100)
        extra = f" · выгода {save}%" if save > 0 else ""
        rub = f" / {RUB_PRICES[key]} ₽" if len(methods()) > 1 else ""
        lines.append(f"🔸 <b>{name}</b> — {stars} ⭐{rub} ({per} ⭐/мес{extra})")
    lines.append(f"\n🔸 Безлимит · до {DEVICES} устройств · {SERVER_NAME}")
    return "\n".join(lines)


def plans_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"{name} — {stars} ⭐" + (f" / {RUB_PRICES[k]} ₽" if len(methods()) > 1 else ""),
        callback_data=f"plan:{k}")] for k, (name, stars, _) in PLANS.items()]
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


GUIDES = {
    "ios": ("🍎 iPhone / iPad", [
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
            back_kb([InlineKeyboardButton(text="🔥 Купить подписку", callback_data="buy")]),
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
        f"🗓 Действует до <b>{fmt_date(u['expires_at'])}</b> (осталось {days_left(u)} дн.)\n"
        f"📱 До {DEVICES} устройств · ⚠️ никому не передавай ключ",
        back_kb([InlineKeyboardButton(text="📱 Как подключить", callback_data="guide")]),
    )


BANNER_DIR = os.path.dirname(os.path.abspath(__file__))
_banner_ids: dict[str, str] = {}  # screen -> file_id (Telegram), чтобы не загружать картинку каждый раз


def banner(screen: str):
    if screen in _banner_ids:
        return _banner_ids[screen]
    path = os.path.join(BANNER_DIR, f"banner_{screen}.png")
    return FSInputFile(path) if os.path.exists(path) else None


def _remember(screen: str, msg):
    try:
        if msg and getattr(msg, "photo", None):
            _banner_ids[screen] = msg.photo[-1].file_id
    except Exception:
        pass


async def show(target, screen: str, text: str, kb: InlineKeyboardMarkup | None = None):
    """Показывает экран: баннер + подпись + кнопки. На нажатие кнопки — меняет картинку в том же сообщении."""
    media = banner(screen)
    kb = decorate(kb)
    msg_obj = target.message if isinstance(target, CallbackQuery) else target
    if media is None:  # нет картинки — обычный текст
        if isinstance(target, CallbackQuery):
            try:
                return await msg_obj.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                pass
        return await msg_obj.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)

    if isinstance(target, CallbackQuery) and msg_obj.photo:
        try:
            res = await msg_obj.edit_media(InputMediaPhoto(media=media, caption=text, parse_mode="HTML"), reply_markup=kb)
            _remember(screen, res)
            return res
        except Exception as e:
            if "not modified" in str(e):
                return
            log.debug("edit_media: %s", e)
            media = banner(screen)  # FSInputFile нельзя переиспользовать после ошибки
    res = await msg_obj.answer_photo(media, caption=text, reply_markup=kb, parse_mode="HTML")
    _remember(screen, res)
    if isinstance(target, CallbackQuery):
        try:
            await msg_obj.delete()  # убираем старое текстовое сообщение, чтобы чат был чистым
        except Exception:
            pass
    return res


async def send_screen(uid: int, screen: str, text: str, kb: InlineKeyboardMarkup | None = None):
    """Отправить экран с баннером пользователю по ID (уведомления, ключи)."""
    media = banner(screen)
    kb = decorate(kb)
    if media is None:
        return await main_bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    res = await main_bot.send_photo(uid, media, caption=text, reply_markup=kb, parse_mode="HTML")
    _remember(screen, res)
    return res


# ---------------- стикеры ----------------
STICKERS = [("hello", "👋"), ("vpn_on", "🔒"), ("paid", "✅"), ("key", "🔑"), ("thanks", "❤️"),
            ("cool", "😎"), ("renew", "⏳"), ("friends", "🤝"), ("support", "💬"), ("fire", "🔥")]
_sticker_ids: dict[str, str] = {}


def sticker_set_name() -> str:
    return f"klyxvpn_by_{MAIN_USERNAME}"


async def load_stickers():
    try:
        st = await main_bot.get_sticker_set(sticker_set_name())
        for (name, _), s in zip(STICKERS, st.stickers):
            _sticker_ids[name] = s.file_id
        log.info("🦊 Стикерпак загружен: %s стикеров", len(_sticker_ids))
    except Exception:
        log.info("🦊 Стикерпака ещё нет — создай его командой /stickers в админке")


async def send_sticker(uid: int, name: str):
    if name in _sticker_ids:
        try:
            await main_bot.send_sticker(uid, _sticker_ids[name])
        except Exception:
            pass


# ---------------- свои иконки на кнопках (custom emoji, нужен Telegram Premium у владельца бота) ----------------
ICONS = ["fire", "key", "phone", "friends", "chat", "bolt", "gift", "star", "coin", "card", "hourglass",
         "link", "chart", "money", "megaphone", "server", "back", "check", "rocket"]
ICON_EMOJI = {"fire": "🔥", "key": "🔑", "phone": "📱", "friends": "🤝", "chat": "💬", "bolt": "⚡️", "gift": "🎁",
              "star": "⭐️", "coin": "🪙", "card": "💳", "hourglass": "⏳", "link": "🔗", "chart": "📊", "money": "💰",
              "megaphone": "📢", "server": "🖥", "back": "⬅️", "check": "✅", "rocket": "🚀"}
# какой эмодзи в начале текста кнопки заменить на какую иконку
EMOJI_TO_ICON = {"🔥": "fire", "🔑": "key", "📱": "phone", "🤝": "friends", "💬": "chat", "⚡️": "bolt", "⚡": "bolt",
                 "🎁": "gift", "⭐": "star", "🪙": "coin", "💳": "card", "⏳": "hourglass", "🔗": "link", "📊": "chart",
                 "💰": "money", "📢": "megaphone", "🖥": "server", "«": "back", "✅": "check", "🚀": "rocket"}
_icon_ids: dict[str, str] = {}


def icons_set_name() -> str:
    return f"klyxicons2_by_{MAIN_USERNAME}"


async def load_icons():
    if db.get_setting("icons") != "1":
        return
    try:
        st = await main_bot.get_sticker_set(icons_set_name())
        for name, s in zip(ICONS, st.stickers):
            if s.custom_emoji_id:
                _icon_ids[name] = s.custom_emoji_id
        log.info("🎨 Иконки кнопок включены: %s шт.", len(_icon_ids))
    except Exception as e:
        log.warning("Иконки кнопок не загрузились: %s", e)


def decorate(kb: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Заменяет эмодзи в начале кнопок на свои оранжевые иконки (если они включены)."""
    if not kb or not _icon_ids:
        return kb
    rows = []
    for row in kb.inline_keyboard:
        new_row = []
        for b in row:
            text = b.text
            for emo, icon in EMOJI_TO_ICON.items():
                if text.startswith(emo) and icon in _icon_ids and not b.icon_custom_emoji_id:
                    rest = text[len(emo):].lstrip("\ufe0f").strip()
                    b = b.model_copy(update={"text": rest or text, "icon_custom_emoji_id": _icon_ids[icon]})
                    break
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_or_send(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup, screen: str = "home"):
    await show(call, screen, text, kb)


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
    if is_new:
        await send_sticker(message.from_user.id, "hello")
    if is_new and u["referrer"]:
        try:
            await main_bot.send_message(
                u["referrer"],
                f"🤝 По твоей ссылке пришёл новый пользователь! Когда он оплатит подписку, ты получишь +{REF_BONUS_DAYS} дн.",
            )
        except Exception:
            pass
    await show(message, "home", home_text(u), home_kb(u))


@main_r.message(Command("menu"))
async def cmd_menu(message: Message):
    u, _ = ensure_user(message.from_user)
    await show(message, "home", home_text(u), home_kb(u))


@main_r.message(Command("buy"))
async def cmd_buy(message: Message):
    ensure_user(message.from_user)
    await show(message, "plans", plans_text(), plans_kb())


@main_r.message(Command("key"))
async def cmd_key(message: Message):
    u, _ = ensure_user(message.from_user)
    text, kb = await key_text(u)
    await show(message, "key", text, kb)


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
    await edit_or_send(call, plans_text(), plans_kb(), "plans")
    await call.answer()


@main_r.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in PLANS:
        return await call.answer("Тариф не найден", show_alert=True)
    ensure_user(call.from_user)
    if len(methods()) == 1:
        return await start_payment(call, "stars", key)
    name, stars, _ = PLANS[key]
    rows = []
    for m in methods():
        price = f"{stars} ⭐" if m == "stars" else f"{RUB_PRICES[key]} ₽"
        rows.append([InlineKeyboardButton(text=f"{METHOD_NAMES[m]} — {price}", callback_data=f"pay:{m}:{key}")])
    rows.append([InlineKeyboardButton(text="« Другой тариф", callback_data="buy")])
    await edit_or_send(call, f"🧾 <b>Тариф: {name}</b>\n\nВыбери способ оплаты:", InlineKeyboardMarkup(inline_keyboard=rows), "pay")
    await call.answer()


@main_r.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery):
    _, method, key = call.data.split(":", 2)
    if key not in PLANS or method not in methods():
        return await call.answer("Способ оплаты недоступен", show_alert=True)
    await start_payment(call, method, key)


async def create_payment(method: str, key: str, uid: int) -> tuple[str, str | None]:
    """Создаёт счёт. Возвращает (ссылка, id_счёта или None для Stars)."""
    name, stars, _ = PLANS[key]
    if method == "stars":
        return await make_invoice_link(key, uid), None
    rub = RUB_PRICES[key]
    if method == "crypto":
        inv_id, url, _ = await crypto.create_invoice(rub, f"{key}:{uid}", f"KlyxVPN — {name}")
        db.add_invoice(f"cb_{inv_id}", uid, key, "crypto", rub, "RUB")
        return url, f"cb_{inv_id}"
    if method == "card":
        inv_id = f"ym_{uid}_{int(time.time())}"
        db.add_invoice(inv_id, uid, key, "card", rub, "RUB")
        return yoomoney.link(rub, inv_id, f"KlyxVPN — {name}", f"https://t.me/{MAIN_USERNAME}"), inv_id
    raise ValueError("unknown method")


async def start_payment(call: CallbackQuery, method: str, key: str):
    name, stars, _ = PLANS[key]
    try:
        link, inv_id = await create_payment(method, key, call.from_user.id)
    except Exception as e:
        log.error("Ошибка создания счёта (%s): %s", method, e)
        return await call.answer("Не получилось создать счёт, попробуй другой способ", show_alert=True)
    await call.answer()
    price = f"{stars} ⭐" if method == "stars" else f"{RUB_PRICES[key]} ₽"
    hints = {
        "stars": "Нажми кнопку ниже — откроется оплата звёздами.",
        "crypto": "Откроется @CryptoBot — оплати в USDT, TON, BTC или другой монете. Сумма считается по курсу.",
        "card": "Откроется страница ЮMoney — оплати картой любого банка РФ.",
    }
    rows = [[InlineKeyboardButton(text=f"Оплатить {price}", url=link)]]
    if inv_id:
        rows.append([InlineKeyboardButton(text="⏳ Я оплатил — проверить", callback_data=f"chk:{inv_id}")])
    rows.append([InlineKeyboardButton(text="« Другой способ", callback_data=f"plan:{key}")])
    await show(call, "pay",
               f"🧾 <b>{name}</b> — {price}\n{METHOD_NAMES[method]}\n\n{hints[method]}\nКлюч придёт сюда сразу после оплаты.",
               InlineKeyboardMarkup(inline_keyboard=rows))


@main_r.callback_query(F.data.startswith("chk:"))
async def cb_check(call: CallbackQuery):
    inv_id = call.data.split(":", 1)[1]
    inv = db.get_invoice(inv_id)
    if not inv:
        return await call.answer("Счёт не найден", show_alert=True)
    if inv["status"] == "paid":
        return await call.answer("✅ Оплата уже получена, ключ отправлен!", show_alert=True)
    if inv["provider"] == "crypto":
        try:
            paid = await crypto.paid_ids([inv_id[3:]])
        except Exception as e:
            log.warning("CryptoBot check: %s", e)
            paid = []
        if paid:
            await call.answer("✅ Оплата найдена!")
            return await fulfill_invoice(inv, paid[0].get("paid_asset", "crypto"))
    await call.answer("Оплата пока не поступила. Если только что оплатил — подожди минуту и нажми ещё раз.", show_alert=True)


@main_r.callback_query(F.data == "key")
async def cb_key(call: CallbackQuery):
    u, _ = ensure_user(call.from_user)
    await call.answer()
    text, kb = await key_text(u)
    await edit_or_send(call, text, kb, "key")


@main_r.callback_query(F.data == "guide")
async def cb_guide(call: CallbackQuery):
    await edit_or_send(call, "📱 <b>Как подключиться</b>\n\nВыбери своё устройство:", guide_kb(), "guide")
    await call.answer()


@main_r.callback_query(F.data.startswith("guide:"))
async def cb_guide_os(call: CallbackQuery):
    title, steps = GUIDES.get(call.data.split(":", 1)[1], GUIDES["ios"])
    text = f"<b>{title}</b>\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мой ключ", callback_data="key")],
        [InlineKeyboardButton(text="« Другое устройство", callback_data="guide")],
    ])
    await edit_or_send(call, text, kb, "guide")
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
        await send_sticker(u["id"], "key")
    if ok and link:
        text = (
            f"🎁 Пробный период на {TRIAL_DAYS} дн. активирован!\n\n"
            f"🔑 Твой ключ (нажми, чтобы скопировать):\n<code>{esc(link)}</code>\n\n"
            "Не знаешь, куда вставить? Жми «Как подключить» 👇"
        )
    else:
        text = f"🎁 Пробный период активирован, но ключ ещё готовится. Загляни в «🔑 Мой ключ» через пару минут или {support_text()}."
    await edit_or_send(call, text, back_kb([InlineKeyboardButton(text="📱 Как подключить", callback_data="guide")]), "trial")


@main_r.callback_query(F.data == "ref")
async def cb_ref(call: CallbackQuery):
    u, _ = ensure_user(call.from_user)
    text = (
        "🤝 <b>Приглашай друзей — получай дни бесплатно</b>\n\n"
        f"За каждого друга, который оплатит подписку, ты получишь <b>+{REF_BONUS_DAYS} дн.</b>\n\n"
        f"Твоя ссылка:\n<code>{ref_link(u['id'])}</code>\n\n"
        f"Приглашено: <b>{db.count_invited(u['id'])}</b> · бонусных дней: <b>{u['bonus_days']}</b>"
    )
    share = f"https://t.me/share/url?url={quote(ref_link(u['id']))}&text={quote('Быстрый VPN, которым пользуюсь сам 👇')}"
    await edit_or_send(call, text, back_kb([InlineKeyboardButton(text="🔗 Поделиться", url=share)]), "ref")
    await call.answer()


@main_r.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    await call.answer()
    await edit_or_send(
        call,
        f"💬 <b>Поддержка</b>\n\nПиши: {support_text()}\nОбычно отвечаем в течение пары часов.\n\n"
        "Перед вопросом проверь:\n• подписка активна (🔑 Мой ключ)\n• ключ вставлен целиком\n• в приложении включён VPN",
        back_kb(),
        "support",
    )


# ---------------- оплата (работает в любом из ботов) ----------------
@pay_r.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    key = query.invoice_payload.split(":", 1)[0]
    await query.answer(ok=key in PLANS, error_message=None if key in PLANS else "Тариф не найден, попробуй ещё раз")


async def fulfill_payment(uid: int, key: str, amount: int, currency: str, charge_id: str, via: str, payer: str = ""):
    """Зачисляет оплату: продлевает подписку, шлёт ключ, начисляет бонус пригласившему, сообщает админу."""
    if db.payment_exists(charge_id):
        return
    name, _, days = PLANS.get(key, ("?", 0, 30))
    if not db.get_user(uid):
        db.upsert_user(uid, None, None)
    first_payment = db.paid_count(uid) == 0
    db.add_payment(uid, key, amount, currency, charge_id)
    ok, link = await add_days(uid, days)
    u = db.get_user(uid)

    if ok and link:
        text = (
            f"🔥 <b>Оплата прошла!</b> Тариф: {name}\n"
            f"🗓 Подписка до <b>{fmt_date(u['expires_at'])}</b>\n\n"
            f"🔑 Твой ключ (нажми, чтобы скопировать):\n<code>{esc(link)}</code>\n\n"
            "Не знаешь, куда вставить? Жми «Как подключить» 👇"
        )
    else:
        text = (
            f"🔥 <b>Оплата прошла!</b> Подписка до <b>{fmt_date(u['expires_at'])}</b>.\n\n"
            f"Ключ готовится — он появится в «🔑 Мой ключ» через пару минут. Если нет — {support_text()}"
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Как подключить", callback_data="guide")],
        [InlineKeyboardButton(text="« В меню", callback_data="home")],
    ])
    await send_sticker(uid, "paid")
    try:
        await send_screen(uid, "success", text, kb)
    except Exception as e:
        log.warning("Не удалось отправить ключ %s: %s", uid, e)

    if first_payment and u["referrer"] and not u["ref_paid"] and REF_BONUS_DAYS:
        db.mark(uid, "ref_paid")
        db.add_bonus_days(u["referrer"], REF_BONUS_DAYS)
        await add_days(u["referrer"], REF_BONUS_DAYS)
        try:
            await send_sticker(u["referrer"], "friends")
            await main_bot.send_message(u["referrer"], f"🎉 Твой друг оплатил подписку — тебе +{REF_BONUS_DAYS} дн. KlyxVPN!")
        except Exception:
            pass

    cur = "⭐" if currency == "XTR" else ("₽" if currency == "RUB" else currency)
    extra = f"\nВозврат: <code>/refund {uid} {charge_id}</code>" if currency == "XTR" else ""
    who = esc(payer or (u["name"] or "")) + (f" (@{esc(u['username'])})" if u["username"] else "")
    await notify_admin(f"💰 <b>Оплата: {amount} {cur}</b> — {name} · {via}\nОт: {who} <code>{uid}</code>{extra}")


async def fulfill_invoice(inv, via: str):
    if not db.set_invoice_status(inv["id"], "paid"):
        return  # уже зачислено
    await fulfill_payment(inv["user_id"], inv["plan"], inv["amount"], inv["currency"], inv["id"], via)


@pay_r.message(F.successful_payment)
async def on_paid(message: Message, bot: Bot):
    sp = message.successful_payment
    key, _, uid_s = sp.invoice_payload.partition(":")
    uid = int(uid_s) if uid_s.isdigit() else message.from_user.id
    db.upsert_user(uid, message.from_user.username, message.from_user.full_name)
    await fulfill_payment(uid, key, sp.total_amount, sp.currency, sp.telegram_payment_charge_id, "Stars",
                          message.from_user.full_name)
    if bot is not main_bot:  # оплатили через платёжного бота — подскажем, куда идти
        await message.answer(f"✅ Оплата получена, спасибо! Ключ отправлен в @{MAIN_USERNAME}")


async def crypto_loop():
    """Каждые 15 секунд проверяет неоплаченные счета CryptoBot."""
    while True:
        try:
            if crypto.enabled:
                pend = db.pending_invoices("crypto", 2 * 3600)
                if pend:
                    paid = await crypto.paid_ids([p["id"][3:] for p in pend])
                    paid_ids = {f"cb_{x['invoice_id']}": x for x in paid}
                    for p in pend:
                        if p["id"] in paid_ids:
                            await fulfill_invoice(p, paid_ids[p["id"]].get("paid_asset", "crypto"))
        except Exception as e:
            log.error("crypto_loop: %s", e)
        await asyncio.sleep(15)


# ---------------- платёжный бот: всё остальное ----------------
@paybot_r.message()
async def paybot_any(message: Message):
    await message.answer(f"Это платёжный бот KlyxVPN 💳\nВсё управление подпиской — в @{MAIN_USERNAME}")


# ---------------- админка ----------------
class AdminSG(StatesGroup):
    give = State()
    broadcast = State()
    password = State()


ADMIN_SESSION_SEC = 12 * 3600
_admin_sessions: dict[int, float] = {}
_admin_fails: dict[int, list[float]] = {}


def admin_authed(uid: int) -> bool:
    return not ADMIN_PASSWORD or _admin_sessions.get(uid, 0) > time.time()


class AdminAuthed(BaseFilter):
    async def __call__(self, event) -> bool:
        return admin_authed(event.from_user.id)


admin_r.message.filter(AdminAuthed())
admin_r.callback_query.filter(AdminAuthed())


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
    "<code>/refund ID CHARGE_ID</code> — вернуть звёзды\n"
    "<code>/stickers</code> — создать стикерпак KlyxVPN\n"
    "<code>/icons</code> — включить свои иконки на кнопках (нужен Premium)\n"
    "<code>/logout</code> — выйти из админки"
)


@admin_login_r.message(Command("admin"))
async def adm_entry(message: Message, state: FSMContext):
    if admin_authed(message.from_user.id):
        return await adm_menu(message, state)
    await state.set_state(AdminSG.password)
    await message.answer("🔐 Введи пароль админки (сообщение с паролем я сразу удалю):")


@admin_login_r.message(AdminSG.password)
async def adm_password(message: Message, state: FSMContext):
    uid = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass
    fails = [t for t in _admin_fails.get(uid, []) if t > time.time() - 3600]
    if len(fails) >= 5:
        await state.clear()
        return await message.answer("⛔️ Слишком много неверных попыток. Попробуй через час.")
    if hmac.compare_digest((message.text or "").strip(), ADMIN_PASSWORD):
        _admin_sessions[uid] = time.time() + ADMIN_SESSION_SEC
        _admin_fails.pop(uid, None)
        await message.answer("✅ Пароль верный, вход на 12 часов.")
        return await adm_menu(message, state)
    fails.append(time.time())
    _admin_fails[uid] = fails
    await state.clear()
    await notify_admin(f"⚠️ Неверный пароль админки от <code>{uid}</code> (попытка {len(fails)}/5)")
    await message.answer("❌ Неверный пароль. Набери /admin, чтобы попробовать снова.")


@admin_login_r.message(Command("logout"))
async def adm_logout(message: Message, state: FSMContext):
    _admin_sessions.pop(message.from_user.id, None)
    await state.clear()
    await message.answer("🔒 Вышел из админки.")


async def adm_menu(message: Message, state: FSMContext):
    await state.clear()
    warn = "" if db.is_persistent() else "\n\n⚠️ <b>База не на Volume — данные сотрутся при перезапуске!</b>"
    await show(message, "admin", ADMIN_HELP + warn, admin_kb())


@admin_r.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery):
    s = db.stats()
    await call.answer()
    await call.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: <b>{s['users']}</b> (+{s['users_today']} за сутки)\n"
        f"🟠 Активных подписок: <b>{s['active']}</b>\n"
        f"🎁 Взяли пробный: {s['trials']}\n"
        f"💰 Оплат: <b>{s['payments']}</b>\n"
        f"⭐ Звёзд всего: <b>{s['stars']}</b> (за сутки: {s['stars_today']})\n"
        f"₽ Рублями (карта/крипта): <b>{s['rub']}</b> (за сутки: {s['rub_today']})",
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
        cur = "⭐" if p["currency"] == "XTR" else "₽"
        lines.append(f"{fmt_date(p['created_at'])} · {p['amount']} {cur} · {PLANS.get(p['plan'], ('?',))[0]} · {who} (<code>{p['user_id']}</code>){ref}")
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
            await send_screen(uid, "success", msg)
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


@admin_r.message(Command("stickers"))
async def cmd_stickers(message: Message):
    name = sticker_set_name()
    try:
        await main_bot.get_sticker_set(name)
        await load_stickers()
        return await message.answer(f"🦊 Стикерпак уже есть: https://t.me/addstickers/{name}")
    except Exception:
        pass
    files = [(n, e, os.path.join(BANNER_DIR, f"sticker_{n}.png")) for n, e in STICKERS]
    missing = [n for n, _, f in files if not os.path.exists(f)]
    if missing:
        return await message.answer(f"❌ Не хватает файлов: {', '.join('sticker_' + m + '.png' for m in missing)}")
    await message.answer("🦊 Создаю стикерпак…")
    try:
        await main_bot.create_new_sticker_set(
            user_id=message.from_user.id,
            name=name,
            title="KlyxVPN 🦊",
            stickers=[InputSticker(sticker=FSInputFile(f), format="static", emoji_list=[e]) for _, e, f in files],
        )
        await load_stickers()
        await message.answer(f"✅ Готово! Добавляй: https://t.me/addstickers/{name}")
        await send_sticker(message.from_user.id, "fire")
    except Exception as e:
        await message.answer(f"❌ Не получилось создать стикерпак: {esc(e)}", parse_mode="HTML")


@admin_r.message(Command("icons"))
async def cmd_icons(message: Message, command: CommandObject):
    if (command.args or "").strip() == "off":
        db.set_setting("icons", "0")
        _icon_ids.clear()
        return await message.answer("Иконки на кнопках выключены, вернулись обычные эмодзи.")
    name = icons_set_name()
    try:
        await main_bot.get_sticker_set(name)
    except Exception:
        def icon_file(i):  # анимированная .webm, если есть, иначе статичная .png
            for ext, fmt in (("webm", "video"), ("png", "static")):
                f = os.path.join(BANNER_DIR, f"icon_{i}.{ext}")
                if os.path.exists(f):
                    return f, fmt
            return None, None
        missing = [i for i in ICONS if not icon_file(i)[0]]
        if missing:
            return await message.answer(f"❌ Не хватает файлов: {', '.join('icon_' + m + '.webm' for m in missing)}")
        await message.answer("🎨 Создаю набор иконок…")
        try:
            await main_bot.create_new_sticker_set(
                user_id=message.from_user.id, name=name, title="KlyxVPN icons",
                sticker_type="custom_emoji",
                stickers=[InputSticker(sticker=FSInputFile(icon_file(i)[0]), format=icon_file(i)[1],
                                       emoji_list=[ICON_EMOJI[i]]) for i in ICONS],
            )
        except Exception as e:
            return await message.answer(f"❌ Не получилось создать иконки: {esc(e)}", parse_mode="HTML")
    db.set_setting("icons", "1")
    await load_icons()
    # проверяем, что Telegram разрешает иконки на кнопках (нужен Premium у владельца бота)
    try:
        await main_bot.send_message(message.chat.id, "🎨 Проверка иконок на кнопках:",
                                    reply_markup=decorate(InlineKeyboardMarkup(inline_keyboard=[
                                        [InlineKeyboardButton(text="🔥 Купить подписку", callback_data="buy")],
                                        [InlineKeyboardButton(text="🔑 Мой ключ", callback_data="key"),
                                         InlineKeyboardButton(text="💬 Поддержка", callback_data="support")]])))
        await message.answer("✅ Если видишь оранжевые иконки на кнопках выше — всё работает! Выключить: /icons off")
    except Exception as e:
        db.set_setting("icons", "0")
        _icon_ids.clear()
        await message.answer(
            "❌ Telegram не разрешил иконки на кнопках. Скорее всего, у владельца бота нет Telegram Premium.\n"
            f"Ошибка: {esc(e)}\nКнопки остаются с обычными эмодзи.", parse_mode="HTML")


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
                    await send_sticker(u["id"], "renew")
                    await send_screen(
                        u["id"], "plans",
                        f"⏳ Подписка KlyxVPN закончится <b>{fmt_date(u['expires_at'])}</b> (через {days_left(u)} дн.).\nПродли, чтобы VPN не отключился 👇",
                        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 Продлить", callback_data="buy")]]),
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
        "plans": [{"key": k, "name": n, "stars": s, "rub": RUB_PRICES[k], "days": d} for k, (n, s, d) in PLANS.items()],
        "methods": methods(),
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
    plan, method = body.get("plan"), body.get("method", "stars")
    if plan not in PLANS or method not in methods():
        return web.json_response({"error": "unknown plan or method"}, status=400)
    link, inv_id = await create_payment(method, plan, u["id"])
    return web.json_response({"link": link, "method": method, "invoice": inv_id})


async def api_yoomoney(request):
    """HTTP-уведомление от ЮMoney о поступлении оплаты."""
    form = dict(await request.post())
    if not yoomoney.enabled or not yoomoney.check(form):
        log.warning("ЮMoney: неверная подпись уведомления")
        return web.Response(status=400, text="bad sign")
    if form.get("codepro") == "true" or form.get("unaccepted") == "true":
        return web.Response(text="ok")
    inv = db.get_invoice(form.get("label", ""))
    if not inv:
        log.warning("ЮMoney: неизвестный label %s", form.get("label"))
        return web.Response(text="ok")
    paid = float(form.get("withdraw_amount") or form.get("amount") or 0)
    if paid + 1 < inv["amount"]:
        await notify_admin(f"⚠️ ЮMoney: по счёту <code>{inv['id']}</code> пришло {paid} ₽ вместо {inv['amount']} ₽ — не зачислено")
        return web.Response(text="ok")
    await fulfill_invoice(inv, "Карта (ЮMoney)")
    return web.Response(text="ok")


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
    app.router.add_post("/yoomoney", api_yoomoney)
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
    log.info("💳 Способы оплаты: %s", ", ".join(methods()))
    await load_stickers()
    await load_icons()
    log.info("🔐 Пароль админки: %s", "задан" if ADMIN_PASSWORD else "НЕ задан (добавь ADMIN_PASSWORD)")
    if yoomoney.enabled:
        log.info("📮 Адрес для уведомлений ЮMoney: %s/yoomoney", PUBLIC_URL or "(нужен PUBLIC_URL)")

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

    dp.include_routers(admin_login_r, admin_r, pay_r, main_r, paybot_r)
    await start_api()
    asyncio.create_task(reminders_loop())
    asyncio.create_task(crypto_loop())
    await dp.start_polling(*bots)


if __name__ == "__main__":
    asyncio.run(main())
