import asyncio
import os
import tempfile
from urllib.parse import urlparse
from fastapi import Body, FastAPI, HTTPException
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import lifespan, DurationCache, engine
import logging

app = FastAPI(lifespan=lifespan)
logger = logging.getLogger(__name__)
ffprobe_semaphore = asyncio.BoundedSemaphore(5)


async def get_cached_durations(session: AsyncSession, urls: list[str]):
    if not urls:
        return []
    result = await session.execute(
        select(DurationCache).where(DurationCache.url.in_(urls))
    )
    return [{"url": row.url, "duration": row.duration} for row in result.scalars()]


async def get_media_duration(content: bytes):
    tmp_filename = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as temp:
            temp.write(content)
            tmp_filename = temp.name

        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", tmp_filename]
        async with ffprobe_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                logger.error("ffprobe timed out")
                await proc.kill()
                raise RuntimeError("ffprobe timed out")

            if proc.returncode != 0:
                logger.error(f"ffprobe failed with return code {proc.returncode}: {stderr.decode()}")
                raise RuntimeError(f"ffprobe failed with return code {proc.returncode}: {stderr.decode()}")

            try:
                return float(stdout.decode().strip())
            except ValueError:
                logger.error(f"ffprobe failed to parse duration: {stdout.decode()}")
                raise RuntimeError(f"ffprobe failed to parse duration: {stdout.decode()}")
    finally:
        if tmp_filename:
            try:
                os.unlink(tmp_filename)
            except FileNotFoundError:
                pass


async def process_url(client: httpx.AsyncClient, url: str):
    parsed_url = urlparse(url)
    if not parsed_url.scheme or not parsed_url.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")

    try:
        response = await client.get(url, follow_redirects=True, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Error fetching URL {url}: {str(e)}")

    try:
        duration = await get_media_duration(response.content)
    except Exception as e:
        logger.error(f"Error processing URL {url}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing URL {url}: {str(e)}")

    # Persist to cache using a dedicated session so concurrent processing
    # of multiple URLs does not share one AsyncSession (which is not safe).
    try:
        async with AsyncSession(engine) as session:
            session.add(DurationCache(url=url, duration=duration))
            await session.commit()
    except IntegrityError:
        # Another request/worker already cached this URL; ignore.
        logger.debug(f"Duration for {url} already cached, skipping insert")

    return {"url": url, "duration": duration}


@app.post("/duration")
async def get_duration(urls: list[str] = Body(..., embed=True)):
    if not urls:
        raise HTTPException(status_code=400, detail="urls must not be empty")

    try:
        async with httpx.AsyncClient() as client:
            async with AsyncSession(engine) as session:
                cached = await get_cached_durations(session, urls)
            cached_urls = {c["url"] for c in cached}
            uncached = [url for url in urls if url not in cached_urls]

            results = list(cached)
            errors = []

            if uncached:
                processed = await asyncio.gather(
                    *[process_url(client, url) for url in uncached],
                    return_exceptions=True,
                )
                for url, outcome in zip(uncached, processed):
                    if isinstance(outcome, Exception):
                        detail = outcome.detail if isinstance(outcome, HTTPException) else str(outcome)
                        errors.append({"url": url, "detail": detail})
                    else:
                        results.append(outcome)

            response = {"result": results}
            if errors:
                response["errors"] = errors
                response["message"] = "Some URLs failed to process"
            return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))