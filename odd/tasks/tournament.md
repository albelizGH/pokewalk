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
- [x] T3 Bracket flow + timers: seeding/byes, sequential matches, team-selection 60 s random fill, 60 s turn timer
  with lost turn and 3-strike loss, disconnect = loss, bracket view, champion screen. — route: delegated writer
- [x] T4 (runs before T2) Room rules `legendarios sí/no` and `míticos sí/no` as two separate rules (PokeAPI
  `is_legendary` / `is_mythical` in pokemon_species.csv), for versus and tournament: shown with the rules, typed
  disallowed Pokémon rejected in the prompt with a clear message, random fills respect them, Tab regional list marks
  them. Ultra Beasts / Paradox are not flagged by PokeAPI and stay allowed (told to the user). — route: delegated writer
- [x] T5 (after T3) Internet mode with nothing to install: relay through the public MQTT broker broker.hivemq.com
  (TLS 8883, stdlib-only minimal MQTT 3.1.1 client), fallback broker configured. Organizer gets a room code
  (e.g. `PIKA-7F3K-92QD`); topic derived from the code; every message HMAC-signed with a key derived from the code
  (no injection; readable by someone who guesses the code — accepted). Works for 1v1 and tournament (organizer stays
  referee). Feasibility evidence (2026-11-04): hivemq 200/200 msgs at 50/s, median ~370 ms (TLS ~420 ms);
  ntfy.sh 429 after ~34 fast posts; broker.emqx.io 44/200; test.mosquitto.org no connect. — route: delegated writer
- [x] T6 (after T2, before T3) Picker UX: (a) ranges like `257-260` in the Tab regional list show exactly those
  (even across regions), Enter writes them all, 10-per-action cap shown in the list; (b) live preview under the
  add/set prompt of what would be added (`#25 pikachu · #4 charmander…`), unknown names flagged before Enter;
  (c) the list stays open after picking in multi-Pokémon prompts (add, set, battle, vsteam) showing what was picked
  so far; info still closes on pick; in battle, Tab inside the list writes ` vs ` and switches to picking the rival
  (header `Tu equipo: … vs Rival: …`); Esc returns to the prompt with everything written. — route: delegated writer

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
  - Flakiness follow-up (parent saw 3/4 failures): both causes were in the test, not the product. (1) Roster order is
    admission order and guests connect from parallel threads, so the test now takes ids from `org.players`. (2) A
    sub-test stopped pumping when the referee said `over`, so that match's `m_over` reached a spectator during the next
    sub-test and met a loose wait. Per-match filtering in `MatchClient` worked correctly. The test now waits for
    `everyone_over()` (every connected participant got that match's `over`) before starting the next match. No product
    change. Result: `smoke_t2.py` 10/10 sequential + 8/8 run 4-at-a-time (4 different roster orders seen);
    `smoke_tour.py`, `check_rules.py`, `check_t4.py` OK.
  - Not verified: on-screen rendering (spectator HUD, intro texts) in Windows Terminal, real sprite downloads for
    spectators, the main() wiring of keys/jobs (`on_match`, `tmatch`, `to_referee`) beyond compile, real LAN.

### T6 (picker UX) — done, uncommitted at time of writing
- Route: delegated writer (pokewalk.py + README.md). Test-first exception: no runnable test harness for the
  curses-like main loop; pure helpers were factored out and checked with a scripted test instead.
- Design: module-level pure helpers next to `species_by_gen()`: `dex_range`, `range_entries(q, {dex: name})`,
  `dex_pick_buf`, `add_vs`, `picked_header`, `preview_terms(buf, species=None)` (no network: uses the already
  loaded `_species`, None if missing), `preview_parts`, `warm_species` (background thread loads the species list once
  so the preview can appear). Closures stay thin: `dex_found` uses the full species map (`dex_all`) for `N-M`;
  Enter on a range writes the first 10 (title warns "solo se agregan 10 por vez"); in multi prompts the list stays
  open, search resets in the same region, a header row shows `Elegidos: …` / `Tu equipo: … vs Rival: …`; info
  still closes on pick; in battle Tab adds ` vs ` once (title note otherwise), footer says `Tab ahora el rival`.
  `add_preview()` caches by (mode, buf, species loaded) and draws one extra row under the add/set prompt only when
  `avail >= 2`. Battle prompt hint and README rows for `a`/`b` updated.
- Evidence: `py -m py_compile pokewalk.py pokenet.py pokebattle.py` exit 0; scratchpad `check_t6.py` exit 0
  (257-260 -> blaziken, mudkip, marshtomp, swampert; 150-155 crosses Kanto->Johto; 1-50 -> 50 shown, 10 written;
  preview `pikachu 4-6` -> #25 #4 #5 #6, `¿pikahcu?` flagged in red, `1-15 mew` -> 10 + "quedan 6 afuera"; no species
  -> None; repeated picks, `vs` once, headers); `smoke_t2.py`, `smoke_tour.py`, `check_rules.py`, `check_t4.py` exit 0.
- Not verified: on-screen rendering and key handling in a real terminal (the `main()` closures `dex_key`,
  `dex_block`, `dex_text`, `add_preview` were only compiled, not driven), narrow-terminal truncation of the longer
  footer/title, and the background species warm-up timing.

### T3 (bracket flow + timers) — done, uncommitted at time of writing
- Route: delegated writer (pokenet.py, pokewalk.py, README.md). Size ~710 changed lines (pokenet ~+480/-35,
  pokewalk ~+130/-30, README ~+30/-6): over the 400 heuristic because the director, the referee clocks and the client
  timeouts only make sense together. pokebattle.py unchanged. Test-first exception: no runnable harness for the main
  loop; pure helpers + injectable clock were checked with scripts, the network path with a localhost smoke.
- Bracket (pure, pokenet): `make_bracket(ids, rng)` -> list of rounds of `[a, b, winner]` (None = unknown,
  `EMPTY = -1` = nobody); byes only in round 1, one per match at spread positions (`i * half // byes`), resolved at
  creation; `advance(rounds, r, j, w)` + `_settle` (an EMPTY side passes the other); `next_match` (bracket order);
  `champion`; `round_name` (Cuartos/Semi/Final). Wire: `{"t": "bracket", rounds, cur, gone, champ, begun, next, note}`
  org -> all; `_TourLobby.set_bracket()` (guests on the message, organizer directly) feeds a shared `view()`:
  items `(round, "Ana vs Beto → Ana", current)` (current drawn in WARN by `room_block`), "pasa directo", "(se fue)",
  title = note / "próxima pelea en N s" / "¡X es el campeón del torneo!".
- `TourDirector(tour, make_chart, build_team, rng, clock)` (organizer): draws the bracket on start, waits for the
  organizer's Enter (`Tournament.key` -> "go", replaces the T2 test-match keys), then runs one `MatchReferee` at a
  time; next match NEXT_SECS (12) + last-turn animation allowance after the previous one. Walkover: a player in
  `tour.gone` when its match comes up loses it without playing. Draw rule (any `over` with winner None: both fainted,
  both 3 strikes, chart failure, referee error): replay once with the same teams (fresh copies, `m_prep` with
  `"replay": true`, no team prompt); a second no-winner -> `rng.choice` coin flip, announced in the note. Champion ->
  `("champion", id)`; after the champion the guests ignore the organizer's close and keep the final screen until Esc.
  Organizer Esc/quit before that -> guests get "X cerró el torneo: se terminó para todos."
- Timers (referee-authoritative, module constants TEAM_SECS 60, TEAM_GRACE 20, TURN_SECS 60, WARN_SECS 15,
  MAX_PASSES 3, STEP_SECS 2.5, ANIM_MAX 40, START_GRACE 20, NEXT_SECS 12; read at call time so tests shrink them):
  - Team: `m_prep` carries `secs`; `MatchTeam(secs, clock)` exposes `secs_left()` / `late()`; the vsteam prompt and
    the move picker show the seconds. At the client deadline pokewalk's `team_timeout()` sends what it has:
    `partial_team(typed, walking, size, rules)` keeps typed names that exist, are allowed and not repeated (`:level`
    ignored), fills the rest with `random_allowed`; nothing typed = all random (walking Pokémon don't count unless
    Enter was pressed); if already loading it sends on arrival; in the picker it sends the pre-rolled moves. Referee
    hard deadline TEAM_SECS + TEAM_GRACE: missing sides are locked (a late `m_team` is ignored) and built by
    `build_team(n, rules)` in a thread (`random_team` in pokewalk: `random_allowed` + `load_fighter` + handicap);
    `m_note` tells everyone; build failure -> that side forfeits (both -> no winner).
  - Battle: `m_clock {secs, who, lost}` after `m_start` (TURN_SECS + START_GRACE) and after every turn (TURN_SECS +
    STEP_SECS per step, capped); at the deadline every waiting side gets a strike and `Referee.pass_turn`; MAX_PASSES
    consecutive -> forfeit (both at once -> no winner). A real accepted action resets the count. Actions carry `n`
    (turns seen); a stale one (sent before a pass reached the client) is dropped and does not reset the count.
  - Client: `MatchSession.clock_text()` -> "Te quedan 12 s para elegir (turnos perdidos seguidos: 2 de 3)" for the
    deciding player, "Ana: 12 s para elegir" for spectators, only in the last WARN_SECS; `BattleScene.hud` shows it
    above the menu (or inline). `BattleScene._poll_net` now animates a turn that arrives while the menu is open (the
    referee passed you); tournament over screens close after 6 s so the bracket is visible between matches; a finished
    scene is closed when your next team prompt opens.
- Evidence:
  - `py -m py_compile pokewalk.py pokenet.py pokebattle.py`: exit 0. pyflakes (scratchpad lib): exit 0, no output.
  - `check_t3.py` (scratchpad, `py -X utf8 -I`): exit 0. Brackets for 2..8 players x 60 seeds: n-1 matches, byes =
    size-n only in round 1, no bye-vs-bye, every player once; walkover/EMPTY helpers; referee team fill with fake
    clock (side locked, late team ignored, m_note), no builder -> missing side loses, both fail -> no winner; 1500 fills
    with legend/myth off respect size and rules; `partial_team` keeps valid part and fills; 2 passes then a real
    action resets, stale action dropped, 3 passes -> loss "Beto perdió 3 turnos seguidos"; both 3 -> no winner; client
    clock text; scene shows the clock and animates a turn arriving with the menu open; director: waits for go,
    walkover of a gone player, champion view; draw rule replay (same teams, `replay` flag) then coin flip.
  - `smoke_t3.py` (scratchpad, localhost sockets, timers shrunk): 4-player and 5-player tournaments to a champion
    with a lazy player (referee random fill), an AFK player (3-strike loss), a guest dropped between matches
    (walkover) and the organizer playing through a headless BattleScene whose first decision per match times out;
    all guests end with the organizer's bracket, champion and gone set, champion title; org close after the champion
    gives no error. 7/7 runs pass (seeds 1-7, ~25 s each).
  - `smoke_t2.py` updated (Enter now returns "go" instead of a test match; the rest unchanged): exit 0.
    `smoke_tour.py`, `check_rules.py`, `check_t4.py`, `check_t6.py`: exit 0 unchanged.
  - Not verified: on-screen rendering (bracket grid, highlighted match, countdown rows, picker timer line) in Windows
    Terminal; the main() wiring (`team_timeout`, `vspool`/`vserror` auto paths, director/key routing) beyond pyflakes;
    real sprite/PokeAPI downloads within the timers; real LAN.

### T5 (internet mode, MQTT relay) — done, uncommitted at time of writing
- Route: delegated writer (new pokerelay.py, pokenet.py, pokewalk.py, README.md). Size ~720 authored lines
  (pokerelay ~560 new, pokenet +120/-6, pokewalk +9/-2, README +38/-2): over the 400 heuristic because the MQTT client,
  the signed envelope/addressing layer and the Link-compatible transport only work together. Test-first exception: no
  harness for the main loop; pure helpers checked by script, transport by real-broker smokes.
- pokerelay.py (stdlib only): `BROKERS` = hivemq (prefix `PIKA`) then emqx (`EEVE`), TLS 8883. Code
  `PIKA-XXXX-XXXX`: 8 chars of Crockford base32 (no I/L/O/U) = 40 bits from `secrets`; `parse_code` is
  case-insensitive, ignores dashes/spaces, maps O->0 and I/L->1, no prefix = first broker; the prefix tells the guest
  which broker the room is on. `derive(code)`: PBKDF2-SHA256 (200k rounds, ~0.25 s) -> topic `pokewalk/r1/<hex>` +
  32-byte HMAC key (topic reveals neither code nor key). Envelope `{"f": sender, "s": seq, "b": [[to_list, msg], ...]}`
  sealed as HMAC-SHA256(32 B) + JSON; bad signature / other code / seq <= last seen from that sender = dropped silently;
  own echo ignored; a guest ignores everything until a message addressed to its own random id arrives (old host
  broadcasts can't be replayed to a newcomer).
  MQTT 3.1.1 minimal: CONNECT (clean session, keepalive 60) -> CONNACK check -> SUBSCRIBE QoS0 (sent after CONNACK) ->
  PUBLISH QoS0, PINGREQ, DISCONNECT; length encoding/decoding and packet splitting on a buffer. One thread per
  connection owns the socket (select + recv, no concurrent SSL read/write): reads, dispatches, flushes the send queue
  at most every `GAP` 0.05 s as one batch (consecutive identical messages merged into one item with all recipients,
  batches capped at 16 KB), heartbeat every `PING_EVERY` 4 s, own-echo liveness, reconnect to the same broker for up
  to `DEAD_AFTER` 15 s; closes itself (flushing the last byes) when the room is closed and no link is left.
  `RelayLink` = `pokenet.Link` interface (`q`, `send`, `poll`, `close(why)`, `alive`, `_closed`), 15 s without
  messages = `_closed` (and the peer is told), `close` sends bye + `_closed` to the peer (TCP-close equivalent).
  `RelayRoom` = `RoomHost` interface (`accept`, `set_tour`, `close`, `ips`) + `code`; host id is `"h"`, new ids are
  accepted only on a `hello`; after close a new hello gets `bye` `full` (1v1) / `started` (tournament) like LAN.
  Messages: largest is a 6-Pokémon team (~4.4 KB), a turn state ~0.8 KB.
- pokenet: Versus menu `5 Por internet` -> stage `net` (`1 Crear sala`, `2 Crear torneo`, `3 Unirse con un código`
  -> `"code"`); create reuses size -> level -> special, then `netopen` (thread `pr.open_room`) -> 1v1 `hosting` with
  the code in title/foot, or `("tournament", Tournament(..., room=RelayRoom))`. `join_code(text)` (False = invalid)
  -> thread `pr.join` -> the normal guest path (hello; handoff to `TournamentClient` when the reply has `tour`). The
  relay guest repeats its hello every 3 s and gives up after `ANSWER_SECS` 12 s with "Nadie contestó en la sala ...".
  `Tournament(name, rules, room=None)`, lobby foot shows the code. pokewalk: prompt `vscode` (invalid code stays in
  the prompt with an error). LAN paths unchanged.
- Evidence (2026-11-04):
  - `py -m py_compile pokewalk.py pokenet.py pokebattle.py pokerelay.py`: exit 0; pyflakes on the four: exit 0, no output.
  - `check_t5.py` (scratchpad, offline): exit 0, 163 checks — 3000 unique codes in format, alphabet 32 = 40 bits,
    parsing variants and rejects, derivation determinism/independence, HMAC accept / tampered body / tampered sig /
    wrong code, remaining length 0/127/128/16383/16384/2097151/2097152/268435455 (+ partial and out-of-range), publish
    roundtrip incl. byte-chunked reads and QoS1 packet id, bus logic with fake sockets: pre-sync broadcast ignored,
    accept on hello, replayed envelope dropped, batch merging, per-guest addressing, wrong-code and unsigned messages
    ignored, refusal after close, close propagation, heartbeat timeout notifies the peer, own echo ignored, big batch split.
  - `smoke_t5_vs.py` over broker.hivemq.com, 3/3 runs exit 0: menus -> room code; 3 forged hellos signed with another
    code + 1 garbage publish on the room topic ignored; unknown code -> "Nadie contestó"; guest joins typing the code in
    lowercase without dashes; handshake 2.3-8.4 s (incl. TLS connect); one-way latency median 298-309 ms, max 339 ms;
    scripted 2v2 battle to the end in 3.7-4.5 s, mirror state == flipped host state, rematch offered; buses closed.
  - `smoke_t5_tour.py` (T3 smoke with the relay; organizer via Versus menus, guests via code -> handoff), seeds 1-3:
    3/3 exit 0, ~55 s each: 4 players to a champion with random team fill, 3-strike loss, a guest whose bus is killed
    silently (heartbeat timeout 6 s in the test -> walkover), organizer turn timeouts; late joiner gets
    "El torneo ya empezó."; all guests end with the organizer's bracket/champion; organizer close -> no errors, buses closed.
  - `smoke_t5_fallback.py`: first broker unresolvable -> room opens on broker.emqx.io as `EEVE-...`, guest joins, 5/5
    echoes; both down -> "No pude conectarme al servidor de internet (...)... Probé también con ... y tampoco..."; guest
    with its broker down gets the same clear message. exit 0.
  - Regression: check_t3, smoke_t3 (seed 1), smoke_t2, smoke_tour, check_rules, check_t4, check_t6: all exit 0.
  - Observed: hivemq sometimes holds CONNACK ~7 s (seen in repeated test connects), so `CONNECT_TIMEOUT` is 15 s.
  - Not verified: two real machines on different networks/NATs; on-screen rendering of the new menus, the code in the
    hosting/lobby views and the `vscode` prompt; long sessions (reconnect path only reasoned, not forced); emqx under
    a full tournament load.
- Accepted limitation (README): messages are signed, not encrypted — anyone watching the public broker could read
  the game traffic; injection needs the code. Participants holding the code are trusted (could spoof another id).

## Next step
Delivery: commit T5, then push (user's decision, see Delivery).
