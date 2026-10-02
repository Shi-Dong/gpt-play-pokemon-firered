"""Normalize the upstream bridge without inventing unreadable game facts."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class State:
    raw: dict[str, Any]

    @classmethod
    def from_bridge(cls, payload: dict[str, Any]) -> "State":
        raw = payload.get("data", payload.get("state", payload))
        if not isinstance(raw, dict) or raw.get("gameVersion") != "FIRERED":
            raise ValueError("Expected the FireRed bridge's gameVersion/state object")
        return cls(raw)

    @property
    def player(self) -> dict[str, Any]:
        return mapping(self.raw.get("player"))

    @property
    def dialog(self) -> dict[str, Any]:
        return mapping(self.raw.get("dialog"))

    @property
    def position(self) -> tuple[int, int]:
        return tuple(self.player.get("position", [0, 0]))

    @property
    def map_id(self) -> str:
        info = mapping(self.raw.get("map"))
        return f"{info.get('group')}:{info.get('number')}"

    @property
    def mode(self) -> str:
        menu = self.dialog.get("menuType")
        if menu:
            return str(menu)
        emulator = mapping(self.raw.get("emulator"))
        if emulator.get("inBattle"):
            return "battle"
        if self.dialog.get("inDialog"):
            return "dialog"
        if emulator.get("allControlsLocked") or emulator.get("fieldControlsLocked"):
            return "locked"
        full_map = mapping(mapping(self.raw.get("map")).get("fullMap"))
        if not mapping(full_map.get("minimap_data")).get("grid"):
            return "boot"
        return "overworld"

    @property
    def fingerprint(self) -> str:
        # Exclude screenshot/frame counters and bridge timings; include everything
        # that can change the meaning or legality of a presented option.
        relevant = {key: self.raw.get(key) for key in (
            "player", "dialog", "battle", "party", "bag", "importantEvents"
        )}
        info = mapping(self.raw.get("map"))
        relevant["map"] = {key: info.get(key) for key in ("group", "number", "name")}
        # Moving background NPCs and fog discovery do not invalidate unrelated
        # decisions. Fresh option generation + per-edge checks handle geometry.
        return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]

    def model_state(self, memory: dict[str, Any]) -> dict[str, Any]:
        info = mapping(self.raw.get("map"))
        full = mapping(info.get("fullMap"))
        grid = mapping(full.get("minimap_data")).get("grid", [])
        location = {"id": self.map_id, "name": info.get("name"), "position": self.position} if grid else {"id": None, "name": None, "position": None}
        nearby = []
        for npc in full.get("npcs", []):
            position = npc.get("position", [])
            if len(position) == 2 and abs(position[0] - self.position[0]) + abs(position[1] - self.position[1]) <= 12:
                nearby.append({key: npc.get(key) for key in ("localId", "type", "position", "wandering")})
        return {
            "mode": self.mode,
            "location": location,
            "nearby_objects": nearby[:20],
            "connections": info.get("connections", []) if grid else [],
            "player": self.player,
            "dialog": self.dialog,
            "battle": self.raw.get("battle"),
            "party": self.raw.get("party"),
            "bag": self.raw.get("bag"),
            "pc": self.raw.get("pc"),
            "events": self.raw.get("importantEvents", {}),
            "memory": memory,
        }
