from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cameras import CameraManager, ConnectionAlreadyRunning, DisconnectionAlreadyRunning
from .config import ConfigRepository
from .models import AppSettings


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(repository: ConfigRepository | None = None) -> FastAPI:
    config_repository = repository or ConfigRepository()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loaded = config_repository.load()
        app.state.settings = loaded.settings
        app.state.config_error = loaded.error
        app.state.camera_manager = CameraManager(loaded.settings)
        if loaded.error:
            app.state.camera_manager.add_program_event("error", loaded.error)
        yield
        await app.state.camera_manager.close()

    app = FastAPI(title="Hikrobot — этап 3", version="3.0.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict:
        return {
            "settings": request.app.state.settings.model_dump(),
            "load_error": request.app.state.config_error,
        }

    @app.put("/api/settings")
    async def save_settings(settings: AppSettings, request: Request) -> dict:
        try:
            config_repository.save(settings)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Не удалось сохранить настройки: {exc}") from exc
        request.app.state.settings = settings
        request.app.state.config_error = None
        request.app.state.camera_manager.update_process_settings(settings)
        return {"settings": settings.model_dump()}

    @app.get("/api/state")
    async def get_state(request: Request) -> dict:
        return request.app.state.camera_manager.snapshot()

    @app.post("/api/cameras/connect")
    async def connect_cameras(settings: AppSettings, request: Request) -> dict:
        manager: CameraManager = request.app.state.camera_manager
        try:
            await manager.connect_all(settings)
        except ConnectionAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return manager.snapshot()

    @app.post("/api/cameras/disconnect")
    async def disconnect_cameras(request: Request) -> dict:
        manager: CameraManager = request.app.state.camera_manager
        try:
            await manager.disconnect_all()
        except DisconnectionAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return manager.snapshot()

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
