"""Probability-only decision endpoint; retain raw output and measured HTTP time."""

import json
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import requests


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate probability key")
        result[key] = value
    return result


def decode(content: str, labels: Sequence[str]) -> tuple[dict[str, float], float]:
    value = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != set(labels):
        raise ValueError("Probability labels differ from offered choices")
    if any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in value.values()):
        raise ValueError("Invalid probability")
    total = sum(value.values())
    if total <= 0:
        raise ValueError("All probabilities are zero")
    # The trained endpoint sometimes emits rounded weights whose total drifts.
    # Preserve ranking, report the correction and never invent a missing weight.
    return {key: value[key] / total for key in labels}, total


def request_body(state: dict[str, Any], options: Sequence[dict[str, Any]], model: str) -> dict[str, Any]:
    labels = [option["label"] for option in options]
    if not labels or len(labels) != len(set(labels)):
        raise ValueError("Expected distinct nonempty option labels")
    prompt = "State:\n" + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    goal = state.get("memory", {}).get("next_goal", {})
    location = state.get("location", {}).get("name", "") or ""
    guidance = goal.get("hint", "Progress toward the Hall of Fame")
    if goal.get("id") == "starter":
        if "PLAYERS_HOUSE_2_F" in location:
            guidance = "Leave the upstairs room by entering the stairs to PLAYERS_HOUSE_1_F. Then leave the house."
        elif "PLAYERS_HOUSE_1_F" in location:
            guidance = "Exit the player's house to PALLET_TOWN."
        elif location == "PALLET_TOWN":
            guidance = "Reach ROUTE_1 at the north edge of Pallet Town to trigger Oak stopping you and leading you to his lab."
            if state.get("location", {}).get("position") == [6, 8]:
                guidance += " You are directly outside your house door. UP from (6,8) re-enters the house; it does not take you toward Route 1. First move LEFT, RIGHT or DOWN away from the door, then navigate around the house toward the north exit. Do not re-enter your house."
        elif "OAKS_LAB" in location:
            guidance = "Choose your starter: advance any displayed dialogue, then approach and interact with one of the ITEM_BALL objects (starter Poké Balls) on the laboratory table. Accept the Pokémon when asked. Do not keep talking to PROF_OAK or BLUE; those conversations repeat without choosing a starter."
    if goal.get("id") == "parcel":
        if "OAKS_LAB" in location:
            guidance = "Your starter is already obtained. Leave Oak's lab through the south exit to PALLET_TOWN. Do not talk to Oak again or inspect the remaining starter balls."
        elif location == "PALLET_TOWN":
            guidance = "Travel north out of Pallet Town to ROUTE_1, then continue north to VIRIDIAN_CITY and enter its POKEMON_MART to collect Oak's Parcel. Do not enter Oak's lab or your house."
        elif location.replace("_", "") == "ROUTE1":
            guidance = "Cross Route 1 northward to VIRIDIAN_CITY. The south exit returns to Pallet Town; avoid it while collecting the parcel."
        elif location == "VIRIDIAN_CITY":
            guidance = "Enter VIRIDIAN_CITY_POKEMON_MART and talk to the shop clerk to receive Oak's Parcel."
        elif "VIRIDIAN" in location and "MART" in location:
            guidance = "Talk to the shop clerk to collect Oak's Parcel."
    recent = state.get("memory", {}).get("recent_actions", [])
    if recent and recent[-1].get("map") != recent[-1].get("after_map"):
        guidance += " You have just crossed a map entrance. The closest doorway may return to the map you just left. Move away from that doorway before continuing toward your objective."
    prompt += f"\n\nCurrent objective: {guidance}\nChoose an offered action that makes progress. Prefer a named destination over individual steps when it leads toward this objective. Avoid actions that just failed.\n\nOptions:\n"
    prompt += "\n".join(f'{option["label"]}. {option["id"]}: {option["description"]}' for option in options)
    prompt += "\n\nReport your probability for every option as a JSON object with exactly the keys "
    prompt += ", ".join(json.dumps(label) for label in labels)
    prompt += ". Use finite numbers between 0 and 1 that sum to 1. Report your uncertainty honestly. Output only JSON, without explanation or Markdown."
    return {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0,
            "max_tokens": max(512, 24 * len(options)), "chat_template_kwargs": {"enable_thinking": False}}


class Model:
    def __init__(self, endpoint: str, name: str, log: Path, timeout: float = 60) -> None:
        self.endpoint = endpoint
        self.name = name
        self.log = log
        self.timeout = timeout
        self.session = requests.Session()

    def decide(self, state: dict[str, Any], options: Sequence[dict[str, Any]]) -> dict[str, Any]:
        body = request_body(state, options, self.name)
        record: dict[str, Any] = {"at": time.time(), "request": body}
        started = time.perf_counter()
        try:
            response = self.session.post(self.endpoint, json=body, timeout=(10, self.timeout))
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            record["http_status"] = response.status_code
            record["raw_response"] = response.text
            response.raise_for_status()
            payload = response.json()
            completion = payload["choices"][0]
            message = completion["message"]
            if completion.get("finish_reason") == "length" or message.get("reasoning_content") or message.get("tool_calls"):
                raise ValueError("Truncated response, reasoning or tool call")
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("Missing probability JSON")
            probabilities, total = decode(content, [option["label"] for option in options])
            best = max(options, key=lambda option: probabilities[option["label"]])
            record.update(probabilities=probabilities, raw_total=total, choice_id=best["id"],
                          description=best["description"], usage=payload.get("usage"))
            return record
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
            record["error"] = str(error)
            raise
        finally:
            record.setdefault("latency_ms", round((time.perf_counter() - started) * 1000, 3))
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
