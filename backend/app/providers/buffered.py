"""Provider em memória usado pelo endpoint /simulate.

Captura todas as chamadas a `send_text` numa lista, sem enviar nada pra
WhatsApp/Evolution. Útil pra testar o agente ponta-a-ponta sem precisar
parear o WhatsApp.
"""

import logging

from .base import WhatsAppProvider

log = logging.getLogger("obstetra.provider.buffered")


class BufferedProvider(WhatsAppProvider):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []  # [(phone, text), ...] na ordem de envio

    async def send_text(self, phone: str, text: str) -> str:
        self.sent.append((phone, text))
        log.info("buffered to=%s len=%d", phone, len(text))
        return f"sim_{len(self.sent)}"

    async def mark_read(self, phone: str, message_id: str) -> None:
        return  # no-op em simulacao
