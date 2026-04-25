import logging

from .config import settings
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
