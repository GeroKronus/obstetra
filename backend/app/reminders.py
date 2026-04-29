"""Auto-criacao de lembretes 24h antes de cada Appointment.

Quando uma Appointment e criada, sincroniza um ScheduledMessage linkado
(appointment_id) que sera disparado pelo scheduler 24h antes da consulta
pedindo confirmacao a paciente.

Quando a consulta e remarcada, atualiza o scheduled_at do lembrete.
Quando a consulta e cancelada, cancela o lembrete linkado.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .models import (
    Appointment,
    AppointmentStatus,
    Patient,
    ScheduledMessage,
    utcnow,
)

log = logging.getLogger("obstetra.reminders")

_BRT = ZoneInfo("America/Sao_Paulo")


def _build_reminder_text(patient: Patient, appointment: Appointment) -> str:
    """Texto fixo (sem LLM) do lembrete pedindo confirmacao."""
    when_brt = appointment.scheduled_at.replace(tzinfo=timezone.utc).astimezone(_BRT)
    nome = patient.name or "tudo bem?"
    nome_first = nome.split()[0] if nome and " " in nome else nome
    hora = when_brt.strftime("%Hh%M").replace("h00", "h")
    weekday = ['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo'][when_brt.weekday()]
    data = when_brt.strftime("%d/%m")
    tipo = (appointment.tipo.value if hasattr(appointment.tipo, "value") else str(appointment.tipo)).lower()

    return (
        f"Oi {nome_first}! Passando pra te lembrar da sua *{tipo}* "
        f"com a Dra. Leiza amanhã ({weekday}, {data}) às {hora}.\n\n"
        f"Pode me confirmar se vai conseguir vir? Responde só *sim* ou *não*. 🙏"
    )


def _compute_reminder_at(appointment_at_utc: datetime) -> Optional[datetime]:
    """Calcula quando enviar o lembrete: 24h antes. Se faltam <24h, envia
    daqui 1 minuto (pra paciente recebar logo). Se appointment ja passou, retorna None."""
    now = utcnow()
    if appointment_at_utc <= now:
        return None

    target = appointment_at_utc - timedelta(hours=24)
    if target <= now:
        # Consulta com <24h de antecedencia — manda daqui 1min
        target = now + timedelta(minutes=1)
    return target


def sync_appointment_reminder(db: Session, appointment: Appointment) -> Optional[ScheduledMessage]:
    """Cria/atualiza/cancela o ScheduledMessage linkado a essa Appointment.
    Idempotente. Chama nos eventos: criar, remarcar, cancelar consulta.

    Retorna o ScheduledMessage afetado (ou None se nada precisou fazer).
    """
    # Busca lembrete linkado (se existe)
    existing = db.exec(
        select(ScheduledMessage)
        .where(ScheduledMessage.appointment_id == appointment.id)
    ).first()

    # Se a consulta foi cancelada/realizada/falta — cancela o lembrete (se ainda nao foi enviado)
    is_active = appointment.status in (AppointmentStatus.AGENDADA, AppointmentStatus.CONFIRMADA, AppointmentStatus.REMARCADA)
    if not is_active:
        if existing and existing.sent_at is None and existing.cancelled_at is None:
            existing.cancelled_at = utcnow()
            db.add(existing)
            db.commit()
            log.info("lembrete consulta id=%d cancelado (consulta status=%s)", appointment.id, appointment.status)
        return existing

    # Calcula quando enviar
    target = _compute_reminder_at(appointment.scheduled_at)
    if target is None:
        # Consulta ja passou — nao cria lembrete
        return None

    patient = db.exec(select(Patient).where(Patient.id == appointment.patient_id)).first()
    if not patient:
        return None

    text = _build_reminder_text(patient, appointment)

    if existing:
        # Se ja foi enviado, nao mexe (paciente ja foi lembrada)
        if existing.sent_at is not None:
            return existing

        # Atualiza scheduled_at + texto (caso consulta foi remarcada)
        if existing.cancelled_at is not None:
            # Re-ativa um lembrete cancelado
            existing.cancelled_at = None
        existing.scheduled_at = target
        existing.text = text
        db.add(existing)
        db.commit()
        log.info("lembrete consulta id=%d atualizado pra %s", appointment.id, target.isoformat(timespec="minutes"))
        return existing

    # Cria novo
    sched = ScheduledMessage(
        tenant_id=appointment.tenant_id,
        patient_id=appointment.patient_id,
        appointment_id=appointment.id,
        text=text,
        scheduled_at=target,
        created_by="appointment_reminder",
    )
    db.add(sched)
    db.commit()
    db.refresh(sched)
    log.info("lembrete consulta id=%d criado (sched id=%d, %s)", appointment.id, sched.id, target.isoformat(timespec="minutes"))
    return sched
