"""
KlyxVPN — Telegram-бот (версия 1: тарифы + оплата в Stars + мини-апка).
VPN-сервер подключим на следующем этапе — тогда бот начнёт выдавать настоящие ключи.

Переменные окружения (задаются в Railway → Variables):
  BOT_TOKEN        — токен от @BotFather (обязательно)
  ADMIN_ID         — твой Telegram ID (уже вписан в код: 8949720653)
  WEBAPP_URL       — ссылка на мини-апку (GitHub Pages), можно добавить позже
  SUPPORT_USERNAME — юзернейм поддержки без @, например klyx_support
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("klyxvpn")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8949720653") or 8949720653)
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
SUPPORT = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")

if not BOT_TOKEN:
    raise SystemExit("❌ Не задан BOT_TOKEN. Добавь его в Railway → Variables.")

# Тарифы: ключ -> (название, цена в Stars, месяцев)
PLANS = {
    "m1": ("1 месяц", 129, 1),
    "m3": ("3 месяца", 324, 3),
    "m12": ("12 месяцев", 1039, 12),
}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ---------- клавиатуры ----------
def main_kb() -> InlineKeyboardMarkup:
    rows = []
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="🚀 Открыть KlyxVPN", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([InlineKeyboardButton(text="💎 Тарифы", callback_data="plans")])
    rows.append([InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{name} — {price} ⭐", callback_data=f"buy:{key}")]
        for key, (name, price, _) in PLANS.items()
    ]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


WELCOME = (
    "👋 Привет! Это <b>KlyxVPN</b>\n\n"
    "🔒 Быстрый VPN на протоколе VLESS Reality\n"
    "♾ Безлимитный трафик\n"
    "📱 До 3 устройств на один ключ\n"
    "🌍 Нидерланды, Германия, Финляндия и другие страны\n\n"
    "Выбери тариф и получи ключ за минуту 👇"
)


# ---------- команды ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    # command.args — то, что после ?start= (пригодится для рефералки)
    if command.args:
        log.info("start from %s with args=%s", message.from_user.id, command.args)
    await message.answer(WELCOME, reply_markup=main_kb(), parse_mode="HTML")


@dp.message(Command("plans"))
async def cmd_plans(message: Message):
    await message.answer("💎 Выбери тариф:", reply_markup=plans_kb())


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📄 Условия использования\n\n"
        "1. Подписка даёт доступ к VPN на выбранный срок.\n"
        "2. Ключ нельзя передавать более чем на 3 устройства.\n"
        "3. Запрещено использовать сервис для незаконной деятельности.\n"
        "4. Если сервис не заработал — напиши в поддержку, вернём оплату."
    )


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    contact = f"@{SUPPORT}" if SUPPORT else "в поддержку"
    await message.answer(f"💬 По вопросам оплаты и возвратов пиши {contact}. Ответим в течение суток.")


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


# Возврат Stars (только для админа) — удобно, чтобы вернуть себе тестовые платежи
@dp.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args or len(command.args.split()) != 2:
        await message.answer("Формат: /refund <user_id> <charge_id>")
        return
    user_id, charge_id = command.args.split()
    try:
        await bot.refund_star_payment(user_id=int(user_id), telegram_payment_charge_id=charge_id)
        await message.answer("✅ Звёзды возвращены")
    except Exception as e:
        await message.answer(f"❌ Ошибка возврата: {e}")


# ---------- кнопки ----------
@dp.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    await call.message.edit_text(WELCOME, reply_markup=main_kb(), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "plans")
async def cb_plans(call: CallbackQuery):
    await call.message.edit_text("💎 Выбери тариф:", reply_markup=plans_kb())
    await call.answer()


@dp.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    contact = f"@{SUPPORT}" if SUPPORT else "скоро здесь появится контакт поддержки"
    await call.answer()
    await call.message.answer(f"🆘 Поддержка: {contact}")


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in PLANS:
        await call.answer("Тариф не найден", show_alert=True)
        return
    name, price, months = PLANS[key]
    await call.answer()
    # Оплата в Telegram Stars: валюта XTR, provider_token не нужен
    await call.message.answer_invoice(
        title=f"KlyxVPN — {name}",
        description=f"Доступ к VPN на {name}. Безлимитный трафик, до 3 устройств.",
        payload=f"{key}:{call.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=name, amount=price)],
    )


# ---------- оплата ----------
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    # Тут можно проверить, что тариф существует, и только потом подтвердить
    key = query.invoice_payload.split(":", 1)[0]
    if key in PLANS:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Тариф не найден, попробуй ещё раз")


@dp.message(F.successful_payment)
async def on_paid(message: Message):
    sp = message.successful_payment
    key = sp.invoice_payload.split(":", 1)[0]
    name = PLANS.get(key, ("?", 0, 0))[0]
    log.info(
        "PAID user=%s plan=%s stars=%s charge_id=%s",
        message.from_user.id, key, sp.total_amount, sp.telegram_payment_charge_id,
    )
    await message.answer(
        f"✅ Оплата прошла! Тариф: <b>{name}</b>\n\n"
        "🔑 Ключ будет выдаваться автоматически, как только подключим VPN-сервер "
        "(сейчас бот в тестовом режиме).\n\n"
        f"ID платежа: <code>{sp.telegram_payment_charge_id}</code>",
        parse_mode="HTML",
    )
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 Новая оплата: {sp.total_amount} ⭐ — {name}\n"
                f"От: {message.from_user.full_name} (id {message.from_user.id})\n"
                f"Возврат: <code>/refund {message.from_user.id} {sp.telegram_payment_charge_id}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def main():
    me = await bot.get_me()
    log.info("✅ Бот запущен: @%s", me.username)
    await bot.delete_webhook(drop_pending_updates=True)
    # Кнопка мини-апки слева от поля ввода (для всех пользователей)
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="KlyxVPN", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            log.info("✅ Кнопка меню установлена: %s", WEBAPP_URL)
        except Exception as e:
            log.warning("Не удалось поставить кнопку меню: %s", e)
    else:
        log.warning("WEBAPP_URL не задан — кнопки мини-апки не будет")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
