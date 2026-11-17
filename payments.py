"""Внешние способы оплаты: CryptoBot (крипта) и ЮMoney (карта, рубли)."""
import hashlib
import json
import logging
from urllib.parse import urlencode

log = logging.getLogger("klyxvpn.pay")


class CryptoPay:
    """Crypto Pay API (@CryptoBot). Токен: @CryptoBot → Crypto Pay → Create App."""

    def __init__(self, token: str, testnet: bool = False):
        self.token = token
        self.base = "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"
        self._session = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, **params):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.post(
            f"{self.base}/{method}", json=params, headers={"Crypto-Pay-API-Token": self.token}
        ) as r:
            data = await r.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(f"CryptoBot: {data.get('error')}")
        return data["result"]

    async def create_invoice(self, amount_rub: int, payload: str, description: str) -> tuple[str, str, str]:
        """Счёт в рублях, оплата любой криптой. Возвращает (invoice_id, ссылка в бота, ссылка в мини-апп)."""
        inv = await self._call(
            "createInvoice",
            currency_type="fiat",
            fiat="RUB",
            amount=str(amount_rub),
            accepted_assets="USDT,TON,BTC,LTC,TRX",
            description=description[:1024],
            payload=payload[:4096],
            expires_in=3600,
            allow_comments=False,
            allow_anonymous=False,
        )
        url = inv.get("bot_invoice_url") or inv.get("pay_url")
        return str(inv["invoice_id"]), url, inv.get("mini_app_invoice_url") or url

    async def paid_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        res = await self._call("getInvoices", invoice_ids=",".join(ids), status="paid")
        return res.get("items", []) if isinstance(res, dict) else res


class YooMoney:
    """Приём оплаты картой на кошелёк ЮMoney (форма quickpay + HTTP-уведомления)."""

    def __init__(self, wallet: str, secret: str):
        self.wallet = wallet
        self.secret = secret

    @property
    def enabled(self) -> bool:
        return bool(self.wallet and self.secret)

    def link(self, amount_rub: int, label: str, title: str, success_url: str = "") -> str:
        params = {
            "receiver": self.wallet,
            "quickpay-form": "button",
            "paymentType": "AC",  # банковская карта
            "sum": str(amount_rub),
            "label": label,
            "targets": title,
        }
        if success_url:
            params["successURL"] = success_url
        return "https://yoomoney.ru/quickpay/confirm?" + urlencode(params)

    def check(self, form: dict) -> bool:
        """Проверка подписи HTTP-уведомления ЮMoney."""
        keys = ["notification_type", "operation_id", "amount", "currency", "datetime", "sender", "codepro"]
        s = "&".join(form.get(k, "") for k in keys) + f"&{self.secret}&" + form.get("label", "")
        return hashlib.sha1(s.encode()).hexdigest() == form.get("sha1_hash", "")
