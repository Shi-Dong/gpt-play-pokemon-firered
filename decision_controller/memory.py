"""Atomic, bounded records and verified story milestones; no model summaries."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from decision_controller.state import State, mapping

ROM_MD5 = "e26ee0d44e809351c8ce2d73c7400cdd"
BADGES = ("BOULDER", "CASCADE", "THUNDER", "RAINBOW", "SOUL", "MARSH", "VOLCANO", "EARTH")
EVENTS = (
    ("starter", "EVENT_GOT_STARTER", "Choose a starter in Oak's laboratory"),
    ("pokedex", "EVENT_GOT_POKEDEX", "Bring Oak's Parcel from Viridian Mart to Oak"),
    ("rocket_hideout", "EVENT_BEAT_ROCKET_HIDEOUT_GIOVANNI", "Defeat Giovanni beneath Celadon Game Corner"),
    ("poke_flute", "EVENT_GOT_POKE_FLUTE", "Rescue Mr. Fuji in Pokémon Tower"),
    ("surf", "EVENT_GOT_HM03", "Find the Safari Zone Secret House and get Surf"),
    ("silph", "EVENT_BEAT_SILPH_CO_GIOVANNI", "Free Silph Co. using its Card Key and teleporters"),
    ("elite_four", "EVENT_BEAT_ELITE_FOUR", "Defeat the Elite Four"),
    ("champion", "EVENT_BEAT_CHAMPION_RIVAL", "Defeat the Champion"),
    ("hall_of_fame", "EVENT_HALL_OF_FAME", "Enter the Hall of Fame"),
)
ITEM_GOALS = (
    ("parcel", "OAKS_PARCEL", "Collect Oak's Parcel in Viridian Mart"),
    ("ticket", "SS_TICKET", "Help Bill on Route 25"),
    ("cut", "HM01", "Help the S.S. Anne captain and get Cut"),
    ("flash", "HM05", "Get Flash from Oak's aide near Diglett's Cave"),
    ("scope", "SILPH_SCOPE", "Recover the Silph Scope from Rocket Hideout"),
    ("teeth", "GOLD_TEETH", "Find the Warden's Gold Teeth in the Safari Zone"),
    ("strength", "HM04", "Return Gold Teeth to the Warden and get Strength"),
    ("card_key", "CARD_KEY", "Find the Silph Co. Card Key"),
    ("secret_key", "SECRET_KEY", "Find the Pokémon Mansion Secret Key"),
)
ORDER = ("starter", "parcel", "pokedex", "badge_BOULDER", "badge_CASCADE", "ticket", "cut",
         "badge_THUNDER", "flash", "badge_RAINBOW", "rocket_hideout", "scope", "poke_flute",
         "surf", "teeth", "strength", "badge_SOUL", "card_key", "silph", "badge_MARSH",
         "secret_key", "badge_VOLCANO", "badge_EARTH", "elite_four", "champion", "hall_of_fame")
OPTIONAL = {"flash"}


def normalize_name(name: str) -> str:
    return name.upper().replace("’", "").replace("'", "").replace(".", "").replace(" ", "_")


def item_names(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, list):
        for entry in value:
            result.update(item_names(entry))
    elif isinstance(value, dict):
        name = value.get("itemName", value.get("name"))
        if isinstance(name, str) and value.get("quantity", value.get("count", 1)) > 0:
            result.add(normalize_name(name))
        for key, entry in value.items():
            if isinstance(entry, (list, dict)):
                result.update(item_names(entry))
    return result


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as output:
        temporary = Path(output.name)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


class Memory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"schema": 1, "rom_md5": ROM_MD5, "milestones": {}, "maps": {},
                                     "targets": {}, "history": [], "failures": []}
        if path.exists():
            loaded = json.loads(path.read_text())
            if loaded.get("schema") != 1 or loaded.get("rom_md5") != ROM_MD5:
                raise ValueError("Memory schema/ROM mismatch")
            self.data = loaded

    def observe(self, state: State) -> None:
        completed = self.data["milestones"]
        events = mapping(state.raw.get("importantEvents"))
        for name, event, _ in EVENTS:
            if events.get(event) is True:
                completed[name] = {"evidence": event}
        badges = state.player.get("badges", {})
        for badge in BADGES:
            if (isinstance(badges, dict) and badges.get(badge) is True) or (isinstance(badges, list) and badge in badges):
                completed[f"badge_{badge}"] = {"evidence": f"badge:{badge}"}
        items = item_names(state.raw.get("bag"))
        for name, item, _ in ITEM_GOALS:
            if item in items:
                completed[name] = {"evidence": f"inventory:{item}"}
        # Consumed story items can be inferred only from a verified later event.
        implications = {"pokedex": ("starter", "parcel"), "rocket_hideout": (), "poke_flute": ("scope", "rocket_hideout"),
                        "strength": ("teeth",), "silph": ("card_key",), "hall_of_fame": ("elite_four", "champion",)}
        for milestone, implied in implications.items():
            if milestone in completed:
                for previous in implied:
                    completed.setdefault(previous, {"evidence": f"implied by {milestone}"})
        if state.mode == "overworld":
            info = mapping(state.raw.get("map"))
            entry = self.data["maps"].setdefault(state.map_id, {"name": info.get("name"), "positions": []})
            position = list(state.position)
            if position not in entry["positions"]:
                entry["positions"].append(position)
                entry["positions"] = entry["positions"][-1024:]
        self.save()

    def record(self, before: State, choice_id: str, after: State, result: str) -> None:
        key = f"{before.map_id}:{choice_id}"
        self.data["targets"][key] = self.data["targets"].get(key, 0) + 1
        entry = {"choice": choice_id, "map": before.map_id, "from": before.position,
                 "to": after.position, "after_map": after.map_id, "after_mode": after.mode, "result": result}
        self.data["history"] = [*self.data["history"][-63:], entry]
        if result in {"blocked", "stale", "target_moved", "error"}:
            self.data["failures"] = [*self.data["failures"][-31:], entry]
        self.observe(after)

    def save(self) -> None:
        atomic_json(self.path, self.data)

    def public(self) -> dict[str, Any]:
        goals = {name: hint for name, _, hint in (*EVENTS, *ITEM_GOALS)}
        goals.update({f"badge_{badge}": f"Earn the {badge.title()} Badge" for badge in BADGES})
        complete = self.data["milestones"]
        next_goal = next((name for name in ORDER if name not in complete and name not in OPTIONAL), None)
        return {"completed": complete, "next_goal": {"id": next_goal, "hint": goals.get(next_goal)},
                "game_complete": "hall_of_fame" in complete, "recent_actions": self.data["history"][-12:],
                "recent_failures": self.data["failures"][-8:], "visited_maps": list(self.data["maps"])}
