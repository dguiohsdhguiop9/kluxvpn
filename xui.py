"""Работа с панелью 3X-UI: создание и продление ключей, сборка vless-ссылки."""
import json
import logging
import uuid as uuidlib
from urllib.parse import quote, urlparse

log = logging.getLogger("klyxvpn.xui")


def build_link(client_uuid: str, host: str, inbound: dict, name: str) -> str:
    """Собирает ссылку vless:// из настроек инбаунда 3X-UI."""
    port = inbound["port"]
    stream = inbound.get("streamSettings") or "{}"
    if isinstance(stream, str):
        stream = json.loads(stream)
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    params = {"type": network, "security": security, "encryption": "none"}

    if security == "reality":
        rs = stream.get("realitySettings", {})
        rset = rs.get("settings", {})
        params["pbk"] = rset.get("publicKey", "")
        params["fp"] = rset.get("fingerprint") or "chrome"
        names = rs.get("serverNames") or [""]
        params["sni"] = names[0]
        sids = [s for s in (rs.get("shortIds") or []) if s]
        if sids:
            params["sid"] = sids[0]
        params["spx"] = rset.get("spiderX") or "/"
        params["flow"] = "xtls-rprx-vision"
    elif security == "tls":
        ts = stream.get("tlsSettings", {})
        params["sni"] = ts.get("serverName", "")
        params["fp"] = (ts.get("settings") or {}).get("fingerprint") or "chrome"

    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v != "")
    return f"vless://{client_uuid}@{host}:{port}?{query}#{quote(name)}"


class XUI:
    def __init__(self, base_url: str, username: str, password: str, inbound_id: int, host: str = ""):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.inbound_id = int(inbound_id)
        self.host = host or (urlparse(self.base).hostname or "")
        self._session = None
        self._inbound_cache = None

    @property
    def configured(self) -> bool:
        return bool(self.base and self.username and self.password and self.inbound_id)

    async def _sess(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),  # панель часто с самоподписанным сертификатом
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def login(self):
        s = await self._sess()
        async with s.post(f"{self.base}/login", data={"username": self.username, "password": self.password}) as r:
            data = await r.json(content_type=None)
            if not data.get("success"):
                raise RuntimeError(f"Не удалось войти в панель 3X-UI: {data.get('msg')}")

    async def _req(self, method: str, path: str, **kw):
        s = await self._sess()
        for attempt in range(2):
            async with s.request(method, f"{self.base}{path}", **kw) as r:
                if r.status in (401, 404) and attempt == 0:
                    await self.login()
                    continue
                text = await r.text()
                try:
                    data = json.loads(text)
                except Exception:
                    if attempt == 0:  # сессия протухла — панель вернула HTML логина
                        await self.login()
                        continue
                    raise RuntimeError(f"Панель ответила не JSON (HTTP {r.status})")
                if not data.get("success"):
                    raise RuntimeError(f"Ошибка панели: {data.get('msg')}")
                return data.get("obj")
        raise RuntimeError("Нет доступа к панели 3X-UI")

    async def inbound(self, refresh: bool = False) -> dict:
        if self._inbound_cache is None or refresh:
            self._inbound_cache = await self._req("GET", f"/panel/api/inbounds/get/{self.inbound_id}")
        return self._inbound_cache

    def _client(self, client_uuid: str, email: str, expiry_ms: int, tg_id: int, limit_ip: int) -> dict:
        return {
            "id": client_uuid,
            "flow": "xtls-rprx-vision",
            "email": email,
            "limitIp": limit_ip,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": str(tg_id),
            "subId": client_uuid.replace("-", "")[:16],
            "reset": 0,
        }

    async def create_client(self, tg_id: int, expiry_ts: int, limit_ip: int = 3) -> str:
        client_uuid = str(uuidlib.uuid4())
        body = {
            "id": self.inbound_id,
            "settings": json.dumps({"clients": [self._client(client_uuid, f"klyx_{tg_id}", expiry_ts * 1000, tg_id, limit_ip)]}),
        }
        await self._req("POST", "/panel/api/inbounds/addClient", json=body)
        log.info("3X-UI: создан клиент %s для %s", client_uuid, tg_id)
        return client_uuid

    async def update_client(self, client_uuid: str, tg_id: int, expiry_ts: int, limit_ip: int = 3, enable: bool = True):
        c = self._client(client_uuid, f"klyx_{tg_id}", expiry_ts * 1000, tg_id, limit_ip)
        c["enable"] = enable
        body = {"id": self.inbound_id, "settings": json.dumps({"clients": [c]})}
        await self._req("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json=body)
        log.info("3X-UI: обновлён клиент %s (до %s)", client_uuid, expiry_ts)

    async def link(self, client_uuid: str, name: str = "KlyxVPN") -> str:
        return build_link(client_uuid, self.host, await self.inbound(), name)

    async def ping(self) -> str:
        ib = await self.inbound(refresh=True)
        clients = json.loads(ib.get("settings") or "{}").get("clients", [])
        return f"✅ Панель доступна. Инбаунд #{self.inbound_id}: {ib.get('protocol')}, порт {ib.get('port')}, клиентов: {len(clients)}"

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
