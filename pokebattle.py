"""
pokebattle - motor de batallas de pokewalk (sin IA, sin terminal).

Batallas por equipos (hasta 6 contra 6) con lo esencial de los juegos, sin objetos:
Luchar / cambiar de Pokémon, PP, prioridad, estados (parálisis, quemadura, veneno, sueño, hielo,
confusión), subidas y bajadas de estadísticas, curación, absorción, retroceso, golpes múltiples,
retroceso por amedrentar, críticos, STAB y efectividad de tipos. Cada Pokémon tiene su nivel (50 por defecto).
No hay habilidades, clima, trampas ni objetos.

Los datos se buscan en PokeAPI en el momento de armar la pelea y quedan en el caché de disco:
stats base, ataques (con sus efectos, que se leen del campo `meta` de la API, así que no hay que
programar ataque por ataque) y la tabla de tipos. Los ataques cuyo efecto no soportamos se descartan.

`fetch(url, cache_name) -> bytes` lo pasa quien llama (pokewalk.py), así este módulo no depende de él.
"""
import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

API = "https://pokeapi.co/api/v2"
LEVEL = 50
HP_MULT = 2             # con el HP de los juegos casi todo termina en 2 golpes

TYPES_ES = {
    "normal": "Normal", "fire": "Fuego", "water": "Agua", "electric": "Eléctrico", "grass": "Planta",
    "ice": "Hielo", "fighting": "Lucha", "poison": "Veneno", "ground": "Tierra", "flying": "Volador",
    "psychic": "Psíquico", "bug": "Bicho", "rock": "Roca", "ghost": "Fantasma", "dragon": "Dragón",
    "dark": "Siniestro", "steel": "Acero", "fairy": "Hada",
}
STAT_KEYS = {"hp": "hp", "attack": "atk", "defense": "def",
             "special-attack": "spa", "special-defense": "spd", "speed": "spe"}
STAGE_STATS = {k: v for k, v in STAT_KEYS.items() if k != "hp"}      # las que suben/bajan (precisión y evasión no)
STAT_ES = {"atk": ("El", "Ataque"), "def": ("La", "Defensa"), "spa": ("El", "Ataque Esp."),
           "spd": ("La", "Defensa Esp."), "spe": ("La", "Velocidad")}

AILMENTS = {"paralysis", "burn", "poison", "sleep", "freeze", "confusion"}
AILMENT_TEXT = {"paralysis": "fue paralizado", "burn": "se quemó", "poison": "fue envenenado",
                "sleep": "se durmió", "freeze": "fue congelado"}
STATUS_TAG = {"paralysis": "PAR", "burn": "QUE", "poison": "ENV", "sleep": "DOR", "freeze": "HIE"}
IMMUNE = {"paralysis": {"electric"}, "burn": {"fire"}, "poison": {"poison", "steel"}, "freeze": {"ice"}}
CRIT_CHANCE = [1 / 24, 1 / 8, 1 / 2, 1.0]
DAMAGE_CATS = {"damage", "damage+ailment", "damage+lower", "damage+raise", "damage+heal"}
FOE_TARGETS = {"selected-pokemon", "all-opponents", "random-opponent", "all-other-pokemon"}

# Se autodestruyen, necesitan dos turnos / recarga, o dependen de cosas que acá no existen.
BANNED = {
    "explosion", "self-destruct", "misty-explosion", "final-gambit", "memento", "healing-wish", "lunar-dance",
    "mind-blown", "steel-beam", "belly-drum", "struggle", "chloroblast",
    "hyper-beam", "giga-impact", "blast-burn", "frenzy-plant", "hydro-cannon", "rock-wrecker", "roar-of-time",
    "prismatic-laser", "eternabeam", "meteor-assault",
    "fly", "dig", "dive", "bounce", "solar-beam", "solar-blade", "skull-bash", "sky-attack", "razor-wind",
    "freeze-shock", "ice-burn", "shadow-force", "phantom-force", "geomancy", "meteor-beam",
    "focus-punch", "future-sight", "doom-desire", "fake-out", "first-impression", "dream-eater", "last-resort",
    "belch", "synchronoise", "poltergeist", "steel-roller", "upper-hand",
}


def pretty(name):
    return name.replace("-", " ").title()


def get_json(fetch, url, cache_name):
    return json.loads(fetch(url, cache_name).decode("utf-8"))


@dataclass
class Move:
    name: str
    type: str
    power: int                              # 0 = ataque de estado
    acc: Optional[int]                      # None = no falla
    special: bool
    pp: int = 10
    priority: int = 0
    ailment: Optional[str] = None
    ailment_chance: int = 0
    stat_changes: Tuple[Tuple[str, int], ...] = ()
    stat_self: bool = False                 # los cambios de stats van al que ataca (si no, al rival)
    stat_chance: int = 0
    drain: int = 0                          # % del daño que cura; negativo = retroceso
    healing: int = 0                        # % del HP máximo que cura (ataques de estado)
    min_hits: int = 1
    max_hits: int = 1
    flinch: int = 0
    crit: int = 0


FALLBACK_MOVE = Move("Placaje", "normal", 40, 100, False, pp=35)
STRUGGLE = Move("Forcejeo", "normal", 50, None, False, pp=1, drain=-25)


@dataclass
class Fighter:
    name: str
    dex: int
    types: List[str]
    stats: dict
    moves: List[Move]
    max_hp: int = 0
    hp: int = 0
    level: int = LEVEL
    pp: List[int] = field(default_factory=list)
    status: Optional[str] = None
    sleep: int = 0
    confused: int = 0
    flinch: bool = False
    fainted: bool = False
    stages: dict = field(default_factory=lambda: {k: 0 for k in STAT_ES})

    def __post_init__(self):
        self.max_hp = self.hp = self.stats["hp"]
        self.pp = [m.pp for m in self.moves]


# --------------------------------------------------------------------------- datos de la API
def load_move(fetch, name):
    """Un ataque que sabemos simular, o None (efecto no soportado, o falla la red)."""
    try:
        m = get_json(fetch, f"{API}/move/{name}", f"api/move-{name}.json")
    except Exception:
        return None
    cls = m["damage_class"]["name"]
    meta = m.get("meta") or {}
    cat = (meta.get("category") or {}).get("name", "")
    target = (m.get("target") or {}).get("name", "")
    power = m.get("power") or 0
    ail = (meta.get("ailment") or {}).get("name")
    ail = ail if ail in AILMENTS else None
    changes = tuple((STAGE_STATS[s["stat"]["name"]], s["change"]) for s in m.get("stat_changes", [])
                    if s["stat"]["name"] in STAGE_STATS)
    heal = meta.get("healing") or 0
    stat_self = False
    if cls == "status":
        power = 0
        if cat == "ailment" and ail and target in FOE_TARGETS:
            changes = ()
        elif cat == "net-good-stats" and changes and (target == "user" or target in FOE_TARGETS):
            ail, stat_self = None, target == "user"
        elif cat == "heal" and heal > 0 and target == "user":
            ail, changes = None, ()
        else:
            return None
    else:
        if power <= 0 or cat not in DAMAGE_CATS:
            return None
        stat_self = cat == "damage+raise"
        if cat in ("damage", "damage+heal"):
            changes = ()
        if cat != "damage+ailment":
            ail = None
        heal = 0
    es = next((n["name"] for n in m.get("names", []) if n["language"]["name"] == "es"), pretty(name))
    return Move(
        name=es, type=m["type"]["name"], power=power, acc=m.get("accuracy"), special=cls == "special",
        pp=m.get("pp") or 10, priority=m.get("priority") or 0,
        ailment=ail, ailment_chance=(meta.get("ailment_chance") or 100) if ail else 0,
        stat_changes=changes, stat_self=stat_self, stat_chance=(meta.get("stat_chance") or 100) if changes else 0,
        drain=meta.get("drain") or 0, healing=heal,
        min_hits=meta.get("min_hits") or 1, max_hits=meta.get("max_hits") or 1,
        flinch=meta.get("flinch_chance") or 0, crit=meta.get("crit_rate") or 0)


def candidate_moves(moves_json, level=LEVEL):
    """Nombres de ataques a probar: los que aprende por nivel hasta `level` (los más nuevos primero)
    más algunos de otros métodos (MT, huevo...), menos cuanto más bajo el nivel."""
    by_level, other = {}, []
    for m in moves_json:
        name = m["move"]["name"]
        if name in BANNED:
            continue
        best = None
        for d in m["version_group_details"]:
            if d["move_learn_method"]["name"] == "level-up" and d["level_learned_at"] <= level:
                best = d["level_learned_at"] if best is None else max(best, d["level_learned_at"])
        if best is None:
            other.append(name)
        else:
            by_level[name] = best
    top = sorted(by_level, key=by_level.get, reverse=True)[:12]
    extra = 14 if level >= 40 else 8 if level >= 25 else 3      # a nivel bajo casi no tiene MT
    return top + random.sample(other, min(extra, len(other)))


def choose_moves(moves, types, n=4):
    """Hasta 3 ataques de daño (los mejores por poder x precisión, con bonus STAB y algo de azar)
    y 1 de estado al azar, como un set de juego. Sin de estado, 4 de daño."""
    seen, unique = set(), []
    for m in moves:
        if m.name not in seen:
            seen.add(m.name)
            unique.append(m)
    dmg = [m for m in unique if m.power]
    status = [m for m in unique if not m.power]
    dmg.sort(key=lambda m: m.power * (m.acc or 100) / 100 * (1.5 if m.type in types else 1.0)
             * random.uniform(0.85, 1.15), reverse=True)
    if not dmg:
        dmg = [FALLBACK_MOVE]
    picked = dmg[:n - 1] if status else dmg[:n]
    if status:                                       # los que provocan estados o suben stats, más seguido
        w = [3 if m.ailment else 2 if m.stat_changes and m.stat_self else 1 for m in status]
        picked.append(random.choices(status, w)[0])
    return picked


def calc_stats(base, level=LEVEL):
    """Stats con IV 31 y EV 84 (como un equipo competitivo promedio). El HP va multiplicado por
    HP_MULT: con el HP de los juegos casi todo termina en 2 golpes."""
    out = {"hp": ((2 * base["hp"] + 31 + 21) * level // 100 + level + 10) * HP_MULT}
    for k in ("atk", "def", "spa", "spd", "spe"):
        out[k] = (2 * base[k] + 31 + 21) * level // 100 + 5
    return out


def load_fighter(fetch, dex, key, level=LEVEL):
    p = get_json(fetch, f"{API}/pokemon/{dex}", f"api/pokemon-{dex}.json")
    base = {STAT_KEYS[s["stat"]["name"]]: s["base_stat"] for s in p["stats"]}
    types = [t["type"]["name"] for t in p["types"]]
    with ThreadPoolExecutor(8) as ex:
        found = [m for m in ex.map(lambda n: load_move(fetch, n), candidate_moves(p["moves"], level)) if m]
    return Fighter(pretty(key), dex, types, calc_stats(base, level), choose_moves(found, types), level=level)


def describe(fetch, dex, key):
    """Una línea con tipo y stats base (tecla `i`)."""
    p = get_json(fetch, f"{API}/pokemon/{dex}", f"api/pokemon-{dex}.json")
    st = {STAT_KEYS[s["stat"]["name"]]: s["base_stat"] for s in p["stats"]}
    types = "/".join(TYPES_ES.get(t["type"]["name"], t["type"]["name"]) for t in p["types"])
    return (f"{pretty(key)} #{dex} · {types} · HP {st['hp']}  Atq {st['atk']}  Def {st['def']}  "
            f"AtqE {st['spa']}  DefE {st['spd']}  Vel {st['spe']}  · {p['height'] / 10:g} m  {p['weight'] / 10:g} kg")


class TypeChart:
    """Efectividad de tipos, buscada en la API una sola vez por tipo atacante."""

    def __init__(self, fetch):
        self.fetch = fetch
        self.rel = {}

    def load(self, t):
        if t not in self.rel:
            j = get_json(self.fetch, f"{API}/type/{t}", f"api/type-{t}.json")["damage_relations"]
            self.rel[t] = {k: {x["name"] for x in j[f"{k}_damage_to"]} for k in ("double", "half", "no")}
        return self.rel[t]

    def preload(self, types):
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(self.load, sorted(types | {"normal"})))

    def mult(self, atk, defenders):
        if atk not in TYPES_ES:                  # ataque sin tipo (el golpe por confusión)
            return 1.0
        r, m = self.load(atk), 1.0
        for d in defenders:
            m *= 0.0 if d in r["no"] else 2.0 if d in r["double"] else 0.5 if d in r["half"] else 1.0
        return m


# --------------------------------------------------------------------------- combate
def stage_mult(n):
    return (2 + n) / 2 if n >= 0 else 2 / (2 - n)


@dataclass
class Step:
    """Un paso de la animación de un turno."""
    text: str
    atk: Optional[int] = None        # lado que embiste (0 = vos, 1 = rival)
    snap: Optional[tuple] = None     # estado de ambos lados después de este paso (ver Battle.snap)
    faint: Optional[int] = None      # lado que se debilita
    dur: float = 1.0
    move: Optional[Move] = None      # ataque usado (elige la animación y el efecto)
    tgt: Optional[int] = None        # lado sobre el que cae el efecto
    miss: bool = False               # el ataque no llega (falló o no afecta)


class Battle:
    """Dos equipos. Lado 0 = vos, lado 1 = rival (lo maneja `ai_action`)."""

    def __init__(self, team_a, team_b, chart):
        self.teams = [team_a, team_b]
        self.chart = chart
        self.active = [0, 0]
        self.over = False
        self.winner = None
        self.pending = [False, False]        # el lado tiene que elegir reemplazo

    # --- consultas
    def fighter(self, s):
        return self.teams[s][self.active[s]]

    def alive(self, s):
        return [i for i, f in enumerate(self.teams[s]) if f.hp > 0]

    def lab(self, s):
        f = self.fighter(s)
        return f.name if s == 0 else f"{f.name} rival"

    def snap(self):
        """(índice activo, estado, confuso, HP de todo el equipo) por lado."""
        return tuple((self.active[s], self.fighter(s).status, self.fighter(s).confused > 0,
                      tuple(f.hp for f in self.teams[s])) for s in (0, 1))

    def step(self, text, **kw):
        return Step(text, snap=self.snap(), **kw)

    def speed(self, f):
        v = f.stats["spe"] * stage_mult(f.stages["spe"])
        return v * 0.5 if f.status == "paralysis" else v

    def move_of(self, s, i):
        return STRUGGLE if i < 0 else self.fighter(s).moves[i]

    def exp_frac(self, att, dfn, mv):
        """Daño esperado de `mv` como fracción del HP máximo del que lo recibe."""
        if not mv.power:
            return 0.0
        ak, dk = ("spa", "spd") if mv.special else ("atk", "def")
        base = ((2 * att.level // 5 + 2) * mv.power * att.stats[ak] / dfn.stats[dk]) / 50 + 2
        stab = 1.5 if mv.type in att.types else 1.0
        eff = self.chart.mult(mv.type, dfn.types)
        return base * stab * eff * (mv.acc or 100) / 100 * 0.925 / max(1, dfn.max_hp)

    # --- IA (reglas, sin modelos): la usa el rival, y el piloto automático por el lado 0
    def _score(self, f, foe, mv):
        if mv.power:
            return min(self.exp_frac(f, foe, mv), foe.hp / foe.max_hp)
        if mv.ailment:
            if mv.ailment == "confusion":
                ok = not foe.confused
            else:
                ok = not foe.status and not set(foe.types) & IMMUNE.get(mv.ailment, set())
            return 0.30 if ok and foe.hp / foe.max_hp > 0.4 else 0.0
        if mv.healing:
            return 0.55 if f.hp / f.max_hp < 0.5 else 0.0
        if mv.stat_changes:
            k = mv.stat_changes[0][0]
            if mv.stat_self:
                return 0.22 if f.hp / f.max_hp > 0.5 and f.stages[k] < 2 else 0.0
            return 0.15 if foe.stages[k] > -2 else 0.0
        return 0.0

    def matchup(self, f, foe):
        """Qué tan bien le va `f` contra `foe`: lo que le pega menos lo que recibe."""
        off = max((self.exp_frac(f, foe, m) for m in f.moves), default=0.0)
        dfn = max((self.exp_frac(foe, f, m) for m in foe.moves), default=0.0)
        return off - dfn

    def best_replacement(self, s):
        foe = self.fighter(1 - s)
        cands = [i for i in self.alive(s) if i != self.active[s]]
        return max(cands, key=lambda i: self.matchup(self.teams[s][i], foe)) if cands else None

    def ai_action(self, s):
        f, foe = self.fighter(s), self.fighter(1 - s)
        usable = [i for i, _ in enumerate(f.moves) if f.pp[i] > 0]
        if not usable:
            return ("move", -1)
        cand = self.best_replacement(s)
        if cand is not None and random.random() < 0.6:
            best_off = max(self.exp_frac(f, foe, f.moves[i]) for i in usable)
            threat = max((self.exp_frac(foe, f, m) for m in foe.moves), default=0.0)
            if best_off < 0.18 and threat > 0.35 and \
                    self.matchup(self.teams[s][cand], foe) > self.matchup(f, foe) + 0.2:
                return ("switch", cand)
        if random.random() < 0.2:
            return ("move", random.choice(usable))
        return ("move", max(usable, key=lambda i: self._score(f, foe, f.moves[i]) + random.uniform(0, 0.03)))

    # --- turnos
    def turn(self, action):
        """Resuelve un turno completo ("move", i) / ("switch", i) y devuelve los pasos para animarlo."""
        acts = [action, self.ai_action(1)]
        for s in (0, 1):
            self.fighter(s).flinch = False
        steps = []
        for s in sorted((s for s in (0, 1) if acts[s][0] == "switch"), key=lambda s: -self.speed(self.fighter(s))):
            steps += self._switch(s, acts[s][1])
        movers = [s for s in (0, 1) if acts[s][0] == "move"]
        movers.sort(key=lambda s: (self.move_of(s, acts[s][1]).priority, self.speed(self.fighter(s)),
                                   random.random()), reverse=True)
        for s in movers:
            if self.over:
                break
            if self.fighter(s).hp > 0:
                steps += self._use(s, self.move_of(s, acts[s][1]), acts[s][1])
        for s in sorted((0, 1), key=lambda s: -self.speed(self.fighter(s))):
            if not self.over:
                steps += self._residual(s)
        steps += self._replacements()
        return steps

    def send(self, idx):
        """Reemplazo elegido por el jugador después de que se debilita su Pokémon."""
        self.pending[0] = False
        return self._switch(0, idx, forced=True)

    def _replacements(self):
        steps = []
        if self.over:
            return steps
        for s in (1, 0):
            if self.fighter(s).hp <= 0:
                if s == 1:
                    steps += self._switch(1, self.best_replacement(1), forced=True)
                else:
                    self.pending[0] = True
        return steps

    def _switch(self, s, idx, forced=False):
        old, steps = self.fighter(s), []
        if not forced and old.hp > 0:
            steps.append(self.step(f"¡Vuelve {old.name}!" if s == 0 else f"¡El rival retira a {old.name}!", dur=1.0))
        old.stages = {k: 0 for k in STAT_ES}
        old.confused, old.flinch = 0, False
        self.active[s] = idx
        new = self.fighter(s)
        steps.append(self.step(f"¡Adelante, {new.name}!" if s == 0 else f"¡El rival envía a {new.name}!", dur=1.3))
        return steps

    def _check_faints(self, att_side=None):
        steps = []
        for s in (0, 1):
            f = self.fighter(s)
            if f.hp <= 0 and not f.fainted:
                f.fainted = True
                steps.append(self.step(f"¡{self.lab(s)} se debilitó!", faint=s, dur=1.5))
        if not self.over:
            wiped = [s for s in (0, 1) if not self.alive(s)]
            if wiped:
                self.over = True
                self.winner = 1 - wiped[0] if len(wiped) == 1 else (None if att_side is None else 1 - att_side)
                steps.append(self.step("¡Ganó tu equipo!" if self.winner == 0 else "¡Ganó el equipo rival!", dur=1.8))
        return steps

    def _can_move(self, s, steps):
        f, lab = self.fighter(s), self.lab(s)
        if f.flinch:
            f.flinch = False
            steps.append(self.step(f"¡{lab} se amedrentó!", dur=0.9))
            return False
        if f.status == "sleep":
            f.sleep -= 1
            if f.sleep > 0:
                steps.append(self.step(f"¡{lab} está dormido como un tronco!", dur=1.0))
                return False
            f.status = None
            steps.append(self.step(f"¡{lab} se despertó!", dur=1.0))
        elif f.status == "freeze":
            if random.random() < 0.2:
                f.status = None
                steps.append(self.step(f"¡{lab} se descongeló!", dur=1.0))
            else:
                steps.append(self.step(f"¡{lab} está congelado!", dur=1.0))
                return False
        if f.status == "paralysis" and random.random() < 0.25:
            steps.append(self.step(f"¡{lab} está paralizado! ¡No se puede mover!", dur=1.0))
            return False
        if f.confused:
            f.confused -= 1
            if f.confused == 0:
                steps.append(self.step(f"¡{lab} ya no está confuso!", dur=0.9))
            elif random.random() < 1 / 3:
                dmg, _ = self._damage(f, f, Move("confusión", "???", 40, None, False), 1.0, crit_ok=False)
                f.hp = max(0, f.hp - dmg)
                steps.append(self.step(f"¡{lab} está tan confuso que se hirió a sí mismo!", dur=1.2))
                steps += self._check_faints(att_side=s)
                return False
        return True

    def _damage(self, att, dfn, mv, eff, crit_ok=True):
        crit = crit_ok and random.random() < CRIT_CHANCE[min(3, mv.crit)]
        ak, dk = ("spa", "spd") if mv.special else ("atk", "def")
        a_st, d_st = att.stages[ak], dfn.stages[dk]
        if crit:                                     # el crítico ignora lo que te perjudica
            a_st, d_st = max(0, a_st), min(0, d_st)
        a = att.stats[ak] * stage_mult(a_st)
        d = dfn.stats[dk] * stage_mult(d_st)
        base = ((2 * att.level // 5 + 2) * mv.power * a / d) / 50 + 2
        if att.status == "burn" and not mv.special:
            base *= 0.5
        stab = 1.5 if mv.type in att.types else 1.0
        dmg = int(base * stab * eff * random.uniform(0.85, 1.0) * (1.5 if crit else 1.0))
        return (max(1, dmg) if eff > 0 else 0), crit

    def _use(self, s, mv, idx):
        att, dfn, o = self.fighter(s), self.fighter(1 - s), 1 - s
        steps = []
        if not self._can_move(s, steps):
            return steps
        if idx >= 0:
            att.pp[idx] -= 1
        announce = f"¡{self.lab(s)} usó {mv.name}!"
        if mv.acc is not None and random.randint(1, 100) > mv.acc:
            return steps + [self.step(announce, atk=s, dur=1.2, move=mv, tgt=o, miss=True),
                            self.step("¡Pero falló!", dur=0.9)]
        if not mv.power:
            on_self = bool(mv.healing or (mv.stat_changes and mv.stat_self))
            steps.append(self.step(announce, atk=s, dur=1.2, move=mv, tgt=s if on_self else o))
            steps += self._status_move(s, mv)
            return steps + self._check_faints(att_side=s)
        eff = self.chart.mult(mv.type, dfn.types)
        if eff == 0:
            return steps + [self.step(announce, atk=s, dur=1.2, move=mv, tgt=o, miss=True),
                            self.step(f"No afecta a {self.lab(o)}...", dur=1.0)]

        lo, hi = mv.min_hits, mv.max_hits
        hits = 1 if hi <= 1 else (random.choices([2, 3, 4, 5], [35, 35, 15, 15])[0] if (lo, hi) == (2, 5)
                                  else random.randint(lo, hi))
        total, done, crit_any = 0, 0, False
        for h in range(hits):
            dmg, crit = self._damage(att, dfn, mv, eff)
            dfn.hp = max(0, dfn.hp - dmg)
            total, done, crit_any = total + dmg, done + 1, crit_any or crit
            steps.append(self.step(announce if h == 0 else "¡Otro golpe!", atk=s, dur=1.3 if h == 0 else 0.7,
                                   move=mv, tgt=o))
            if dfn.hp == 0:
                break
        if done > 1:
            steps.append(self.step(f"¡Golpeó {done} veces!", dur=0.9))
        if eff > 1:
            steps.append(self.step("¡Es supereficaz!", dur=1.0))
        elif eff < 1:
            steps.append(self.step("No es muy eficaz...", dur=1.0))
        if crit_any:
            steps.append(self.step("¡Golpe crítico!", dur=0.9))
        if mv.drain > 0 and total:
            att.hp = min(att.max_hp, att.hp + max(1, total * mv.drain // 100))
            steps.append(self.step(f"¡{self.lab(s)} absorbió energía!", dur=1.0))
        elif mv.drain < 0 and total:
            att.hp = max(0, att.hp - max(1, total * -mv.drain // 100))
            steps.append(self.step(f"¡{self.lab(s)} se hirió por el retroceso!", dur=1.0))
        if dfn.hp > 0:
            if mv.ailment and random.randint(1, 100) <= mv.ailment_chance:
                steps += self._inflict(o, mv.ailment, forced=False)
            if mv.stat_changes and not mv.stat_self and random.randint(1, 100) <= mv.stat_chance:
                steps += [x for k, c in mv.stat_changes for x in self._stage(o, k, c)]
            if mv.flinch and random.randint(1, 100) <= mv.flinch:
                dfn.flinch = True
        if mv.stat_changes and mv.stat_self and att.hp > 0 and random.randint(1, 100) <= mv.stat_chance:
            steps += [x for k, c in mv.stat_changes for x in self._stage(s, k, c)]
        return steps + self._check_faints(att_side=s)

    def _status_move(self, s, mv):
        o, steps = 1 - s, []
        if mv.ailment:
            steps += self._inflict(o, mv.ailment, forced=True)
        if mv.stat_changes:
            tgt = s if mv.stat_self else o
            steps += [x for k, c in mv.stat_changes for x in self._stage(tgt, k, c)]
        if mv.healing:
            f = self.fighter(s)
            if f.hp >= f.max_hp:
                steps.append(self.step(f"¡{self.lab(s)} ya tiene el HP al máximo!", dur=1.0))
            else:
                f.hp = min(f.max_hp, f.hp + f.max_hp * mv.healing // 100)
                steps.append(self.step(f"¡{self.lab(s)} recuperó salud!", dur=1.1))
        return steps

    def _inflict(self, t, ail, forced):
        f, lab = self.fighter(t), self.lab(t)
        if ail == "confusion":
            if f.confused:
                return [self.step(f"¡{lab} ya está confuso!", dur=1.0)] if forced else []
            f.confused = random.randint(2, 4)
            return [self.step(f"¡{lab} está confuso!", dur=1.1)]
        if f.status or set(f.types) & IMMUNE.get(ail, set()):
            return [self.step("¡Pero falló!", dur=0.9)] if forced else []
        f.status = ail
        if ail == "sleep":
            f.sleep = random.randint(1, 3)
        return [self.step(f"¡{lab} {AILMENT_TEXT[ail]}!", dur=1.1)]

    def _stage(self, t, stat, change):
        f, lab = self.fighter(t), self.lab(t)
        art, nom = STAT_ES[stat]
        new = max(-6, min(6, f.stages[stat] + change))
        if new == f.stages[stat]:
            return [self.step(f"¡{art} {nom} de {lab} no puede {'subir' if change > 0 else 'bajar'} más!", dur=1.0)]
        f.stages[stat] = new
        how = ("subió" if change > 0 else "bajó") + (" mucho" if abs(change) >= 2 else "")
        return [self.step(f"¡{art} {nom} de {lab} {how}!", dur=1.1)]

    def _residual(self, s):
        f = self.fighter(s)
        if f.hp <= 0 or f.status not in ("burn", "poison"):
            return []
        burn = f.status == "burn"
        f.hp = max(0, f.hp - max(1, f.max_hp // (16 if burn else 8)))
        text = f"¡{self.lab(s)} sufre quemaduras!" if burn else f"¡{self.lab(s)} sufre el veneno!"
        return [self.step(text, dur=1.0)] + self._check_faints()
