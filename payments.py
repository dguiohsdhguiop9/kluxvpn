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


class Platega:
    """Platega.io: СБП (QR / ссылка в банковском приложении) и крипта. Метод оплаты клиент выбирает на странице оплаты.

    Ключи: личный кабинет Platega → «Настройки» (MerchantId и API-ключ).
    Колбэк: укажи в кабинете адрес https://<твой-домен>/platega
    Оплату бот всегда перепроверяет запросом статуса в API Platega — подделать колбэк нельзя.
    """

    MAX_RUB = 10_000  # лимит провайдера на одну транзакцию

    def __init__(self, merchant_id: str, secret: str, base_url: str = "https://app.platega.io"):
        self.merchant_id = merchant_id
        self.secret = secret
        self.base = (base_url or "https://app.platega.io").rstrip("/")
        self._session = None

    @property
    def enabled(self) -> bool:
        return bool(self.merchant_id and self.secret)

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        headers = {"X-MerchantId": self.merchant_id, "X-Secret": self.secret,
                   "Content-Type": "application/json", "Accept": "application/json"}
        async with self._session.request(method, f"{self.base}{path}", json=body, headers=headers) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Platega HTTP {r.status}: {text[:300]}")
        try:
            return json.loads(text)
        except Exception:
            raise RuntimeError(f"Platega: неожиданный ответ {text[:200]}")

    async def create(self, amount_rub: int, tg_id: int, description: str, payload: str,
                     return_url: str, fail_url: str, user_name: str = "", client_ip: str = "") -> tuple[str, str]:
        """Создаёт платёж. Возвращает (transactionId, ссылка на оплату)."""
        if amount_rub > self.MAX_RUB:
            raise ValueError(f"Сумма больше лимита {self.MAX_RUB} ₽")
        metadata = {
            "user_email": f"{tg_id}@telegram.org",  # формат Platega для магазинов в Telegram
            "userId": str(tg_id),
        }
        if user_name:
            metadata["userName"] = user_name[:100]
        if client_ip:
            metadata["clientIp"] = client_ip
        res = await self._call("POST", "/v2/transaction/process", {
            "paymentDetails": {"amount": amount_rub, "currency": "RUB"},
            "description": description[:250],
            "return": return_url,
            "failedUrl": fail_url,
            "payload": payload,
            "metadata": metadata,
        })
        tid = res.get("transactionId") or res.get("id")
        url = res.get("url") or res.get("redirect") or res.get("paymentUrl")
        if not tid or not url:
            raise RuntimeError(f"Platega: в ответе нет ссылки: {str(res)[:200]}")
        return str(tid), url

    async def status(self, transaction_id: str) -> str:
        """Статус платежа: CONFIRMED / PENDING / CANCELED / ... (в верхнем регистре)."""
        res = await self._call("GET", f"/transaction/{transaction_id}")
        return str(res.get("status") or res.get("Status") or "").upper()
