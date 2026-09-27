from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import TunnelError
from .multi import MultiTunnelManager
from .discovery import discover

manager = MultiTunnelManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await manager.reconcile()
    yield
    await manager.close()


app = FastAPI(title="AutoTunnel", lifespan=lifespan)


@app.exception_handler(TunnelError)
async def tunnel_error(_, exc: TunnelError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


class CreateRequest(BaseModel):
    zone_id: str
    prefix: str
    port: int = Field(ge=1, le=65535)
    name: str = Field(default="", max_length=80)
    host_mode: str = "auto"


class UpdateRequest(BaseModel):
    name: str = Field(default="", max_length=80)
    prefix: str
    port: int = Field(ge=1, le=65535)
    host_mode: str = "auto"


@app.get("/api/status")
def status():
    return manager.status()


@app.get("/api/services")
def services():
    return discover()


@app.get("/api/zones")
async def zones():
    return await manager.zones()


@app.post("/api/login")
async def login():
    return await manager.begin_login()


@app.get("/api/login/{login_id}")
def login_status(login_id: str):
    return manager.login_status(login_id)


@app.get("/api/login/{login_id}/zones")
async def login_zones(login_id: str):
    return await manager.zones(login_id)


@app.get("/api/tunnels")
async def tunnels():
    return await manager.tunnels()


@app.post("/api/tunnels")
async def create_tunnel(request: CreateRequest):
    return await manager.create(request.zone_id, request.prefix, request.port, request.name, request.host_mode)


@app.put("/api/tunnels/{id}")
async def update_tunnel(id: str, request: UpdateRequest):
    return await manager.update(id, name=request.name, prefix=request.prefix, port=request.port, host_mode=request.host_mode)


@app.post("/api/tunnels/{id}/start")
async def start_tunnel(id: str):
    return await manager.start(id)


@app.post("/api/tunnels/{id}/stop")
async def stop_tunnel(id: str):
    return await manager.stop(id)


@app.delete("/api/tunnels/{id}")
async def delete_tunnel(id: str):
    await manager.delete(id)
    return {"deleted": True}


DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        return FileResponse(DIST / "index.html")
