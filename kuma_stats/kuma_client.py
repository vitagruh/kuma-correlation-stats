"""Минималистичный клиент KUMA REST API v3 на стандартной библиотеке."""
from __future__ import annotations

import json
import logging
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import KumaConfig

RETRY_STATUSES = {429, 500, 502, 503, 504}
PAGE_SIZE = 250


class KumaError(Exception):
    """Общая ошибка взаимодействия с KUMA."""


class KumaAuthError(KumaError):
    """Ошибка аутентификации/авторизации."""


class KumaConnectionError(KumaError):
    """Сеть, DNS, TLS, таймаут."""


class KumaHTTPError(KumaError):
    def __init__(self, status: int, message: str, body: str = ""):
        self.status = status
        self.message = message
        self.body = body
        super().__init__(f"HTTP {status}: {message}")


class KumaClient:
    def __init__(self, config: KumaConfig, logger: logging.Logger | None = None):
        self.config = config
        self.log = logger or logging.getLogger("kuma_stats")
        base = config.base_url.rstrip("/")
        if base.endswith("/api/v3"):
            self.api_root = base
            self.root = base[: -len("/api/v3")]
        else:
            self.api_root = base + "/api/v3"
            self.root = base

        self._token = config.resolved_token()
        self._login_attempted = False
        self._ssl_context = self._build_ssl_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context)
        )

        if not self._token:
            if not (config.username and config.password):
                raise KumaAuthError(
                    "Не задан токен KUMA. Укажите kuma.token/kuma.token_file "
                    "или kuma.username и kuma.password."
                )
            self._token = self.login()

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def _build_ssl_context(self) -> ssl.SSLContext:
        if not self.config.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.log.warning(
                "Проверка TLS-сертификата KUMA отключена (verify_tls=false). "
                "Используйте только в доверенном контуре."
            )
            return context
        if self.config.ca_bundle:
            bundle = Path(self.config.ca_bundle).expanduser()
            return ssl.create_default_context(cafile=str(bundle))
        return ssl.create_default_context()

    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "kuma-daily-stats/1.0",
        }
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _raw_request(
        self,
        method: str,
        url: str,
        body: Any = None,
        auth: bool = True,
        timeout: int | None = None,
    ) -> tuple[int, str]:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=self._headers(auth), method=method
        )
        try:
            with self._opener.open(request, timeout=timeout or self.config.timeout) as response:
                return int(response.status), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - тело ошибки не критично
                text = ""
            return int(exc.code), text
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
                raise KumaConnectionError(
                    "TLS-сертификат KUMA не прошёл проверку. Укажите kuma.ca_bundle "
                    "или (осознанно) kuma.verify_tls=false."
                ) from exc
            raise KumaConnectionError(f"Не удалось подключиться к KUMA: {reason}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise KumaConnectionError(
                f"Таймаут запроса к KUMA ({self.config.timeout} с): {url}"
            ) from exc
        except OSError as exc:
            raise KumaConnectionError(f"Сетевая ошибка при запросе к KUMA: {exc}") from exc

    def _request_json(
        self,
        method: str,
        url: str,
        body: Any = None,
        auth: bool = True,
        allow_login: bool = True,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                status, text = self._raw_request(method, url, body=body, auth=auth)
            except KumaConnectionError as exc:
                last_error = exc
                if attempt < self.config.retries:
                    self._sleep_backoff(attempt)
                    continue
                raise

            if status == 401:
                if allow_login and self.config.username and self.config.password and not self._login_attempted:
                    self.log.info("Токен KUMA отклонён (401), пробую авторизацию по логину/паролю")
                    self._token = self.login()
                    continue
                raise KumaAuthError(
                    "KUMA отклонил токен (401). Проверьте срок действия токена и права учётной записи."
                )
            if status == 403:
                raise KumaAuthError(
                    "Недостаточно прав (403). Нужен доступ к событиям и ресурсам KUMA."
                )
            if status in RETRY_STATUSES:
                last_error = KumaHTTPError(status, self._error_message(status, text), text)
                if attempt < self.config.retries:
                    self.log.warning(
                        "KUMA вернул HTTP %s, повтор %s/%s",
                        status,
                        attempt + 1,
                        self.config.retries,
                    )
                    self._sleep_backoff(attempt, text)
                    continue
                raise last_error
            if 200 <= status < 300:
                return self._parse_json(text, url)

            raise KumaHTTPError(status, self._error_message(status, text), text)

        if last_error:
            raise last_error
        raise KumaError("Запрос к KUMA не выполнен по неизвестной причине")

    def _sleep_backoff(self, attempt: int, body: str | None = None) -> None:
        delay = self.config.retry_backoff * (2 ** attempt)
        if body:
            retry_after = _retry_after(body)
            if retry_after is not None:
                delay = max(delay, retry_after)
        delay += random.uniform(0, 0.4)
        time.sleep(min(delay, 120.0))

    def _parse_json(self, text: str, url: str) -> Any:
        if not text or not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise KumaError(
                f"KUMA вернул не-JSON ответ ({url}): {text[:300]}"
            ) from exc

    @staticmethod
    def _error_message(status: int, text: str) -> str:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            details = payload.get("details")
            if message and details:
                return f"{message} ({_short_json(details)})"
            if message:
                return str(message)
        cleaned = (text or "").strip()
        return cleaned[:300] if cleaned else f"HTTP {status}"

    # ------------------------------------------------------------------ #
    # Авторизация и служебные методы
    # ------------------------------------------------------------------ #

    def login(self) -> str:
        if self._login_attempted:
            raise KumaAuthError("Повторная авторизация по логину/паролю уже выполнялась")
        self._login_attempted = True
        if not (self.config.username and self.config.password):
            raise KumaAuthError("Для авторизации нужны kuma.username и kuma.password")
        url = self.root + "/api/v1/login"
        body = {"username": self.config.username, "password": self.config.password}
        status, text = self._raw_request("POST", url, body=body, auth=False)
        if status != 200:
            raise KumaAuthError(
                f"Авторизация в KUMA не удалась (HTTP {status}): "
                f"{self._error_message(status, text)}. "
                "Если в вашей версии KUMA нет /api/v1/login, используйте API-токен."
            )
        token = _extract_token(text)
        if not token:
            raise KumaAuthError(
                "Авторизация прошла, но токен не найден в ответе KUMA. "
                "Используйте API-токен (kuma.token)."
            )
        self.log.info("Получен токен KUMA для пользователя %s", self.config.username)
        return token

    def whoami(self) -> dict[str, Any]:
        data = self._request_json("GET", self.api_root + "/users/whoami")
        return data if isinstance(data, dict) else {}

    def get_clusters(self) -> list[dict[str, Any]]:
        data = self._request_json("GET", self.api_root + "/events/clusters")
        if not isinstance(data, list):
            raise KumaError("Неожиданный ответ /events/clusters: ожидался список")
        return [item for item in data if isinstance(item, dict)]

    def resolve_cluster(self) -> tuple[str, str]:
        if self.config.cluster_id:
            return self.config.cluster_id.strip(), ""
        try:
            clusters = self.get_clusters()
        except KumaError as exc:
            self.log.warning(
                "Не удалось получить список кластеров (%s). "
                "Если кластер один, KUMA обычно сам подставит его.",
                exc,
            )
            return "", ""
        if not clusters:
            return "", ""
        if self.config.cluster_name:
            wanted = self.config.cluster_name.strip().casefold()
            for cluster in clusters:
                if str(cluster.get("name", "")).strip().casefold() == wanted:
                    return str(cluster.get("id") or ""), str(cluster.get("name") or "")
            names = ", ".join(str(c.get("name") or c.get("id")) for c in clusters)
            raise KumaError(
                f"Кластер '{self.config.cluster_name}' не найден. Доступны: {names}"
            )
        if len(clusters) == 1:
            cluster = clusters[0]
            return str(cluster.get("id") or ""), str(cluster.get("name") or "")
        names = "; ".join(
            f"{c.get('name') or '—'} ({c.get('id') or '—'})" for c in clusters
        )
        raise KumaError(
            "Доступно несколько кластеров хранения. Укажите kuma.cluster_id "
            f"(или kuma.cluster_name). Список: {names}"
        )

    def get_tenants(self) -> list[dict[str, Any]]:
        return self._paginate("/tenants")

    def get_correlation_rules(self) -> list[dict[str, Any]]:
        items = self._paginate("/resources", params={"kind": "correlationRule"})
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("kind") and item.get("kind") != "correlationRule":
                continue
            key = str(item.get("id") or item.get("name") or "")
            if key:
                result[key] = item
        return list(result.values())

    def _paginate(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        collected: list[Any] = []
        page = 1
        while page <= 500:
            query = dict(params or {})
            query["page"] = str(page)
            url = self.api_root + path + "?" + urllib.parse.urlencode(query)
            data = self._request_json("GET", url)
            if data is None:
                break
            if isinstance(data, dict):
                data = data.get("items") or data.get("data") or []
            if not isinstance(data, list):
                raise KumaError(f"Неожиданный ответ {path}: ожидался список")
            collected.extend(data)
            if len(data) < PAGE_SIZE:
                break
            page += 1
        return collected

    # ------------------------------------------------------------------ #
    # События
    # ------------------------------------------------------------------ #

    def search_events(
        self,
        sql: str,
        period_from: str,
        period_to: str,
        cluster_id: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_cluster = (
            cluster_id if cluster_id is not None else self.config.cluster_id
        ).strip()
        payload: dict[str, Any] = {
            "period": {"from": period_from, "to": period_to},
            "emptyFields": True,
            "rawTimestamps": False,
            "sql": sql,
        }
        if effective_cluster:
            payload["clusterID"] = effective_cluster

        try:
            data = self._request_json("POST", self.api_root + "/events", body=payload)
        finally:
            if self.config.delay_between_queries > 0:
                time.sleep(self.config.delay_between_queries)

        if data is None:
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            message = data.get("message") or data.get("error") or _short_json(data)
            raise KumaError(f"KUMA вернул объект вместо списка событий: {message}")
        raise KumaError(f"Неожиданный ответ API событий: {type(data).__name__}")


def _short_json(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _retry_after(body: str) -> float | None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("retryAfter") or payload.get("retry_after")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_token(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if raw.startswith("{") or raw.startswith("["):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        return _find_token(payload)
    if raw.startswith('"') and raw.endswith('"') and len(raw) > 2:
        try:
            return str(json.loads(raw)).strip()
        except json.JSONDecodeError:
            return raw.strip('"')
    if " " not in raw and len(raw) < 20000:
        return raw
    return ""


def _find_token(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("token", "access_token", "accessToken", "jwt", "kuma_token", "kumaToken"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("data", "result", "auth"):
            value = payload.get(key)
            if isinstance(value, (dict, str)):
                token = _find_token(value)
                if token:
                    return token
    return ""
