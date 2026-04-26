import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from sqlmodel import Session, select

from . import classifier
from .agent import get_agent
from .config import settings
from .db import engine
from .models import (
    Message,
    MessageDirection,
    MessageSource,
    OnboardingState,
    Patient,
    utcnow,
)
from .providers.evolution import EvolutionProvider
from .relay import get_relay_agent

log = logging.getLogger("obstetra.webhook")

router = APIRouter()

# Janela de inatividade após a qual um takeover manual é considerado encerrado.
HANDOVER_INACTIVITY_MINUTES = 10


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


def _last_message_timestamp(db: Session, patient_id: int, exclude_id: int | None = None):
    q = select(Message).where(Message.patient_id == patient_id)
    if exclude_id is not None:
        q = q.where(Message.id != exclude_id)
    q = q.order_by(Message.created_at.desc()).limit(1)
    msg = db.exec(q).first()
    return msg.created_at if msg else None


async def _process_message(phone: str, text: str, whatsapp_message_id: str | None) -> None:
    """Runs the full agent loop unless paciente está em takeover manual ativo."""
    with Session(engine) as db:
        patient = _get_or_create_patient(db, phone)

        # Persiste inbound primeiro (sempre — independente de takeover)
        inbound = Message(
            patient_id=patient.id,
            direction=MessageDirection.INBOUND,
            text=text,
            whatsapp_message_id=whatsapp_message_id,
            source=MessageSource.PATIENT,
        )
        db.add(inbound)
        db.commit()
        db.refresh(inbound)

        # Verifica se a paciente está sob atendimento manual da Dra.
        if patient.manual_handover_at is not None:
            now = utcnow()
            last_at = _last_message_timestamp(db, patient.id, exclude_id=inbound.id)
            inactive_for = (now - last_at) if last_at else None
            cutoff = timedelta(minutes=HANDOVER_INACTIVITY_MINUTES)

            if inactive_for is None or inactive_for > cutoff:
                # Mais de 10min sem interação → libera o takeover, bot reassume
                log.info(
                    "takeover de %s expirado por inatividade (%s) — bot reassume",
                    phone, inactive_for,
                )
                patient.manual_handover_at = None
                db.add(patient)
                db.commit()
                # Cai no fluxo normal abaixo
            else:
                # Ainda em takeover. Classifica se a paciente despediu.
                if await classifier.is_closing_message(text):
                    log.info("paciente %s sinalizou encerramento durante takeover — liberando", phone)
                    patient.manual_handover_at = None
                    db.add(patient)
                    db.commit()
                    # Não responde — paciente acabou de se despedir
                    return
                # Bot continua silencioso. Mensagem fica registrada pra Dra. ver no WhatsApp.
                log.info("paciente %s em takeover ativo — bot silente", phone)
                return

        # Fluxo normal: agente assume
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
    """Processa mensagens vindas do telefone pessoal da doutora via relay agent."""
    with Session(engine) as db:
        provider = EvolutionProvider()
        try:
            agent = get_relay_agent()
            await agent.handle_doctor_message(text=text, provider=provider, db=db)
        finally:
            await provider.aclose()


async def _process_manual_outbound(
    phone: str,
    text: str,
    whatsapp_message_id: str | None,
) -> None:
    """Alguém digitou manualmente no WhatsApp do bot (Dra. assumindo o atendimento).

    Marca a paciente como em takeover. Roda classificador pra detectar se já é uma
    despedida (caso em que o takeover é encerrado imediatamente).
    """
    with Session(engine) as db:
        patient = _get_or_create_patient(db, phone)

        # Persiste como manual
        db.add(Message(
            patient_id=patient.id,
            direction=MessageDirection.OUTBOUND,
            text=text,
            whatsapp_message_id=whatsapp_message_id,
            source=MessageSource.MANUAL,
        ))

        # Atualiza/cria o takeover (refresh do timer)
        is_closing = await classifier.is_closing_message(text)
        if is_closing:
            log.info("Dra. encerrou conversa com %s manualmente — sem takeover", phone)
            patient.manual_handover_at = None
        else:
            log.info("Dra. assumiu manualmente o atendimento de %s", phone)
            patient.manual_handover_at = utcnow()

        db.add(patient)
        db.commit()


def _is_known_bot_message(db: Session, msg_id: str | None) -> bool:
    """Verifica se a message_id veio do próprio bot (já registrada no DB com source=BOT)."""
    if not msg_id:
        return False
    msg = db.exec(
        select(Message).where(Message.whatsapp_message_id == msg_id)
    ).first()
    return msg is not None and msg.source == MessageSource.BOT


@router.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks) -> dict:
    payload = await request.json()
    event = payload.get("event")
    log.info("webhook event=%s instance=%s", event, payload.get("instance"))

    if event != "messages.upsert":
        return {"ignored": True}

    data = payload.get("data") or {}
    key = data.get("key") or {}
    remote_jid: str = key.get("remoteJid", "")
    if not remote_jid.endswith("@s.whatsapp.net"):
        return {"ignored": "non-dm"}

    phone = remote_jid.split("@", 1)[0]
    msg_id = key.get("id")
    text = _extract_text(data.get("message") or {})

    # ===== Mensagem outbound (do número do bot) =====
    if key.get("fromMe"):
        if not text:
            return {"ignored": "outbound-non-text"}

        # Se foi o próprio bot que enviou (já registrada no DB com source=BOT), ignora.
        # Caso contrário, é um humano (Dra. ou secretária) digitando manualmente
        # no WhatsApp do bot — sinal de takeover.
        with Session(engine) as db:
            if _is_known_bot_message(db, msg_id):
                return {"ignored": "bot-echo"}

        log.info("MANUAL outbound detected to=%s text=%r", phone, text[:120])
        background.add_task(_process_manual_outbound, phone, text, msg_id)
        return {"received": True, "manual_takeover": True, "phone": phone}

    # ===== Mensagem do telefone pessoal da doutora ==========
    if settings.doctor_phone_number and phone == settings.doctor_phone_number:
        if not text:
            log.info("doctor inbound non-text — ignoring for MVP")
            return {"ignored": "doctor-non-text"}
        log.info("inbound from DOCTOR text=%r", text[:120])
        background.add_task(_process_doctor_message, text)
        return {"received": True, "from": "doctor"}

    # ===== Mensagem inbound de uma paciente =====
    if not text:
        log.info("received non-text message from %s — ignoring for MVP", phone)
        return {"ignored": "non-text"}

    log.info("inbound from=%s text=%r", phone, text[:120])
    background.add_task(_process_message, phone, text, msg_id)

    return {"received": True, "phone": phone}
