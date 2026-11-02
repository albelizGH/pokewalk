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
    "add": "Agregar (nombre o número, Enter)",
    "set": "Cambiar todos (separados por espacio, Enter)",
    "del": "Sacar (nombre, número o principio del nombre; Enter solo = el último)",
    "battle": "Batalla (pika char = 1 vs 1;  pika char vs mew snor = equipos;  Enter solo = 3 vs 3 al azar)",
    "pet": "Mimar a (nombre, número o principio; Enter solo = uno al azar)",
    "info": "Info de (nombre, número o principio; Enter solo = uno que camina)",
}


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


# --------------------------------------------------------------------------- batalla
PET_LINES = ["<3", "¡Hola!", "¿Jugamos?", "¡Me encanta caminar!", "¡Qué lindo día!", "¿Me das una baya?", "¡Presente!"]


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


def prepare_battle(sides, loader, jobs, walking=()):
    """Hilo: busca en PokeAPI stats y ataques de todos los Pokémon de ambos equipos (más la tabla de
    tipos) y baja los sprites. Cuando está todo listo avisa a la pantalla; mientras tanto la animación sigue."""
    def build(name):
        dex, key = resolve(expand_name(name, walking))
        if (dex, "batalla") not in loader.cache:         # versión con las animaciones de combate
            loader.cache[(dex, "batalla")] = Sprite(dex, loader.scale, battle=True)
        return pb.load_fighter(fetch, dex, key), loader.cache[(dex, "batalla")]
    try:
        with ThreadPoolExecutor(4) as ex:
            built = list(ex.map(build, [n for side in sides for n in side]))
        teams, sprites, k = [], [], 0
        for side in sides:
            chunk = built[k:k + len(side)]
            k += len(side)
            teams.append([f for f, _ in chunk])
            sprites.append([s for _, s in chunk])
        chart = pb.TypeChart(fetch)
        chart.preload({m.type for team in teams for f in team for m in f.moves})
        jobs.put(("battle", pb.Battle(teams[0], teams[1], chart), sprites))
    except Exception as e:     # sin internet, nombre inválido, sprite faltante...
        jobs.put(("berror", f"No pude preparar la batalla: {e}", 6))


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

    def __init__(self, battle, sprites, ppc):
        self.b, self.sprites, self.ppc = battle, sprites, ppc     # sprites[lado][índice en el equipo]
        self.active = [0, 0]
        self.pets = [self._mkpet(0, 0), self._mkpet(1, 0)]
        self.hidden = [False, False]
        self.shown = [float(battle.fighter(s).hp) for s in (0, 1)]    # HP que se dibuja (baja suave)
        self.target = list(self.shown)
        self.shake = [0.0, 0.0]
        self.auto, self.wait, self.over_t, self.forced = False, 0.0, 0.0, False
        self.note, self.note_t = "", 0.0
        self.anim_left = [0.0, 0.0]                    # lo que le queda a la animación de acción de cada lado
        self.down = [False, False]                     # debilitado y tirado en el piso
        self.fx, self.hit_t, self.dur_eff, self.lunge = [], 0.0, 0.0, False
        self._apply(battle.snap())
        a, b = battle.teams
        intro = f"¡{a[0].name} vs {b[0].name}!" if len(a) == len(b) == 1 else \
            f"¡Batalla de equipos! {len(a)} contra {len(b)}"
        self._start([pb.Step(intro, dur=1.8)])

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
        self.msg, self.step_t, self.applied = st.text, 0.0, False
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
        self._start(self.b.turn(action))

    def _finish(self):
        b = self.b
        self.wait = 0.8
        if b.over:
            self.phase, self.over_t = "over", 0.0
            who = "¡Ganó tu equipo!" if b.winner == 0 else "¡Ganó el equipo rival!" if b.winner == 1 else "¡Empate!"
            self.msg = who + "   Enter: volver"
        elif b.pending[0]:
            self.phase, self.forced = "switch", True
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
            return "done" if ch in ("\r", "\n", " ", "\x1b") or c == "q" else None
        if c == "q":
            return "flee"
        if c == "a":
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
                    self._start(b.send(i))
                else:
                    self._act(("switch", i))
        return None

    def update(self, dt, cols):
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
            if self.over_t > 15:                            # se aburrió de esperar
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
        plain = f"{f.name}{tag} [" + "█" * fill + "░" * (bw - fill) + right + \
            (" " + "●" * len(dots) if len(dots) > 1 else "")
        ansi = (f"\x1b[97m{f.name}\x1b[33m{tag}\x1b[0m [\x1b[{color}m" + "█" * fill + "\x1b[90m" + "░" * (bw - fill)
                + "\x1b[0m" + right)
        if len(dots) > 1:
            ansi += " " + "".join("\x1b[32m●" if d else "\x1b[90m○" for d in dots) + "\x1b[0m"
        return plain, ansi

    def menu(self):
        b = self.b
        me, foe = b.fighter(0), b.fighter(1)
        if self.phase == "main":
            return f"¿Qué hará {me.name}?   1 Luchar   2 Pokémon   3 Huir   (a: automático)"
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
            parts.append(f"{i + 1} {f.name} {f.hp}/{f.max_hp}{state}")
        return ("¡Elegí al próximo!  " if self.forced else "") + "  ".join(parts) + ("" if self.forced else "   0 volver")

    def hud(self, cols):
        """Fila 1: barras de HP, estado y equipo (vos a la izquierda, rival a la derecha). Fila 2: mensaje o menú."""
        (lp, la), (rp, ra) = self._bar(0, cols), self._bar(1, cols)
        out = f"\x1b[1;1H\x1b[2K{la}\x1b[1;{max(len(lp) + 2, cols - len(rp))}H{ra}"
        if self.phase in ("play", "over"):
            text = self.msg
        elif self.note_t > 0:
            text = self.note
        elif self.auto:
            text = "Piloto automático   (a: tomar el control   q: huir)"
        else:
            text = self.menu()
        return out + f"\x1b[2;1H\x1b[2K\x1b[97m{text[:max(0, cols - 1)]}\x1b[0m"


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
    w = sys.stdout.write
    w("\x1b[?1049h\x1b[?25l\x1b[2J")

    def bye(*_):
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

    def end_scene(how):
        nonlocal scene
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
                    battling, scene, redraw = False, BattleScene(job[1], job[2], px_per_col), True
                    flash("", 0)
                else:                                           # "flash" | "error" | "berror" | "ierror"
                    battling = battling and job[0] != "berror"
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
                if mode:
                    cur = min(cur, len(buf))
                    if ch in ("\r", "\n"):
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
                        elif parts:
                            loader.request(parts, replace=(mode == "set"))
                            flash("Cargando " + ", ".join(parts) + "...", 30)
                        mode, buf = None, ""
                    elif ch == "\x1b":
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
                if scene.update(dt, cols):
                    end_scene("done")
                    w("\x1b[2J")
                else:
                    frame = (render_sixel(scene.visible(), cols, rows, bg, reserve=2) if args.sixel
                             else render(scene.visible(), cols, rows, reserve=2))
                    w("\x1b[?2026h" + frame + scene.hud(cols) + "\x1b[?2026l")
                    sys.stdout.flush()
                    time.sleep(max(0, tick - (time.perf_counter() - now)))
                    continue

            idle = time.time() - last_activity
            for p in pets:
                p.update(dt, cols * px_per_col, idle, sleep_after)

            # --- línea de estado
            if mode:
                label = f"{prompt_err}  →  corregí y Enter (Esc cancela)" if prompt_err else PROMPTS[mode]
                status = prompt_line(label, buf, cur, cols)
            elif time.time() < msg_until:
                status = status_line(msg, cols, True)
            else:
                lista = ", ".join(p.name for p in pets) or "(vacío)"
                status = status_line(f"{lista}   ·  a agregar  c cambiar  x sacar  r azar  b batalla  i info  h mimar  q salir", cols)

            frame = render_sixel(pets, cols, rows, bg) if args.sixel else render(pets, cols, rows)
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
