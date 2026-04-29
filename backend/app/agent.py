import logging
from pathlib import Path
from typing import Any

import anthropic
from sqlmodel import Session, select

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from . import vault
from .config import settings
from .escalation import notify_doctor, notify_secretary
from .models import (
    Appointment,
    AppointmentStatus,
    Escalation,
    Message,
    MessageDirection,
    OnboardingState,
    Patient,
    utcnow,
)
from .protocol.red_flags import RED_FLAGS_GESTANTES
from .providers.base import WhatsAppProvider

log = logging.getLogger("obstetra.agent")

MAX_HISTORY_MESSAGES = 40
MAX_TOOL_ITERATIONS = 8

_PROTOCOL_PATH = Path(__file__).parent / "protocol" / "system_prompt.md"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "responder_paciente",
        "description": (
            "Envia uma mensagem de WhatsApp para a paciente. Use SEMPRE que quiser "
            "falar com ela — inclusive durante a triagem, para fazer uma pergunta "
            "de múltipla escolha. Uma mensagem por chamada."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {
                    "type": "string",
                    "description": "Texto exato que será enviado à paciente via WhatsApp.",
                }
            },
            "required": ["texto"],
        },
    },
    {
        "name": "escalar_para_doutora",
        "description": (
            "Notifica a Dra. Leiza no WhatsApp dela. Use com parcimônia, apenas "
            "quando realmente necessário. Sempre responda a paciente ANTES de "
            "escalar (com responder_paciente), para que ela saiba o que fazer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "enum": ["red_flag", "duvida_eletiva", "fora_do_escopo", "incerteza"],
                    "description": "Categoria da escalada.",
                },
                "resumo": {
                    "type": "string",
                    "description": (
                        "Resumo do caso em 1-3 frases, em português, pra doutora "
                        "entender o contexto sem ler a conversa inteira."
                    ),
                },
            },
            "required": ["motivo", "resumo"],
        },
    },
    {
        "name": "confirmar_consulta",
        "description": (
            "Use APENAS quando a paciente está respondendo afirmativamente a um lembrete "
            "de consulta (ex: 'sim', 'vou estar lá', 'tô confirmando', 'pode contar comigo'). "
            "Marca a consulta como confirmada. SEMPRE chame `responder_paciente` ANTES "
            "(ou na mesma chamada lógica) pra dar feedback positivo. "
            "O id da consulta vem do contexto `<consulta_proxima>` que aparece quando "
            "a paciente tem uma consulta nas próximas 48h."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "ID da consulta a confirmar (vem de <consulta_proxima> no contexto).",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "marcar_desistencia_consulta",
        "description": (
            "Use APENAS quando a paciente está respondendo NEGATIVAMENTE a um lembrete de "
            "consulta (ex: 'não vou conseguir', 'preciso desmarcar', 'não vai dar'). "
            "Marca consulta como cancelada e AVISA A SECRETÁRIA pra ela poder dar o slot "
            "pra outra paciente que pediu encaixe. SEMPRE chame `responder_paciente` ANTES "
            "(ou logo após) com algo como 'Sem problema, anotei. Quando puder remarcar, é "
            "só me chamar.' — A INICIATIVA DE REMARCAR É DA PACIENTE, não nossa."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "ID da consulta a cancelar (vem de <consulta_proxima> no contexto).",
                },
                "motivo": {
                    "type": "string",
                    "description": "Motivo curto que a paciente deu (ex: 'imprevisto no trabalho', 'não pode mais').",
                },
            },
            "required": ["id"],
        },
    },
]


def _load_system_prompt() -> str:
    template = _PROTOCOL_PATH.read_text(encoding="utf-8")
    red_flags = "\n".join(f"- {flag}" for flag in RED_FLAGS_GESTANTES)
    return template.format(doctor_name=settings.doctor_name, red_flags=red_flags)


def _consulta_proxima_block(db: Session, patient_id: int) -> str:
    """Se a paciente tem consulta nas proximas 48h (agendada/confirmada/remarcada),
    inclui no contexto. Crucial pra agent saber se mensagem dela e' resposta a um lembrete."""
    now_utc = utcnow()
    cutoff = now_utc + timedelta(hours=48)
    rows = db.exec(
        select(Appointment)
        .where(Appointment.patient_id == patient_id)
        .where(Appointment.scheduled_at >= now_utc)
        .where(Appointment.scheduled_at <= cutoff)
        .where(Appointment.status.in_([AppointmentStatus.AGENDADA, AppointmentStatus.CONFIRMADA, AppointmentStatus.REMARCADA]))
        .order_by(Appointment.scheduled_at)
        .limit(3)
    ).all()

    if not rows:
        return "<consulta_proxima>nenhuma consulta nas proximas 48h</consulta_proxima>"

    brt = ZoneInfo("America/Sao_Paulo")
    lines = ["<consulta_proxima>"]
    for ap in rows:
        when_brt = ap.scheduled_at.replace(tzinfo=timezone.utc).astimezone(brt)
        when_str = when_brt.strftime("%d/%m %H:%M")
        tipo = ap.tipo.value if hasattr(ap.tipo, "value") else str(ap.tipo)
        status = ap.status.value if hasattr(ap.status, "value") else str(ap.status)
        lines.append(
            f"  - id={ap.id} | quando: {when_str} BRT | tipo: {tipo} | status: {status}"
        )
    lines.append("</consulta_proxima>")
    lines.append(
        "<!-- Se a mensagem da paciente parecer resposta a um lembrete dessa consulta "
        "(confirmando/desistindo), use as tools confirmar_consulta(id) ou "
        "marcar_desistencia_consulta(id). -->"
    )
    return "\n".join(lines)


def _patient_context_block(patient: Patient) -> str:
    lines = [
        "<estado_da_paciente>",
        f"telefone: +{patient.phone}",
        f"nome: {patient.name or 'AINDA_NAO_SEI'}",
        f"semanas_gestacao: {patient.gestational_weeks if patient.gestational_weeks is not None else 'AINDA_NAO_SEI'}",
        f"confirmou_ser_paciente_da_doutora: {patient.is_doctor_patient if patient.is_doctor_patient is not None else 'AINDA_NAO_SEI'}",
        f"onboarding: {patient.onboarding_state.value}",
        "</estado_da_paciente>",
    ]
    return "\n".join(lines)


def _build_messages(
    patient: Patient,
    history: list[Message],
    inbound_text: str,
    vault_ctx: vault.PatientContext,
    consulta_proxima_block: str,
) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for m in history:
        role = "user" if m.direction == MessageDirection.INBOUND else "assistant"
        msgs.append({"role": role, "content": m.text})

    last_user_content = (
        f"{vault_ctx.to_prompt_block()}\n\n"
        f"{_patient_context_block(patient)}\n\n"
        f"{consulta_proxima_block}\n\n"
        f"{inbound_text}"
    )
    msgs.append({"role": "user", "content": last_user_content})
    return msgs


class ClinicalAgent:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._system_prompt = _load_system_prompt()
        self._model = settings.anthropic_model

    async def handle_inbound(
        self,
        *,
        patient: Patient,
        inbound_text: str,
        provider: WhatsAppProvider,
        db: Session,
    ) -> None:
        history = db.exec(
            select(Message)
            .where(Message.patient_id == patient.id)
            .order_by(Message.created_at.desc())
            .limit(MAX_HISTORY_MESSAGES)
        ).all()
        history = list(reversed(history))

        try:
            vault_ctx = await vault.read_patient(patient.phone)
        except Exception:
            log.exception("vault.read_patient falhou — seguindo sem contexto")
            vault_ctx = vault.PatientContext(found=False)

        consulta_proxima_block = _consulta_proxima_block(db, patient.id or 0)
        messages = _build_messages(patient, history, inbound_text, vault_ctx, consulta_proxima_block)

        # Track o que aconteceu neste turno pra logar no vault depois
        outbound_msgs_this_turn: list[str] = []
        escalation_info: dict[str, str] | None = None

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=8000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": self._system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOLS,
                messages=messages,
            )

            log.info(
                "agent turn patient=%s iter=%d stop=%s in=%d cache_r=%d cache_w=%d out=%d",
                patient.phone,
                iteration,
                response.stop_reason,
                response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                response.usage.output_tokens,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                # Fallback: o modelo às vezes termina o turno sem chamar a tool,
                # mas ainda gera texto. Envia esse texto como responder_paciente
                # pra paciente não ficar no silêncio.
                text_blocks = [
                    b.text for b in response.content
                    if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip()
                ]
                if text_blocks:
                    log.warning(
                        "agent fallback — gerou texto sem tool call (patient=%s, %d blocos)",
                        patient.phone, len(text_blocks),
                    )
                    for text in text_blocks:
                        text = text.strip()
                        try:
                            msg_id = await provider.send_text(patient.phone, text)
                            outbound_msgs_this_turn.append(text)
                            db.add(Message(
                                patient_id=patient.id,
                                direction=MessageDirection.OUTBOUND,
                                text=text,
                                whatsapp_message_id=msg_id or None,
                            ))
                            db.commit()
                        except Exception:
                            log.exception("fallback send_text falhou")
                else:
                    log.warning(
                        "agent ended turn sem tool call e sem texto (patient=%s stop=%s)",
                        patient.phone, response.stop_reason,
                    )
                break

            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                # Captura intent pra logar no vault depois
                if tool_use.name == "responder_paciente":
                    texto = str(tool_use.input.get("texto", "")).strip()
                    if texto:
                        outbound_msgs_this_turn.append(texto)
                elif tool_use.name == "escalar_para_doutora":
                    escalation_info = {
                        "motivo": str(tool_use.input.get("motivo", "")),
                        "resumo": str(tool_use.input.get("resumo", "")),
                    }
                try:
                    result_text = await self._execute_tool(
                        tool_use.name,
                        tool_use.input,
                        patient=patient,
                        provider=provider,
                        db=db,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result_text,
                        }
                    )
                except Exception as exc:
                    log.exception("tool %s failed", tool_use.name)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": f"Erro interno ao executar: {exc}",
                            "is_error": True,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn":
                break
        else:
            log.warning(
                "agent hit MAX_TOOL_ITERATIONS=%d for patient=%s",
                MAX_TOOL_ITERATIONS,
                patient.phone,
            )

        # Se o agente chamou escalar_para_doutora mas não falou nada pra paciente,
        # manda um ack de cortesia pra ela não ficar no silêncio
        if escalation_info and not outbound_msgs_this_turn:
            fallback_text = (
                "Vou registrar essa dúvida pra Dra. Leiza te responder. "
                "Qualquer coisa nova ou se algo piorar, me avisa na hora, tá?"
            )
            log.warning(
                "agent escalou sem responder à paciente — enviando ack default (patient=%s)",
                patient.phone,
            )
            try:
                msg_id = await provider.send_text(patient.phone, fallback_text)
                outbound_msgs_this_turn.append(fallback_text)
                db.add(Message(
                    patient_id=patient.id,
                    direction=MessageDirection.OUTBOUND,
                    text=fallback_text,
                    whatsapp_message_id=msg_id or None,
                ))
                db.commit()
            except Exception:
                log.exception("escalation-only fallback send failed")

        patient.updated_at = utcnow()
        db.add(patient)
        db.commit()

        # Loga o turno no vault (best-effort — não falha o fluxo se quebrar)
        try:
            escalation_summary = None
            if escalation_info:
                escalation_summary = f"escalada como `{escalation_info['motivo']}` — {escalation_info['resumo']}"
            await vault.append_conversation(
                patient.phone,
                inbound_messages=[inbound_text],
                outbound_messages=outbound_msgs_this_turn,
                escalation_summary=escalation_summary,
            )
        except Exception:
            log.exception("vault.append_conversation falhou para patient=%s", patient.phone)

    async def _execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        patient: Patient,
        provider: WhatsAppProvider,
        db: Session,
    ) -> str:
        if name == "responder_paciente":
            texto = str(arguments.get("texto", "")).strip()
            if not texto:
                return "Erro: texto vazio."
            msg_id = await provider.send_text(patient.phone, texto)
            db.add(
                Message(
                    patient_id=patient.id,
                    direction=MessageDirection.OUTBOUND,
                    text=texto,
                    whatsapp_message_id=msg_id or None,
                )
            )
            db.commit()
            return "Mensagem enviada à paciente."

        if name == "escalar_para_doutora":
            motivo = str(arguments.get("motivo", "incerteza"))
            resumo = str(arguments.get("resumo", "")).strip()
            sent = await notify_doctor(
                provider,
                patient_name=patient.name,
                patient_phone=patient.phone,
                motivo=motivo,
                resumo=resumo,
            )
            db.add(
                Escalation(
                    patient_id=patient.id,
                    reason=motivo,
                    summary=resumo,
                    notified_doctor=sent,
                )
            )
            db.commit()
            if sent:
                return "Doutora notificada com sucesso."
            return "Doutora NÃO foi notificada (DOCTOR_PHONE_NUMBER não configurado ou falha no envio). Continue a atender a paciente normalmente e informe que a doutora será avisada assim que possível."

        if name == "confirmar_consulta":
            ap_id = arguments.get("id")
            if ap_id is None:
                return "Erro: id é obrigatório."
            try:
                ap_id = int(ap_id)
            except (ValueError, TypeError):
                return f"Erro: id inválido ({ap_id})."

            ap = db.exec(
                select(Appointment)
                .where(Appointment.id == ap_id)
                .where(Appointment.patient_id == patient.id)
            ).first()
            if not ap:
                return f"Erro: consulta id={ap_id} não pertence a essa paciente."
            if ap.status == AppointmentStatus.CANCELADA:
                return f"Consulta id={ap_id} já estava cancelada — não dá pra confirmar."

            ap.status = AppointmentStatus.CONFIRMADA
            ap.updated_at = utcnow()
            db.add(ap)
            db.commit()
            log.info("paciente confirmou consulta id=%d (patient=%s)", ap_id, patient.phone)
            return f"Consulta id={ap_id} marcada como CONFIRMADA."

        if name == "marcar_desistencia_consulta":
            ap_id = arguments.get("id")
            motivo = str(arguments.get("motivo", "")).strip()
            if ap_id is None:
                return "Erro: id é obrigatório."
            try:
                ap_id = int(ap_id)
            except (ValueError, TypeError):
                return f"Erro: id inválido ({ap_id})."

            ap = db.exec(
                select(Appointment)
                .where(Appointment.id == ap_id)
                .where(Appointment.patient_id == patient.id)
            ).first()
            if not ap:
                return f"Erro: consulta id={ap_id} não pertence a essa paciente."

            ap.status = AppointmentStatus.CANCELADA
            ap.cancelled_at = utcnow()
            ap.updated_at = utcnow()
            db.add(ap)
            db.commit()
            db.refresh(ap)

            # Cancela o lembrete linkado tambem (se houver)
            from .reminders import sync_appointment_reminder
            sync_appointment_reminder(db, ap)

            # Notifica SECRETARIA (com fallback pra doutora se nao houver SECRETARY_PHONE)
            from datetime import timezone as _tz
            from zoneinfo import ZoneInfo as _ZI
            when_brt = ap.scheduled_at.replace(tzinfo=_tz.utc).astimezone(_ZI("America/Sao_Paulo"))
            when_str = when_brt.strftime("%d/%m/%Y às %H:%M")
            tipo = ap.tipo.value if hasattr(ap.tipo, "value") else str(ap.tipo)
            display_name = patient.name or patient.phone
            motivo_part = f"\nMotivo informado: \"{motivo}\"" if motivo else ""
            text = (
                f"*[Obstetra — desistência]*\n"
                f"A paciente *{display_name}* (+{patient.phone}) avisou que NÃO vai conseguir vir "
                f"na consulta marcada pra *{when_str}* ({tipo}).{motivo_part}\n\n"
                f"O slot está liberado pra encaixe. A iniciativa de remarcar é da paciente — ela "
                f"voltará a falar quando puder. Se quiser, entre em contato com ela."
            )
            await notify_secretary(provider, text=text)
            log.info("paciente desistiu consulta id=%d (patient=%s) — secretaria notificada", ap_id, patient.phone)
            return f"Consulta id={ap_id} cancelada por desistência da paciente. Secretária notificada."

        return f"Ferramenta desconhecida: {name}"


_agent_instance: ClinicalAgent | None = None


def get_agent() -> ClinicalAgent:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = ClinicalAgent()
    return _agent_instance
