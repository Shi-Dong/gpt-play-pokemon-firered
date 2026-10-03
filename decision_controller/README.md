# FireRed probability controller

This controller is separate from the upstream OpenAI agent. Its optional
autonomous worker connects an OpenAI-compatible probability-JSON endpoint.
Without that worker, the viewer supports manual testing of the current adventure.

## What is implemented

- Isolated mGBA Lua socket, upstream Python bridge, separate browser viewer.
- Structured observations containing position, dialogue/menu, battle, party,
  inventory, PC, badges, event flags and bounded persistent history.
- At most 26 labeled choices, each with a concrete local execution plan.
- Semantic selections for decoded choice menus, battle actions and available-PP
  moves; bounded cursor controls for complex naming, inventory, PC, Fly and
  quantity screens. These controls preserve access to the native game menus.
- Local breadth-first shortest paths through discovered terrain. Respect walls,
  directional collision, one-way ledges and walking versus Surfing. Stop at
  warps/spinners; re-read and replan after every movement input.
- Reachable interactions, exits, exploration frontiers and Strength-enabled
  boulder pushes. Stop after a moved target, collision, unexpected movement,
  battle, dialogue or map transition. Reject stale snapshots and unknown IDs.
- Evidence-based story goals, all eight badges, required key items, Elite Four,
  Champion and Hall of Fame; atomic memory, visit counts and recent failures.
- Checkpoint pairs containing ROM identity, savestate hash and memory.
  Existing memory cannot silently start a new game; use explicit `--resume`.
- Fresh user-only State/Options model requests with A–Z labels and thinking
  disabled. Reject malformed, duplicate, missing, extra, nonnumeric, nonfinite,
  out-of-range or all-zero probability weights, reasoning and truncated output.
  Normalize positive totals while logging the raw total and response; select
  the highest probability without substituting a scripted strategic answer.
- Autonomous play, live call timing, pause/resume, stale-response rejection,
  bounded retries, checkpoints and progress-preserving stops.

## Install and launch

The upstream supported ROM is FireRed USA v1.0 with MD5
`e26ee0d44e809351c8ce2d73c7400cdd`. Supply your own ROM. ROMs, runtime saves,
screenshots and model credentials must never be committed.

Use a recent mGBA **Qt** frontend built with Lua 5.4 and scripting enabled.
The older SDL frontend does not provide Qt's `--script` option. On Ubuntu 22.04
install `qtbase5-dev qtmultimedia5-dev libqt5opengl5-dev qttools5-dev
qttools5-dev-tools liblua5.4-dev xvfb`, then configure mGBA with
`-DBUILD_QT=ON -DBUILD_SDL=OFF -DUSE_LUA=ON -DENABLE_SCRIPTING=ON
-DUSE_SQLITE3=ON`. The tested mGBA revision is
`c3c8e5e` from its master branch. This revision's Qt frontend crashes during
window construction when SQLite support is disabled; keep it enabled.
The launcher disables audio synchronization and uses video synchronization at
60 FPS. Without a headless audio device, audio synchronization can stall the
CPU thread and prevent Lua sockets from answering.
For shallow mGBA checkouts without release tags, also use `-DSKIP_GIT=ON`.

From the repository root, using an interactive Fish shell:

```fish
uv sync --extra test --no-install-project
.venv/bin/python -m pytest
.venv/bin/python -m decision_controller.launch \
    --rom /path/to/pokemon_firered.gba \
    --emulator /path/to/mgba-qt \
    --host 100.64.0.10 \
    --port 8788 \
    --bridge-port 8001 \
    --socket-port 8898
```

Replace the example paths and Tailnet IP with your own. Choose unused ports. The raw emulator socket and upstream bridge bind to
loopback; only the staging viewer binds to the specified host. Bind the viewer
to a Tailnet address to view it from another Tailnet machine. Keep the launcher
inside a persistent tmux session. It manages only the processes it starts.

After the viewer is ready, run its worker in another persistent tmux session:

```fish
.venv/bin/python -m decision_controller.autoplay \
    --viewer-url http://100.64.0.10:8788 \
    --endpoint http://your-inference-server:30000/v1/chat/completions \
    --model your-trained-model \
    --runtime /absolute/path/to/repository/.runtime \
    --autoplay
```

Use the same runtime directory for the launcher and worker. `--autoplay` is an
explicit startup instruction; omit it to start paused. The endpoint must return
a JSON object assigning every offered letter a probability. The worker sends
`chat_template_kwargs: {"enable_thinking": false}`, temperature zero and one
user message, without tool calls or accumulated conversation. It requests every
decision afresh. No application answer cache is used.

The viewer shows the latest HTTP model-call duration in milliseconds and seconds,
the elapsed time of a pending call, selected action and probabilities. This is
client-observed HTTP latency, including transport and server processing; it is
not a GPU-only measurement or time to first token. Button execution and saving
are excluded. Raw requests/responses are written to `model-calls.jsonl` inside
the ignored runtime directory. A changing game state or pause/resume instruction
during inference discards the eventual action. A route executes at most eight
movement steps before returning to the model; each step is re-observed.

## APIs

- `GET /api/state`: structured state, fingerprint and labeled options.
- `GET /api/controller`: phase, call timing, latest choice, error and heartbeat.
- `POST /api/control`: `{"enabled":false}` pauses; `true` resumes the worker.
  Pause cancels pending inference; an already executing bounded local action
  finishes before the next model decision. Manual inputs require paused play.
- `POST /api/execute`: `{"choice_id":"…","fingerprint":"…"}`. Returns 409
  on a stale snapshot. Fetch fresh state and choose again.
- `GET /screen.png`: current native framebuffer.
- `POST /api/checkpoint`: save at a stable menu, dialogue or overworld decision boundary. Rejects
  saves if the observed game state changes during the operation.

The viewer can exercise the same options manually. Cross-origin browser
control requests are rejected. This is a Tailnet staging service; it is not a
public internet deployment.

The staging launcher disables the upstream dialogue cache with
`FIRERED_DIALOG_CACHE=0`. Its cache does not cover all the memory used by screen
decoders, so retaining decoded text can hide an intro/menu change.

After a successful checkpoint, restart with the same arguments plus `--resume`.
This verifies the checkpoint and restores the associated memory and emulator
state together. No automatic resets, rollback or adventure repetition exist.
The worker attempts a checkpoint after each executed decision at a stable
boundary and on graceful shutdown. The newest 20 pairs are retained. Three
consecutive request/protocol errors or repeated unchanged state/action pause
the worker and expose the error. Resume retries the existing adventure. Hall
of Fame evidence stops autonomous play; defeating an arbitrary trainer does not.

## Validation scope and remaining work

Tests exercise the real upstream command schema, plus stale-choice rejection,
menu navigation, walls, unknown terrain, water, ledges, directional collision,
warps, battle/route interruptions, zero-PP filtering, trainer escape rejection,
memory persistence, milestone evidence and browser origin checks. Captured
native emulator state is used as a further integration fixture.

**Full-game completion has not been demonstrated by this controller.** Complex
screens currently use decoded-state cursor controls rather than complete
high-level plans such as "teach Surf to Pokémon X" or "withdraw item Y".
Move legality beyond PP (Disable, Encore, trapping and other battle restrictions)
requires further state decoding or game feedback. Connection crossings are
bounded attempts through observed reachable map boundaries; narrow offset
connections can reject an attempted entrance. Strength puzzles are incremental
push choices, not a proven puzzle solver. Silph teleporters and Rocket spinners
are observed transitions, not precomputed solutions. These are material limits
when assessing readiness for unattended end-to-end play.

Protocol tests also exercise the actual endpoint request body and raw response
consumer, normalization, paused-call cancellation, stale-call discard and
unchanged-action stops. Native staged acceptance with a trained SGLang endpoint
has verified introductory dialogue, name selection and browser pause/resume.
Starter/rival, Brock and later-game mechanics still require real-game acceptance.

Upstream game decoding and Lua bridge remain credited to
[Clad3815/gpt-play-pokemon-firered](https://github.com/Clad3815/gpt-play-pokemon-firered)
under the repository's existing license.
