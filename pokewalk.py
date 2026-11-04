#!/usr/bin/env python3
"""
pokewalk - Pokémon caminando en la terminal (estilo vscode-pokemon).

Uso:
    python pokewalk.py                       # arranca con los últimos que elegiste
    python pokewalk.py pikachu charmander    # o con estos (nombre en inglés o número)
    python pokewalk.py --random 3            # 3 al azar (gen 1-4)

Teclas mientras corre:
    a  agregar uno (escribís nombre o número + Enter)
    c  cambiar todos (escribís varios separados por espacio + Enter)
    x  sacar: escribís nombre, número o el principio del nombre (varios con
       espacio) + Enter. Enter solo saca el último.
    r  agregar uno al azar
    q  salir   (Esc cancela lo que estés escribiendo)

Requisitos: Python 3.8+ y Pillow  ->  pip install pillow
Los sprites son los de PMDCollab/SpriteCollab (Pokémon Mystery Dungeon) y se
cachean después de la primera descarga. Tip: para verlos chiquitos y nítidos,
achicá la fuente del panel en vez de usar --scale < 1.
"""
import argparse
import csv
import io
import json
import math
import os
import queue
import random
import re
import shutil
import signal
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image
except ImportError:
    sys.exit("Falta Pillow:  pip install pillow")

import pokebattle as pb
import pokenet as pn

SPRITE_URL = "https://raw.githubusercontent.com/PMDCollab/SpriteCollab/master/sprite/{id:04d}/{file}"
SPECIES_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species.csv"
DEFAULT = ["pikachu", "charmander", "squirtle"]

ROW_RIGHT, ROW_LEFT = 2, 6      # filas del sprite sheet de PMD
ALPHA_MIN = 128
SLEEP_AFTER = 5 * 60            # s sin comandos para que empiecen a dormirse (--sleep-after, en minutos)
WAKE_LINES = ["*bostezo*", "Mmm... ¿qué hora es?", "*se estira*", "¿Seguís ahí?", "Un ratito más..."]

if os.name == "nt":
    CACHE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pokewalk")
else:
    CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pokewalk")
STATE_FILE = os.path.join(CACHE, "ultimos.txt")
EVENTS_FILE = os.path.join(CACHE, "events.jsonl")   # lo escribe pokewalk-hook.ps1
ALIVE_FILE = os.path.join(CACHE, "alive")           # latido: el hook solo escribe si está fresco


# --------------------------------------------------------------------------- red / cache
def fetch(url, cache_name):
    path = os.path.join(CACHE, cache_name)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "pokewalk"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return data


_species = None
_species_lock = threading.Lock()


def resolve(name):
    """Nombre o número -> (dex, nombre). Lanza ValueError si no existe."""
    global _species
    name = name.strip().lower()
    with _species_lock:
        if _species is None:
            raw = fetch(SPECIES_URL, "species.csv").decode("utf-8")
            _species = {r["identifier"]: int(r["id"]) for r in csv.DictReader(io.StringIO(raw))}
    if name.isdigit():
        dex = int(name)
        back = {v: k for k, v in _species.items()}
        if dex not in back:
            raise ValueError(f"no existe el #{dex}")
        return dex, back[dex]
    key = name.replace(" ", "-").replace(".", "").replace("'", "")
    if key not in _species:
        raise ValueError(f"no conozco a '{name}'")
    return _species[key], key


def load_saved():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            names = f.read().split()
        return names or None
    except OSError:
        return None


def save(names):
    try:
        os.makedirs(CACHE, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(" ".join(names))
    except OSError:
        pass


# --------------------------------------------------------------------------- sprites
def load_anim(dex, anim_name, anims_xml):
    anims = {a.findtext("Name"): a for a in anims_xml.iter("Anim")}
    a = anims.get(anim_name)
    if a is None:
        return None
    if a.find("CopyOf") is not None:
        anim_name = a.findtext("CopyOf")
        a = anims.get(anim_name)
        if a is None:
            return None
    fw, fh = int(a.findtext("FrameWidth")), int(a.findtext("FrameHeight"))
    durs = [int(d.text) / 60 for d in a.iter("Duration")]   # PMD: 60 ticks/s
    sheet = Image.open(io.BytesIO(fetch(
        SPRITE_URL.format(id=dex, file=f"{anim_name}-Anim.png"),
        f"{dex:04d}/{anim_name}-Anim.png"))).convert("RGBA")

    rows = sheet.height // fh                       # algunas (Sleep, Faint...) no tienen 8 direcciones
    r_right = ROW_RIGHT if rows > ROW_RIGHT else 0
    r_left = ROW_LEFT if rows > ROW_LEFT else 0

    def row(r):
        return [sheet.crop((i * fw, r * fh, (i + 1) * fw, (r + 1) * fh)) for i in range(len(durs))]

    hit = a.findtext("HitFrame")                    # cuadro en que "pega": para sincronizar el daño
    hit_t = sum(durs[:int(hit)]) if hit and hit.strip().isdigit() else None
    return row(r_right), row(r_left), durs, hit_t


# Animaciones extra de PMD que se usan en batalla (la primera de cada grupo que el Pokémon tenga):
# a = ataque físico, s = ataque a distancia, c = prepararse (ataques de estado), h = recibir daño,
# f = debilitarse, z = dormido.
BATTLE_ANIMS = {"a": ("Attack", "QuickStrike", "Strike", "Swing"), "s": ("Shoot", "Shock", "Charge"),
                "c": ("Charge", "Rotate"), "h": ("Hurt", "Pain", "Cringe"), "f": ("Faint",),
                "z": ("Sleep", "EventSleep")}


class Sprite:
    def __init__(self, dex, scale, battle=False):
        xml = ET.fromstring(fetch(SPRITE_URL.format(id=dex, file="AnimData.xml"), f"{dex:04d}/AnimData.xml"))
        walk = load_anim(dex, "Walk", xml)
        if walk is None:
            raise ValueError("no tiene animación de caminata")
        idle = load_anim(dex, "Idle", xml) or ([walk[0][0]], [walk[1][0]], [1.0], None)

        extra = {}                                  # solo en batalla: bajar estas hojas para los demás sería gastar de más
        for code, names in (BATTLE_ANIMS.items() if battle else ()):
            for n in names:
                try:
                    got = load_anim(dex, n, xml)
                except Exception:                   # la hoja figura pero no se pudo bajar: probar la siguiente
                    got = None
                if got:
                    extra[code] = got
                    break

        every = walk[0] + walk[1] + idle[0] + idle[1] + [im for e in extra.values() for im in e[0] + e[1]]
        W = max(im.width for im in every)
        H = max(im.height for im in every)

        def norm(im):
            c = Image.new("RGBA", (W, H))
            c.paste(im, ((W - im.width) // 2, H - im.height))
            return c

        sets = {k: [norm(im) for im in v] for k, v in
                {"wr": walk[0], "wl": walk[1], "ir": idle[0], "il": idle[1]}.items()}
        for code, e in extra.items():
            sets[code + "r"] = [norm(im) for im in e[0]]
            sets[code + "l"] = [norm(im) for im in e[1]]

        box = None
        for frames in sets.values():
            for im in frames:
                b = im.getchannel("A").point(lambda a: 255 if a >= ALPHA_MIN else 0).getbbox()
                if b:
                    box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                                 max(box[2], b[2]), max(box[3], b[3]))
        box = box or (0, 0, W, H)

        bw, bh = box[2] - box[0], box[3] - box[1]
        self.w = max(1, round(bw * scale))
        self.h = max(1, round(bh * scale))
        self.scale = scale

        def prep(im):
            im = im.crop(box)
            if (self.w, self.h) != (bw, bh):
                im = im.resize((self.w, self.h), Image.NEAREST if scale > 1 else Image.BOX)
            return list(im.getdata()), im.width, im.height

        self.anims = {k: [prep(im) for im in v] for k, v in sets.items()}
        self.durs = {"w": walk[2], "i": idle[2]}
        self.durs.update({c: e[2] for c, e in extra.items()})
        self.hit = {c: e[3] for c, e in extra.items()}      # segundos hasta el cuadro de impacto (o None)


class Pet:
    lift = 0                                        # píxeles por encima del piso (los efectos flotan)

    def __init__(self, name, sprite, width, speed_mult=1.0):
        self.name = name
        self.s = sprite
        self.hold = False                           # congelar en el último cuadro (debilitado)
        self.x = random.uniform(0, max(1, width - sprite.w))
        self.dir = random.choice((1, -1))
        self.speed = random.uniform(7, 13) * min(1.5, max(0.4, sprite.scale)) * speed_mult
        self.say_text, self.say_until = "", 0.0
        self.boost_left = 0.0                       # corre más rápido (susto)
        self.sleeping = False
        self.sleep_left = 0.0
        self.awake_left = 0.0                       # rato despierto aunque haya poca actividad
        self.zzz_in = 0.0
        self.drowse_delay = random.uniform(0, 20)   # para que no se duerman todos a la vez
        self.set_state("w")

    def set_state(self, st):
        self.state, self.frame, self.hold = st, 0, False
        self.frame_left = self.s.durs[st][0]
        self.state_left = random.uniform(2, 7) if st == "w" else random.uniform(1, 3.5)

    def say(self, text, secs=4.0):
        self.say_text, self.say_until = text, time.time() + secs

    def wake_up(self):
        self.awake_left = 0.0
        if self.sleeping:
            self.sleeping = False
            self.set_state("w")

    def scare(self):
        self.wake_up()
        self.dir *= -1
        self.set_state("w")
        self.state_left, self.boost_left = 2.0, 1.5

    def cheer(self):
        self.wake_up()
        self.set_state("i")
        self.state_left = 2.5

    def _nap(self, dt, idle, sleep_after):
        """Sueño: tras `sleep_after` s sin actividad se duerme un rato, se despierta a
        estirarse un poco, y vuelve a dormirse mientras siga sin pasar nada."""
        if sleep_after <= 0 or idle < sleep_after + self.drowse_delay:
            self.wake_up()
        elif self.sleeping:
            self.sleep_left -= dt
            self.zzz_in -= dt
            if self.zzz_in <= 0:
                self.say("Zzz...", 2.5)
                self.zzz_in = random.uniform(6, 10)
            if self.sleep_left <= 0:
                self.sleeping = False
                self.awake_left = random.uniform(10, 25)
                self.set_state("w")
                self.say(random.choice(WAKE_LINES), 3)
        elif self.awake_left > 0:
            self.awake_left -= dt
        else:
            self.sleeping = True
            self.sleep_left = random.uniform(25, 70)
            self.zzz_in = 0.0
            self.set_state("i")

    def update(self, dt, width, idle=0.0, sleep_after=0.0):
        self._nap(dt, idle, sleep_after)
        if self.sleeping:
            return                                  # quieto, en el primer cuadro de idle
        self.boost_left = max(0.0, self.boost_left - dt)
        self.state_left -= dt
        if self.state_left <= 0:
            if self.state == "w" and random.random() < 0.5:
                self.set_state("i")
            else:
                if self.state == "i" and random.random() < 0.4:
                    self.dir *= -1
                self.set_state("w")
        self.tick_frame(dt)
        if self.state == "w":
            self.x += self.dir * self.speed * (2.5 if self.boost_left else 1.0) * dt
            maxx = max(0, width - self.s.w)
            if self.x <= 0:
                self.x, self.dir = 0, 1
            elif self.x >= maxx:
                self.x, self.dir = maxx, -1

    def tick_frame(self, dt):
        """Avanza la animación del estado actual (sin moverlo ni cambiarlo)."""
        durs = self.s.durs[self.state]
        if self.hold and self.frame >= len(durs) - 1:
            return
        self.frame_left -= dt
        while self.frame_left <= 0:
            if self.hold and self.frame >= len(durs) - 1:
                break
            self.frame = (self.frame + 1) % len(durs)
            self.frame_left += durs[self.frame]

    def pixels(self):
        return self.s.anims[self.state + ("r" if self.dir > 0 else "l")][self.frame]


# --------------------------------------------------------------------------- carga en segundo plano
class Loader:
    """Resuelve nombres y baja sprites sin congelar la animación."""

    def __init__(self, scale):
        self.scale = scale
        self.cache = {}
        self.done = queue.Queue()
        self.busy = 0

    def request(self, names, replace):
        self.busy += 1
        threading.Thread(target=self._work, args=(names, replace), daemon=True).start()

    def _work(self, names, replace):
        ok, errors = [], []
        for n in names:
            try:
                dex, key = resolve(n)
                if dex not in self.cache:
                    self.cache[dex] = Sprite(dex, self.scale)
                ok.append((key, self.cache[dex]))
            except Exception as e:  # red, nombre inválido, sprite faltante...
                errors.append(f"{n}: {e}")
        self.done.put((replace, ok, errors))


# --------------------------------------------------------------------------- teclado sin bloquear
# Teclas de edición del prompt. Son strings no imprimibles, así que nunca se confunden con texto escrito.
K_LEFT, K_RIGHT, K_HOME, K_END, K_DEL = "\x00L", "\x00R", "\x00H", "\x00E", "\x00D"
WIN_EDIT_KEYS = {"K": K_LEFT, "M": K_RIGHT, "G": K_HOME, "O": K_END, "S": K_DEL}
ANSI_EDIT_KEYS = {"\x1b[D": K_LEFT, "\x1bOD": K_LEFT, "\x1b[C": K_RIGHT, "\x1bOC": K_RIGHT,
                  "\x1b[H": K_HOME, "\x1bOH": K_HOME, "\x1b[1~": K_HOME, "\x1b[F": K_END, "\x1bOF": K_END,
                  "\x1b[4~": K_END, "\x1b[3~": K_DEL}


class Keys:
    def __init__(self):
        self.win = os.name == "nt"
        if self.win:
            import msvcrt
            self.m = msvcrt
        else:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)

    def read(self):
        out = []
        if self.win:
            while self.m.kbhit():
                ch = self.m.getwch()
                if ch in ("\x00", "\xe0"):      # teclas especiales: las de edición se pasan, el resto se descarta
                    tok = WIN_EDIT_KEYS.get(self.m.getwch())
                    if tok:
                        out.append(tok)
                    continue
                out.append(ch)
        else:
            import select
            while select.select([sys.stdin], [], [], 0)[0]:
                data = os.read(self.fd, 64).decode("utf-8", "ignore")
                if not data:
                    break
                for piece in re.split(r"(\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z])", data):
                    if piece.startswith(("\x1b[", "\x1bO")) and len(piece) > 2:
                        tok = ANSI_EDIT_KEYS.get(piece)
                        if tok:
                            out.append(tok)
                    else:
                        out.extend(piece)
        return out

    def restore(self):
        if not self.win:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


# --------------------------------------------------------------------------- comandos de la otra terminal
class Events:
    """Lee los comandos que avisa pokewalk-hook.ps1 (una línea JSON por comando).
    Arranca desde el final del archivo: solo reacciona a lo que pasa mientras está abierto."""

    def __init__(self, path):
        self.path = path
        self.partial = b""
        try:
            if os.path.getsize(path) > 200_000:     # el hook solo agrega: acá lo mantenemos chico
                open(path, "w").close()
        except OSError:
            pass
        self.pos = self._size()

    def _size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def poll(self):
        size = self._size()
        if size < self.pos:                          # lo truncaron
            self.pos, self.partial = 0, b""
        if size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return []
        self.pos += len(data)
        *lines, self.partial = (self.partial + data).split(b"\n")
        evs = []
        for line in lines:
            try:
                ev = json.loads(line.decode("utf-8-sig"))
            except ValueError:
                continue
            if isinstance(ev, dict):
                evs.append(ev)
        return evs[-5:]


def touch(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


FAIL_LINES = ["¡Uy! Eso falló...", "Hmm, eso no salió bien.", "¿Qué pasó ahí?", "¡Ay! Error."]
OK_LINES = ["¡Bien!", "¡Eso!", "¡Genial!", "Todo en orden."]
QUIET = {"cd", "ls", "dir", "cls", "clear", "pwd", "echo", "exit", "z", "history", "gci", "sl"}
TEST_RUNNERS = {"pytest", "jest", "vitest", "mocha"}
TEST_TOOLS = {"npm", "yarn", "pnpm", "dotnet", "cargo", "go", "mvn", "gradle"}


def fmt_secs(s):
    s = int(round(s))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


class Reactor:
    """Reglas simples (sin IA): de un comando terminado a (texto, estado de ánimo) o None."""

    def __init__(self):
        self.fails = 0

    def react(self, ev):
        ok = bool(ev.get("ok", True))
        try:
            dur = float(ev.get("dur") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        words = str(ev.get("cmd") or "").lower().split()
        first = words[0] if words else ""
        sub = words[1] if len(words) > 1 else ""
        tests = first in TEST_RUNNERS or (first in TEST_TOOLS and sub in ("test", "t"))
        was, self.fails = self.fails, (0 if ok else self.fails + 1)

        if not ok:
            if self.fails >= 5:
                return f"Respirá hondo... ya van {self.fails} errores", "calm"
            if self.fails == 3:
                return "Van 3 errores seguidos...", "scare"
            if tests:
                return "Los tests fallaron...", "scare"
            if dur >= 10:
                return f"Tardó {fmt_secs(dur)} y falló...", "scare"
            return random.choice(FAIL_LINES), "scare"
        if was >= 3:
            return "¡Por fin anduvo!", "happy"
        if first == "git" and sub == "commit":
            return "¡Buen commit!", "happy"
        if first == "git" and sub == "push":
            return "¡Subido!", "happy"
        if tests:
            return "¡Tests en verde!", "happy"
        if first in ("rm", "del", "rd", "rmdir", "remove-item", "ri"):
            return "¡Cuidado con lo que borrás!", "scare"
        if dur >= 30:
            return f"¡Terminó! ({fmt_secs(dur)})", "happy"
        if first in QUIET:
            return None
        return (random.choice(OK_LINES), "happy") if random.random() < 0.15 else None


# --------------------------------------------------------------------------- render
def top_row_of(pets, rows, sixel, reserve=1):
    """Primera fila de pantalla que ocupan los sprites (misma cuenta que render*).
    `reserve` = filas de texto arriba (1 = línea de estado; 2 en batalla)."""
    ph = max(p.s.h for p in pets)
    if sixel:
        return max(reserve + 1, rows - -(-ph // 20))
    ph += ph % 2
    lines = ph // 2
    return rows - (lines - max(0, lines - max(1, rows - reserve))) + 1


def bubbles(pets, cols, rows, sixel, px_per_col):
    """Globos de texto sobre la cabeza de los Pokémon que están hablando."""
    now = time.time()
    talking = [p for p in pets if p.say_until > now]
    row = top_row_of(pets, rows, sixel) - 1 if pets else 0
    if not talking or row < 2:
        return ""
    out = [f"\x1b[{row};1H\x1b[2K"]
    for p in talking:
        text = f" {p.say_text} "[:max(1, cols - 1)]
        center = (int(p.x) + p.s.w // 2) // px_per_col
        col = min(max(0, center - len(text) // 2), max(0, cols - len(text) - 1))
        out.append(f"\x1b[{row};{col + 1}H\x1b[30;47m{text}\x1b[0m")
    return "".join(out)


def render(pets, cols, rows, reserve=1):
    if not pets:
        return ""
    ph = max(p.s.h for p in pets)
    ph += ph % 2
    canvas = [[None] * cols for _ in range(ph)]
    for p in sorted(pets, key=lambda p: p.s.h, reverse=True):
        data, w, h = p.pixels()
        ox, oy = int(p.x), max(0, ph - h - p.lift)
        for y in range(h):
            row = canvas[oy + y]
            base = y * w
            for x in range(w):
                cx = ox + x
                if 0 <= cx < cols:
                    r, g, b, a = data[base + x]
                    if a >= ALPHA_MIN:
                        row[cx] = (r, g, b)

    lines = ph // 2
    avail = max(1, rows - reserve)           # arriba van las filas de texto (estado)
    skip = max(0, lines - avail)             # si no entra, se corta la parte de arriba
    top_row = rows - (lines - skip) + 1
    out = []
    for i in range(skip, lines):
        up, dn = canvas[2 * i], canvas[2 * i + 1]
        out.append(f"\x1b[{top_row + i - skip};1H")
        last = None
        for x in range(cols):
            t, b = up[x], dn[x]
            if t is None and b is None:
                st, ch = "\x1b[0m", " "
            elif b is None:
                st, ch = "\x1b[0m\x1b[38;2;%d;%d;%dm" % t, "▀"
            elif t is None:
                st, ch = "\x1b[0m\x1b[38;2;%d;%d;%dm" % b, "▄"
            else:
                st, ch = "\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm" % (t + b), "▀"
            if st != last:
                out.append(st)
                last = st
            out.append(ch)
        out.append("\x1b[0m")
    return "".join(out)


def render_sixel(pets, cols, rows, bg, cell_w=10, cell_h=20, reserve=1):
    """Dibuja los Pokémon como imagen Sixel (píxeles reales, texto normal).
    Windows Terminal mapea 10x20 píxeles Sixel a cada celda."""
    if not pets:
        return ""
    W = cols * cell_w
    ph = max(p.s.h for p in pets)
    cells = -(-ph // cell_h)
    top_row = max(reserve + 1, rows - cells)    # deja la última fila libre: así no scrollea
    cells = min(cells, max(1, rows - top_row))
    H = cells * cell_h

    # 1) componer: {(x, y): (r, g, b)}
    px = {}
    for p in sorted(pets, key=lambda p: p.s.h, reverse=True):
        data, w, h = p.pixels()
        ox, oy = int(p.x), max(0, H - h - p.lift)
        for y in range(h):
            yy = oy + y
            if yy < 0:
                continue
            base = y * w
            for x in range(w):
                cx = ox + x
                if 0 <= cx < W:
                    r, g, b, a = data[base + x]
                    if a >= ALPHA_MIN:
                        px[(cx, yy)] = (r, g, b)

    # 2) paleta (índice 0 = fondo)
    colors = set(px.values())
    if len(colors) > 250:   # por las dudas: reducir colores
        px = {k: (r & 0xF0, g & 0xF0, b & 0xF0) for k, (r, g, b) in px.items()}
        colors = set(px.values())
    pal = {c: i + 1 for i, c in enumerate(colors)}

    def reg(i, c):
        return "#%d;2;%d;%d;%d" % (i, c[0] * 100 // 255, c[1] * 100 // 255, c[2] * 100 // 255)

    head = []
    if bg:
        head.append(reg(0, bg))
    head += [reg(i, c) for c, i in pal.items()]

    # 3) bandas de 6 filas: {banda: {color: {x: bits}}}
    bands = {}
    for (x, y), c in px.items():
        b, bit = divmod(y, 6)
        cols_map = bands.setdefault(b, {}).setdefault(pal[c], {})
        cols_map[x] = cols_map.get(x, 0) | (1 << bit)

    def rle(ch, n):
        return ("!%d%s" % (n, ch)) if n > 3 else ch * n

    body = []
    for b in range(-(-H // 6)):
        parts = []
        if bg:
            rows_here = min(6, H - 6 * b)          # no pintar fondo más allá de H
            parts.append("#0" + rle(chr(63 + (1 << rows_here) - 1), W))
        for idx, xs in bands.get(b, {}).items():
            out = ["#%d" % idx]
            pos = 0
            run_ch, run_n = None, 0
            for x in sorted(xs):
                if x > pos:
                    if run_n:
                        out.append(rle(run_ch, run_n))
                        run_n = 0
                    out.append(rle("?", x - pos))
                ch = chr(63 + xs[x])
                if ch == run_ch and run_n:
                    run_n += 1
                else:
                    if run_n:
                        out.append(rle(run_ch, run_n))
                    run_ch, run_n = ch, 1
                pos = x + 1
            if run_n:
                out.append(rle(run_ch, run_n))
            parts.append("".join(out))
        body.append("$".join(parts))

    erase = "" if bg else "".join(f"\x1b[{top_row + i};1H\x1b[2K" for i in range(cells))
    return (erase + f"\x1b[{top_row};1H\x1bP0;1;0q\"1;1;{W};{H}" +
            "".join(head) + "-".join(body) + "\x1b\\")


def find_pet(pets, term):
    """Índice del Pokémon a sacar: por nombre, número o prefijo. None = el último."""
    if not pets:
        return None
    if term is None:
        return len(pets) - 1
    term = term.lower()
    key = term
    if term.isdigit():
        try:
            key = resolve(term)[1]
        except Exception:
            return None
    for match in (lambda n: n == key, lambda n: n.startswith(term)):
        idx = [i for i, p in enumerate(pets) if match(p.name)]
        if idx:
            return idx[-1]
    return None


PROMPTS = {
    "add": "Agregar (nombre, número o rango 1-10, Enter;  Tab o Enter solo = lista por región)",
    "set": "Cambiar todos (separados por espacio, rangos 1-10, Enter;  Tab = lista por región)",
    "del": "Sacar (nombre, número o principio del nombre; Enter solo = el último)",
    "battle": "Batalla (pika char = 1 vs 1;  pika char vs mew snor = equipos;  Enter solo = 3 vs 3 al azar;  Tab = lista)",
    "pet": "Mimar a (nombre, número o principio; Enter solo = uno al azar)",
    "info": "Info de (nombre, número o principio; Enter solo = uno que camina;  Tab = lista por región)",
    "vsip": "IP del amigo (ej. 192.168.1.20; Esc vuelve)",
    "vsteam": "Conectado con {peer}. Tu equipo (hasta 6; pika:60 = nivel 60; Enter solo = los que caminan;  Tab = lista)",
    "vsteam_again": "Revancha con {peer}. Enter solo = el mismo equipo y ataques de recién; o armá otro (pika:60 char;  Tab = lista)",
}
DEX_MODES = ("add", "set", "battle", "info", "vsteam")   # prompts donde Tab abre la lista por región


def prompt_line(label, buf, cur, cols):
    """Línea de entrada con cursor (video inverso). Lo escrito siempre se ve: si no entra, primero se
    acorta la etiqueta y, si hace falta, se desplaza una ventana sobre el texto."""
    width = max(8, cols - 1)
    room = max(10, width - len(label) - 2)
    if len(buf) + 1 > room:
        keep = width - (len(buf) + 1) - 3
        label = label[:keep] + "…" if keep >= 6 else label[:6] + "…"
        room = max(10, width - len(label) - 2)
    start = max(0, cur - room + 1) if len(buf) + 1 > room else 0
    seen = buf[start:start + room]
    k = cur - start
    view = seen[:k] + "\x1b[7m" + (seen[k:k + 1] or " ") + "\x1b[27m" + seen[k + 1:]
    return f"\x1b[1;1H\x1b[2K\x1b[97m{label}: {view}\x1b[0m"


def status_line(text, cols, bright=False):
    text = text[:max(0, cols - 1)]
    color = "\x1b[97m" if bright else "\x1b[90m"
    return f"\x1b[1;1H\x1b[2K{color}{text}\x1b[0m"


# --------------------------------------------------------------------------- menús de varias líneas
# Con espacio arriba de los Pokémon los menús se dibujan en cuadrícula y con colores, como en los juegos;
# si el panel es bajito (no hay filas libres) cada menú cae a su línea compacta de siempre.
KEY, DIM, HI, GOOD, BAD, WARN = "\x1b[1;36m", "\x1b[90m", "\x1b[97m", "\x1b[32m", "\x1b[31m", "\x1b[33m"


def rgb(c):
    return "\x1b[38;2;%d;%d;%dm" % c


def plen(parts):
    return sum(len(t) for t, _ in parts)


def seg(parts, width):
    """Segmentos [(texto, estilo ANSI)] -> texto con colores, cortado y rellenado a `width` columnas."""
    out, used = [], 0
    for text, style in parts:
        text = text[:max(0, width - used)]
        if not text:
            break
        out.append(f"{style}{text}\x1b[0m" if style else text)
        used += len(text)
    return "".join(out) + " " * max(0, width - used)


def opt(key, label, extra=()):
    """Una opción de menú: la tecla resaltada y su texto (más segmentos opcionales)."""
    return [(key, KEY), (" " + label, HI)] + list(extra)


def flat(items, sep="   "):
    """Varias opciones en una sola fila."""
    out = []
    for i, it in enumerate(items):
        out += ([(sep, "")] if i else []) + it
    return out


def grid(title, items, foot, cols, rows_max, ncols=None):
    """Menú en cuadrícula: título, opciones en columnas (de izquierda a derecha y de arriba abajo) y pie.
    Si no entra en `rows_max` filas se saca primero el pie y después el título; si ni así entra devuelve None
    y quien llama usa su línea compacta."""
    width = max(10, cols - 1)
    w = max((plen(it) for it in items), default=0) + 2
    n = max(1, min(ncols or len(items) or 1, width // max(1, w)))
    body = -(-len(items) // n) if items else 0
    lines = (1 if title else 0) + body + (1 if foot else 0)
    if lines > rows_max and foot:
        foot, lines = None, lines - 1
    if lines > rows_max and title:
        title, lines = None, lines - 1
    if lines > rows_max:
        return None
    cw = min(width // n, w + 6)             # columnas parejas pero sin separarlas de más en pantallas anchas
    out = [seg(title, width)] if title else []
    for r in range(body):
        out.append("".join(seg(items[i], cw) for i in range(r * n, min(len(items), (r + 1) * n))))
    if foot:
        out.append(seg(foot, width))
    return out


def put_rows(lines, start=1, clear_to=0):
    """Escribe `lines` desde la fila `start` y deja limpias las filas hasta `clear_to` (restos de un menú más alto)."""
    last = max(start + len(lines) - 1, clear_to)
    return "".join(f"\x1b[{r};1H\x1b[2K" + (lines[r - start] if r - start < len(lines) else "")
                   for r in range(start, last + 1))


def text_rows_free(pets, rows, sixel, reserve=1):
    """Filas libres arriba de los Pokémon (las de texto), como mínimo `reserve`."""
    if not pets:
        return max(reserve, rows - 1)
    return max(reserve, top_row_of(pets, rows, sixel, reserve) - 1)


# --------------------------------------------------------------------------- batalla
PET_LINES = ["<3", "¡Hola!", "¿Jugamos?", "¡Me encanta caminar!", "¡Qué lindo día!", "¿Me das una baya?", "¡Presente!"]


MAX_PER_ADD = 10    # Pokémon por vez al agregar/cambiar (así un 1-151 no llena la memoria)


def expand_ranges(parts, limit=MAX_PER_ADD):
    """`1-10 pikachu` -> ['1', ..., '10', 'pikachu'] (números de Pokédex; `ho-oh` sigue siendo un nombre).
    Devuelve (términos, cuántos se dejaron afuera por pasar `limit`)."""
    out = []
    for term in parts:
        a, sep, b = term.partition("-")
        if sep and a.isdigit() and b.isdigit():
            lo, hi = sorted((int(a), int(b)))
            out += [str(n) for n in range(lo, hi + 1)]
        else:
            out.append(term)
    return out[:limit], max(0, len(out) - limit)


def expand_name(term, walking):
    """Nombre completo para lo que escribió el usuario: un nombre o número válido se respeta tal cual
    (así `mew` no se confunde con `mewtwo`); si no, vale el principio del nombre de uno que camina (`pika`)."""
    t = term.strip().lower()
    if t in walking:
        return t
    try:
        resolve(t)
        return t
    except Exception:
        pass
    return next((n for n in reversed(walking) if n.startswith(t)), t)


def battle_sprite(loader, dex):
    """Sprite con las animaciones de combate (se baja una sola vez)."""
    if (dex, "batalla") not in loader.cache:
        loader.cache[(dex, "batalla")] = Sprite(dex, loader.scale, battle=True)
    return loader.cache[(dex, "batalla")]


def build_fighter(spec, loader, walking=()):
    """(nombre, nivel) -> (Fighter, sprite). Bloquea: busca stats y ataques en PokeAPI y baja el sprite."""
    name, level = spec
    dex, key = resolve(expand_name(name, walking))
    return pb.load_fighter(fetch, dex, key, level), battle_sprite(loader, dex)


def build_battle(sides, loader, walking=()):
    """sides = [[(nombre, nivel), ...] (vos), [...] (rival)] -> (Battle, sprites). Bloquea mientras busca
    en PokeAPI stats y ataques de todos (más la tabla de tipos) y baja los sprites: va siempre en un hilo."""
    with ThreadPoolExecutor(4) as ex:
        built = list(ex.map(lambda spec: build_fighter(spec, loader, walking), [spec for side in sides for spec in side]))
    teams, sprites, k = [], [], 0
    for side in sides:
        chunk = built[k:k + len(side)]
        k += len(side)
        teams.append([f for f, _ in chunk])
        sprites.append([s for _, s in chunk])
    chart = pb.TypeChart(fetch)
    chart.preload({m.type for team in teams for f in team for m in f.moves})
    return pb.Battle(teams[0], teams[1], chart), sprites


def prepare_battle(sides, loader, jobs, walking=()):
    """Hilo: batalla común, todos a nivel 50. Avisa a la pantalla cuando está lista."""
    try:
        battle, sprites = build_battle([[(n, pb.LEVEL) for n in side] for side in sides], loader, walking)
        jobs.put(("battle", battle, sprites, None))
    except Exception as e:     # sin internet, nombre inválido, sprite faltante...
        jobs.put(("berror", f"No pude preparar la batalla: {e}", 6))


def prepare_gym_battle(trainer, mine, loader, jobs):
    """Hilo: tu equipo contra el de un líder / Alto Mando. Los del rival van con sus niveles originales; cada
    uno de los tuyos sale con el nivel medio del rival más un azar entre -20 y +20 (entre 1 y 100)."""
    try:
        foes = trainer["equipo"]
        avg = round(sum(p["nivel"] for p in foes) / len(foes))
        mine_levels = [max(1, min(100, avg + random.randint(-20, 20))) for _ in mine]
        battle, sprites = build_battle(
            [list(zip(mine, mine_levels)), [(str(p["dex"]), p["nivel"]) for p in foes]], loader, mine)
        intro = [f"¡{trainer['nombre']} quiere combatir!   (nivel medio del rival: {avg})",
                 "Tu equipo: " + " · ".join(f"{f.name} Nv{f.level}" for f in battle.teams[0])]
        jobs.put(("battle", battle, sprites, intro))
    except Exception as e:
        jobs.put(("gerror", f"No pude preparar el combate: {e}", 6))


def load_my_team(parts, loader, jobs, walking):
    """Hilo (versus): arma tu equipo. Cada término es `nombre` o `nombre:nivel` (nivel 1-100, por defecto 50);
    sin nada, los que caminan (o 3 al azar)."""
    try:
        specs = []
        for term in (parts or list(walking)[:pn.MAX_TEAM]):
            name, _, lvl = term.partition(":")
            try:
                level = max(1, min(100, int(lvl))) if lvl else pb.LEVEL
            except ValueError:
                raise ValueError(f"nivel inválido: {term}") from None
            specs.append((name, level))
        if len(specs) > pn.MAX_TEAM:
            raise ValueError(f"hasta {pn.MAX_TEAM} Pokémon por equipo")
        specs = specs or [(str(random.randint(1, 493)), pb.LEVEL) for _ in range(3)]
        def build(spec):
            dex, key = resolve(expand_name(spec[0], walking))
            f, pool = pb.load_fighter(fetch, dex, key, spec[1], with_pool=True)
            return f, pool, battle_sprite(loader, dex)

        with ThreadPoolExecutor(3) as ex:
            built = list(ex.map(build, specs))
        jobs.put(("vspool", [b[0] for b in built], [b[1] for b in built], [b[2] for b in built]))
    except Exception as e:
        jobs.put(("vserror", f"No pude armar el equipo: {e}", 6))


class MovePicker:
    """Menú (en la línea de arriba) para elegir hasta 4 ataques de cada Pokémon de tu equipo, entre todos los que
    puede usar a su nivel. Arranca con el set sorteado marcado: Enter lo deja así."""
    HEAD = 56       # columnas que se reservan para el encabezado y las teclas

    def __init__(self, fighters, pools):
        self.fighters, self.pools = fighters, pools
        self.i, self.page = 0, 0
        self.avail = 1          # filas de texto disponibles (lo fija `lines` en cada cuadro)
        self.sel = []
        for f, pool in zip(fighters, pools):
            names = {m.name for m in f.moves}
            self.sel.append([k for k, m in enumerate(pool) if m.name in names][:4])

    def _cell(self, n, k, m):
        """Un ataque en la cuadrícula: número, * si está elegido, nombre (color del tipo), tipo y poder."""
        on = k in self.sel[self.i]
        return [(str(n), KEY), ("*" if on else " ", GOOD),
                (m.name, rgb(TYPE_COLORS.get(m.type, (255, 255, 255))) + ("\x1b[1m" if on else "")),
                (f"  {pb.TYPES_ES[m.type][:3]} {m.power or 'estado'}", DIM)]

    def _per(self, cols):
        """Ataques por página en cuadrícula (máx. 9, uno por dígito), o None si no hay filas y va la línea compacta."""
        if self.avail < 3:                                   # título + pie + al menos una fila de ataques
            return None
        w = max(plen(self._cell(9, k, m)) for k, m in enumerate(self.pools[self.i])) + 2
        return max(1, min(9, max(1, min(3, max(10, cols - 1) // w)) * (self.avail - 2)))

    def lines(self, cols, avail):
        """El menú en cuadrícula, o None si no entra (se usa `text`)."""
        self.avail = avail
        per = self._per(cols)
        if not per:
            return None
        pages = self._pages(cols)
        self.page = min(self.page, len(pages) - 1)
        f, sel, pool = self.fighters[self.i], self.sel[self.i], self.pools[self.i]
        items = [self._cell(n + 1, k, pool[k]) for n, k in enumerate(pages[self.page])]
        last = self.i == len(self.fighters) - 1
        title = [(f"{f.name} Nv{f.level}", HI), (f"  ({self.i + 1}/{len(self.fighters)})   elegidos {len(sel)}/4: ", DIM),
                 (", ".join(pool[k].name for k in sel) or "ninguno (marcá al menos uno)", GOOD if sel else WARN)]
        foot = [("1-9", KEY), (" marcar   ", DIM), ("←→", KEY), (f" página {self.page + 1}/{len(pages)}   ", DIM),
                ("Enter", KEY), (" listo   " if last else " siguiente Pokémon   ", DIM), ("r", KEY), (" al azar", DIM)]
        return grid(title, items, foot, cols, avail, 3)

    def _pages(self, cols):
        """Reparte los ataques en páginas: de tamaño fijo en cuadrícula, o las que entren en el ancho en la línea
        compacta (máx. 9, uno por dígito)."""
        per = self._per(cols)
        if per:
            ks = list(range(len(self.pools[self.i])))
            return [ks[j:j + per] for j in range(0, len(ks), per)] or [[]]
        pool, pages, cur, used = self.pools[self.i], [], [], 0
        room = max(30, cols - self.HEAD)
        for k, m in enumerate(pool):
            w = len(self._label(0, k, m)) + 2
            if cur and (used + w > room or len(cur) == 9):
                pages.append(cur)
                cur, used = [], 0
            cur.append(k)
            used += w
        return pages + [cur]

    def _label(self, n, k, m):
        mark = "*" if k in self.sel[self.i] else ""
        kind = "estado" if not m.power else str(m.power)
        return f"{n}{mark} {m.name} ({pb.TYPES_ES[m.type][:3]} {kind})"

    def text(self, cols):
        f, pages = self.fighters[self.i], self._pages(cols)
        self.page = min(self.page, len(pages) - 1)
        items = "  ".join(self._label(n + 1, k, self.pools[self.i][k]) for n, k in enumerate(pages[self.page]))
        last = self.i == len(self.fighters) - 1
        return (f"{f.name} Nv{f.level} ({self.i + 1}/{len(self.fighters)}) {len(self.sel[self.i])}/4 · {items} · "
                f"pág {self.page + 1}/{len(pages)} ←→  Enter={'listo' if last else 'sigue'}  r=azar")

    def key(self, ch, cols):
        """Devuelve "done" al confirmar el último Pokémon."""
        pages = self._pages(cols)
        sel = self.sel[self.i]
        if ch == K_RIGHT:
            self.page = (self.page + 1) % len(pages)
        elif ch == K_LEFT:
            self.page = (self.page - 1) % len(pages)
        elif ch.isdigit() and ch != "0" and int(ch) <= len(pages[min(self.page, len(pages) - 1)]):
            k = pages[min(self.page, len(pages) - 1)][int(ch) - 1]
            if k in sel:
                sel.remove(k)
            elif len(sel) < 4:
                sel.append(k)
        elif ch in ("r", "R"):
            self.sel[self.i] = sorted(random.sample(range(len(self.pools[self.i])), min(4, len(self.pools[self.i]))))
        elif ch in ("\r", "\n") and sel:
            if self.i == len(self.fighters) - 1:
                for f, pool, s in zip(self.fighters, self.pools, self.sel):
                    pb.set_moves(f, [pool[k] for k in s])
                return "done"
            self.i, self.page = self.i + 1, 0
        return None


def prepare_versus(vs, loader, jobs):
    """Hilo (versus): con los dos equipos ya en la mano, baja los sprites del rival y arma la batalla."""
    try:
        foe_sprites = [battle_sprite(loader, f.dex) for f in vs.foe]
        chart = pb.TypeChart(fetch)
        chart.preload({m.type for team in (vs.my, vs.foe) for f in team for m in f.moves})
        battle = pb.Battle(vs.my, vs.foe, chart)            # el invitado juega sobre una copia espejo
        battle.human = [True, True]
        link = vs.take_link()
        if link is None:                                    # cancelaron mientras tanto
            return
        session = (pn.HostSession if vs.role == "host" else pn.GuestSession)(battle, link, vs.peer)
        intro = [f"¡Versus! {vs.name} contra {vs.peer}", f"{len(vs.my)} contra {len(vs.foe)}"]
        jobs.put(("vsbattle", battle, [vs.my_sprites, foe_sprites], intro, session))
    except Exception as e:
        jobs.put(("vsfatal", f"No pude preparar la batalla: {e}", 6))


REGIONS = ["Kanto", "Johto", "Hoenn", "Sinnoh", "Teselia", "Kalos", "Alola", "Galar", "Paldea"]   # por generación
DEX_PAGE = 9    # Pokémon por página en la lista por región


def species_by_gen():
    """Generación (1..9) -> [(número de Pokédex, nombre)] de esa generación en orden (de pokemon_species.csv)."""
    raw = fetch(SPECIES_URL, "species.csv").decode("utf-8")
    out = {}
    for r in sorted(csv.DictReader(io.StringIO(raw)), key=lambda r: int(r["id"])):
        out.setdefault(int(r["generation_id"]), []).append((int(r["id"]), r["identifier"]))
    return out


def load_gyms():
    """gimnasios.json (junto al programa) -> lista de regiones, o [] si falta."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gimnasios.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)["regiones"]
    except (OSError, ValueError, KeyError):
        return []


def lookup_info(term, jobs, walking=()):
    """Hilo: tipo y stats de un Pokémon (tecla i)."""
    try:
        dex, key = resolve(expand_name(term, walking))
        jobs.put(("flash", "[Esc] " + pb.describe(fetch, dex, key), 12))
    except Exception as e:
        jobs.put(("ierror", f"No pude: {term}: {e}", 6))


TYPE_COLORS = {
    "normal": (200, 200, 190), "fire": (255, 140, 40), "water": (70, 140, 255), "electric": (255, 225, 60),
    "grass": (90, 200, 80), "ice": (140, 230, 245), "fighting": (210, 70, 50), "poison": (170, 80, 200),
    "ground": (200, 160, 90), "flying": (160, 170, 250), "psychic": (255, 100, 160), "bug": (170, 200, 50),
    "rock": (190, 170, 110), "ghost": (110, 80, 170), "dragon": (110, 60, 240), "dark": (90, 80, 90),
    "steel": (180, 190, 210), "fairy": (250, 160, 220),
}


class Fx:
    """Proyectil ("orb") o destello ("burst") del color del tipo del ataque. Se dibuja como un
    Pokémon más: tiene x, lift (altura sobre el piso), s.w/s.h y pixels() (ver render*)."""
    lift = 0

    def __init__(self, kind, color, size):
        self.x, self.size = 0.0, size
        self.s = type("FxSprite", (), {"w": size, "h": size})()
        c, r = size / 2 - 0.5, size / 2
        thick = max(1.0, size / 10)
        data = []
        for y in range(size):
            for x in range(size):
                dx, dy = x - c, y - c
                d = (dx * dx + dy * dy) ** 0.5
                if kind == "orb":
                    on, core = d <= r, d <= r * 0.5
                else:                                   # estrella de 8 puntas
                    ax, ay = abs(dx), abs(dy)
                    on = (d <= r and (ax <= thick or ay <= thick or abs(ax - ay) <= thick)) or d <= r * 0.4
                    core = d <= r * 0.3
                data.append((255, 255, 255, 255) if core and on else (*color, 255) if on else (0, 0, 0, 0))
        self._px = (data, size, size)

    def pixels(self):
        return self._px


class BattleScene:
    """Batalla por equipos: dos Pokémon enfrentados, barras de HP, y el menú de los juegos
    (Luchar / Pokémon / Huir, sin Mochila). `a` alterna piloto automático.
    Cada ataque usa las animaciones de PMD del Pokémon (Attack / Shoot / Charge, Hurt, Faint, Sleep) y,
    si tiene sentido, un proyectil y un destello del color del tipo."""

    def __init__(self, battle, sprites, ppc, intro=None, net=None):
        self.b, self.sprites, self.ppc = battle, sprites, ppc     # sprites[lado][índice en el equipo]
        self.net = net          # pokenet.Session si el rival es un amigo en red (si no, lo maneja la computadora)
        self.queued = []        # turnos que llegaron por la red y esperan su turno para animarse
        self.active = [0, 0]
        self.pets = [self._mkpet(0, 0), self._mkpet(1, 0)]
        self.hidden = [False, False]
        self.shown = [float(battle.fighter(s).hp) for s in (0, 1)]    # HP que se dibuja (baja suave)
        self.target = list(self.shown)
        self.shake = [0.0, 0.0]
        self.auto, self.wait, self.over_t, self.forced = False, 0.0, 0.0, False
        self.over_who, self.rematch_ok = "", False     # pantalla final: quién ganó, y si se puede pedir la revancha
        self.asked = self.peer_asked = self.peer_gone = False
        self.note, self.note_t = "", 0.0
        self.anim_left = [0.0, 0.0]                    # lo que le queda a la animación de acción de cada lado
        self.down = [False, False]                     # debilitado y tirado en el piso
        self.fx, self.hit_t, self.dur_eff, self.lunge = [], 0.0, 0.0, False
        self._apply(battle.snap())
        a, b = battle.teams
        intro = intro or [f"¡{a[0].name} vs {b[0].name}!" if len(a) == len(b) == 1 else
                          f"¡Batalla de equipos! {len(a)} contra {len(b)}"]
        self._start([pb.Step(text, dur=2.2 if len(intro) > 1 else 1.8) for text in intro])

    def _mkpet(self, s, i):
        p = Pet(self.b.teams[s][i].name, self.sprites[s][i], 1)
        p.dir = 1 if s == 0 else -1          # se miran entre sí
        p.set_state("i")
        return p

    def _apply(self, snap):
        """Lleva lo dibujado al estado `snap` (cambio de Pokémon, HP, estados)."""
        for s, (a, st, conf, hps) in enumerate(snap):
            if a != self.active[s]:
                self.active[s] = a
                self.pets[s] = self._mkpet(s, a)
                self.hidden[s] = self.down[s] = False
                self.anim_left[s] = 0.0
                self.shown[s] = float(hps[a])
            elif hps[a] < self.target[s] and not self.down[s]:      # le pegaron
                pet = self.pets[s]
                if "h" in pet.s.durs:
                    pet.set_state("h")
                    self.anim_left[s] = sum(pet.s.durs["h"]) + 0.05
                else:
                    self.shake[s] = 0.45                             # sin animación: tiembla
            self.target[s] = float(hps[a])
        self.status = [(x[1], x[2]) for x in snap]
        self.team_hp = [x[3] for x in snap]

    # --- turnos
    def _start(self, steps):
        self.phase, self.steps, self.step_i = "play", steps, 0
        self._begin()

    def _begin(self):
        st = self.steps[self.step_i]
        self.msg, self.step_t, self.applied = pb.resolve_text(st.text), 0.0, False
        self.fx, self.lunge = [], False
        self.hit_t = 0.25 if st.atk is not None else 0.0     # cuándo cae el daño dentro del paso
        self.dur_eff = st.dur
        if st.faint is not None:
            pet = self.pets[st.faint]
            if "f" in pet.s.durs:                            # se cae y queda tirado hasta que lo reemplacen
                pet.set_state("f")
                pet.hold = True
                self.down[st.faint] = True
            else:
                self.hidden[st.faint] = True
        if st.atk is not None and st.move is not None:
            self._attack_fx(st)

    def _attack_fx(self, st):
        """Elige la animación del atacante según el ataque y programa proyectil / impacto."""
        s, mv = st.atk, st.move
        pet = self.pets[s]
        spr = pet.s
        codes = ("c",) if not mv.power else ("s", "a") if mv.special else ("a",)
        code = next((c for c in codes if c in spr.durs), None)
        ranged = bool(mv.power and mv.special) or (not mv.power and st.tgt != s)
        d = sum(spr.durs[code]) if code else 0.4
        if code:
            pet.set_state(code)
            self.anim_left[s] = d + 0.05
        if ranged:
            go = 0.4 * d if code else 0.1                     # el proyectil sale a mitad de la animación
            self.hit_t = go + 0.45
        elif mv.power:
            self.hit_t = max(0.15, (spr.hit.get(code) if code else None) or 0.6 * d)
        else:
            self.hit_t = 0.5 * d
        self.lunge = bool(mv.power) and (not ranged or code is None)
        self.dur_eff = max(st.dur, self.hit_t + 0.6, d + 0.2)
        color = TYPE_COLORS.get(mv.type, (255, 255, 255))
        size = max(8, spr.h // 4)
        if ranged:
            self.fx.append({"obj": Fx("orb", color, size), "t0": go, "t1": self.hit_t, "kind": "orb",
                            "src": s, "dst": st.tgt})
        if not st.miss:
            big = max(10, min(44, spr.h // 2))
            self.fx.append({"obj": Fx("burst", color, big), "t0": self.hit_t, "t1": self.hit_t + 0.4,
                            "kind": "burst", "src": s, "dst": st.tgt})

    def _act(self, action):
        if self.net:                                   # contra un amigo: lo manda y espera la respuesta del árbitro
            self.net.act(action)
            self.phase = "wait"
        else:
            self._start(self.b.turn(action))

    def _send(self, idx):
        """Reemplazo obligatorio después de que se debilita tu Pokémon."""
        if self.net:
            self.net.send(idx)
            self.phase = "wait"
        else:
            self._start(self.b.send(idx))

    def _poll_net(self):
        """Novedades de la red. Devuelve "rematch" cuando los dos pidieron la revancha."""
        for ev in self.net.poll():
            if ev[0] == "steps":
                self.queued.append(ev[1])
            elif ev[0] == "end" and self.phase != "over":      # el otro se fue o se cortó la conexión
                self.queued.clear()
                self.phase, self.over_t = "over", 0.0
                self.over_who, self.rematch_ok = ev[1], False
                self.msg = self._over_msg()
            elif ev[0] == "peer_rematch":
                self.peer_asked = True
            elif ev[0] == "gone":                              # terminó la batalla y el otro cerró
                self.peer_gone = True
            elif ev[0] == "rematch":
                return "rematch"
        if self.phase == "wait" and self.queued:
            self._start(self.queued.pop(0))
        return None

    def _over_msg(self):
        """Texto de la pantalla final (con las opciones que haya)."""
        who = self.over_who
        if not (self.net and self.rematch_ok):
            return who + "   Enter: volver"
        peer = self.net.peer
        if self.peer_gone:
            return f"{who}   {peer} se fue.   Enter: volver"
        if self.asked:
            return f"{who}   Esperando a {peer}...   Enter: salir"
        if self.peer_asked:
            return f"{who}   ¡{peer} quiere revancha!   R: aceptar   Enter: salir"
        return f"{who}   R: revancha   Enter: salir"

    def _finish(self):
        b = self.b
        self.wait = 0.8
        if b.over:
            self.phase, self.over_t = "over", 0.0
            self.over_who = ("¡Ganó tu equipo!" if b.winner == 0 else "¡Ganó el equipo rival!" if b.winner == 1
                             else "¡Empate!")
            self.rematch_ok = bool(self.net)
            self.msg = self._over_msg()
        elif b.pending[0]:
            self.phase, self.forced = "switch", True
        elif self.net and b.pending[1]:                    # el rival está eligiendo a su reemplazo
            self.phase, self.forced = "wait", False
        else:
            self.phase, self.forced = "main", False

    def _play(self, dt):
        st = self.steps[self.step_i]
        self.step_t += dt
        if st.snap and not self.applied and self.step_t >= self.hit_t:
            self._apply(st.snap)
            self.applied = True
        if self.step_t >= self.dur_eff:
            self.step_i += 1
            if self.step_i < len(self.steps):
                self._begin()
            else:
                self._finish()

    def _tell(self, text):
        self.note, self.note_t = text, 1.8

    # --- teclado
    def key(self, ch):
        """Devuelve "done" (terminó la batalla) / "flee" (huiste) / None."""
        c, b = ch.lower(), self.b
        if self.phase == "over":
            if c == "r" and self.net and self.rematch_ok and not self.peer_gone and not self.asked:
                self.asked = True                      # los dos tienen que pedirla para volver a la sala
                self.net.rematch()
                return None
            return "done" if ch in ("\r", "\n", " ", "\x1b") or c == "q" else None
        if c == "q":
            return "flee"
        if c == "a":
            if self.net:
                self._tell("No hay piloto automático contra un amigo")
            else:
                self.auto = not self.auto
                self.wait = 0.3
            return None
        back = c == "0" or ch in ("\x1b", "\x08", "\x7f")
        mine = b.fighter(0)
        if self.phase == "main":
            if c == "1":
                self.phase = "fight"
            elif c == "2":
                if len(b.alive(0)) > 1:
                    self.phase = "switch"
                else:
                    self._tell("¡No tenés más Pokémon para cambiar!")
            elif c == "3":
                return "flee"
        elif self.phase == "fight":
            if back:
                self.phase = "main"
            elif c in ("1", "2", "3", "4"):
                i = int(c) - 1
                if not any(p > 0 for p in mine.pp):           # sin PP: Forcejeo
                    if i == 0:
                        self._act(("move", -1))
                elif i < len(mine.moves):
                    if mine.pp[i] <= 0:
                        self._tell("¡No quedan PP para ese ataque!")
                    else:
                        self._act(("move", i))
        elif self.phase == "switch":
            if back and not self.forced:
                self.phase = "main"
            elif c in ("1", "2", "3", "4", "5", "6") and int(c) <= len(b.teams[0]):
                i = int(c) - 1
                f = b.teams[0][i]
                if f.hp <= 0:
                    self._tell(f"¡{f.name} no puede combatir!")
                elif i == b.active[0]:
                    self._tell(f"¡{f.name} ya está en combate!")
                elif self.forced:
                    self._send(i)
                else:
                    self._act(("switch", i))
        return None

    def update(self, dt, cols):
        if self.net and self._poll_net() == "rematch":
            return "rematch"
        for p in self.pets:
            p.tick_frame(dt)
        for s in (0, 1):                                    # las barras bajan de a poco
            f = self.b.teams[s][self.active[s]]
            step = max(1.0, f.max_hp) * 1.2 * dt
            self.shown[s] += max(-step, min(step, self.target[s] - self.shown[s]))
            self.shake[s] = max(0.0, self.shake[s] - dt)
        self.note_t -= dt
        for s in (0, 1):                                    # terminada la acción: vuelve a idle (o a dormir)
            if self.anim_left[s] > 0:
                self.anim_left[s] -= dt
            elif not self.down[s]:
                pet = self.pets[s]
                want = "z" if self.status[s][0] == "sleep" and "z" in pet.s.durs else "i"
                if pet.state != want:
                    pet.set_state(want)
        if self.phase == "play":
            self._play(dt)
        elif self.phase == "over":
            self.over_t += dt
            if self.net and self.rematch_ok:
                self.msg = self._over_msg()                 # cambia cuando el otro pide la revancha o se va
            if self.over_t > (180 if self.net else 15):     # se aburrió de esperar
                return "done"
        elif self.auto:
            self.wait -= dt
            if self.wait <= 0:
                if self.phase == "switch" and self.forced:
                    self._start(self.b.send(self.b.best_replacement(0)))
                elif self.phase == "switch":
                    self.phase = "main"
                else:
                    self.phase = "main"
                    self._act(self.b.ai_action(0))
        world = cols * self.ppc
        for s, p in enumerate(self.pets):
            x = 2 * self.ppc if s == 0 else world - p.s.w - 2 * self.ppc
            if self.phase == "play":
                st, t = self.steps[self.step_i], self.step_t
                if st.atk == s and self.lunge:              # embiste hasta el impacto y vuelve
                    th = self.hit_t
                    k = min(1.0, t / (th * 0.8)) if t < th else max(0.0, 1 - (t - th) / 0.25)
                    x += 3 * self.ppc * k * (1 if s == 0 else -1)
            if self.shake[s] > 0:
                x += self.ppc * (1 if int(self.shake[s] * 50) % 2 else -1)
            p.x = max(0, x)
        for e in self.fx:                                   # proyectil: del atacante al rival; destello: sobre el rival
            o, src, dst = e["obj"], self.pets[e["src"]], self.pets[e["dst"]]
            if e["kind"] == "orb":
                k = (self.step_t - e["t0"]) / max(1e-6, e["t1"] - e["t0"])
                x0 = src.x + (src.s.w if e["src"] == 0 else -o.size)
                o.x = x0 + (dst.x + dst.s.w / 2 - o.size / 2 - x0) * max(0.0, min(1.0, k))
                o.lift = max(0, src.s.h // 2 - o.size // 2)
            else:
                o.x = dst.x + dst.s.w / 2 - o.size / 2
                o.lift = max(0, dst.s.h // 2 - o.size // 2)
        return None

    def visible(self):
        live = [e["obj"] for e in self.fx if self.phase == "play" and e["t0"] <= self.step_t < e["t1"]]
        return [p for p, h in zip(self.pets, self.hidden) if not h] + live

    # --- texto
    def _bar(self, s, cols):
        team = self.b.teams[s]
        f = team[self.active[s]]
        hp = max(0, math.ceil(self.shown[s] - 1e-6))
        ratio = hp / f.max_hp
        bw = 8 if cols >= 90 else 5 if cols >= 60 else 3
        fill = min(bw, max(1 if hp else 0, round(bw * ratio)))
        color = 32 if ratio > 0.5 else 33 if ratio > 0.2 else 31
        st, conf = self.status[s]
        tag = (" " + pb.STATUS_TAG[st] if st else "") + (" CNF" if conf else "")
        right = f"] {hp}/{f.max_hp}"
        dots = [h > 0 for h in self.team_hp[s]]
        plain = f"{f.name} Nv{f.level}{tag} [" + "█" * fill + "░" * (bw - fill) + right + \
            (" " + "●" * len(dots) if len(dots) > 1 else "")
        ansi = (f"\x1b[97m{f.name}\x1b[0m Nv{f.level}\x1b[33m{tag}\x1b[0m [\x1b[{color}m" + "█" * fill
                + "\x1b[90m" + "░" * (bw - fill) + "\x1b[0m" + right)
        if len(dots) > 1:
            ansi += " " + "".join("\x1b[32m●" if d else "\x1b[90m○" for d in dots) + "\x1b[0m"
        return plain, ansi

    def menu(self):
        b = self.b
        me, foe = b.fighter(0), b.fighter(1)
        if self.phase == "wait":
            return "Esperando al rival...   (q: huir)"
        if self.phase == "main":
            return f"¿Qué hará {me.name}?   1 Luchar   2 Pokémon   3 Huir" + ("" if self.net else "   (a: automático)")
        if self.phase == "fight":
            if not any(p > 0 for p in me.pp):
                return "1 Forcejeo (sin PP)   0 volver"
            parts = []
            for i, m in enumerate(me.moves):
                kind = "estado" if not m.power else f"{m.power}"
                e = b.chart.mult(m.type, foe.types) if m.power else 1
                tag = "" if e == 1 else " x2" if e > 1 else " x½" if e > 0 else " x0"
                parts.append(f"{i + 1} {m.name} {me.pp[i]}/{m.pp} ({pb.TYPES_ES[m.type][:4]} {kind}{tag})")
            return "  ".join(parts) + "   0 volver"
        parts = []
        for i, f in enumerate(b.teams[0]):
            state = " K.O." if f.hp <= 0 else " (en combate)" if i == b.active[0] else ""
            parts.append(f"{i + 1} {f.name} Nv{f.level} {f.hp}/{f.max_hp}{state}")
        return ("¡Elegí al próximo!  " if self.forced else "") + "  ".join(parts) + ("" if self.forced else "   0 volver")

    def _menu_cells(self, cols, avail):
        """El menú como en los juegos: en cuadrícula y con colores (tipo del ataque, PP, efectividad, HP).
        Devuelve las filas, o None si no hay lugar (entonces se usa `menu()`, la línea compacta)."""
        b = self.b
        me, foe = b.fighter(0), b.fighter(1)
        width = max(10, cols - 1)
        back = [("0", KEY), (" volver", DIM)]
        if self.phase == "main" and avail >= 2:
            keys = [opt("1", "Luchar"), opt("2", "Pokémon"), opt("3", "Huir")]
            if not self.net:
                keys.append(opt("a", "Automático"))
            return [seg([(f"¿Qué hará {me.name}?", HI)], width), seg(flat(keys, "     "), width)]
        if self.phase == "fight":
            if not any(p > 0 for p in me.pp):
                return grid(None, [opt("1", "Forcejeo (sin PP)")], back, cols, avail, 1)
            items = []
            for i, m in enumerate(me.moves):
                e = b.chart.mult(m.type, foe.types) if m.power else 1
                eff = [] if e == 1 else [(" x2", GOOD)] if e > 1 else [(" x½", BAD)] if e > 0 else [(" x0", BAD)]
                pp = me.pp[i]
                items.append([(str(i + 1), KEY), (" " + m.name, rgb(TYPE_COLORS.get(m.type, (255, 255, 255)))),
                              (f"  {pp}/{m.pp}", BAD if pp == 0 else WARN if pp <= 2 else DIM),
                              (f"  {pb.TYPES_ES[m.type][:3]} {m.power or 'estado'}", DIM)] + eff)
            return grid(None, items, back, cols, avail, 2)
        if self.phase == "switch":
            items = []
            for i, f in enumerate(b.teams[0]):
                ratio = f.hp / f.max_hp
                hp = [("  K.O.", BAD)] if f.hp <= 0 else \
                     [(f"  {f.hp}/{f.max_hp}", GOOD if ratio > 0.5 else WARN if ratio > 0.2 else BAD)]
                here = i == b.active[0]
                items.append([(str(i + 1), KEY), (("▶" if here else " ") + f.name, WARN if here else DIM if f.hp <= 0 else HI),
                              (f" Nv{f.level}", DIM)] + hp)
            title = [("¡Elegí al próximo Pokémon!", WARN)] if self.forced else None
            return grid(title, items, None if self.forced else back, cols, avail, 3)
        return None

    def hud(self, cols, avail=1):
        """Fila 1: barras de HP, estado y equipo (vos a la izquierda, rival a la derecha). Debajo, hasta `avail`
        filas: el mensaje o el menú."""
        (lp, la), (rp, ra) = self._bar(0, cols), self._bar(1, cols)
        out = f"\x1b[1;1H\x1b[2K{la}\x1b[1;{max(len(lp) + 2, cols - len(rp))}H{ra}"
        width = max(10, cols - 1)
        if self.phase in ("play", "over"):
            lines = [seg([(self.msg, HI)], width)]
        elif self.note_t > 0:
            lines = [seg([(self.note, HI)], width)]
        elif self.auto:
            lines = [seg([("Piloto automático   (a: tomar el control   q: huir)", HI)], width)]
        else:
            lines = self._menu_cells(cols, avail) or [seg([(self.menu(), HI)], width)]
        return out + put_rows(lines, 2, 1 + avail)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Pokémon caminando en la terminal")
    ap.add_argument("pokemon", nargs="*", help="nombres (en inglés) o números de Pokédex")
    ap.add_argument("--random", type=int, metavar="N", help="N Pokémon al azar (gen 1-4)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="tamaño: 1 original (default, el más nítido), 2 grandes; <1 se ve borroso")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--sixel", action="store_true",
                    help="dibujar con imágenes Sixel: Pokémon chiquitos con texto de tamaño normal "
                         "(Windows Terminal 1.22+, WezTerm, foot...)")
    ap.add_argument("--bg", default="181818",
                    help="color de fondo para --sixel (hex, ej. 181818) o 'none' para transparente")
    ap.add_argument("--sleep-after", type=float, default=SLEEP_AFTER / 60, metavar="MIN",
                    help="minutos sin comandos para que se duerman (default 5, 0 = nunca)")
    args = ap.parse_args()
    sleep_after = max(0.0, args.sleep_after) * 60
    bg = None
    if args.sixel and args.bg.lower() != "none":
        h = args.bg.lstrip("#")
        bg = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    px_per_col = 10 if args.sixel else 1           # ancho del "mundo" en píxeles por columna
    speed_mult = 3.0 if args.sixel else 1.0

    if os.name == "nt":
        os.system("")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    names = list(args.pokemon)
    if args.random:
        names += [str(random.randint(1, 493)) for _ in range(args.random)]
    if not names:
        names = load_saved() or DEFAULT

    loader = Loader(max(0.2, args.scale))
    loader.request(names, replace=True)

    keys = Keys()
    events = Events(EVENTS_FILE)
    reactor = Reactor()
    touch(ALIVE_FILE)
    last_beat = last_activity = time.time()
    had_bubble = False
    jobs = queue.Queue()   # resultados de hilos: ("battle", Battle, sprites) | ("flash"|"error", texto, seg)
    scene = None           # BattleScene mientras hay una pelea
    battling = False       # preparando una pelea (buscando datos en internet)
    gyms = load_gyms()     # regiones -> grupos -> entrenadores (gimnasios.json)
    gym = None             # menú de combates (tecla g): {"stage": 0..3, "r": región, "g": grupo, "t": entrenador}
    dex = None             # elegir por región (a + Enter solo): {"gen": None | 1..9, "q": filtro, "page": n}
    dex_names = None       # generación -> nombres (se carga la primera vez que se abre)
    vs = None              # sala de versus en red (tecla v): pokenet.Versus
    picker = None          # elegir ataques del equipo de versus: MovePicker
    w = sys.stdout.write
    w("\x1b[?1049h\x1b[?25l\x1b[2J")

    def bye(*_):
        if scene and scene.net:
            scene.net.close()
        if vs:
            vs.close()
        try:
            os.remove(ALIVE_FILE)
        except OSError:
            pass
        keys.restore()
        w("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        os._exit(0)

    signal.signal(signal.SIGINT, bye)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, bye)

    cols, rows = shutil.get_terminal_size()
    pets = []
    mode = None            # None | "add" | "set"
    buf, cur = "", 0       # texto del prompt y posición del cursor
    last_input, prompt_err = {}, ""
    msg, msg_until = "Cargando...", time.time() + 30
    tick = 1 / max(5, args.fps)
    last = time.perf_counter()
    drawn_rows = 1         # filas de texto que dibujó el cuadro anterior (para borrar lo que sobre)

    def flash(text, secs=4):
        nonlocal msg, msg_until
        msg, msg_until = text, time.time() + secs

    def on_command(ev):
        """Un comando terminó en la otra terminal: despierta a todos y uno reacciona."""
        if not pets:
            return
        sleepers = [p for p in pets if p.sleeping]
        for p in pets:
            p.wake_up()
        found = reactor.react(ev)
        speaker = random.choice([p for p in pets if p not in sleepers] or pets)
        if found:
            text, mood = found
            speaker.say(text, 4)
            if top_row_of(pets, rows, args.sixel) < 3:      # no hay lugar arriba para el globo
                flash(f"{speaker.name}: {text}", 4)
            if mood == "scare":
                speaker.scare()
            elif mood == "happy":
                speaker.cheer()
        if sleepers and sleepers[0] is not speaker:
            sleepers[0].say("¡Ah! ¡Volviste!" if not found else "¿Eh? ¿Qué pasó?", 3)

    def start_battle(parts):
        """Tecla b. `pika char vs mew snor` = tu equipo vs el rival (hasta 6 por lado); `pika char` = 1 contra 1.
        El lado vacío se arma con los que caminan o al azar; sin nada, 3 contra 3."""
        nonlocal battling
        if battling:
            return
        low = [x.lower() for x in parts]
        if "vs" in low:
            i = low.index("vs")
            mine, rival = parts[:i], parts[i + 1:]
        elif len(parts) <= 2:
            mine, rival = parts[:1], parts[1:]
        else:
            flash("Para equipos usá vs:  pika char vs mew snor   (hasta 6 por lado)", 6)
            return
        if max(len(mine), len(rival)) > 6:
            flash("Hasta 6 Pokémon por equipo", 4)
            return
        walking = [p.name for p in pets]
        mine, rival = ([expand_name(n, walking) for n in team] for team in (mine, rival))
        size = max(len(mine), len(rival)) or 3
        used = set(mine + rival)
        for team in (mine, rival):
            while not team:
                for _ in range(size):
                    free = [n for n in walking if n not in used]
                    pick = random.choice(free) if free else str(random.randint(1, 493))
                    team.append(pick)
                    used.add(pick)
        battling = True
        flash(f"Buscando en internet {', '.join(mine)} vs {', '.join(rival)}... (la primera vez tarda)", 90)
        threading.Thread(target=prepare_battle, args=([mine, rival], loader, jobs, walking), daemon=True).start()

    def start_gym(trainer):
        """Tu equipo (los que caminan, hasta 6; si no hay, 3 al azar) contra un líder / Alto Mando."""
        nonlocal battling
        if battling:
            return
        mine = [p.name for p in pets][:6] or [str(random.randint(1, 493)) for _ in range(3)]
        battling = True
        flash(f"Buscando en internet el combate contra {trainer['nombre']}... (la primera vez tarda)", 90)
        threading.Thread(target=prepare_gym_battle, args=(trainer, mine, loader, jobs), daemon=True).start()

    def gym_trainers():
        return gyms[gym["r"]]["grupos"][gym["g"]]["entrenadores"]

    def gym_text():
        if not gyms:
            return "No encontré gimnasios.json (tiene que estar junto a pokewalk.py).   Esc cierra"
        st = gym["stage"]
        if st == 0:
            return "Combates:  " + "   ".join(f"{i + 1} {r['nombre']}" for i, r in enumerate(gyms)) + "      Esc vuelve"
        region = gyms[gym["r"]]
        if st == 1:
            return f"{region['nombre']}:  " + "   ".join(
                f"{i + 1} {g['nombre']}" for i, g in enumerate(region["grupos"])) + "      0 volver"
        if st == 2:
            return f"{region['nombre']} · {region['grupos'][gym['g']]['nombre']}:  " + "   ".join(
                f"{i + 1} {t['nombre']}" for i, t in enumerate(gym_trainers())) + "      0 volver"
        t = gym["t"]
        team = " · ".join(f"{p['nombre']} Nv{p['nivel']}" for p in t["equipo"])
        return f"{t['nombre']} ({t['detalle']}): {team}      Enter: pelear   0 volver"

    def gym_block(avail):
        """Menú de combates en cuadrícula (None si no hay filas libres: se usa `gym_text`)."""
        if not gyms:
            return None
        st = gym["stage"]
        back = [("0", KEY), (" volver    ", DIM), ("Esc", KEY), (" cierra", DIM)]
        if st == 0:
            return grid([("Combates de gimnasio · ¿de qué región?", HI)],
                        [opt(str(i + 1), r["nombre"]) for i, r in enumerate(gyms)],
                        [("Esc", KEY), (" cancela", DIM)], cols, avail)
        region = gyms[gym["r"]]
        if st == 1:
            return grid([(region["nombre"], HI)], [opt(str(i + 1), g["nombre"]) for i, g in enumerate(region["grupos"])],
                        back, cols, avail)
        if st == 2:
            title = [(f"{region['nombre']} · {region['grupos'][gym['g']]['nombre']}", HI)]
            return grid(title, [opt(str(i + 1), t["nombre"]) for i, t in enumerate(gym_trainers())], back, cols, avail, 4)
        t = gym["t"]
        return grid([(t["nombre"], WARN), (f"  {t['detalle']} · su equipo:", DIM)],
                    [[(p["nombre"], HI), (f" Nv{p['nivel']}", DIM)] for p in t["equipo"]],
                    [("Enter", KEY), (" pelear    ", DIM), ("0", KEY), (" volver", DIM)], cols, avail, 3)

    def vs_block(avail):
        """Sala de versus en cuadrícula (None si no hay filas libres o la etapa no tiene menú)."""
        v = vs.view()
        if not v:
            return None
        return grid([(v["title"], WARN if v.get("warn") else HI)], [opt(k, label) for k, label in v["items"]],
                    [(v["foot"], DIM)], cols, avail, 3)

    def idle_block(avail, talking):
        """Ayuda de teclas (dos filas agrupadas si hay lugar y ancho; si no, una sola con lo esencial)."""
        names = ", ".join(p.name for p in pets) or "(vacío)"
        fights = [opt("b", "batalla"), opt("v", "versus"), opt("g", "gimnasios")]
        team = [opt("a", "agregar"), opt("c", "cambiar"), opt("x", "sacar"), opt("r", "azar")]
        more = [opt("i", "info"), opt("h", "mimar"), opt("q", "salir")]
        width = max(10, cols - 1)
        if avail >= 2 and cols >= 84 and not talking:
            return [seg([("Combates  ", DIM)] + flat(fights) + [("      Tus Pokémon: ", DIM), (names, HI)], width),
                    seg([("Equipo    ", DIM)] + flat(team) + [("      Más: ", DIM)] + flat(more), width)]
        return [seg(flat(fights + team + more, "  ") + [("   ·  ", DIM), (names, DIM)], width)]

    def gym_key(ch):
        """Menú de combates con dígitos: región -> grupo -> entrenador -> Enter. 0 vuelve, Esc cierra."""
        nonlocal gym
        if ch == "\x1b" or not gyms:
            gym = None
        elif ch == "0" or ch in ("\x08", "\x7f"):
            gym = None if gym["stage"] == 0 else {**gym, "stage": gym["stage"] - 1}
        elif gym["stage"] == 3:
            if ch in ("\r", "\n"):
                trainer, gym = gym["t"], None
                start_gym(trainer)
        elif ch.isdigit():
            i, st = int(ch) - 1, gym["stage"]
            if st == 0 and i < len(gyms):
                gym = {"stage": 1, "r": i}
            elif st == 1 and i < len(gyms[gym["r"]]["grupos"]):
                gym = {**gym, "stage": 2, "g": i}
            elif st == 2 and i < len(gym_trainers()):
                gym = {**gym, "stage": 3, "t": gym_trainers()[i]}

    def open_dex():
        nonlocal dex, dex_names
        try:
            dex_names = dex_names or species_by_gen()
            dex = {"gen": None}
        except (OSError, ValueError, KeyError) as e:
            flash(f"No pude bajar la lista de Pokémon: {e}", 5)

    def dex_found():
        """Los de la región que coinciden con lo tipeado: dígitos = número de Pokédex que empieza así, letras = nombre."""
        q = dex["q"]
        return [(i, n) for i, n in dex_names.get(dex["gen"], [])
                if (str(i).startswith(q) if q.isdigit() else q in n)]

    def dex_page():
        """((número, nombre) de la página actual, página, total de páginas, cuántos coinciden con el filtro)."""
        found = dex_found()
        pages = max(1, -(-len(found) // DEX_PAGE))
        page = max(0, min(dex["page"], pages - 1))
        return found[page * DEX_PAGE:(page + 1) * DEX_PAGE], page, pages, len(found)

    def dex_text():
        if dex["gen"] is None:
            return "Elegir de:  " + "   ".join(f"{g} {REGIONS[g - 1]}" for g in sorted(dex_names)) + "      Esc vuelve"
        shown, page, pages, _ = dex_page()
        return (f"{REGIONS[dex['gen'] - 1]} {page + 1}/{pages}{' [' + dex['q'] + ']' if dex['q'] else ''}:  "
                + "   ".join(f"#{i} {n}" for i, n in shown) + "      número o nombre + Enter   ←→ página")

    def dex_block(avail):
        """Lista por región en cuadrícula (None si no hay filas libres: se usa `dex_text`)."""
        if dex["gen"] is None:
            return grid([("Elegir Pokémon · ¿de qué región?", HI)],
                        [opt(str(g), f"{REGIONS[g - 1]} (gen {g})") for g in sorted(dex_names)],
                        [("Esc", KEY), (" vuelve", DIM)], cols, avail)
        shown, page, pages, total = dex_page()
        title = [(f"{REGIONS[dex['gen'] - 1]} · {total} Pokémon", HI), (f"   página {page + 1}/{pages}", DIM)]
        if dex["q"]:
            title += [("   buscando: ", DIM), (dex["q"], WARN)]
        foot = [("número o nombre", KEY), (" + Enter elige    ", DIM), ("←→", KEY), (" página    ", DIM),
                ("Backspace", KEY), (" regiones    ", DIM), ("Esc", KEY), (" vuelve", DIM)]
        items = [opt(f"#{i}", n) for i, n in shown] or [[("(ninguno coincide)", DIM)]]
        return grid(title, items, foot, cols, avail, 3)

    def dex_key(ch):
        """Región (1-9) -> lista: dígitos o letras buscan (por número de Pokédex o nombre), Enter elige
        el número exacto o el primero que coincide, ←→ pasan de página, Backspace con la búsqueda vacía vuelve.
        Lo elegido se escribe en el prompt que la abrió (Esc vuelve a ese prompt sin elegir)."""
        nonlocal dex, buf, cur
        if ch == "\x1b":
            dex = None
        elif dex["gen"] is None:
            if ch == "0" or ch in ("\x08", "\x7f"):
                dex = None
            elif ch.isdigit() and int(ch) in dex_names:
                dex = {"gen": int(ch), "q": "", "page": 0}
        elif ch in ("\x08", "\x7f"):
            dex = {**dex, "q": dex["q"][:-1], "page": 0} if dex["q"] else {"gen": None}
        elif ch in (K_LEFT, K_RIGHT):
            _, page, pages, _ = dex_page()
            dex = {**dex, "page": max(0, min(pages - 1, page + (1 if ch == K_RIGHT else -1)))}
        elif ch in ("\r", "\n"):
            found = dex_found()
            exact = [p for p in found if str(p[0]) == dex["q"]]
            pick = (exact or dex_page()[0] or [None])[0]
            if pick:
                dex = None
                if mode == "info" or not buf.strip():       # info toma uno solo: lo reemplaza
                    buf = pick[1]
                else:
                    buf = buf.rstrip() + " " + pick[1]
                buf += " " if mode != "info" else ""         # listo para tipear otro (o Tab de nuevo)
                cur = len(buf)
        elif ch.isprintable() and ch != " ":
            dex = {**dex, "q": dex["q"] + ch.lower(), "page": 0}

    def start_info(parts):
        term = parts[0] if parts else (random.choice(pets).name if pets else str(random.randint(1, 493)))
        flash(f"Buscando a {term}...", 30)
        threading.Thread(target=lookup_info, args=(term, jobs, [p.name for p in pets]), daemon=True).start()

    def pet_them(parts):
        """Tecla h: mima a los nombrados (nombre, número o principio del nombre); Enter solo = uno al azar."""
        targets, missing = [], []
        for term in parts:
            i = find_pet(pets, term)
            if i is None:
                missing.append(term)
            else:
                targets.append(pets[i])
        if not parts and pets:
            targets = [random.choice(pets)]
        for p in targets:
            p.wake_up()
            p.cheer()
            p.say(f"{pb.pretty(p.name)}: {random.choice(PET_LINES)}", 3)
        if missing:
            flash("No está: " + ", ".join(missing), 3)

    def close_vs(text="", secs=5):
        """Sale de la sala de versus (avisa al amigo) y cierra lo que estuviera abierto."""
        nonlocal vs, picker, mode, buf
        if vs:
            vs.close()
        vs = picker = None
        if mode in ("vsteam", "vsip"):
            mode, buf = None, ""
        if text:
            flash(text, secs)

    def vs_connect(text):
        """Tecla v -> 3: `ip` o `ip:puerto`."""
        host, _, port = text.strip().partition(":")
        try:
            port = int(port) if port else pn.TCP_PORT
        except ValueError:
            port = -1
        if not host or not 0 < port < 65536:
            flash("Escribí la IP del amigo (ej. 192.168.1.20)", 4)
        elif vs:
            vs.connect(host, port)

    def end_scene(how):
        nonlocal scene, vs
        sess = scene.net
        if how == "rematch":
            # los dos la pidieron: de vuelta a la sala con el mismo amigo y la misma conexión (sin IPs ni búsqueda).
            # El equipo de recién se puede repetir con un Enter (copias nuevas, con todo el HP y los PP).
            prev = ([pn.fighter_from_wire(pn.fighter_to_wire(f)) for f in scene.b.teams[0]], list(scene.sprites[0]))
            vs = pn.Versus(pn.player_name(), sess.link, sess.peer, sess.role, prev)
            flash(f"¡Revancha con {sess.peer}!", 3)
        else:
            if sess:
                sess.close()
            if how == "flee":
                flash("¡Escapaste del combate!", 3)
        scene = None

    try:
        while True:
            now = time.perf_counter()
            dt, last = now - last, now
            redraw = False

            # --- resultados de hilos (batalla / info)
            while not jobs.empty():
                job = jobs.get()
                if job[0] == "battle":
                    battling, redraw = False, True
                    scene = BattleScene(job[1], job[2], px_per_col, job[3])
                    flash("", 0)
                elif job[0] == "vspool":                        # tu equipo cargó: ahora elegís los ataques
                    if vs:
                        picker = MovePicker(job[1], job[2])
                        vs.my_sprites, vs.stage = job[3], "moves"
                        flash("", 0)
                elif job[0] == "vsbattle":                      # los dos equipos listos: empieza el versus
                    if vs is None:                              # cancelaron mientras se preparaba
                        job[4].close()
                    else:
                        vs, redraw = None, True
                        scene = BattleScene(job[1], job[2], px_per_col, job[3], job[4])
                        flash("", 0)
                elif job[0] == "vserror":                       # falló el equipo: reabrir el prompt con lo escrito
                    flash(job[1], job[2])
                    if vs and mode is None:
                        vs.stage = "team"
                        mode, buf, prompt_err = "vsteam", last_input.get("vsteam", ""), job[1].rsplit(": ", 1)[-1]
                        cur = len(buf)
                elif job[0] == "vsfatal":
                    close_vs(job[1], job[2])
                else:                                          # "flash" | "error" | "berror" | "ierror"
                    battling = battling and job[0] not in ("berror", "gerror")
                    flash(job[1], job[2])
                    again = {"berror": "battle", "ierror": "info"}.get(job[0])
                    if again and mode is None and not scene:    # falló un nombre: reabrir con lo escrito, para corregir
                        mode, buf, prompt_err = again, last_input.get(again, ""), job[1].rsplit(": ", 1)[-1]
                        cur = len(buf)

            # --- resultados de cargas
            while not loader.done.empty():
                replace, ok, errors = loader.done.get()
                loader.busy -= 1
                if replace and ok:
                    pets = []
                pets += [Pet(n, s, cols * px_per_col, speed_mult) for n, s in ok]
                save([p.name for p in pets])
                redraw = True
                if errors:
                    flash("No pude: " + " | ".join(errors), 6)
                elif ok:
                    flash("Listo: " + ", ".join(n for n, _ in ok), 2)

            # --- comandos de la otra terminal
            tnow = time.time()
            if tnow - last_beat >= 5:
                touch(ALIVE_FILE)
                last_beat = tnow
            evs = events.poll()
            if evs:
                last_activity = tnow
                if not scene:
                    on_command(evs[-1])

            # --- sala de versus
            if vs:
                ev = vs.update()
                if ev and ev[0] == "team":
                    mode, buf, cur, prompt_err = "vsteam", "", 0, ""
                elif ev and ev[0] == "ready":
                    threading.Thread(target=prepare_versus, args=(vs, loader, jobs), daemon=True).start()
                elif ev and ev[0] == "error":
                    close_vs(ev[1], 6)
            # --- teclado
            typed = keys.read()
            if typed:
                last_activity = tnow
            for ch in typed:
                if scene:
                    how = scene.key(ch)
                    if how:
                        end_scene(how)
                        redraw = True
                    continue
                if picker:
                    if ch == "\x1b":
                        close_vs("Saliste de la sala")
                    elif picker.key(ch, cols) == "done":
                        vs.set_team(picker.fighters, vs.my_sprites)
                        picker = None
                    continue
                if vs and not mode:
                    act = vs.key(ch)
                    if act == "cancel":
                        close_vs("Saliste de la sala", 2)
                    elif act == "ip":
                        mode, buf, cur, prompt_err = "vsip", "", 0, ""
                    continue
                if gym:
                    gym_key(ch)
                    continue
                if dex:
                    dex_key(ch)
                    continue
                if mode:
                    cur = min(cur, len(buf))
                    if mode in DEX_MODES and (ch == "\t" or (mode == "add" and ch in ("\r", "\n") and not buf.strip())):
                        open_dex()
                    elif ch in ("\r", "\n"):
                        parts = buf.split()
                        last_input[mode], prompt_err = buf, ""      # por si falla un nombre: se reabre con esto
                        if mode == "del":
                            gone, missing = [], []
                            for term in (parts or [None]):
                                i = find_pet(pets, term)
                                if i is None:
                                    missing.append(term)
                                else:
                                    gone.append(pets.pop(i).name)
                            if gone:
                                save([p.name for p in pets])
                                redraw = True
                            txt = ("Chau " + ", ".join(gone)) if gone else ""
                            if missing:
                                txt += ("   " if txt else "") + "No está: " + ", ".join(missing)
                            flash(txt or "No hay nada para sacar", 3)
                        elif mode == "battle":
                            start_battle(parts)
                        elif mode == "info":
                            start_info(parts)
                        elif mode == "pet":
                            pet_them(parts)
                        elif mode == "vsteam":
                            if vs and not parts and vs.prev_team:       # revancha: Enter solo = el mismo equipo y ataques
                                vs.set_team(vs.prev_team, vs.prev_sprites)
                                flash(f"Mismo equipo y ataques. Esperando a {vs.peer}...", 4)
                            elif vs:
                                vs.stage = "loading"
                                flash("Buscando tu equipo en internet... (la primera vez tarda)", 90)
                                threading.Thread(target=load_my_team, daemon=True,
                                                 args=(parts, loader, jobs, [p.name for p in pets])).start()
                        elif mode == "vsip":
                            vs_connect(buf)
                        elif parts:
                            parts, cut = expand_ranges(parts)
                            loader.request(parts, replace=(mode == "set"))
                            note = f"   (máximo {MAX_PER_ADD} por vez: quedaron {cut} afuera)" if cut else ""
                            flash("Cargando " + ", ".join(parts) + "..." + note, 30)
                        mode, buf = None, ""
                    elif ch == "\x1b":
                        if mode == "vsteam":                      # ya estabas en la sala: Esc = salir de ella
                            close_vs("Saliste de la sala", 2)
                        mode, buf, prompt_err = None, "", ""
                    elif ch in ("\x08", "\x7f"):
                        if cur:
                            buf, cur = buf[:cur - 1] + buf[cur:], cur - 1
                    elif ch == K_DEL:
                        buf = buf[:cur] + buf[cur + 1:]
                    elif ch == K_LEFT:
                        cur = max(0, cur - 1)
                    elif ch == K_RIGHT:
                        cur = min(len(buf), cur + 1)
                    elif ch == K_HOME:
                        cur = 0
                    elif ch == K_END:
                        cur = len(buf)
                    elif ch.isprintable():
                        buf, cur = buf[:cur] + ch + buf[cur:], cur + 1
                else:
                    c = ch.lower()
                    if ch in ("\x1b", "\r", "\n"):
                        msg_until = 0.0                 # cierra el mensaje (info, errores...)
                    elif c == "q":
                        bye()
                    elif c == "a":
                        mode, buf = "add", ""
                    elif c == "c":
                        mode, buf = "set", ""
                    elif c == "x":
                        mode, buf = "del", ""
                    elif c == "r":
                        loader.request([str(random.randint(1, 493))], replace=False)
                        flash("Buscando uno al azar...", 30)
                    elif c == "b":
                        mode, buf = "battle", ""
                    elif c == "i":
                        mode, buf = "info", ""
                    elif c == "g":
                        gym = {"stage": 0}
                    elif c == "v":
                        if battling:
                            flash("Esperá a que termine de prepararse la batalla", 3)
                        else:
                            vs = pn.Versus(pn.player_name())
                    elif c == "h" and pets:
                        mode, buf = "pet", ""

            # --- tamaño
            size = shutil.get_terminal_size()
            if size != (cols, rows):
                cols, rows = size
                redraw = True
            if redraw:
                w("\x1b[2J")

            if scene:
                how = scene.update(dt, cols)
                if how:
                    end_scene(how)
                    w("\x1b[2J")
                else:
                    frame = (render_sixel(scene.visible(), cols, rows, bg, reserve=2) if args.sixel
                             else render(scene.visible(), cols, rows, reserve=2))
                    avail = text_rows_free(scene.pets, rows, args.sixel, 2) - 1
                    w("\x1b[?2026h" + frame + scene.hud(cols, avail) + "\x1b[?2026l")
                    sys.stdout.flush()
                    time.sleep(max(0, tick - (time.perf_counter() - now)))
                    continue

            idle = time.time() - last_activity
            for p in pets:
                p.update(dt, cols * px_per_col, idle, sleep_after)

            # --- filas de texto arriba: los menús usan varias (en cuadrícula) si hay lugar, si no, una sola
            avail = text_rows_free(pets, rows, args.sixel)
            talking = any(p.say_until > time.time() for p in pets)
            block = None
            if gym:
                block = gym_block(avail)
                status = None if block else status_line(gym_text(), cols, True)
            elif dex:
                block = dex_block(avail)
                status = None if block else status_line(dex_text(), cols, True)
            elif mode:
                label = f"{prompt_err}  →  corregí y Enter (Esc cancela)" if prompt_err else PROMPTS[mode]
                if mode == "vsteam" and vs and not prompt_err:
                    label = PROMPTS["vsteam_again" if vs.prev_team else "vsteam"].format(peer=vs.peer)
                status = prompt_line(label, buf, cur, cols)
            elif picker:
                block = picker.lines(cols, avail)
                status = None if block else status_line(picker.text(cols), cols, True)
            elif vs:
                block = vs_block(avail)
                status = None if block else status_line(vs.text(), cols, True)
            elif time.time() < msg_until:
                status = status_line(msg, cols, True)
            else:
                block = idle_block(avail, talking)
            if block:
                status = put_rows(block, 1, drawn_rows)
            elif drawn_rows > 1:
                status += put_rows([], 2, drawn_rows)           # borra lo que dejó un menú más alto
            drawn_rows = len(block) if block else 1

            frame = render_sixel(pets, cols, rows, bg) if args.sixel else render(pets, cols, rows)
            if block and pets and len(block) >= top_row_of(pets, rows, args.sixel) - 1:
                bub = ""                                    # no hay lugar para el globo sin pisar el menú
            else:
                bub = bubbles(pets, cols, rows, args.sixel, px_per_col)
            if had_bubble and not bub:
                w("\x1b[2J")                         # se fue el globo: limpiar su rastro
            had_bubble = bool(bub)
            w("\x1b[?2026h" + frame + bub + status + "\x1b[?2026l")
            sys.stdout.flush()
            time.sleep(max(0, tick - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        bye()
    finally:
        keys.restore()


if __name__ == "__main__":
    main()
