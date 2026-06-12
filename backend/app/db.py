import logging
from pathlib import Path
from typing import Iterator

import bcrypt
from sqlmodel import Session, SQLModel, create_engine, select

from .config import settings

log = logging.getLogger("obstetra.db")

_db_url = settings.database_url
if _db_url.startswith("sqlite:///"):
    path_part = _db_url.removeprefix("sqlite:///")
    if path_part.startswith("/"):
        db_path = Path(path_part)
    else:
        db_path = Path(path_part).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

if _db_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
else:
    # Forca UTF-8 na conexao Postgres pra evitar double-encoding em strings
    # com acentos ("ã" → "Ã£") quando o servidor tem client_encoding != utf8
    _connect_args = {"client_encoding": "utf8"}

engine = create_engine(
    _db_url,
    echo=False,
    connect_args=_connect_args,
)


def init_db() -> None:
    from . import models  # noqa: F401 — ensure tables are registered
    SQLModel.metadata.create_all(engine)
    _run_lightweight_migrations()
    _bootstrap_default_tenant()
    _import_vault_legacy()


# =====================================================================
# Migrations leves — adicionam colunas em tabelas existentes
# (SQLAlchemy create_all cria tabelas novas e adiciona colunas em models
# novos, mas NÃO altera tabelas existentes pra adicionar colunas).
# =====================================================================

def _run_lightweight_migrations() -> None:
    """Idempotente. Roda em todo startup."""

    # Ordem importa: primeiro adiciona tenant_id em todas as tabelas dependentes
    # de Tenant, depois adiciona colunas de anamnese em Patient.
    migrations = [
        # tenant_id em todas as tabelas (default 1 = primeiro tenant seedado)
        ("patient",          "tenant_id",        "INTEGER NOT NULL DEFAULT 1"),
        ("message",          "tenant_id",        "INTEGER NOT NULL DEFAULT 1"),
        ("escalation",       "tenant_id",        "INTEGER NOT NULL DEFAULT 1"),
        ("scheduledmessage", "tenant_id",        "INTEGER NOT NULL DEFAULT 1"),
        ("appointment",      "tenant_id",        "INTEGER NOT NULL DEFAULT 1"),
        ("tenant",           "secretary_phone",  "TEXT NULL"),
        ("scheduledmessage", "appointment_id",   "INTEGER NULL"),
        ("escalation",       "status",           "TEXT NOT NULL DEFAULT 'SENT'"),
        ("escalation",       "sent_at",          "TIMESTAMP NULL"),

        # Patient: campos de anamnese
        ("patient", "data_nascimento",            "DATE NULL"),
        ("patient", "endereco",                   "TEXT NULL"),
        ("patient", "plano_saude",                "TEXT NULL"),
        ("patient", "hospital_referencia",        "TEXT NULL"),
        ("patient", "medico_obstetra",            "TEXT NULL"),
        ("patient", "dum",                        "DATE NULL"),
        ("patient", "tipo_gestacao",              "TEXT NULL"),
        ("patient", "risco",                      "TEXT NULL"),
        ("patient", "gestacao_planejada",         "BOOLEAN NULL"),
        ("patient", "gestas",                     "INTEGER NULL"),
        ("patient", "partos_normais",             "INTEGER NULL"),
        ("patient", "cesareas",                   "INTEGER NULL"),
        ("patient", "abortos",                    "INTEGER NULL"),
        ("patient", "alergias",                   "TEXT NULL"),
        ("patient", "condicoes_pre_existentes",   "TEXT NULL"),
        ("patient", "medicacoes_em_uso",          "TEXT NULL"),
        ("patient", "grupo_sanguineo",            "TEXT NULL"),
        ("patient", "contato_emergencia_nome",    "TEXT NULL"),
        ("patient", "contato_emergencia_telefone","TEXT NULL"),
        ("patient", "contato_emergencia_relacao", "TEXT NULL"),
        ("patient", "historico_clinico",          "TEXT NULL"),
        ("patient", "historico_obstetrico",       "TEXT NULL"),
        ("patient", "observacoes_dra",            "TEXT NULL"),
        ("patient", "preferencias_atendimento",   "TEXT NULL"),
        ("patient", "status",                     "TEXT NOT NULL DEFAULT 'ATIVA'"),
        ("patient", "manual_handover_at",         "TIMESTAMP NULL"),
        ("message",  "source",                    "TEXT NOT NULL DEFAULT 'BOT'"),
    ]

    with engine.connect() as conn:
        for table, column, decl in migrations:
            try:
                if engine.dialect.name == "sqlite":
                    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                    cols = {r[1] for r in rows}
                else:
                    rows = conn.exec_driver_sql(
                        f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}'"
                    ).fetchall()
                    cols = {r[0] for r in rows}
                if column not in cols:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    conn.commit()
                    log.info("migration ALTER %s ADD %s", table, column)
            except Exception:
                pass

        # Normaliza valores de message.source pra uppercase (enum-name) — antiga
        # migration deixou 'bot' em rows velhas
        try:
            conn.exec_driver_sql("UPDATE message SET source = 'BOT'     WHERE source = 'bot'")
            conn.exec_driver_sql("UPDATE message SET source = 'PATIENT' WHERE source = 'patient'")
            conn.exec_driver_sql("UPDATE message SET source = 'MANUAL'  WHERE source = 'manual'")
            conn.commit()
        except Exception:
            pass


# =====================================================================
# Bootstrap: garante que existe ao menos um Tenant (id=1) com creds do .env
# =====================================================================

def _bootstrap_default_tenant() -> None:
    from .models import Tenant

    if not (settings.admin_user and settings.admin_password):
        log.warning("ADMIN_USER/PASSWORD ausentes no env — pulando bootstrap de tenant")
        return

    with Session(engine) as db:
        existing = db.exec(select(Tenant).where(Tenant.id == 1)).first()
        if existing:
            # Atualiza creds/instance se mudaram no env
            changed = False
            password_hash = _hash_password(settings.admin_password)
            if existing.admin_user != settings.admin_user:
                existing.admin_user = settings.admin_user
                changed = True
            if not bcrypt.checkpw(settings.admin_password.encode(), existing.admin_password_hash.encode()):
                existing.admin_password_hash = password_hash
                changed = True
            if existing.doctor_phone != (settings.doctor_phone_number or ""):
                existing.doctor_phone = settings.doctor_phone_number or ""
                changed = True
            if existing.doctor_name != settings.doctor_name:
                existing.doctor_name = settings.doctor_name
                changed = True
            if existing.evolution_instance_name != settings.evolution_instance_name:
                existing.evolution_instance_name = settings.evolution_instance_name
                changed = True
            if (existing.secretary_phone or "") != (settings.secretary_phone_number or ""):
                existing.secretary_phone = settings.secretary_phone_number or None
                changed = True
            if changed:
                from .models import utcnow
                existing.updated_at = utcnow()
                db.add(existing)
                db.commit()
                log.info("tenant default sincronizado com env")
            return

        # Cria tenant default
        tenant = Tenant(
            id=1,
            slug="default",
            name=settings.doctor_name,
            doctor_name=settings.doctor_name,
            doctor_phone=settings.doctor_phone_number or "",
            secretary_phone=settings.secretary_phone_number or None,
            evolution_instance_name=settings.evolution_instance_name,
            admin_user=settings.admin_user,
            admin_password_hash=_hash_password(settings.admin_password),
        )
        db.add(tenant)
        db.commit()
        log.info("tenant default criado (id=1, slug=default)")


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# =====================================================================
# Importacao one-shot do vault legacy: le pacientes/<phone>/anamnese.md
# se ainda existirem em disco e popula Patient rows que tem name=NULL.
# Idempotente — pula pacientes que ja tem dados.
# =====================================================================

def _import_vault_legacy() -> None:
    from datetime import date as _date
    try:
        import frontmatter
    except ImportError:
        return
    from pathlib import Path
    from .config import settings as _settings
    from .models import Patient, OnboardingState

    vault_root = Path(_settings.vault_local_path) / "pacientes"
    if not vault_root.exists():
        return

    log.info("import vault legacy: scanning %s", vault_root)

    def _to_date(v):
        if isinstance(v, _date):
            return v
        if isinstance(v, str) and v.strip():
            try:
                return _date.fromisoformat(v.strip())
            except ValueError:
                return None
        return None

    def _join(v):
        if v is None: return None
        if isinstance(v, list): return ", ".join(str(x) for x in v if x)
        return str(v)

    def _section(body: str, heading: str) -> str | None:
        if not body: return None
        target = f"## {heading}".lower()
        out: list[str] = []
        capturing = False
        for line in body.splitlines():
            ls = line.strip().lower()
            if ls.startswith("## "):
                if capturing: break
                if ls == target: capturing = True; continue
            if capturing: out.append(line)
        text = "\n".join(out).strip()
        if text.startswith("*(") and text.endswith(")*") and "\n" not in text:
            return None
        return text or None

    imported = 0
    with Session(engine) as db:
        for d in sorted(vault_root.iterdir()):
            if not d.is_dir():
                continue
            anamnese = d / "anamnese.md"
            if not anamnese.exists():
                continue
            try:
                post = frontmatter.load(anamnese)
                fm = post.metadata or {}
                body = post.content or ""
            except Exception:
                continue

            phone = d.name
            existing = db.exec(select(Patient).where(Patient.phone == phone)).first()
            if existing is None:
                existing = Patient(
                    phone=phone,
                    onboarding_state=OnboardingState.DONE,
                )
                db.add(existing)

            # Atualiza apenas campos vazios (idempotente, nao sobrescreve dados ja editados via web)
            def _set(attr: str, value):
                if value is None or (isinstance(value, str) and not value.strip()):
                    return
                if getattr(existing, attr, None) in (None, ""):
                    setattr(existing, attr, value)

            _set("name", fm.get("nome"))
            _set("data_nascimento", _to_date(fm.get("data_nascimento")))
            _set("endereco", fm.get("endereco"))
            _set("plano_saude", fm.get("plano_saude"))
            _set("hospital_referencia", fm.get("hospital_referencia"))
            _set("medico_obstetra", fm.get("medico_obstetra"))
            _set("dum", _to_date(fm.get("dum")))
            _set("tipo_gestacao", fm.get("tipo_gestacao"))
            _set("risco", fm.get("risco"))
            if fm.get("gestacao_planejada") is not None and existing.gestacao_planejada is None:
                existing.gestacao_planejada = bool(fm.get("gestacao_planejada"))
            for k in ("gestas", "partos_normais", "cesareas", "abortos"):
                v = fm.get(k)
                if v is not None and getattr(existing, k, None) in (None, 0):
                    try: setattr(existing, k, int(v))
                    except (TypeError, ValueError): pass
            _set("alergias", _join(fm.get("alergias")))
            _set("condicoes_pre_existentes", _join(fm.get("condicoes_pre_existentes")))
            _set("medicacoes_em_uso", _join(fm.get("medicacoes_em_uso")))
            _set("grupo_sanguineo", fm.get("grupo_sanguineo"))
            _set("contato_emergencia_nome", fm.get("contato_emergencia_nome"))
            _set("contato_emergencia_telefone", fm.get("contato_emergencia_telefone"))
            _set("contato_emergencia_relacao", fm.get("contato_emergencia_relacao"))
            _set("preferencias_atendimento", fm.get("preferencias_atendimento"))
            _set("historico_clinico", _section(body, "Histórico clínico relevante"))
            _set("historico_obstetrico", _section(body, "Histórico obstétrico"))
            _set("observacoes_dra", _section(body, "Observações pessoais da Dra."))

            db.add(existing)
            imported += 1

        if imported:
            db.commit()
            log.info("import vault legacy: %d pacientes processadas", imported)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
