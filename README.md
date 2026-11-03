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
| `b` | **batalla** por equipos (abajo): `pika char` = 1 contra 1, `pika char vs mew snor` = equipos de hasta 6, Enter solo = 3 contra 3 al azar |
| `g` | **combates de gimnasio** (abajo): líderes, Alto Mando y campeón de Kanto y Johto |
| `i` | **info**: tipo, stats base, altura y peso de un Pokémon (Enter solo = uno que camina) |
| `h` | **mimar**: escribís a quién (nombre, número o el principio: `pika`; varios con espacio) y se alegra y te dice algo. Enter solo = uno al azar |

### La batalla

Como en los juegos, **sin objetos**: cada turno elegís **1 Luchar**, **2 Pokémon** (cambiar) o **3 Huir**.

| Tecla | Qué hace |
|---|---|
| `1` Luchar | elegís un ataque con `1`-`4` (muestra PP, tipo, poder y `x2` / `x½` / `x0` contra el rival). `0` o Esc vuelve |
| `2` Pokémon | cambiás de Pokémon con `1`-`6` (el cambio gasta el turno). Si se debilita el tuyo, el cambio es obligatorio |
| `3` Huir / `q` | te vas de la batalla |
| `a` | piloto automático: tu equipo también lo maneja la computadora (apretá `a` de nuevo para volver a mandar vos) |
| Enter | volver cuando termina |

**Qué hay:** equipos de hasta 6, PP, prioridad de ataques, estados (parálisis, quemadura, veneno, sueño, hielo,
confusión), subidas y bajadas de stats, curación, absorción, retroceso, golpes múltiples, amedrentar, críticos,
STAB y efectividad de tipos. Nivel 50; el HP va duplicado para que duren más de 2 golpes (`HP_MULT` en `pokebattle.py`).
**Qué no hay:** objetos, habilidades, clima, trampas ni dobles. Los ataques cuyo efecto no soportamos se descartan.

**Animaciones:** cada ataque usa las animaciones de PMDCollab del propio Pokémon: `Attack` (físicos, con embestida),
`Shoot` (a distancia), `Charge` (ataques de estado), `Hurt` al recibir daño, `Faint` al caer (queda tirado hasta que lo
reemplacen) y `Sleep` mientras está dormido. Los ataques especiales lanzan un proyectil y todos hacen un destello al
impactar, del color del tipo del ataque (la API no trae imágenes de ataques, esto se dibuja acá). Si un Pokémon no tiene
alguna animación, se usa una embestida simple. En batalla los sprites son un poco más grandes que caminando: si tu panel
es bajito, agrandalo con `py install.py --size 0.3`.

Los datos se buscan en [PokéAPI](https://pokeapi.co) **en el momento**: stats base, ataques con sus efectos (en español)
y la tabla de tipos. Hace falta internet la primera vez; después queda en caché (`%LOCALAPPDATA%\pokewalk\api`).
Cada Pokémon lleva hasta 3 ataques de daño y 1 de estado, sorteados entre los que puede aprender.

El rival **no usa IA**, son reglas (`Battle.ai_action` en `pokebattle.py`): casi siempre elige el ataque de mayor daño
esperado; a veces uno al azar; usa ataques de estado si le sirven (curarse con poca vida, subir stats, dormirte si no
tenés estado); y cambia de Pokémon si el suyo casi no te hace daño y le pegás fuerte, o cuando se debilita.

### Combates de gimnasio (`g`)

Menú con números: región (Kanto / Johto) → Gimnasios o Alto Mando y campeón → entrenador → vista previa de su equipo → Enter
para pelear (`0` vuelve, Esc cierra). Son 28 entrenadores: los 8 líderes de cada región, el Alto Mando y el campeón
(Blue en sus tres versiones, y Lance en Johto).

- **Tu equipo** son los Pokémon que caminan (hasta 6; si no hay, 3 al azar). Cambialos antes con `c` o `a`/`x`.
- **El rival** usa sus Pokémon y **niveles originales** de los juegos (Kanto: Rojo/Azul; Johto: Oro/Plata/Cristal).
- **Tus niveles** salen al azar: nivel medio del rival ±20 (entre 1 y 100) para cada uno. Algunos combates salen
  fáciles y otros duros. El nivel se ve en las barras (`Pikachu Nv32`) y la batalla lo tiene en cuenta (stats, HP, daño
  y ataques que puede aprender).
- Los ataques de todos se sortean de los que el Pokémon puede aprender a ese nivel, no son los de los juegos.
- Los equipos están en `gimnasios.json` (se pueden editar o agregar entrenadores con el mismo formato).

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
- Equipos de los líderes, Alto Mando y campeones (`gimnasios.json`): [Bulbapedia](https://bulbapedia.bulbagarden.net)
  (CC BY-NC-SA 2.5).
- Inspirado en [vscode-pokemon](https://github.com/jakobhoeg/vscode-pokemon).

Proyecto de fans sin fines de lucro. Pokémon y sus personajes son marcas de Nintendo, Creatures Inc. y GAME FREAK.
