"""Generation endpoint - runs the AI pipeline synchronously for POC."""

import base64
import json
import logging
import time
from uuid import uuid4

import httpx
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, HTTPException

from app.config import settings
from app.database import get_db_pool
from app.models.schemas import (
    GenerateRequest,
    GenerateResponse,
    Hotspot,
    JobStatus,
    RenderResult,
)
from app.services.storage import upload_image

logger = logging.getLogger(__name__)
router = APIRouter()

# Global ARQ redis pool
_arq_pool = None


async def get_arq_pool():
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool

@router.post("/generate", response_model=GenerateResponse)
async def trigger_generation(request: GenerateRequest):
    job_id = str(uuid4())
    session_id = str(request.session_id)
    style_id = request.style_id

    # Initialize status in Redis
    from redis.asyncio import Redis
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    initial_status = {"job_id": job_id, "status": "queued"}
    await redis.set(f"job:{job_id}:status", json.dumps(initial_status), ex=3600)
    await redis.close()

    # Enqueue task in ARQ
    arq_pool = await get_arq_pool()
    await arq_pool.enqueue_job("generate_renders_task", job_id, session_id, style_id)

    return GenerateResponse(job_id=job_id, status="queued")

@router.get("/generate/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    from redis.asyncio import Redis
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    
    status_json = await redis.get(f"job:{job_id}:status")
    await redis.close()
    
    if not status_json:
        raise HTTPException(status_code=404, detail="Job not found")
        
    status_data = json.loads(status_json)
    return JobStatus(**status_data)

async def _run_pipeline_impl(job_id: str, session_id: str, style_id: str, redis):
    """Run the full AI pipeline: validate -> render -> map hotspots."""
    pool = get_db_pool()
    start_time = time.time()

    async def update_status(**kwargs):
        status = {"job_id": job_id, **kwargs}
        await redis.set(f"job:{job_id}:status", json.dumps(status), ex=3600)

    # --- Phase 1: Analyzing ---
    await update_status(status="analyzing", progress="Analyzing your room...")

    # Get session and uploaded image
    session = await pool.fetchrow("SELECT * FROM sessions WHERE id = $1 LIMIT 1", session_id)
    if not session:
        await update_status(status="failed", error="Session not found")
        return

    image_url = session["uploaded_image_url"]

    # Download the uploaded image
    original_image_bytes = await _download_image(image_url)
    if not original_image_bytes:
        await update_status(status="failed", error="Could not load uploaded image")
        return

    # Validate room (Google Vision)
    from app.ai_pipeline.room_validator import validate_room
    validation = await validate_room(settings.google_cloud_api_key, original_image_bytes)
    if not validation.get("valid"):
        await update_status(status="failed", error=validation.get("error", "Image rejected"))
        return

    # Get the bundle for this style
    bundle = await pool.fetchrow("SELECT * FROM bundles WHERE style = $1 LIMIT 1", style_id)
    if not bundle:
        await update_status(status="failed", error=f"No bundle for style: {style_id}")
        return

    product_ids = bundle["product_ids"]

    # Get products
    if product_ids:
        products_rows = await pool.fetch("SELECT * FROM products WHERE id = ANY($1::uuid[])", product_ids)
        products = [dict(r) for r in products_rows]
    else:
        products = []

    # Download reference product images (up to 4)
    reference_images = []
    for product in products[:4]:
        if product["image_urls"]:
            img_bytes = await _download_image(product["image_urls"][0])
            if img_bytes:
                reference_images.append(img_bytes)

    logger.info("Pipeline phase 1 done in %.1fs", time.time() - start_time)

    # --- Phase 2: Rendering ---
    await update_status(status="rendering", progress="Styling your room...")

    if not settings.openrouter_api_key:
        # No API key - use placeholder renders with real hotspots
        logger.warning("No OpenRouter API key - using placeholder renders")
        renders = await _create_placeholder_renders(job_id, session_id, products, str(bundle["id"]))
        
        # Update session
        render_urls = [r.url for r in renders]
        hotspots_data = [[h.model_dump(mode="json") for h in r.hotspots] for r in renders]
        await pool.execute(
            "UPDATE sessions SET style = $1, bundle_id = $2, render_urls = $3, hotspots = $4 WHERE id = $5",
            style_id, bundle["id"], render_urls, json.dumps(hotspots_data), session_id
        )

        await update_status(status="completed", progress="Done", renders=[r.model_dump(mode="json") for r in renders], bundle_id=str(bundle["id"]))
        return

    # Generate styled renders via Gemini 3.1 Flash Image
    from app.ai_pipeline.style_renderer import generate_styled_room

    render_results = []
    for variant_idx in range(2):  # Generate 2 variants for speed
        render_bytes = await generate_styled_room(
            api_key=settings.openrouter_api_key,
            original_image_bytes=original_image_bytes,
            style=style_id,
            reference_image_bytes_list=reference_images,
            variant_index=variant_idx,
        )

        if render_bytes:
            # Upload render to storage
            render_url = upload_image(render_bytes, folder=f"renders/{session_id}")
            render_results.append((render_bytes, render_url))
        else:
            logger.warning("Variant %d failed to generate", variant_idx)

    if not render_results:
        # All variants failed - use placeholder
        logger.error("All render variants failed, using placeholders")
        renders = await _create_placeholder_renders(job_id, session_id, products, str(bundle["id"]))
        await update_status(status="completed", progress="Done", renders=[r.model_dump(mode="json") for r in renders], bundle_id=str(bundle["id"]))
        return

    logger.info("Pipeline phase 2 done in %.1fs (%d renders)", time.time() - start_time, len(render_results))

    # --- Phase 3: Mapping hotspots ---
    await update_status(status="mapping", progress="Finding products...")

    from app.ai_pipeline.hotspot_mapper import map_hotspots

    renders = []
    for render_bytes, render_url in render_results:
        hotspots_raw = await map_hotspots(
            api_key=settings.openrouter_api_key,
            render_image_bytes=render_bytes,
            bundle_products=products,
        )

        hotspots = [
            Hotspot(
                product_id=h["product_id"],
                x_pct=h["x_pct"],
                y_pct=h["y_pct"],
                category=h["category"],
            )
            for h in hotspots_raw
        ]

        renders.append(RenderResult(url=render_url, hotspots=hotspots))

    # Update session in database
    render_urls = [r.url for r in renders]
    hotspots_data = [[h.model_dump(mode="json") for h in r.hotspots] for r in renders]
    await pool.execute(
        "UPDATE sessions SET style = $1, bundle_id = $2, render_urls = $3, hotspots = $4 WHERE id = $5",
        style_id, bundle["id"], render_urls, json.dumps(hotspots_data), session_id
    )

    elapsed = time.time() - start_time
    logger.info("Pipeline complete in %.1fs", elapsed)

    await update_status(status="completed", progress="Done", renders=[r.model_dump(mode="json") for r in renders], bundle_id=str(bundle["id"]))

async def _download_image(url: str) -> bytes | None:
    """Download an image from a URL."""
    if not url or url.startswith("data:"):
        if url and url.startswith("data:"):
            # Extract base64 data
            try:
                b64 = url.split(",", 1)[1]
                return base64.b64decode(b64)
            except Exception:
                pass
        return None

    try:
        # Handle local storage URLs (when running without cloud storage)
        api_url = settings.api_url.rstrip("/")
        if url.startswith(api_url + "/public/"):
            local_path = url.replace(api_url + "/", "")
            from pathlib import Path
            p = Path(local_path)
            if p.exists():
                return p.read_bytes()

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.warning("Failed to download image %s: %s", url[:100], e)
        return None

async def _create_placeholder_renders(
    job_id: str, session_id: str, products: list[dict], bundle_id: str
) -> list[RenderResult]:
    """Create placeholder render results when AI is not available."""
    from app.ai_pipeline.hotspot_mapper import _fallback_hotspots

    hotspots_raw = _fallback_hotspots(products)
    hotspots = [
        Hotspot(
            product_id=h["product_id"],
            x_pct=h["x_pct"],
            y_pct=h["y_pct"],
            category=h["category"],
        )
        for h in hotspots_raw
    ]

    # Return placeholder URLs
    return [
        RenderResult(
            url=f"https://placehold.co/1200x900/e2e8f0/475569?text=Styled+Room+Variant+1",
            hotspots=hotspots,
        ),
        RenderResult(
            url=f"https://placehold.co/1200x900/fef3c7/92400e?text=Styled+Room+Variant+2",
            hotspots=hotspots,
        ),
    ]
