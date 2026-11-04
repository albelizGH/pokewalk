"""
Configuración de pokewalk, compartida por el juego (tecla `o`) e install.py:

  - config.json en el caché (%LOCALAPPDATA%\\pokewalk): escala, minutos para dormirse, letra y alto del panel.
    Lo guardado ahí gana sobre `--scale` / `--sleep-after` de la línea de comandos (las que puso install.py).
  - settings.json de Windows Terminal: el tamaño de letra del perfil "Pokewalk" y el alto del panel (el `size`
    del splitPane del atajo). Antes de la primera escritura de cada sesión se guarda una copia
    settings.json.bak-pokewalk-<fecha>, como hace install.py.

Todas las funciones reciben la ruta del archivo, así se pueden probar con copias.
"""
import json
import os
import re
import shutil
import time

GUID = "{3f6b2c1e-8a4d-4e5f-9b7c-25a0c0ffee25}"     # el perfil "Pokewalk" que crea install.py
ACTION_ID = "User.Pokewalk"                           # la acción del atajo (abre el panel abajo)

if os.name == "nt":
    CACHE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pokewalk")
else:
    CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "pokewalk")
CONFIG_FILE = os.path.join(CACHE, "config.json")

# Valores que se ofrecen (+ / - van al siguiente) y los límites de lo que se acepta del archivo
SCALES = [1, 1.5, 2, 2.5, 3, 4]
FONT_SIZES = list(range(6, 25))
FONT_DEFAULT = 12                                     # el de Windows Terminal si el perfil no dice nada
PANELS = [0.12, 0.15, 0.18, 0.22, 0.25, 0.3, 0.35, 0.4, 0.5]
SLEEPS = [0, 1, 2, 3, 5, 10, 15, 30, 60]              # minutos (0 = nunca)
LIMITS = {"scale": (0.2, 8), "font_size": (4, 72), "panel": (0.05, 0.9), "sleep_after": (0, 24 * 60)}


class SettingsError(Exception):
    """No se pudo leer o escribir el settings.json de Windows Terminal (el mensaje ya está en castellano)."""


# --------------------------------------------------------------------------- config.json
def clean_config(raw):
    """Solo las claves conocidas, numéricas y dentro de sus límites (lo demás se ignora)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, (lo, hi) in LIMITS.items():
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            continue
        out[k] = int(v) if k == "font_size" else float(v)
    return out


def load_config(path=CONFIG_FILE):
    """La configuración guardada ({} si no hay archivo o está roto)."""
    try:
        with open(path, encoding="utf-8") as f:
            return clean_config(json.load(f))
    except (OSError, ValueError):
        return {}


def update_config(changes, path=CONFIG_FILE):
    """Mezcla `changes` con lo guardado y lo escribe de una (archivo temporal + reemplazo). Devuelve lo guardado."""
    cfg = load_config(path)
    cfg.update(clean_config(changes))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    return cfg


def fmt_num(v):
    """2.0 -> "2", 2.5 -> "2.5"."""
    return f"{v:g}"


def step(values, current, delta):
    """El valor de la lista que sigue a `current` hacia arriba (delta > 0) o hacia abajo; si `current` no está en
    la lista, el más cercano en esa dirección. En los extremos se queda donde está."""
    if delta > 0:
        return next((v for v in values if v > current + 1e-9), values[-1] if current < values[-1] else current)
    return next((v for v in reversed(values) if v < current - 1e-9), values[0] if current > values[0] else current)


# --------------------------------------------------------------------------- settings.json de Windows Terminal
def settings_candidates():
    la = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(la, "Packages", "Microsoft.WindowsTerminal_8wekyb3d8bbwe", "LocalState", "settings.json"),
        os.path.join(la, "Packages", "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe", "LocalState", "settings.json"),
        os.path.join(la, "Microsoft", "Windows Terminal", "settings.json"),
    ]


def find_settings():
    """Ruta del settings.json de Windows Terminal, o None."""
    return next((p for p in settings_candidates() if os.path.exists(p)), None)


def strip_jsonc(text):
    """Saca comentarios // y /* */ y comas colgantes, respetando strings."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    s = "".join(out)
    # comas colgantes (fuera de strings)
    out, in_str, i = [], False, 0
    while i < len(s):
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < len(s):
                out.append(s[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == ",":
            m = re.match(r",\s*([}\]])", s[i:])
            if not m:
                out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_settings(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(raw))


def read_settings(path):
    """(dict, texto original). Lanza SettingsError si no está o no se entiende."""
    if not path or not os.path.exists(path):
        raise SettingsError("No encontré el settings.json de Windows Terminal")
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:    # con sus fines de línea tal cual
            raw = f.read()
        return parse_settings(raw), raw
    except (OSError, ValueError) as e:
        raise SettingsError(f"No pude leer el settings.json de Windows Terminal ({e})")


_backed_up = set()      # settings.json que ya tienen copia en esta sesión (una copia por sesión, no una por cambio)


def write_settings(path, s, raw):
    """Escribe `s` con la misma sangría que tenía el archivo (como install.py: mismas claves; los comentarios
    // de un settings.json escrito a mano no se conservan). La primera vez en la sesión guarda una copia."""
    m = re.search(r"\n([ \t]+)\S", raw)
    indent = m.group(1) if m else 2
    if isinstance(indent, str) and indent.strip(" ") == "":
        indent = len(indent)
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = (json.dumps(s, indent=indent, ensure_ascii=False) + ("\n" if raw.endswith("\n") else "")).replace("\n", nl)
    try:
        if path not in _backed_up:
            shutil.copy2(path, f"{path}.bak-pokewalk-{time.strftime('%Y%m%d-%H%M%S')}")
            _backed_up.add(path)
        # en el lugar (si es un enlace a tus dotfiles, sigue siéndolo) y con sus mismos fines de línea
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        raise SettingsError(f"No pude escribir el settings.json de Windows Terminal ({e})")


def pokewalk_profile(s):
    profiles = s.get("profiles", {})
    plist = profiles.get("list", []) if isinstance(profiles, dict) else profiles
    return next((p for p in plist if isinstance(p, dict) and (p.get("guid") == GUID or p.get("name") == "Pokewalk")),
                None)


def pokewalk_split(s):
    """El splitPane del atajo de pokewalk (dentro de su multipleActions), o None."""
    for a in s.get("actions", []):
        if not isinstance(a, dict) or a.get("id") != ACTION_ID:
            continue
        cmd = a.get("command")
        subs = cmd.get("actions", []) if isinstance(cmd, dict) else []
        return next((x for x in subs if isinstance(x, dict) and x.get("action") == "splitPane"),
                    cmd if isinstance(cmd, dict) and cmd.get("action") == "splitPane" else None)
    return None


def wt_values(path):
    """(tamaño de letra del perfil Pokewalk, alto del panel) que dice settings.json (el alto: None si no está el
    atajo). Lanza SettingsError si no se puede leer."""
    s, _ = read_settings(path)
    size = None
    profiles = s.get("profiles")
    defaults = profiles.get("defaults", {}) if isinstance(profiles, dict) else {}
    for prof in (pokewalk_profile(s) or {}, defaults if isinstance(defaults, dict) else {}):
        font = prof.get("font") if isinstance(prof.get("font"), dict) else {}
        size = font.get("size", prof.get("fontSize"))
        if size is not None:
            break
    size = FONT_DEFAULT if size is None else size        # nadie lo cambió: el de Windows Terminal
    split = pokewalk_split(s) or {}
    panel = split.get("size")
    return (size if isinstance(size, (int, float)) else None,
            panel if isinstance(panel, (int, float)) else None)


def set_font_size(path, size):
    """Cambia `font.size` del perfil Pokewalk (Windows Terminal relee settings.json solo y se lo aplica al panel)."""
    s, raw = read_settings(path)
    prof = pokewalk_profile(s)
    if prof is None:
        raise SettingsError("No encontré el perfil Pokewalk en Windows Terminal (corré py install.py)")
    font = prof.get("font")
    if not isinstance(font, dict):
        font = prof["font"] = {}
    font["size"] = size
    prof.pop("fontSize", None)                       # la clave vieja ganaría o confundiría
    write_settings(path, s, raw)


def set_panel_size(path, frac):
    """Cambia el alto del panel que abre el atajo (fracción de la ventana): vale desde la próxima vez que se abre."""
    s, raw = read_settings(path)
    split = pokewalk_split(s)
    if split is None:
        raise SettingsError("No encontré el atajo de pokewalk en Windows Terminal (corré py install.py)")
    split["size"] = frac
    write_settings(path, s, raw)
