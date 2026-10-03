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
    assert runner.cycle_history == []


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


def test_alternating_maps_are_detected_despite_changing_fingerprint(tmp_path: Path) -> None:
    runner = runner_at(tmp_path)
    class SequenceSession(Session):
        def __init__(self) -> None:
            super().__init__()
            self.index = 0
        def get(self, url: str, **kwargs: Any) -> Response:
            index = self.index
            class Snapshot(Response):
                def json(self) -> dict[str, Any]:
                    payload = super().json()
                    payload["fingerprint"] = str(index)
                    payload["state"].update(location={"id":str(index % 2), "position":[6,8]}, mode="overworld")
                    return payload
            return Snapshot()
        def post(self, url: str, **kwargs: Any) -> Response:
            response = super().post(url, **kwargs)
            if url.endswith("/api/execute"):
                self.index += 1
                response.status_code = 200
                response.ok = True
            return response
    class Model:
        def decide(self, *args: Any) -> dict[str, Any]:
            return decision()
    runner.session = SequenceSession()
    runner.model = Model()
    for _ in range(7):
        runner.step()
    assert not read_json(runner.control)["enabled"]
    assert runner.status["phase"] == "stuck"
    assert "cycle" in runner.status["error"]
    assert len([url for url, _ in runner.session.posts if url.endswith("/api/execute")]) == 6


def test_changing_battle_state_is_progress_not_cycle(tmp_path: Path) -> None:
    runner = runner_at(tmp_path)
    class BattleSession(Session):
        def __init__(self) -> None:
            super().__init__()
            self.hp = 100
        def get(self, url: str, **kwargs: Any) -> Response:
            hp = self.hp
            class Snapshot(Response):
                def json(self) -> dict[str, Any]:
                    payload = super().json()
                    payload["fingerprint"] = str(hp)
                    payload["state"]["battle"] = {"enemy_hp":hp}
                    return payload
            return Snapshot()
        def post(self, url: str, **kwargs: Any) -> Response:
            response = super().post(url, **kwargs)
            if url.endswith("/api/execute"):
                self.hp -= 1
                response.status_code = 200
                response.ok = True
            return response
    class Model:
        def decide(self, *args: Any) -> dict[str, Any]:
            return decision()
    runner.session = BattleSession()
    runner.model = Model()
    for _ in range(8):
        runner.step()
    assert read_json(runner.control)["enabled"]
    assert runner.status["decisions"] == 8


def test_new_dialogue_pages_are_progress_not_cycle(tmp_path: Path) -> None:
    runner = runner_at(tmp_path)
    class DialogueSession(Session):
        def __init__(self) -> None:
            super().__init__()
            self.page = 1
        def get(self, url: str, **kwargs: Any) -> Response:
            page = self.page
            class Snapshot(Response):
                def json(self) -> dict[str, Any]:
                    payload = super().json()
                    payload["fingerprint"] = str(page)
                    payload["state"]["dialog"] = {"currentPage":page, "visibleText":f"Page {page}"}
                    return payload
            return Snapshot()
        def post(self, url: str, **kwargs: Any) -> Response:
            response = super().post(url, **kwargs)
            if url.endswith("/api/execute"):
                self.page += 1
                response.status_code = 200
                response.ok = True
            return response
    class Model:
        def decide(self, *args: Any) -> dict[str, Any]:
            return decision()
    runner.session = DialogueSession()
    runner.model = Model()
    for _ in range(8):
        runner.step()
    assert read_json(runner.control)["enabled"]
    assert runner.status["decisions"] == 8
