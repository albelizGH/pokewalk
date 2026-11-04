# Feature: ranges when adding + versus room rules with level handicap

Branch: `feat/region-picker-and-versus-rules`

## Objective
Add Pokémon faster with dex ranges, and make versus fair: the room host fixes team size and level, and each
Pokémon gets a small random level handicap for fun.

## Scope / acceptance criteria
- T1 Ranges: in `a` (add) and `c` (set), a term `N-M` (both numbers) expands to dex N..M. At most 10 Pokémon per
  action; extra ones are dropped with a notice. Hyphenated names (`ho-oh`, `mr-mime`) keep working.
- T2 Room rules: the host picks team size (1-6) and level (1-100) before opening the room. The guest sees them.
  Both teams are validated against the team size. In versus the player types only names; `name:level` is no
  longer accepted (clear error).
- T3 Handicap: every versus Pokémon gets level = room level + random(-5..+5), clamped to 1-100. Rationale: at
  level 50, ±5 is ~10% stat swing: noticeable but not decisive (±10 can mean 40 vs 60, ~50% more stats).

## Constraints
- Code comments and UI copy follow the project's existing Spanish.
- No automated test suite exists; checks are `py -m py_compile` plus targeted scripted checks of pure helpers.

## Tasks
- [x] T1 Ranges in add/set, capped at 10 — route: inline (one file, small)
- [ ] T2 Room rules (team size + level) in pokenet.py / pokewalk.py — route: delegated writer (2 non-trivial files)
- [ ] T3 Level handicap ±5 in versus — route: delegated writer (same unit as T2)

## Progress / evidence
- Prior work committed: 4192673 (regional picker with Tab).

## Next step
T1.
