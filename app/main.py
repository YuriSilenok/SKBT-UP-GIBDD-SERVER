from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.devices import router as devices_router
from app.api.employee import router as employee_router
from app.api.messages import router as messages_router
from app.api.realtime import router as realtime_router
from app.api.privacy import router as privacy_router
import asyncio
from app.core.config import get_settings
from app.db.database import close_database, init_database


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database(get_settings())

    async def cleanup_loop():
        from app.services.privacy import cleanup_personal_data
        from app.db.database import database_proxy
        import logging
        def cleanup():
            try:
                cleanup_personal_data()
            finally:
                close_database()
        while True:
            try:
                await asyncio.to_thread(cleanup)
            except Exception:
                logging.getLogger(__name__).exception("Privacy cleanup failed")
            await asyncio.sleep(60)

    cleanup_task = asyncio.create_task(cleanup_loop())

    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        close_database()


def create_app(enable_lifespan: bool = True) -> FastAPI:
    app = FastAPI(
        title=get_settings().app_name,
        lifespan=lifespan if enable_lifespan else None,
    )
    from app.core.security import SecurityMiddleware
    app.add_middleware(SecurityMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(devices_router)
    app.include_router(employee_router)
    app.include_router(messages_router)
    app.include_router(realtime_router)
    app.include_router(privacy_router)
    return app


app = create_app()
