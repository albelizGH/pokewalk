#!/usr/bin/env python3
"""
Instala pokewalk en Windows Terminal:
  - instala Pillow
  - agrega el perfil "Pokewalk" (Sixel, fondo sólido igual al de tu tema)
  - agrega el atajo Alt+Shift+P que abre el panel abajo y te devuelve el foco arriba

Uso:   py install.py                 (se puede volver a correr para actualizar)
       py install.py --scale 1.5 --size 0.2 --font-size 10 --keys alt+shift+k
Antes de tocar nada guarda una copia: settings.json.bak-pokewalk-<fecha>
Las opciones que pasás se guardan también en la configuración del juego (tecla o); las que no pasás
quedan como las elegiste ahí.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from pokeconfig import ACTION_ID, CONFIG_FILE, GUID, fmt_num, load_config, parse_settings, settings_candidates, \
    update_config

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "pokewalk.py")
DEFAULTS = {"scale": 2.0, "panel": 0.22, "sleep_after": 5.0}

# Fondos de los esquemas que vienen con Windows Terminal (no aparecen en settings.json)
BUILTIN_BG = {
    "campbell": "0C0C0C", "campbell powershell": "012456", "vintage": "000000",
    "one half dark": "282C34", "one half light": "FAFAFA", "solarized dark": "002B36",
    "solarized light": "FDF6E3", "tango dark": "000000", "tango light": "FFFFFF",
    "dark+": "1E1E1E", "cga": "000000", "ibm 5153": "000000",
}


def scheme_background(s):
    d = s.get("profiles", {})
    defaults = d.get("defaults", {}) if isinstance(d, dict) else {}
    if defaults.get("background"):
        return defaults["background"].lstrip("#"), defaults.get("colorScheme")
    name = defaults.get("colorScheme") or "Campbell"
    if isinstance(name, dict):                       # {"dark": "...", "light": "..."}
        name = name.get("dark") or next(iter(name.values()))
    for sc in s.get("schemes", []):
        if sc.get("name", "").lower() == name.lower() and sc.get("background"):
            return sc["background"].lstrip("#"), name
    return BUILTIN_BG.get(name.lower(), "0C0C0C"), name


HOOK = os.path.join(HERE, "pokewalk-hook.ps1")
HOOK_MARK = "# pokewalk-hook"


def powershell_profiles():
    """Perfiles de PowerShell donde cargar el hook: pwsh (se crea si falta) y Windows
    PowerShell 5.1 (solo si ya tiene perfil: no le creamos uno a quien no lo usa)."""
    found = []
    for exe, create in (("pwsh", True), ("powershell", False)):
        if not shutil.which(exe):
            continue
        try:
            out = subprocess.check_output([exe, "-NoProfile", "-Command", "$PROFILE"], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            continue
        if out and (create or os.path.exists(out)):
            found.append(out)
    return found


def install_hook():
    """Agrega al final del perfil la línea que carga pokewalk-hook.ps1 (al final, para
    que envuelva el prompt de Oh My Posh/starship y no al revés)."""
    line = f'. "{HOOK}"   {HOOK_MARK}'
    for prof in powershell_profiles():
        text, enc = "", "utf-8"
        if os.path.exists(prof):
            with open(prof, "rb") as f:
                raw = f.read()
            enc = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
            text = raw.decode(enc)
            shutil.copy2(prof, f"{prof}.bak-pokewalk-{time.strftime('%Y%m%d-%H%M%S')}")
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines(keepends=True)
        mine = [i for i, l in enumerate(lines) if HOOK_MARK in l]
        if mine:                                       # ya estaba: actualizar la ruta
            lines[mine[0]] = line + nl
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += nl
            lines += [nl, line + nl] if lines else [line + nl]
        os.makedirs(os.path.dirname(prof), exist_ok=True)
        with open(prof, "w", encoding=enc, newline="") as f:
            f.write("".join(lines))
        print("Hook de PowerShell cargado en:", prof)


def main():
    ap = argparse.ArgumentParser(description="Instala pokewalk en Windows Terminal")
    # Sin --scale / --size / --sleep-after se usa lo que elegiste en el juego (tecla o, config.json) o el default;
    # lo que pasás acá también se guarda en config.json, así el juego y la instalación dicen lo mismo.
    ap.add_argument("--scale", type=float, help="tamaño de los Pokémon (default 2)")
    ap.add_argument("--size", type=float, help="alto del panel, fracción de la ventana (default 0.22)")
    ap.add_argument("--font-size", type=int, help="tamaño de letra del panel (default: el de tu terminal)")
    ap.add_argument("--keys", default="alt+shift+p", help="atajo de teclado (default alt+shift+p)")
    ap.add_argument("--settings", help="ruta a settings.json si no lo encuentra solo")
    ap.add_argument("--config", default=CONFIG_FILE, help=argparse.SUPPRESS)     # para probar con una copia
    ap.add_argument("--no-pip", action="store_true", help="no instalar Pillow")
    ap.add_argument("--no-hook", action="store_true",
                    help="no tocar el perfil de PowerShell (los Pokémon no reaccionan a tus comandos)")
    ap.add_argument("--sleep-after", type=float, help="minutos sin comandos para que se duerman (default 5, 0 = nunca)")
    args = ap.parse_args()
    chosen = {k: v for k, v in (("scale", args.scale), ("panel", args.size), ("sleep_after", args.sleep_after),
                                ("font_size", args.font_size)) if v is not None}
    opts = {**DEFAULTS, **load_config(args.config), **chosen}

    if not args.no_pip:
        print("Instalando Pillow...")
        subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "pillow"])

    path = args.settings or next((p for p in settings_candidates() if os.path.exists(p)), None)
    if not path:
        sys.exit("No encontré el settings.json de Windows Terminal. Abrilo una vez o pasá --settings <ruta>.")
    print("Configuración:", path)

    raw = open(path, encoding="utf-8-sig").read()
    s = parse_settings(raw)

    backup = f"{path}.bak-pokewalk-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    print("Copia de seguridad:", backup)

    bg, scheme = scheme_background(s)
    cmd = (f'"{sys.executable}" "{SCRIPT}" --sixel --bg {bg} --scale {fmt_num(opts["scale"])}'
           f' --sleep-after {fmt_num(opts["sleep_after"])}')

    profiles = s.setdefault("profiles", {})
    if isinstance(profiles, list):                    # formato viejo
        profiles = s["profiles"] = {"list": profiles}
    plist = profiles.setdefault("list", [])
    old = [p for p in plist if p.get("guid") == GUID or p.get("name") == "Pokewalk"]
    font = next((p["font"] for p in old if isinstance(p.get("font"), dict)), None)   # la letra que elegiste (tecla o)
    plist[:] = [p for p in plist if p not in old]
    prof = {
        "guid": GUID,
        "name": "Pokewalk",
        "icon": "⚡",
        "hidden": False,
        "commandline": cmd,
        "padding": "0, 0, 0, 0",
        "scrollbarState": "hidden",
        "closeOnExit": "always",
        "suppressApplicationTitle": True,
        "tabTitle": "Pokewalk",
        "opacity": 100,
        "useAcrylic": False,
    }
    if scheme and isinstance(scheme, str):
        prof["colorScheme"] = scheme
    if "font_size" in opts:
        font = {**(font or {}), "size": opts["font_size"]}
    if font:
        prof["font"] = font
    plist.append(prof)

    actions = s.setdefault("actions", [])
    actions[:] = [a for a in actions if a.get("id") != ACTION_ID]
    action = {
        "id": ACTION_ID,
        "name": "Pokémon caminando (panel abajo)",
        "command": {"action": "multipleActions", "actions": [
            {"action": "splitPane", "split": "down", "size": opts["panel"], "profile": "Pokewalk"},
            {"action": "moveFocus", "direction": "up"}]},
    }
    keys = args.keys.lower()
    if "keybindings" in s or not any("keys" in a for a in actions):
        # formato nuevo (Windows Terminal 1.21+): acciones y atajos separados
        kb = s.setdefault("keybindings", [])
        clash = [k for k in kb if k.get("keys", "").lower() == keys and k.get("id") != ACTION_ID]
        kb[:] = [k for k in kb if k.get("id") != ACTION_ID]
        kb.append({"id": ACTION_ID, "keys": keys})
    else:
        clash = [a for a in actions if str(a.get("keys", "")).lower() == keys]
        action["keys"] = keys
    actions.append(action)
    if clash:
        print(f"Ojo: {keys} ya estaba usado por otra acción; puede que gane la otra. Probá --keys alt+shift+k")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    if chosen:
        try:
            update_config(chosen, args.config)
        except OSError as e:
            print("Ojo: no pude guardar las opciones en", args.config, f"({e})")

    if not args.no_hook:
        install_hook()

    print(f"\nListo. Apretá {keys.upper()} en Windows Terminal (si no anda, cerralo y abrilo).")
    print(f"Fondo usado: #{bg} ({scheme}). Necesitás Windows Terminal 1.22+ para Sixel.")


if __name__ == "__main__":
    main()
