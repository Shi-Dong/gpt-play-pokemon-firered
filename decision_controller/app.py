"""Separate staging viewer and manual choice API. No inference is started."""

import hashlib
import threading
import uuid
from pathlib import Path
from typing import Any

import uvicorn
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from tap import Tap

from decision_controller.choices import choices
from decision_controller.executor import Bridge, Executor
from decision_controller.memory import Memory, ROM_MD5, atomic_json
from firered_bridge.mgba_client import mgba_save_state_file, mgba_screenshot


class Arguments(Tap):
    host: str = "127.0.0.1"
    port: int = 8788
    bridge_url: str = "http://127.0.0.1:8001"
    runtime: str = ".runtime"


class Selection(BaseModel):
    choice_id: str
    fingerprint: str


def create_app(bridge: Bridge, runtime: Path) -> FastAPI:
    runtime.mkdir(parents=True, exist_ok=True)
    memory = Memory(runtime / "memory.json")
    executor = Executor(bridge, memory)
    lock = threading.RLock()
    app = FastAPI(title="FireRed decision controller staging")

    @app.middleware("http")
    async def origin_check(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host')}"
        if request.method != "GET" and origin and origin != expected:
            return JSONResponse({"error": "Cross-origin control is forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (Path(__file__).parent / "viewer.html").read_text()

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        with lock:
            current = bridge.state()
            memory.observe(current)
            return {"model_connected": False, "autoplay": False, "fingerprint": current.fingerprint,
                    "state": current.model_state(memory.public()),
                    "options": [choice.public(index) for index, choice in enumerate(choices(current, memory.data))]}

    @app.post("/api/execute")
    def execute(selection: Selection) -> dict[str, Any]:
        with lock:
            try:
                result = executor.execute(selection.choice_id, selection.fingerprint)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            except (RuntimeError, requests.RequestException) as error:
                raise HTTPException(502, str(error)) from error
            if result.get("result") == "stale":
                raise HTTPException(409, result)
            return result

    @app.get("/screen.png")
    def screen() -> FileResponse:
        with lock:
            destination = runtime / "screen.new.png"
            result = mgba_screenshot(str(destination.resolve()))
            if not result.get("ok") or not destination.exists():
                raise HTTPException(503, "Emulator screenshot unavailable")
            stable = runtime / "screen.png"
            destination.replace(stable)
            return FileResponse(stable, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/checkpoint")
    def checkpoint() -> dict[str, Any]:
        with lock:
            current = bridge.state()
            if current.mode in {"boot", "locked", "battle"}:
                raise HTTPException(409, "Checkpoint at a stable menu, dialogue or overworld decision boundary")
            memory.observe(current)
            generation = uuid.uuid4().hex[:12]
            path = runtime / f"checkpoint-{generation}.ss0"
            result = mgba_save_state_file(str(path.resolve()))
            if not result.get("ok") or not path.exists():
                raise HTTPException(502, result)
            after = bridge.state()
            if after.fingerprint != current.fingerprint:
                path.unlink(missing_ok=True)
                raise HTTPException(409, "Game changed while saving; retry at an idle boundary")
            manifest = {"schema": 1, "rom_md5": ROM_MD5, "savestate": path.name,
                        "savestate_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "fingerprint": current.fingerprint, "memory": memory.data}
            atomic_json(runtime / f"checkpoint-{generation}.json", manifest)
            atomic_json(runtime / "latest-checkpoint.json", manifest)
            return {"ok": True, "checkpoint": path.name}

    return app


def main() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()
    app = create_app(Bridge(args.bridge_url), Path(args.runtime).resolve())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
