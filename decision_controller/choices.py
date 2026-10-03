"""Every presented choice contains a bounded, executable local plan."""

from dataclasses import asdict, dataclass
from typing import Any

from decision_controller.navigation import DIRECTIONS, FORCED, INTERACTIVE, PORTALS, Navigator
from decision_controller.state import State, mapping


@dataclass(frozen=True)
class Choice:
    id: str
    description: str
    kind: str = "command"
    commands: tuple[str, ...] = ()
    target: tuple[int, int] | None = None
    target_id: int | None = None
    menu_index: int | None = None

    def public(self, index: int) -> dict[str, Any]:
        return {"label": chr(65 + index), **asdict(self)}


def menu_payload(state: State) -> dict[str, Any]:
    dialog = state.dialog
    # Context menus take precedence over the underlying inventory/party list.
    party = mapping(dialog.get("partyMenu"))
    for value in (party.get("actionMenu"), dialog.get("battleUi"), dialog.get("choiceMenu"), dialog.get("startMenu")):
        menu = mapping(value)
        if isinstance(menu.get("options"), list) and menu["options"]:
            return menu
    for value in dialog.values():
        menu = mapping(value)
        if isinstance(menu.get("options"), list) and menu["options"]:
            return menu
    return {}


def option_name(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("name", option.get("label", option.get("text", "Unknown entry"))))
    return str(option)


def menu_cursor(menu: dict[str, Any]) -> int:
    options = menu.get("options", [])
    for index, option in enumerate(options):
        if isinstance(option, dict) and option.get("selected"):
            return index
    return int(menu.get("cursorPosition", 0))


def menu_commands(menu: dict[str, Any], index: int) -> tuple[str, ...]:
    cursor = menu_cursor(menu)
    if menu.get("layout") == "grid2x2":
        horizontal = index % 2 - cursor % 2
        vertical = index // 2 - cursor // 2
        return (*(("right" if horizontal > 0 else "left",) if horizontal else ()),
                *(("down" if vertical > 0 else "up",) if vertical else ()))
    delta = index - cursor
    return ("down" if delta > 0 else "up",) * abs(delta)


def menu_choices(state: State) -> list[Choice]:
    if state.mode == "namingScreen":
        naming = mapping(state.dialog.get("choiceMenu"))
        cursor = mapping(naming.get("cursor"))
        return [Choice("naming:finish", f'Finish naming with current text {naming.get("text", "")!r} (default name if empty)', "naming_finish"),
                Choice("naming:type", f'Type highlighted character {cursor.get("selected", "")!r}', commands=("a",)),
                *[Choice(f"cursor:{key}", f"Move keyboard cursor {key}", commands=(key,)) for key in DIRECTIONS],
                Choice("naming:delete", "Delete last character", commands=("b",)),
                Choice("wait", "Wait for naming animation", "wait")]
    menu = menu_payload(state)
    options = menu.get("options", [])
    result = []
    for index, option in enumerate(options[:22]):
        name = option_name(option)
        if not name or name.upper() in {"NONE", "-", "---"}:
            continue
        if isinstance(option, dict) and (option.get("disabled") or option.get("enabled") is False):
            continue
        pp = menu.get("pp", [])
        if state.mode == "battleMoves" and (index >= len(pp) or pp[index] <= 0):
            continue
        # Trainer battles cannot be escaped, even though RUN is painted on screen.
        battle = mapping(mapping(state.raw.get("battle")).get("data"))
        if name.upper() == "RUN" and (battle.get("isTrainerBattle") or mapping(state.raw.get("battle")).get("isTrainerBattle")):
            continue
        result.append(Choice(f"menu:{state.mode}:{index}", f"Choose {name}", "menu", menu_index=index))
    if result:
        return result + [Choice("menu:cancel", "Cancel / return", commands=("b",)), Choice("wait", "Wait for the game to settle", "wait")]
    if state.mode in {"dialog", "battle", "locked", "questLogPlayback", "pikachuIntro"}:
        return [Choice("continue", "Advance the displayed dialogue", commands=("a",)), Choice("wait", "Wait for animation / scripted movement", "wait")]
    # Complex screens (naming, bag pockets, PC grids, Fly map, quantity chooser)
    # expose their decoded state and bounded cursor steps. No guessed item IDs.
    result = [Choice(f"cursor:{key}", f"Move {key} in {state.mode}", commands=(key,)) for key in DIRECTIONS]
    result.extend([Choice("confirm", "Confirm the highlighted entry", commands=("a",)),
                   Choice("cancel", "Cancel / return", commands=("b",)),
                   Choice("wait", "Wait for animation", "wait")])
    if state.mode in {"boot", "namingScreen", "naming", "titleMenu", "mainMenu"}:
        result.append(Choice("menu:start", "Press Start on this screen", commands=("start",)))
    if state.mode in {"bagMenu", "tmCase", "pokemonStorage", "summaryScreen"}:
        result.extend([Choice("tab:left", "Previous pocket / page", commands=("l",)),
                       Choice("tab:right", "Next pocket / page", commands=("r",))])
    return result


def choices(state: State, memory: dict[str, Any] | None = None) -> list[Choice]:
    if state.mode != "overworld":
        return menu_choices(state)
    nav = Navigator(state)
    paths = nav.routes()
    candidates: list[tuple[int, Choice]] = []
    full_map = mapping(mapping(state.raw.get("map")).get("fullMap"))
    npcs = {tuple(npc["position"]): npc for npc in full_map.get("npcs", []) if isinstance(npc.get("position"), list)}
    warps = {tuple(warp["position"]): warp for warp in full_map.get("warp_events", []) if isinstance(warp.get("position"), list)}
    height = len(nav.grid)
    width = len(nav.grid[0]) if height else 0
    frontiers = [pos for pos in paths if pos != state.position and nav.code(pos) not in PORTALS | FORCED and
                 any(0 <= pos[0] + dx < width and 0 <= pos[1] + dy < height and
                     nav.code((pos[0] + dx, pos[1] + dy)) is None for dx, dy in DIRECTIONS.values())]
    for connection in mapping(state.raw.get("map")).get("connections", []):
        direction = connection.get("direction")
        destination = connection.get("mapName")
        if direction not in DIRECTIONS or not destination or destination == "MAP_NONE":
            continue
        boundary = [pos for pos in paths if
                    (direction == "up" and pos[1] == 0) or
                    (direction == "down" and pos[1] == height - 1) or
                    (direction == "left" and pos[0] == 0) or
                    (direction == "right" and pos[0] == width - 1)]
        # Present a few entrances; an offset/narrow connection can reject one
        # boundary crossing, which is recorded rather than blindly retried.
        for target in sorted(boundary, key=lambda pos: len(paths[pos]))[:3]:
            candidates.append((len(paths[target]), Choice(f"exit:{direction}:{target[0]}:{target[1]}",
                              f"Travel {direction} to {destination}", "exit", (direction,), target)))
        if not boundary and frontiers:
            dx, dy = DIRECTIONS[direction]
            target = min(frontiers, key=lambda pos: (-(pos[0]*dx + pos[1]*dy), len(paths[pos])))
            candidates.append((len(paths[target]), Choice(f"approach-exit:{direction}",
                              f"Explore toward the {direction} exit to {destination} via {target}", "travel", target=target)))
    for y, row in enumerate(nav.grid):
        for x, code in enumerate(row):
            target = (x, y)
            if code in INTERACTIVE:
                route = nav.interaction_route(target, paths)
                if route is None:
                    continue
                npc = npcs.get(target, {})
                label = npc.get("name", npc.get("type", "object"))
                description = f"Interact with {label} at ({x},{y})"
                if code == 33:
                    if not state.player.get("strengthEnabled"):
                        continue
                    behind = (x + DIRECTIONS[route[1]][0], y + DIRECTIONS[route[1]][1])
                    if not nav.walkable(behind):
                        continue
                    description = f"Push boulder {route[1]} at ({x},{y})"
                kind = "push" if code == 33 else "interact"
                candidates.append((len(route[0]), Choice(f"{kind}:{x}:{y}", description, kind, target=target,
                                                         target_id=npc.get("localId"))))
            elif target in paths and target != state.position:
                if code in PORTALS | FORCED:
                    warp = warps.get(target, {})
                    if not warp:
                        # The bridge renders directional stair/door tiles one
                        # cell away from the native warp-event approach square.
                        adjacent = [(pos, event) for pos, event in warps.items()
                                    if abs(pos[0] - x) + abs(pos[1] - y) == 1]
                        if len(adjacent) == 1:
                            warp = adjacent[0][1]
                    destination = warp.get("destMapName", "next area")
                    candidates.append((len(paths[target]), Choice(f"travel:{x}:{y}", f"Enter {destination} via ({x},{y})", "travel", target=target)))
                elif any(0 <= x + dx < width and 0 <= y + dy < height and nav.code((x + dx, y + dy)) is None for dx, dy in DIRECTIONS.values()):
                    candidates.append((len(paths[target]) + 10, Choice(f"explore:{x}:{y}", f"Explore undiscovered terrain near ({x},{y})", "travel", target=target)))
    for target, warp in warps.items():
        if target in paths or not frontiers:
            continue
        frontier = min(frontiers, key=lambda pos: (abs(pos[0]-target[0]) + abs(pos[1]-target[1]), len(paths[pos])))
        candidates.append((len(paths[frontier]), Choice(f"approach:{target[0]}:{target[1]}",
                          f'Approach exit to {warp.get("destMapName", "next area")} at {target}; reveal terrain from {frontier}',
                          "travel", target=frontier)))
    basic = [Choice(f"step:{edge.command}", f"Move one tile {edge.command}", "step", (edge.command,), edge.destination) for edge in nav.edges(state.position)]
    basic.extend([Choice("interact:front", "Interact with the object directly ahead", commands=("a",)),
                   Choice("open:start", "Open the game menu (party, bag, save)", commands=("start",))])
    result: list[Choice] = []
    seen: set[str] = set()
    visits = (memory or {}).get("targets", {})
    for _, choice in sorted(candidates, key=lambda item: item[0] + 8 * visits.get(f"{state.map_id}:{item[1].id}", 0)):
        if choice.id not in seen:
            result.append(choice)
            seen.add(choice.id)
        if len(result) >= 25 - len(basic):
            break
    # Named destinations and interactions precede cursor-sized movement choices.
    result.extend(basic)
    # A settled, unlocked overworld has no pending animation to wait for.
    # Dialogue/battle/locked modes retain their separate Wait choices.
    return result
