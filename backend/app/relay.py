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
from datetime import timedelta
from pathlib import Path
from typing import Any

import anthropic
from sqlmodel import Session, select

from .config import settings
from .models import Escalation, Message, MessageDirection, Patient, utcnow
from .providers.base import WhatsAppProvider

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
]


_DEFAULT_RELAY_PROMPT = """\
Você é assistente da {doctor_name}. Neste contexto, você está recebendo mensagens DA PRÓPRIA {doctor_name}, não de pacientes.

A {doctor_name} costuma usar este canal para:

1. **Encaminhar uma resposta dela à paciente** que você escalou recentemente. Ex:
   - "Responda à Patricia que entrarei em contato segunda."
   - "Diz pra ela que pode tomar dipirona."
   - "Mando ela vir aqui amanhã às 9h."
   Quando isso acontecer:
   - Identifique a paciente correta. Geralmente é a mais recente da lista de escaladas. Se a doutora citar nome (Patricia, Maria, etc.), bate com a lista.
   - **REFORMULE** a mensagem dela em primeira pessoa cordial e direta para a paciente. Comece com algo como "A {doctor_name} pediu pra te avisar que…" ou "A {doctor_name} me pediu pra te dizer que…". NUNCA encaminhe literal — a paciente não é a interlocutora original.
   - Use a tool `encaminhar_para_paciente` com o telefone certo + a mensagem reformulada.
   - Em seguida, use `responder_doutora` confirmando ("Encaminhei à Patricia").

2. **Pergunta ou comando ambíguo** (qual paciente? não tem na lista? mensagem confusa?) — use `responder_doutora` pra pedir clarificação curta e objetiva.

3. **Mensagem operacional** (ack, pergunta sobre o sistema, "ok obrigado", etc.) — use `responder_doutora` com uma resposta curta. Se for um simples "ok" e não exige ação nem resposta, pode chamar `responder_doutora` com algo como "Combinado, doutora." ou só não chamar nada.

**Princípios:**
- A {doctor_name} é sua superior. Tom cordial e profissional, sempre conciso. Pode chamar de "doutora".
- A lista de escaladas recentes (últimas 24h) está no contexto, com telefone + nome + resumo de cada caso. Use ela como verdade pra identificar a paciente.
- Se houver SOMENTE UMA escalada recente e a doutora não especificar paciente, assuma que é essa.
- Se houver várias e a mensagem for ambígua, pergunte qual.
- Quando reformular pra paciente: tom acolhedor, primeira pessoa, sem jargão clínico.
- SEMPRE confirme à doutora que a ação foi tomada — ela precisa saber que o pedido foi atendido.
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
        return "<escaladas_recentes>Nenhuma escalada nas últimas 24h. Se a doutora pedir pra encaminhar algo, peça clarificação.</escaladas_recentes>"

    lines = ["<escaladas_recentes_24h>"]
    for esc, pat in rows:
        when_iso = esc.created_at.isoformat(timespec="minutes")
        nome = pat.name or "(sem nome no DB — pode estar no vault)"
        lines.append(
            f"  - paciente: {nome} | telefone: {pat.phone} | escalada_em: {when_iso} | motivo: {esc.reason} | resumo: {esc.summary}"
        )
    lines.append("</escaladas_recentes_24h>")
    return "\n".join(lines)


class RelayAgent:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    async def handle_doctor_message(
        self,
        *,
        text: str,
        provider: WhatsAppProvider,
        db: Session,
    ) -> None:
        system_prompt = _load_relay_system_prompt()
        recent = _recent_escalations_block(db)
        user_content = f"{recent}\n\n<mensagem_da_doutora>\n{text}\n</mensagem_da_doutora>"

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

        return f"Ferramenta desconhecida: {name}"


_relay_instance: RelayAgent | None = None


def get_relay_agent() -> RelayAgent:
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = RelayAgent()
    return _relay_instance
