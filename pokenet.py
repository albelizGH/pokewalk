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

Protocolo: una línea de JSON por mensaje, sobre TCP. Nada de esto es seguro contra un rival tramposo
(los equipos los arma cada uno): está pensado para jugar con amigos, no para torneos.
"""
import base64
import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict

import pokebattle as pb

PROTO = 1
TCP_PORT = 5757         # puerto de la sala (si está ocupado se usa otro, y el beacon avisa cuál)
UDP_PORT = 5758         # puerto donde se anuncian las salas
MAX_TEAM = 6
BEACON_EVERY = 1.0      # s entre avisos de la sala
ROOM_TTL = 4.0          # s sin aviso para que una sala desaparezca de la lista
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

    def __init__(self, name):
        self.ips = local_ips()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("", TCP_PORT))
        except OSError:                              # ocupado (otro pokewalk en esta compu): uno libre
            srv.bind(("", 0))
        srv.listen(1)
        srv.setblocking(False)
        self.srv, self.port = srv, srv.getsockname()[1]
        self.beacon = json.dumps({"app": "pokewalk", "v": PROTO, "name": name, "port": self.port}).encode()
        self.stop = threading.Event()
        threading.Thread(target=self._announce, daemon=True).start()

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


class RoomFinder:
    """Escucha los avisos de salas de la red. `rooms()` = las que avisaron hace poco."""

    def __init__(self):
        self.found = {}
        self.error = ""
        self.sock = None
        self.stop = threading.Event()
        self._lock = threading.Lock()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", UDP_PORT))
            s.settimeout(0.5)
        except OSError as e:
            self.error = f"No pude escuchar salas ({e})"
            return
        self.sock = s
        threading.Thread(target=self._listen, daemon=True).start()

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
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            with self._lock:
                self.found[(addr[0], port)] = (name, time.time())

    def rooms(self):
        now = time.time()
        with self._lock:
            live = [(n, ip, p) for (ip, p), (n, t) in self.found.items() if now - t < ROOM_TTL]
        return [{"name": n, "ip": ip, "port": p} for n, ip, p in sorted(live)]

    def close(self):
        self.stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- la sala: de conectarse a empezar
class Versus:
    """Todo lo previo a la batalla. Etapas:
        menu -> (hosting | browsing | pidiendo IP) -> connecting -> hello -> team (armás tu equipo)
        -> loading -> waiting (el otro todavía no terminó) -> starting -> la batalla (la arma pokewalk.py).
    pokewalk.py llama a `update()` en cada cuadro y reacciona a lo que devuelve:
        ("team",)        conectados: pedile al jugador su equipo
        ("ready",)       los dos equipos están listos: armar la batalla
        ("error", txt)   se cortó o falló algo: cerrar la sala
    """

    def __init__(self, name):
        self.name, self.peer, self.role = name, "", None
        self.stage = "menu"
        self.room = self.finder = self.link = None
        self.target = ""
        self.my = self.my_sprites = self.foe = None
        self.closed = False
        self._conn = None
        self._fail = ""
        self._fw = None
        self.noperm = False        # Windows no dio el permiso: puede que el amigo no pueda conectarse
        self._lock = threading.Lock()
        self._hello = False
        if os.name == "nt" and not _fw_ok:             # antes de nada: que Windows deje pasar a pokewalk
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
        if st == "menu":
            warn = "⚠ Sin permiso de Windows (salí y entrá de nuevo y aceptá el aviso) · " if self.noperm else ""
            return warn + "Versus en la red local:   1 Crear sala   2 Buscar salas   3 Conectar por IP      Esc cancela"
        if st == "hosting":
            where = ", ".join(self.room.ips) if self.room and self.room.ips else "esta compu"
            return f"Sala «{self.name}» abierta ({where}) · esperando a un amigo...      Esc cierra la sala"
        if st == "browsing":
            if self.finder and self.finder.error:
                return f"{self.finder.error}   ·   i = conectar por IP   0 volver   Esc cancela"
            rooms = self.finder.rooms() if self.finder else []
            if not rooms:
                return "Buscando salas en la red...   (si no aparece: i = conectar por IP)   0 volver   Esc cancela"
            return "Salas:  " + "   ".join(f"{i + 1} {r['name']}" for i, r in enumerate(rooms[:9])) + \
                   "      i = por IP   0 volver   Esc cancela"
        if st == "connecting":
            return f"Conectando con {self.target}...      Esc cancela"
        if st == "hello":
            return "Conectado, saludando...      Esc cancela"
        if st == "team":
            return "Armá tu equipo..."
        if st == "loading":
            return "Buscando tu equipo en internet... (la primera vez tarda)"
        if st == "waiting":
            extra = " (¡ya armó el suyo!)" if self.foe else ""
            return f"Tu equipo está listo. Esperando a {self.peer}...{extra}      Esc cancela"
        return "Preparando la batalla..."

    # --- teclado: devuelve "cancel" | "ip" | None
    def key(self, ch):
        if ch == "\x1b":
            return "cancel"
        if self.stage == "menu":
            if ch == "1":
                self.host()
            elif ch == "2":
                self.browse()
            elif ch in ("3", "i", "I"):
                return "ip"
        elif self.stage == "browsing":
            if ch in ("i", "I"):
                return "ip"
            if ch == "0":
                self._stop_finder()
                self.stage = "menu"
            elif ch.isdigit() and self.finder:
                rooms = self.finder.rooms()
                if 1 <= int(ch) <= len(rooms[:9]):
                    r = rooms[int(ch) - 1]
                    self.connect(r["ip"], r["port"])
        return None

    # --- pasos
    def host(self):
        try:
            self.room = RoomHost(self.name)
        except OSError as e:
            self._fail = f"No pude abrir la sala: {e}"
            return
        self.role, self.stage = "host", "hosting"

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
        link.send({"t": "hello", "v": PROTO, "name": self.name})

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
        if self.stage == "firewall":
            with self._lock:
                res = self._fw
            if not res:
                return None
            self.stage, self.noperm = "menu", res == "declined"
            return None
        if self.stage == "hosting":
            link = self.room.accept()
            if link:
                self.room.close()
                self.room = None
                self._linked(link)
        elif self.stage == "connecting":
            with self._lock:
                res, self._conn = self._conn, None
            if res and res[0] == "ok":
                self._linked(res[1])
            elif res:
                return ("error", f"No pude conectar con {self.target}: {res[1]}")
        if not self.link:
            return None
        event = None
        for m in self.link.poll():
            t = m.get("t")
            who = self.peer or "El otro jugador"
            if t == "hello" and not self._hello:
                if m.get("v") != PROTO:
                    return ("error", "El otro pokewalk es de otra versión: actualicen los dos.")
                self.peer, self._hello = _text(m.get("name"), 20) or "Rival", True
                if self.stage == "hello":
                    self.stage, event = "team", ("team",)
            elif t == "team":
                try:
                    self.foe = team_from_wire(m["fighters"])
                except (KeyError, TypeError, ValueError, AttributeError):
                    return ("error", f"{who} mandó un equipo inválido.")
            elif t == "bye":
                return ("error", f"{who} se fue de la sala.")
            elif t == "_closed":
                return ("error", f"Se cortó la conexión con {who}.")
        if event:
            return event
        if self.my and self.foe and self.stage == "waiting":
            self.stage = "starting"
            return ("ready",)
        return None

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
        if self.link:
            self.link.close("bye")
            self.link = None


# --------------------------------------------------------------------------- la batalla en red
class Session:
    """Lo que ve la escena: `act(acción)` / `send(índice)` para elegir, `poll()` para enterarse
    (("steps", pasos) cuando hay un turno para animar, ("end", texto) cuando se acaba o se cae todo)
    y `close()` al salir."""

    def __init__(self, battle, link, peer):
        self.b, self.link, self.peer = battle, link, peer
        self.done = False       # la batalla terminó: ya no importa si el otro se va
        self.left = False
        self.out = []

    def _left(self, kind):
        if self.done or self.left:
            return
        self.left = True
        self.out.append(("end", f"¡{self.peer} huyó del combate!  ¡Ganaste!" if kind == "bye"
                         else f"Se cortó la conexión con {self.peer}."))

    def _bad(self):
        if not self.done and not self.left:
            self.left = True
            self.out.append(("end", f"{self.peer} mandó algo que no entiendo. Se cierra el combate."))
            self.link.close("bye")

    def close(self):
        self.link.close(None if self.done else "flee")


class HostSession(Session):
    """El árbitro: tiene la batalla de verdad (lado 0 = yo, lado 1 = el invitado)."""

    def __init__(self, battle, link, peer):
        super().__init__(battle, link, peer)
        self.choice = [None, None]

    def act(self, action):
        self._choose(0, "act", action)

    def send(self, idx):
        self._choose(0, "send", idx)

    def _expected(self, s):
        b = self.b
        if b.over:
            return None
        if b.pending[s]:
            return "send"
        return None if any(b.pending) else "act"

    def _choose(self, s, kind, val):
        if kind != self._expected(s) or self.choice[s] is not None:
            return
        # una jugada ilegible vale igual (queda []), y `_clean` la cambia por una válida: así nadie se cuelga
        self.choice[s] = val if kind == "send" else (list(val) if isinstance(val, (list, tuple)) else [])
        self._resolve()

    def _clean(self, s, a):
        """La jugada del invitado, validada: una inválida se cambia por un ataque que se pueda usar."""
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
                return
            steps = []
            for s in need:
                idx = self.choice[s]
                if not (isinstance(idx, int) and idx in b.alive(s) and idx != b.active[s]):
                    idx = b.best_replacement(s)
                steps += b.send(idx, s)
        else:
            if None in self.choice:
                return
            steps = b.turn(self._clean(0, self.choice[0]), self._clean(1, self.choice[1]))
        self.choice = [None, None]
        self.done = b.over
        self.link.send({"t": "turn", "steps": [step_to_wire(x, flip=True) for x in steps],
                        "state": dump_state(b, flip=True)})
        self.out.append(("steps", steps))

    def poll(self):
        for m in self.link.poll():
            try:
                t = m.get("t")
                if t == "act":
                    self._choose(1, "act", m.get("a"))
                elif t == "send":
                    self._choose(1, "send", m.get("i"))
                elif t in ("bye", "_closed"):
                    self._left(t)
            except Exception:       # mensaje raro del otro lado: no tumbar la pantalla
                self._bad()
        out, self.out = self.out, []
        return out


class GuestSession(Session):
    """El invitado: juega sobre una copia espejo (él es el lado 0) y solo repite lo que decide el árbitro."""

    def act(self, action):
        self.link.send({"t": "act", "a": list(action)})

    def send(self, idx):
        self.link.send({"t": "send", "i": idx})

    def poll(self):
        for m in self.link.poll():
            try:
                t = m.get("t")
                if t == "turn":
                    sizes = [len(x) for x in self.b.teams]
                    steps = [step_from_wire(x, sizes) for x in m["steps"]]
                    load_state(self.b, m["state"])
                    self.done = self.b.over
                    self.out.append(("steps", steps))
                elif t in ("bye", "_closed"):
                    self._left(t)
            except Exception:
                self._bad()
        out, self.out = self.out, []
        return out
