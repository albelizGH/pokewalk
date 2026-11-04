# Feature: on-the-spot LAN tournament

Branch: `feat/tournament` (based on `feat/region-picker-and-versus-rules`, reuses room rules + ±5 handicap)

## Objective
Let 3-8 players on the same LAN join one room and play a single-elimination tournament right away, with every
participant watching the current match live.

## Decisions (agreed with the user)
- Single elimination, random seeding, byes when the player count is not a power of two.
- One match at a time; everyone else (waiting or eliminated, organizer included) spectates live. Parallel matches
  are out of scope for now.
- Star topology: everyone connects only to the organizer's machine, which referees every match (also matches
  it does not play). Players send actions; the referee broadcasts turns/state to both fighters and spectators.
- Room rules from the organizer: team size (1-6), level (1-100), ±5 level handicap per Pokémon (same as versus).
- Team selection: 60 s. When time runs out, missing team slots are filled with random Pokémon (no machine play).
  Move selection timeout keeps the pre-rolled 4-move set (current default).
- In battle: 60 s per decision, countdown visible during the last 15 s. Timeout = that player loses the turn
  (their Pokémon does nothing; the opponent still acts). 3 consecutive lost turns = loses the match.
  Autopilot is NOT used for timeouts (user: the machine playing for someone is cheating).
- Disconnection = loses the current match (and is out of the tournament).

## Constraints
- Code comments and UI copy follow the project's existing Spanish; identifiers in English.
- No automated test suite; checks are `py -m py_compile`, scripted checks of pure helpers, and localhost smoke
  tests driving several pokenet objects in one process.
- Delivery forecast ~600-900 authored lines: commit per task on this branch; PR slicing decided when pushing.

## Tasks
- [x] T1 Tournament lobby: organizer opens a tournament room with rules; up to 8 players join over the existing
  room discovery; lobby shows players; organizer starts with 3+. Disconnects in lobby are removed. — route: delegated writer
- [ ] T2 Headless referee + spectators: run the battle referee independently of the organizer's own BattleScene;
  broadcast turn steps/state to both fighters and to spectators, who render it read-only. — route: delegated writer
- [ ] T3 Bracket flow + timers: seeding/byes, sequential matches, team-selection 60 s random fill, 60 s turn timer
  with lost turn and 3-strike loss, disconnect = loss, bracket view, champion screen. — route: delegated writer

## Progress / evidence
### T1 (lobby) — done, uncommitted at time of writing
- Entry: Versus menu option `4 Crear torneo` (reuses firewall check, `size` stage and `vslevel` prompt). Versus hands
  the tournament to pokewalk.py via the `("tournament", obj)` event; pokewalk.py keeps it in `tour`.
- Join: picking a tournament in the room browser creates `TournamentClient(name, ip, port)`; manual IP goes through
  the normal 1v1 `Versus` guest, which hands its link (plus the queued messages) to `TournamentClient` when the
  host's hello carries `"tour": 1`.
- `PROTO` bumped 1 -> 2 so older builds neither list nor join new rooms (they would misread a tournament as 1v1).
- Classes (pokenet.py): `_TourLobby` (shared `view()`/`text()`/`key()`), `Tournament` (organizer: `players`, `rules`,
  `links` id->Link, `gone`, `update()` -> list of events `join`/`left`/`roster`/`start`/`msg`, `send(id, msg)`,
  `broadcast(msg, exclude)`, `start()`, `close()`), `TournamentClient` (guest: `update()` -> `roster`/`start`/`msg`/
  `error`, `send(msg)`). Stages: connecting -> hello -> lobby -> started.
- `RoomHost(name, rules, backlog, tour)` + `set_tour()` rebuilds the beacon; `probe_room` returns
  `(name, rules, tour)`; `RoomFinder.rooms()` items carry `"tour"`; `room_label()` renders
  `Torneo de Ana (ip) · 3/8 · 3 Pokémon · nivel 50`.
- Message shapes (player = `{"id", "name"}`, organizer id 0, ids never reused):
  - guest -> org `{"t":"hello","v":PROTO,"name"}`
  - org -> guest `{"t":"hello","v":PROTO,"name":org,"rules","tour":1,"you":id}`
  - org -> all `{"t":"roster","players":[...],"max":8}` on every join/leave
  - org -> all `{"t":"start","players":[...]}` final ordered roster; listening socket closed
  - org -> guest `{"t":"bye","why":"full"|"started"|"version"}` refusal; org -> all `{"t":"bye","why":"cancel"}`
  - probe reply / UDP beacon add `"tour":{"n","max"}`
  - anything else surfaces as `("msg", ...)` events (seam for T2/T3).
- After start the roster stays fixed; disconnects go to `Tournament.gone` (T3: disconnect = loss).
- Evidence:
  - `py -m py_compile pokewalk.py pokenet.py pokebattle.py`: OK.
  - Localhost smoke (`py -I` scratchpad script, `_fw_ok = True`): probe shows `tour {n:1,max:8}`; org + 3 guests (one via
    manual-IP Versus handoff, duplicate name -> "Ana 2") all see the same 4-player roster; a `bye` leave and an abrupt
    socket close each drop to 3 everywhere; a new guest joins (4); fill to 8, 9th gets "La sala está llena."; Enter
    starts, all 7 guests get `start` with identical ordered ids, org gets one `start` event; late join refused
    (connection refused, probe returns None); `send`/guest `send` deliver `msg` events; org close -> guests get
    "Org canceló el torneo."; 1v1 Versus host/guest handshake still yields `("team",)` on both with rules {2, 40}.
  - Not verified: on-screen rendering in Windows Terminal, real two-machine LAN, UDP beacon reception.

## Next step
T2.
