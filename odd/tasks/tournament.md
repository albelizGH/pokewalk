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
- [x] T2 Headless referee + spectators: run the battle referee independently of the organizer's own BattleScene;
  broadcast turn steps/state to both fighters and to spectators, who render it read-only. — route: delegated writer
- [ ] T3 Bracket flow + timers: seeding/byes, sequential matches, team-selection 60 s random fill, 60 s turn timer
  with lost turn and 3-strike loss, disconnect = loss, bracket view, champion screen. — route: delegated writer
- [x] T4 (runs before T2) Room rules `legendarios sí/no` and `míticos sí/no` as two separate rules (PokeAPI
  `is_legendary` / `is_mythical` in pokemon_species.csv), for versus and tournament: shown with the rules, typed
  disallowed Pokémon rejected in the prompt with a clear message, random fills respect them, Tab regional list marks
  them. Ultra Beasts / Paradox are not flagged by PokeAPI and stay allowed (told to the user). — route: delegated writer
- [ ] T5 (after T3) Internet mode with nothing to install: relay through the public MQTT broker broker.hivemq.com
  (TLS 8883, stdlib-only minimal MQTT 3.1.1 client), fallback broker configured. Organizer gets a room code
  (e.g. `PIKA-7F3K-92QD`); topic derived from the code; every message HMAC-signed with a key derived from the code
  (no injection; readable by someone who guesses the code — accepted). Works for 1v1 and tournament (organizer stays
  referee). Feasibility evidence (2026-11-04): hivemq 200/200 msgs at 50/s, median ~370 ms (TLS ~420 ms);
  ntfy.sh 429 after ~34 fast posts; broker.emqx.io 44/200; test.mosquitto.org no connect. — route: delegated writer

## Delivery
User decision: push everything together when all tasks are done (main history rewrite force-push is still the
user's own command; branch push after that).

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

### T4 (legendarios / míticos) — done, uncommitted at time of writing
- Rules dict gains `"legend"`, `"myth"` (bool); `room_rules()` defaults missing/non-False to True (allowed), so they
  travel unchanged in hello, probe reply, beacon and tournament hello. `rules_text()` appends ` · sin legendarios` /
  ` · sin míticos` only when forbidden.
- Picking: new `Versus` stage `"special"` after the `vslevel` prompt (`ask_special(level)`), one key:
  `1` todos (Enter), `2` sin legendarios, `3` sin míticos, `4` sin ninguno, `0` back to `size`. Shared by 1v1 and
  tournament (`host(size, level, legend, myth)`).
- Data: `pokewalk.parse_species_flags(csv)` (pure) -> `{dex: "legend"|"myth"}`; `species_flags()` caches it from
  `species.csv` and is injected as `pn.species_flags`. pokenet helpers for T2/T3: `special_kind(dex)`,
  `rule_problem(dex, name, rules)`, `team_problem(fighters, rules)`; pokewalk `random_allowed(n, rules)` (1..493, no
  repeats, only allowed species) for T3's random fill.
- Enforcement: `parse_vs_team(parts, walking, size, rules)` resolves every name (typed or walking) and rejects
  disallowed ones in the prompt ("Mewtwo es legendario: esta sala no los permite"; walking default is rejected with
  "(camina con vos: escribí tu equipo)" rather than silently dropped); unknown names now also fail in the prompt
  ("No conozco a 'x'"). Receiving side of `Versus` rejects a team that breaks the rules ("mandó un equipo inválido").
- Tab regional list: ` ★` legendario / ` ✦` mítico always; from the versus team prompt, entries the room forbids are
  dimmed and the footer says so.
- Evidence:
  - `py -m py_compile pokewalk.py pokenet.py pokebattle.py`: OK.
  - `check_t4.py` (scratchpad, `py -I`): flags 150 legend / 151 myth / 25 none, 793 & 1005 unflagged (26 legendary +
    9 mythical in 1..493); `parse_vs_team` rejects mewtwo/150 with legend off, mew/151 with myth off, walking mewtwo,
    accepts them when allowed; 3000 random 6-teams with both off and 2000 per single rule never pick a disallowed one;
    `room_rules` defaults; `rules_text` -> "3 Pokémon · nivel 50 · sin legendarios · sin míticos"; `special` stage keys;
    localhost handshake: guest receives `legend: False, myth: True`; host rejects a sent Mewtwo team.
  - `check_rules.py` and `smoke_tour.py` re-run: ALL OK (updated for the new rule keys, real names in
    `parse_vs_team` placeholders, and the `special` step; tournament guests receive `legend: False`, 1v1 guest
    `myth: False`).
  - Not verified: on-screen rendering of the `special` menu and the ★/✦ marks in Windows Terminal; real LAN.

### T2 (headless referee + spectators) — done, uncommitted at time of writing
- Size: ~560 authored changed lines (pokenet ~+390, pokewalk ~+125, pokebattle ~+30, README); over the 400 heuristic
  because the shared referee refactor, the client side and the spectator scene only work together.
- pokebattle: `Battle.turn` accepts `("pass", 0)` (step "¡X no hizo nada! (se le acabó el tiempo)", the other side
  still acts). `side_text(s, mine, theirs, watch)` gains an optional spectator variant with `pb.WHO` = that side's
  player; `resolve_text(text, None, names)` resolves it ("¡Pikachu de Ana usó…!", "¡Beto envía a…!", "¡Ganó Ana!");
  without a variant a spectator reads `theirs`. `flip_text` keeps the 4th field.
- pokenet shared core: `Referee(battle)` (link-free: `expected(s)`, `waiting()`, `choose(s, kind, val)` -> steps|None,
  `pass_turn(s)`: act -> internal `PASS` (identity-checked, a wire `["pass",0]` is cleaned into a move), send -> first
  alive) and `turn_msg(b, steps, flip, **extra)`. `HostSession` (1v1) now delegates to `Referee`; `Session._turn(m)`
  is the shared mirror-apply used by `GuestSession` and `MatchSession`. `Session.rematchable` (False in tournament).
- Organizer: `MatchReferee(tour, a_id, b_id, chart)` -> broadcasts `m_prep`; collects `m_team` (validated with
  `team_from_wire` + size + `team_problem`; invalid = forfeit); preloads `chart` in a thread; `m_start` with both
  teams on the wire; resolves turns via `Referee`; sends `turn` to a (plain), b (`flip`), every spectator (plain, a on
  the left); `m_over` to all. Organizer (id 0) has no socket: its messages go to `ref.local` (`take_local()`), its
  sends enter via `receive(0, msg)`. `update(tour_events)` consumes this match's `msg`s and `left` (= forfeit) and
  returns `("started",)`, `("turn", steps)`, `("over", winner_id|None, text)`.
  T3 hooks: `waiting_for()` (ids owing team/action/replacement), `pass_turn(id)` (lost turn), `forfeit(id, text)`.
  Draw (`winner None`) and chart-download failure (`over` with None) are left for T3 to decide.
- Participant: `MatchClient(me, name, send_fn, make_chart).handle(msg)` -> `("team", MatchTeam)`, `("start",
  MatchSession)`, `("note", text)`, `("over", winner, text)`. `MatchTeam` impersonates `Versus` so pokewalk reuses the
  vsteam prompt -> load -> MovePicker -> `set_team` path (prompt `vsteam_tour`); closing it before sending = `m_quit`.
  `MatchSession` = mirror battle fed by `feed()`, sends `act`/`send` with `"m"`, `close()` mid-battle = `m_quit`,
  `abort(text)` on `m_over`/tournament closed (fighters get "¡Ganaste!"/"Perdiste.").
- Message shapes (all carry `"m"` = match number; documented in pokenet.py above `MatchSession`):
  `m_prep {a, b, rules}` org->all · `m_team {fighters}` fighter->org · `m_start {a, b, rules, teams}` org->all ·
  `act {a}` / `send {i}` fighter->org · `turn {steps, state}` org->all (b flipped) · `m_quit` fighter->org ·
  `m_over {winner, why}` org->all.
- pokewalk: `BattleScene(..., watch=(left, right))` read-only: no menus, keys ignored until over, HUD "Mirando: A
  (izquierda) vs B (derecha)" + last log lines (one-line fallback), "¡Ganó Ana!", auto-close after 20 s.
  `_finish` animates already-queued turns before deciding the phase (fixes a menu-before-queued-turn race in 1v1 too).
  `prepare_match` thread downloads both teams' sprites (`battle_sprite`) + chart for every participant, then
  `("tmatch", ...)` builds the scene. Main loop: `mc` created on tour `start` (org sends via `to_referee`, guests via
  `tour.send`); org routes `tour.update()` events into `match_ref.update()` and drains `take_local()` into its `mc`.
- T2 manual trigger (T3 replaces with the bracket): after start the organizer's lobby foot shows
  `Enter` = 1st vs 2nd (org vs first guest), `2` = 2nd vs 3rd (two guests, org spectates); `Tournament.key` returns
  `("match", a, b)`; one match at a time. The lobby title shows `tour.note` (match status).
- Evidence:
  - `py -m py_compile pokewalk.py pokenet.py pokebattle.py`: OK.
  - `smoke_t2.py` (scratchpad, `py -I`, fake Fighters/moves + fake type chart, no PokeAPI; real localhost sockets;
    fighters and spectators driven through real headless `BattleScene`s with fake sprites and key presses): pass action
    (pp unchanged, rival acts; player/rival/spectator texts); `Referee.pass_turn` act and send; a wire `["pass",0]` does
    not pass. Org + 3 guests: Ana vs Beto refereed by org, Ana's first turn passed by `pass_turn` -> 7 turns, Beto wins,
    `over` everywhere; Ana's steps == referee plain, Beto's == flipped, Caro and org-as-local-spectator == plain; final
    mirror states equal (Beto's flipped); spectator HUD has "Mirando:", keys return None, no menu phase. Org vs Caro
    (org fights through its local scene, no socket; Ana/Beto watch) -> 7 turns, identical checks. Esc before team ->
    `m_quit` -> other wins. Socket cut mid-battle -> `left` -> forfeit, winner's scene "… ¡Ganaste!". Legendary in a
    "sin legendarios" room -> referee forfeits the sender. 1v1 Versus (refactored HostSession/GuestSession) end-to-end
    battle with scenes: both reach over, mirror state == flipped host state, rematch still offered. ALL OK.
  - `smoke_tour.py`, `check_rules.py`, `check_t4.py` re-run unchanged: ALL OK.
  - symtable scan of pokewalk.py / pokenet.py for referenced-but-undefined globals (covers the untested main()
    wiring): none.
  - Not verified: on-screen rendering (spectator HUD, intro texts) in Windows Terminal, real sprite downloads for
    spectators, the main() wiring of keys/jobs (`on_match`, `tmatch`, `to_referee`) beyond compile, real LAN.

## Next step
T3.
