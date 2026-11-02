# pokewalk ⚡

Pokémon caminando en la terminal, estilo la extensión **vscode-pokemon**, pero para cualquier terminal.
Viven en un panel chiquito abajo de Windows Terminal mientras vos laburás en el de arriba.

- Animación de caminata de verdad (sprites de Pokémon Mystery Dungeon), van y vienen y se paran a descansar.
- Agregás, sacás o cambiás Pokémon en vivo desde el mismo panel.
- Se acuerda de los últimos que elegiste.
- Modo **Sixel**: Pokémon chiquitos y nítidos con el texto de tamaño normal (Windows Terminal 1.22+).
- Modo bloques (`▀▄`) para terminales sin Sixel.

## Instalación (Windows + Windows Terminal)

Necesitás Python 3.8+ y Windows Terminal 1.22 o más nuevo.

```powershell
git clone https://github.com/albelizGH/pokewalk.git $HOME\pokewalk
cd $HOME\pokewalk
py install.py
```

Listo: apretá **`Alt+Shift+P`** en Windows Terminal. Si no aparece, cerrá y abrí la terminal.

`install.py` instala Pillow, agrega el perfil **Pokewalk** y el atajo a tu `settings.json`
(antes guarda una copia `settings.json.bak-pokewalk-<fecha>`). Se puede volver a correr para cambiar opciones:

```powershell
py install.py --scale 1.5          # tamaño de los Pokémon (default 2; los enteros se ven más nítidos)
py install.py --size 0.18          # alto del panel (fracción de la ventana, default 0.22)
py install.py --keys alt+shift+k   # otro atajo
```

## Teclas (con el panel de los Pokémon seleccionado)

| Tecla | Qué hace |
|---|---|
| `a` | agregar: nombre o número + Enter |
| `c` | cambiar todos: varios separados por espacio + Enter |
| `x` | sacar: nombre, número o el principio del nombre (`pika`) + Enter. Enter solo saca el último |
| `r` | agregar uno al azar |
| `q` | salir |
| `Esc` | cancelar lo que estás escribiendo |

Los nombres van en inglés (`charmander`, `bulbasaur`) o por número de Pokédex.

## Reaccionan a tus comandos (solo PowerShell)

`install.py` agrega una línea al final de tu perfil de PowerShell que carga `pokewalk-hook.ps1`.
Ese hook avisa a pokewalk de cada comando que terminás (comando, si salió bien, cuánto tardó) y los
Pokémon reaccionan con un globo de texto, sin IA, con reglas simples:

- **Falla un comando** → se asustan y salen corriendo. A los 3 errores seguidos se preocupan; a los 5 te piden que respires.
- **Tests, `git commit`, `git push`** → comentan; si pasan tests o algo largo (+30 s) termina, festejan.
- **Después de varios errores, algo anda** → "¡Por fin anduvo!".
- **`rm`/`del`** → "¡Cuidado!".
- **Sin actividad** (default 5 min, `--sleep-after MIN`, `0` = nunca) → se duermen (`Zzz...`), pero cada tanto
  se despiertan a estirarse un rato y vuelven a dormirse. Cualquier comando o tecla los despierta.

El hook envuelve `prompt` sin alterar `$?` ni `$LASTEXITCODE`, así que convive con Oh My Posh / starship
(por eso tiene que ir al final del perfil). Solo escribe mientras pokewalk está abierto. Si no lo querés:
`py install.py --no-hook`.

## Batallas, info y mimos

| Tecla | Qué hace |
|---|---|
| `b` | **batalla**: escribís tu Pokémon y el rival (`pikachu charmander`). Si falta alguno, lo completa con los que caminan o al azar |
| `i` | **info**: tipo, stats base, altura y peso de un Pokémon (Enter solo = uno que camina) |
| `h` | **mimar**: uno de los que caminan se alegra y te dice algo |

En la batalla controlás al primero con `1`-`4` (ataques, con `x2` / `x½` / `x0` si el rival es débil, resiste o es inmune), `a` alterna piloto
automático (se pelean solos), `q` huye, Enter vuelve cuando termina.

Los datos se buscan en [PokéAPI](https://pokeapi.co) **en el momento**: stats base, 4 ataques que el Pokémon puede aprender
(en español, con poder y precisión) y la tabla de tipos. Hace falta internet la primera vez; después queda en caché
(`%LOCALAPPDATA%\pokewalk\api`). Nivel 50, fórmula de daño de los juegos (STAB, efectividad, críticos, variación),
HP duplicado para que duren más de 2 golpes. Solo ataques que hacen daño: sin estados, clima ni objetos.

El rival **no usa IA**: casi siempre elige el ataque de mayor daño esperado (la misma fórmula) y a veces uno al azar
(`Battle.ai_move` en `pokebattle.py`).

## Uso directo (cualquier terminal)

```bash
pip install pillow
python pokewalk.py pikachu charmander squirtle      # modo bloques
python pokewalk.py --sixel --bg 181818 --scale 2   # modo Sixel (bg = color de fondo de tu terminal)
python pokewalk.py --random 3
```

En Linux/macOS con tmux:

```bash
tmux split-window -v -l 8 'python3 ~/pokewalk/pokewalk.py --sixel'
```

## Desinstalar

Borrá el perfil **Pokewalk** desde la configuración de Windows Terminal (y la acción "Pokémon caminando"),
o restaurá la copia `settings.json.bak-pokewalk-*`. Borrá también la línea `# pokewalk-hook` al final de tu perfil
de PowerShell (`$PROFILE`). El caché de sprites está en `%LOCALAPPDATA%\pokewalk`.

## Créditos

- Sprites: [PMDCollab/SpriteCollab](https://github.com/PMDCollab/SpriteCollab) (se descargan al usarlo, no están en este repo).
- Nombres y números: [PokéAPI](https://github.com/PokeAPI/pokeapi).
- Inspirado en [vscode-pokemon](https://github.com/jakobhoeg/vscode-pokemon).

Proyecto de fans sin fines de lucro. Pokémon y sus personajes son marcas de Nintendo, Creatures Inc. y GAME FREAK.
