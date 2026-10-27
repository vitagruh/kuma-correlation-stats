"""Уведомления: электронная почта и webhook."""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

from .config import Config
from .utils import fmt_float, fmt_int, fmt_pct, parse_dt


@dataclass
class NotifyResult:
    email_sent: bool = False
    webhook_sent: bool = False
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def notify(
    config: Config,
    summary: dict[str, Any],
    html_path: Path | str | None,
    *,
    email: bool = True,
    webhook: bool = True,
    logger: logging.Logger | None = None,
) -> NotifyResult:
    log = logger or logging.getLogger("kuma_stats")
    result = NotifyResult()

    if email and config.email.enabled:
        try:
            send_email(config, summary, html_path, logger=log)
            result.email_sent = True
            log.info("Письмо отправлено: %s", ", ".join(config.email.to))
        except Exception as exc:  # noqa: BLE001 - уведомление не должно ломать отчёт
            result.errors.append(f"Email: {exc}")
            log.error("Не удалось отправить письмо: %s", exc)
    elif email:
        result.skipped.append("email выключен в конфигурации")

    if webhook and config.webhook.enabled:
        if config.webhook.only_on_anomalies and not _has_anomalies(config, summary):
            result.skipped.append("webhook: аномалий нет (only_on_anomalies=true)")
            log.info("Webhook пропущен: аномалий нет")
        else:
            try:
                send_webhook(config, summary, html_path, logger=log)
                result.webhook_sent = True
                log.info("Webhook отправлен: %s", config.webhook.url)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"Webhook: {exc}")
                log.error("Не удалось отправить webhook: %s", exc)
    elif webhook:
        result.skipped.append("webhook выключен в конфигурации")

    return result


def send_email(
    config: Config,
    summary: dict[str, Any],
    html_path: Path | str | None,
    *,
    logger: logging.Logger | None = None,
) -> None:
    log = logger or logging.getLogger("kuma_stats")
    email = config.email
    message = EmailMessage()
    message["Subject"] = compose_subject(config, summary)
    message["From"] = email.from_addr
    message["To"] = ", ".join(email.to)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=email.from_addr.split("@")[-1] or None)
    message.set_content(compose_text(summary))

    if email.attach_html and html_path:
        path = Path(html_path)
        if path.is_file():
            message.add_attachment(
                path.read_bytes(),
                maintype="text",
                subtype="html",
                filename=path.name,
                disposition="attachment",
            )
        else:
            log.warning("HTML-файл не найден, письмо будет без вложения: %s", path)

    context = ssl.create_default_context()
    timeout = 30
    if email.use_ssl:
        log.debug("SMTP SSL: %s:%s", email.host, email.port)
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            email.host, email.port, timeout=timeout, context=context
        )
    else:
        log.debug("SMTP: %s:%s (starttls=%s)", email.host, email.port, email.starttls)
        server = smtplib.SMTP(email.host, email.port, timeout=timeout)

    try:
        server.ehlo()
        if not email.use_ssl and email.starttls:
            server.starttls(context=context)
            server.ehlo()
        if email.username:
            server.login(email.username, email.password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            server.close()


def send_webhook(
    config: Config,
    summary: dict[str, Any],
    html_path: Path | str | None,
    *,
    logger: logging.Logger | None = None,
) -> None:
    log = logger or logging.getLogger("kuma_stats")
    payload = webhook_payload(summary, html_path)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "kuma-daily-stats/1.0",
    }
    headers.update({str(k): str(v) for k, v in config.webhook.headers.items()})
    request = urllib.request.Request(
        config.webhook.url, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"сетевая ошибка: {exc.reason}") from exc
    if not 200 <= status < 300:
        raise RuntimeError(f"неожиданный HTTP {status}")
    log.debug("Webhook ответил HTTP %s", status)


def compose_subject(config: Config, summary: dict[str, Any]) -> str:
    prefix = (config.email.subject_prefix or "").strip()
    day = _day_label(summary.get("day"))
    total = fmt_int(summary.get("total"))
    anomalies = int(summary.get("anomalies") or 0)
    new_rules = int(summary.get("new_rules") or 0)
    tail = f"сработок: {total}"
    if anomalies:
        tail += f", аномалий: {anomalies}"
    if new_rules:
        tail += f", новых правил: {new_rules}"
    subject = f"Сработки KUMA за {day}: {tail}"
    return f"{prefix} {subject}".strip()


def compose_text(summary: dict[str, Any]) -> str:
    day = _day_label(summary.get("day"))
    lines = [
        f"Отчёт по сработкам правил корреляции KUMA за {day}",
        "",
        f"Всего сработок:      {fmt_int(summary.get('total'))}",
    ]
    delta = summary.get("total_delta_pct")
    z = summary.get("total_z")
    if delta is not None:
        lines.append(
            f"Отклонение:          {fmt_pct(delta, signed=True)} к среднему"
            + (f" (z = {fmt_float(z)})" if z is not None else "")
        )
    lines.extend(
        [
            f"Уникальных правил:   {fmt_int(summary.get('unique_rules'))}",
            f"Источников:          {fmt_int(summary.get('unique_sources'))}",
            f"Аномалий:            {fmt_int(summary.get('anomalies'))}"
            f" (новых правил: {fmt_int(summary.get('new_rules'))})",
            f"Молчащих правил:     {fmt_int(summary.get('silent_rules'))}",
            f"Предупреждений:      {fmt_int(summary.get('warning_count'))}",
            f"История:             {fmt_int(summary.get('history_days'))} дн.",
        ]
    )

    top_rules = summary.get("top_rules") or []
    if top_rules:
        lines.extend(["", "Топ правил:"])
        for index, item in enumerate(top_rules[:5], start=1):
            lines.append(
                f"  {index}. {item.get('name')} — {fmt_int(item.get('count'))}"
                f" ({fmt_pct(item.get('share_pct'))})"
            )

    top_anomalies = summary.get("top_anomalies") or []
    if top_anomalies:
        lines.extend(["", "Аномалии:"])
        for item in top_anomalies[:10]:
            flags = ", ".join(item.get("flags") or [])
            details = []
            if item.get("delta_pct") is not None:
                details.append(fmt_pct(item.get("delta_pct"), signed=True))
            if item.get("z") is not None:
                details.append(f"z = {fmt_float(item.get('z'))}")
            suffix = f" [{'; '.join(details)}]" if details else ""
            lines.append(
                f"  • {item.get('name')}: {fmt_int(item.get('count'))}"
                f" — {flags}{suffix}"
            )

    lines.extend(
        [
            "",
            f"Полный отчёт: {summary.get('report') or 'report.html'}",
            "Письмо сформировано автоматически, отвечать на него не нужно.",
        ]
    )
    return "\n".join(lines) + "\n"


def webhook_payload(
    summary: dict[str, Any], html_path: Path | str | None
) -> dict[str, Any]:
    payload = {
        "event": "kuma_daily_report",
        "tool": "kuma-daily-stats",
        "day": summary.get("day"),
        "generated_at": summary.get("generated_at"),
        "cluster_id": summary.get("cluster_id"),
        "cluster_name": summary.get("cluster_name"),
        "total": summary.get("total"),
        "unique_rules": summary.get("unique_rules"),
        "unique_sources": summary.get("unique_sources"),
        "anomalies": summary.get("anomalies"),
        "new_rules": summary.get("new_rules"),
        "silent_rules": summary.get("silent_rules"),
        "total_delta_pct": summary.get("total_delta_pct"),
        "total_z": summary.get("total_z"),
        "warning_count": summary.get("warning_count"),
        "top_rules": summary.get("top_rules") or [],
        "top_anomalies": summary.get("top_anomalies") or [],
        "report": summary.get("report"),
    }
    if html_path:
        payload["report_path"] = str(Path(html_path).resolve())
    return payload


def _has_anomalies(config: Config, summary: dict[str, Any]) -> bool:
    if int(summary.get("anomalies") or 0) > 0:
        return True
    z = summary.get("total_z")
    delta = summary.get("total_delta_pct")
    if (
        z is None
        or delta is None
        or int(summary.get("history_days") or 0) < config.baseline.min_days
    ):
        return False
    return (
        abs(float(z)) >= config.baseline.z_threshold
        and abs(float(delta)) >= config.baseline.total_min_delta_pct
    )


def _day_label(value: Any) -> str:
    text = str(value or "")
    parsed = parse_dt(text)
    return parsed.strftime("%d.%m.%Y") if parsed else (text or "—")
