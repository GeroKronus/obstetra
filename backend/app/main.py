import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .db import init_db
from .webhook import router as webhook_router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("obstetra")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Obstetra backend started (model=%s)", settings.anthropic_model)
    yield


app = FastAPI(title="Obstetra", lifespan=lifespan)
app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
