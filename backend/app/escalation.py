import logging

from sqlmodel import Session, select

from .config import settings
from .models import Escalation, EscalationStatus, Patient, utcnow
from .providers.base import WhatsAppProvider

log = logging.getLogger("obstetra.escalation")


async def notify_doctor(
    provider: WhatsAppProvider,
    *,
    patient_name: str | None,
    patient_phone: str,
    motivo: str,
    resumo: str,
) -> bool:
    if not settings.doctor_phone_number:
        log.warning(
            "escalation fired but DOCTOR_PHONE_NUMBER not set (motivo=%s, patient=%s)",
            motivo,
            patient_phone,
        )
        return False

    display_name = patient_name or patient_phone
    text = (
        f"*[Obstetra - {motivo}]*\n"
        f"Paciente: {display_name}\n"
        f"Telefone: +{patient_phone}\n\n"
        f"{resumo}"
    )
    try:
        await provider.send_text(settings.doctor_phone_number, text)
        return True
    except Exception:
        log.exception("failed to notify doctor for motivo=%s", motivo)
        return False


async def flush_pending_escalation(
    db: Session,
    provider: WhatsAppProvider,
    patient: Patient,
) -> bool:
    """Envia pra doutora a escalada pendente mais recente da paciente (resumo
    mais completo) e marca como enviada. Pendentes anteriores viram superseded.

    Retorna True se algo foi enviado."""
    pendings = db.exec(
        select(Escalation)
        .where(Escalation.patient_id == patient.id)
        .where(Escalation.status == EscalationStatus.PENDING)
        .order_by(Escalation.created_at.desc())
    ).all()
    if not pendings:
        return False

    latest = pendings[0]
    sent = await notify_doctor(
        provider,
        patient_name=patient.name,
        patient_phone=patient.phone,
        motivo=latest.reason,
        resumo=latest.summary,
    )

    latest.status = EscalationStatus.SENT
    latest.notified_doctor = sent
    latest.sent_at = utcnow()
    db.add(latest)
    for older in pendings[1:]:
        older.status = EscalationStatus.SUPERSEDED
        db.add(older)
    db.commit()

    log.info(
        "escalada pendente enviada (patient=%s, motivo=%s, %d antigas superseded, sent=%s)",
        patient.phone, latest.reason, len(pendings) - 1, sent,
    )
    return sent


async def notify_secretary(
    provider: WhatsAppProvider,
    *,
    text: str,
) -> bool:
    """Envia notificacao pra secretaria (telefone separado).
    Fallback pra doutora se SECRETARY_PHONE_NUMBER nao configurado."""
    target = settings.secretary_phone_number or settings.doctor_phone_number
    if not target:
        log.warning("notify_secretary sem destino — nem SECRETARY_PHONE_NUMBER nem DOCTOR_PHONE_NUMBER")
        return False
    try:
        await provider.send_text(target, text)
        return True
    except Exception:
        log.exception("failed to notify secretary")
        return False
