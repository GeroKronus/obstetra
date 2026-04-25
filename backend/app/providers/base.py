from abc import ABC, abstractmethod


class WhatsAppProvider(ABC):
    """Abstracts the WhatsApp channel so we can swap Evolution API for the
    official Business API later without touching business logic.
    """

    @abstractmethod
    async def send_text(self, phone: str, text: str) -> str:
        """Send a plain-text message. Returns the provider's message id."""

    @abstractmethod
    async def mark_read(self, phone: str, message_id: str) -> None:
        """Mark an inbound message as read (best-effort; may be a no-op)."""
