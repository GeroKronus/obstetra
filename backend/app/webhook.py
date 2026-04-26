import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlmodel import Session, select

from .agent import get_agent
from .config import settings
from .db import engine, get_session
from .models import Message, MessageDirection, OnboardingState, Patient
from .providers.evolution import EvolutionProvider
from .relay import get_relay_agent

log = logging.getLogger("obstetra.webhook")

router = APIRouter()


def _extract_text(message: dict[str, Any]) -> str | None:
    if not message:
        return None
    if "conversation" in message:
        return message["conversation"]
    ext = message.get("extendedTextMessage")
    if ext and "text" in ext:
        return ext["text"]
    return None


def _get_or_create_patient(db: Session, phone: str) -> Patient:
    existing = db.exec(select(Patient).where(Patient.phone == phone)).first()
    if existing:
        return existing
    patient = Patient(
        phone=phone,
        onboarding_state=OnboardingState.NEW,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


async def _process_message(phone: str, text: str, whatsapp_message_id: str | None) -> None:
    """Runs the full agent loop. Uses its own DB session since it runs in a background task."""
    with Session(engine) as db:
        patient = _get_or_create_patient(db, phone)

        db.add(
            Message(
                patient_id=patient.id,
                direction=MessageDirection.INBOUND,
                text=text,
                whatsapp_message_id=whatsapp_message_id,
            )
        )
        db.commit()

        provider = EvolutionProvider()
        try:
            agent = get_agent()
            await agent.handle_inbound(
                patient=patient,
                inbound_text=text,
                provider=provider,
                db=db,
            )
        finally:
            await provider.aclose()


async def _process_doctor_message(text: str) -> None:
    """Processa mensagens vindas do telefone da doutora via relay agent."""
    with Session(engine) as db:
        provider = EvolutionProvider()
        try:
            agent = get_relay_agent()
            await agent.handle_doctor_message(text=text, provider=provider, db=db)
        finally:
            await provider.aclose()


@router.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks) -> dict:
    payload = await request.json()
    event = payload.get("event")
    log.info("webhook event=%s instance=%s", event, payload.get("instance"))

    if event != "messages.upsert":
        return {"ignored": True}

    data = payload.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return {"ignored": "fromMe"}

    remote_jid: str = key.get("remoteJid", "")
    if not remote_jid.endswith("@s.whatsapp.net"):
        return {"ignored": "non-dm"}

    phone = remote_jid.split("@", 1)[0]

    # Mensagens vindas do telefone da doutora vão pro relay agent
    # (encaminhar resposta dela à paciente, pedir clarificação, etc.)
    if settings.doctor_phone_number and phone == settings.doctor_phone_number:
        text = _extract_text(data.get("message") or {})
        if not text:
            log.info("doctor inbound non-text — ignoring for MVP")
            return {"ignored": "doctor-non-text"}
        log.info("inbound from DOCTOR text=%r", text[:120])
        background.add_task(_process_doctor_message, text)
        return {"received": True, "from": "doctor"}

    text = _extract_text(data.get("message") or {})
    if not text:
        log.info("received non-text message from %s — ignoring for MVP", phone)
        return {"ignored": "non-text"}

    log.info("inbound from=%s text=%r", phone, text[:120])

    whatsapp_message_id = key.get("id")
    # Run the agent asynchronously so the webhook responds to Evolution API immediately.
    background.add_task(_process_message, phone, text, whatsapp_message_id)

    return {"received": True, "phone": phone}
