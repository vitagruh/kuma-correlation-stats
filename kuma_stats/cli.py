"""Командная строка kuma-daily-stats."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import TOOL_NAME, __version__
from .analytics import (
    build_rows,
    inventory_analysis,
    load_baseline,
    select_anomalies,
    silent_rules,
    total_anomaly,
)
from .collector import Collector
from .config import Config, ConfigError, example_config_dict, load_config
from .kuma_client import KumaAuthError, KumaClient, KumaError
from .models import DailyData
from .notify import notify
from .report import build_report
from .storage import HistoryStore
from .utils import (
    day_bounds,
    ensure_dir,
    fmt_int,
    inclusive_period,
    load_timezone,
    now_utc,
    read_json,
    write_json,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_KUMA = 3
EXIT_REPORT = 4
EXIT_ANOMALY = 5


@dataclass
class RunOutcome:
    summary: dict[str, Any]
    html_path: Path
    total_anomaly: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_CONFIG

    if args.command == "init-config":
        return _cmd_init_config(args)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        print(
            "Подсказка: создайте файл командой "
            f"`{TOOL_NAME} -c {args.config} init-config`.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    logger = _setup_logging(config, args.log_level)

    try:
        if args.command == "run":
            return _cmd_run(config, args, logger)
        if args.command == "check":
            return _cmd_check(config, args, logger)
        if args.command == "render":
            return _cmd_render(config, args, logger)
        if args.command == "daemon":
            return _cmd_daemon(config, args, logger)
    except KeyboardInterrupt:
        logger.warning("Остановлено пользователем")
        return EXIT_OK
    except ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return EXIT_CONFIG
    except KumaAuthError as exc:
        logger.error("Ошибка доступа к KUMA: %s", exc)
        return EXIT_KUMA
    except KumaError as exc:
        logger.error("Ошибка KUMA: %s", exc)
        return EXIT_KUMA
    except Exception as exc:  # noqa: BLE001 - верхняя граница CLI
        logger.exception("Неожиданная ошибка: %s", exc)
        return EXIT_ERROR

    parser.print_help()
    return EXIT_CONFIG


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #

def _cmd_run(config: Config, args: argparse.Namespace, logger: logging.Logger) -> int:
    if args.output_dir:
        config.report.output_dir = args.output_dir
    target = _parse_target_date(args.date, config)
    try:
        outcome = _execute_run(
            config,
            target,
            logger,
            write_store=not args.no_store,
            send_notifications=not args.no_notify,
        )
    except KumaAuthError as exc:
        logger.error("Доступ к KUMA отклонён: %s", exc)
        return EXIT_KUMA
    except KumaError as exc:
        logger.error("Сбор данных не выполнен: %s", exc)
        return EXIT_KUMA
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.exception("Не удалось сформировать отчёт: %s", exc)
        return EXIT_REPORT

    if args.fail_on_anomaly and (
        outcome.summary.get("anomalies") or outcome.total_anomaly
    ):
        logger.warning("Обнаружены аномалии — код возврата %s", EXIT_ANOMALY)
        return EXIT_ANOMALY
    return EXIT_OK


def _cmd_check(config: Config, args: argparse.Namespace, logger: logging.Logger) -> int:
    failures = 0
    print(f"{TOOL_NAME} {__version__} · проверка доступов")
    print(f"Конфигурация: {config.source_path or '—'}")
    print(f"KUMA: {config.kuma.base_url}")
    print()

    client: KumaClient | None = None
    try:
        client = KumaClient(config.kuma, logger)
        _print_step("Клиент и токен", True, "инициализированы")
    except KumaError as exc:
        _print_step("Клиент и токен", False, str(exc))
        return EXIT_KUMA

    try:
        info = client.whoami()
        name = info.get("login") or info.get("name") or "—"
        _print_step("Проверка токена (/users/whoami)", True, f"пользователь: {name}")
    except KumaError as exc:
        failures += 1
        _print_step(
            "Проверка токена (/users/whoami)",
            False,
            f"{exc}. Если эндпоинт отсутствует, задайте kuma.verify_token=false",
        )

    cluster_id, cluster_name = "", ""
    try:
        clusters = client.get_clusters()
        names = ", ".join(
            f"{item.get('name') or '—'} ({item.get('id') or '—'})"
            for item in clusters
        ) or "список пуст"
        _print_step("Кластеры хранения (/events/clusters)", True, names)
    except KumaError as exc:
        clusters = []
        failures += 1
        _print_step("Кластеры хранения (/events/clusters)", False, str(exc))

    try:
        cluster_id, cluster_name = client.resolve_cluster()
        label = cluster_name or cluster_id or "по умолчанию"
        _print_step("Выбор кластера", True, label)
    except KumaError as exc:
        failures += 1
        _print_step("Выбор кластера", False, str(exc))

    try:
        tenants = client.get_tenants()
        _print_step("Тенанты (/tenants)", True, f"получено: {len(tenants)}")
    except KumaError as exc:
        _print_step("Тенанты (/tenants)", False, f"{exc} (не критично)")

    if args.inventory:
        try:
            rules = client.get_correlation_rules()
            _print_step(
                "Инвентарь правил (/resources?kind=correlationRule)",
                True,
                f"получено: {len(rules)}",
            )
        except KumaError as exc:
            _print_step("Инвентарь правил", False, f"{exc} (не критично)")

    target = _parse_target_date(args.date, config)
    try:
        tz, _ = load_timezone(config.report.timezone, config.report.utc_offset_hours)
        start, end = day_bounds(target, tz)
        period_from, period_to = inclusive_period(start, end)
        sql = (
            f"SELECT count(ID) FROM `events` "
            f"WHERE Type = {int(config.kuma.event_type)}"
        )
        rows = client.search_events(sql, period_from, period_to, cluster_id=cluster_id)
        count = sum(_count(row) for row in rows)
        _print_step(
            f"Запрос событий Type={config.kuma.event_type} за {target.isoformat()}",
            True,
            f"сработок: {fmt_int(count)}",
        )
    except KumaError as exc:
        failures += 1
        _print_step("Запрос событий", False, str(exc))

    for label, path in (
        ("Каталог отчётов", config.report.output_dir),
        ("Каталог состояния", config.report.state_dir),
    ):
        try:
            ensure_dir(path)
            probe = Path(path) / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            _print_step(f"{label} ({path})", True, "доступен для записи")
        except OSError as exc:
            failures += 1
            _print_step(f"{label} ({path})", False, str(exc))

    print()
    if failures:
        print(f"Проверка завершена с ошибками: {failures}. См. сообщения выше.")
        return EXIT_KUMA
    print("Проверка успешно завершена. Можно запускать сбор.")
    return EXIT_OK


def _cmd_render(config: Config, args: argparse.Namespace, logger: logging.Logger) -> int:
    snapshot_path = _find_snapshot(config, args.path)
    if snapshot_path is None:
        logger.error("Снимок не найден. Укажите путь к snapshot.json или каталогу отчёта.")
        return EXIT_REPORT
    try:
        payload = read_json(snapshot_path)
        data = DailyData.from_dict(payload)  # type: ignore[arg-type]
    except (OSError, ValueError, TypeError) as exc:
        logger.error("Не удалось прочитать снимок %s: %s", snapshot_path, exc)
        return EXIT_REPORT

    store: HistoryStore | None = None
    try:
        store = HistoryStore(_history_path(config))
        baseline = load_baseline(store, config, data)
        rows = build_rows(data, baseline, config)
        anomalies = select_anomalies(rows)
        silent = silent_rules(data, baseline, config)
        inventory_info = inventory_analysis(data, baseline, config)
        result = build_report(
            config,
            data,
            baseline,
            rows,
            anomalies,
            silent,
            inventory_info,
            write_snapshot=False,
            out_html=args.out,
            write_index=not args.no_index,
            logger=logger,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.exception("Не удалось построить отчёт: %s", exc)
        return EXIT_REPORT
    finally:
        if store is not None:
            store.close()

    print(f"Отчёт перестроен: {result.html_path}")
    return EXIT_OK


def _cmd_daemon(config: Config, args: argparse.Namespace, logger: logging.Logger) -> int:
    at_hour, at_minute = _parse_time(args.at)
    tz, warning = load_timezone(config.report.timezone, config.report.utc_offset_hours)
    if warning:
        logger.warning(warning)
    if args.output_dir:
        config.report.output_dir = args.output_dir

    logger.info(
        "Режим демона: ежедневный запуск в %02d:%02d (%s)",
        at_hour,
        at_minute,
        config.report.timezone,
    )
    if args.run_now:
        _daemon_run(config, logger)

    while True:
        now_local = datetime.now(tz)
        next_run = now_local.replace(
            hour=at_hour, minute=at_minute, second=0, microsecond=0
        )
        if next_run <= now_local:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now_local).total_seconds()
        logger.info("Следующий запуск: %s (через %.1f ч)", next_run.strftime("%d.%m.%Y %H:%M"), wait_seconds / 3600)
        _sleep_until(wait_seconds)
        _daemon_run(config, logger)


def _cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.path or args.config or "config.json").expanduser()
    if path.exists() and not args.force:
        print(
            f"Файл уже существует: {path}. Используйте --force, чтобы перезаписать.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    try:
        write_json(path, example_config_dict())
    except OSError as exc:
        print(f"Не удалось записать {path}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Создан файл конфигурации: {path}")
    print(
        "Дальше: укажите kuma.base_url и kuma.token (или kuma.token_file), "
        "при необходимости поправьте report.timezone и email/webhook, "
        f"затем выполните `{TOOL_NAME} -c {path} check`."
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Общий сценарий сбора
# --------------------------------------------------------------------------- #

def _execute_run(
    config: Config,
    target: date | None,
    logger: logging.Logger,
    *,
    write_store: bool,
    send_notifications: bool,
) -> RunOutcome:
    started = time.monotonic()
    client = KumaClient(config.kuma, logger)
    store: HistoryStore | None = None
    try:
        data = Collector(config, client, logger).collect(target)
        if write_store:
            store = HistoryStore(_history_path(config))
            baseline = load_baseline(store, config, data)
            store.save_day(data)
        else:
            baseline = load_baseline(None, config, data)

        rows = build_rows(data, baseline, config)
        anomalies = select_anomalies(rows)
        silent = silent_rules(data, baseline, config)
        inventory_info = inventory_analysis(data, baseline, config)
        anomaly_state = total_anomaly(baseline, config)

        result = build_report(
            config,
            data,
            baseline,
            rows,
            anomalies,
            silent,
            inventory_info,
            write_snapshot=True,
            logger=logger,
        )

        logger.info(
            "Готово: %s сработок, %s правил, %s аномалий, %s предупреждений за %s с",
            fmt_int(data.total),
            fmt_int(len(rows)),
            fmt_int(len(anomalies)),
            len(data.warnings),
            f"{time.monotonic() - started:.1f}",
        )

        if send_notifications:
            outcome = notify(
                config,
                result.summary,
                result.html_path,
                logger=logger,
            )
            for item in outcome.skipped:
                logger.info("Уведомление пропущено: %s", item)

        return RunOutcome(
            summary=result.summary,
            html_path=result.html_path,
            total_anomaly=bool(anomaly_state.get("is_anomaly")),
        )
    finally:
        if store is not None:
            store.close()


def _daemon_run(config: Config, logger: logging.Logger) -> None:
    try:
        _execute_run(
            config,
            None,
            logger,
            write_store=True,
            send_notifications=True,
        )
    except Exception as exc:  # noqa: BLE001 - демон должен пережить сбой
        logger.exception("Ежедневный запуск не удался: %s", exc)


def _sleep_until(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 300.0))


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Ежедневная статистика сработок правил корреляции Kaspersky KUMA: "
            "сбор через REST API v3, история, аномалии, HTML-отчёт и графики."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            f"  {TOOL_NAME} -c config.json init-config\n"
            f"  {TOOL_NAME} -c config.json check\n"
            f"  {TOOL_NAME} -c config.json run\n"
            f"  {TOOL_NAME} -c config.json run --date 2026-10-26\n"
            f"  {TOOL_NAME} -c config.json render reports/2026-10-26\n"
            f"  {TOOL_NAME} -c config.json daemon --at 01:00\n"
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("KUMA_STATS_CONFIG", "config.json"),
        help="путь к JSON-конфигурации (по умолчанию config.json)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="уровень логирования (переопределяет logging.level)",
    )
    parser.add_argument(
        "--version", action="version", version=f"{TOOL_NAME} {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="команда")

    run = sub.add_parser("run", help="собрать данные и сформировать отчёт за сутки")
    run.add_argument(
        "--date",
        help="дата в формате YYYY-MM-DD (по умолчанию — предыдущие сутки)",
    )
    run.add_argument("--output-dir", help="переопределить report.output_dir")
    run.add_argument(
        "--no-store", action="store_true", help="не читать и не писать историю"
    )
    run.add_argument(
        "--no-notify", action="store_true", help="не отправлять email и webhook"
    )
    run.add_argument(
        "--fail-on-anomaly",
        action="store_true",
        help=f"вернуть код {EXIT_ANOMALY}, если найдены аномалии",
    )

    check = sub.add_parser("check", help="проверить доступы, кластер и запросы")
    check.add_argument("--date", help="дата проверочного запроса (YYYY-MM-DD)")
    check.add_argument(
        "--inventory",
        action="store_true",
        help="дополнительно запросить инвентарь правил корреляции",
    )

    render = sub.add_parser(
        "render", help="перестроить отчёт из сохранённого snapshot.json"
    )
    render.add_argument(
        "path",
        nargs="?",
        help="путь к snapshot.json или каталогу отчёта (по умолчанию — последний)",
    )
    render.add_argument("--out", help="путь к HTML-файлу (по умолчанию — рядом со снимком)")
    render.add_argument(
        "--no-index", action="store_true", help="не обновлять архив отчётов"
    )

    daemon = sub.add_parser("daemon", help="запускать сбор ежедневно по расписанию")
    daemon.add_argument(
        "--at",
        default="01:00",
        help="локальное время запуска ЧЧ:ММ (по умолчанию 01:00)",
    )
    daemon.add_argument(
        "--run-now", action="store_true", help="выполнить запуск сразу при старте"
    )
    daemon.add_argument("--output-dir", help="переопределить report.output_dir")

    init = sub.add_parser(
        "init-config", help="создать шаблон config.json"
    )
    init.add_argument("path", nargs="?", help="куда записать шаблон")
    init.add_argument("--force", action="store_true", help="перезаписать существующий файл")

    return parser


def _setup_logging(config: Config, level_override: str | None) -> logging.Logger:
    logger = logging.getLogger("kuma_stats")
    logger.handlers.clear()
    logger.propagate = False
    level = (level_override or config.logging.level or "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if config.logging.file:
        try:
            ensure_dir(Path(config.logging.file).expanduser().parent)
            handler = logging.handlers.RotatingFileHandler(
                str(Path(config.logging.file).expanduser()),
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except OSError as exc:
            logger.warning("Не удалось открыть лог-файл %s: %s", config.logging.file, exc)

    return logger


def _history_path(config: Config) -> Path:
    return Path(config.report.state_dir) / "history.sqlite3"


def _parse_target_date(value: str | None, config: Config) -> date | None:
    if not value:
        return None
    text = value.strip().lower()
    tz, _ = load_timezone(config.report.timezone, config.report.utc_offset_hours)
    today = now_utc().astimezone(tz).date()
    if text in {"today", "сегодня"}:
        return today
    if text in {"yesterday", "вчера"}:
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(
            f"Некорректная дата '{value}'. Ожидается YYYY-MM-DD, today или yesterday."
        ) from exc


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hours_text, minutes_text = value.split(":", 1)
        hours = int(hours_text)
        minutes = int(minutes_text)
    except (AttributeError, ValueError) as exc:
        raise ConfigError(f"Некорректное время '{value}'. Ожидается ЧЧ:ММ.") from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ConfigError(f"Некорректное время '{value}'. Допустимо 00:00..23:59.")
    return hours, minutes


def _find_snapshot(config: Config, value: str | None) -> Path | None:
    if value:
        path = Path(value)
        if path.is_dir():
            candidate = path / "snapshot.json"
            return candidate if candidate.is_file() else None
        return path if path.is_file() else None

    output = Path(config.report.output_dir)
    if not output.is_dir():
        return None
    candidates: list[Path] = []
    for child in output.iterdir():
        snapshot = child / "snapshot.json"
        if child.is_dir() and snapshot.is_file():
            candidates.append(snapshot)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.parent.name, reverse=True)
    return candidates[0]


def _print_step(label: str, ok: bool, detail: str) -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {label}: {detail}")


def _count(row: dict[str, Any]) -> int:
    for key, value in row.items():
        if str(key).lower().startswith("count("):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0
    return 0
