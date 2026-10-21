import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from . import activity, dashboard, jobs, schema, storage
from .age.analyzer import shutdown as age_shutdown
from .db import open_db
from . import auth
from .routers.api import public, router
from .routers.auth import router as auth_router
from .routers.users import router as users_router
from .routers.settings import router as settings_router
from .routers.admin import router as admin_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    with open_db() as db:
        schema.ensure(db)  # creates the tables (in their own schema) on first start, adds new columns later
        auth.bootstrap_admin(db)
        auth.purge_expired(db)
        activity.backfill(db)
    storage.purge_cache()
    jobs.start()
    # import torch and probe the services now, so the first dashboard load isn't the one that pays for it
    threading.Thread(target=dashboard.engines, daemon=True).start()
    yield
    jobs.stop()
    age_shutdown()


app = FastAPI(title="PIVC Video Analysis API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(public)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(router)
app.include_router(settings_router)
app.include_router(admin_router)
