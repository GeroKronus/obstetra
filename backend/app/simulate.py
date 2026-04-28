"""Endpoint de simulacao: recebe {phone, text} e roda o agente sem enviar
ao WhatsApp. Usado pra testar a integracao com o vault sem precisar parear
um numero real."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import Session, select

from .agent import get_agent
from .config import settings
from .db import engine
from .models import Message, MessageDirection, ScheduledMessage
from .providers.buffered import BufferedProvider
from .relay import get_relay_agent
from .webhook import _get_or_create_patient

log = logging.getLogger("obstetra.simulate")

router = APIRouter()


class SimulateRequest(BaseModel):
    phone: str
    text: str


class EscalationInfo(BaseModel):
    motivo: str
    resumo: str
    notified_doctor: bool


class SimulateResponse(BaseModel):
    messages_to_patient: list[str]
    messages_to_doctor: list[str]
    escalation_this_turn: EscalationInfo | None
    patient_state: dict


@router.post("/simulate", response_model=SimulateResponse)
async def simulate(req: SimulateRequest) -> SimulateResponse:
    from sqlmodel import select
    from .models import Escalation

    log.info("simulate inbound from=%s text=%r", req.phone, req.text[:120])

    with Session(engine) as db:
        patient = _get_or_create_patient(db, req.phone)
        db.add(Message(
            patient_id=patient.id,
            direction=MessageDirection.INBOUND,
            text=req.text,
        ))
        db.commit()

        # Marca o numero de Escalation antes do turno pra detectar quais foram criadas
        prior_escalation_count = len(
            db.exec(select(Escalation).where(Escalation.patient_id == patient.id)).all()
        )

        provider = BufferedProvider()
        agent = get_agent()
        await agent.handle_inbound(
            patient=patient,
            inbound_text=req.text,
            provider=provider,
            db=db,
        )

        doctor_phone = settings.doctor_phone_number or ""
        to_patient = [t for (p, t) in provider.sent if p == req.phone]
        to_doctor = [t for (p, t) in provider.sent if doctor_phone and p == doctor_phone]

        # Pega a escalada criada neste turno (se houver)
        escalations = db.exec(
            select(Escalation)
            .where(Escalation.patient_id == patient.id)
            .order_by(Escalation.created_at.desc())
        ).all()
        escalation_this_turn: EscalationInfo | None = None
        if len(escalations) > prior_escalation_count:
            latest = escalations[0]
            escalation_this_turn = EscalationInfo(
                motivo=latest.reason,
                resumo=latest.summary,
                notified_doctor=latest.notified_doctor,
            )

        db.refresh(patient)
        state = {
            "phone": patient.phone,
            "name": patient.name,
            "gestational_weeks": patient.gestational_weeks,
            "is_doctor_patient": patient.is_doctor_patient,
            "onboarding_state": patient.onboarding_state.value,
        }

    return SimulateResponse(
        messages_to_patient=to_patient,
        messages_to_doctor=to_doctor,
        escalation_this_turn=escalation_this_turn,
        patient_state=state,
    )


class SimulateResetResponse(BaseModel):
    deleted_patient: bool
    deleted_messages: int


class SimulateDoctorRequest(BaseModel):
    text: str


class ScheduledLite(BaseModel):
    id: int
    patient_phone: str
    scheduled_at_utc: str
    scheduled_at_brt: str
    text: str


class SimulateDoctorResponse(BaseModel):
    messages_to_doctor: list[str]
    messages_to_patients: list[dict]
    scheduled_now: list[ScheduledLite]


@router.post("/simulate/doctor", response_model=SimulateDoctorResponse)
async def simulate_doctor(req: SimulateDoctorRequest) -> SimulateDoctorResponse:
    """Simula uma mensagem da doutora pro relay agent (sem usar Evolution API real).
    Retorna o que o agente teria respondido e quais ScheduledMessages foram criadas."""
    log.info("simulate_doctor inbound text=%r", req.text[:120])
    from .models import Patient

    with Session(engine) as db:
        cutoff = datetime.utcnow()
        provider = BufferedProvider()
        agent = get_relay_agent()
        await agent.handle_doctor_message(text=req.text, provider=provider, db=db)

        # Captura mensagens enviadas
        doctor_phone = settings.doctor_phone_number or ""
        to_doctor = [t for (p, t) in provider.sent if doctor_phone and p == doctor_phone]
        to_patients = [
            {"phone": p, "text": t}
            for (p, t) in provider.sent
            if not doctor_phone or p != doctor_phone
        ]

        # Captura agendamentos criados durante esse turno
        new_scheds = db.exec(
            select(ScheduledMessage)
            .where(ScheduledMessage.created_at >= cutoff)
            .order_by(ScheduledMessage.created_at)
        ).all()

        scheduled_lite: list[ScheduledLite] = []
        brt = ZoneInfo("America/Sao_Paulo")
        for s in new_scheds:
            patient = db.exec(select(Patient).where(Patient.id == s.patient_id)).first()
            scheduled_lite.append(ScheduledLite(
                id=s.id or 0,
                patient_phone=patient.phone if patient else "?",
                scheduled_at_utc=s.scheduled_at.isoformat(timespec="minutes"),
                scheduled_at_brt=s.scheduled_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(brt).strftime("%d/%m/%Y %H:%M %Z"),
                text=s.text,
            ))

    return SimulateDoctorResponse(
        messages_to_doctor=to_doctor,
        messages_to_patients=to_patients,
        scheduled_now=scheduled_lite,
    )


class CancelScheduledResponse(BaseModel):
    cancelled_count: int
    cancelled_ids: list[int]


@router.post("/simulate/cancel-all-scheduled", response_model=CancelScheduledResponse)
async def cancel_all_scheduled() -> CancelScheduledResponse:
    """Cancela todos os ScheduledMessage que ainda nao foram enviados nem cancelados."""
    from datetime import datetime as dt

    with Session(engine) as db:
        pending = db.exec(
            select(ScheduledMessage)
            .where(ScheduledMessage.sent_at.is_(None))
            .where(ScheduledMessage.cancelled_at.is_(None))
        ).all()

        ids: list[int] = []
        now = dt.utcnow()
        for s in pending:
            s.cancelled_at = now
            db.add(s)
            ids.append(s.id or 0)
        db.commit()

    log.info("cancelados %d lembretes pendentes: %s", len(ids), ids)
    return CancelScheduledResponse(cancelled_count=len(ids), cancelled_ids=ids)


@router.post("/simulate/cancel-scheduled/{sched_id}", response_model=CancelScheduledResponse)
async def cancel_scheduled(sched_id: int) -> CancelScheduledResponse:
    """Cancela um ScheduledMessage especifico por id."""
    from datetime import datetime as dt

    with Session(engine) as db:
        s = db.exec(
            select(ScheduledMessage).where(ScheduledMessage.id == sched_id)
        ).first()
        if not s:
            return CancelScheduledResponse(cancelled_count=0, cancelled_ids=[])
        if s.sent_at is not None:
            log.info("cancel #%d ignorado — ja foi enviado", sched_id)
            return CancelScheduledResponse(cancelled_count=0, cancelled_ids=[])
        if s.cancelled_at is not None:
            log.info("cancel #%d ignorado — ja estava cancelado", sched_id)
            return CancelScheduledResponse(cancelled_count=0, cancelled_ids=[])
        s.cancelled_at = dt.utcnow()
        db.add(s)
        db.commit()

    log.info("cancelado lembrete #%d", sched_id)
    return CancelScheduledResponse(cancelled_count=1, cancelled_ids=[sched_id])


@router.get("/simulate/scheduled", response_model=list[dict])
async def list_scheduled() -> list[dict]:
    """Lista todos os agendamentos com status."""
    with Session(engine) as db:
        rows = db.exec(
            select(ScheduledMessage).order_by(ScheduledMessage.scheduled_at)
        ).all()
        return [
            {
                "id": s.id,
                "patient_id": s.patient_id,
                "scheduled_at": s.scheduled_at.isoformat(timespec="minutes"),
                "sent_at": s.sent_at.isoformat(timespec="minutes") if s.sent_at else None,
                "cancelled_at": s.cancelled_at.isoformat(timespec="minutes") if s.cancelled_at else None,
                "text": s.text[:100] + ("..." if len(s.text) > 100 else ""),
            }
            for s in rows
        ]


@router.delete("/simulate/{phone}", response_model=SimulateResetResponse)
async def reset(phone: str) -> SimulateResetResponse:
    """Limpa o estado simulado de uma paciente no banco SQLite (nao mexe no vault)."""
    from sqlmodel import select
    from .models import Escalation, Patient

    with Session(engine) as db:
        patient = db.exec(select(Patient).where(Patient.phone == phone)).first()
        if not patient:
            return SimulateResetResponse(deleted_patient=False, deleted_messages=0)
        msgs = db.exec(select(Message).where(Message.patient_id == patient.id)).all()
        for m in msgs:
            db.delete(m)
        for e in db.exec(select(Escalation).where(Escalation.patient_id == patient.id)).all():
            db.delete(e)
        db.delete(patient)
        db.commit()
        return SimulateResetResponse(deleted_patient=True, deleted_messages=len(msgs))
