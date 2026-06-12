"""Classificadores leves usados em hot paths do webhook.

Objetivo: latência baixa, custo mínimo, decisão binária. Usa Haiku 4.5
(~$0.0005 por chamada) em vez do Opus 4.7 da triagem principal.
"""

from __future__ import annotations

import logging

import anthropic

from .config import settings

log = logging.getLogger("obstetra.classifier")

_HAIKU_MODEL = "claude-haiku-4-5"

_CLOSING_PROMPT = """\
Sua única tarefa é classificar se uma mensagem trocada entre médica e paciente \
sinaliza encerramento natural da conversa atual. Conta como encerramento:
- despedida ou agradecimento final ("até logo", "tchau", "obrigada", "vou indo", \
"qualquer coisa eu te chamo", "até segunda", "beijos", etc.)
- autorização pra comunicar/passar o caso pra doutora ("pode passar", "pode avisar \
ela", "pode comunicar", "só isso mesmo", "não tenho mais nada a acrescentar", "é isso")

Responda EXATAMENTE com uma das duas palavras, sem mais nada:
- SIM (despedida, encerramento claro, ou autorização pra comunicar a doutora)
- NAO (pergunta, pedido de informação, sintoma novo, ou continuação ativa do diálogo)

Mensagem a classificar:
"{text}"
"""


_async_client: anthropic.AsyncAnthropic | None = None


def _client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _async_client


async def is_closing_message(text: str) -> bool:
    """Retorna True se a mensagem parece encerramento de conversa."""
    if not text or not text.strip():
        return False

    # Pré-filtro de keywords óbvias — economiza uma chamada de API quando o caso é trivial
    lowered = text.strip().lower()
    if len(lowered) <= 60:
        triviais = (
            "tchau", "até logo", "até mais", "obrigada doutora", "obrigado doutora",
            "obrigada", "obrigado", "valeu", "beijos", "abraço", "abraços",
            "até semana", "até segunda", "até amanhã", "boa noite",
            "pode passar", "pode avisar", "pode comunicar", "pode falar com ela",
            "só isso", "é só isso", "so isso", "é isso", "nada mais", "mais nada",
        )
        for kw in triviais:
            if kw in lowered:
                # Se a mensagem é curta e tem keyword de despedida, classifica como sim direto
                return True

    try:
        resp = await _client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=8,
            messages=[{"role": "user", "content": _CLOSING_PROMPT.format(text=text[:500])}],
        )
        out = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                out = (block.text or "").strip().upper()
                break
        decision = out.startswith("SIM")
        log.info("classifier closing: %r → %s (Haiku=%s)", text[:60], decision, out)
        return decision
    except Exception:
        log.exception("closing classifier failed — assumindo nao-encerramento")
        return False
