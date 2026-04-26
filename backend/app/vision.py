"""Descricao factual de imagens enviadas pela paciente via WhatsApp.

Usa Claude Haiku 4.5 (multimodal) pra gerar uma descricao curta e neutra
do conteudo da imagem. Essa descricao depois e passada como TEXTO
pro agente principal (Opus 4.7), que decide o que fazer.

Restricoes deliberadas:
- NUNCA interpreta clinicamente (diagnostico, opiniao medica)
- Apenas descreve o que se ve / transcreve texto visivel
- Em imagens de exame: transcreve valores
- Em imagens corporais: descreve neutralmente (cor, localizacao)
"""

from __future__ import annotations

import logging

import anthropic

from .config import settings

log = logging.getLogger("obstetra.vision")

_HAIKU_MODEL = "claude-haiku-4-5"

_DESCRIBE_PROMPT = """\
Voce e um descritor factual de imagens enviadas por pacientes gestantes a um consultorio de obstetricia.

Sua unica tarefa: descrever em PT-BR, em 1-4 frases curtas, EXATAMENTE o que esta visivel na imagem.

Tipos comuns de imagens e como descrever cada um:

- DOCUMENTO de exame (USG, hemograma, urina, glicemia, etc.): TRANSCREVA os dados principais que aparecem (nome do exame, valores numericos, valores de referencia se visiveis, conclusao do laudo). Identifique o tipo de exame primeiro.
- RECEITA medica: transcreva nome do(s) medicamento(s), dose, posologia.
- FOTO DE PARTE DO CORPO (mancha, inchaco, lesao, etc.): descreva neutramente — cor, tamanho aparente, localizacao na foto. Sem usar termos medicos como "edema", "eritema", etc.; use "inchaco", "vermelhidao".
- FOTO DE TESTE DE GRAVIDEZ: descreva o resultado visivel (positivo/negativo/inconclusivo, se houver indicacoes).
- FOTO DE BARRIGA, USG impresso, etc.: descreva neutramente o que aparece.
- FOTO PESSOAL ou cenario nao-clinico: descreva brevemente em 1 frase.
- IMAGEM ILEGIVEL/escura/desfocada: diga que nao foi possivel identificar.

PROIBIDO:
- Fazer diagnostico
- Dar opiniao clinica ("isso parece...", "pode ser...", "indica que...")
- Recomendar conduta
- Tranquilizar ou alarmar
- Especular sobre o que nao esta visivel

APENAS DESCREVA o que esta literalmente na imagem.
"""


_async_client: anthropic.AsyncAnthropic | None = None


def _client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _async_client


async def describe_image(base64_data: str, mimetype: str = "image/jpeg") -> str:
    """Retorna descricao factual da imagem em PT-BR. String vazia em caso de erro."""
    if not base64_data:
        return ""

    # Normaliza mimetype (Evolution as vezes manda só "image" sem subtipo)
    if "/" not in mimetype:
        mimetype = "image/jpeg"

    try:
        resp = await _client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=500,
            system=_DESCRIBE_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mimetype,
                            "data": base64_data,
                        },
                    },
                    {"type": "text", "text": "Descreva esta imagem seguindo exatamente as regras."},
                ],
            }],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                description = (block.text or "").strip()
                log.info("vision described %d-byte image: %r", len(base64_data), description[:120])
                return description
        return ""
    except Exception:
        log.exception("describe_image failed")
        return ""
