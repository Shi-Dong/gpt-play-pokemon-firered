import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decision_controller.app import create_app
from decision_controller.choices import Choice, choices, menu_commands, menu_payload
from decision_controller.executor import Executor
from decision_controller.memory import Memory, ROM_MD5
from decision_controller.navigation import Navigator
from decision_controller.state import State
from firered_mgba_bridge import SendCommandsBody


def state(grid: list[list[int | None]] | None = None, mode: str | None = None) -> State:
    return State({"gameVersion": "FIRERED", "player": {"position": [1, 1], "badges": {}, "surfing": False},
                  "map": {"group": 1, "number": 0, "name": "TEST", "fullMap": {"minimap_data": {"grid": grid or [[0]*5, [0, 1, 1, 1, 0], [0]*5]}}},
                  "emulator": {}, "dialog": {"menuType": mode}, "importantEvents": {}, "bag": {}, "party": [], "battle": {}})


class FakeBridge:
    def __init__(self, initial: State, updates: list[State] | None = None) -> None:
        self.current = initial
        self.updates = list(updates or [])
        self.commands: list[str] = []

    def state(self) -> State:
        return self.current

    def command(self, command: str) -> dict[str, bool]:
        # Validate the generated payload with the real downstream bridge schema.
        assert SendCommandsBody.model_validate({"commands": [command]}).commands == [command]
        self.commands.append(command)
        if self.updates:
            self.current = self.updates.pop(0)
        return {"ok": True, "status": True}


def changed(original: State, **updates: object) -> State:
    raw = copy.deepcopy(original.raw)
    raw.update(updates)
    return State(raw)


def test_actual_bridge_envelope() -> None:
    original = state()
    assert State.from_bridge({"ok": True, "data": original.raw}) == original
    with pytest.raises(ValueError):
        State.from_bridge({"ok": False})


def test_boot_is_not_treated_as_walkable_overworld() -> None:
    original = state()
    original.raw["map"]["fullMap"]["minimap_data"]["grid"] = []
    assert original.mode == "boot"
    assert all(option.kind != "travel" for option in choices(original))
    assert "menu:start" in {option.id for option in choices(original)}


def test_navigation_walls_unknown_and_water() -> None:
    original = state([[0,0,0,0,0], [0,1,3,1,0], [0,1,None,1,0], [0,1,1,1,0], [0,0,0,0,0]])
    paths = Navigator(original).routes()
    assert (2,1) not in paths and (2,2) not in paths
    assert len(paths[(3,1)]) == 6
    surf = changed(original, player={**original.player, "surfing": True})
    assert len(Navigator(surf).routes()[(3,1)]) == 2


def test_ledge_direction_and_two_tile_landing() -> None:
    original = state([[0]*5, [0,1,5,1,0], [0]*5])
    edges = list(Navigator(original).edges(original.position))
    assert [(edge.command, edge.destination) for edge in edges] == [("right", (3,1))]
    reverse = changed(original, player={**original.player, "position": [3,1]})
    assert list(Navigator(reverse).edges(reverse.position)) == []


def test_directional_collision_and_warp_stop() -> None:
    original = state([[0]*6, [0,1,70,1,1,0], [0]*6])
    assert (3,1) not in Navigator(original).routes()
    portal = state([[0]*6, [0,1,9,1,1,0], [0]*6])
    paths = Navigator(portal).routes()
    assert (2,1) in paths and (3,1) not in paths


def test_zero_pp_and_trainer_run_filtered() -> None:
    original = state(mode="battleMoves")
    original.raw["dialog"]["battleUi"] = {"options": ["TACKLE", "GROWL", "—", "—"], "pp": [0,5,0,0], "cursorPosition": 0, "layout": "grid2x2"}
    offered = choices(original)
    assert "menu:battleMoves:1" in {option.id for option in offered}
    assert "menu:battleMoves:0" not in {option.id for option in offered}
    original.raw["dialog"] = {"menuType": "battleActions", "battleUi": {"options": ["FIGHT", "BAG", "POKéMON", "RUN"], "cursorPosition": 0, "layout": "grid2x2"}}
    original.raw["battle"] = {"isTrainerBattle": True}
    assert all(option.description != "Choose RUN" for option in choices(original))


def test_menu_grid_navigation() -> None:
    assert menu_commands({"cursorPosition": 0, "layout": "grid2x2"}, 3) == ("right", "down")
    assert menu_commands({"cursorPosition": 3, "layout": "grid2x2"}, 0) == ("left", "up")


def test_map_connection_has_bounded_executable_exit(tmp_path: Path) -> None:
    original = state([[0,0,0], [0,1,1], [0,0,0]])
    original.raw["map"]["connections"] = [{"direction": "right", "mapName": "ROUTE_1", "offset": 0}]
    offered = choices(original)
    exit_choice = next(option for option in offered if option.kind == "exit")
    edge = changed(original, player={**original.player, "position": [2,1]})
    destination = changed(edge, map={**edge.raw["map"], "number": 1})
    bridge = FakeBridge(original, [edge, destination])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _: None)
    after, result, count = executor.run(original, exit_choice)
    assert after.map_id != original.map_id and result == "interrupted" and count == 2
    assert bridge.commands == ["right", "right"]


def test_moving_npc_aborts_before_interaction(tmp_path: Path) -> None:
    original = state([[0]*5, [0,1,1,10,0], [0]*5])
    original.raw["map"]["fullMap"]["npcs"] = [{"localId": 7, "position": [3,1], "type": "NPC"}]
    moved = changed(original, player={**original.player, "position": [2,1]})
    moved.raw["map"]["fullMap"]["npcs"][0]["position"] = [3,2]
    bridge = FakeBridge(original, [moved])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _: None)
    choice = next(option for option in choices(original) if option.target_id == 7)
    after, result, count = executor.run(original, choice)
    assert result == "target_moved" and count == 1 and bridge.commands == ["right"]


def test_choice_count_and_public_labels_are_bounded() -> None:
    original = state([[1]*40 for _ in range(10)])
    original.raw["map"]["fullMap"]["minimap_data"]["grid"][2] = [None]*40
    offered = choices(original)
    assert len(offered) <= 26
    assert [choice.public(index)["label"] for index, choice in enumerate(offered)] == list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:len(offered)])


def test_stale_and_unknown_choices_do_not_press_buttons(tmp_path: Path) -> None:
    original = state()
    bridge = FakeBridge(original)
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _: None)
    assert executor.execute("step:right", "wrong")["result"] == "stale"
    with pytest.raises(ValueError):
        executor.execute("reset", original.fingerprint)
    assert bridge.commands == []


def test_route_interrupts_before_remaining_inputs(tmp_path: Path) -> None:
    original = state()
    battle = changed(original, dialog={"menuType": "battleActions"}, player={**original.player, "position": [2,1]})
    bridge = FakeBridge(original, [battle])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _: None)
    after, result, count = executor.run(original, Choice("test", "walk", "travel", target=(3,1)))
    assert result == "interrupted" and count == 1 and after.mode == "battleActions"
    assert bridge.commands == ["right"]


def test_collision_does_not_retry_or_reset(tmp_path: Path) -> None:
    original = state()
    bridge = FakeBridge(original)
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _: None)
    result = executor.execute("step:right", original.fingerprint)
    assert result["result"] == "blocked" and bridge.commands == ["right"]
    assert executor.memory.data["failures"][-1]["result"] == "blocked"


def test_menu_confirmation_only_after_observed_cursor(tmp_path: Path) -> None:
    original = state(mode="yesNo")
    original.raw["dialog"]["choiceMenu"] = {"options": ["YES", "NO"], "cursorPosition": 0}
    next_state = copy.deepcopy(original)
    next_state.raw["dialog"]["choiceMenu"]["cursorPosition"] = 1
    bridge = FakeBridge(original, [next_state, state()])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _: None)
    executor.execute("menu:yesNo:1", original.fingerprint)
    assert bridge.commands == ["down", "a"]


def test_story_progress_requires_evidence_and_survives_restart(tmp_path: Path) -> None:
    memory = Memory(tmp_path / "memory.json")
    original = state()
    memory.observe(original)
    assert memory.public()["next_goal"]["id"] == "starter"
    assert not memory.public()["game_complete"]
    original.raw["importantEvents"] = {"EVENT_GOT_POKEDEX": True}
    original.raw["player"]["badges"] = {"BOULDER": True, "CASCADE": False}
    original.raw["bag"] = {"Key Items": [{"name": "S.S. TICKET", "quantity": 1}, {"name": "HM01", "quantity": 0}]}
    memory.observe(original)
    restored = Memory(memory.path)
    assert {"pokedex", "parcel", "badge_BOULDER", "ticket"} <= restored.data["milestones"].keys()
    assert "cut" not in restored.data["milestones"] and "badge_CASCADE" not in restored.data["milestones"]
    assert not restored.public()["game_complete"]
    original.raw["importantEvents"] = {"EVENT_HALL_OF_FAME": True}
    restored.observe(original)
    assert restored.public()["game_complete"]


def test_memory_rejects_wrong_rom(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    path.write_text(json.dumps({"schema": 1, "rom_md5": "wrong"}))
    with pytest.raises(ValueError):
        Memory(path)


def test_staging_api_and_cross_origin_control(tmp_path: Path) -> None:
    original = state()
    bridge = FakeBridge(original)
    with TestClient(create_app(bridge, tmp_path)) as client:
        payload = client.get("/api/state").json()
        assert payload["model_connected"] is False and payload["autoplay"] is False
        response = client.post("/api/execute", json={"choice_id": "wait", "fingerprint": payload["fingerprint"]}, headers={"Origin": "https://unrelated.example"})
        assert response.status_code == 403 and bridge.commands == []
        response = client.post("/api/execute", json={"choice_id": "wait", "fingerprint": "wrong"})
        assert response.status_code == 409
        assert ROM_MD5 not in client.get("/").text


def test_captured_native_fixture() -> None:
    path = Path(__file__).parent / "fixtures" / "native-state.json"
    if not path.exists():
        pytest.skip("Capture the staging emulator's native state before this smoke test")
    native = State.from_bridge(json.loads(path.read_text()))
    offered = choices(native)
    assert 1 <= len(offered) <= 26
    for option in offered:
        assert isinstance(option.description, str) and option.id
        SendCommandsBody.model_validate({"commands": list(option.commands)})
