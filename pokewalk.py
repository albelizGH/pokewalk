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
import os
import queue
import random
import shutil
import signal
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

try:
    from PIL import Image
except ImportError:
    sys.exit("Falta Pillow:  pip install pillow")

SPRITE_URL = "https://raw.githubusercontent.com/PMDCollab/SpriteCollab/master/sprite/{id:04d}/{file}"
SPECIES_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species.csv"
DEFAULT = ["pikachu", "charmander", "squirtle"]

ROW_RIGHT, ROW_LEFT = 2, 6      # filas del sprite sheet de PMD
ALPHA_MIN = 128

if os.name == "nt":
    CACHE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pokewalk")
else:
    CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pokewalk")
STATE_FILE = os.path.join(CACHE, "ultimos.txt")


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

    def row(r):
        return [sheet.crop((i * fw, r * fh, (i + 1) * fw, (r + 1) * fh)) for i in range(len(durs))]

    return row(ROW_RIGHT), row(ROW_LEFT), durs


class Sprite:
    def __init__(self, dex, scale):
        xml = ET.fromstring(fetch(SPRITE_URL.format(id=dex, file="AnimData.xml"), f"{dex:04d}/AnimData.xml"))
        walk = load_anim(dex, "Walk", xml)
        if walk is None:
            raise ValueError("no tiene animación de caminata")
        idle = load_anim(dex, "Idle", xml) or ([walk[0][0]], [walk[1][0]], [1.0])

        every = walk[0] + walk[1] + idle[0] + idle[1]
        W = max(im.width for im in every)
        H = max(im.height for im in every)

        def norm(im):
            c = Image.new("RGBA", (W, H))
            c.paste(im, ((W - im.width) // 2, H - im.height))
            return c

        sets = {k: [norm(im) for im in v] for k, v in
                {"wr": walk[0], "wl": walk[1], "ir": idle[0], "il": idle[1]}.items()}

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


class Pet:
    def __init__(self, name, sprite, width, speed_mult=1.0):
        self.name = name
        self.s = sprite
        self.x = random.uniform(0, max(1, width - sprite.w))
        self.dir = random.choice((1, -1))
        self.speed = random.uniform(7, 13) * min(1.5, max(0.4, sprite.scale)) * speed_mult
        self.set_state("w")

    def set_state(self, st):
        self.state, self.frame = st, 0
        self.frame_left = self.s.durs[st][0]
        self.state_left = random.uniform(2, 7) if st == "w" else random.uniform(1, 3.5)

    def update(self, dt, width):
        self.state_left -= dt
        if self.state_left <= 0:
            if self.state == "w" and random.random() < 0.5:
                self.set_state("i")
            else:
                if self.state == "i" and random.random() < 0.4:
                    self.dir *= -1
                self.set_state("w")
        durs = self.s.durs[self.state]
        self.frame_left -= dt
        while self.frame_left <= 0:
            self.frame = (self.frame + 1) % len(durs)
            self.frame_left += durs[self.frame]
        if self.state == "w":
            self.x += self.dir * self.speed * dt
            maxx = max(0, width - self.s.w)
            if self.x <= 0:
                self.x, self.dir = 0, 1
            elif self.x >= maxx:
                self.x, self.dir = maxx, -1

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
                if ch in ("\x00", "\xe0"):      # flechas/F1..: descartar
                    self.m.getwch()
                    continue
                out.append(ch)
        else:
            import select
            while select.select([sys.stdin], [], [], 0)[0]:
                data = os.read(self.fd, 64).decode("utf-8", "ignore")
                if not data:
                    break
                out.extend(data)
        return out

    def restore(self):
        if not self.win:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


# --------------------------------------------------------------------------- render
def render(pets, cols, rows):
    if not pets:
        return ""
    ph = max(p.s.h for p in pets)
    ph += ph % 2
    canvas = [[None] * cols for _ in range(ph)]
    for p in sorted(pets, key=lambda p: p.s.h, reverse=True):
        data, w, h = p.pixels()
        ox, oy = int(p.x), ph - h
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
    avail = max(1, rows - 1)                 # la fila 1 es para el estado
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


def render_sixel(pets, cols, rows, bg, cell_w=10, cell_h=20):
    """Dibuja los Pokémon como imagen Sixel (píxeles reales, texto normal).
    Windows Terminal mapea 10x20 píxeles Sixel a cada celda."""
    if not pets:
        return ""
    W = cols * cell_w
    ph = max(p.s.h for p in pets)
    cells = -(-ph // cell_h)
    top_row = max(2, rows - cells)          # deja la última fila libre: así no scrollea
    cells = min(cells, max(1, rows - top_row))
    H = cells * cell_h

    # 1) componer: {(x, y): (r, g, b)}
    px = {}
    for p in sorted(pets, key=lambda p: p.s.h, reverse=True):
        data, w, h = p.pixels()
        ox, oy = int(p.x), H - h
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


def status_line(text, cols, bright=False):
    text = text[:max(0, cols - 1)]
    color = "\x1b[97m" if bright else "\x1b[90m"
    return f"\x1b[1;1H\x1b[2K{color}{text}\x1b[0m"


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
    args = ap.parse_args()
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
    w = sys.stdout.write
    w("\x1b[?1049h\x1b[?25l\x1b[2J")

    def bye(*_):
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
    buf = ""
    msg, msg_until = "Cargando...", time.time() + 30
    tick = 1 / max(5, args.fps)
    last = time.perf_counter()

    def flash(text, secs=4):
        nonlocal msg, msg_until
        msg, msg_until = text, time.time() + secs

    try:
        while True:
            now = time.perf_counter()
            dt, last = now - last, now
            redraw = False

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

            # --- teclado
            for ch in keys.read():
                if mode:
                    if ch in ("\r", "\n"):
                        parts = buf.split()
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
                        elif parts:
                            loader.request(parts, replace=(mode == "set"))
                            flash("Cargando " + ", ".join(parts) + "...", 30)
                        mode, buf = None, ""
                    elif ch == "\x1b":
                        mode, buf = None, ""
                    elif ch in ("\x08", "\x7f"):
                        buf = buf[:-1]
                    elif ch.isprintable():
                        buf += ch
                else:
                    c = ch.lower()
                    if c == "q":
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

            # --- tamaño
            size = shutil.get_terminal_size()
            if size != (cols, rows):
                cols, rows = size
                redraw = True
            if redraw:
                w("\x1b[2J")

            for p in pets:
                p.update(dt, cols * px_per_col)

            # --- línea de estado
            if mode == "add":
                status = status_line(f"Agregar (nombre o número, Enter): {buf}█", cols, True)
            elif mode == "set":
                status = status_line(f"Cambiar todos (separados por espacio, Enter): {buf}█", cols, True)
            elif mode == "del":
                status = status_line(f"Sacar (nombre, número o el principio del nombre; Enter solo = el último): {buf}█",
                                     cols, True)
            elif time.time() < msg_until:
                status = status_line(msg, cols, True)
            else:
                lista = ", ".join(p.name for p in pets) or "(vacío)"
                status = status_line(f"{lista}   ·  a agregar  c cambiar  x sacar  r azar  q salir", cols)

            frame = render_sixel(pets, cols, rows, bg) if args.sixel else render(pets, cols, rows)
            w("\x1b[?2026h" + frame + status + "\x1b[?2026l")
            sys.stdout.flush()
            time.sleep(max(0, tick - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        bye()
    finally:
        keys.restore()


if __name__ == "__main__":
    main()
