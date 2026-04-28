import os
from pathlib import Path
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine

from .config import settings

_db_url = settings.database_url
if _db_url.startswith("sqlite:///"):
    path_part = _db_url.removeprefix("sqlite:///")
    if path_part.startswith("/"):
        db_path = Path(path_part)
    else:
        db_path = Path(path_part).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    _db_url,
    echo=False,
    connect_args={"check_same_thread": False} if _db_url.startswith("sqlite") else {},
)


def init_db() -> None:
    from . import models  # noqa: F401 — ensure tables are registered
    SQLModel.metadata.create_all(engine)
    _run_lightweight_migrations()


def _run_lightweight_migrations() -> None:
    """Adiciona colunas novas em tabelas existentes (SQLAlchemy create_all não faz isso).
    Verifica se a coluna existe antes de tentar adicionar (idempotente).
    Funciona pra SQLite e Postgres."""
    from sqlalchemy import text

    migrations = [
        ("patient", "manual_handover_at", "TIMESTAMP NULL"),
        ("message",  "source",             "TEXT NOT NULL DEFAULT 'BOT'"),
    ]
    with engine.connect() as conn:
        for table, column, decl in migrations:
            try:
                # Tenta listar colunas; se a coluna já existe, pula
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
            except Exception:
                # Se a tabela ainda não existe (primeiro startup), create_all já lidou
                pass

        # Normaliza valores de enum em message.source — SQLAlchemy persiste pelo NOME
        # (uppercase), mas a migration anterior setou DEFAULT 'bot' (lowercase). Idempotente.
        try:
            conn.exec_driver_sql(
                "UPDATE message SET source = 'BOT' WHERE source = 'bot'"
            )
            conn.exec_driver_sql(
                "UPDATE message SET source = 'PATIENT' WHERE source = 'patient'"
            )
            conn.exec_driver_sql(
                "UPDATE message SET source = 'MANUAL' WHERE source = 'manual'"
            )
            conn.commit()
        except Exception:
            pass


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
