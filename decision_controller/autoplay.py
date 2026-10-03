"""Persistent, reset-free model loop against the guarded local controller API."""

import json
import signal
import threading
import time
from pathlib import Path
from typing import Any

import requests
from tap import Tap

from decision_controller.memory import atomic_json
from decision_controller.model import Model


class Arguments(Tap):
    viewer_url: str
    endpoint: str
    model: str = "jev-qwen36-step384"
    runtime: str = ".runtime"
    timeout: float = 60
    autoplay: bool = False


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class Runner:
    def __init__(self, args: Arguments) -> None:
        self.url = args.viewer_url.rstrip("/")
        self.runtime = Path(args.runtime).resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.control = self.runtime / "control.json"
        self.status_path = self.runtime / "controller.json"
        self.stop = threading.Event()
        self.model = Model(args.endpoint, args.model, self.runtime / "model-calls.jsonl", args.timeout)
        self.session = requests.Session()
        previous = read_json(self.status_path)
        same_model = previous.get("endpoint") == args.endpoint and previous.get("model") == args.model
        self.status: dict[str, Any] = {"model_connected": False, "endpoint": args.endpoint,
                                     "model": args.model, "phase": "starting",
                                     "calls": previous.get("calls", 0) if same_model else 0,
                                     "decisions": previous.get("decisions", 0) if same_model else 0}
        self.failures = 0
        self.repeats = 0
        self.previous: tuple[str, str] | None = None
        # Explicit startup flag controls this run; persisted pause stays until
        # an explicit --autoplay launch or browser Resume instruction.
        atomic_json(self.control, {"enabled": args.autoplay, "revision": time.time_ns()})

    def publish(self, **fields: Any) -> None:
        self.status.update(fields, updated_at=time.time())
        atomic_json(self.status_path, self.status)

    def checkpoint(self) -> None:
        response = self.session.post(self.url + "/api/checkpoint", timeout=20)
        if response.ok:
            self.publish(checkpoint=response.json()["checkpoint"])

    def step(self) -> None:
        control = read_json(self.control)
        if not control.get("enabled"):
            self.previous = None
            self.repeats = 0
            self.publish(autoplay=False, phase="paused")
            self.stop.wait(0.5)
            return
        response = self.session.get(self.url + "/api/state", timeout=45)
        response.raise_for_status()
        snapshot = response.json()
        if snapshot["state"]["memory"]["game_complete"]:
            self.checkpoint()
            atomic_json(self.control, {"enabled": False, "revision": time.time_ns()})
            self.publish(autoplay=False, phase="complete", game_complete=True)
            return
        self.publish(autoplay=True, phase="calling", call_started_at=time.time(), error=None)
        call_started = time.perf_counter()
        try:
            decision = self.model.decide(snapshot["state"], snapshot["options"])
        finally:
            self.publish(calls=self.status["calls"] + 1, latest_latency_ms=round((time.perf_counter()-call_started)*1000, 3),
                         call_started_at=None)
        self.publish(model_connected=True, latest_latency_ms=decision["latency_ms"], latest_decision={
            key: decision[key] for key in ("choice_id", "description", "probabilities", "raw_total", "latency_ms", "at", "usage")})
        # A pause during inference cancels its eventual input, even after Resume.
        if self.stop.is_set() or read_json(self.control) != control:
            self.publish(phase="discarded", result="Control changed during inference")
            return
        key = (snapshot["fingerprint"], decision["choice_id"])
        self.repeats = self.repeats + 1 if key == self.previous else 0
        self.previous = key
        if self.repeats >= 12:
            atomic_json(self.control, {"enabled": False, "revision": time.time_ns()})
            self.publish(autoplay=False, phase="stuck", error="Repeated unchanged state/action; paused without resetting")
            return
        self.publish(phase="executing")
        response = self.session.post(self.url + "/api/execute", json={"choice_id": decision["choice_id"],
            "fingerprint": snapshot["fingerprint"], "automatic": True}, timeout=180)
        if response.status_code == 409:
            self.publish(phase="discarded", result="State changed during inference; re-observing")
            return
        response.raise_for_status()
        self.publish(phase="observing", result=response.json(), decisions=self.status["decisions"] + 1)
        self.checkpoint()
        self.failures = 0

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.step()
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
                self.failures += 1
                self.publish(phase="error", error=str(error), consecutive_errors=self.failures, call_started_at=None)
                if self.failures >= 3:
                    atomic_json(self.control, {"enabled": False, "revision": time.time_ns()})
                    self.publish(autoplay=False, phase="paused", error=f"Three consecutive errors: {error}")
                    self.failures = 0
                self.stop.wait(min(5, self.failures + 1))
        try:
            self.checkpoint()
        except requests.RequestException:
            pass
        self.publish(autoplay=False, phase="stopped")


def main() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()
    runner = Runner(args)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: runner.stop.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: runner.stop.set())
    runner.run()


if __name__ == "__main__":
    main()
