"""
pokebattle - motor de batallas de pokewalk (sin IA, sin terminal).

Los datos se buscan en PokeAPI en el momento de armar la pelea (y quedan en el caché de disco,
así la segunda vez es instantánea): stats base, ataques con su poder/precisión y la tabla de tipos.
Combate a nivel 50 con la fórmula de daño de los juegos (STAB, efectividad de tipos, críticos,
variación aleatoria). Solo ataques que hacen daño; sin estados, clima ni objetos.

`fetch(url, cache_name) -> bytes` lo pasa quien llama (pokewalk.py), así este módulo no depende de él.
"""
import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Tuple

API = "https://pokeapi.co/api/v2"
LEVEL = 50
HP_MULT = 2

TYPES_ES = {
    "normal": "Normal", "fire": "Fuego", "water": "Agua", "electric": "Eléctrico", "grass": "Planta",
    "ice": "Hielo", "fighting": "Lucha", "poison": "Veneno", "ground": "Tierra", "flying": "Volador",
    "psychic": "Psíquico", "bug": "Bicho", "rock": "Roca", "ghost": "Fantasma", "dragon": "Dragón",
    "dark": "Siniestro", "steel": "Acero", "fairy": "Hada",
}
STAT_KEYS = {"hp": "hp", "attack": "atk", "defense": "def",
             "special-attack": "spa", "special-defense": "spd", "speed": "spe"}
# Se autodestruyen o dependen de cosas que acá no existen: mejor no sortearlos.
BANNED = {"explosion", "self-destruct", "misty-explosion", "final-gambit", "memento", "healing-wish",
          "lunar-dance", "mind-blown", "steel-beam", "belly-drum", "struggle"}


def pretty(name):
    return name.replace("-", " ").title()


def get_json(fetch, url, cache_name):
    return json.loads(fetch(url, cache_name).decode("utf-8"))


@dataclass
class Move:
    name: str
    type: str
    power: int
    acc: int
    special: bool


FALLBACK_MOVE = Move("Placaje", "normal", 40, 100, False)


@dataclass
class Fighter:
    name: str
    dex: int
    types: List[str]
    stats: dict
    moves: List[Move]
    max_hp: int = 0
    hp: int = 0

    def __post_init__(self):
        self.max_hp = self.hp = self.stats["hp"]


# --------------------------------------------------------------------------- datos de la API
def load_move(fetch, name):
    """Un ataque que hace daño, o None (de estado, sin poder fijo, o si falla la red)."""
    try:
        m = get_json(fetch, f"{API}/move/{name}", f"api/move-{name}.json")
    except Exception:
        return None
    if not m.get("power") or m["damage_class"]["name"] == "status":
        return None
    es = next((n["name"] for n in m.get("names", []) if n["language"]["name"] == "es"), pretty(name))
    return Move(es, m["type"]["name"], m["power"], m.get("accuracy") or 100,
                m["damage_class"]["name"] == "special")


def candidate_moves(moves_json):
    """Nombres de ataques a probar: los que aprende por nivel hasta el 50 (los más nuevos primero)
    más algunos de otros métodos (MT, huevo...). Con eso se arman 4 al azar pero con criterio."""
    by_level, other = {}, []
    for m in moves_json:
        name = m["move"]["name"]
        if name in BANNED:
            continue
        best = None
        for d in m["version_group_details"]:
            if d["move_learn_method"]["name"] == "level-up" and d["level_learned_at"] <= LEVEL:
                best = d["level_learned_at"] if best is None else max(best, d["level_learned_at"])
        if best is None:
            other.append(name)
        else:
            by_level[name] = best
    top = sorted(by_level, key=by_level.get, reverse=True)[:10]
    return top + random.sample(other, min(6, len(other)))


def choose_moves(moves, types, n=4):
    """Los n mejores por poder x precisión (con bonus STAB) y un poco de azar para que varíe."""
    seen, unique = set(), []
    for m in moves:
        if m.name not in seen:
            seen.add(m.name)
            unique.append(m)
    unique.sort(key=lambda m: m.power * m.acc / 100 * (1.5 if m.type in types else 1.0)
                * random.uniform(0.85, 1.15), reverse=True)
    return unique[:n] or [FALLBACK_MOVE]


def calc_stats(base):
    """Stats a nivel 50 con IV 31 y EV 84 (como un equipo competitivo promedio).
    El HP va multiplicado por HP_MULT: con el HP de los juegos todo termina en 2 golpes."""
    out = {"hp": ((2 * base["hp"] + 31 + 21) * LEVEL // 100 + LEVEL + 10) * HP_MULT}
    for k in ("atk", "def", "spa", "spd", "spe"):
        out[k] = (2 * base[k] + 31 + 21) * LEVEL // 100 + 5
    return out


def load_fighter(fetch, dex, key):
    p = get_json(fetch, f"{API}/pokemon/{dex}", f"api/pokemon-{dex}.json")
    base = {STAT_KEYS[s["stat"]["name"]]: s["base_stat"] for s in p["stats"]}
    types = [t["type"]["name"] for t in p["types"]]
    with ThreadPoolExecutor(8) as ex:
        found = [m for m in ex.map(lambda n: load_move(fetch, n), candidate_moves(p["moves"])) if m]
    return Fighter(pretty(key), dex, types, calc_stats(base), choose_moves(found, types))


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
            list(ex.map(self.load, sorted(types)))

    def mult(self, atk, defenders):
        r, m = self.load(atk), 1.0
        for d in defenders:
            m *= 0.0 if d in r["no"] else 2.0 if d in r["double"] else 0.5 if d in r["half"] else 1.0
        return m


# --------------------------------------------------------------------------- combate
@dataclass
class Step:
    """Un paso de la animación de un turno."""
    text: str
    atk: Optional[int] = None                 # quién embiste (0 = vos, 1 = rival)
    hp: Optional[Tuple[int, int]] = None      # HP de ambos tras el golpe
    faint: Optional[int] = None               # quién se debilita
    dur: float = 1.0


class Battle:
    def __init__(self, a, b, chart):
        self.f = [a, b]
        self.chart = chart
        self.winner = None

    def _hit(self, i, mv):
        att, dfn = self.f[i], self.f[1 - i]
        a = att.stats["spa" if mv.special else "atk"]
        d = dfn.stats["spd" if mv.special else "def"]
        base = ((2 * LEVEL // 5 + 2) * mv.power * a // d) // 50 + 2
        stab = 1.5 if mv.type in att.types else 1.0
        eff = self.chart.mult(mv.type, dfn.types)
        return base, stab, eff

    def ai_move(self, i):
        """Casi siempre el de mayor daño esperado; a veces uno al azar para no ser predecible."""
        moves = self.f[i].moves
        if random.random() < 0.25:
            return random.choice(moves)

        def expected(mv):
            base, stab, eff = self._hit(i, mv)
            return base * stab * eff * mv.acc / 100
        return max(moves, key=expected)

    def turn(self, mine):
        """Resuelve un turno completo y devuelve los pasos para animarlo."""
        choices = [mine, self.ai_move(1)]
        order = sorted((0, 1), key=lambda i: (self.f[i].stats["spe"], random.random()), reverse=True)
        steps = []
        for i in order:
            if self.winner is not None:
                break
            att, dfn, mv = self.f[i], self.f[1 - i], choices[i]
            announce = f"¡{att.name} usó {mv.name}!"
            if random.randint(1, 100) > mv.acc:
                steps += [Step(announce, atk=i, dur=1.2), Step("¡Pero falló!", dur=0.9)]
                continue
            base, stab, eff = self._hit(i, mv)
            crit = random.random() < 1 / 16
            dmg = int(base * stab * eff * random.uniform(0.85, 1.0) * (1.5 if crit else 1.0))
            if eff > 0:
                dmg = max(1, dmg)
            dfn.hp = max(0, dfn.hp - dmg)
            steps.append(Step(announce, atk=i, hp=(self.f[0].hp, self.f[1].hp), dur=1.3))
            if eff == 0:
                steps.append(Step(f"No afecta a {dfn.name}...", dur=1.0))
            elif eff > 1:
                steps.append(Step("¡Es supereficaz!", dur=1.0))
            elif eff < 1:
                steps.append(Step("No es muy eficaz...", dur=1.0))
            if crit and eff > 0:
                steps.append(Step("¡Golpe crítico!", dur=0.9))
            if dfn.hp == 0:
                self.winner = i
                steps += [Step(f"¡{dfn.name} se debilitó!", faint=1 - i, dur=1.5),
                          Step(f"¡Ganó {att.name}!", dur=1.5)]
        return steps
