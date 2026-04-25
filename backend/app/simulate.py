"""Endpoint de simulacao: recebe {phone, text} e roda o agente sem enviar
ao WhatsApp. Usado pra testar a integracao com o vault sem precisar parear
um numero real."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import Session

from .agent import get_agent
from .config import settings
from .db import engine
from .models import Message, MessageDirection
from .providers.buffered import BufferedProvider
from .webhook import _get_or_create_patient

log = logging.getLogger("obstetra.simulate")

router = APIRouter()


class SimulateRequest(BaseModel):
    phone: str
    text: str


class SimulateResponse(BaseModel):
    messages_to_patient: list[str]
    messages_to_doctor: list[str]
    patient_state: dict


@router.post("/simulate", response_model=SimulateResponse)
async def simulate(req: SimulateRequest) -> SimulateResponse:
    log.info("simulate inbound from=%s text=%r", req.phone, req.text[:120])

    with Session(engine) as db:
        patient = _get_or_create_patient(db, req.phone)
        db.add(Message(
            patient_id=patient.id,
            direction=MessageDirection.INBOUND,
            text=req.text,
        ))
        db.commit()

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
        patient_state=state,
    )


class SimulateResetResponse(BaseModel):
    deleted_patient: bool
    deleted_messages: int


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
