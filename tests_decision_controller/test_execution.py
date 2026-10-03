from pathlib import Path

from decision_controller.choices import choices, option_label
from decision_controller.executor import Executor
from decision_controller.memory import Memory
from tests_decision_controller.test_controller import FakeBridge, changed, state


def test_naming_finish_requires_observed_ok_cursor(tmp_path: Path) -> None:
    original = state(mode="namingScreen")
    original.raw["dialog"]["choiceMenu"] = {"text":"RED", "cursor":{"selected":"R"}}
    bridge = FakeBridge(original)
    executor = Executor(bridge, Memory(tmp_path / "memory.json"))
    result = executor.execute("naming:finish", original.fingerprint)
    assert result["result"] == "blocked"
    assert bridge.commands == ["start"]
    assert "naming:finish" in {choice.id for choice in choices(original)}


def test_naming_finish_and_menu_selected_flags(tmp_path: Path) -> None:
    original = state(mode="namingScreen")
    original.raw["dialog"]["choiceMenu"] = {"text":"RED", "cursor":{"selected":"R"}}
    ok = changed(original, dialog={"menuType":"namingScreen", "choiceMenu":{"text":"RED", "cursor":{"selected":"OK"}}})
    bridge = FakeBridge(original, [ok, state(mode="dialog")])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"))
    result = executor.execute("naming:finish", original.fingerprint)
    assert result["ok"] and bridge.commands == ["start", "a"]
    original = state(mode="startMenu")
    original.raw["dialog"]["startMenu"] = {"options":[{"name":"PARTY","selected":True},{"name":"BAG","selected":False}]}
    next_state = changed(original, dialog={"menuType":"startMenu", "startMenu":{"options":[{"name":"PARTY","selected":False},{"name":"BAG","selected":True}]}})
    bridge = FakeBridge(original, [next_state, state(mode="bagMenu")])
    executor = Executor(bridge, Memory(tmp_path / "other.json"))
    result = executor.execute("menu:startMenu:1", original.fingerprint)
    assert result["ok"] and bridge.commands == ["down", "a"]


def test_directional_stairs_label_and_unknown_exit_frontier() -> None:
    original = state([[0]*6,[0,1,1,30,0,0],[0,1,1,1,1,0],[0,None,None,None,None,0],[0]*6])
    original.raw["map"]["fullMap"]["warp_events"] = [
        {"position":[2,1],"destMapName":"UPSTAIRS"},
        {"position":[2,3],"destMapName":"OUTSIDE"}]
    offered = choices(original)
    stairs = next(choice for choice in offered if choice.id == "travel:3:1")
    assert "UPSTAIRS" in stairs.description
    approach = next(choice for choice in offered if choice.id == "approach:2:3")
    assert "OUTSIDE" in approach.description
    assert approach.target is not None
    assert original.raw["map"]["fullMap"]["minimap_data"]["grid"][approach.target[1]][approach.target[0]] is not None


def test_unknown_map_boundary_offers_named_exploration_without_idle_wait() -> None:
    original = state([[0, None, None, 0],[0,1,1,0],[0,0,0,0]])
    original.raw["map"]["connections"] = [{"direction":"up", "mapName":"ROUTE_1"}]
    offered = choices(original)
    approach = next(choice for choice in offered if choice.id == "approach-exit:up")
    assert "ROUTE_1" in approach.description and approach.kind == "travel"
    assert approach.target == (2,1)
    assert all(choice.id != "wait" for choice in offered)


def test_labels_extend_without_collisions() -> None:
    assert [option_label(index) for index in [0,25,26,27,51,52,701,702]] == [
        "A","Z","AA","AB","AZ","BA","ZZ","AAA"]
    assert len({option_label(index) for index in range(2000)}) == 2000


def test_large_menu_retains_and_executes_last_entry(tmp_path: Path) -> None:
    original = state(mode="startMenu")
    options = [f"ITEM_{index}" for index in range(35)]
    original.raw["dialog"]["startMenu"] = {"options": options, "cursorPosition":0}
    offered = choices(original)
    assert len(offered) == 37
    assert next(choice for choice in offered if choice.menu_index == 34).public(34)["label"] == "AI"
    updates = [changed(original, dialog={"menuType":"startMenu", "startMenu":{
        "options":options,"cursorPosition":index}}) for index in range(1,35)]
    updates.append(state(mode="dialog"))
    bridge = FakeBridge(original, updates)
    executor = Executor(bridge, Memory(tmp_path / "memory.json"))
    result = executor.execute("menu:startMenu:34",original.fingerprint)
    assert result["ok"] and bridge.commands == ["down"]*34 + ["a"]


def test_dialogue_retry_after_missed_short_tap(tmp_path: Path) -> None:
    original = state(mode="dialog")
    original.raw["dialog"].update(visibleText="First page", currentPage=1)
    advanced = changed(original, dialog={"menuType":"dialog","visibleText":"Second page","currentPage":2})
    bridge = FakeBridge(original,[original,advanced])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _:None)
    result = executor.execute("continue",original.fingerprint)
    assert result["ok"] and result["result"] == "advanced"
    assert bridge.commands == ["a",{"type":"hold","button":"A","frames":15}]


def test_dialogue_stops_at_new_choice_without_retry(tmp_path: Path) -> None:
    original = state(mode="dialog")
    choice = state(mode="yesNoMenu")
    bridge = FakeBridge(original,[choice])
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _:None)
    result = executor.execute("continue",original.fingerprint)
    assert result["ok"] and bridge.commands == ["a"]


def test_dialogue_reports_blocked_after_bounded_retry(tmp_path: Path) -> None:
    original = state(mode="dialog")
    bridge = FakeBridge(original)
    executor = Executor(bridge, Memory(tmp_path / "memory.json"), sleep=lambda _:None)
    result = executor.execute("continue",original.fingerprint)
    assert not result["ok"] and result["result"] == "blocked"
    assert result["commands_executed"] == 2


def test_all_connection_entrances_are_offered() -> None:
    original = state([[1]*40 for _ in range(3)])
    original.raw["map"]["connections"] = [{"direction":"up","mapName":"ROUTE_1"}]
    offered = choices(original)
    assert len([choice for choice in offered if choice.kind == "exit"]) == 40
