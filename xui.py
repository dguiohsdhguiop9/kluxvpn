"""Работа с панелью 3X-UI: создание и продление ключей, получение vless-ссылки.

Поддерживает обе версии API:
  • 3X-UI 3.x  — клиенты отдельно: /panel/api/clients/add, /update/{email}, /links/{email}
  • 3X-UI 2.x  — клиенты внутри инбаунда: /panel/api/inbounds/addClient, /updateClient/{uuid}
Версия определяется автоматически.
"""
import json
import logging
import re
import uuid as uuidlib
from urllib.parse import quote, urlparse

log = logging.getLogger("klyxvpn.xui")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _as_dict(v):
    if isinstance(v, str):
        try:
            return json.loads(v or "{}")
        except Exception:
            return {}
    return v or {}


def build_link(client_uuid: str, host: str, inbound: dict, name: str) -> str:
    """Собирает ссылку vless:// из настроек инбаунда (запасной вариант, если панель не отдала ссылку)."""
    port = inbound["port"]
    stream = _as_dict(inbound.get("streamSettings"))
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
    elif security == "tls":
        ts = stream.get("tlsSettings", {})
        params["sni"] = ts.get("serverName", "")
        params["fp"] = (ts.get("settings") or {}).get("fingerprint") or "chrome"

    if network in ("xhttp", "splithttp"):
        xs = stream.get("xhttpSettings") or stream.get("splithttpSettings") or {}
        params["path"] = xs.get("path", "/")
        if xs.get("host"):
            params["host"] = xs["host"]
        if xs.get("mode"):
            params["mode"] = xs["mode"]
    elif network == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
    params["flow"] = flow_for(inbound)

    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items() if v != "")
    return f"vless://{client_uuid}@{host}:{port}?{query}#{quote(name)}"


def flow_for(inbound: dict) -> str:
    """xtls-rprx-vision допустим только для TCP/RAW + Reality/TLS. Для XHTTP, WS, gRPC и т.п. — пусто."""
    stream = _as_dict(inbound.get("streamSettings"))
    if stream.get("network", "tcp") in ("tcp", "raw") and stream.get("security") in ("reality", "tls"):
        return "xtls-rprx-vision"
    return ""


def _find_strings(obj, prefix: str):
    """Ищет в ответе панели строки, начинающиеся с prefix (например, vless://)."""
    if isinstance(obj, str):
        for line in obj.splitlines():
            if line.strip().startswith(prefix):
                yield line.strip()
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _find_strings(v, prefix)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_strings(v, prefix)


def _find_uuid(obj):
    """Ищет UUID клиента (поле id) в ответе панели."""
    if isinstance(obj, dict):
        v = obj.get("id")
        if isinstance(v, str) and UUID_RE.match(v):
            return v
        for k in ("client", "obj"):
            if k in obj:
                r = _find_uuid(obj[k])
                if r:
                    return r
        for v in obj.values():
            if isinstance(v, (dict, list)):
                r = _find_uuid(v)
                if r:
                    return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_uuid(v)
            if r:
                return r
    return None


class PanelError(RuntimeError):
    pass


class XUI:
    def __init__(self, base_url: str, username: str, password: str, inbound_id: int, host: str = "", token: str = ""):
        self.base = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.inbound_id = int(inbound_id or 0)
        self.host = host or (urlparse(self.base).hostname or "")
        self.mode = None  # "v3" или "v2", определяется автоматически
        self._logged_in = False
        self._session = None
        self._inbound_cache = None

    @property
    def configured(self) -> bool:
        return bool(self.base and self.inbound_id and (self.token or (self.username and self.password)))

    @staticmethod
    def email_for(tg_id: int) -> str:
        return f"klyx_{tg_id}"

    async def _sess(self):
        import aiohttp

        if self._session is None or self._session.closed:
            headers = {"Accept": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),  # панель часто с самоподписанным сертификатом
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=20),
                headers=headers,
            )
        return self._session

    async def login(self):
        if self.token:
            return
        s = await self._sess()
        async with s.post(f"{self.base}/login", data={"username": self.username, "password": self.password}) as r:
            text = await r.text()
        try:
            data = json.loads(text)
        except Exception:
            raise PanelError(f"Панель не отвечает на /login (HTTP {r.status}). Проверь XUI_URL")
        if not data.get("success"):
            raise PanelError(f"Не удалось войти в панель: {data.get('msg') or 'неверный логин или пароль'}")
        self._logged_in = True
        log.info("3X-UI: вход выполнен")

    async def _raw(self, method: str, path: str, **kw):
        """Возвращает (http_status, json|None).
        Панель 3X-UI отвечает 404 на ЛЮБОЙ запрос к API без входа, поэтому
        сначала входим, а при 404/401/не-JSON — входим заново и повторяем один раз."""
        s = await self._sess()
        if not self.token and not self._logged_in:
            await self.login()
        for attempt in range(2):
            async with s.request(method, f"{self.base}{path}", **kw) as r:
                status = r.status
                text = await r.text()
            try:
                data = json.loads(text) if status != 404 else None
            except Exception:
                data = None
            if (status in (401, 403, 404) or data is None) and attempt == 0 and not self.token:
                await self.login()
                continue
            return status, data
        return status, data

    async def _req(self, method: str, path: str, **kw):
        status, data = await self._raw(method, path, **kw)
        if status == 404:
            raise PanelError(f"Панель не знает адрес {path} (404)")
        if data is None:
            raise PanelError(f"Панель ответила не JSON (HTTP {status}) на {path}")
        if not data.get("success"):
            raise PanelError(f"Ошибка панели: {data.get('msg')}")
        return data.get("obj")

    async def detect(self) -> str:
        if self.mode:
            return self.mode
        status, _ = await self._raw("GET", "/panel/api/clients/list/paged?page=1&pageSize=1")
        if status == 404:
            status, _ = await self._raw("GET", "/panel/api/clients/list")
        self.mode = "v2" if status == 404 else "v3"
        log.info("3X-UI: режим API %s", self.mode)
        return self.mode

    async def inbound(self, refresh: bool = False) -> dict:
        if self._inbound_cache is None or refresh:
            self._inbound_cache = await self._req("GET", f"/panel/api/inbounds/get/{self.inbound_id}")
        return self._inbound_cache

    # ---------- клиенты ----------
    async def create_client(self, tg_id: int, expiry_ts: int, limit_ip: int = 3) -> str:
        """Создаёт ключ. Если клиент с таким email уже есть — продлевает его. Возвращает UUID."""
        mode = await self.detect()
        email = self.email_for(tg_id)
        client_uuid = str(uuidlib.uuid4())
        flow = flow_for(await self.inbound())
        client = {
            "id": client_uuid,
            "email": email,
            "flow": flow,
            "limitIp": limit_ip,
            "totalGB": 0,
            "expiryTime": expiry_ts * 1000,
            "enable": True,
            "subId": client_uuid.replace("-", "")[:16],
            "comment": "KlyxVPN",
        }
        try:
            if mode == "v3":
                await self._req("POST", "/panel/api/clients/add",
                                json={"client": {**client, "tgId": int(tg_id)}, "inboundIds": [self.inbound_id]})
            else:
                await self._req("POST", "/panel/api/inbounds/addClient",
                                json={"id": self.inbound_id,
                                      "settings": json.dumps({"clients": [{**client, "tgId": str(tg_id), "reset": 0}]})})
        except PanelError as e:
            if "exist" in str(e).lower() or "duplicate" in str(e).lower():
                existing = await self.get_uuid(tg_id)
                if existing:
                    await self.update_client(existing, tg_id, expiry_ts, limit_ip)
                    return existing
            raise
        log.info("3X-UI: создан клиент %s для %s", email, tg_id)
        if mode == "v3":
            # панель могла выдать свой UUID — уточним
            return (await self.get_uuid(tg_id)) or client_uuid
        return client_uuid

    async def get_uuid(self, tg_id: int) -> str | None:
        mode = await self.detect()
        email = self.email_for(tg_id)
        if mode == "v3":
            try:
                return _find_uuid(await self._req("GET", f"/panel/api/clients/get/{quote(email)}"))
            except PanelError:
                return None
        ib = await self.inbound(refresh=True)
        for c in _as_dict(ib.get("settings")).get("clients", []):
            if c.get("email") == email:
                return c.get("id")
        return None

    async def update_client(self, client_uuid: str, tg_id: int, expiry_ts: int, limit_ip: int = 3, enable: bool = True):
        mode = await self.detect()
        email = self.email_for(tg_id)
        if mode == "v3":
            await self._req("POST", f"/panel/api/clients/update/{quote(email)}",
                            json={"email": email, "expiryTime": expiry_ts * 1000, "limitIp": limit_ip,
                                  "enable": enable, "tgId": int(tg_id)})
        else:
            c = {"id": client_uuid, "email": email, "flow": flow_for(await self.inbound()), "limitIp": limit_ip, "totalGB": 0,
                 "expiryTime": expiry_ts * 1000, "enable": enable, "tgId": str(tg_id),
                 "subId": client_uuid.replace("-", "")[:16], "reset": 0}
            await self._req("POST", f"/panel/api/inbounds/updateClient/{client_uuid}",
                            json={"id": self.inbound_id, "settings": json.dumps({"clients": [c]})})
        log.info("3X-UI: обновлён клиент %s (до %s)", email, expiry_ts)

    async def link(self, client_uuid: str, tg_id: int, name: str = "KlyxVPN") -> str:
        mode = await self.detect()
        if mode == "v3":
            try:
                obj = await self._req("GET", f"/panel/api/clients/links/{quote(self.email_for(tg_id))}")
                links = list(_find_strings(obj, "vless://"))
                if links:
                    base = links[0].split("#", 1)[0]
                    return f"{base}#{quote(name)}"
            except PanelError as e:
                log.warning("Не удалось взять ссылку у панели, собираю сам: %s", e)
        return build_link(client_uuid, self.host, await self.inbound(), name)

    async def ping(self) -> str:
        mode = await self.detect()
        ib = await self.inbound(refresh=True)
        stream = _as_dict(ib.get("streamSettings"))
        return (f"✅ Панель доступна (API {mode}). Инбаунд #{self.inbound_id}: {ib.get('protocol')}, "
                f"порт {ib.get('port')}, {stream.get('network', '?')} + {stream.get('security', '?')}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
