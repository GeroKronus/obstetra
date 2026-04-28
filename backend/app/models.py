from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OnboardingState(str, Enum):
    NEW = "new"
    ASKING_NAME = "asking_name"
    ASKING_WEEKS = "asking_weeks"
    ASKING_CONFIRM_PATIENT = "asking_confirm_patient"
    DONE = "done"
    OUT_OF_SCOPE = "out_of_scope"


class Patient(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str = Field(index=True, unique=True)
    name: Optional[str] = None
    gestational_weeks: Optional[int] = None
    is_doctor_patient: Optional[bool] = None
    onboarding_state: OnboardingState = Field(default=OnboardingState.NEW)
    # Quando a Dra. assume manualmente o atendimento (digita no WhatsApp do bot),
    # esse campo recebe a hora do takeover. Enquanto != None, o agente fica silencioso.
    # É limpo quando alguma mensagem é classificada como "fim de conversa" pelo Haiku.
    manual_handover_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageSource(str, Enum):
    BOT = "bot"           # outbound enviado pelo agente via API
    MANUAL = "manual"     # outbound digitado manualmente no WhatsApp do bot (Dra. assumiu)
    PATIENT = "patient"   # inbound da paciente


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    direction: MessageDirection
    text: str
    whatsapp_message_id: Optional[str] = Field(default=None, index=True)
    source: MessageSource = Field(default=MessageSource.BOT)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Escalation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    reason: str
    summary: str
    notified_doctor: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class ScheduledMessage(SQLModel, table=True):
    """Mensagem agendada pra enviar a uma paciente em momento futuro.
    Tipicamente criada pelo relay agent quando a Dra. pede um lembrete.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    text: str
    scheduled_at: datetime = Field(index=True)  # armazenado em UTC
    sent_at: Optional[datetime] = Field(default=None, index=True)
    cancelled_at: Optional[datetime] = None
    error: Optional[str] = None
    created_by: str = "doctor_relay"  # quem criou (futuro: admin, system, etc.)
    created_at: datetime = Field(default_factory=utcnow)
