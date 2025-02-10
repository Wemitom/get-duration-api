import asyncio
import os
import tempfile
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import lifespan, DurationCache, engine
import logging

app = FastAPI(lifespan=lifespan)
logger = logging.getLogger(__name__)

async def get_cached_durations(session: AsyncSession, urls: list[str]):
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
            os.unlink(tmp_filename)

async def process_url(client: httpx.AsyncClient, session: AsyncSession, url: str):
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
    
    cache_entry = DurationCache(url=url, duration=duration)
    session.add(cache_entry)
    await session.commit()
    
    return {"url": url, "duration": duration}

@app.get("/duration")
async def get_duration(urls: str):
    try:
        async with httpx.AsyncClient() as client:
            urls_list = urls.split(",")
            async with AsyncSession(engine) as session:
                cached = await get_cached_durations(session, urls_list)
                uncached = [url for url in urls_list if url not in [c["url"] for c in cached]]

                uncached_durations = await asyncio.gather(*[process_url(client, session, url) for url in uncached], return_exceptions=True)
                for uncached_duration in uncached_durations:
                    if isinstance(uncached_duration, Exception):
                        if isinstance(uncached_duration, HTTPException):
                            detail = uncached_duration.detail
                        else:
                            detail = str(uncached_duration)
                        
                        return {
                            "result": cached,
                            "error": detail,
                            "message": "Some URLs failed to process"
                        }
                    
                cached.extend(uncached_durations)

                return {"result": cached}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))