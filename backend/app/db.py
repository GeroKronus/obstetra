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


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
