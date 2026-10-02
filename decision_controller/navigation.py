"""Deterministic routes through discovered terrain, with no LLM pathfinding."""

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

from decision_controller.state import State, mapping
from firered_bridge.constants.tiles import MINIMAP_TILES

DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
LEDGES = {5: "right", 6: "left", 7: "up", 8: "down"}
BLOCKS = {68: {"up"}, 69: {"down"}, 70: {"right"}, 71: {"left"},
          72: {"up", "right"}, 73: {"up", "left"}, 74: {"down", "right"}, 75: {"down", "left"}}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
WATER = {3, 4, 50, 51, 52, 53, 54}
FORCED = {44, 45, 46, 47, 60, 61, 62, 63}
PORTALS = {9, 23, 26, 27, 28, 29, 30, 31, 32}
INTERACTIVE = {10, 11, 14, 15, 16, 18, 21, 22, 33, 35, 36, 55, 65, 67}


@dataclass(frozen=True)
class Edge:
    command: str
    destination: tuple[int, int]


class Navigator:
    def __init__(self, state: State) -> None:
        self.state = state
        self.grid = mapping(mapping(mapping(state.raw.get("map")).get("fullMap")).get("minimap_data")).get("grid", [])

    def code(self, pos: tuple[int, int]) -> int | None:
        x, y = pos
        if 0 <= y < len(self.grid) and 0 <= x < len(self.grid[y]):
            value = self.grid[y][x]
            return value if type(value) is int else None
        return None

    def walkable(self, pos: tuple[int, int]) -> bool:
        code = self.code(pos)
        tile = MINIMAP_TILES.get(code)
        if code in WATER and not self.state.player.get("surfing"):
            return False
        return bool(tile and tile.passability and code not in INTERACTIVE and code not in LEDGES and code != 4)

    def edges(self, pos: tuple[int, int]) -> Iterator[Edge]:
        for command, (dx, dy) in DIRECTIONS.items():
            target = (pos[0] + dx, pos[1] + dy)
            code = self.code(target)
            if command in BLOCKS.get(self.code(pos), set()) or OPPOSITE[command] in BLOCKS.get(code, set()):
                continue
            if code in LEDGES:
                if LEDGES[code] != command:
                    continue
                target = (target[0] + dx, target[1] + dy)
            if self.walkable(target):
                yield Edge(command, target)

    def routes(self, max_steps: int = 80) -> dict[tuple[int, int], tuple[Edge, ...]]:
        start = self.state.position
        paths: dict[tuple[int, int], tuple[Edge, ...]] = {start: ()}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            route = paths[current]
            # Never plan through a door, warp, spinner or map transition.
            if len(route) >= max_steps or (current != start and self.code(current) in PORTALS | FORCED):
                continue
            for edge in self.edges(current):
                if edge.destination not in paths:
                    paths[edge.destination] = (*route, edge)
                    queue.append(edge.destination)
        return paths

    def interaction_route(self, target: tuple[int, int], paths: dict[tuple[int, int], tuple[Edge, ...]]) -> tuple[tuple[Edge, ...], str] | None:
        candidates = []
        for direction, (dx, dy) in DIRECTIONS.items():
            stand = (target[0] - dx, target[1] - dy)
            if stand in paths:
                candidates.append((paths[stand], direction))
        return min(candidates, key=lambda item: len(item[0])) if candidates else None
