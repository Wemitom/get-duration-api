from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import Column, String, Float, TIMESTAMP
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI

Base = declarative_base()

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_async_engine(DATABASE_URL)

class DurationCache(Base):
    __tablename__ = "duration_cache"

    url = Column(String, primary_key=True, index=True)
    duration = Column(Float)
    created_at = Column(type_=TIMESTAMP(timezone=True), default=datetime.now(tz=timezone.utc))

async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield