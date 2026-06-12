from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Retorna UTC naive (sem tzinfo) — alinhado com o que SQLite armazena
    e evita TypeError 'offset-naive vs offset-aware' em comparações."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# =====================================================================
# Tenant — base do multi-tenant. Um por médico/clínica.
# =====================================================================

class Tenant(SQLModel, table=True):
    """Médico/clínica usando o sistema. Toda outra entidade pertence a um Tenant.

    Pra MVP single-tenant, o startup auto-cria um Tenant id=1 a partir das env vars.
    Schema multi-tenant; código resolve tenant_id=1 por padrao.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)  # ex: "leiza", usado em URLs futuras
    name: str  # nome amigavel da clinica/medico (ex: "Dra. Leiza Moulin")
    doctor_name: str  # como chamar nos prompts (ex: "Dra. Leiza")
    doctor_phone: str  # E.164 sem +, ex: 5528981110534
    secretary_phone: Optional[str] = None  # E.164 sem +; recebe avisos de desistencia
    evolution_instance_name: str  # qual instancia Evolution recebe webhooks desse tenant
    admin_user: str  # username do painel web
    admin_password_hash: str  # bcrypt hash da senha
    settings_json: Optional[str] = None  # extras futuros (JSON)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# =====================================================================
# Patient — agora carrega anamnese inline (sem markdown/vault)
# =====================================================================

class OnboardingState(str, Enum):
    NEW = "new"
    ASKING_NAME = "asking_name"
    ASKING_WEEKS = "asking_weeks"
    ASKING_CONFIRM_PATIENT = "asking_confirm_patient"
    DONE = "done"
    OUT_OF_SCOPE = "out_of_scope"


class PatientStatus(str, Enum):
    ATIVA = "ativa"
    INATIVA = "inativa"
    CONCLUIDA = "concluida"  # gestação encerrada (parto/perda)


class Patient(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(default=1, foreign_key="tenant.id", index=True)

    # Identificação
    phone: str = Field(index=True)  # unique por tenant via constraint composta
    name: Optional[str] = None
    data_nascimento: Optional[date] = None
    endereco: Optional[str] = None
    plano_saude: Optional[str] = None
    hospital_referencia: Optional[str] = None
    medico_obstetra: Optional[str] = None  # nome do médico responsavel pela paciente

    # Gestação atual
    dum: Optional[date] = None  # Data Última Menstruação — fonte canônica
    tipo_gestacao: Optional[str] = None  # "unica" | "gemelar"
    risco: Optional[str] = None  # "habitual" | "alto"
    gestacao_planejada: Optional[bool] = None
    # cache: paciente reportou X semanas (usado quando DUM não disponível)
    gestational_weeks: Optional[int] = None

    # Histórico obstétrico
    gestas: Optional[int] = None
    partos_normais: Optional[int] = None
    cesareas: Optional[int] = None
    abortos: Optional[int] = None

    # Histórico clínico (campos estruturados ao invés de listas frontmatter)
    alergias: Optional[str] = None  # texto livre, separado por virgula ou nova linha
    condicoes_pre_existentes: Optional[str] = None
    medicacoes_em_uso: Optional[str] = None
    grupo_sanguineo: Optional[str] = None

    # Contato de emergência
    contato_emergencia_nome: Optional[str] = None
    contato_emergencia_telefone: Optional[str] = None
    contato_emergencia_relacao: Optional[str] = None

    # Observações texto livre (lidas pelo bot pra triagem contextual)
    historico_clinico: Optional[str] = None
    historico_obstetrico: Optional[str] = None
    observacoes_dra: Optional[str] = None
    preferencias_atendimento: Optional[str] = None

    # Estado operacional do bot
    is_doctor_patient: Optional[bool] = None  # paciente da Dra. (vs. atendimento out-of-scope)
    onboarding_state: OnboardingState = Field(default=OnboardingState.NEW)
    manual_handover_at: Optional[datetime] = None  # Dra. assumiu = bot silencioso
    status: PatientStatus = Field(default=PatientStatus.ATIVA)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# =====================================================================
# Mensagens, escaladas, agendamentos
# =====================================================================

class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageSource(str, Enum):
    BOT = "bot"           # outbound enviado pelo agente via API
    MANUAL = "manual"     # outbound digitado manualmente no WhatsApp do bot (Dra. assumiu)
    PATIENT = "patient"   # inbound da paciente


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(default=1, foreign_key="tenant.id", index=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    direction: MessageDirection
    text: str
    whatsapp_message_id: Optional[str] = Field(default=None, index=True)
    source: MessageSource = Field(default=MessageSource.BOT)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class EscalationStatus(str, Enum):
    PENDING = "pending"        # registrada; aguardando fim da conversa pra notificar
    SENT = "sent"              # notificacao enviada a doutora
    SUPERSEDED = "superseded"  # substituida por uma versao mais completa na mesma conversa


class Escalation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(default=1, foreign_key="tenant.id", index=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    reason: str
    summary: str
    notified_doctor: bool = False
    status: EscalationStatus = Field(default=EscalationStatus.SENT, index=True)
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class ScheduledMessage(SQLModel, table=True):
    """Mensagem agendada pra enviar a uma paciente em momento futuro.
    Tipicamente criada pelo relay agent quando a Dra. pede um lembrete,
    ou automaticamente 24h antes de uma consulta.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(default=1, foreign_key="tenant.id", index=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    appointment_id: Optional[int] = Field(default=None, foreign_key="appointment.id", index=True)
    text: str
    scheduled_at: datetime = Field(index=True)  # armazenado em UTC
    sent_at: Optional[datetime] = Field(default=None, index=True)
    cancelled_at: Optional[datetime] = None
    error: Optional[str] = None
    created_by: str = "doctor_relay"
    created_at: datetime = Field(default_factory=utcnow)


class AppointmentStatus(str, Enum):
    AGENDADA = "agendada"
    CONFIRMADA = "confirmada"
    REALIZADA = "realizada"
    FALTA = "falta"
    CANCELADA = "cancelada"
    REMARCADA = "remarcada"


class AppointmentType(str, Enum):
    CONSULTA = "consulta"
    RETORNO = "retorno"
    EXAME = "exame"
    OUTRO = "outro"


class Appointment(SQLModel, table=True):
    """Consulta agendada pra paciente."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(default=1, foreign_key="tenant.id", index=True)
    patient_id: int = Field(foreign_key="patient.id", index=True)
    scheduled_at: datetime = Field(index=True)  # UTC naive
    duracao_min: int = Field(default=30)
    tipo: AppointmentType = Field(default=AppointmentType.CONSULTA)
    obs: Optional[str] = None
    status: AppointmentStatus = Field(default=AppointmentStatus.AGENDADA)
    created_by: str = Field(default="secretaria")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    cancelled_at: Optional[datetime] = None
