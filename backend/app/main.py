import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from . import vault
from .admin import router as admin_router
from .config import settings
from .db import init_db
from .scheduler import scheduler_loop
from .simulate import router as simulate_router
from .webhook import router as webhook_router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("obstetra")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        await vault.init_vault()
    except Exception:
        log.exception("vault init falhou — bot vai funcionar sem contexto da paciente")

    # Background scheduler task — envia ScheduledMessages quando chega o momento
    scheduler_task = asyncio.create_task(scheduler_loop())

    log.info("Obstetra backend started (model=%s)", settings.anthropic_model)
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Obstetra", lifespan=lifespan)
# GZip cuts response payload (HTML/CSS) ~7-10x, big win on Brazil↔US-East route
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(webhook_router)
app.include_router(simulate_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
