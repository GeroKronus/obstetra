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
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    direction: MessageDirection
    text: str
    whatsapp_message_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Escalation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    reason: str
    summary: str
    notified_doctor: bool = False
    created_at: datetime = Field(default_factory=utcnow)
