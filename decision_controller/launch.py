"""Launch isolated emulator, upstream bridge and staging viewer together."""

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from tap import Tap

from decision_controller.memory import ROM_MD5, atomic_json


class Arguments(Tap):
    rom: str
    emulator: str
    host: str = "127.0.0.1"
    port: int = 8788
    bridge_port: int = 8001
    socket_port: int = 8898
    runtime: str = ".runtime"
    resume: bool = False


def main() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()
    source = Path(args.rom).resolve()
    if hashlib.md5(source.read_bytes()).hexdigest() != ROM_MD5:
        raise ValueError("ROM checksum does not match FireRed USA v1.0")
    runtime = Path(args.runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    for host, port in ((args.host, args.port), ("127.0.0.1", args.bridge_port), ("127.0.0.1", args.socket_port)):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    resume_args = []
    if args.resume:
        manifest = json.loads((runtime / "latest-checkpoint.json").read_text())
        saved = (runtime / manifest["savestate"]).resolve()
        memory = manifest.get("memory", {})
        if saved.parent != runtime or manifest.get("schema") != 1 or memory.get("schema") != 1 or memory.get("rom_md5") != ROM_MD5:
            raise ValueError("Checkpoint path/schema/memory mismatch")
        if manifest.get("rom_md5") != ROM_MD5 or hashlib.sha256(saved.read_bytes()).hexdigest() != manifest["savestate_sha256"]:
            raise ValueError("Checkpoint ROM/hash mismatch")
        atomic_json(runtime / "memory.json", manifest["memory"])
        resume_args = ["-t", str(saved)]
    elif (runtime / "memory.json").exists():
        raise ValueError("Existing adventure memory: use --resume with a checkpoint or a different --runtime")
    game = runtime / "pokemon_firered.gba"
    if not game.exists():
        shutil.copyfile(source, game)
    if hashlib.md5(game.read_bytes()).hexdigest() != ROM_MD5:
        raise ValueError("Runtime ROM checksum mismatch")
    root = Path(__file__).resolve().parents[1]
    lua = (root / "mgba/scripts/FireRedBridgeSocketServer.lua").read_text()
    lua = lua.replace("local port = 8888", f"local port = {args.socket_port}")
    lua = lua.replace("socket.bind(nil, port)", 'socket.bind("127.0.0.1", port)')
    lua = lua.replace("port = port + 1", 'error("Configured FireRed socket port is already in use")')
    (runtime / "bridge.lua").write_text(lua)
    env = dict(os.environ, MGBA_TRANSPORT="socket", MGBA_SOCKET_HOST="127.0.0.1",
               MGBA_SOCKET_PORT=str(args.socket_port), MGBA_SOCKET_PORT_MAX=str(args.socket_port),
               FIRERED_MINIMAPS_DIR=str(runtime / "minimaps"), FIRERED_DIALOG_CACHE="0", QT_OPENGL="software")
    env["FIRERED_GAME_DATA_DIR"] = str(root / "game_data_firered")
    processes: list[subprocess.Popen[bytes]] = []

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        processes.append(subprocess.Popen(["xvfb-run", "-a", args.emulator,
                                           "-C", "audioSync=0", "-C", "videoSync=1", "-C", "fpsTarget=60",
                                           "-C", "pauseOnFocusLost=0", "--script", str(runtime / "bridge.lua"),
                                           *resume_args, str(game)], env=env, start_new_session=True))
        processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "firered_mgba_bridge:app", "--host", "127.0.0.1", "--port", str(args.bridge_port)], cwd=root, env=env, start_new_session=True))
        for _ in range(120):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("Emulator or bridge exited during startup")
            try:
                response = requests.get(f"http://127.0.0.1:{args.bridge_port}/requestData", timeout=3)
                if response.ok and response.json().get("ok"):
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("FireRed bridge did not become ready")
        processes.append(subprocess.Popen([sys.executable, "-m", "decision_controller.app", "--host", args.host,
                                           "--port", str(args.port), "--bridge-url", f"http://127.0.0.1:{args.bridge_port}",
                                           "--runtime", str(runtime)], cwd=root, env=env, start_new_session=True))
        print(f"FireRed staging viewer: http://{args.host}:{args.port}; model disconnected", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        raise RuntimeError("A FireRed staging process exited; stopping its siblings")
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)


if __name__ == "__main__":
    main()
