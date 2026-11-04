"""
pokenet - batallas de pokewalk contra un amigo en la misma red local.

Cómo funciona:
  * Sala: uno crea una sala (escucha por TCP y avisa por broadcast UDP "acá hay una sala") y el otro
    la ve en su lista, o se conecta escribiendo la IP. Ya conectados, cada uno arma su equipo y se lo
    manda al otro (con sus stats y ataques ya sorteados, así los dos tienen exactamente los mismos datos).
  * Batalla: el que creó la sala es el árbitro. Cada turno, cada uno manda solo su elección ("luchar 2",
    "cambiar 3"); el árbitro resuelve el turno con el mismo `pokebattle.Battle` de siempre (todo el azar
    se tira en un solo lado, así no hay desincronización) y le devuelve a los dos los pasos para animarlo.
    El invitado juega sobre una copia espejo de la batalla (él es el lado 0) que se actualiza con el estado
    que manda el árbitro al final de cada turno.

  * Torneo: el organizador abre una sala de torneo (la misma sala, con más lugares) y entran hasta 7 amigos;
    cada uno tiene una sola conexión, con la compu del organizador (estrella). Ver `Tournament`. El organizador
    arbitra todas las peleas (`MatchReferee`), las juegue o no, y todos los que no pelean las miran en vivo. El
    cuadro (eliminación simple, con byes) y los relojes los lleva `TourDirector`, también en su compu.

  * Por internet: lo mismo, pero en vez de TCP directo los mensajes pasan por un servidor MQTT público
    (`pokerelay`: la sala tiene un código, y `RelayLink` / `RelayRoom` hacen de `Link` / `RoomHost`).

Protocolo: una línea de JSON por mensaje, sobre TCP. Nada de esto es seguro contra un rival tramposo
(los equipos los arma cada uno): está pensado para jugar con amigos.
"""
import base64
import hashlib
import json
import math
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pokebattle as pb
import pokerelay as pr

PROTO = 2               # 2: salas de torneo (un pokewalk viejo no las entendería: ni las ve ni entra)
TCP_PORT = 5757         # puerto de la sala (si está ocupado se usa otro, y el beacon avisa cuál)
UDP_PORT = 5758         # puerto donde se anuncian las salas
MAX_TEAM = 6
MAX_PLAYERS = 8         # jugadores de un torneo, contando al organizador
MIN_PLAYERS = 3         # para empezar un torneo
# relojes del torneo (los manda el árbitro: un cliente tocado no puede trabar la pelea)
TEAM_SECS = 60          # s para armar el equipo; al vencer, el cliente manda lo que tenga (y el resto al azar)
TEAM_GRACE = 20         # s más que espera el árbitro (para que ese equipo se baje); después lo completa él al azar
TURN_SECS = 60          # s para cada decisión de la pelea (más lo que tarda en animarse el turno anterior)
WARN_SECS = 15          # el reloj se muestra en los últimos segundos
MAX_PASSES = 3          # turnos perdidos seguidos (por tiempo) para perder la pelea
STEP_SECS = 2.5         # s que se le dan a la pantalla para animar cada paso de un turno
ANIM_MAX = 40           # tope de ese margen
START_GRACE = 20        # s más para la primera decisión (se bajan los sprites)
NEXT_SECS = 12          # s entre una pelea y la siguiente (todos ven el cuadro)
EMPTY = -1              # en el cuadro: nadie (un bye de la 1ª ronda, o se fueron los dos)
BEACON_EVERY = 1.0      # s entre avisos de la sala
ROOM_TTL = 7.0          # s sin verse para que una sala desaparezca de la lista
SCAN_EVERY = 2.0        # s entre un escaneo de la subred y el siguiente
PROBE_TIMEOUT = 0.35    # s para que un equipo conteste a la prueba de sala (en una LAN responden en milisegundos)
PENDING_TTL = 5.0       # s que se espera el primer mensaje de una conexión entrante
PING_EVERY = 5.0        # s de silencio para mandar un latido
DEAD_AFTER = 30.0       # s sin recibir nada para dar por perdida la conexión
MAX_BUF = 1 << 20       # tope de una línea sin terminar


def player_name():
    name = os.environ.get("USERNAME") or os.environ.get("USER") or socket.gethostname() or "Entrenador"
    return _text(name, 20) or "Entrenador"


def _text(s, n):
    """Solo caracteres imprimibles (los de control se usan como marcadores en los textos de batalla)."""
    return "".join(c for c in str(s) if c.isprintable())[:n].strip()


def _int(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = lo
    return max(lo, min(hi, n))


def room_rules(raw=None):
    """Reglas de la sala, saneadas: {"size": 1..MAX_TEAM, "level": 1..100, "legend": bool, "myth": bool}. Lo que
    falte o venga roto queda en lo de siempre (equipo de MAX_TEAM, nivel pb.LEVEL, legendarios y míticos
    permitidos), para no caerse con un mensaje incompleto (o de una versión que no los conoce)."""
    raw = raw if isinstance(raw, dict) else {}
    size = _int(raw["size"], 1, MAX_TEAM) if "size" in raw else MAX_TEAM
    level = _int(raw["level"], 1, 100) if "level" in raw else pb.LEVEL
    return {"size": size, "level": level, "legend": raw.get("legend") is not False,
            "myth": raw.get("myth") is not False}


def rules_text(rules):
    """"3 Pokémon · nivel 50", más " · sin legendarios" / " · sin míticos" si la sala no los deja."""
    r = room_rules(rules)
    return (f"{r['size']} Pokémon · nivel {r['level']}" + ("" if r["legend"] else " · sin legendarios")
            + ("" if r["myth"] else " · sin míticos"))


# Legendarios (los de la historia, uno por juego) y míticos (de evento). `species_flags` lo pone pokewalk.py: una
# función que devuelve {dex: "legend" | "myth"} (de pokemon_species.csv de PokeAPI). Sin ella no se marca ninguno.
species_flags = None
SPECIAL_ES = {"legend": "legendario", "myth": "mítico"}


def special_kind(dex):
    """"legend", "myth" o None para ese número de Pokédex."""
    try:
        return species_flags().get(dex) if species_flags else None
    except (OSError, ValueError, KeyError):
        return None


def rule_problem(dex, name, rules):
    """Texto de por qué ese Pokémon no entra en la sala ("Mewtwo es legendario: esta sala no los permite"), o
    None si entra."""
    kind = special_kind(dex)
    if kind and not room_rules(rules)[kind]:
        return f"{name} es {SPECIAL_ES[kind]}: esta sala no los permite"
    return None


def team_problem(team, rules):
    """El primer problema de un equipo (lista de pb.Fighter) con las reglas de la sala, o None si está bien."""
    if len(team) > room_rules(rules)["size"]:
        return "más Pokémon que los de la sala"
    return next((p for p in (rule_problem(f.dex, f.name, rules) for f in team) if p), None)


def tour_info(raw):
    """Datos de torneo de un aviso o una prueba de sala, saneados: {"n": jugadores, "max": lugares}, o None si es
    una sala de 1 contra 1."""
    if not isinstance(raw, dict):
        return None
    top = _int(raw.get("max"), 2, MAX_PLAYERS)
    return {"n": _int(raw.get("n"), 1, top), "max": top}


def room_label(r):
    """Cómo se ve una sala en la lista: "Ana (192.168.1.5) · 3 Pokémon · nivel 50" o, si es un torneo,
    "Torneo de Ana (192.168.1.5) · 3/8 · 3 Pokémon · nivel 50"."""
    tour = r.get("tour")
    if not tour:
        return f"{r['name']} ({r['ip']}) · {rules_text(r['rules'])}"
    full = " (lleno)" if tour["n"] >= tour["max"] else ""
    return f"Torneo de {r['name']} ({r['ip']}) · {tour['n']}/{tour['max']}{full} · {rules_text(r['rules'])}"


_BYE_WHY = {"full": "La sala está llena.", "started": "El torneo ya empezó.",
            "version": "El otro pokewalk es de otra versión: actualicen los dos."}


def _side(v):
    if v is None:
        return None
    if v not in (0, 1):
        raise ValueError("lado inválido")
    return int(v)


# --------------------------------------------------------------------------- firewall de Windows
# Windows Defender Firewall descarta todo lo que entra a un programa que no tenga permiso, y sin avisar:
# el que conecta ve un timeout. Windows suele preguntar "¿permitir a Python?" la primera vez que se abre un
# puerto, pero si ese aviso se cancela, no aparece (red pública, políticas de la empresa) o se cerró solo, queda
# una regla que BLOQUEA a python.exe y la sala no anda. Por eso, al entrar a Versus, pokewalk revisa y, si hace
# falta, pide permiso de administrador (el aviso normal de Windows, una sola vez) y deja una regla que permite
# a este Python y borra las que lo bloqueaban. Solo toca reglas de ESTE python.exe.
_fw_ok = False          # ya revisado en esta sesión


def _image_path():
    """Ruta real del .exe de este proceso (la que ve el firewall; con Python de la Store difiere de sys.executable)."""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if k.QueryFullProcessImageNameW(k.GetCurrentProcess(), 0, buf, ctypes.byref(n)):
            return buf.value
    except Exception:
        pass
    return sys.executable


def _fw_ids():
    exe = _image_path()
    return exe, "pokewalk (" + hashlib.sha1(exe.lower().encode("utf-8")).hexdigest()[:8] + ")"


def _ps_str(s):
    return "'" + s.replace("'", "''") + "'"


_FW_FIND_BLOCKS = ("Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True -ErrorAction SilentlyContinue"
                   " | Where-Object { ($_ | Get-NetFirewallApplicationFilter).Program -eq $exe }")


def _fw_check_script(exe, name):
    return (f"$exe={_ps_str(exe)}; $n={_ps_str(name)}; $blk=@({_FW_FIND_BLOCKS}); "
            "$ok=[bool](Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue); "
            "if ($ok -and $blk.Count -eq 0) { 'ok' } else { 'fix' }")


def _fw_fix_script(exe, name):
    return (f"$exe={_ps_str(exe)}; $n={_ps_str(name)}; {_FW_FIND_BLOCKS} | Remove-NetFirewallRule; "
            "if (-not (Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue)) { "
            "New-NetFirewallRule -DisplayName $n -Direction Inbound -Action Allow -Program $exe -Profile Any | Out-Null }")


def _powershell(args, timeout):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive"] + args, capture_output=True, text=True,
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def firewall_status():
    """"ok" (permitido, sin bloqueos) | "fix" (falta la regla o hay una que bloquea) | "n/a" (no se pudo revisar)."""
    exe, name = _fw_ids()
    try:
        out = _powershell(["-Command", _fw_check_script(exe, name)], 60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "n/a"
    return out if out in ("ok", "fix") else "n/a"


def ensure_firewall():
    """Bloquea (llamar desde un hilo): "ok" | "added" | "declined" (no dieron el permiso) | "n/a" (no es Windows)."""
    global _fw_ok
    if os.name != "nt" or _fw_ok:
        return "ok" if _fw_ok else "n/a"
    status = firewall_status()
    if status == "n/a":
        return "n/a"
    added = False
    if status == "fix":
        exe, name = _fw_ids()
        enc = base64.b64encode(_fw_fix_script(exe, name).encode("utf-16-le")).decode("ascii")
        outer = (f"Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                 f"-ArgumentList '-NoProfile','-EncodedCommand','{enc}'")     # el aviso de Windows (UAC)
        try:
            _powershell(["-Command", outer], 180)
        except (OSError, subprocess.SubprocessError):
            pass
        status, added = firewall_status(), True
    _fw_ok = status == "ok"
    return ("added" if added else "ok") if _fw_ok else "declined"


# --------------------------------------------------------------------------- datos <-> JSON
# Todo lo que llega por la red se valida: un mensaje raro no puede tumbar el programa ni dibujar basura.
def move_to_wire(m):
    return asdict(m)


def move_from_wire(d):
    ail = d.get("ailment")
    acc = d.get("acc")
    return pb.Move(
        name=_text(d.get("name"), 30) or "???",
        type=d.get("type") if d.get("type") in pb.TYPES_ES else "normal",
        power=_int(d.get("power"), 0, 250),
        acc=None if acc is None else _int(acc, 1, 100),
        special=bool(d.get("special")),
        pp=_int(d.get("pp"), 1, 64),
        priority=_int(d.get("priority"), -7, 5),
        ailment=ail if ail in pb.AILMENTS else None,
        ailment_chance=_int(d.get("ailment_chance"), 0, 100),
        stat_changes=tuple((k, _int(v, -6, 6)) for k, v in d.get("stat_changes", []) if k in pb.STAT_ES),
        stat_self=bool(d.get("stat_self")),
        stat_chance=_int(d.get("stat_chance"), 0, 100),
        drain=_int(d.get("drain"), -100, 100),
        healing=_int(d.get("healing"), 0, 100),
        min_hits=_int(d.get("min_hits"), 1, 5),
        max_hits=_int(d.get("max_hits"), 1, 5),
        flinch=_int(d.get("flinch"), 0, 100),
        crit=_int(d.get("crit"), 0, 3))


def fighter_to_wire(f):
    return {"name": f.name, "dex": f.dex, "types": list(f.types), "stats": dict(f.stats), "level": f.level,
            "moves": [move_to_wire(m) for m in f.moves]}


def fighter_from_wire(d):
    st = d["stats"]
    stats = {"hp": _int(st["hp"], 1, 3000)}
    stats.update({k: _int(st[k], 1, 1000) for k in ("atk", "def", "spa", "spd", "spe")})
    types = [t for t in list(d["types"])[:2] if t in pb.TYPES_ES] or ["normal"]
    moves = [move_from_wire(m) for m in list(d["moves"])[:4]] or [pb.FALLBACK_MOVE]
    return pb.Fighter(_text(d["name"], 24) or "???", _int(d["dex"], 1, 2000), types, stats, moves,
                      level=_int(d.get("level"), 1, 100))


def team_from_wire(lst):
    team = [fighter_from_wire(x) for x in list(lst)[:MAX_TEAM]]
    if not team:
        raise ValueError("equipo vacío")
    return team


def step_to_wire(st, flip=False):
    """Un paso de animación. `flip` lo da vuelta para quien juega el otro lado."""
    def fl(x):
        return x if (x is None or not flip) else 1 - x
    snap = st.snap[::-1] if (flip and st.snap is not None) else st.snap
    return {"text": pb.flip_text(st.text) if flip else st.text, "atk": fl(st.atk), "snap": snap,
            "faint": fl(st.faint), "dur": st.dur, "move": move_to_wire(st.move) if st.move else None,
            "tgt": fl(st.tgt), "miss": st.miss}


def step_from_wire(d, sizes):
    """`sizes` = tamaño de cada equipo en la copia local, para no aceptar índices fuera de rango."""
    snap = d.get("snap")
    if snap is not None:
        if len(snap) != 2:
            raise ValueError("snap inválido")
        fixed = []
        for s, (a, status, conf, hps) in enumerate(snap):
            if not 0 <= a < sizes[s] or len(hps) != sizes[s]:
                raise ValueError("snap fuera de rango")
            fixed.append((int(a), status if status in pb.STATUS_TAG else None, bool(conf),
                          tuple(_int(h, 0, 100000) for h in hps)))
        snap = tuple(fixed)
    mv = d.get("move")
    # el texto NO pasa por _text(): lleva los marcadores de perspectiva (caracteres de control) de pokebattle
    return pb.Step(str(d["text"])[:400], atk=_side(d.get("atk")), snap=snap, faint=_side(d.get("faint")),
                   dur=max(0.1, min(5.0, float(d.get("dur", 1.0)))), move=move_from_wire(mv) if mv else None,
                   tgt=_side(d.get("tgt")), miss=bool(d.get("miss")))


def dump_state(b, flip=False):
    """Estado completo de la batalla; `flip` lo escribe desde el lado del invitado."""
    sides = (1, 0) if flip else (0, 1)
    winner = None if b.winner is None else (1 - b.winner if flip else b.winner)
    return {"active": [b.active[s] for s in sides], "pending": [b.pending[s] for s in sides],
            "over": b.over, "winner": winner,
            "teams": [[{"hp": f.hp, "pp": list(f.pp), "status": f.status, "sleep": f.sleep,
                        "confused": f.confused, "flinch": f.flinch, "fainted": f.fainted,
                        "stages": dict(f.stages)} for f in b.teams[s]] for s in sides]}


def load_state(b, st):
    """Pone la copia espejo de la batalla en el estado que mandó el árbitro."""
    if [len(t) for t in st["teams"]] != [len(t) for t in b.teams]:
        raise ValueError("equipos distintos")
    b.active = [_int(a, 0, len(b.teams[s]) - 1) for s, a in enumerate(st["active"])]
    b.pending = [bool(x) for x in st["pending"]]
    b.over = bool(st["over"])
    b.winner = _side(st["winner"])
    for team, saved in zip(b.teams, st["teams"]):
        for f, w in zip(team, saved):
            f.hp = _int(w["hp"], 0, f.max_hp)
            if len(w["pp"]) == len(f.moves):
                f.pp = [_int(p, 0, 99) for p in w["pp"]]
            f.status = w["status"] if w["status"] in pb.STATUS_TAG else None
            f.sleep, f.confused = _int(w["sleep"], 0, 9), _int(w["confused"], 0, 9)
            f.flinch, f.fainted = bool(w["flinch"]), bool(w["fainted"])
            f.stages = {k: _int(w["stages"].get(k, 0), -6, 6) for k in pb.STAT_ES}


# --------------------------------------------------------------------------- conexión
class Link:
    """Una conexión TCP con mensajes JSON de una línea. Un hilo lee y los deja en una cola; todo lo demás
    (poll, send) se llama desde el hilo principal. Manda latidos y da por muerta la conexión si el otro
    deja de contestar."""

    def __init__(self, sock):
        self.sock = sock
        sock.settimeout(1.0)
        self.q = queue.Queue()
        self.alive = True
        self.last_rx = self.last_tx = time.time()
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        while self.alive:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            if len(buf) > MAX_BUF:
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.last_rx = time.time()
                    self.q.put(msg)
        self.alive = False
        self.q.put({"t": "_closed"})

    def send(self, obj):
        if not self.alive:
            return False
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self._lock:
                self.sock.sendall(data)
            self.last_tx = time.time()
            return True
        except OSError:
            self.alive = False
            self.q.put({"t": "_closed"})
            return False

    def poll(self):
        """Mensajes que llegaron desde la última vez. Un {"t": "_closed"} avisa que se cortó."""
        now = time.time()
        if self.alive and now - self.last_tx > PING_EVERY:
            self.send({"t": "ping"})
        if self.alive and now - self.last_rx > DEAD_AFTER:
            self.alive = False
            self.q.put({"t": "_closed"})
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return [m for m in out if m.get("t") != "ping"]

    def close(self, why=None):
        """Cierra; con `why` le avisa antes al otro ("flee" = me fui del combate)."""
        if why and self.alive:
            self.send({"t": "bye", "why": why})
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- salas (descubrimiento por UDP)
def local_ips():
    """IPs de esta compu en la red (sin loopback)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))            # no manda nada: solo elige por qué interfaz saldría
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


class RoomHost:
    """Sala abierta: escucha conexiones TCP y avisa por broadcast UDP cada segundo (a la dirección general
    y al broadcast de cada red de esta compu, porque con varias placas / VPN el general sale solo por una)."""

    def __init__(self, name, rules=None, backlog=1, tour=None):
        """`tour` = {"n", "max"} si es una sala de torneo (el aviso lo lleva; se actualiza con `set_tour`)."""
        self.ips = local_ips()
        self.name, self.rules = name, room_rules(rules)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("", TCP_PORT))
        except OSError:                              # ocupado (otro pokewalk en esta compu): uno libre
            srv.bind(("", 0))
        srv.listen(backlog)
        srv.setblocking(False)
        self.srv, self.port = srv, srv.getsockname()[1]
        self.set_tour(tour)
        self.stop = threading.Event()
        threading.Thread(target=self._announce, daemon=True).start()

    def set_tour(self, tour):
        info = {"app": "pokewalk", "v": PROTO, "name": self.name, "port": self.port, "rules": self.rules}
        if tour:
            info["tour"] = tour
        self.beacon = json.dumps(info).encode()      # el hilo del aviso toma el nuevo en la próxima vuelta

    def _announce(self):
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            return
        targets = ["255.255.255.255"] + [".".join(ip.split(".")[:3] + ["255"]) for ip in self.ips]
        while not self.stop.is_set():
            for t in targets:
                try:
                    u.sendto(self.beacon, (t, UDP_PORT))
                except OSError:
                    pass
            self.stop.wait(BEACON_EVERY)
        u.close()

    def accept(self):
        """Link del amigo que se conectó, o None si todavía no."""
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return None
        return Link(conn)

    def close(self):
        self.stop.set()
        try:
            self.srv.close()
        except OSError:
            pass


def probe_room(ip, port=TCP_PORT):
    """¿Hay una sala de pokewalk en ip:port? Devuelve (nombre, reglas, torneo o None) o None. Es una conexión SALIENTE: el firewall
    de quien busca no la frena, a diferencia de recibir avisos por broadcast (que Windows bloquea sin permiso)."""
    try:
        s = socket.create_connection((ip, port), timeout=PROBE_TIMEOUT)
    except OSError:
        return None
    try:
        s.settimeout(1.5)
        s.sendall(b'{"t":"probe"}\n')
        data = b""
        while b"\n" not in data and len(data) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        m = json.loads(data.split(b"\n")[0])
        if m.get("t") == "room" and m.get("v") == PROTO:
            return _text(m.get("name"), 20) or ip, room_rules(m.get("rules")), tour_info(m.get("tour"))
    except (OSError, ValueError, AttributeError):
        pass
    finally:
        s.close()
    return None


class RoomFinder:
    """Busca salas de dos maneras a la vez:
      * escaneo: prueba conectarse al puerto de sala de cada equipo de tu subred (andá lo que andes con el
        firewall, y no depende del broadcast, que muchas redes y firewalls bloquean);
      * avisos UDP de las salas, si el firewall deja recibirlos.
    `rooms()` = las que se vieron hace poco."""

    def __init__(self):
        self.found = {}         # (ip, puerto) -> (nombre, visto, verificado por escaneo, reglas, torneo o None)
        self.error = ""
        self.sock = None
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.me = set(local_ips())
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", UDP_PORT))
            s.settimeout(0.5)
            self.sock = s
            threading.Thread(target=self._listen, daemon=True).start()
        except OSError:
            pass                # sin UDP igual se busca por escaneo
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        while not self.stop.is_set():
            nets = dict.fromkeys(".".join(ip.split(".")[:3]) for ip in self.me)
            targets = [f"{n}.{i}" for n in nets for i in range(1, 255) if f"{n}.{i}" not in self.me]
            with ThreadPoolExecutor(128) as ex:
                for ip, res in zip(targets, ex.map(probe_room, targets)):
                    if res:
                        with self._lock:
                            self.found[(ip, TCP_PORT)] = (res[0], time.time(), True, res[1], res[2])
            self.stop.wait(SCAN_EVERY)

    def _listen(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                m = json.loads(data)
                if m.get("app") != "pokewalk" or m.get("v") != PROTO:
                    continue
                port, name = _int(m["port"], 1, 65535), _text(m.get("name"), 20) or addr[0]
                rules, tour = room_rules(m.get("rules")), tour_info(m.get("tour"))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            with self._lock:
                old = self.found.get((addr[0], port))
                self.found[(addr[0], port)] = (name, time.time(),
                                               bool(old and old[2] and time.time() - old[1] < ROOM_TTL), rules, tour)

    def rooms(self):
        """Salas vistas hace poco. Una sala con varias IPs (varias placas) sale una sola vez, con la IP que
        se comprobó por escaneo si la hay (las que solo avisaron por UDP pueden ser de una red virtual)."""
        now = time.time()
        with self._lock:
            items = list(self.found.items())
        best = {}
        for (ip, port), (name, seen, verified, rules, tour) in items:
            if now - seen >= ROOM_TTL:
                continue
            cur = best.get((name, port))
            if cur is None or (verified and not cur[3]):
                best[(name, port)] = (ip, port, name, verified, rules, tour)
        return [{"name": n, "ip": ip, "port": p, "rules": r, "tour": tr}
                for ip, p, n, _, r, tr in sorted(best.values(), key=lambda x: (x[2], x[0]))]

    def close(self):
        self.stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- la sala: de conectarse a empezar
# Etapa "special": legendarios / míticos, de una sola tecla. Tecla -> (legendarios, míticos) permitidos.
SPECIAL_RULES = {"1": (True, True), "2": (False, True), "3": (True, False), "4": (False, False)}
SPECIAL_OPTS = [("1", "Todos permitidos"), ("2", "Sin legendarios"), ("3", "Sin míticos"),
                ("4", "Sin legendarios ni míticos")]


class Versus:
    """Todo lo previo a la batalla. Etapas:
        menu -> (size + nivel + special -> hosting | browsing | pidiendo IP) -> connecting -> hello -> team (armás tu
        equipo) -> loading -> waiting (el otro todavía no terminó) -> starting -> la batalla (la arma pokewalk.py).
    Las reglas de la sala (`rules`: tamaño del equipo y nivel) las elige quien la crea y viajan en su "hello":
    el invitado las tiene antes de armar su equipo.
    Desde el menú también se crea un torneo (4: tamaño + nivel como una sala, y se abre un `Tournament`) y se
    entra a uno (eligiéndolo en la lista, o por IP: si la sala contesta que es un torneo, la conexión pasa a un
    `TournamentClient`). En los dos casos la sala de versus se lo entrega a pokewalk.py y termina.
    pokewalk.py llama a `update()` en cada cuadro y reacciona a lo que devuelve:
        ("team",)        conectados: pedile al jugador su equipo
        ("ready",)       los dos equipos están listos: armar la batalla
        ("tournament", t) es un torneo: seguir con `t` (Tournament o TournamentClient) y cerrar esta sala
        ("error", txt)   se cortó o falló algo: cerrar la sala
    """

    def __init__(self, name, link=None, peer="", role=None, prev=None, rules=None):
        """Con `link` (revancha) se retoma la sala con el mismo amigo y la misma conexión, directo en "armá tu
        equipo", con las mismas `rules`. `prev` = (equipo, sprites) de la pelea anterior, para repetirlo con un Enter."""
        self.name, self.peer, self.role = name, peer, role
        self.rules = room_rules(rules)
        self.size = MAX_TEAM       # lo elige el anfitrión en la etapa "size" (el nivel lo pide pokewalk.py)
        self.level = pb.LEVEL      # el nivel que escribió, mientras elige legendarios / míticos (etapa "special")
        self.tourney = False       # la sala que se está creando es un torneo
        self.relay = False         # por internet (pokerelay): sala con código en vez de IP
        self._opened = None        # ("ok", RelayRoom) | ("fail", texto): resultado del hilo que abre la sala por internet
        self._hello_msg, self._hello_at = None, 0.0     # saludo del invitado por internet (se repite si no contestan)
        self._handoff = None       # torneo que se le pasa a pokewalk.py en el próximo update()
        self.stage = "menu"
        self.room = self.finder = self.link = None
        self.prev_team, self.prev_sprites = prev if prev else (None, None)
        self._ask_team = False
        self.target = ""
        self.my = self.my_sprites = self.foe = None
        self.closed = False
        self._conn = None
        self._fail = ""
        self._fw = None
        self._pending = []         # conexiones entrantes que todavía no dijeron quiénes son (sala abierta)
        self.noperm = False        # Windows no dio el permiso: puede que el amigo no pueda conectarse
        self._lock = threading.Lock()
        self._hello = False
        if link:                                       # revancha: ya estamos conectados y el permiso está dado
            self.link, self._hello, self.stage, self._ask_team = link, True, "team", True
        elif os.name == "nt" and not _fw_ok:           # antes de nada: que Windows deje pasar a pokewalk
            self.stage = "firewall"
            threading.Thread(target=self._firewall, daemon=True).start()

    def _firewall(self):
        res = ensure_firewall()
        with self._lock:
            self._fw = res

    # --- texto de la línea de estado
    def text(self):
        st = self.stage
        if st == "firewall":
            return ("Revisando los permisos de red de Windows... Si aparece un aviso de Windows, aceptalo: "
                    "es para que tu amigo pueda conectarse (una sola vez).      Esc cancela")
        if st in ("menu", "lan"):
            warn = "⚠ Sin permiso de Windows (salí y entrá de nuevo y aceptá el aviso) · " if self.noperm else ""
            if st == "menu":
                return warn + "Versus:   1 En tu red   2 Por internet      Esc cancela"
            return warn + ("En tu red:   1 Crear sala   2 Crear torneo   3 Buscar salas"
                           "      0 volver   Esc cancela")
        if st == "net":
            return ("Por internet (con un código):   1 Crear sala   2 Crear torneo   3 Unirse con un código"
                    "      0 volver   Esc cancela")
        if st == "netopen":
            return (f"Conectando con el servidor de internet ({pr.BROKERS[0][0]})...      Esc cancela")
        if st == "size":
            what = self._what()
            return f"{what}: ¿de cuántos Pokémon los equipos?   1-{MAX_TEAM}      0 volver   Esc cancela"
        if st == "special":
            return ("¿Legendarios y míticos?   " + "   ".join(f"{k} {t}" for k, t in SPECIAL_OPTS)
                    + "      Enter = 1   0 volver   Esc cancela")
        if st == "hosting" and self.relay:
            return (f"Sala por internet · código {self.room.code} · {rules_text(self.rules)} · pasale el código a tu "
                    "amigo y esperá...      Esc cierra la sala")
        if st == "hosting":
            where = ", ".join(self.room.ips) if self.room and self.room.ips else "esta compu"
            return (f"Sala «{self.name}» abierta ({where}) · {rules_text(self.rules)} · esperando a un amigo..."
                    "      Esc cierra la sala")
        if st == "browsing":
            if self.finder and self.finder.error:
                return f"{self.finder.error}   ·   i = conectar por IP   0 volver   Esc cancela"
            rooms = self.finder.rooms() if self.finder else []
            if not rooms:
                return "Buscando salas en la red...   (¿no aparece? i = escribí su IP)   0 volver   Esc cancela"
            return "Salas:  " + "   ".join(f"{i + 1} {room_label(r)}" for i, r in enumerate(rooms[:9])) + \
                   "      i = por IP   0 volver   Esc cancela"
        if st == "connecting":
            return f"Conectando con {self.target}...      Esc cancela"
        if st == "hello":
            return "Conectado, saludando...      Esc cancela"
        if st == "team":
            return f"Sala: {rules_text(self.rules)} · armá tu equipo..."
        if st == "loading":
            return "Buscando tu equipo en internet... (la primera vez tarda)"
        if st == "waiting":
            extra = " (¡ya armó el suyo!)" if self.foe else ""
            return (f"Sala: {rules_text(self.rules)} · tu equipo está listo. Esperando a {self.peer}...{extra}"
                    "      Esc cancela")
        return "Preparando la batalla..."

    def view(self):
        """El menú de la etapa actual como datos, para dibujarlo en varias filas: {"title", "items": [(tecla,
        texto)], "foot", "warn"}. None si la etapa no tiene menú (se usa `text()`)."""
        st = self.stage
        if st in ("menu", "lan"):
            title = "Versus · ¿dónde juegan?" if st == "menu" else "En tu red (misma casa o mismo Wi-Fi)"
            if self.noperm:
                title = "⚠ Sin permiso de Windows (salí y entrá de nuevo y aceptá el aviso) · " + title
            if st == "menu":
                return {"title": title, "warn": self.noperm, "foot": "Esc cancela",
                        "items": [("1", "En tu red (misma casa o mismo Wi-Fi)"),
                                  ("2", "Por internet, con un código (amigos fuera de tu red)")]}
            return {"title": title, "warn": self.noperm, "foot": "0  volver      Esc cancela",
                    "items": [("1", "Crear sala"),
                              ("2", f"Crear torneo ({MIN_PLAYERS}-{MAX_PLAYERS} jugadores)"),
                              ("3", "Buscar salas (si no aparece, ahí podés escribir la IP)")]}
        if st == "net":
            return {"title": "Por internet: sin instalar nada (pasa por un servidor público gratuito)",
                    "foot": "0  volver      Esc cancela",
                    "items": [("1", "Crear sala (te doy un código para tu amigo)"),
                              ("2", f"Crear torneo ({MIN_PLAYERS}-{MAX_PLAYERS} jugadores)"),
                              ("3", "Unirse con un código")]}
        if st == "size":
            what = self._what()
            return {"title": f"{what}: ¿de cuántos Pokémon los equipos?", "foot": "0  volver      Esc cancela",
                    "items": [(str(n), f"{n} Pokémon") for n in range(1, MAX_TEAM + 1)]}
        if st == "special":
            what = self._what()
            return {"title": f"{what} · {self.size} Pokémon · nivel {self.level}: ¿legendarios y míticos?",
                    "foot": "Enter  1      0  volver      Esc cancela", "items": list(SPECIAL_OPTS)}
        if st == "hosting" and self.relay:
            return {"title": f"Sala por internet · código  {self.room.code}  · {rules_text(self.rules)}", "items": [],
                    "warn": True,
                    "foot": "Pasale el código a tu amigo: él entra con v → 2 → 3 «Unirse con un código»      "
                            "Esc cierra la sala"}
        if st == "hosting":
            ips = ", ".join(self.room.ips) if self.room and self.room.ips else "la de esta compu"
            return {"title": f"Sala «{self.name}» abierta ({rules_text(self.rules)}) · esperando a un amigo...",
                    "items": [],
                    "foot": f"Tu amigo la ve en «Buscar salas», o conecta por IP a {ips}      Esc cierra la sala"}
        if st == "browsing":
            rooms = self.finder.rooms()[:9] if self.finder else []
            foot = "¿No aparece la sala?  i  escribí su IP      0  volver      Esc cancela"
            if not rooms:
                return {"title": "Buscando salas en tu red...", "items": [], "foot": foot}
            return {"title": "Salas en tu red:", "foot": foot,
                    "items": [(str(i + 1), room_label(r)) for i, r in enumerate(rooms)]}
        return None

    # --- teclado: devuelve "cancel" | "ip" | "level" (pedile al anfitrión el nivel de la sala) | None
    def key(self, ch):
        if ch == "\x1b":
            return "cancel"
        if self.stage == "menu":
            if ch == "1":
                self.stage = "lan"
            elif ch == "2":
                self.stage = "net"
        elif self.stage == "lan":
            if ch in ("1", "2"):
                self.stage, self.tourney = "size", ch == "2"
            elif ch == "3":
                self.browse()
            elif ch == "0":
                self.stage = "menu"
        elif self.stage == "net":
            if ch in ("1", "2"):
                self.stage, self.tourney, self.relay = "size", ch == "2", True
            elif ch == "3":
                return "code"
            elif ch == "0":
                self.stage = "menu"
        elif self.stage == "size":
            if ch == "0":
                self.stage, self.tourney = ("net" if self.relay else "lan"), False
                self.relay = False
            elif ch.isdigit() and 1 <= int(ch) <= MAX_TEAM:
                self.size = int(ch)
                return "level"
        elif self.stage == "special":
            if ch == "0":
                self.stage = "size"
            elif ch in ("\r", "\n") or ch in SPECIAL_RULES:
                legend, myth = SPECIAL_RULES.get(ch, (True, True))
                self.host(self.size, self.level, legend, myth)
        elif self.stage == "browsing":
            if ch in ("i", "I"):
                return "ip"
            if ch == "0":
                self._stop_finder()
                self.stage = "lan"
            elif ch.isdigit() and self.finder:
                rooms = self.finder.rooms()
                if 1 <= int(ch) <= len(rooms[:9]):
                    r = rooms[int(ch) - 1]
                    if r.get("tour"):                     # un torneo: lo sigue su propio cliente
                        self._stop_finder()
                        self._handoff = TournamentClient(self.name, r["ip"], r["port"])
                    else:
                        self.connect(r["ip"], r["port"])
        return None

    # --- pasos
    def ask_special(self, level):
        """Ya está el nivel (lo pidió pokewalk.py): falta elegir si entran legendarios y míticos."""
        self.level, self.stage = level, "special"

    def host(self, size=MAX_TEAM, level=pb.LEVEL, legend=True, myth=True):
        """Abre la sala con estas reglas (equipos de hasta `size`, todos al nivel `level` ± el handicap; con o sin
        legendarios / míticos). Si se eligió "Crear torneo", abre un `Tournament` con esas reglas y se lo pasa a
        pokewalk.py."""
        self.rules = room_rules({"size": size, "level": level, "legend": legend, "myth": myth})
        if self.relay:                                 # por internet: conectar tarda (TLS), en un hilo
            self.stage = "netopen"
            threading.Thread(target=self._open_relay, args=("started" if self.tourney else "full",),
                             daemon=True).start()
            return
        if self.tourney:
            try:
                self._handoff = Tournament(self.name, self.rules)
            except OSError as e:
                self._fail = f"No pude abrir el torneo: {e}"
            return
        try:
            self.room = RoomHost(self.name, self.rules)
        except OSError as e:
            self._fail = f"No pude abrir la sala: {e}"
            return
        self.role, self.stage = "host", "hosting"

    def _what(self):
        return ("Nuevo torneo" if self.tourney else "Nueva sala") + (" por internet" if self.relay else "")

    def _open_relay(self, refuse):
        try:
            res = ("ok", pr.open_room(refuse))
        except pr.RelayError as e:
            res = ("fail", str(e))
        with self._lock:
            if self.closed and res[0] == "ok":
                res[1].close()
                return
            self._opened = res

    def join_code(self, text):
        """Unirse por internet con el código que escribió (da igual mayúsculas y guiones). False si no es un
        código válido (pokewalk.py se queda en el prompt)."""
        code = pr.parse_code(text)
        if not code:
            return False
        self.relay, self.role, self.stage = True, "guest", "connecting"
        self.target = f"la sala {code} (por internet, {pr.BROKERS[pr.broker_of(code)][0]})"
        threading.Thread(target=self._join_relay, args=(code,), daemon=True).start()
        return True

    def _join_relay(self, code):
        try:
            res = ("ok", pr.join(code))
        except pr.RelayError as e:
            res = ("fail", str(e))
        with self._lock:
            if self.closed and res[0] == "ok":
                res[1].close()
                return
            self._conn = res

    def browse(self):
        self.finder = RoomFinder()
        self.stage = "browsing"

    def connect(self, ip, port=TCP_PORT):
        self._stop_finder()
        self.role, self.stage, self.target = "guest", "connecting", f"{ip}:{port}"
        threading.Thread(target=self._connect, args=(ip, port), daemon=True).start()

    def _connect(self, ip, port):
        try:
            res = ("ok", Link(socket.create_connection((ip, port), timeout=5)))
        except socket.timeout:
            res = ("err", "no contesta. Revisá que sea la IP de la sala (la del Wi-Fi) y que el otro tenga la "
                          "última versión de pokewalk: ahí se abre solo el permiso de Windows.")
        except (OSError, ValueError) as e:
            res = ("err", str(e) or e.__class__.__name__)
        with self._lock:
            if self.closed and res[0] == "ok":
                res[1].close()
                return
            self._conn = res

    def _stop_finder(self):
        if self.finder:
            self.finder.close()
            self.finder = None

    def _linked(self, link):
        self.link, self.stage = link, "hello"
        hello = {"t": "hello", "v": PROTO, "name": self.name}
        if self.role == "host":                       # las reglas son de quien creó la sala
            hello["rules"] = self.rules
        elif self.relay:                               # por internet, si se pierde, se repite (ver update)
            self._hello_msg, self._hello_at = hello, time.time()
        link.send(hello)

    def set_team(self, fighters, sprites):
        """Tu equipo ya está armado: se lo mando al otro."""
        self.my, self.my_sprites = fighters, sprites
        self.stage = "waiting"
        if self.link:
            self.link.send({"t": "team", "fighters": [fighter_to_wire(f) for f in fighters]})

    def update(self):
        if self._fail:
            fail, self._fail = self._fail, ""
            return ("error", fail)
        if self._handoff:
            tour, self._handoff = self._handoff, None
            return ("tournament", tour)
        if self._ask_team:
            self._ask_team = False
            return ("team",)
        if self.stage == "firewall":
            with self._lock:
                res = self._fw
            if not res:
                return None
            self.stage, self.noperm = "menu", res == "declined"
            return None
        if self.stage == "netopen":
            with self._lock:
                res, self._opened = self._opened, None
            if res and res[0] == "fail":
                return ("error", res[1])
            if res and self.tourney:
                return ("tournament", Tournament(self.name, self.rules, room=res[1]))
            if res:
                self.room, self.role, self.stage = res[1], "host", "hosting"
        if self.stage == "hosting":
            self._triage()
        elif self.stage == "connecting":
            with self._lock:
                res, self._conn = self._conn, None
            if res and res[0] == "ok":
                self._linked(res[1])
            elif res and res[0] == "fail":                 # por internet: el texto ya viene completo
                return ("error", res[1])
            elif res:
                return ("error", f"No pude conectar con {self.target}: {res[1]}")
        if not self.link:
            return None
        if self.stage == "hello" and self._hello_msg and not self.link.heard:
            if self.link.unanswered():
                return ("error", f"Nadie contestó en {self.target}: revisá que el código esté bien escrito y que "
                                 "la sala siga abierta (se cierra cuando el que la creó sale).")
            if time.time() - self._hello_at > 3.0:      # por si el servidor perdió el saludo
                self._hello_at = time.time()
                self.link.send(self._hello_msg)
        event = None
        msgs = self.link.poll()
        for i, m in enumerate(msgs):
            t = m.get("t")
            who = self.peer or "El otro jugador"
            if t == "hello" and not self._hello and m.get("tour") and self.role != "host":
                # entraste por IP a un torneo: la conexión (y lo que llegó detrás del saludo) pasa a su cliente
                link, self.link = self.link, None
                return ("tournament", TournamentClient(self.name, link=link, backlog=msgs[i:]))
            if t == "hello" and not self._hello:
                if m.get("v") != PROTO:
                    return ("error", "El otro pokewalk es de otra versión: actualicen los dos.")
                self.peer, self._hello = _text(m.get("name"), 20) or "Rival", True
                if self.role != "host":                 # el invitado juega con las reglas de la sala
                    self.rules = room_rules(m.get("rules"))
                if self.stage == "hello":
                    self.stage, event = "team", ("team",)
            elif t == "team":
                try:
                    foe = team_from_wire(m["fighters"])
                    if len(m["fighters"]) > self.rules["size"] or team_problem(foe, self.rules):
                        raise ValueError("no cumple las reglas de la sala")   # un cliente tocado se salta el chequeo
                    self.foe = foe
                except (KeyError, TypeError, ValueError, AttributeError):
                    return ("error", f"{who} mandó un equipo inválido.")
            elif t == "bye":
                return ("error", _BYE_WHY.get(m.get("why")) or f"{who} se fue de la sala.")
            elif t == "_closed":
                return ("error", f"Se cortó la conexión con {who}.")
        if event:
            return event
        if self.my and self.foe and self.stage == "waiting":
            self.stage = "starting"
            return ("ready",)
        return None

    def _triage(self):
        """Conexiones entrantes a la sala: un "probe" es alguien buscando salas (se le contesta el nombre y se
        corta, la sala sigue abierta); un "hello" es el amigo que de verdad entra."""
        link = self.room.accept()
        if link:
            self._pending.append((link, time.time()))
        for item in list(self._pending):
            link, t0 = item
            for m in link.poll():
                kind = m.get("t")
                if kind == "probe":
                    link.send({"t": "room", "v": PROTO, "name": self.name, "rules": self.rules})
                    link.close()
                    self._pending.remove(item)
                    break
                if kind == "hello":
                    link.q.put(m)                       # lo procesa update(), como siempre
                    self._pending.remove(item)
                    self.room.close()
                    self.room = None
                    self._drop_pending()
                    self._linked(link)
                    return
                if kind == "_closed":
                    self._pending.remove(item)
                    break
            else:
                if time.time() - t0 > PENDING_TTL:      # conectó y no dijo nada
                    link.close()
                    self._pending.remove(item)

    def _drop_pending(self):
        for link, _ in self._pending:
            link.close()
        self._pending = []

    def take_link(self):
        """La batalla se queda con la conexión."""
        link, self.link = self.link, None
        return link

    def close(self):
        with self._lock:
            self.closed = True
        for part in (self.room, self.finder):
            if part:
                part.close()
        self.room = self.finder = None
        self._drop_pending()
        if self._handoff:                              # torneo que nadie llegó a tomar
            self._handoff.close()
            self._handoff = None
        if self.link:
            self.link.close("bye")
            self.link = None


# --------------------------------------------------------------------------- torneo: la sala
# Estrella: cada invitado tiene un solo Link, con la compu del organizador, que es la que manda. Un jugador es
# {"id": n, "name": texto}; el organizador es el id 0 y los demás reciben un id nuevo al entrar (no se reusan).
# Mensajes, además del "ping" y de la búsqueda ("probe" -> "room", que en un torneo trae "tour": {"n", "max"}):
#   invitado -> org   {"t": "hello", "v": PROTO, "name": nombre}            el mismo saludo que en el versus
#   org -> invitado   {"t": "hello", "v": PROTO, "name": org, "rules": reglas, "tour": 1, "you": id}
#   org -> todos      {"t": "roster", "players": [jugador...], "max": MAX_PLAYERS}   cada vez que alguien entra o sale
#   org -> todos      {"t": "start", "players": [jugador...]}               lista final y en orden; la sala se cierra
#   org -> invitado   {"t": "bye", "why": "full" | "started" | "version"}   no puede entrar (y se corta)
#   org -> todos      {"t": "bye", "why": "cancel"}                         el organizador cerró el torneo
#   invitado -> org   {"t": "bye", "why": "bye"}                            se fue
#   org -> todos      {"t": "bracket", "rounds", "cur", "gone", "champ", "begun", "next", "note"}   el cuadro (ver
#                     `make_bracket`); cur = [ronda, pelea] en curso, next = s hasta la próxima pelea
# Cualquier otro mensaje (las peleas) llega como evento ("msg", ...).
def players_from_wire(raw):
    out, seen = [], set()
    for p in list(raw)[:MAX_PLAYERS]:
        pid = _int(p.get("id"), 0, 999)
        if pid in seen:
            raise ValueError("jugador repetido")
        seen.add(pid)
        out.append({"id": pid, "name": _text(p.get("name"), 20) or "?"})
    return out


# --------------------------------------------------------------------------- torneo: el cuadro
# Eliminación simple. El cuadro es una lista de rondas; cada pelea es [a, b, ganador] con ids de jugador, None
# (todavía no se sabe) o EMPTY (nadie). La pelea j de una ronda manda su ganador a la pelea j // 2 de la siguiente
# (lado j % 2). Las peleas se juegan de a una, en orden: toda la ronda 1, después la 2...
def make_bracket(ids, rng=random):
    """Cuadro para los `ids` (2 o más) sorteados con `rng`. Si no son potencia de 2, los que sobran pasan directo
    (bye) en la 1ª ronda, repartidos de a uno por pelea: nunca hay bye contra bye. Los byes ya salen resueltos."""
    ids = list(ids)
    rng.shuffle(ids)
    size = 2
    while size < len(ids):
        size *= 2
    half, byes = size // 2, size - len(ids)
    bye_at = {i * half // byes for i in range(byes)} if byes else set()
    it = iter(ids)
    rounds = [[[next(it), EMPTY if j in bye_at else next(it), None] for j in range(half)]]
    while len(rounds[-1]) > 1:
        rounds.append([[None, None, None] for _ in range(len(rounds[-1]) // 2)])
    _settle(rounds)
    return rounds


def _put(rounds, r, j, w):
    rounds[r][j][2] = w
    if r + 1 < len(rounds):
        rounds[r + 1][j // 2][j % 2] = w


def _settle(rounds):
    """Lo que no hace falta pelear: si de un lado no hay nadie (EMPTY), pasa el otro (o nadie, si tampoco hay)."""
    for r, rnd in enumerate(rounds):
        for j, (a, b, w) in enumerate(rnd):
            if w is None and a is not None and b is not None and EMPTY in (a, b):
                _put(rounds, r, j, b if a == EMPTY else a)


def advance(rounds, r, j, winner):
    """`winner` (un id, o EMPTY) ganó la pelea j de la ronda r: pasa a la siguiente."""
    _put(rounds, r, j, winner)
    _settle(rounds)


def next_match(rounds):
    """(ronda, pelea) de la próxima pelea por jugar (los dos ya se saben), o None."""
    for r, rnd in enumerate(rounds):
        for j, (a, b, w) in enumerate(rnd):
            if w is None and None not in (a, b) and EMPTY not in (a, b):
                return r, j
    return None


def champion(rounds):
    """El campeón (id), EMPTY si se fueron todos, o None si falta."""
    return rounds[-1][0][2]


def round_name(r, total):
    left = total - 1 - r
    return ("Final", "Semi", "Cuartos")[left] if left < 3 else f"Ronda {r + 1}"


class _TourLobby:
    """Lo que comparten las dos puntas del torneo: jugadores, reglas, el cuadro y cómo se ve la sala (como `Versus`:
    `view()` en cuadrícula, `text()` en una línea). Etapas: connecting -> hello -> lobby -> started."""
    stage = "lobby"
    max = MAX_PLAYERS
    note = ""               # novedad de las peleas: se ve como título del cuadro
    bracket = None          # el cuadro que mandó el organizador: {"rounds", "cur", "gone", "champ"}
    begun = False           # ya arrancó la primera pelea
    next_at = None          # cuándo arranca la próxima pelea (reloj de esta compu), para mostrarlo

    def player(self, pid):
        return next((p for p in self.players if p["id"] == pid), None)

    def name_of(self, pid):
        p = self.player(pid)
        return p["name"] if p else "?"

    def _label(self, p):
        tags = (["organiza"] if p["id"] == 0 else []) + (["vos"] if p["id"] == self.me else [])
        return p["name"] + (f" ({', '.join(tags)})" if tags else "")

    def _foot(self):
        return "Esc  salir del torneo"

    def _foot_started(self):
        if self.bracket and self.bracket["champ"] is not None:
            return "Esc  volver"
        return "Esc  salir del torneo (perdés tu próxima pelea)"

    def set_bracket(self, m):
        """Un {"t": "bracket"} del organizador (él mismo lo usa igual): queda para `view()`."""
        def pid(v):
            return None if v is None else _int(v, EMPTY, 999)
        rounds = [[[pid(x) for x in list(mt)[:3]] for mt in list(rnd)[:MAX_PLAYERS]] for rnd in list(m["rounds"])[:4]]
        if not rounds or any(len(mt) != 3 for rnd in rounds for mt in rnd):
            raise ValueError("cuadro roto")
        cur = m.get("cur")
        self.bracket = {"rounds": rounds, "cur": [_int(x, 0, 9) for x in list(cur)[:2]] if cur else None,
                        "gone": {pid(x) for x in list(m.get("gone") or [])}, "champ": pid(m.get("champ"))}
        self.begun = self.begun or bool(m.get("begun"))
        nxt = m.get("next")
        self.next_at = None if nxt is None else time.time() + _int(nxt, 0, 600)
        if m.get("note"):
            self.note = _text(m["note"], 160)

    def _who(self, pid, gone):
        if pid is None:
            return "?"
        if pid == EMPTY:
            return "nadie"
        return self.name_of(pid) + (" (se fue)" if pid in gone else "") + (" (vos)" if pid == self.me else "")

    def bracket_items(self):
        """Las peleas del cuadro para la vista: (ronda, texto, ¿es la que se juega ahora?)."""
        br = self.bracket
        rounds, gone, cur = br["rounds"], br["gone"], br["cur"]
        out = []
        for r, rnd in enumerate(rounds):
            for j, (a, b, w) in enumerate(rnd):
                hot = cur == [r, j]
                if r == 0 and EMPTY in (a, b):
                    text = f"{self._who(b if a == EMPTY else a, gone)} pasa directo"
                else:
                    text = f"{self._who(a, gone)} vs {self._who(b, gone)}"
                    if w is not None:
                        text += f" → {self._who(w, gone)}"
                out.append((round_name(r, len(rounds)), ("▶ " if hot else "") + text, hot))
        return out

    def view(self):
        if self.stage not in ("lobby", "started"):
            return None
        n = len(self.players)
        if self.stage == "started":
            foot, warn = self._foot_started(), False
            if not self.bracket:
                return {"title": self.note or f"Torneo: {n} jugadores", "foot": foot,
                        "items": [(str(i + 1), self._label(p)) for i, p in enumerate(self.players)]}
            champ = self.bracket["champ"]
            if champ == EMPTY:
                title, warn = "El torneo terminó sin campeón (se fueron todos)", True
            elif champ is not None:
                title, warn = f"¡{self.name_of(champ)} es el campeón del torneo!", True
            else:
                title = self.note or "Cuadro del torneo"
                if not self.begun:
                    title += f" · esperando a que {self.org} empiece la primera pelea"
                elif self.next_at and self.next_at > time.time():
                    title += f" · próxima pelea en {math.ceil(self.next_at - time.time())} s"
            return {"title": title, "foot": foot, "warn": warn, "items": self.bracket_items()}
        title = f"Torneo de {self.org} · {rules_text(self.rules)} · {n}/{self.max} jugadores"
        return {"title": title, "foot": self._foot(),
                "items": [(str(i + 1), self._label(p)) for i, p in enumerate(self.players)]}

    def text(self):
        v = self.view()
        if not v:
            if self.stage == "connecting":
                return f"Conectando con {self.target}...      Esc cancela"
            return "Conectado, entrando al torneo...      Esc cancela"
        return f"{v['title']}:  " + ", ".join(f"{it[0]}: {it[1]}" if len(it) > 2 else it[1] for it in v["items"]) \
            + f"      {v['foot']}"

    def key(self, ch):
        """Esc -> "cancel" (pokewalk.py llama a close()); el organizador además empieza con Enter."""
        return "cancel" if ch == "\x1b" else None


class Tournament(_TourLobby):
    """Torneo, lado del organizador: abre la sala (hasta MAX_PLAYERS, contándose), recibe a los invitados y
    les manda la lista de jugadores; con Enter (MIN_PLAYERS o más) cierra la sala y manda "start".
    `update()` devuelve una lista de eventos:
        ("join", id, nombre) / ("left", id, nombre)   entró / se fue alguien (en la sala o, ya empezado, se cortó)
        ("roster",)                                   cambió la lista de jugadores
        ("start", jugadores)                          empezó: la lista final, en orden
        ("msg", id, mensaje)                          cualquier otro mensaje de ese jugador
    `send(id, msg)` le manda algo a un jugador; `broadcast(msg)` a todos los invitados."""
    role = "host"

    def __init__(self, name, rules=None, room=None):
        """`room` = una `pokerelay.RelayRoom` ya abierta si el torneo es por internet (si no, sala en la red local)."""
        self.name = self.org = name
        self.rules, self.me = room_rules(rules), 0
        self.players = [{"id": 0, "name": name}]
        self.links = {}            # id -> Link de cada invitado
        self.gone = set()          # ids que se cortaron con el torneo ya empezado (siguen en la lista)
        self.stage = "lobby"
        self._pending = []         # conexiones entrantes que todavía no dijeron quiénes son
        self._next, self._out = 1, []
        self.code = getattr(room, "code", "")      # por internet: el código de la sala
        self.room = room or RoomHost(name, self.rules, backlog=MAX_PLAYERS, tour=self._tour())

    def _tour(self):
        return {"n": len(self.players), "max": MAX_PLAYERS}

    def _foot(self):
        n = len(self.players)
        if self.code:
            code = f"Código {self.code} (entran con v → 2 → 3 «Unirse con un código»)"
            go = f"Enter  empezar (mínimo {MIN_PLAYERS})" if n >= MIN_PLAYERS else f"Faltan {MIN_PLAYERS - n}"
            return f"{code} · {go}      Esc  cancela el torneo"
        if n >= MIN_PLAYERS:
            return f"Enter  empezar (mínimo {MIN_PLAYERS})      Esc  cancela el torneo"
        ips = ", ".join(self.room.ips) if self.room and self.room.ips else "la de esta compu"
        return (f"Faltan {MIN_PLAYERS - n} para empezar · entran con «Buscar salas» o por IP a {ips}"
                "      Esc  cancela el torneo")

    def _foot_started(self):
        if self.bracket and self.bracket["champ"] is not None:
            return "Esc  volver"
        go = "" if self.begun else "Enter  empezar la primera pelea      "
        return go + "Esc  cancela el torneo (para todos)"

    def key(self, ch):
        """Además de Esc: Enter abre la sala; ya empezado, Enter -> "go" (arrancar la primera pelea del cuadro)."""
        if self.stage == "started":
            if ch in ("\r", "\n") and not self.begun:
                return "go"
        elif ch in ("\r", "\n"):
            self.start()
            return None
        return super().key(ch)

    def send(self, pid, msg):
        """A un invitado (el organizador, id 0, no tiene conexión: lo suyo lo resuelve quien llama)."""
        link = self.links.get(pid)
        return link.send(msg) if link else False

    def broadcast(self, msg, exclude=()):
        for pid, link in list(self.links.items()):
            if pid not in exclude:
                link.send(msg)

    def start(self):
        """Cierra la sala a los que lleguen tarde y manda la lista final. False si todavía son pocos."""
        if self.stage != "lobby" or len(self.players) < MIN_PLAYERS:
            return False
        self.room.close()
        self.room = None
        self._drop_pending()
        self.stage = "started"
        self.broadcast({"t": "start", "players": self.players})
        self._out.append(("start", list(self.players)))
        return True

    def update(self):
        events, self._out = self._out, []
        if self.room:
            self._triage(events)
        for pid, link in list(self.links.items()):
            left = False
            for m in link.poll():
                t = m.get("t")
                if t in ("bye", "_closed"):
                    left = True
                    break
                if t != "hello":
                    events.append(("msg", pid, m))
            if left or not link.alive:
                self._drop(pid, events)
        return events

    def _triage(self, events):
        """Conexiones entrantes: un "probe" es alguien buscando salas (se le contesta y se corta); un "hello"
        es un jugador que entra (si hay lugar)."""
        while len(self._pending) < 16:
            link = self.room.accept()
            if not link:
                break
            self._pending.append((link, time.time()))
        for item in list(self._pending):
            link, t0 = item
            for m in link.poll():
                kind = m.get("t")
                if kind == "probe":
                    link.send({"t": "room", "v": PROTO, "name": self.name, "rules": self.rules, "tour": self._tour()})
                    link.close()
                    self._pending.remove(item)
                    break
                if kind == "hello":
                    self._pending.remove(item)
                    self._admit(link, m, events)
                    break
                if kind == "_closed":
                    self._pending.remove(item)
                    break
            else:
                if time.time() - t0 > PENDING_TTL:      # conectó y no dijo nada
                    link.close()
                    self._pending.remove(item)

    def _admit(self, link, m, events):
        why = ("version" if m.get("v") != PROTO else "started" if self.stage != "lobby"
               else "full" if len(self.players) >= MAX_PLAYERS else None)
        if why:
            link.close(why)
            return
        pid, self._next = self._next, self._next + 1
        name = base = _text(m.get("name"), 20) or "Entrenador"
        taken, n = {p["name"] for p in self.players}, 2
        while name in taken:                           # dos con el mismo nombre de usuario: "Ana 2"
            name, n = f"{base[:17]} {n}", n + 1
        self.players.append({"id": pid, "name": name})
        self.links[pid] = link
        link.send({"t": "hello", "v": PROTO, "name": self.name, "rules": self.rules, "tour": 1, "you": pid})
        events.append(("join", pid, name))
        self._roster(events)

    def _drop(self, pid, events):
        link = self.links.pop(pid, None)
        if link:
            link.close()
        name = next((p["name"] for p in self.players if p["id"] == pid), "?")
        events.append(("left", pid, name))
        if self.stage == "lobby":
            self.players = [p for p in self.players if p["id"] != pid]
            self._roster(events)
        else:                                          # ya empezado: la lista queda fija (el cuadro la usa)
            self.gone.add(pid)

    def _roster(self, events):
        self.broadcast({"t": "roster", "players": self.players, "max": MAX_PLAYERS})
        if self.room:
            self.room.set_tour(self._tour())
        events.append(("roster",))

    def _drop_pending(self):
        for link, _ in self._pending:
            link.close()
        self._pending = []

    def close(self):
        """Cierra el torneo y les avisa a todos."""
        if self.room:
            self.room.close()
            self.room = None
        self._drop_pending()
        for link in self.links.values():
            link.close("cancel")
        self.links = {}


class TournamentClient(_TourLobby):
    """Torneo, lado de un invitado: una conexión con el organizador. Se crea con `ip`/`port` (elegido en la lista
    de salas) o con un `link` ya saludado (entró por IP desde el versus) y `backlog` = lo que llegó con el saludo.
    `update()` devuelve una lista de eventos:
        ("roster",)             cambió la lista de jugadores
        ("start", jugadores)    empezó: la lista final, en orden
        ("msg", mensaje)        cualquier otro mensaje del organizador
        ("error", texto)        no pudo entrar, se cortó o el organizador cerró el torneo: cerrar
    `send(msg)` le manda algo al organizador."""
    role = "guest"

    def __init__(self, name, ip=None, port=TCP_PORT, link=None, backlog=()):
        self.name, self.org, self.me = name, "", None
        self.rules, self.players = room_rules(None), []
        self.link, self._backlog = link, list(backlog)
        self.target = f"{ip}:{port}"
        self._conn, self.closed, self._lock = None, False, threading.Lock()
        if link:
            self.stage = "hello"                       # el saludo ya lo mandó la sala de versus
        else:
            self.stage = "connecting"
            threading.Thread(target=self._connect, args=(ip, port), daemon=True).start()

    def _connect(self, ip, port):
        try:
            res = ("ok", Link(socket.create_connection((ip, port), timeout=5)))
        except (OSError, ValueError) as e:
            res = ("err", str(e) or e.__class__.__name__)
        with self._lock:
            if self.closed and res[0] == "ok":
                res[1].close()
                return
            self._conn = res

    def _foot(self):
        return f"Esperando a que {self.org} empiece...      Esc  salir del torneo"

    def send(self, msg):
        return self.link.send(msg) if self.link else False

    def update(self):
        if self.stage == "connecting":
            with self._lock:
                res, self._conn = self._conn, None
            if not res:
                return []
            if res[0] == "err":
                return [("error", f"No pude conectar con {self.target}: {res[1]}")]
            self.link, self.stage = res[1], "hello"
            self.link.send({"t": "hello", "v": PROTO, "name": self.name})
        if not self.link:
            return []
        events = []
        msgs, self._backlog = self._backlog + self.link.poll(), []
        for m in msgs:
            t, org = m.get("t"), self.org or "El organizador"
            try:
                if t == "hello" and self.stage == "hello":
                    if m.get("v") != PROTO:
                        return events + [("error", _BYE_WHY["version"])]
                    if not m.get("tour"):
                        return events + [("error", "Esa sala no es un torneo (es de 1 contra 1).")]
                    self.org = _text(m.get("name"), 20) or "Organizador"
                    self.rules, self.me, self.stage = room_rules(m.get("rules")), _int(m.get("you"), 1, 999), "lobby"
                elif t == "roster" and self.stage == "lobby":
                    self.players, self.max = players_from_wire(m["players"]), _int(m.get("max"), 2, MAX_PLAYERS)
                    events.append(("roster",))
                elif t == "start" and self.stage == "lobby":
                    self.players, self.stage = players_from_wire(m["players"]), "started"
                    events.append(("start", list(self.players)))
                elif t == "bracket" and self.stage == "started":
                    self.set_bracket(m)
                    events.append(("bracket", self.note))
                elif t in ("bye", "_closed") and self.bracket and self.bracket["champ"] is not None:
                    self.link.close()                  # ya hay campeón: se sigue viendo el final hasta Esc
                    self.link = None
                    return events
                elif t == "bye":
                    why = m.get("why")
                    txt = _BYE_WHY.get(why) or (
                        f"{org} canceló el torneo." if why == "cancel" and self.stage != "started"
                        else f"{org} cerró el torneo: se terminó para todos.")
                    return events + [("error", txt)]
                elif t == "_closed":
                    return events + [("error", f"Se cortó la conexión con {org}: se terminó el torneo.")]
                elif t != "hello":
                    events.append(("msg", m))
            except (KeyError, TypeError, ValueError, AttributeError):
                return events + [("error", f"{org} mandó algo que no entiendo.")]
        return events

    def close(self):
        with self._lock:
            self.closed = True
        if self.link:
            self.link.close("bye")
            self.link = None


# --------------------------------------------------------------------------- la batalla en red
class Session:
    """Lo que ve la escena: `act(acción)` / `send(índice)` para elegir, `poll()` para enterarse
    (("steps", pasos) cuando hay un turno para animar, ("end", texto) cuando se acaba o se cae todo)
    y `close()` al salir."""

    role = None                 # "host" | "guest"
    rules = None                # reglas de la sala (las pone pokewalk.py), para la revancha
    rematchable = True          # en el torneo no hay revancha

    def __init__(self, battle, link, peer):
        self.b, self.link, self.peer = battle, link, peer
        self.done = False       # la batalla terminó: ya no importa si el otro se va
        self.left = False
        self.want = self.peer_want = self.rematching = False       # revancha: quién la pidió
        self.out = []

    def _left(self, kind):
        if self.left:
            return
        self.left = True
        if self.done:                       # ya había terminado: solo se pierde la posibilidad de revancha
            self.out.append(("gone",))
            return
        self.out.append(("end", f"¡{self.peer} huyó del combate!  ¡Ganaste!" if kind == "bye"
                         else f"Se cortó la conexión con {self.peer}."))

    def _turn(self, m):
        """Un turno que mandó el árbitro (copia espejo): pasos para animar y el estado en que queda."""
        sizes = [len(x) for x in self.b.teams]
        steps = [step_from_wire(x, sizes) for x in m["steps"]]
        load_state(self.b, m["state"])
        self.done = self.b.over
        self.out.append(("steps", steps))

    # --- revancha: cuando los dos la piden se vuelve a la sala (misma conexión, sin IPs ni búsqueda)
    def rematch(self):
        """Pedir la revancha (después de terminada la batalla)."""
        if self.done and not self.left and not self.want:
            self.want = True
            self.link.send({"t": "rematch"})
            self._check_rematch()

    def _on_rematch(self):
        if not self.peer_want:
            self.peer_want = True
            if not self.want:
                self.out.append(("peer_rematch",))
            self._check_rematch()

    def _check_rematch(self):
        if self.want and self.peer_want and not self.rematching:
            self.rematching = True
            self.out.append(("rematch",))

    def _bad(self):
        if not self.done and not self.left:
            self.left = True
            self.out.append(("end", f"{self.peer} mandó algo que no entiendo. Se cierra el combate."))
            self.link.close("bye")

    def close(self):
        self.link.close(None if self.done else "flee")


PASS = ("pass", 0)             # jugada que pone el árbitro cuando a alguien se le acaba el tiempo (nunca llega por red)


class Referee:
    """El que resuelve los turnos, sin red: tiene la batalla de verdad, junta lo que elige cada lado y, cuando
    están los dos, resuelve. Lo usan el versus (`HostSession`) y el torneo (`MatchReferee`)."""

    def __init__(self, battle):
        self.b = battle
        self.choice = [None, None]

    def expected(self, s):
        """Qué tiene que mandar el lado `s`: "act" (su jugada), "send" (reemplazo) o None (nada)."""
        b = self.b
        if b.over:
            return None
        if b.pending[s]:
            return "send"
        return None if any(b.pending) else "act"

    def waiting(self):
        """Lados que todavía no eligieron lo que les toca (para el reloj del torneo)."""
        return [s for s in (0, 1) if self.expected(s) and self.choice[s] is None]

    def choose(self, s, kind, val):
        """Lo que eligió el lado `s`. Devuelve los pasos del turno si con esto se pudo resolver, si no None."""
        if kind != self.expected(s) or self.choice[s] is not None:
            return None
        # una jugada ilegible vale igual (queda []), y `_clean` la cambia por una válida: así nadie se cuelga
        self.choice[s] = val if kind == "send" else (list(val) if isinstance(val, (list, tuple)) else [])
        return self._resolve()

    def pass_turn(self, s):
        """Se le acabó el tiempo al lado `s`: pierde el turno (su Pokémon no hace nada; el otro juega igual). Si le
        tocaba elegir reemplazo, entra el primero que puede pelear. Devuelve los pasos (o None, como `choose`)."""
        kind = self.expected(s)
        if not kind or self.choice[s] is not None:
            return None
        self.choice[s] = PASS if kind == "act" else self.b.alive(s)[0]
        return self._resolve()

    def _clean(self, s, a):
        """La jugada de un lado, validada: una inválida se cambia por un ataque que se pueda usar."""
        if a is PASS:
            return PASS
        b, f = self.b, self.b.fighter(s)
        try:
            kind, i = a
        except (TypeError, ValueError):
            kind, i = None, None
        if kind == "switch" and isinstance(i, int) and i in b.alive(s) and i != b.active[s]:
            return ("switch", i)
        usable = [j for j in range(len(f.moves)) if f.pp[j] > 0]
        if not usable:
            return ("move", -1)
        return ("move", i) if kind == "move" and i in usable else ("move", usable[0])

    def _resolve(self):
        b = self.b
        need = [s for s in (0, 1) if b.pending[s]]
        if need:
            if any(self.choice[s] is None for s in need):
                return None
            steps = []
            for s in need:
                idx = self.choice[s]
                if not (isinstance(idx, int) and idx in b.alive(s) and idx != b.active[s]):
                    idx = b.best_replacement(s)
                steps += b.send(idx, s)
        else:
            if None in self.choice:
                return None
            steps = b.turn(self._clean(0, self.choice[0]), self._clean(1, self.choice[1]))
        self.choice = [None, None]
        return steps


def turn_msg(b, steps, flip=False, **extra):
    """{"t": "turn", "steps", "state"}: un turno resuelto, para quien juega el lado 1 si `flip`."""
    return {"t": "turn", "steps": [step_to_wire(x, flip) for x in steps], "state": dump_state(b, flip), **extra}


class HostSession(Session):
    """El árbitro del versus: tiene la batalla de verdad (lado 0 = yo, lado 1 = el invitado)."""
    role = "host"

    def __init__(self, battle, link, peer):
        super().__init__(battle, link, peer)
        self.ref = Referee(battle)

    def act(self, action):
        self._choose(0, "act", action)

    def send(self, idx):
        self._choose(0, "send", idx)

    def _choose(self, s, kind, val):
        steps = self.ref.choose(s, kind, val)
        if steps is None:
            return
        self.done = self.b.over
        self.link.send(turn_msg(self.b, steps, flip=True))
        self.out.append(("steps", steps))

    def poll(self):
        for m in self.link.poll():
            try:
                t = m.get("t")
                if t == "act":
                    self._choose(1, "act", m.get("a"))
                elif t == "send":
                    self._choose(1, "send", m.get("i"))
                elif t == "rematch":
                    self._on_rematch()
                elif t in ("bye", "_closed"):
                    self._left(t)
            except Exception:       # mensaje raro del otro lado: no tumbar la pantalla
                self._bad()
        out, self.out = self.out, []
        return out


class GuestSession(Session):
    """El invitado: juega sobre una copia espejo (él es el lado 0) y solo repite lo que decide el árbitro."""
    role = "guest"

    def act(self, action):
        self.link.send({"t": "act", "a": list(action)})

    def send(self, idx):
        self.link.send({"t": "send", "i": idx})

    def poll(self):
        for m in self.link.poll():
            try:
                t = m.get("t")
                if t == "turn":
                    self._turn(m)
                elif t == "rematch":
                    self._on_rematch()
                elif t in ("bye", "_closed"):
                    self._left(t)
            except Exception:
                self._bad()
        out, self.out = self.out, []
        return out


# --------------------------------------------------------------------------- torneo: las peleas
# El organizador arbitra TODAS las peleas (también las que no juega) con un `MatchReferee`; los dos que pelean le
# mandan su equipo y sus jugadas, y él les manda a ellos y a todos los demás (que miran) cada turno resuelto. Del
# lado de cada participante (invitados, y el organizador en su propia compu, sin socket) lo atiende un `MatchClient`.
# Toda pelea lleva "m": su número; lo de otra pelea se ignora. Jugador = {"id", "name"}; `a` es el lado 0 del árbitro.
#   org -> todos       {"t": "m_prep", "m", "a", "b", "rules"}       se arma la pelea: a y b, armen su equipo
#   peleador -> org    {"t": "m_team", "m", "fighters": [luchador...]}   su equipo (validado con `team_problem`)
#   org -> todos       {"t": "m_start", "m", "a", "b", "rules", "teams": [[luchador...] de a, [...] de b]}
#   peleador -> org    {"t": "act", "m", "n", "a": [tipo, i]} / {"t": "send", "m", "n", "i": índice}   como en el
#                      versus; n = turnos que ya vio (si el árbitro ya le pasó el turno por tiempo, se descarta)
#   org -> todos       {"t": "turn", "m", "steps", "state"}           a `b` dado vuelta (flip); al resto, desde `a`
#   org -> todos       {"t": "m_clock", "m", "secs", "who": [ids], "lost": [[id, n]...]}   arranca el reloj: `who`
#                      tienen `secs` para elegir; lost = turnos perdidos seguidos de cada uno
#   org -> todos       {"t": "m_note", "m", "text"}                    novedad (ej. un equipo completado al azar)
#   peleador -> org    {"t": "m_quit", "m"}                           abandonó (pierde la pelea)
#   org -> todos       {"t": "m_over", "m", "winner": id | None, "why": texto}   terminó (None: empate o se cayó todo)
# "m_prep" trae además "secs" (tiempo para el equipo) y "replay" (se repite la pelea con los mismos equipos).
class MatchSession(Session):
    """Una pelea del torneo vista desde un participante: una copia espejo de la batalla (lado 0 = yo; si solo miro,
    el jugador `a`) que se actualiza con los turnos del árbitro. No lee el Link: los mensajes se los pasa
    `MatchClient` (`feed`) y lo que elige sale por `send_fn`. `side` = mi lado (0 / 1 del árbitro) o None si miro;
    `names` / `ids` = (jugador de la izquierda, de la derecha) como se ven en mi pantalla."""
    role = "guest"
    rematchable = False

    def __init__(self, battle, mid, side, names, send_fn, rules=None, me=None, ids=(None, None)):
        super().__init__(battle, None, names[1] if side is not None else f"{names[0]} vs {names[1]}")
        self.mid, self.side, self.names, self.send_fn, self.rules, self.me = mid, side, names, send_fn, rules, me
        self.ids = tuple(ids)
        self.seen = 0                   # turnos recibidos (va con cada jugada)
        self.deadline, self.who, self.lost = None, [], {}

    def act(self, action):
        self.send_fn({"t": "act", "m": self.mid, "n": self.seen, "a": list(action)})

    def send(self, idx):
        self.send_fn({"t": "send", "m": self.mid, "n": self.seen, "i": idx})

    def clock_text(self, now=None):
        """El reloj del árbitro en sus últimos WARN_SECS: para quien elige ("Te quedan 12 s...") o, si miro, quiénes
        están eligiendo. None si no hay que mostrarlo."""
        if self.deadline is None or self.done or self.left:
            return None
        secs = math.ceil(self.deadline - (time.time() if now is None else now))
        if secs > WARN_SECS:
            return None
        secs = max(0, secs)
        if self.side is not None:
            if self.me not in self.who:
                return None
            k = self.lost.get(self.me, 0)
            return f"Te quedan {secs} s para elegir" + (f" (turnos perdidos seguidos: {k} de {MAX_PASSES})" if k else "")
        who = [self.names[self.ids.index(p)] for p in self.who if p in self.ids]
        return f"{' y '.join(who)}: {secs} s para elegir" if who else None

    def feed(self, m):
        try:
            if m.get("t") == "turn":
                self.seen += 1
                self._turn(m)
            elif m.get("t") == "m_clock":
                self.deadline = time.time() + _int(m.get("secs"), 0, 600)
                self.who = [_int(x, 0, 999) for x in list(m.get("who") or [])[:2]]
                self.lost = {_int(p, 0, 999): _int(n, 0, 99) for p, n in list(m.get("lost") or [])[:2]}
            elif m.get("t") == "m_over":
                why = _text(m.get("why"), 120) or "Terminó la pelea."
                if self.side is not None and m.get("winner") is not None:
                    why += "  ¡Ganaste!" if m.get("winner") == self.me else "  Perdiste."
                self.abort(why)
        except Exception:                               # algo raro del árbitro: no tumbar la pantalla
            self.abort("El organizador mandó algo que no entiendo. Se corta la pelea.")

    def abort(self, text):
        """Se terminó por algo que no fue la batalla (abandono, desconexión, se cerró el torneo)."""
        if not self.done and not self.left:
            self.left = True
            self.out.append(("end", text))

    def poll(self):
        out, self.out = self.out, []
        return out

    def close(self):
        if self.side is not None and not self.done and not self.left:
            self.send_fn({"t": "m_quit", "m": self.mid})        # salir a mitad de la pelea = perderla
        self.left = True


class MatchTeam:
    """Tu equipo para una pelea del torneo. Se hace pasar por la sala de versus (`Versus`) para que pokewalk.py
    use el mismo camino de siempre: el prompt del equipo, los ataques y `set_team`. Hay `secs` para mandarlo: al
    vencer (`late()`), pokewalk.py manda lo que haya (lo escrito que valga, el resto al azar; los ataques sorteados)
    y marca `auto`. Si ni así llega, el árbitro lo completa él."""
    tour_match = True
    role = "guest"
    prev_team = prev_sprites = None

    def __init__(self, name, mid, foe, rules, send_fn, secs=None, clock=time.time):
        self.name, self.mid, self.peer, self.rules, self.send_fn = name, mid, foe, room_rules(rules), send_fn
        self.stage = "team"
        self.my = self.my_sprites = self.foe = None
        self.sent = False
        self.auto = False               # se acabó el tiempo: lo que se cargue se manda sin elegir ataques
        self.clock = clock
        self.deadline = None if secs is None else clock() + secs

    def secs_left(self):
        return None if self.deadline is None else max(0, math.ceil(self.deadline - self.clock()))

    def late(self):
        """Se acabó el tiempo y todavía no se mandó (ni se está mandando solo)."""
        return self.deadline is not None and not self.sent and not self.auto and self.clock() >= self.deadline

    def set_team(self, fighters, sprites):
        self.my, self.my_sprites, self.stage, self.sent = fighters, sprites, "waiting", True
        self.send_fn({"t": "m_team", "m": self.mid, "fighters": [fighter_to_wire(f) for f in fighters]})

    def update(self):
        return None

    def key(self, ch):
        return "cancel" if ch == "\x1b" else None

    def view(self):
        return None

    def text(self):
        if self.stage == "waiting":
            return f"Tu equipo está listo. Esperando a que {self.peer} arme el suyo..."
        return f"Torneo · tu pelea contra {self.peer} · armá tu equipo..."

    def close(self):
        if not self.sent:                               # salir sin mandar equipo = perder la pelea
            self.send_fn({"t": "m_quit", "m": self.mid})
            self.sent = True


class MatchClient:
    """Las peleas del torneo desde un participante (un invitado, o el organizador en su compu). `handle(msg)` toma
    un mensaje de pelea y devuelve eventos para pokewalk.py:
        ("team", MatchTeam)       te toca pelear: armá tu equipo (MatchTeam hace de sala de versus)
        ("start", MatchSession)   arrancó la pelea (la jugás o la mirás): armar la escena
        ("note", texto)           novedad para mostrar en la sala del torneo
        ("over", ganador, texto)  terminó (la escena se entera por su sesión)
    `send_fn(msg)` le hace llegar algo al árbitro; `make_chart()` da la tabla de tipos para la copia espejo."""

    def __init__(self, me, name, send_fn, make_chart):
        self.me, self.name, self.send_fn, self.make_chart = me, name, send_fn, make_chart
        self.session = None
        self.mid = None

    def handle(self, m):
        t = m.get("t")
        try:
            if t == "m_prep":
                return self._prep(m)
            if t == "m_start":
                return self._start(m)
            if t == "m_note" and m.get("m") == self.mid:
                return [("note", _text(m.get("text"), 160))]
            if t in ("turn", "m_clock", "m_over") and m.get("m") == self.mid:
                if self.session:
                    self.session.feed(m)
                if t == "m_over":
                    win = m.get("winner")
                    return [("over", win if isinstance(win, int) else None, _text(m.get("why"), 120))]
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            return [("note", "El organizador mandó una pelea que no entiendo.")]
        return []

    def _prep(self, m):
        a, b = players_from_wire([m["a"], m["b"]])
        self.mid, self.session = m["m"], None
        if m.get("replay"):                             # se repite con los mismos equipos: no hay que armar nada
            return [("note", f"Se repite la pelea {a['name']} vs {b['name']} con los mismos equipos")]
        if self.me in (a["id"], b["id"]):
            foe = b if self.me == a["id"] else a
            secs = _int(m["secs"], 1, 600) if "secs" in m else None
            return [("team", MatchTeam(self.name, self.mid, foe["name"], m.get("rules"), self.send_fn, secs)),
                    ("note", f"¡Te toca! Tu pelea: contra {foe['name']}. Armá tu equipo"
                             + (f" ({secs} s)." if secs else "."))]
        return [("note", f"Próxima pelea: {a['name']} vs {b['name']} (están armando sus equipos)")]

    def _start(self, m):
        a, b = players_from_wire([m["a"], m["b"]])
        self.mid = m["m"]                               # (también si me perdí el "m_prep")
        teams =[team_from_wire(x) for x in list(m["teams"])[:2]]
        rules = room_rules(m.get("rules"))
        side = 0 if self.me == a["id"] else 1 if self.me == b["id"] else None
        names, ids = (a["name"], b["name"]), (a["id"], b["id"])
        if side == 1:                                   # el árbitro me tiene en el lado 1: yo me veo a la izquierda
            teams.reverse()
            names, ids = names[::-1], ids[::-1]
        battle = pb.Battle(teams[0], teams[1], self.make_chart())
        battle.human = [True, True]
        self.session = MatchSession(battle, self.mid, side, names, self.send_fn, rules, self.me, ids)
        what = "¡A pelear!" if side is not None else "Mirando:"
        return [("start", self.session), ("note", f"{what} {a['name']} vs {b['name']}")]


class MatchReferee:
    """El organizador arbitra una pelea del torneo entre los jugadores `a` y `b` (ids; `a` es el lado 0), la juegue
    él o no. Junta los equipos, arma la batalla (la tabla de tipos `chart` se precarga en un hilo) y resuelve cada
    turno cuando están las dos jugadas; se lo manda a los dos y a todos los que miran. Lo del organizador (id 0, sin
    socket) queda en `local` y pokewalk.py lo toma con `take_local()`; lo que él manda entra por `receive(0, msg)`.
    `update(eventos del torneo)` toma los mensajes de esta pelea y las desconexiones, mira el reloj y devuelve:
        ("started",)                    los dos equipos están y arrancó la batalla
        ("turn", pasos)                 se resolvió un turno (el reloj vuelve a empezar)
        ("over", ganador_id, texto)     terminó (ganador None: empate o no se pudo armar)
    El reloj es del árbitro (`clock` se puede cambiar para las pruebas), así un cliente tocado no traba nada:
      * equipos: TEAM_SECS (+ TEAM_GRACE de changüí); al que no mandó, el árbitro le arma uno al azar con
        `build_team(n, reglas)` (en un hilo). Si no hay cómo, pierde la pelea.
      * pelea: TURN_SECS por decisión (más lo que tarda en animarse el turno). Al vencer pierde el turno (su Pokémon
        no hace nada, el otro juega igual); MAX_PASSES seguidos = pierde la pelea. Una jugada de verdad pone la
        cuenta en 0. Si les pasa a los dos a la vez, la pelea queda sin ganador.
    `teams` = equipos ya armados (se repite una pelea que quedó sin ganador): se usan copias nuevas.
    También: `waiting_for()` (quiénes deben algo), `pass_turn(id)` (pierde el turno ya), `forfeit(id, texto)`."""
    _seq = 0

    def __init__(self, tour, a, b, chart, clock=time.time, build_team=None, teams=None):
        MatchReferee._seq += 1
        self.mid, self.tour, self.ids, self.chart = MatchReferee._seq, tour, [a, b], chart
        self.clock, self.build_team = clock, build_team
        self.players = [tour.player(a) or {"id": a, "name": "?"}, tour.player(b) or {"id": b, "name": "?"}]
        self.teams = [None, None]
        self.stage = "teams"            # teams -> loading -> battle -> over
        self.ref = None
        self.winner = None
        self.local, self._out = [], []
        self._chart_ok = threading.Event()
        self._chart_err = ""
        self.lost = [0, 0]              # turnos perdidos seguidos (por tiempo) de cada lado
        self.nturn = 0                  # turnos resueltos (cada jugada dice cuántos vio: una vieja se descarta)
        self.anim = 0.0                 # lo que tarda en animarse el último turno
        self.filling = [None, None]     # equipo al azar del árbitro: None | "busy" | ("ok", equipo) | ("err", texto)
        replay = bool(teams and all(teams))
        self.deadline = clock() + TEAM_SECS + TEAM_GRACE
        self._all({"t": "m_prep", "m": self.mid, "a": self.players[0], "b": self.players[1], "rules": tour.rules,
                   "secs": TEAM_SECS, "replay": replay})
        if replay:                                      # copias nuevas: con todo el HP y los PP
            self.teams = [team_from_wire([fighter_to_wire(f) for f in t]) for t in teams]
            self._loading()

    # --- a quién le llega
    def _to(self, pid, msg):
        if pid == 0:
            self.local.append(msg)
        else:
            self.tour.send(pid, msg)

    def _all(self, msg):
        for p in self.tour.players:
            if p["id"] not in self.tour.gone:
                self._to(p["id"], msg)

    def _note(self, text):
        self._all({"t": "m_note", "m": self.mid, "text": text})

    def take_local(self):
        """Mensajes para el organizador (su `MatchClient`), en orden."""
        out, self.local = self.local, []
        return out

    def _name(self, pid):
        return self.players[self.ids.index(pid)]["name"]

    # --- lo que llega
    def update(self, events=()):
        for ev in events:
            if ev[0] == "msg":
                self.receive(ev[1], ev[2])
            elif ev[0] == "left" and ev[1] in self.ids:
                self.forfeit(ev[1], f"Se cortó la conexión con {self._name(ev[1])}.")
        self.tick()
        if self.stage == "loading" and self._chart_ok.is_set():
            if self._chart_err:
                self._end(None, f"No pude preparar la pelea: {self._chart_err}")
            else:
                self._begin()
        out, self._out = self._out, []
        return out

    def receive(self, pid, m):
        """Un mensaje de un jugador (el organizador es el id 0)."""
        if not isinstance(m, dict) or m.get("m") != self.mid or pid not in self.ids or self.stage == "over":
            return
        s, t = self.ids.index(pid), m.get("t")
        try:
            if t == "m_team" and self.stage == "teams" and self.teams[s] is None and self.filling[s] is None:
                self._team(s, m)
            elif t in ("act", "send") and self.stage == "battle":
                if m.get("n") != self.nturn or s not in self.ref.waiting() or t != self.ref.expected(s):
                    return                              # tardía (ya se le pasó el turno) o repetida
                self.lost[s] = 0
                steps = self.ref.choose(s, t, m.get("a") if t == "act" else m.get("i"))
                if steps is not None:
                    self._turn(steps)
            elif t == "m_quit":
                self.forfeit(pid, f"{self._name(pid)} abandonó la pelea.")
        except Exception as e:                          # nada de un jugador puede tumbar al organizador
            self._end(None, f"Se cortó la pelea por un error: {e}")

    def _team(self, s, m):
        rules = self.tour.rules
        try:
            team = team_from_wire(m["fighters"])
            problem = ("más Pokémon que los de la sala" if len(m["fighters"]) > rules["size"]
                       else team_problem(team, rules))
        except (KeyError, TypeError, ValueError, AttributeError):
            problem = "no se entiende"
        if problem:                                     # un cliente tocado se salta el chequeo del prompt
            self.forfeit(self.ids[s], f"{self.players[s]['name']} mandó un equipo inválido ({problem}).")
            return
        self.teams[s] = team
        if all(self.teams):
            self._loading()

    def _loading(self):
        self.stage = "loading"
        types = {mv.type for t in self.teams for f in t for mv in f.moves}
        threading.Thread(target=self._preload, args=(types,), daemon=True).start()

    def _preload(self, types):
        try:
            self.chart.preload(types)
        except Exception as e:                          # sin internet la primera vez
            self._chart_err = str(e) or e.__class__.__name__
        self._chart_ok.set()

    def _build(self, s):
        """Hilo: el equipo al azar del lado `s` (se le acabó el tiempo)."""
        rules = self.tour.rules
        try:
            team = list(self.build_team(rules["size"], rules))
            problem = "vacío" if not team else team_problem(team, rules)
            self.filling[s] = ("err", problem) if problem else ("ok", team)
        except Exception as e:                          # sin internet...
            self.filling[s] = ("err", str(e) or e.__class__.__name__)

    def _begin(self):
        battle = pb.Battle(self.teams[0], self.teams[1], self.chart)
        battle.human = [True, True]
        self.ref, self.stage = Referee(battle), "battle"
        self._all({"t": "m_start", "m": self.mid, "a": self.players[0], "b": self.players[1], "rules": self.tour.rules,
                   "teams": [[fighter_to_wire(f) for f in t] for t in self.teams]})
        self._out.append(("started",))
        self._clock(TURN_SECS + START_GRACE)

    def _turn(self, steps):
        b = self.ref.b
        self.nturn += 1
        self.anim = min(ANIM_MAX, STEP_SECS * len(steps))
        plain, flipped = turn_msg(b, steps, m=self.mid), turn_msg(b, steps, flip=True, m=self.mid)
        for p in self.tour.players:
            if p["id"] not in self.tour.gone:
                self._to(p["id"], flipped if p["id"] == self.ids[1] else plain)
        self._out.append(("turn", steps))
        if b.over:
            win = None if b.winner is None else self.ids[b.winner]
            self._end(win, "¡Empate!" if win is None else f"¡Ganó {self._name(win)}!")
        else:
            self._clock(TURN_SECS + self.anim)

    def _end(self, winner, why):
        if self.stage == "over":
            return
        self.stage, self.winner = "over", winner
        self._all({"t": "m_over", "m": self.mid, "winner": winner, "why": why})
        self._out.append(("over", winner, why))

    # --- el reloj
    def _clock(self, secs):
        """Vuelve a empezar el reloj de las decisiones y les avisa a todos (para que lo muestren)."""
        self.deadline = self.clock() + secs
        self._all({"t": "m_clock", "m": self.mid, "secs": math.ceil(secs), "who": self.waiting_for(),
                   "lost": [[pid, n] for pid, n in zip(self.ids, self.lost)]})

    def tick(self):
        """Lo que pasa cuando vence el reloj: equipos al azar, turnos perdidos."""
        now = self.clock()
        if self.stage == "teams":
            self._fill(now)
        elif self.stage == "battle" and now >= self.deadline:
            late = self.ref.waiting()
            for s in late:
                self.lost[s] += 1
            out = [s for s in late if self.lost[s] >= MAX_PASSES]
            if len(out) == 2:
                self._end(None, f"{self.players[0]['name']} y {self.players[1]['name']} perdieron {MAX_PASSES} "
                                "turnos seguidos (se les acabó el tiempo).")
            elif out:
                self.forfeit(self.ids[out[0]], f"{self.players[out[0]]['name']} perdió {MAX_PASSES} turnos "
                                               "seguidos (se le acabó el tiempo).")
            else:
                for s in late:
                    steps = self.ref.pass_turn(s)
                    if steps is not None:
                        self._turn(steps)
                if self.stage == "battle" and self.deadline <= now:      # (nada que resolver: otra vuelta)
                    self._clock(TURN_SECS)

    def _fill(self, now):
        """Vencido el tiempo de los equipos: el árbitro arma uno al azar para cada uno que no mandó el suyo."""
        missing = [s for s in (0, 1) if self.teams[s] is None]
        if not missing or now < self.deadline:
            return
        for s in missing:
            if self.filling[s] is None:
                if self.build_team is None:
                    self.filling[s] = ("err", "no hay cómo armarlo")
                else:
                    self.filling[s] = "busy"
                    threading.Thread(target=self._build, args=(s,), daemon=True).start()
                    self._note(f"Se le acabó el tiempo a {self.players[s]['name']}: le toca un equipo al azar.")
        if any(not isinstance(self.filling[s], tuple) for s in missing):
            return                                      # todavía se están armando
        bad = [s for s in missing if self.filling[s][0] != "ok"]
        if len(bad) == 2:
            self._end(None, f"Nadie armó su equipo a tiempo y no pude completarlos ({self.filling[bad[0]][1]}).")
        elif bad:
            s = bad[0]
            self.forfeit(self.ids[s], f"{self.players[s]['name']} no armó su equipo a tiempo "
                                      f"(y no pude completarlo: {self.filling[s][1]}).")
        else:
            for s in missing:
                self.teams[s] = self.filling[s][1]
            self._loading()

    def waiting_for(self):
        """Ids de los que deben algo: su equipo, su jugada o su reemplazo."""
        if self.stage == "teams":
            return [pid for pid, t in zip(self.ids, self.teams) if t is None]
        if self.stage == "battle":
            return [self.ids[s] for s in self.ref.waiting()]
        return []

    def pass_turn(self, pid):
        """`pid` pierde el turno ya (sin contarlo como tiempo vencido). True si había algo que elegir."""
        if self.stage != "battle" or pid not in self.ids:
            return False
        s = self.ids.index(pid)
        if s not in self.ref.waiting():
            return False
        steps = self.ref.pass_turn(s)
        if steps is not None:
            self._turn(steps)
        return True

    def forfeit(self, pid, why):
        """`pid` pierde la pelea (abandono, desconexión, equipo inválido, turnos perdidos)."""
        if self.stage != "over" and pid in self.ids:
            self._end(self.ids[1 - self.ids.index(pid)], why)


class TourDirector:
    """El torneo desde el organizador: sortea el cuadro con los jugadores (`make_bracket`), juega las peleas de a una
    y en orden (cada una con su `MatchReferee`) y le manda el cuadro a todos con cada novedad. La primera pelea
    arranca con `go()` (Enter del organizador); las siguientes solas, NEXT_SECS después de la anterior (más lo que
    tarda en animarse su último turno). Reglas:
      * desconexión = pierde la pelea que esté jugando y queda afuera; si le tocaba otra, el rival pasa sin pelear.
      * pelea sin ganador (empate, los dos sin tiempo, no se pudo preparar): se repite UNA vez con los mismos
        equipos; si otra vez no hay ganador, una moneda (`rng`) elige quién pasa, y se avisa a todos.
    `update(eventos del torneo)` devuelve ("bracket", texto) cuando cambia el cuadro y ("champion", id) al final.
    Lo del organizador como jugador sale por `take_local()` (para su `MatchClient`) y entra por `receive(0, msg)`."""

    def __init__(self, tour, make_chart, build_team=None, rng=random, clock=time.time):
        self.tour, self.make_chart, self.build_team, self.rng, self.clock = tour, make_chart, build_team, rng, clock
        self.rounds = make_bracket([p["id"] for p in tour.players if p["id"] not in tour.gone] or [0], rng)
        self.ref, self.cur = None, None
        self.begun, self.done = False, False
        self.next_at = None             # cuándo arranca la próxima pelea (None: espera el Enter del organizador)
        self.tries = 0                  # veces que la pelea actual quedó sin ganador
        self.teams = None               # equipos para repetirla
        self._local, self._out = [], []
        self._publish(f"Cuadro sorteado: {len(tour.players)} jugadores")

    def go(self):
        """Enter del organizador: arranca la primera pelea."""
        if not self.begun:
            self.begun = True
            self.next_at = self.clock()
            if champion(self.rounds) is not None:       # (se fueron todos antes de empezar)
                self._after("")

    def receive(self, pid, msg):
        if self.ref:
            self.ref.receive(pid, msg)

    def take_local(self):
        out = self._local + (self.ref.take_local() if self.ref else [])
        self._local = []
        return out

    def update(self, events=()):
        left = [ev[1] for ev in events if ev[0] == "left"]
        n = len(self._out)
        if self.ref:
            for ev in self.ref.update(events):
                if ev[0] == "over":
                    self._result(ev[1], ev[2])
        if not self.ref and not self.done and self.begun and self.clock() >= self.next_at:
            self._next()
        if left and not self.done and len(self._out) == n:     # se fue alguien que no estaba peleando: se ve en el cuadro
            self._publish(", ".join(self.tour.name_of(p) for p in left) + " se fue del torneo.")
        out, self._out = self._out, []
        return out

    def _publish(self, note=""):
        champ = champion(self.rounds)
        wait = None if self.ref or self.done or not self.begun else max(0, math.ceil(self.next_at - self.clock()))
        msg = {"t": "bracket", "rounds": self.rounds, "cur": list(self.cur) if self.ref else None,
               "gone": sorted(self.tour.gone), "champ": champ, "begun": self.begun, "next": wait, "note": note}
        self.tour.broadcast(msg)
        self.tour.set_bracket(msg)                      # el organizador lo ve igual que los demás
        self._out.append(("bracket", note))

    def _name(self, pid):
        return "nadie" if pid == EMPTY else self.tour.name_of(pid)

    def _result(self, winner, why):
        r, j = self.cur
        a, b = self.ref.ids
        self._local += self.ref.take_local()
        wait = NEXT_SECS + self.ref.anim
        if winner is None:
            self.tries += 1
            if self.tries == 1:                         # se repite una vez, con los mismos equipos
                self.teams = self.ref.teams if all(self.ref.teams) else None
                self.ref, self.next_at = None, self.clock() + wait
                self._publish(f"{why} Sin ganador: se repite {self._name(a)} vs {self._name(b)} (una sola vez).")
                return
            live = [x for x in (a, b) if x not in self.tour.gone] or [a, b]
            winner = self.rng.choice(live)
            why = f"{why} Otra vez sin ganador: la moneda eligió a {self._name(winner)}."
        self.ref, self.teams, self.tries = None, None, 0
        advance(self.rounds, r, j, winner)
        self._after(f"{why} {self._name(winner)} pasa de ronda." if champion(self.rounds) is None else why, wait)

    def _after(self, note, wait=0.0):
        champ = champion(self.rounds)
        if champ is None:
            self.next_at = self.clock() + wait
            self._publish(note)
            return
        self.done = True
        self._publish((note + " " if note else "") + ("Se fueron todos: el torneo terminó sin campeón." if champ == EMPTY
                                                       else f"¡{self._name(champ)} es el campeón del torneo!"))
        self._out.append(("champion", champ))

    def _next(self):
        """Arranca la próxima pelea del cuadro; antes, los que se fueron pierden sin pelear (walkover)."""
        notes = []
        while True:
            nm = next_match(self.rounds)
            if nm is None:
                self._after(" ".join(notes))
                if not self.done:                       # (no debería pasar: cuadro sin peleas y sin campeón)
                    self.done = True
                return
            r, j = nm
            a, b = self.rounds[r][j][:2]
            out = [x for x in (a, b) if x in self.tour.gone]
            if not out:
                break
            w = EMPTY if len(out) == 2 else b if a in out else a
            self.teams, self.tries = None, 0
            advance(self.rounds, r, j, w)
            notes.append(f"{self._name(w)} pasa sin pelear ({' y '.join(self._name(x) for x in out)} se fue).")
            if champion(self.rounds) is not None:
                self._after(" ".join(notes))
                return
        self.cur = (r, j)
        self.ref = MatchReferee(self.tour, a, b, self.make_chart(), self.clock, self.build_team, self.teams)
        what = f"{round_name(r, len(self.rounds))}: {self._name(a)} vs {self._name(b)}"
        self._publish(" ".join(notes + [what + (" (se repite)" if self.teams else "")]))
