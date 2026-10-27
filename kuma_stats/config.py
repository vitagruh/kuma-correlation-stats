"""Загрузка и проверка конфигурации."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .utils import is_valid_field_name, load_timezone


class ConfigError(Exception):
    pass


@dataclass
class KumaConfig:
    base_url: str = "https://kuma.example.local:7223"
    token: str = ""
    token_file: str = ""
    username: str = ""
    password: str = ""
    verify_tls: bool = True
    ca_bundle: str = ""
    timeout: int = 120
    retries: int = 3
    retry_backoff: float = 2.0
    delay_between_queries: float = 0.1
    cluster_id: str = ""
    cluster_name: str = ""
    event_type: int = 3
    rule_field: str = "Name"
    max_rule_rows: int = 1000
    entity_fields: list[str] = field(
        default_factory=lambda: [
            "SourceAddress",
            "SourceUserName",
            "DestinationAddress",
            "DeviceHostName",
        ]
    )
    entity_limit: int = 100
    hourly_breakdown: bool = True
    hourly_rules_limit: int = 250
    collect_tactics: bool = True
    collect_tenants: bool = True
    collect_priority: bool = False
    collect_health: bool = True
    collect_raw_events: bool = True
    raw_events_limit: int = 20000
    raw_fields: list[str] = field(
        default_factory=lambda: [
            "Timestamp",
            "Name",
            "CorrelationRuleID",
            "Severity",
            "Priority",
            "SourceAddress",
            "SourceUserName",
            "DestinationAddress",
            "DestinationUserName",
            "DeviceHostName",
            "SourceHostName",
            "Tactic",
            "Technique",
            "BaseEventCount",
            "TenantID",
        ]
    )
    collect_rule_inventory: bool = False
    verify_token: bool = True

    def resolved_token(self) -> str:
        token = (self.token or "").strip()
        if token:
            return token
        if self.token_file:
            path = Path(self.token_file).expanduser()
            if not path.is_file():
                raise ConfigError(f"Файл с токеном не найден: {path}")
            return path.read_text(encoding="utf-8").strip()
        return ""


@dataclass
class ReportConfig:
    timezone: str = "Europe/Moscow"
    utc_offset_hours: float | None = 3.0
    window: str = "previous_day"
    day_offset: int = 1
    top_n: int = 20
    output_dir: str = "./reports"
    state_dir: str = "./state"
    index_limit: int = 60
    title: str = "KUMA · сработки правил корреляции"


@dataclass
class BaselineConfig:
    days: int = 30
    min_days: int = 7
    min_events: int = 5
    z_threshold: float = 3.0
    min_delta: int = 10
    new_rule_days: int = 30
    noise_min_events: int = 50
    noise_max_delta_pct: float = 25.0
    total_min_delta_pct: float = 20.0


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    use_ssl: bool = False
    starttls: bool = True
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to: list[str] = field(default_factory=list)
    subject_prefix: str = "[KUMA]"
    attach_html: bool = True


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    only_on_anomalies: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""


@dataclass
class Config:
    kuma: KumaConfig = field(default_factory=KumaConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source_path", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], source_path: str = "") -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("Корневой элемент конфигурации должен быть объектом JSON")
        known = {"kuma", "report", "baseline", "email", "webhook", "logging"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError("Неизвестные разделы конфигурации: " + ", ".join(unknown))
        return cls(
            kuma=_build(KumaConfig, data.get("kuma"), "kuma"),
            report=_build(ReportConfig, data.get("report"), "report"),
            baseline=_build(BaselineConfig, data.get("baseline"), "baseline"),
            email=_build(EmailConfig, data.get("email"), "email"),
            webhook=_build(WebhookConfig, data.get("webhook"), "webhook"),
            logging=_build(LoggingConfig, data.get("logging"), "logging"),
            source_path=source_path,
        )

    def validate(self) -> None:
        kuma = self.kuma
        report = self.report
        baseline = self.baseline

        if not kuma.base_url.startswith(("http://", "https://")):
            raise ConfigError("kuma.base_url должен начинаться с http:// или https://")
        kuma.base_url = kuma.base_url.rstrip("/")

        if not (kuma.token or kuma.token_file or (kuma.username and kuma.password)):
            raise ConfigError(
                "Не задан доступ к KUMA: укажите kuma.token, kuma.token_file "
                "или пару kuma.username/kuma.password"
            )
        if kuma.token_file and not Path(kuma.token_file).expanduser().is_file():
            raise ConfigError(f"Файл с токеном не найден: {kuma.token_file}")
        if kuma.ca_bundle and not Path(kuma.ca_bundle).expanduser().is_file():
            raise ConfigError(f"CA-сертификат не найден: {kuma.ca_bundle}")

        if not 1 <= int(kuma.event_type) <= 5:
            raise ConfigError("kuma.event_type должен быть в диапазоне 1..5 (3 — корреляция)")
        if not is_valid_field_name(kuma.rule_field):
            raise ConfigError(f"Недопустимое имя поля kuma.rule_field: {kuma.rule_field}")
        if not kuma.entity_fields:
            raise ConfigError("kuma.entity_fields не может быть пустым")
        for name in kuma.entity_fields:
            if not is_valid_field_name(name):
                raise ConfigError(f"Недопустимое имя поля в kuma.entity_fields: {name}")
        if not kuma.raw_fields:
            raise ConfigError("kuma.raw_fields не может быть пустым")
        for name in kuma.raw_fields:
            if not is_valid_field_name(name):
                raise ConfigError(f"Недопустимое имя поля в kuma.raw_fields: {name}")

        _check_range(kuma.max_rule_rows, 1, 50000, "kuma.max_rule_rows")
        _check_range(kuma.entity_limit, 1, 100000, "kuma.entity_limit")
        _check_range(kuma.hourly_rules_limit, 1, 10000, "kuma.hourly_rules_limit")
        _check_range(kuma.raw_events_limit, 1, 1_000_000, "kuma.raw_events_limit")
        _check_range(kuma.retries, 0, 20, "kuma.retries")
        _check_range(kuma.timeout, 1, 3600, "kuma.timeout")
        if kuma.retry_backoff <= 0:
            raise ConfigError("kuma.retry_backoff должен быть больше нуля")
        if kuma.delay_between_queries < 0:
            raise ConfigError("kuma.delay_between_queries не может быть отрицательным")

        if report.window not in {"previous_day", "last_24h"}:
            raise ConfigError("report.window: допустимо 'previous_day' или 'last_24h'")
        if report.day_offset < 0:
            raise ConfigError("report.day_offset не может быть отрицательным")
        _check_range(report.top_n, 1, 1000, "report.top_n")
        _check_range(report.index_limit, 1, 10000, "report.index_limit")
        if not report.output_dir or not report.state_dir:
            raise ConfigError("report.output_dir и report.state_dir должны быть заданы")
        if not report.title.strip():
            raise ConfigError("report.title не может быть пустым")
        if report.utc_offset_hours is not None:
            _check_range(report.utc_offset_hours, -14, 14, "report.utc_offset_hours")
        try:
            load_timezone(report.timezone, report.utc_offset_hours)
        except (ValueError, TypeError) as exc:
            raise ConfigError(str(exc)) from exc

        _check_range(baseline.days, 1, 3650, "baseline.days")
        _check_range(baseline.min_days, 1, baseline.days, "baseline.min_days")
        _check_range(baseline.min_events, 1, 1_000_000, "baseline.min_events")
        _check_range(baseline.min_delta, 0, 1_000_000, "baseline.min_delta")
        _check_range(baseline.new_rule_days, 1, 3650, "baseline.new_rule_days")
        _check_range(baseline.noise_min_events, 1, 1_000_000, "baseline.noise_min_events")
        _check_range(baseline.noise_max_delta_pct, 0, 10000, "baseline.noise_max_delta_pct")
        _check_range(baseline.total_min_delta_pct, 0, 10000, "baseline.total_min_delta_pct")
        if baseline.z_threshold <= 0:
            raise ConfigError("baseline.z_threshold должен быть больше нуля")

        if self.email.enabled:
            if not self.email.host or not self.email.from_addr or not self.email.to:
                raise ConfigError(
                    "Для email.enabled=true нужны host, from_addr и хотя бы один адрес в to"
                )
            _check_range(self.email.port, 1, 65535, "email.port")

        if self.webhook.enabled:
            if not self.webhook.url.startswith(("http://", "https://")):
                raise ConfigError("webhook.url должен начинаться с http:// или https://")

        if self.logging.level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("logging.level: допустимо DEBUG, INFO, WARNING, ERROR, CRITICAL")
        self.logging.level = self.logging.level.upper()


def load_config(path: str | os.PathLike[str], apply_env: bool = True) -> Config:
    import json

    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Ошибка JSON в {config_path}: {exc}") from exc

    config = Config.from_dict(raw, source_path=str(config_path))
    if apply_env:
        _apply_env(config)
    config.validate()
    return config


def _apply_env(config: Config) -> None:
    token = os.environ.get("KUMA_TOKEN")
    if token:
        config.kuma.token = token.strip()
    password = os.environ.get("KUMA_PASSWORD")
    if password:
        config.kuma.password = password
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if smtp_password:
        config.email.password = smtp_password


def _build(cls: type, data: Any, prefix: str):
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"Раздел {prefix} должен быть объектом JSON")
    allowed = {item.name for item in fields(cls)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            f"Неизвестные параметры в разделе {prefix}: " + ", ".join(unknown)
        )
    values = dict(data)
    if cls is EmailConfig and isinstance(values.get("to"), str):
        raw_to = values["to"]
        values["to"] = [item.strip() for item in raw_to.split(",") if item.strip()]
    return cls(**values)


def _check_range(value: Any, minimum: float, maximum: float, name: str) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} должен быть числом") from exc
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} должен быть в диапазоне {minimum}..{maximum}")


def example_config_dict() -> dict[str, Any]:
    """Шаблон для команды init-config."""
    config = Config()
    config.kuma.token = "<API_TOKEN>"
    config.kuma.verify_tls = True
    config.report.timezone = "Europe/Moscow"
    config.report.utc_offset_hours = 3.0
    config.report.output_dir = "./reports"
    config.report.state_dir = "./state"
    config.logging.file = "./state/kuma_stats.log"
    data = config.to_dict()
    data["email"] = {
        "enabled": False,
        "host": "smtp.example.local",
        "port": 587,
        "use_ssl": False,
        "starttls": True,
        "username": "",
        "password": "",
        "from_addr": "kuma-stats@example.local",
        "to": ["soc@example.local"],
        "subject_prefix": "[KUMA]",
        "attach_html": True,
    }
    data["webhook"] = {
        "enabled": False,
        "url": "https://chat.example.local/hooks/xxx",
        "headers": {},
        "only_on_anomalies": False,
    }
    return data
