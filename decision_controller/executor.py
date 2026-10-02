"""Revalidate, execute, observe; never reset the adventure automatically."""

import time
from collections.abc import Callable
from typing import Any

import requests

from decision_controller.choices import Choice, choices, menu_commands, menu_cursor, menu_payload
from decision_controller.memory import Memory
from decision_controller.navigation import Navigator
from decision_controller.state import State, mapping


class Bridge:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.session = requests.Session()

    def state(self) -> State:
        response = self.session.get(f"{self.url}/requestData", timeout=45)
        response.raise_for_status()
        return State.from_bridge(response.json())

    def command(self, command: str) -> dict[str, Any]:
        response = self.session.post(f"{self.url}/sendCommands", json={"commands": [command]}, timeout=90)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok") or not payload.get("status"):
            raise RuntimeError(f"Bridge rejected {command}: {payload}")
        return payload


class Executor:
    def __init__(self, bridge: Bridge, memory: Memory, sleep: Callable[[float], None] = time.sleep) -> None:
        self.bridge = bridge
        self.memory = memory
        self.sleep = sleep

    def execute(self, choice_id: str, fingerprint: str) -> dict[str, Any]:
        before = self.bridge.state()
        if before.fingerprint != fingerprint:
            return {"ok": False, "result": "stale", "fingerprint": before.fingerprint}
        offered = {choice.id: choice for choice in choices(before, self.memory.data)}
        if choice_id not in offered:
            raise ValueError("Choice was not offered in this state")
        choice = offered[choice_id]
        try:
            after, result, count = self.run(before, choice)
        except (RuntimeError, requests.RequestException):
            self.memory.record(before, choice_id, before, "error")
            raise
        self.memory.record(before, choice_id, after, result)
        return {"ok": result not in {"blocked", "stale", "target_moved", "error"}, "result": result,
                "commands_executed": count, "fingerprint": after.fingerprint,
                "position": after.position, "mode": after.mode}

    def run(self, before: State, choice: Choice) -> tuple[State, str, int]:
        if choice.kind == "wait":
            self.sleep(0.25)
            return self.bridge.state(), "waited", 0
        if choice.kind == "menu":
            return self.select_menu(before, choice)
        if choice.kind in {"travel", "interact", "push", "exit"}:
            return self.travel(before, choice)
        current = before
        count = 0
        for command in choice.commands:
            self.bridge.command(command)
            count += 1
            current = self.bridge.state()
        if choice.kind == "step":
            if current.map_id != before.map_id or current.mode != before.mode:
                return current, "interrupted", count
            if current.position != choice.target:
                return current, "blocked", count
        return current, "executed", count

    def select_menu(self, before: State, choice: Choice) -> tuple[State, str, int]:
        current = before
        original_options = menu_payload(before).get("options")
        count = 0
        for _ in range(24):
            menu = menu_payload(current)
            if current.mode != before.mode or menu.get("options") != original_options:
                return current, "interrupted", count
            index = choice.menu_index
            if index is None:
                raise ValueError("Missing menu index")
            if menu_cursor(menu) == index:
                self.bridge.command("a")
                after = self.bridge.state()
                return after, "selected" if after.fingerprint != current.fingerprint else "blocked", count + 1
            commands = menu_commands(menu, index)
            old_cursor = menu_cursor(menu)
            self.bridge.command(commands[0])
            count += 1
            current = self.bridge.state()
            if current.mode == before.mode and menu_cursor(menu_payload(current)) == old_cursor:
                return current, "blocked", count
        return current, "blocked", count

    def travel(self, before: State, choice: Choice) -> tuple[State, str, int]:
        current = before
        count = 0
        if choice.target is None:
            raise ValueError("Missing route target")
        for _ in range(80):
            if current.map_id != before.map_id or current.mode != "overworld":
                return current, "interrupted", count
            nav = Navigator(current)
            paths = nav.routes()
            direction = None
            if choice.kind in {"interact", "push"}:
                npcs = mapping(mapping(current.raw.get("map")).get("fullMap")).get("npcs", [])
                if choice.target_id is not None and not any(npc.get("localId") == choice.target_id and tuple(npc.get("position", [])) == choice.target for npc in npcs):
                    return current, "target_moved", count
                result = nav.interaction_route(choice.target, paths)
                if result is None:
                    return current, "blocked", count
                route, direction = result
            else:
                route = paths.get(choice.target)
                if route is None:
                    return current, "blocked", count
            if not route:
                if choice.kind == "exit":
                    self.bridge.command(choice.commands[0])
                    after = self.bridge.state()
                    return after, "interrupted" if after.map_id != before.map_id or after.mode != "overworld" else "blocked", count + 1
                if direction:
                    if choice.kind == "push":
                        self.bridge.command(direction)
                        count += 1
                        after = self.bridge.state()
                        if after.mode != "overworld" or after.map_id != current.map_id:
                            return after, "interrupted", count
                        return after, "arrived" if after.position == choice.target else "blocked", count
                    else:
                        self.bridge.command(f"face_{direction}")
                        count += 1
                        faced = self.bridge.state()
                        if faced.mode != "overworld" or faced.map_id != current.map_id or faced.position != current.position:
                            return faced, "interrupted", count
                        self.bridge.command("a")
                        count += 1
                        after = self.bridge.state()
                        return after, "arrived" if after.fingerprint != faced.fingerprint else "blocked", count
                return self.bridge.state(), "arrived", count
            edge = route[0]
            old_position = current.position
            self.bridge.command(edge.command)
            count += 1
            current = self.bridge.state()
            if current.map_id != before.map_id or current.mode != "overworld":
                return current, "interrupted", count
            if current.position != edge.destination:
                # Stop after a collision or forced spinner motion; never keep
                # executing a route whose coordinate assumptions are obsolete.
                return current, "blocked" if current.position == old_position else "interrupted", count
        return current, "bounded", count
