import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from sqlmodel import Session, select

from . import classifier, vision
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


# Detecção de mídia que o bot não processa (audio, imagem, PDF etc.)
_MEDIA_TYPES = {
    "audioMessage": "audio",
    "pttMessage": "audio",   # push-to-talk — mensagem de voz
    "imageMessage": "imagem",
    "videoMessage": "video",
    "documentMessage": "documento",
    "stickerMessage": "figurinha",
    "locationMessage": "localizacao",
    "contactMessage": "contato",
    "contactsArrayMessage": "contato",
}


def _detect_media_type(message: dict[str, Any]) -> str | None:
    """Retorna o tipo de mídia (audio/imagem/etc.) ou None se for texto/desconhecido."""
    if not message:
        return None
    for key, label in _MEDIA_TYPES.items():
        if key in message:
            return label
    return None


def _media_rejection_text(media_type: str) -> str:
    if media_type == "audio":
        return (
            "Oi! Vi que você me mandou uma mensagem de voz. "
            "Não tenho como escutar áudios — se você puder me escrever em texto "
            "o que está acontecendo, eu te ajudo na hora."
        )
    if media_type == "imagem":
        return (
            "Oi! Recebi sua imagem, mas não tenho como analisar fotos por aqui. "
            "Se for algo importante (foto de exame, receita, sintoma visível), "
            "me conta em texto o que é, ou aguarda que vou repassar à Dra. Leiza."
        )
    if media_type == "documento":
        return (
            "Oi! Recebi seu documento, mas não tenho como ler arquivos por aqui. "
            "Se for algo urgente, me descreve em texto que eu te ajudo. "
            "Senão, vou repassar à Dra. Leiza pra ela ver."
        )
    # video, figurinha, localizacao, contato — resposta genérica
    return (
        "Oi! Não tenho como processar esse tipo de mensagem por aqui — só texto. "
        "Se puder me escrever o que precisa, eu te ajudo."
    )


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


async def _handle_unsupported_media(
    phone: str,
    media_type: str,
    whatsapp_message_id: str | None,
) -> None:
    """Para audio/PDF/video/etc. — responde explicando que so processa texto.
    (Imagem nao passa por aqui; tem caminho proprio via vision.)"""
    with Session(engine) as db:
        patient = _get_or_create_patient(db, phone)

        db.add(Message(
            patient_id=patient.id,
            direction=MessageDirection.INBOUND,
            text=f"[{media_type} recebido — bot nao processa]",
            whatsapp_message_id=whatsapp_message_id,
            source=MessageSource.PATIENT,
        ))
        db.commit()

        if patient.manual_handover_at is not None:
            log.info("midia (%s) em takeover — bot silente", media_type)
            return

        rejection = _media_rejection_text(media_type)
        provider = EvolutionProvider()
        try:
            msg_id = await provider.send_text(phone, rejection)
            db.add(Message(
                patient_id=patient.id,
                direction=MessageDirection.OUTBOUND,
                text=rejection,
                whatsapp_message_id=msg_id or None,
                source=MessageSource.BOT,
            ))
            db.commit()
        finally:
            await provider.aclose()


async def _handle_image_message(
    phone: str,
    full_data: dict,
    caption: str,
    whatsapp_message_id: str | None,
) -> None:
    """Quando paciente manda imagem: baixa do Evolution, descreve via Haiku Vision,
    e passa a descricao + legenda como texto pro agente principal Opus 4.7.

    Respeita takeover (Dra. ve a foto direto no WhatsApp dela)."""
    with Session(engine) as db:
        patient = _get_or_create_patient(db, phone)

        marker_text = f"[imagem recebida{(' — legenda: ' + caption) if caption else ''}]"
        db.add(Message(
            patient_id=patient.id,
            direction=MessageDirection.INBOUND,
            text=marker_text,
            whatsapp_message_id=whatsapp_message_id,
            source=MessageSource.PATIENT,
        ))
        db.commit()

        if patient.manual_handover_at is not None:
            log.info("imagem em takeover (%s) — bot silente, Dra. ve direto", phone)
            return

        provider = EvolutionProvider()
        try:
            media = await provider.download_media_base64(full_data)
            if not media:
                log.warning("falha ao baixar imagem de %s — fallback rejeicao", phone)
                rejection = _media_rejection_text("imagem")
                msg_id = await provider.send_text(phone, rejection)
                db.add(Message(
                    patient_id=patient.id,
                    direction=MessageDirection.OUTBOUND,
                    text=rejection,
                    whatsapp_message_id=msg_id or None,
                    source=MessageSource.BOT,
                ))
                db.commit()
                return

            base64_data, mimetype = media
            description = await vision.describe_image(base64_data, mimetype)
            if not description:
                description = "(nao foi possivel descrever a imagem)"

            # Sintese: o que a paciente "disse" do ponto de vista do agente
            parts = ["[Paciente acabou de enviar uma imagem.]"]
            if caption:
                parts.append(f"Legenda da paciente: \"{caption}\"")
            parts.append(f"Descricao factual da imagem (gerada por IA, nao interprete clinicamente sem cautela): {description}")
            synthetic_text = "\n".join(parts)

            agent = get_agent()
            await agent.handle_inbound(
                patient=patient,
                inbound_text=synthetic_text,
                provider=provider,
                db=db,
            )
        finally:
            await provider.aclose()


def _extract_caption(message: dict[str, Any]) -> str:
    """Extrai legenda de imagem/video/documento, se houver."""
    if not message:
        return ""
    for key in ("imageMessage", "videoMessage", "documentMessage"):
        m = message.get(key)
        if m and isinstance(m, dict):
            cap = m.get("caption")
            if cap:
                return cap.strip()
    return ""


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
        message_obj = data.get("message") or {}
        media_type = _detect_media_type(message_obj)

        # Imagem tem caminho próprio: passa pelo Claude Vision (Haiku) antes do agente
        if media_type == "imagem":
            caption = _extract_caption(message_obj)
            log.info("paciente %s mandou imagem (caption=%r) — vision pipeline", phone, caption[:60])
            background.add_task(_handle_image_message, phone, data, caption, msg_id)
            return {"received": True, "phone": phone, "media_type": "imagem"}

        # Outros tipos (audio, video, PDF, etc.) — rejeição educada por enquanto
        if media_type:
            log.info("paciente %s mandou %s — bot vai responder pedindo texto", phone, media_type)
            background.add_task(_handle_unsupported_media, phone, media_type, msg_id)
            return {"received": True, "phone": phone, "media_type": media_type}

        log.info("received unknown non-text message from %s — ignoring", phone)
        return {"ignored": "non-text"}

    log.info("inbound from=%s text=%r", phone, text[:120])
    background.add_task(_process_message, phone, text, msg_id)

    return {"received": True, "phone": phone}
