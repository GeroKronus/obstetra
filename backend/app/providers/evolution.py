import logging

import httpx

from ..config import settings
from .base import WhatsAppProvider

log = logging.getLogger("obstetra.provider.evolution")


class EvolutionProvider(WhatsAppProvider):
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        instance: str | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = (base_url or settings.evolution_api_url).rstrip("/")
        self.api_key = api_key or settings.evolution_api_key
        self.instance = instance or settings.evolution_instance_name
        self._client = httpx.AsyncClient(
            timeout=timeout_s,
            headers={"apikey": self.api_key, "Content-Type": "application/json"},
        )

    async def send_text(self, phone: str, text: str) -> str:
        url = f"{self.base_url}/message/sendText/{self.instance}"
        resp = await self._client.post(url, json={"number": phone, "text": text})
        resp.raise_for_status()
        body = resp.json()
        msg_id = (body.get("key") or {}).get("id") or ""
        log.info("sent to=%s id=%s len=%d", phone, msg_id, len(text))
        return msg_id

    async def mark_read(self, phone: str, message_id: str) -> None:
        url = f"{self.base_url}/chat/markMessageAsRead/{self.instance}"
        try:
            await self._client.post(
                url,
                json={
                    "readMessages": [
                        {
                            "remoteJid": f"{phone}@s.whatsapp.net",
                            "fromMe": False,
                            "id": message_id,
                        }
                    ]
                },
            )
        except httpx.HTTPError as exc:
            log.warning("markRead failed for %s: %s", phone, exc)

    async def download_media_base64(self, message_payload: dict) -> tuple[str, str] | None:
        """Baixa midia (imagem/audio/etc.) de uma mensagem ja recebida pelo webhook.
        Retorna (base64, mimetype) ou None em caso de falha."""
        url = f"{self.base_url}/chat/getBase64FromMediaMessage/{self.instance}"
        body = {
            "message": {
                "key": message_payload.get("key"),
                "message": message_payload.get("message"),
            },
            "convertToMp4": False,
        }
        try:
            resp = await self._client.post(url, json=body, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            b64 = data.get("base64") or ""
            mime = data.get("mimetype") or "image/jpeg"
            if not b64:
                log.warning("getBase64FromMediaMessage retornou vazio: %s", data)
                return None
            return b64, mime
        except Exception as exc:
            log.exception("download_media_base64 falhou: %s", exc)
            return None

    async def aclose(self) -> None:
        await self._client.aclose()
