"""Saúde do monitor: estado das verificações e do Telegram, mostrado na página e usado pelo vigia.

O estado fica em memória: o job roda no mesmo processo, então um job preso ou atrasado é visível aqui.
Se o processo inteiro cair, quem avisa é o Railway (notificações de deploy/crash).
"""
import html
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, text

from app.config import (
    CHECK_INTERVAL_HOURS,
    HEALTH_ALERT_REPEAT_HOURS,
    HEALTH_STUCK_MINUTES,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
)
from app.db import Product, SessionLocal, utcnow

logger = logging.getLogger(__name__)

JOB_ID = "check_all_products"
STALE_EXTRA_MINUTES = 10
MIN_FAILING_LINKS = 3


@dataclass(frozen=True)
class Issue:
    key: str
    level: str  # "error" | "warn"
    message: str


_lock = threading.Lock()
_started_at = utcnow()
_run = {"started": None, "finished": None, "duration": None, "total": 0, "done": 0}
_telegram = {"ok": None, "at": None, "error": None}
_alerted: dict[str, datetime] = {}
_scheduler = None


def set_scheduler(scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def run_started(total: int) -> None:
    with _lock:
        _run.update(started=utcnow(), total=total, done=0)


def product_done() -> None:
    with _lock:
        _run["done"] += 1


def run_finished(duration_seconds: float) -> None:
    with _lock:
        _run.update(finished=utcnow(), duration=duration_seconds)


def record_telegram(ok: bool, error: Optional[str] = None) -> None:
    with _lock:
        _telegram.update(ok=ok, at=utcnow(), error=None if ok else error)


def telegram_configured() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def snapshot() -> dict:
    with _lock:
        run = dict(_run)
    started, finished = run["started"], run["finished"]
    run["running"] = started is not None and (finished is None or finished < started)
    return run


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def format_age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def next_run_at() -> Optional[datetime]:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    next_run = getattr(job, "next_run_time", None) if job else None
    return next_run.astimezone(timezone.utc).replace(tzinfo=None) if next_run else None


def collect_db_facts() -> dict:
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            active = session.query(func.count(Product.id)).filter(Product.active.is_(True)).scalar() or 0
            failing = (
                session.query(func.count(Product.id))
                .filter(Product.active.is_(True), Product.last_error.isnot(None))
                .scalar()
                or 0
            )
        return {"db_ok": True, "db_error": None, "links_active": active, "links_failing": failing}
    except Exception as exc:
        return {"db_ok": False, "db_error": type(exc).__name__, "links_active": None, "links_failing": None}


def evaluate(facts: dict, now: Optional[datetime] = None) -> list[Issue]:
    now = now or utcnow()
    run = snapshot()
    issues = []

    if not facts["db_ok"]:
        issues.append(
            Issue("db_down", "error", f"Banco de dados inacessível ({facts['db_error']}): verificações e cadastros podem falhar.")
        )

    if run["running"]:
        running_for = now - run["started"]
        if running_for > timedelta(minutes=HEALTH_STUCK_MINUTES):
            issues.append(
                Issue(
                    "job_stuck",
                    "error",
                    f"A rodada de verificação está em andamento há {format_age(running_for)} "
                    f"({run['done']}/{run['total']} links); pode estar travada.",
                )
            )
    else:
        reference = run["finished"] or _started_at
        limit = timedelta(hours=CHECK_INTERVAL_HOURS * 1.5, minutes=STALE_EXTRA_MINUTES)
        if now - reference > limit:
            issues.append(
                Issue(
                    "job_stale",
                    "error",
                    f"Nenhuma verificação concluída há {format_age(now - reference)} "
                    f"(esperado a cada {CHECK_INTERVAL_HOURS:g} h).",
                )
            )

    if not telegram_configured():
        issues.append(Issue("telegram_off", "warn", "Telegram não configurado: nenhum alerta de preço será enviado."))
    elif _telegram["ok"] is False:
        issues.append(Issue("telegram_failed", "warn", f"O último envio ao Telegram falhou ({_telegram['error']})."))

    total, failing = facts["links_active"], facts["links_failing"]
    if total and failing and failing >= MIN_FAILING_LINKS and failing / total > 0.5:
        issues.append(Issue("many_unreadable", "warn", f"{failing} de {total} links ativos estão sem leitura de preço."))

    return issues


def view(facts: dict, now: Optional[datetime] = None) -> dict:
    issues = evaluate(facts, now)
    run = snapshot()
    level = "error" if any(i.level == "error" for i in issues) else "warn" if issues else "ok"

    if not telegram_configured():
        telegram = "Telegram não configurado"
    elif _telegram["ok"] is False:
        telegram = "Telegram: último envio falhou"
    elif _telegram["ok"]:
        telegram = "Telegram ok"
    else:
        telegram = "Telegram configurado"

    return {
        "level": level,
        "title": {"ok": "Tudo certo", "warn": "Atenção", "error": "Problema"}[level],
        "issues": issues,
        "running": run["running"],
        "progress": f"{run['done']}/{run['total']}",
        "last_finished": run["finished"],
        "last_duration": format_duration(run["duration"]),
        "last_total": run["total"],
        "next_run": next_run_at(),
        "telegram": telegram,
        "links_active": facts["links_active"],
        "links_failing": facts["links_failing"],
    }


def check_and_alert(now: Optional[datetime] = None) -> None:
    """Vigia: avisa no Telegram quando surge um problema grave (e quando ele some). Roda a cada poucos minutos."""
    from app.notifier import send_telegram_message  # import tardio: o notifier também usa este módulo

    if not telegram_configured():
        return

    now = now or utcnow()
    errors = {issue.key: issue for issue in evaluate(collect_db_facts(), now) if issue.level == "error"}
    repeat = timedelta(hours=HEALTH_ALERT_REPEAT_HOURS)

    for key, issue in errors.items():
        last = _alerted.get(key)
        if last is None or now - last >= repeat:
            if send_telegram_message(f"⚠️ <b>Monitor de preços</b>\n{html.escape(issue.message)}"):
                _alerted[key] = now
            else:
                logger.warning("Não foi possível enviar o aviso de saúde '%s' ao Telegram.", key)

    recovered = [key for key in _alerted if key not in errors]
    if recovered and send_telegram_message("✅ <b>Monitor de preços</b>\nO problema avisado foi resolvido; o monitor voltou ao normal."):
        for key in recovered:
            del _alerted[key]
