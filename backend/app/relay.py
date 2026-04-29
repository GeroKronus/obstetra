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
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from sqlmodel import Session, select

from .config import settings
from .models import (
    Escalation,
    Message,
    MessageDirection,
    Patient,
    PatientStatus,
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
    {
        "name": "cancelar_lembrete",
        "description": (
            "Cancela um lembrete que ainda NÃO foi enviado. "
            "Use quando a doutora pedir 'cancela o lembrete pra X', 'desmarca aquele agendamento das 12h55', "
            "'esquece aquele último', etc. "
            "Pegue o `id` do lembrete na lista de <lembretes_pendentes> do contexto. "
            "Se a doutora pedir múltiplos, chame essa tool várias vezes (uma por id)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "ID do lembrete a cancelar (vem da lista <lembretes_pendentes> do contexto).",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "feedback_interno",
        "description": (
            "Gera uma análise clínica interna pra Dra. Leiza sobre uma paciente específica e ENVIA "
            "diretamente pra ela no WhatsApp. Use APENAS quando a doutora pedir explicitamente "
            "'feedback interno', 'fb interno', 'feedback clínico', 'me dá um feedback', etc. "
            "A análise é INTERNA, NUNCA vai pra paciente — vai só pro WhatsApp da doutora. "
            "Identifique a paciente: se a doutora citar nome, use esse; senão pegue da última escalada nas "
            "<escaladas_recentes>. Se não houver contexto suficiente (sem escalada recente e sem nome), "
            "use `responder_doutora` pedindo o nome em vez de chamar essa ferramenta. "
            "Após chamar essa tool com sucesso, NÃO precisa chamar `responder_doutora` — a tool já enviou."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "telefone": {
                    "type": "string",
                    "description": "Telefone da paciente em formato E.164 sem +, ex: 5528988030050. Tem que bater com a lista de escaladas recentes ou pacientes ativas.",
                },
            },
            "required": ["telefone"],
        },
    },
]


_DEFAULT_RELAY_PROMPT = """\
Você é assistente da {doctor_name}. Neste contexto, você está recebendo mensagens DA PRÓPRIA {doctor_name}, não de pacientes.

A {doctor_name} costuma usar este canal para 7 coisas:

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

3. **Cancelar um lembrete agendado.** Ex:
   - "Cancela o lembrete pra Patricia das 12h55."
   - "Esquece aquele último que agendei."
   - "Desmarca todos os lembretes da Maria."
   Quando isso acontecer:
   - Olhe a lista `<lembretes_pendentes>` no contexto e identifique pelo `id`.
   - Use `cancelar_lembrete(id)` — múltiplos = chamadas múltiplas.
   - "O último que agendei" = id mais alto. "Aquele das 12h55" = match pelo horário. "Todos da Maria" = todos cujo paciente seja Maria.
   - Em seguida, use `responder_doutora` confirmando o cancelamento.

4. **Consultar agenda / lembretes pendentes.** Ex:
   - "Qual minha agenda hoje?"
   - "O que tenho marcado pra amanhã?"
   - "Lembretes da Patricia pra essa semana"
   - "Tem alguma coisa pra hoje à tarde?"
   Quando isso acontecer:
   - Olhe a lista `<lembretes_pendentes>` no contexto
   - Filtre mentalmente pelo critério da pergunta (data, paciente, período)
   - Use `responder_doutora` com a resposta formatada, tipo:
     ```
     Sua agenda de hoje (28/04):
     • 10h00 — Maria Almeida: confirmar consulta de amanhã
     • 12h55 — Patricia: lembrete reforço (cães ao banho)
     ```
   - Se o filtro retornar vazio, fale isso ("Nada agendado pra amanhã, doutora.")
   - Se a doutora citar nome ambíguo ou data inválida, peça clarificação.

5. **Feedback interno (apoio à decisão clínica).** Ex:
   - "feedback interno" (logo após uma escalada — assume a paciente da escalada)
   - "fb interno Maria" / "feedback clínico da Patricia" (paciente nomeada)
   - "me dá um feedback sobre a Joana"
   Quando isso acontecer:
   - Identifique a paciente: nome citado > última escalada nas <escaladas_recentes>.
   - Se não tiver contexto (sem escalada recente, sem nome), use `responder_doutora` perguntando: "Doutora, qual paciente?"
   - Use `feedback_interno(telefone)` — a tool monta o histórico, roda o raciocínio clínico em modelo separado e ENVIA direto pra doutora.
   - **REGRA RÍGIDA — após `feedback_interno` retornar com sucesso:** PARE. Não chame mais nenhuma ferramenta. Não chame `responder_doutora`. Não envie eco/confirmação. Encerre o turno em silêncio. A doutora já recebeu o feedback completo — uma confirmação adicional só polui o WhatsApp dela.

6. **Pergunta ou comando ambíguo** (qual paciente? hora não especificada? mensagem confusa?) — `responder_doutora` pra pedir clarificação curta.

7. **Mensagem operacional** (ack, pergunta sobre o sistema, "ok obrigado") — `responder_doutora` curto. Se for só "ok" sem exigir ação, pode chamar com "Combinado, doutora." ou nada.

**Princípios:**
- A {doctor_name} é superior. Tom cordial-profissional, conciso. Pode chamar "doutora".
- **REGRA DE OURO — UMA mensagem por turno:** chame `responder_doutora` NO MÁXIMO 1 vez por turno. Se já chamou alguma tool de ação (`encaminhar_para_paciente`, `agendar_lembrete`, `cancelar_lembrete`, `feedback_interno`), a confirmação à doutora deve ser FEITA NA MESMA chamada de `responder_doutora`, não em uma adicional. Não duplique. Após a confirmação, ENCERRE o turno.
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


def _active_patients_block(db: Session) -> str:
    """Lista pacientes ativas (nome + telefone) — lido do DB."""
    rows = db.exec(
        select(Patient)
        .where(Patient.status == PatientStatus.ATIVA)
        .where(Patient.name.is_not(None))
        .order_by(Patient.name)
        .limit(60)
    ).all()

    if not rows:
        return "<pacientes_ativas>nenhuma paciente ativa cadastrada</pacientes_ativas>"

    lines = ["<pacientes_ativas>"]
    for p in rows:
        lines.append(f"  - {p.name} | telefone: {p.phone}")
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


def _pending_scheduled_block(db: Session) -> str:
    """Lista lembretes ainda nao enviados — pra agente referenciar quando doutora
    pedir cancelamento ou modificacao."""
    rows = db.exec(
        select(ScheduledMessage, Patient)
        .join(Patient, ScheduledMessage.patient_id == Patient.id)
        .where(ScheduledMessage.sent_at.is_(None))
        .where(ScheduledMessage.cancelled_at.is_(None))
        .order_by(ScheduledMessage.scheduled_at)
        .limit(20)
    ).all()

    if not rows:
        return "<lembretes_pendentes>nenhum lembrete agendado no momento</lembretes_pendentes>"

    lines = ["<lembretes_pendentes>"]
    for s, pat in rows:
        when_brt = s.scheduled_at.replace(tzinfo=timezone.utc).astimezone(_BRT)
        when_str = when_brt.strftime("%d/%m %H:%M")
        nome = pat.name or pat.phone
        text_short = s.text[:80] + ("…" if len(s.text) > 80 else "")
        lines.append(
            f"  - id={s.id} | paciente: {nome} ({pat.phone}) | quando: {when_str} BRT | texto: \"{text_short}\""
        )
    lines.append("</lembretes_pendentes>")
    return "\n".join(lines)


def _format_patient_anamnese(patient: Patient) -> str:
    """Renderiza a anamnese da Patient row como texto estruturado pro prompt."""
    lines: list[str] = []

    def _add(label: str, val: Any) -> None:
        if val is None or (isinstance(val, str) and not val.strip()):
            return
        if hasattr(val, "value"):  # enum
            val = val.value
        lines.append(f"{label}: {val}")

    _add("nome", patient.name)
    _add("telefone", patient.phone)
    _add("data_nascimento", patient.data_nascimento)
    _add("endereco", patient.endereco)
    _add("dum", patient.dum)
    _add("tipo_gestacao", patient.tipo_gestacao)
    _add("risco", patient.risco)
    if patient.gestacao_planejada is not None:
        _add("gestacao_planejada", patient.gestacao_planejada)
    _add("gestational_weeks (cache)", patient.gestational_weeks)
    _add("gestas", patient.gestas)
    _add("partos_normais", patient.partos_normais)
    _add("cesareas", patient.cesareas)
    _add("abortos", patient.abortos)
    _add("alergias", patient.alergias)
    _add("condicoes_pre_existentes", patient.condicoes_pre_existentes)
    _add("medicacoes_em_uso", patient.medicacoes_em_uso)
    _add("grupo_sanguineo", patient.grupo_sanguineo)
    _add("plano_saude", patient.plano_saude)
    _add("hospital_referencia", patient.hospital_referencia)
    _add("medico_obstetra", patient.medico_obstetra)
    _add("contato_emergencia_nome", patient.contato_emergencia_nome)
    _add("contato_emergencia_telefone", patient.contato_emergencia_telefone)
    _add("contato_emergencia_relacao", patient.contato_emergencia_relacao)
    _add("preferencias_atendimento", patient.preferencias_atendimento)

    sections: list[str] = []
    if patient.historico_clinico:
        sections.append(f"## Histórico clínico relevante\n\n{patient.historico_clinico.strip()}")
    if patient.historico_obstetrico:
        sections.append(f"## Histórico obstétrico\n\n{patient.historico_obstetrico.strip()}")
    if patient.observacoes_dra:
        sections.append(f"## Observações pessoais da Dra.\n\n{patient.observacoes_dra.strip()}")

    fm_text = "\n".join(lines)
    body_text = "\n\n".join(sections)
    if fm_text and body_text:
        return f"{fm_text}\n\n---\n\n{body_text}"
    return fm_text or body_text


def _load_recent_messages(db: Session, patient_id: int, limit: int = 50) -> list[Message]:
    """Ultimas N mensagens da paciente (todos os direcionamentos), ordem cronologica."""
    rows = db.exec(
        select(Message)
        .where(Message.patient_id == patient_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))  # ordem cronologica pra leitura natural


def _load_recent_escalation(db: Session, patient_id: int) -> Escalation | None:
    """Pega a escalada mais recente da paciente (sem cutoff temporal — pode ser antiga)."""
    return db.exec(
        select(Escalation)
        .where(Escalation.patient_id == patient_id)
        .order_by(Escalation.created_at.desc())
        .limit(1)
    ).first()


_CLINICAL_FEEDBACK_PROMPT = """\
Você é um assistente clínico de apoio à decisão da {doctor_name}, médica obstetra/ginecologista.
Recebeu o histórico de uma paciente e precisa devolver uma análise INTERNA pra doutora — \
não pra paciente.

**Estrutura da resposta** (use markdown leve, sem cabeçalho de saudação, em pt-BR):

**Feedback interno — {patient_name}**

**Hipóteses (em ordem de probabilidade):**
- 1ª hipótese · breve justificativa baseada nos sintomas
- 2ª · ...
- 3ª se relevante

**Diferenciais a considerar:**
- alternativas razoáveis dado o quadro

**Sinais de alarme a vigiar:**
- bandeira vermelha 1
- bandeira vermelha 2

**O que perguntar / examinar pra refinar:**
- 2-4 perguntas/exames específicos

**Conduta sugerida (apoio, não prescrição):**
- 1-3 condutas seguras dado o contexto da paciente (idade gestacional, comorbidades)
- Mencione fármacos com restrições gestacionais quando aplicável

_Hipóteses pra apoio, doutora — decisão clínica é sua._

**Restrições:**
- Se o histórico for insuficiente (poucas mensagens, sem queixa clara), diga isso e sugira o que falta saber.
- Considere SEMPRE idade gestacional e comorbidades da anamnese se disponíveis.
- Não invente dados que não estão no histórico.
- Tom direto, profissional, sem floreios.
- Sem disclaimer longo — só a frase final em itálico.
- Máximo ~400 palavras.
"""


async def _run_clinical_feedback(
    *,
    patient: Patient,
    db: Session,
) -> str:
    """Chama Opus 4.7 com o historico da paciente e devolve a analise como string."""
    anamnese = _format_patient_anamnese(patient)
    messages = _load_recent_messages(db, patient.id or 0, limit=50)
    last_escalation = _load_recent_escalation(db, patient.id or 0)

    display_name = patient.name or patient.phone

    # Monta contexto
    parts: list[str] = []
    parts.append(f"<paciente>\nNome: {display_name}\nTelefone: {patient.phone}")
    if patient.gestational_weeks is not None:
        parts.append(f"Semanas gestacionais (campo no DB): {patient.gestational_weeks}")
    parts.append("</paciente>")

    if anamnese:
        parts.append(f"<anamnese_vault>\n{anamnese}\n</anamnese_vault>")
    else:
        parts.append("<anamnese_vault>(sem anamnese cadastrada no vault)</anamnese_vault>")

    if last_escalation:
        when = last_escalation.created_at.replace(tzinfo=timezone.utc).astimezone(_BRT)
        parts.append(
            f"<ultima_escalada>\n"
            f"  data: {when.strftime('%d/%m/%Y %H:%M')} BRT\n"
            f"  motivo: {last_escalation.reason}\n"
            f"  resumo: {last_escalation.summary}\n"
            f"</ultima_escalada>"
        )
    else:
        parts.append("<ultima_escalada>(nenhuma escalada registrada)</ultima_escalada>")

    if messages:
        parts.append("<conversa_recente>")
        for m in messages:
            who = "PACIENTE" if m.direction == MessageDirection.INBOUND else "BOT/DRA"
            when = m.created_at.replace(tzinfo=timezone.utc).astimezone(_BRT).strftime("%d/%m %H:%M")
            text_short = (m.text or "").strip().replace("\n", " ")
            if len(text_short) > 400:
                text_short = text_short[:400] + "…"
            parts.append(f"  [{when}] {who}: {text_short}")
        parts.append("</conversa_recente>")
    else:
        parts.append("<conversa_recente>(sem mensagens registradas)</conversa_recente>")

    user_content = "\n\n".join(parts)

    system_prompt = _CLINICAL_FEEDBACK_PROMPT.format(
        doctor_name=settings.doctor_name,
        patient_name=display_name,
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model,  # Opus 4.7 — raciocinio clinico
        max_tokens=2000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=[{"type": "text", "text": system_prompt}],
        messages=[{"role": "user", "content": user_content}],
    )

    log.info(
        "feedback_interno paciente=%s in=%d out=%d",
        patient.phone,
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    text_blocks = [
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip()
    ]
    if not text_blocks:
        return f"Feedback interno — {patient.name or patient.phone}\n\n(modelo não retornou análise — tente novamente)"
    return "\n".join(text_blocks).strip()


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
            _active_patients_block(db),
            _pending_scheduled_block(db),
        ]
        user_content = (
            "\n\n".join(context_parts)
            + f"\n\n<mensagem_da_doutora>\n{text}\n</mensagem_da_doutora>"
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

        # Dedup: garante NO MÁXIMO 1 chamada de responder_doutora por turno do relay
        # (Sonnet às vezes ignora a regra de ouro do prompt e duplica confirmações).
        responder_doutora_count = 0

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
                # Dedup: bloqueia chamada repetida de responder_doutora no mesmo turno
                if tool_use.name == "responder_doutora":
                    if responder_doutora_count >= 1:
                        log.info("relay dedup: responder_doutora ja chamada neste turno — bloqueada")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": "Confirmação à doutora já foi enviada neste turno. Encerre o turno.",
                            "is_error": True,
                        })
                        continue
                    responder_doutora_count += 1

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

        if name == "cancelar_lembrete":
            sched_id = arguments.get("id")
            if sched_id is None:
                return "Erro: id é obrigatório."
            try:
                sched_id = int(sched_id)
            except (ValueError, TypeError):
                return f"Erro: id inválido ({sched_id})."

            sched = db.exec(
                select(ScheduledMessage).where(ScheduledMessage.id == sched_id)
            ).first()
            if not sched:
                return f"Erro: lembrete id={sched_id} não existe."
            if sched.sent_at is not None:
                when = sched.sent_at.replace(tzinfo=timezone.utc).astimezone(_BRT).strftime("%d/%m %H:%M")
                return f"Erro: lembrete id={sched_id} já foi enviado em {when}, não dá pra cancelar."
            if sched.cancelled_at is not None:
                return f"Lembrete id={sched_id} já estava cancelado."

            sched.cancelled_at = utcnow()
            db.add(sched)
            db.commit()
            log.info("cancelado lembrete id=%d via relay agent", sched_id)
            return f"Lembrete id={sched_id} cancelado com sucesso."

        if name == "feedback_interno":
            telefone = str(arguments.get("telefone", "")).strip()
            if not telefone:
                return "Erro: telefone é obrigatório."
            if not settings.doctor_phone_number:
                return "Erro: DOCTOR_PHONE_NUMBER não configurado."

            patient = db.exec(select(Patient).where(Patient.phone == telefone)).first()
            if not patient:
                return f"Erro: paciente com telefone {telefone} não está cadastrada."

            try:
                feedback_text = await _run_clinical_feedback(patient=patient, db=db)
            except Exception as exc:
                log.exception("falha gerando feedback interno pra %s", telefone)
                return f"Erro ao gerar feedback: {exc}"

            await provider.send_text(settings.doctor_phone_number, feedback_text)
            return (
                f"Feedback enviado à doutora sobre {patient.name or telefone}. "
                f"PARE AQUI — não chame mais nenhuma ferramenta neste turno."
            )

        return f"Ferramenta desconhecida: {name}"


_relay_instance: RelayAgent | None = None


def get_relay_agent() -> RelayAgent:
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = RelayAgent()
    return _relay_instance
