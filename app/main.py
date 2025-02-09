from fastapi import FastAPI, HTTPException
import subprocess
import httpx
import os
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import lifespan, DurationCache, engine

app = FastAPI(lifespan=lifespan)

@app.get("/duration")
async def get_duration(urls: str):
    try:
        async with httpx.AsyncClient() as client:
            urls_list = urls.split(",")
            result_list = list[dict[str, float]]()
            for url in urls_list:
                async with AsyncSession(engine) as session:
                    duration_cache = await session.get(DurationCache, url)
                    if duration_cache:
                        result_list.append({"url": url, "duration": duration_cache.duration})
                        continue

                response = await client.get(url)
                response.raise_for_status()
                
                if response.status_code == 200:
                    temp_file = "temp_media"
                    with open(temp_file, "wb") as f:
                        f.write(response.content)
                    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", temp_file]
                    cmd_result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
                    os.remove(temp_file)

                    duration = float(cmd_result.stdout.strip())

                    result_list.append({"url": url, "duration": duration})

                    async with AsyncSession(engine) as session:
                        duration_cache = DurationCache(url=url, duration=duration)
                        session.add(duration_cache)
                        await session.commit()
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)

            return result_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))