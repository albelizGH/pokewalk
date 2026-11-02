#!/usr/bin/env python3
"""
Instala pokewalk en Windows Terminal:
  - instala Pillow
  - agrega el perfil "Pokewalk" (Sixel, fondo sólido igual al de tu tema)
  - agrega el atajo Alt+Shift+P que abre el panel abajo y te devuelve el foco arriba

Uso:   py install.py                 (se puede volver a correr para actualizar)
       py install.py --scale 1.5 --size 0.2 --keys alt+shift+k
Antes de tocar nada guarda una copia: settings.json.bak-pokewalk-<fecha>
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "pokewalk.py")
GUID = "{3f6b2c1e-8a4d-4e5f-9b7c-25a0c0ffee25}"
ACTION_ID = "User.Pokewalk"

# Fondos de los esquemas que vienen con Windows Terminal (no aparecen en settings.json)
BUILTIN_BG = {
    "campbell": "0C0C0C", "campbell powershell": "012456", "vintage": "000000",
    "one half dark": "282C34", "one half light": "FAFAFA", "solarized dark": "002B36",
    "solarized light": "FDF6E3", "tango dark": "000000", "tango light": "FFFFFF",
    "dark+": "1E1E1E", "cga": "000000", "ibm 5153": "000000",
}


def settings_candidates():
    la = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(la, "Packages", "Microsoft.WindowsTerminal_8wekyb3d8bbwe", "LocalState", "settings.json"),
        os.path.join(la, "Packages", "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe", "LocalState", "settings.json"),
        os.path.join(la, "Microsoft", "Windows Terminal", "settings.json"),
    ]


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
    ap.add_argument("--scale", default="2", help="tamaño de los Pokémon (default 2)")
    ap.add_argument("--size", type=float, default=0.22, help="alto del panel, fracción de la ventana (default 0.22)")
    ap.add_argument("--keys", default="alt+shift+p", help="atajo de teclado (default alt+shift+p)")
    ap.add_argument("--settings", help="ruta a settings.json si no lo encuentra solo")
    ap.add_argument("--no-pip", action="store_true", help="no instalar Pillow")
    ap.add_argument("--no-hook", action="store_true",
                    help="no tocar el perfil de PowerShell (los Pokémon no reaccionan a tus comandos)")
    ap.add_argument("--sleep-after", default="5", help="minutos sin comandos para que se duerman (default 5, 0 = nunca)")
    args = ap.parse_args()

    if not args.no_pip:
        print("Instalando Pillow...")
        subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "pillow"])

    path = args.settings or next((p for p in settings_candidates() if os.path.exists(p)), None)
    if not path:
        sys.exit("No encontré el settings.json de Windows Terminal. Abrilo una vez o pasá --settings <ruta>.")
    print("Configuración:", path)

    raw = open(path, encoding="utf-8-sig").read()
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        s = json.loads(strip_jsonc(raw))

    backup = f"{path}.bak-pokewalk-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    print("Copia de seguridad:", backup)

    bg, scheme = scheme_background(s)
    cmd = f'"{sys.executable}" "{SCRIPT}" --sixel --bg {bg} --scale {args.scale} --sleep-after {args.sleep_after}'

    profiles = s.setdefault("profiles", {})
    if isinstance(profiles, list):                    # formato viejo
        profiles = s["profiles"] = {"list": profiles}
    plist = profiles.setdefault("list", [])
    plist[:] = [p for p in plist if p.get("guid") != GUID and p.get("name") != "Pokewalk"]
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
    plist.append(prof)

    actions = s.setdefault("actions", [])
    actions[:] = [a for a in actions if a.get("id") != ACTION_ID]
    action = {
        "id": ACTION_ID,
        "name": "Pokémon caminando (panel abajo)",
        "command": {"action": "multipleActions", "actions": [
            {"action": "splitPane", "split": "down", "size": args.size, "profile": "Pokewalk"},
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

    if not args.no_hook:
        install_hook()

    print(f"\nListo. Apretá {keys.upper()} en Windows Terminal (si no anda, cerralo y abrilo).")
    print(f"Fondo usado: #{bg} ({scheme}). Necesitás Windows Terminal 1.22+ para Sixel.")


if __name__ == "__main__":
    main()
