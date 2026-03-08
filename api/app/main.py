import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db_pool, close_db_pool
from app.routers import analytics, generate, products, styles, upload

logger = logging.getLogger(__name__)
DEPLOY_MARKER = "diag-20260308-1"


async def _init_db_with_retry(max_attempts: int = 5, delay: float = 5.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            await init_db_pool()
            logger.info("Database pool initialized successfully")
            return
        except Exception as exc:
            logger.warning("DB connect attempt %d/%d failed: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                await asyncio.sleep(delay)
    logger.error("Could not connect to database after %d attempts – DB-backed endpoints will fail", max_attempts)


PUBLIC_DIR = Path(__file__).parent.parent / "public"
PUBLIC_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Try to connect to the DB but don't crash the process if it's unreachable.
    # This keeps /health alive while the DB warms up or is being provisioned.
    asyncio.ensure_future(_init_db_with_retry())

    yield

    await close_db_pool()


app = FastAPI(
    title="AI Home Styling Platform",
    description="POC API for AI-powered interior design styling",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/public", StaticFiles(directory=str(PUBLIC_DIR)), name="public")

app.include_router(upload.router, prefix="/api", tags=["Upload"])
app.include_router(styles.router, prefix="/api", tags=["Styles"])
app.include_router(generate.router, prefix="/api", tags=["Generate"])
app.include_router(products.router, prefix="/api", tags=["Products"])
app.include_router(analytics.router, prefix="/api", tags=["Analytics"])


@app.get("/")
async def root():
    return {"status": "ok", "message": "AI Home Styling API"}


@app.get("/health")
async def health():
    return {"status": "ok", "marker": DEPLOY_MARKER}
