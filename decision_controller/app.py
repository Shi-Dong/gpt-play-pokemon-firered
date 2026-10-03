"""Separate live viewer, guarded choice API and autonomous controller controls."""

import hashlib
import json
import threading
import time
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
    automatic: bool = False


class Control(BaseModel):
    enabled: bool


def create_app(bridge: Bridge, runtime: Path) -> FastAPI:
    runtime.mkdir(parents=True, exist_ok=True)
    memory = Memory(runtime / "memory.json")
    executor = Executor(bridge, memory)
    lock = threading.RLock()
    screenshot_lock = threading.Lock()
    app = FastAPI(title="FireRed decision controller staging")

    def controller_status() -> dict[str, Any]:
        try:
            status = json.loads((runtime / "controller.json").read_text())
            control = json.loads((runtime / "control.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"model_connected": False, "autoplay": False, "phase": "manual"}
        status["autoplay"] = control.get("enabled", False)
        status["heartbeat_age_seconds"] = round(time.time() - status.get("updated_at", 0), 1)
        return status

    @app.get("/api/controller")
    def controller() -> dict[str, Any]:
        return controller_status()

    @app.post("/api/control")
    def control(body: Control) -> dict[str, Any]:
        atomic_json(runtime / "control.json", {"enabled": body.enabled, "revision": time.time_ns()})
        return {"ok": True, "enabled": body.enabled}

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
            status = controller_status()
            return {"model_connected": status["model_connected"], "autoplay": status["autoplay"], "fingerprint": current.fingerprint,
                    "state": current.model_state(memory.public()),
                    "options": [choice.public(index) for index, choice in enumerate(choices(current, memory.data))]}

    @app.post("/api/execute")
    def execute(selection: Selection) -> dict[str, Any]:
        with lock:
            if controller_status().get("autoplay") and not selection.automatic:
                raise HTTPException(409, "Pause autonomous play before manual input")
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
        # Screenshot requests have no game input. Separate locking keeps frames
        # visible while a bounded local route is executing.
        with screenshot_lock:
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
            # Bound storage growth without overwriting a valid checkpoint.
            manifests = sorted(runtime.glob("checkpoint-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
            for old in manifests[20:]:
                old.with_suffix(".ss0").unlink(missing_ok=True)
                old.unlink(missing_ok=True)
            return {"ok": True, "checkpoint": path.name}

    return app


def main() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()
    app = create_app(Bridge(args.bridge_url), Path(args.runtime).resolve())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
