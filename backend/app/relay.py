"""Relay agent: processa mensagens vindas do DOCTOR_PHONE_NUMBER.

Quando a doutora responde a uma escalada via WhatsApp dela, ou dá uma
instrução qualquer, esse módulo interpreta o que ela quer e age:

- Geralmente: encaminha a mensagem dela (reformulada) pra paciente certa
- Se ambíguo: pergunta clarificação à doutora
- Se for ack ou pergunta operacional: responde direto à doutora

Isolado do agente principal de triagem porque tem outro propósito,
outro contexto, outras tools.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import frontmatter
from sqlmodel import Session, select

from .config import settings
from .models import (
    Escalation,
    Message,
    MessageDirection,
    Patient,
    ScheduledMessage,
    utcnow,
)
from .providers.base import WhatsAppProvider

_BRT = ZoneInfo("America/Sao_Paulo")

log = logging.getLogger("obstetra.relay")

MAX_RELAY_ITERATIONS = 6
RECENT_ESCALATION_HOURS = 24


RELAY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "encaminhar_para_paciente",
        "description": (
            "Envia uma mensagem em nome da Dra. Leiza para uma paciente específica. "
            "Use quando a doutora pedir pra você relayar uma resposta dela à paciente. "
            "REFORMULE a mensagem da doutora pra primeira pessoa, num tom direto e cordial pra paciente, "
            "começando com algo como 'A Dra. Leiza pediu pra te avisar que…' ou 'A Dra. Leiza me pediu pra te dizer que…'. "
            "NUNCA encaminhe literal — sempre reformule."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "telefone": {
                    "type": "string",
                    "description": "Telefone da paciente em formato E.164 sem +, ex: 5528988030050. Tem que bater com um telefone na lista de escaladas recentes.",
                },
                "mensagem": {
                    "type": "string",
                    "description": "Mensagem reformulada para a paciente, em primeira pessoa cordial e direta.",
                },
            },
            "required": ["telefone", "mensagem"],
        },
    },
    {
        "name": "responder_doutora",
        "description": (
            "Envia uma mensagem para a Dra. Leiza no WhatsApp dela. Use pra: "
            "(1) confirmar que uma ação foi feita ('Encaminhei à Patricia'); "
            "(2) pedir clarificação quando a mensagem dela for ambígua "
            "('Doutora, qual paciente — Patricia ou Maria?'); "
            "(3) responder uma pergunta operacional dela; "
            "(4) ack curto se ela só agradeceu ou disse algo sem ação requerida."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {
                    "type": "string",
                    "description": "Texto da resposta para a Dra. Leiza.",
                },
            },
            "required": ["texto"],
        },
    },
    {
        "name": "agendar_lembrete",
        "description": (
            "Agenda um lembrete que será enviado AUTOMATICAMENTE à paciente no momento futuro especificado. "
            "Use quando a doutora pedir 'lembre a [paciente] de [algo] em [horário]', 'avisa [paciente] amanhã às X', etc. "
            "Você compõe a mensagem reformulada em primeira pessoa cordial pra paciente, começando com algo como "
            "'Oi [Nome]! A Dra. Leiza pediu pra te lembrar de…' ou 'A Dra. Leiza me pediu pra te avisar que…'. "
            "NUNCA encaminhe literal a fala da doutora — sempre reformule pra paciente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "telefone": {
                    "type": "string",
                    "description": "Telefone da paciente em formato E.164 sem +, ex: 5528988030050. Tem que bater com a lista de pacientes no contexto.",
                },
                "momento_iso": {
                    "type": "string",
                    "description": (
                        "Momento do envio em ISO 8601 com timezone, ex: '2026-04-26T11:00:00-03:00' "
                        "(Brasília, UTC-3). A hora atual está no contexto — use ela pra calcular 'hoje', 'amanhã', etc. "
                        "Se a doutora não especificar horário, use o que parecer razoável (ex: 'amanhã' = 9h da manhã)."
                    ),
                },
                "mensagem": {
                    "type": "string",
                    "description": "Texto do lembrete a ser enviado à paciente, reformulado em primeira pessoa cordial.",
                },
            },
            "required": ["telefone", "momento_iso", "mensagem"],
        },
    },
]


_DEFAULT_RELAY_PROMPT = """\
Você é assistente da {doctor_name}. Neste contexto, você está recebendo mensagens DA PRÓPRIA {doctor_name}, não de pacientes.

A {doctor_name} costuma usar este canal para 4 coisas:

1. **Encaminhar uma resposta dela à paciente** que você escalou recentemente. Ex:
   - "Responda à Patricia que entrarei em contato segunda."
   - "Diz pra ela que pode tomar dipirona."
   - "Mando ela vir aqui amanhã às 9h."
   Quando isso acontecer:
   - Identifique a paciente correta na lista do contexto.
   - **REFORMULE** a mensagem dela em primeira pessoa cordial pra paciente, começando com "A {doctor_name} pediu pra te avisar…" ou similar. NUNCA encaminhe literal.
   - Use `encaminhar_para_paciente` + depois `responder_doutora` confirmando.

2. **Agendar um lembrete pra paciente em momento futuro.** Ex:
   - "Lembre a Patricia de ir ao consultório hoje às 11h."
   - "Avisa a Maria amanhã às 9h pra trazer os exames."
   - "Lembrete pra Leiza: tomar dose dTpa daqui 3 dias."
   Quando isso acontecer:
   - Identifique a paciente.
   - Calcule o `momento_iso` no fuso de São Paulo (UTC-3) usando a hora atual que está no contexto. "Hoje", "amanhã", "daqui 3 dias" — converta corretamente.
   - Componha a mensagem reformulada pra paciente (primeira pessoa cordial), tipo: "Oi Patricia! A Dra. Leiza pediu pra te lembrar de [coisa] hoje às 11h. Te aguardo!"
   - Use `agendar_lembrete(telefone, momento_iso, mensagem)`.
   - Em seguida, use `responder_doutora` confirmando: "Agendado lembrete pra Patricia hoje às 11h."

3. **Pergunta ou comando ambíguo** (qual paciente? hora não especificada? mensagem confusa?) — `responder_doutora` pra pedir clarificação curta.

4. **Mensagem operacional** (ack, pergunta sobre o sistema, "ok obrigado") — `responder_doutora` curto. Se for só "ok" sem exigir ação, pode chamar com "Combinado, doutora." ou nada.

**Princípios:**
- A {doctor_name} é superior. Tom cordial-profissional, conciso. Pode chamar "doutora".
- Lista de escaladas recentes + outras pacientes ativas + hora atual — tudo no contexto. Use como verdade.
- Se SOMENTE UMA escalada recente e doutora não especifica paciente, assuma essa.
- Se ambíguo entre várias, pergunte.
- Reformulação pra paciente: tom acolhedor, primeira pessoa, sem jargão clínico.
- SEMPRE confirme à doutora que a ação foi tomada.
- Se o momento ficar no passado ou for inválido, pergunte clarificação em vez de agendar.
"""


def _load_relay_system_prompt() -> str:
    return _DEFAULT_RELAY_PROMPT.format(doctor_name=settings.doctor_name)


def _recent_escalations_block(db: Session) -> str:
    cutoff = utcnow() - timedelta(hours=RECENT_ESCALATION_HOURS)
    rows = db.exec(
        select(Escalation, Patient)
        .join(Patient, Escalation.patient_id == Patient.id)
        .where(Escalation.created_at >= cutoff)
        .order_by(Escalation.created_at.desc())
        .limit(10)
    ).all()

    if not rows:
        return "<escaladas_recentes>Nenhuma escalada nas últimas 24h.</escaladas_recentes>"

    lines = ["<escaladas_recentes_24h>"]
    for esc, pat in rows:
        when_iso = esc.created_at.isoformat(timespec="minutes")
        nome = pat.name or "(sem nome no DB — pode estar no vault)"
        lines.append(
            f"  - paciente: {nome} | telefone: {pat.phone} | escalada_em: {when_iso} | motivo: {esc.reason} | resumo: {esc.summary}"
        )
    lines.append("</escaladas_recentes_24h>")
    return "\n".join(lines)


def _active_patients_block() -> str:
    """Lê o vault e lista pacientes ativas (nome + telefone) pra agente identificar
    quando a doutora cita uma paciente que não está em escalada recente."""
    vault_path = Path(settings.vault_local_path) / "pacientes"
    if not vault_path.exists():
        return "<pacientes_ativas>vault indisponível</pacientes_ativas>"

    lines = ["<pacientes_ativas>"]
    count = 0
    for d in sorted(vault_path.iterdir()):
        if not d.is_dir():
            continue
        anamnese = d / "anamnese.md"
        if not anamnese.exists():
            continue
        try:
            post = frontmatter.load(anamnese)
            fm = post.metadata or {}
        except Exception:
            continue
        if fm.get("status") and fm.get("status") != "ativa":
            continue
        nome = fm.get("nome") or d.name
        lines.append(f"  - {nome} | telefone: {d.name}")
        count += 1
        if count >= 60:
            break
    lines.append("</pacientes_ativas>")
    return "\n".join(lines)


def _now_brt_block() -> str:
    now_brt = datetime.now(_BRT)
    weekday_pt = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][now_brt.weekday()]
    return (
        f"<hora_atual_brasil>\n"
        f"  iso: {now_brt.isoformat(timespec='minutes')}\n"
        f"  legivel: {weekday_pt}, {now_brt.strftime('%d/%m/%Y às %H:%M')} (Brasília)\n"
        f"</hora_atual_brasil>"
    )


class RelayAgent:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_relay_model

    async def handle_doctor_message(
        self,
        *,
        text: str,
        provider: WhatsAppProvider,
        db: Session,
    ) -> None:
        system_prompt = _load_relay_system_prompt()
        context_parts = [
            _now_brt_block(),
            _recent_escalations_block(db),
            _active_patients_block(),
        ]
        user_content = (
            "\n\n".join(context_parts)
            + f"\n\n<mensagem_da_doutora>\n{text}\n</mensagem_da_doutora>"
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

        for iteration in range(MAX_RELAY_ITERATIONS):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=RELAY_TOOLS,
                messages=messages,
            )

            log.info(
                "relay turn iter=%d stop=%s in=%d cache_r=%d cache_w=%d out=%d",
                iteration,
                response.stop_reason,
                response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                response.usage.output_tokens,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                # Fallback: agent gerou texto sem tool — manda pra doutora
                text_blocks = [
                    b.text for b in response.content
                    if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip()
                ]
                if text_blocks and settings.doctor_phone_number:
                    log.warning("relay fallback — texto sem tool call, enviando à doutora")
                    for txt in text_blocks:
                        try:
                            await provider.send_text(settings.doctor_phone_number, txt.strip())
                        except Exception:
                            log.exception("relay fallback send failed")
                else:
                    log.warning("relay terminou sem ação nenhuma — nada enviado")
                break

            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict[str, Any]] = []
            for tool_use in tool_uses:
                try:
                    result = await self._execute_tool(
                        tool_use.name,
                        tool_use.input,
                        provider=provider,
                        db=db,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })
                except Exception as exc:
                    log.exception("relay tool %s failed", tool_use.name)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": f"Erro: {exc}",
                        "is_error": True,
                    })

            messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn":
                break
        else:
            log.warning("relay hit MAX_RELAY_ITERATIONS=%d", MAX_RELAY_ITERATIONS)

    async def _execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        provider: WhatsAppProvider,
        db: Session,
    ) -> str:
        if name == "encaminhar_para_paciente":
            telefone = str(arguments.get("telefone", "")).strip()
            mensagem = str(arguments.get("mensagem", "")).strip()
            if not telefone or not mensagem:
                return "Erro: telefone ou mensagem ausente."

            patient = db.exec(select(Patient).where(Patient.phone == telefone)).first()
            if not patient:
                return (
                    f"Erro: paciente com telefone {telefone} não está no DB. "
                    f"Verifique se o telefone bate com algum da lista de escaladas recentes."
                )

            msg_id = await provider.send_text(telefone, mensagem)
            db.add(Message(
                patient_id=patient.id,
                direction=MessageDirection.OUTBOUND,
                text=mensagem,
                whatsapp_message_id=msg_id or None,
            ))
            db.commit()
            return f"Mensagem encaminhada à {patient.name or telefone} (telefone {telefone})."

        if name == "responder_doutora":
            texto = str(arguments.get("texto", "")).strip()
            if not texto:
                return "Erro: texto vazio."
            if not settings.doctor_phone_number:
                return "Erro: DOCTOR_PHONE_NUMBER não configurado."
            await provider.send_text(settings.doctor_phone_number, texto)
            return "Resposta enviada à doutora."

        if name == "agendar_lembrete":
            telefone = str(arguments.get("telefone", "")).strip()
            momento_iso = str(arguments.get("momento_iso", "")).strip()
            mensagem = str(arguments.get("mensagem", "")).strip()
            if not (telefone and momento_iso and mensagem):
                return "Erro: telefone, momento_iso e mensagem são todos obrigatórios."

            # Parse momento ISO; aceita forma com timezone ou assume BRT
            try:
                momento = datetime.fromisoformat(momento_iso)
            except ValueError:
                return f"Erro: momento_iso inválido ('{momento_iso}'). Use ISO 8601 com timezone, ex: 2026-04-26T11:00:00-03:00"
            if momento.tzinfo is None:
                momento = momento.replace(tzinfo=_BRT)
            momento_utc = momento.astimezone(timezone.utc).replace(tzinfo=None)

            # Valida que está no futuro
            now_utc = utcnow()
            if momento_utc <= now_utc:
                return f"Erro: o momento {momento.isoformat()} já passou (hora atual {datetime.now(_BRT).isoformat(timespec='minutes')}). Confirme o horário com a doutora."

            # Acha paciente
            patient = db.exec(select(Patient).where(Patient.phone == telefone)).first()
            if not patient:
                return f"Erro: paciente com telefone {telefone} não está cadastrada. Verifique se o telefone bate com a lista de pacientes ativas."

            # Cria agendamento
            sched = ScheduledMessage(
                patient_id=patient.id,
                text=mensagem,
                scheduled_at=momento_utc,
                created_by="doctor_relay",
            )
            db.add(sched)
            db.commit()
            db.refresh(sched)

            human_when = momento.astimezone(_BRT).strftime("%d/%m/%Y às %H:%M")
            log.info("agendado lembrete id=%d para %s em %s", sched.id, telefone, human_when)
            return f"Lembrete agendado pra {patient.name or telefone} em {human_when} (id={sched.id})."

        return f"Ferramenta desconhecida: {name}"


_relay_instance: RelayAgent | None = None


def get_relay_agent() -> RelayAgent:
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = RelayAgent()
    return _relay_instance
