"""Scheduler que envia ScheduledMessages quando chega o momento.

Roda como task asyncio dentro do lifespan do FastAPI. A cada `POLL_INTERVAL_SECONDS`
busca mensagens com `scheduled_at <= now()` e `sent_at IS NULL`, envia via Evolution
API e marca como enviada. Idempotente — `sent_at` evita reenvio mesmo se o container
reiniciar no meio.
"""

from __future__ import annotations

import asyncio
import logging

from sqlmodel import Session, select

from .config import settings
from .db import engine
from .models import (
    Message,
    MessageDirection,
    MessageSource,
    Patient,
    ScheduledMessage,
    utcnow,
)
from .providers.evolution import EvolutionProvider

log = logging.getLogger("obstetra.scheduler")

POLL_INTERVAL_SECONDS = 30
BATCH_SIZE = 20


async def _run_due_now() -> None:
    """Executa um ciclo: busca lembretes devidos e envia."""
    with Session(engine) as db:
        now = utcnow()
        due = db.exec(
            select(ScheduledMessage)
            .where(ScheduledMessage.sent_at.is_(None))
            .where(ScheduledMessage.cancelled_at.is_(None))
            .where(ScheduledMessage.scheduled_at <= now)
            .order_by(ScheduledMessage.scheduled_at)
            .limit(BATCH_SIZE)
        ).all()

        if not due:
            return

        log.info("scheduler: %d lembrete(s) devidos", len(due))
        provider = EvolutionProvider()
        try:
            for sched in due:
                patient = db.exec(
                    select(Patient).where(Patient.id == sched.patient_id)
                ).first()
                if not patient:
                    sched.error = f"paciente id={sched.patient_id} nao encontrada"
                    sched.sent_at = utcnow()  # marca como "processado" pra nao reprocessar
                    db.add(sched)
                    db.commit()
                    log.warning("lembrete id=%d: paciente sumiu", sched.id)
                    continue

                # Skip se a paciente está em takeover ativo
                if patient.manual_handover_at is not None:
                    log.info(
                        "lembrete id=%d para %s: takeover ativo — adiado por 5min",
                        sched.id, patient.phone,
                    )
                    # Adia 5min em vez de enviar / marcar enviado
                    from datetime import timedelta
                    sched.scheduled_at = utcnow() + timedelta(minutes=5)
                    db.add(sched)
                    db.commit()
                    continue

                try:
                    msg_id = await provider.send_text(patient.phone, sched.text)
                    # Persiste como mensagem outbound do bot
                    db.add(Message(
                        patient_id=patient.id,
                        direction=MessageDirection.OUTBOUND,
                        text=sched.text,
                        whatsapp_message_id=msg_id or None,
                        source=MessageSource.BOT,
                    ))
                    sched.sent_at = utcnow()
                    sched.error = None
                    db.add(sched)
                    db.commit()
                    log.info(
                        "lembrete id=%d enviado pra %s (msg=%s)",
                        sched.id, patient.phone, msg_id,
                    )
                except Exception as exc:
                    log.exception("lembrete id=%d falhou", sched.id)
                    sched.error = str(exc)[:500]
                    db.add(sched)
                    db.commit()
                    # Não marca sent_at — vai retentar no próximo ciclo
        finally:
            await provider.aclose()


async def _flush_stale_escalations() -> None:
    """Escaladas pendentes cuja conversa esfriou: envia pra doutora mesmo sem
    a paciente ter se despedido. Timeout: 5min pra red_flag, 10min pro resto."""
    from datetime import timedelta

    from .escalation import flush_pending_escalation
    from .models import Escalation, EscalationStatus

    with Session(engine) as db:
        pendings = db.exec(
            select(Escalation).where(Escalation.status == EscalationStatus.PENDING)
        ).all()
        if not pendings:
            return

        by_patient: dict[int, list] = {}
        for e in pendings:
            by_patient.setdefault(e.patient_id, []).append(e)

        now = utcnow()
        provider = EvolutionProvider()
        try:
            for pid, escs in by_patient.items():
                patient = db.exec(select(Patient).where(Patient.id == pid)).first()
                if not patient:
                    continue
                last_msg = db.exec(
                    select(Message)
                    .where(Message.patient_id == pid)
                    .order_by(Message.created_at.desc())
                    .limit(1)
                ).first()
                last_activity = last_msg.created_at if last_msg else max(e.created_at for e in escs)
                has_red_flag = any(e.reason == "red_flag" for e in escs)
                timeout_min = 5 if has_red_flag else 10
                if (now - last_activity) >= timedelta(minutes=timeout_min):
                    log.info(
                        "escalada pendente de %s esfriou (%dmin) — enviando por timeout",
                        patient.phone, timeout_min,
                    )
                    await flush_pending_escalation(db, provider, patient)
        finally:
            await provider.aclose()


async def scheduler_loop() -> None:
    """Loop infinito do scheduler. Rodado como background task no lifespan."""
    log.info("scheduler iniciado (poll a cada %ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            if settings.bot_paused:
                log.info("scheduler: bot_paused=true — pulando ciclo")
            else:
                await _run_due_now()
                await _flush_stale_escalations()
        except asyncio.CancelledError:
            log.info("scheduler cancelado — saindo")
            raise
        except Exception:
            log.exception("scheduler iteration falhou — continuando")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
