"""
KluxVPN — Telegram-бот + API для мини-апки.

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
import secrets
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    KeyboardButton,
    ReplyKeyboardMarkup,
    BufferedInputFile,
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
from payments import CryptoPay, Platega, YooMoney
from xui import XUI

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("klyxvpn")

# ---------------- настройки ----------------
env = lambda k, d="": (os.getenv(k) or d).strip()

BOT_TOKEN = env("BOT_TOKEN")
# Отдельный бот-«перенаправлялка»: на /start присылает сообщение с кнопкой-ссылкой (необязательно)
PROMO_BOT_TOKEN = env("PROMO_BOT_TOKEN")
PROMO_TEXT = env("PROMO_TEXT", "👋 Привет! Всё самое интересное — в нашем основном боте 👇")
PROMO_BUTTON = env("PROMO_BUTTON", "👉 Перейти в бота")
PROMO_URL = env("PROMO_URL")  # куда ведёт кнопка, например https://t.me/kluxvpn_bot
PROMO_PHOTO = env("PROMO_PHOTO")  # необязательно: ссылка на картинку для сообщения
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
platega = Platega(env("PLATEGA_MERCHANT_ID"), env("PLATEGA_SECRET"), env("PLATEGA_URL", "https://app.platega.io"))
SUPPORT_EMAIL = env("SUPPORT_EMAIL")
CODE_WORD = env("CODE_WORD")  # кодовое слово для проверки платёжкой (например PlaVER), потом удалить
DOCS_BASE = env("DOCS_URL") or (WEBAPP_URL.split("?")[0].rstrip("/") if WEBAPP_URL else "")
TERMS_URL = f"{DOCS_BASE}/terms.html" if DOCS_BASE else ""
PRIVACY_URL = f"{DOCS_BASE}/privacy.html" if DOCS_BASE else ""

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

CRYPTO_HELP = (
    "🪙 <b>Как оплатить криптой, если её нет</b>\n\n"
    "Это проще, чем кажется — 3–5 минут:\n\n"
    "<b>1.</b> Открой @CryptoBot → нажми <b>Start</b>\n"
    "<b>2.</b> Зайди в <b>Кошелёк</b> → <b>P2P-маркет</b> → <b>Купить</b> → выбери <b>USDT</b>\n"
    "<b>3.</b> Выбери продавца с оплатой <b>картой или СБП в рублях</b> (смотри на рейтинг и число сделок)\n"
    "<b>4.</b> Переведи рубли продавцу по реквизитам и нажми «Я оплатил» — USDT придут в кошелёк\n"
    "<b>5.</b> Вернись сюда и нажми <b>«Оплатить»</b> — счёт откроется в @CryptoBot, подтверди оплату\n\n"
    "🔸 Покупай чуть больше суммы счёта — курс может немного меняться\n"
    "🔸 Ключ придёт сюда автоматически сразу после оплаты\n"
    "🔸 Застрял? Жми «Поддержка» — поможем"
)

METHOD_NAMES = {"stars": "⭐ Telegram Stars", "sbp": "🏦 СБП (рубли)", "crypto": "🪙 Криптовалюта",
                "card": "💳 Картой (рубли)"}


def methods() -> list[str]:
    m = ["stars"]
    if platega.enabled:
        m.append("sbp")
    if crypto.enabled:
        m.append("crypto")
    if yoomoney.enabled:
        m.append("card")
    return m


main_bot = Bot(BOT_TOKEN)
pay_bot = Bot(PAY_BOT_TOKEN) if PAY_BOT_TOKEN and PAY_BOT_TOKEN != BOT_TOKEN else main_bot
promo_bot = Bot(PROMO_BOT_TOKEN) if PROMO_BOT_TOKEN and PROMO_BOT_TOKEN not in (BOT_TOKEN, PAY_BOT_TOKEN) else None
MAIN_USERNAME = ""
PAY_USERNAME = ""

dp = Dispatcher()
main_r = Router(name="main")
admin_r = Router(name="admin")
pay_r = Router(name="payments")
paybot_r = Router(name="paybot")
admin_login_r = Router(name="admin_login")
promo_r = Router(name="promo")

MSK = timezone(timedelta(hours=3))


# ---------------- бот-перенаправлялка ----------------
def promo_url() -> str:
    return PROMO_URL or (f"https://t.me/{MAIN_USERNAME}" if MAIN_USERNAME else "")


@promo_r.message()
async def promo_any(message: Message):
    """На /start и на любое сообщение — текст с кнопкой-ссылкой на другой бот."""
    url = promo_url()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=PROMO_BUTTON, url=url)]]) if url else None
    try:
        if PROMO_PHOTO:
            await message.answer_photo(PROMO_PHOTO, caption=PROMO_TEXT, reply_markup=kb)
        else:
            await message.answer(PROMO_TEXT, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.warning("promo bot: %s", e)


# ---------------- фильтры ----------------
class IsMainBot(BaseFilter):
    async def __call__(self, event, bot: Bot) -> bool:
        return bot.id == main_bot.id


class IsPayBot(BaseFilter):
    async def __call__(self, event, bot: Bot) -> bool:
        return pay_bot is not main_bot and bot.id == pay_bot.id


class IsPromoBot(BaseFilter):
    async def __call__(self, event, bot: Bot) -> bool:
        return promo_bot is not None and bot.id == promo_bot.id


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
promo_r.message.filter(IsPromoBot())
promo_r.callback_query.filter(IsPromoBot())


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


APP_VERSION = str(int(time.time()))


def webapp_url() -> str:
    if not WEBAPP_URL:
        return ""
    sep = "&" if "?" in WEBAPP_URL else "?"
    # v= меняется при каждом перезапуске бота — Telegram не будет показывать старую закэшированную мини-апку
    url = f"{WEBAPP_URL}{sep}v={APP_VERSION}"
    if PUBLIC_URL:
        url += f"&api={quote(PUBLIC_URL, safe='')}"
    return url


def ref_link(uid: int) -> str:
    return f"https://t.me/{MAIN_USERNAME}?start=ref{uid}"


def support_text() -> str:
    parts = []
    if SUPPORT:
        parts.append(f"@{SUPPORT}")
    if SUPPORT_EMAIL:
        parts.append(SUPPORT_EMAIL)
    return " · ".join(parts) or "напиши администратору"


def docs_row() -> list:
    row = []
    if TERMS_URL:
        row.append(InlineKeyboardButton(text="📄 Соглашение", url=TERMS_URL))
    if PRIVACY_URL:
        row.append(InlineKeyboardButton(text="🔒 Конфиденциальность", url=PRIVACY_URL))
    return row


async def notify_admin(text: str):
    try:
        await main_bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось написать админу: %s", e)


async def key_for(u) -> str | None:
    if not (u and u["uuid"] and xui.configured):
        return None
    try:
        return await xui.link(u["uuid"], u["id"], f"KluxVPN {SERVER_NAME}")
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
        title=f"KluxVPN — {name}",
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
        "⚡️ <b>KluxVPN</b> — быстрый и надёжный VPN\n\n"
        "🔸 Современное шифрование VLESS Reality — защищённое и стабильное подключение\n"
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
    if docs_row():
        rows.append(docs_row())
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
    return f"klyxvpn2_by_{MAIN_USERNAME}"


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


class MsgCall:
    """Позволяет вызывать обработчики кнопок из обычного сообщения (команды и нижнее меню)."""
    def __init__(self, message: Message):
        self.message = message
        self.from_user = message.from_user
        self.data = ""

    async def answer(self, text: str | None = None, show_alert: bool = False, **_):
        if text and show_alert:
            await self.message.answer(text)


async def edit_or_send(call, text: str, kb: InlineKeyboardMarkup, screen: str = "home"):
    await show(call.message if isinstance(call, MsgCall) else call, screen, text, kb)


# ---------- нижнее меню и список команд ----------
BTN_APP, BTN_BUY, BTN_KEY = "⚡️ Открыть приложение", "🔥 Купить VPN", "🔑 Мой ключ"
BTN_GUIDE, BTN_REF, BTN_TRIAL, BTN_SUPPORT = "📱 Подключить", "🤝 Друзья", "🎁 Бесплатно", "💬 Поддержка"


def reply_kb() -> ReplyKeyboardMarkup:
    rows = []
    if webapp_url():
        # обычная кнопка: из нижней клавиатуры Telegram не передаёт мини-апке данные пользователя
        rows.append([KeyboardButton(text=BTN_APP)])
    rows += [
        [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_KEY)],
        [KeyboardButton(text=BTN_GUIDE), KeyboardButton(text=BTN_REF)],
        [KeyboardButton(text=BTN_TRIAL), KeyboardButton(text=BTN_SUPPORT)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Выбери действие 👇")


USER_COMMANDS = [
    ("start", "🚀 Запустить бота"),
    ("menu", "🏠 Главное меню"),
    ("buy", "🔥 Купить подписку"),
    ("key", "🔑 Мой ключ"),
    ("trial", "🎁 Попробовать бесплатно"),
    ("guide", "📱 Как подключить"),
    ("ref", "🤝 Пригласить друга"),
    ("crypto", "🪙 Как оплатить криптой"),
    ("support", "💬 Поддержка"),
    ("terms", "📄 Соглашение и документы"),
    ("privacy", "🔒 Конфиденциальность"),
    ("help", "❓ Все команды"),
]
ADMIN_COMMANDS = [
    ("admin", "🛠 Админ-панель"),
    ("user", "👤 Карточка пользователя: /user ID"),
    ("give", "🎁 Выдать дни: /give ID дни"),
    ("sync", "🔄 Синхронизировать с панелью"),
    ("cancel", "✖️ Отменить действие"),
    ("logout", "🚪 Выйти из админки"),
]


async def setup_commands():
    user_cmds = [BotCommand(command=c, description=d) for c, d in USER_COMMANDS]
    try:
        await main_bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        await main_bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault(), language_code="ru")
        for aid in {ADMIN_ID, *ADMIN_IDS}:
            if aid:
                await main_bot.set_my_commands(
                    user_cmds + [BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                    scope=BotCommandScopeChat(chat_id=aid))
        log.info("✅ Команды бота обновлены")
    except Exception as e:
        log.warning("Не удалось поставить команды: %s", e)


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
        if db.get_setting("notify_new", "1") == "1":
            fu = message.from_user
            src = f" · пришёл от <code>{u['referrer']}</code>" if u["referrer"] else ""
            await notify_admin(
                f"🆕 Новый пользователь: <b>{esc(fu.full_name)}</b>" + (f" @{esc(fu.username)}" if fu.username else "")
                + f" <code>{fu.id}</code>{src}\nВсего: {db.stats()['users']}"
            )
    if is_new and u["referrer"]:
        try:
            await main_bot.send_message(
                u["referrer"],
                f"🤝 По твоей ссылке пришёл новый пользователь! Когда он оплатит подписку, ты получишь +{REF_BONUS_DAYS} дн.",
            )
        except Exception:
            pass
    await show(message, "home", home_text(u), home_kb(u))
    await message.answer("👇 Кнопки меню теперь всегда внизу экрана", reply_markup=reply_kb())


@main_r.message(Command("menu"))
async def cmd_menu(message: Message):
    u, _ = ensure_user(message.from_user)
    await show(message, "home", home_text(u), home_kb(u))
    await message.answer("👇 Меню внизу", reply_markup=reply_kb())


@main_r.message(Command("help"))
async def cmd_help(message: Message):
    lines = "\n".join(f"/{c} — {d}" for c, d in USER_COMMANDS)
    await message.answer(f"❓ <b>Команды KluxVPN</b>\n\n{lines}\n\nИли пользуйся кнопками внизу 👇",
                         parse_mode="HTML", reply_markup=reply_kb())


@main_r.message(Command("trial"))
@main_r.message(F.text == BTN_TRIAL)
async def msg_trial(message: Message):
    u, _ = ensure_user(message.from_user)
    if u["trial_used"] or not TRIAL_DAYS:
        return await message.answer("🎁 Пробный период уже использован. Посмотри тарифы — /buy")
    await cb_trial(MsgCall(message))


@main_r.message(Command("guide"))
@main_r.message(F.text == BTN_GUIDE)
async def msg_guide(message: Message):
    await cb_guide(MsgCall(message))


@main_r.message(Command("ref"))
@main_r.message(F.text == BTN_REF)
async def msg_ref(message: Message):
    await cb_ref(MsgCall(message))


@main_r.message(Command("support"))
@main_r.message(F.text == BTN_SUPPORT)
async def msg_support(message: Message):
    await cb_support(MsgCall(message))


@main_r.message(F.text == BTN_APP)
async def msg_app(message: Message):
    if not webapp_url():
        return await cmd_menu(message)
    await message.answer(
        "⚡️ Жми кнопку ниже — откроется приложение KluxVPN",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚡️ Открыть KluxVPN", web_app=WebAppInfo(url=webapp_url()))]]),
    )


@main_r.message(F.text == BTN_BUY)
async def msg_buy(message: Message):
    await cmd_buy(message)


@main_r.message(F.text == BTN_KEY)
async def msg_key(message: Message):
    await cmd_key(message)


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
    text = (
        "📄 <b>Документы KluxVPN</b>\n\n"
        "🔸 Пользовательское соглашение — условия, тарифы, оплата и возвраты\n"
        "🔸 Политика конфиденциальности — какие данные мы обрабатываем и зачем\n\n"
        f"💬 Поддержка: {support_text()}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[docs_row()] if docs_row() else [])
    await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@main_r.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await cmd_terms(message)


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
    hint = "\n\n🪙 Нет крипты? Не страшно — внутри будет пошаговая инструкция, как купить её за рубли." if "crypto" in methods() else ""
    await edit_or_send(call, f"🧾 <b>Тариф: {name}</b>\n\nВыбери способ оплаты:{hint}", InlineKeyboardMarkup(inline_keyboard=rows), "pay")
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
        inv_id, url, _ = await crypto.create_invoice(rub, f"{key}:{uid}", f"KluxVPN — {name}")
        db.add_invoice(f"cb_{inv_id}", uid, key, "crypto", rub, "RUB")
        return url, f"cb_{inv_id}"
    if method == "sbp":
        u = db.get_user(uid)
        back = f"https://t.me/{MAIN_USERNAME}"
        tid, url = await platega.create(rub, uid, f"KluxVPN — {name}", f"{key}:{uid}", back, back,
                                        user_name=(u["username"] or u["name"] or "") if u else "",
                                        client_ip=(u["app_ip"] or "") if u else "")
        db.add_invoice(f"pg_{tid}", uid, key, "sbp", rub, "RUB")
        return url, f"pg_{tid}"
    if method == "card":
        inv_id = f"ym_{uid}_{int(time.time())}"
        db.add_invoice(inv_id, uid, key, "card", rub, "RUB")
        return yoomoney.link(rub, inv_id, f"KluxVPN — {name}", f"https://t.me/{MAIN_USERNAME}"), inv_id
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
        "sbp": "Откроется страница оплаты: отсканируй QR-код или нажми ссылку — платёж откроется в приложении твоего банка (СБП).",
    }
    rows = [[InlineKeyboardButton(text=f"Оплатить {price}", url=link)]]
    if method == "crypto":
        rows.append([InlineKeyboardButton(text="❓ Как купить крипту за рубли", callback_data=f"cryptohelp:{key}")])
    if inv_id:
        rows.append([InlineKeyboardButton(text="⏳ Я оплатил — проверить", callback_data=f"chk:{inv_id}")])
    rows.append([InlineKeyboardButton(text="« Другой способ", callback_data=f"plan:{key}")])
    await show(call, "pay",
               f"🧾 <b>{name}</b> — {price}\n{METHOD_NAMES[method]}\n\n{hints[method]}\nКлюч придёт сюда сразу после оплаты.",
               InlineKeyboardMarkup(inline_keyboard=rows))


@main_r.callback_query(F.data.startswith("cryptohelp:"))
async def cb_cryptohelp(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Открыть @CryptoBot", url="https://t.me/CryptoBot")],
        [InlineKeyboardButton(text="🔥 Перейти к оплате", callback_data=f"pay:crypto:{key}" if key in PLANS else "buy")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="support")],
    ])
    await show(call, "pay", CRYPTO_HELP, kb)
    await call.answer()


@main_r.message(Command("crypto"))
async def cmd_crypto(message: Message):
    await show(message, "pay", CRYPTO_HELP, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Открыть @CryptoBot", url="https://t.me/CryptoBot")],
        [InlineKeyboardButton(text="🔥 Выбрать тариф", callback_data="buy")],
    ]))


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
    if inv["provider"] == "sbp":
        try:
            st = await platega.status(inv_id[3:])
        except Exception as e:
            log.warning("Platega check: %s", e)
            st = ""
        if st == "CONFIRMED":
            await call.answer("✅ Оплата найдена!")
            return await fulfill_invoice(inv, "СБП (Platega)")
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
        "Перед вопросом проверь:\n• подписка активна (🔑 Мой ключ)\n• ключ вставлен целиком\n• в приложении включён VPN"
        + (f"\n\n🔖 Кодовое слово: <code>{esc(CODE_WORD)}</code>" if CODE_WORD else ""),
        back_kb(*([docs_row()] if docs_row() else [])),
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
            await main_bot.send_message(u["referrer"], f"🎉 Твой друг оплатил подписку — тебе +{REF_BONUS_DAYS} дн. KluxVPN!")
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


async def platega_loop():
    """Каждые 20 секунд перепроверяет неоплаченные счета Platega (на случай, если колбэк не дошёл)."""
    while True:
        try:
            if platega.enabled:
                for p in db.pending_invoices("sbp", 2 * 3600):
                    try:
                        st = await platega.status(p["id"][3:])
                    except Exception as e:
                        log.warning("platega status %s: %s", p["id"], e)
                        continue
                    if st == "CONFIRMED":
                        await fulfill_invoice(p, "СБП (Platega)")
                    elif st in ("CANCELED", "CANCELLED", "EXPIRED", "FAILED", "CHARGEBACKED"):
                        db.set_invoice_status(p["id"], st.lower())
        except Exception as e:
            log.error("platega_loop: %s", e)
        await asyncio.sleep(20)


# ---------------- платёжный бот: всё остальное ----------------
@paybot_r.message()
async def paybot_any(message: Message):
    await message.answer(f"Это платёжный бот KluxVPN 💳\nВсё управление подпиской — в @{MAIN_USERNAME}")


# ---------------- админка ----------------
class AdminSG(StatesGroup):
    give = State()
    broadcast = State()
    password = State()
    find = State()
    write = State()


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
        [b("👥 Пользователи", "adm:ul:all:0"), b("🔎 Найти", "adm:find")],
        [b("🎁 Выдать дни", "adm:give"), b("📢 Рассылка", "adm:bc")],
        [b("🕵️ Шаринг ключей", "adm:share"), b("🌍 Страны", "adm:countries")],
        [b("📥 Выгрузить CSV", "adm:csv"), b("🖥 Проверить сервер", "adm:srv")],
        [b(("🔔" if db.get_setting("notify_new", "1") == "1" else "🔕") + " Уведомления о новых", "adm:notify")],
    ])


ADMIN_HELP = (
    "🛠 <b>Админ-панель KluxVPN</b>\n\n"
    "Команды:\n"
    "<code>/user ID</code> — инфо о пользователе\n"
    "<code>/give ID ДНИ</code> — выдать/продлить подписку\n"
    "<code>/sync ID</code> — повторно отправить ключ в панель\n"
    "<code>/refund ID CHARGE_ID</code> — вернуть звёзды\n"
    "<code>/stickers</code> — создать стикерпак KluxVPN\n"
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
    gs = db.game_stats(game_today())
    ws = db.wheel_stats(game_today())
    await call.answer()
    c = db.count_users
    b = lambda t, d: InlineKeyboardButton(text=t, callback_data=d)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [b(f"🟠 Активные ({c('active')})", "adm:ul:active:0"), b(f"🆕 Новые ({c('new')})", "adm:ul:new:0")],
        [b(f"💰 Платившие ({c('paid')})", "adm:ul:paid:0"), b(f"🎁 Пробный ({c('trial')})", "adm:ul:trial:0")],
        [b(f"⌛️ Кончилась ({c('expired')})", "adm:ul:expired:0"), b(f"⚫️ Без подписки ({c('none')})", "adm:ul:none:0")],
        [b(f"👥 Все ({c('all')})", "adm:ul:all:0")],
    ])
    await call.message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: <b>{s['users']}</b> (+{s['users_today']} за сутки)\n"
        f"🟠 Активных подписок: <b>{s['active']}</b>\n"
        f"🎁 Взяли пробный: {s['trials']}\n"
        f"💰 Оплат: <b>{s['payments']}</b>\n"
        f"⭐ Звёзд всего: <b>{s['stars']}</b> (за сутки: {s['stars_today']})\n"
        f"₽ Рублями (карта/крипта): <b>{s['rub']}</b> (за сутки: {s['rub_today']})\n"
        f"🎮 Игра сегодня: {gs['players']} игроков · {gs['rounds']} раундов · +{gs['minutes']} мин выдано\n"
        f"🎡 Колесо сегодня: {ws['spins']} прокруток · +{ws['minutes']} мин выдано\n\n"
        "👇 Нажми, чтобы увидеть конкретных людей:",
        reply_markup=kb,
        parse_mode="HTML",
    )


# ---------------- список пользователей ----------------
PAGE = 8


def user_status(u) -> str:
    if is_active(u):
        return f"🟠 до {fmt_date(u['expires_at'])}"
    if u["expires_at"]:
        return f"⌛️ кончилась {fmt_date(u['expires_at'])}"
    return "🎁 пробный" if u["trial_used"] else "⚫️ нет"


def user_line(u) -> str:
    who = esc(u["name"] or "без имени")
    un = f" @{esc(u['username'])}" if u["username"] else ""
    money = []
    if u["stars_sum"]:
        money.append(f"{u['stars_sum']}⭐")
    if u["rub_sum"]:
        money.append(f"{u['rub_sum']}₽")
    paid = f" · 💰 {' + '.join(money)}" if money else ""
    return f"<b>{who}</b>{un} <code>{u['id']}</code>\n    {user_status(u)}{paid}"


# ---------------- гео по IP (ip-api.com, бесплатно, без ключа) ----------------
_geo_cache: dict[str, dict] = {}
PLATFORMS = {"ios": "📱 iPhone/iPad", "android": "🤖 Android", "android_x": "🤖 Android", "tdesktop": "💻 Telegram Desktop",
             "macos": "💻 macOS", "weba": "🌐 Telegram Web", "webk": "🌐 Telegram Web", "web": "🌐 Telegram Web",
             "unigram": "💻 Windows (Unigram)"}


def flag(cc: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper()) if cc and len(cc) == 2 else "🌍"


async def geo(ips: list[str]) -> dict[str, dict]:
    need = [ip for ip in ips if ip not in _geo_cache and is_public_ip(ip)]
    if need:
        import aiohttp
        try:  # основной сервис: ip-api.com (до 100 адресов за раз)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                async with sess.post("http://ip-api.com/batch?fields=query,status,country,countryCode,city,isp&lang=ru",
                                     json=[{"query": ip} for ip in need[:100]]) as r:
                    for g in await r.json(content_type=None):
                        if g.get("status") == "success":
                            _geo_cache[g["query"]] = g
        except Exception as e:
            log.warning("geo ip-api: %s", e)
        for ip in [ip for ip in need if ip not in _geo_cache][:20]:  # запасной: ipwho.is
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as sess:
                    async with sess.get(f"https://ipwho.is/{ip}?lang=ru") as r:
                        d = await r.json(content_type=None)
                if d.get("success"):
                    _geo_cache[ip] = {"country": d.get("country"), "countryCode": d.get("country_code"),
                                      "city": d.get("city"), "isp": (d.get("connection") or {}).get("isp")}
            except Exception as e:
                log.warning("geo ipwho: %s", e)
    return {ip: _geo_cache.get(ip, {}) for ip in ips}


def geo_line(ip: str, g: dict) -> str:
    if not is_public_ip(ip):
        return f"🌍 <code>{ip}</code> — внутренний адрес, страну не определить"
    if not g:
        return f"🌍 <code>{ip}</code> — страна не определилась"
    place = ", ".join(x for x in (g.get("country"), g.get("city")) if x)
    return f"{flag(g.get('countryCode', ''))} <code>{ip}</code> — {esc(place)}" + (f" · {esc(g['isp'])}" if g.get("isp") else "")


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024
    return f"{n:.1f} ПБ"


async def connections_block(u) -> str:
    """Блок «Подключения»: устройства VPN сейчас, трафик, последний вход в мини-апку."""
    lines = []
    if u["uuid"] and xui.configured:
        try:
            ips = await xui.client_ips(u["id"])
            tr = await xui.client_traffic(u["id"])
        except Exception as e:
            ips, tr = [], {}
            lines.append(f"⚠️ Панель не ответила: {esc(e)}")
        g = await geo(ips) if ips else {}
        warn = " ⚠️ <b>больше лимита — похоже, делится ключом!</b>" if len(ips) > DEVICES else ""
        lines.append(f"📡 <b>Устройства VPN: {len(ips)} из {DEVICES}</b>{warn}")
        lines += [f"   {geo_line(ip, g.get(ip, {}))}" for ip in ips] or ["   сейчас не подключён (или в панели выключен журнал IP)"]
        if tr:
            lines.append(f"📶 Трафик: ↑ {fmt_bytes(tr.get('up'))} · ↓ {fmt_bytes(tr.get('down'))}")
    if u["last_seen"]:
        dev = PLATFORMS.get(u["platform"] or "", u["platform"] or "?")
        app = f"🕹 Мини-апка: {dev} · {datetime.fromtimestamp(u['last_seen'], MSK).strftime('%d.%m %H:%M')}"
        if u["app_ip"]:
            g = (await geo([u["app_ip"]])).get(u["app_ip"], {})
            app += f"\n   {geo_line(u['app_ip'], g)}"
        lines.append(app)
    return "\n".join(lines)


async def render_user_list(call: CallbackQuery, flt: str, page: int):
    total = db.count_users(flt)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    users = db.list_users(flt, page * PAGE, PAGE)
    title = db.USER_FILTERS.get(flt, db.USER_FILTERS["all"])[0]
    lines = [f"👥 <b>{title}</b> — {total} чел. · стр. {page + 1}/{pages}\n"]
    lines += [f"{page * PAGE + i + 1}. {user_line(u)}" for i, u in enumerate(users)] or ["Пока никого 🤷"]
    b = lambda t, d: InlineKeyboardButton(text=t, callback_data=d)
    rows = []
    for i in range(0, len(users), 2):
        rows.append([b(f"👤 {(u['name'] or u['username'] or u['id'])}"[:28], f"adm:u:{u['id']}:{flt}:{page}") for u in users[i:i + 2]])
    nav = []
    if page > 0:
        nav.append(b("◀️", f"adm:ul:{flt}:{page - 1}"))
    if page < pages - 1:
        nav.append(b("▶️", f"adm:ul:{flt}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([b(f"{'• ' if k == flt else ''}{v[0]}", f"adm:ul:{k}:0") for k, v in list(db.USER_FILTERS.items())[:3]])
    rows.append([b(f"{'• ' if k == flt else ''}{v[0]}", f"adm:ul:{k}:0") for k, v in list(db.USER_FILTERS.items())[3:6]])
    rows.append([b("🔎 Найти", "adm:find"), b("📥 CSV", "adm:csv")])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_r.callback_query(F.data.startswith("adm:ul:"))
async def adm_user_list(call: CallbackQuery):
    _, _, flt, page = call.data.split(":")
    await call.answer()
    await render_user_list(call, flt, int(page))


async def user_card(uid: int, back: str = "all:0"):
    u = db.user_full(uid)
    if not u:
        return "Пользователь не найден", None
    pays = db.user_payments(uid)
    plines = "\n".join(
        f"   • {fmt_date(p['created_at'])} — {p['amount']} {'⭐' if p['currency'] == 'XTR' else '₽'} "
        f"({PLANS.get(p['plan'], ('?',))[0]}){' ↩️' if p['refunded'] else ''}" for p in pays
    ) or "   нет"
    ref = f"<code>{u['referrer']}</code>" if u["referrer"] else "—"
    text = (
        f"👤 <b>{esc(u['name'] or 'без имени')}</b>" + (f" @{esc(u['username'])}" if u["username"] else "") + "\n"
        f"🆔 <code>{u['id']}</code> · с нами с {fmt_date(u['created_at'])}\n\n"
        f"📶 Подписка: {user_status(u)}" + (f" (осталось {days_left(u)} дн.)" if is_active(u) else "") + "\n"
        f"🔑 Ключ в панели: {'есть' if u['uuid'] else 'нет'} · 🎁 пробный: {'брал' if u['trial_used'] else 'нет'}\n"
        f"💰 Оплат: <b>{u['pays']}</b> · ⭐ {u['stars_sum']} · ₽ {u['rub_sum']}\n"
        f"🤝 Пригласил: {u['invited_cnt']} · бонусных дней: {u['bonus_days']} · пришёл от: {ref}\n\n"
        f"🧾 <b>Последние оплаты:</b>\n{plines}"
    )
    conn = await connections_block(u)
    if conn:
        text += "\n\n" + conn
    b = lambda t, d: InlineKeyboardButton(text=t, callback_data=d)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [b("+3 дня", f"adm:ud:{uid}:3:{back}"), b("+7 дней", f"adm:ud:{uid}:7:{back}"), b("+30 дней", f"adm:ud:{uid}:30:{back}")],
        [b("−7 дней", f"adm:ud:{uid}:-7:{back}"), b("⛔️ Отключить", f"adm:uoff:{uid}:{back}")],
        [b("🔑 Отправить ключ заново", f"adm:usync:{uid}:{back}"), b("🔄 Обновить", f"adm:u:{uid}:{back}")],
        [b("✉️ Написать ему от бота", f"adm:uw:{uid}")],
        [b("« К списку", f"adm:ul:{back}")],
    ])
    return text, kb


async def show_user_card(call: CallbackQuery, uid: int, back: str):
    text, kb = await user_card(uid, back)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_r.callback_query(F.data.startswith("adm:u:"))
async def adm_user(call: CallbackQuery):
    _, _, uid, flt, page = call.data.split(":")
    await call.answer()
    await show_user_card(call, int(uid), f"{flt}:{page}")


@admin_r.callback_query(F.data.startswith("adm:ud:"))
async def adm_user_days(call: CallbackQuery):
    _, _, uid, days, flt, page = call.data.split(":")
    uid, days = int(uid), int(days)
    ok, link = await add_days(uid, days)
    await call.answer(f"{'✅' if ok else '⚠️'} {'+' if days > 0 else ''}{days} дн.", show_alert=not ok)
    if days > 0:
        u = db.get_user(uid)
        msg = f"🎁 Тебе начислено <b>+{days} дн.</b> KluxVPN! Подписка до {fmt_date(u['expires_at'])}"
        if link:
            msg += f"\n\n🔑 Ключ:\n<code>{esc(link)}</code>"
        try:
            await send_screen(uid, "success", msg)
        except Exception:
            pass
    await show_user_card(call, uid, f"{flt}:{page}")


@admin_r.callback_query(F.data.startswith("adm:uoff:"))
async def adm_user_off(call: CallbackQuery):
    _, _, uid, flt, page = call.data.split(":")
    uid = int(uid)
    db.set_expires(uid, int(time.time()) - 1)
    try:
        await sync_user(uid)
        await call.answer("⛔️ Подписка отключена")
    except Exception as e:
        await call.answer(f"В базе отключено, панель: {e}"[:190], show_alert=True)
    await show_user_card(call, uid, f"{flt}:{page}")


@admin_r.callback_query(F.data.startswith("adm:usync:"))
async def adm_user_sync(call: CallbackQuery):
    _, _, uid, flt, page = call.data.split(":")
    uid = int(uid)
    try:
        await sync_user(uid)
        link = await key_for(db.get_user(uid))
        if link:
            await main_bot.send_message(uid, f"🔑 Твой ключ KluxVPN:\n<code>{esc(link)}</code>", parse_mode="HTML")
        await call.answer("✅ Ключ отправлен")
    except Exception as e:
        await call.answer(f"❌ {e}"[:190], show_alert=True)


@admin_r.callback_query(F.data.startswith("adm:uw:"))
async def adm_user_write(call: CallbackQuery, state: FSMContext):
    uid = int(call.data.split(":")[2])
    await state.set_state(AdminSG.write)
    await state.update_data(write_to=uid)
    await call.answer()
    await call.message.answer(f"✉️ Напиши сообщение для <code>{uid}</code> (текст, фото — что угодно). Оно уйдёт от имени бота.\n/cancel — отмена", parse_mode="HTML")


@admin_r.message(AdminSG.write)
async def adm_user_write_send(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid = data.get("write_to")
    try:
        await main_bot.copy_message(uid, message.chat.id, message.message_id)
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ Не доставлено: {esc(e)}", parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:find")
async def adm_find(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSG.find)
    await call.answer()
    await call.message.answer("🔎 Пришли ID, @юзернейм или часть имени\n/cancel — отмена")


@admin_r.message(AdminSG.find)
async def adm_find_input(message: Message, state: FSMContext):
    await state.clear()
    users = db.search_users(message.text or "")
    if not users:
        return await message.answer("Никого не нашёл 🤷")
    if len(users) == 1:
        text, kb = await user_card(users[0]["id"])
        return await message.answer(text, reply_markup=kb, parse_mode="HTML")
    b = lambda t, d: InlineKeyboardButton(text=t, callback_data=d)
    lines = [f"🔎 Найдено: {len(users)}\n"] + [f"{i + 1}. {user_line(u)}" for i, u in enumerate(users)]
    rows = [[b(f"👤 {(u['name'] or u['username'] or u['id'])}"[:28], f"adm:u:{u['id']}:all:0")] for u in users]
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:share")
async def adm_share(call: CallbackQuery):
    """Проверяет всех активных: у кого устройств больше лимита."""
    if not xui.configured:
        return await call.answer("Панель не настроена", show_alert=True)
    await call.answer("Проверяю всех активных…")
    ids = db.active_user_ids()
    msg = await call.message.answer(f"🕵️ Проверяю {len(ids)} активных ключей…")
    found = []
    for uid in ids:
        try:
            ips = await xui.client_ips(uid)
        except Exception:
            continue
        if len(ips) > DEVICES:
            found.append((uid, ips))
    if not found:
        return await msg.edit_text(f"✅ Всё чисто: ни у кого из {len(ids)} нет больше {DEVICES} устройств.")
    lines = [f"⚠️ <b>Больше {DEVICES} устройств: {len(found)}</b>\n"]
    rows = []
    for uid, ips in found[:30]:
        u = db.get_user(uid)
        g = await geo(ips)
        countries = " ".join(sorted({flag(g.get(ip, {}).get("countryCode", "")) for ip in ips}))
        lines.append(f"• <b>{esc(u['name'] or uid)}</b> <code>{uid}</code> — {len(ips)} устр. {countries}")
        rows.append([InlineKeyboardButton(text=f"👤 {(u['name'] or str(uid))}"[:28], callback_data=f"adm:u:{uid}:active:0")])
    await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:countries")
async def adm_countries(call: CallbackQuery):
    """Страны пользователей по последнему входу в мини-апку и по подключениям VPN."""
    await call.answer("Считаю…")
    users = db.all_users_full()
    ips = {u["id"]: u["app_ip"] for u in users if u["app_ip"]}
    g = await geo(list(set(ips.values())))
    by_country: dict[str, int] = {}
    for uid, ip in ips.items():
        c = g.get(ip, {})
        key = f"{flag(c.get('countryCode', ''))} {c.get('country') or 'Неизвестно'}"
        by_country[key] = by_country.get(key, 0) + 1
    unknown = len(users) - len(ips)
    lines = ["🌍 <b>Откуда пользователи</b> (по последнему входу в мини-апку)\n"]
    lines += [f"{k} — <b>{v}</b>" for k, v in sorted(by_country.items(), key=lambda x: -x[1])] or ["Пока нет данных"]
    if unknown:
        lines.append(f"\n❔ Ещё не открывали мини-апку: {unknown}")
    await call.message.answer("\n".join(lines), parse_mode="HTML")


@admin_r.callback_query(F.data == "adm:csv")
async def adm_csv(call: CallbackQuery):
    import csv
    import io

    await call.answer("Готовлю файл…")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "username", "имя", "зарегистрирован", "подписка до", "активна", "пробный", "оплат", "звёзд", "рублей",
                "пригласил", "пришёл от", "устройство", "IP мини-апки", "последний вход"])
    for u in db.all_users_full():
        w.writerow([u["id"], u["username"] or "", u["name"] or "", fmt_date(u["created_at"]),
                    fmt_date(u["expires_at"]) if u["expires_at"] else "", "да" if is_active(u) else "нет",
                    "да" if u["trial_used"] else "нет", u["pays"], u["stars_sum"], u["rub_sum"], u["invited_cnt"], u["referrer"] or "",
                    PLATFORMS.get(u["platform"] or "", u["platform"] or ""), u["app_ip"] or "",
                    datetime.fromtimestamp(u["last_seen"], MSK).strftime("%d.%m.%Y %H:%M") if u["last_seen"] else ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel открыл кириллицу
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"kluxvpn_users_{datetime.now(MSK).strftime('%Y-%m-%d')}.csv"),
        caption=f"📥 Все пользователи: {len(db.all_user_ids())}",
    )


@admin_r.callback_query(F.data == "adm:notify")
async def adm_notify(call: CallbackQuery, state: FSMContext):
    on = db.get_setting("notify_new", "1") != "1"
    db.set_setting("notify_new", "1" if on else "0")
    await call.answer("🔔 Уведомления о новых пользователях включены" if on else "🔕 Уведомления выключены", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=decorate(admin_kb()))
    except Exception:
        pass


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
            msg = f"🎁 Тебе начислено <b>+{days} дн.</b> KluxVPN! Подписка до {fmt_date(u['expires_at'])}"
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
            title="KluxVPN 🦊",
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
                user_id=message.from_user.id, name=name, title="KluxVPN icons",
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
            await main_bot.send_message(uid, f"🔑 Твой ключ KluxVPN:\n<code>{esc(link)}</code>", parse_mode="HTML")
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
async def clear_ips_loop():
    """Раз в сутки очищает в панели журнал IP подключений (храним минимум данных)."""
    await asyncio.sleep(600)
    while True:
        try:
            if xui.configured:
                for uid in db.active_user_ids():
                    try:
                        await xui.clear_ips(uid)
                    except Exception:
                        pass
                log.info("🧹 Журнал IP подключений очищен")
        except Exception as e:
            log.error("clear_ips_loop: %s", e)
        await asyncio.sleep(24 * 3600)


async def reminders_loop():
    while True:
        try:
            for u in db.expiring_soon(3 * 86400):
                db.mark(u["id"], "notified")
                try:
                    await send_sticker(u["id"], "renew")
                    await send_screen(
                        u["id"], "plans",
                        f"⏳ Подписка KluxVPN закончится <b>{fmt_date(u['expires_at'])}</b> (через {days_left(u)} дн.).\nПродли, чтобы VPN не отключился 👇",
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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data, X-Platform"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


_ip_headers_logged = False


def is_public_ip(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10"):  # внутренняя сеть Railway / CGNAT
        return False
    return not (a.is_private or a.is_loopback or a.is_reserved or a.is_link_local or a.is_multicast)


def client_ip(request) -> str:
    """Настоящий IP пользователя: берём первый ПУБЛИЧНЫЙ адрес из заголовков прокси Railway."""
    global _ip_headers_logged
    cands = []
    for h in ("CF-Connecting-IP", "X-Real-IP", "X-Envoy-External-Address", "X-Forwarded-For", "Forwarded"):
        v = request.headers.get(h, "")
        if h == "Forwarded":
            v = ",".join(p.split("for=", 1)[1].split(";")[0].strip('"[]') for p in v.split(",") if "for=" in p)
        cands += [x.strip().split("%")[0] for x in v.split(",") if x.strip()]
    if not _ip_headers_logged:
        _ip_headers_logged = True
        log.info("IP-заголовки: %s | remote=%s", {h: request.headers.get(h) for h in
                 ("X-Real-IP", "X-Forwarded-For", "X-Envoy-External-Address", "Forwarded", "CF-Connecting-IP")}, request.remote)
    for ip in cands:
        if is_public_ip(ip):
            return ip
    return ""


def api_user(request):
    tg = check_init_data(request.headers.get("X-Init-Data", ""))
    if not tg or "id" not in tg:
        raise web.HTTPUnauthorized(text='{"error":"bad init data"}')
    name = " ".join(x for x in [tg.get("first_name"), tg.get("last_name")] if x)
    db.upsert_user(tg["id"], tg.get("username"), name)
    ip = client_ip(request)
    platform = (request.headers.get("X-Platform") or "")[:20]
    try:
        db.touch(tg["id"], platform, ip)
    except Exception:
        pass
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
        "support_email": SUPPORT_EMAIL,
        "bot": MAIN_USERNAME,
        "game": game_info(u),
        "wheel": wheel_info(u),
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


async def api_platega(request):
    """Колбэк Platega. Тело не доверяем — статус перепроверяем запросом в API Platega."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    tid = str(body.get("id") or body.get("transactionId") or body.get("TransactionId") or "")
    inv = db.get_invoice(f"pg_{tid}") if tid else None
    if not inv:
        log.warning("Platega callback: неизвестная транзакция %s", tid)
        return web.json_response({"ok": True})
    try:
        st = await platega.status(tid)
    except Exception as e:
        log.error("Platega callback status: %s", e)
        return web.json_response({"ok": False}, status=500)  # пусть Platega повторит
    if st == "CONFIRMED":
        await fulfill_invoice(inv, "СБП (Platega)")
    elif st in ("CANCELED", "CANCELLED", "EXPIRED", "FAILED"):
        db.set_invoice_status(inv["id"], st.lower())
    elif st == "CHARGEBACKED":
        await notify_admin(f"⚠️ Platega: чарджбэк по платежу <code>{tid}</code> пользователя <code>{inv['user_id']}</code>")
    return web.json_response({"ok": True})


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


# ---------------- мини-игра «Взлом сервера» ----------------
GAME_ON = env("GAME_ON", "1") == "1"
GAME_FOR_ALL = env("GAME_FOR_ALL", "0") == "1"      # 1 — играть могут и без подписки
GAME_ROUNDS = int(env("GAME_ROUNDS", "3"))           # раундов в день
GAME_DAILY_MIN = int(env("GAME_DAILY_MIN", "120"))   # максимум минут в день
GAME_ROUND_MIN = int(env("GAME_ROUND_MIN", "40"))    # максимум минут за раунд
GAME_SECONDS = 30
GAME_MAX_SCORE = 300                                 # потолок очков за один взлом
_game_sessions: dict[str, tuple[int, float]] = {}


def game_today() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def game_info(u) -> dict:
    d = db.game_day(u["id"], game_today())
    allowed = GAME_ON and (GAME_FOR_ALL or is_active(u))
    reason = "" if allowed else ("off" if not GAME_ON else "need_sub")
    return {
        "allowed": allowed, "reason": reason,
        "rounds_left": max(0, GAME_ROUNDS - d["rounds"]), "rounds": GAME_ROUNDS,
        "minutes_today": d["minutes"], "daily_cap": GAME_DAILY_MIN, "round_cap": GAME_ROUND_MIN,
        "best": d["best"], "seconds": GAME_SECONDS,
    }


async def add_seconds(uid: int, seconds: int):
    u = db.get_user(uid)
    base = max(int(time.time()), u["expires_at"] or 0)
    db.set_expires(uid, base + seconds)
    try:
        await sync_user(uid)
    except Exception as e:
        log.error("Игра: панель не обновилась для %s: %s", uid, e)


async def api_game_start(request):
    u = api_user(request)
    g = game_info(u)
    if not g["allowed"]:
        return web.json_response({"error": "Игра доступна с активной подпиской"}, status=403)
    if g["rounds_left"] <= 0 or g["minutes_today"] >= GAME_DAILY_MIN:
        return web.json_response({"error": "На сегодня всё — приходи завтра 🧡"}, status=429)
    for t, (uid, ts) in list(_game_sessions.items()):  # чистим старые сессии
        if uid == u["id"] or time.time() - ts > 600:
            _game_sessions.pop(t, None)
    db.game_start_round(u["id"], game_today())
    token = secrets.token_urlsafe(16)
    _game_sessions[token] = (u["id"], time.time())
    return web.json_response({"token": token, "seconds": GAME_SECONDS, "game": game_info(db.get_user(u["id"]))})


async def api_game_finish(request):
    u = api_user(request)
    body = await request.json()
    sess = _game_sessions.pop(str(body.get("token", "")), None)
    if not sess or sess[0] != u["id"]:
        return web.json_response({"error": "Раунд не найден, начни заново"}, status=400)
    elapsed = time.time() - sess[1]
    try:
        score = int(body.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    # «Взлом сервера»: раунд идёт до 3 промахов; слой физически нельзя взять быстрее ~0,3 с
    if elapsed < 2 or elapsed > 900 or score < 0 or score > GAME_MAX_SCORE or score > elapsed * 8 + 4:
        log.warning("Игра: подозрительный результат %s: score=%s за %.1fс", u["id"], score, elapsed)
        score = 0
    d = db.game_day(u["id"], game_today())
    minutes = max(0, min(score, GAME_ROUND_MIN, GAME_DAILY_MIN - d["minutes"]))
    db.game_add_minutes(u["id"], game_today(), minutes, score)
    if minutes:
        await add_seconds(u["id"], minutes * 60)
    u = db.get_user(u["id"])
    return web.json_response({"score": score, "minutes": minutes, "game": game_info(u), "me": await me_payload(u),
                              "rank": db.game_rank(u["id"], game_today())})


# ---------------- колесо удачи и серия дней ----------------
# (минуты, вес) — чем больше вес, тем чаще выпадает
WHEEL_PRIZES = [(10, 30), (15, 25), (20, 18), (30, 13), (45, 7), (60, 5), (120, 1.5), (180, 0.5)]
STREAK_BONUS = {3: 30, 7: 24 * 60}  # день серии → бонус в минутах (на 7-й день — сутки)


def streak_of(uid: int) -> tuple[int, bool]:
    """Серия: сколько дней подряд крутил колесо (включая сегодня/вчера). Возвращает (серия, крутил_сегодня)."""
    days = set(db.wheel_days(uid))
    today = datetime.now(MSK).date()
    spun_today = today.isoformat() in days
    d = today if spun_today else today - timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n, spun_today


def wheel_info(u) -> dict:
    streak, spun = streak_of(u["id"])
    row = db.wheel_spin_today(u["id"], game_today())
    nxt = streak + 1  # какой день серии будет при следующей прокрутке (сегодня или завтра)
    cyc = ((streak - 1) % 7 + 1) if streak else 0             # позиция в недельном цикле
    return {
        "allowed": GAME_ON and (GAME_FOR_ALL or is_active(u)),
        "spun_today": spun, "today_prize": row[0] if row else 0,
        "prizes": [m for m, _ in WHEEL_PRIZES],
        "streak": streak, "cycle_day": cyc, "next_day": ((nxt - 1) % 7 + 1),
        "bonuses": {str(k): v for k, v in STREAK_BONUS.items()},
    }


def short_name(name: str | None, username: str | None) -> str:
    n = (name or "").strip().split(" ")[0] or (username or "Игрок")
    return n[:14]


def game_top_payload(uid: int) -> dict:
    day = game_today()
    top = [{"name": short_name(r[2], r[3]), "score": r[1], "me": r[0] == uid} for r in db.game_top(day)]
    return {"top": top, "my_rank": db.game_rank(uid, day), "my_best": db.game_day(uid, day)["best"]}


async def api_wheel_spin(request):
    u = api_user(request)
    w = wheel_info(u)
    if not w["allowed"]:
        return web.json_response({"error": "Колесо доступно с активной подпиской"}, status=403)
    if w["spun_today"]:
        return web.json_response({"error": "Сегодня уже крутил — приходи завтра 🧡"}, status=429)
    idx = random.choices(range(len(WHEEL_PRIZES)), weights=[wt for _, wt in WHEEL_PRIZES])[0]
    prize = WHEEL_PRIZES[idx][0]
    if not db.wheel_add(u["id"], game_today(), prize):
        return web.json_response({"error": "Сегодня уже крутил"}, status=429)
    streak, _ = streak_of(u["id"])
    cyc = (streak - 1) % 7 + 1
    bonus = STREAK_BONUS.get(cyc, 0)
    await add_seconds(u["id"], (prize + bonus) * 60)
    if prize >= 120 or bonus >= 24 * 60:
        await notify_admin(f"🎡 <code>{u['id']}</code> выиграл в колесе {prize} мин" + (f" + бонус серии {bonus} мин" if bonus else ""))
    u = db.get_user(u["id"])
    return web.json_response({"index": idx, "prize": prize, "bonus": bonus, "streak": streak,
                              "wheel": wheel_info(u), "me": await me_payload(u)})


async def api_game_top(request):
    u = api_user(request)
    return web.json_response(game_top_payload(u["id"]))


async def api_public(request):
    """Публичные данные для страниц документов: контакты поддержки, тарифы, кодовое слово."""
    return web.json_response({
        "support": SUPPORT, "support_email": SUPPORT_EMAIL, "bot": MAIN_USERNAME, "code_word": CODE_WORD,
        "devices": DEVICES, "trial_days": TRIAL_DAYS,
        "plans": [{"name": n, "stars": st, "rub": RUB_PRICES[k], "days": d} for k, (n, st, d) in PLANS.items()],
    })


async def api_root(request):
    return web.Response(text="KluxVPN API ok")


async def start_api():
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", api_root)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/public", api_public)
    app.router.add_post("/api/invoice", api_invoice)
    app.router.add_post("/api/trial", api_trial)
    app.router.add_post("/api/game/start", api_game_start)
    app.router.add_post("/api/game/finish", api_game_finish)
    app.router.add_post("/api/wheel/spin", api_wheel_spin)
    app.router.add_get("/api/game/top", api_game_top)
    app.router.add_post("/yoomoney", api_yoomoney)
    app.router.add_post("/platega", api_platega)
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
    if platega.enabled:
        log.info("🏦 Колбэк Platega: %s/platega", PUBLIC_URL or "(нужен PUBLIC_URL)")
    too_big = [k for k, v in RUB_PRICES.items() if v > Platega.MAX_RUB]
    if too_big:
        log.warning("⚠️ Цены выше лимита Platega 10 000 ₽: %s", too_big)
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

    await setup_commands()
    if webapp_url():
        try:
            await main_bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="KluxVPN", web_app=WebAppInfo(url=webapp_url())))
            log.info("✅ Кнопка мини-апки: %s", webapp_url())
        except Exception as e:
            log.warning("Не удалось поставить кнопку меню: %s", e)

    bots = [main_bot] if pay_bot is main_bot else [main_bot, pay_bot]
    if promo_bot:
        bots.append(promo_bot)
        log.info("🔗 Бот-перенаправлялка запущен → %s", promo_url())
    for b in bots:
        await b.delete_webhook(drop_pending_updates=False)

    dp.include_routers(promo_r, admin_login_r, admin_r, pay_r, main_r, paybot_r)
    await start_api()
    asyncio.create_task(reminders_loop())
    asyncio.create_task(crypto_loop())
    asyncio.create_task(platega_loop())
    asyncio.create_task(clear_ips_loop())
    await dp.start_polling(*bots)


if __name__ == "__main__":
    asyncio.run(main())
