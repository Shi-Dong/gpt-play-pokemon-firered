from pathlib import Path

from decision_controller.choices import choices
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
