from pathlib import Path
from typing import Any

from decision_controller.autoplay import Arguments, Runner, read_json
from decision_controller.memory import atomic_json


class Response:
    status_code = 200
    ok = True
    def json(self) -> dict[str, Any]:
        return {"fingerprint":"before", "state":{"memory":{"game_complete":False}},
                "options":[{"label":"A", "id":"continue", "description":"Continue"}]}
    def raise_for_status(self) -> None:
        pass


class Session:
    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []
    def get(self, url: str, **kwargs: Any) -> Response:
        return Response()
    def post(self, url: str, **kwargs: Any) -> Response:
        self.posts.append((url, kwargs))
        response = Response()
        response.status_code = 409
        response.ok = False
        return response


def runner_at(path: Path) -> Runner:
    args = Arguments(underscores_to_dashes=True).parse_args([
        "--viewer-url", "http://viewer", "--endpoint", "http://model", "--runtime", str(path), "--autoplay"])
    runner = Runner(args)
    runner.session = Session()
    return runner


def decision() -> dict[str, Any]:
    return {"choice_id":"continue", "description":"Continue", "probabilities":{"A":1},
            "raw_total":1, "latency_ms":1, "at":1, "usage":{}}


def test_pause_during_inference_discards_input(tmp_path: Path) -> None:
    runner = runner_at(tmp_path)
    class Model:
        def decide(self, *args: Any) -> dict[str, Any]:
            atomic_json(runner.control, {"enabled":False, "revision":2})
            return decision()
    runner.model = Model()
    runner.step()
    assert not runner.session.posts
    assert runner.status["phase"] == "discarded"


def test_stale_response_reobserves_without_checkpoint_or_reset(tmp_path: Path) -> None:
    runner = runner_at(tmp_path)
    class Model:
        def decide(self, *args: Any) -> dict[str, Any]:
            return decision()
    runner.model = Model()
    runner.step()
    assert len(runner.session.posts) == 1
    url, arguments = runner.session.posts[0]
    assert url.endswith("/api/execute")
    assert arguments["json"]["fingerprint"] == "before"
    assert runner.status["phase"] == "discarded"


def test_unchanged_action_pauses_without_reset(tmp_path: Path) -> None:
    runner = runner_at(tmp_path)
    class Model:
        def decide(self, *args: Any) -> dict[str, Any]:
            return decision()
    runner.model = Model()
    for _ in range(13):
        runner.step()
    assert not read_json(runner.control)["enabled"]
    assert runner.status["phase"] == "stuck"
    assert all(url.endswith("/api/execute") for url, _ in runner.session.posts)
