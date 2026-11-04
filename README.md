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
| `a` | agregar: nombre, número o rango (`1-10`, máximo 10 por vez) + Enter. `Tab` (o Enter solo) abre una lista por región (Kanto, Johto...): cada uno aparece con su número de Pokédex: tipeá el número (o parte del nombre) y Enter, `←` `→` cambian de página, Backspace vuelve. La lista con `Tab` también anda al cambiar todos (`c`), en batallas (`b`), info (`i`) y al armar el equipo del versus: lo que elegís se escribe en el prompt |
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
| `v` | **versus contra un amigo** en la misma red (abajo, "Versus contra un amigo"), o un **torneo** de 3 a 8 (abajo, "Torneo") |
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

### Versus contra un amigo en la misma red (`v`)

Los dos tienen que tener pokewalk (la misma versión) y estar en la misma red WiFi / LAN. No hace falta instalar nada más.

1. Uno aprieta **`v`** y elige **1 Crear sala**, y fija **las reglas de la sala**: de cuántos Pokémon son los equipos
   (`1`-`6`), el nivel (1-100, Enter solo = 50) y si entran **legendarios** y **míticos** (`1` todos, `2` sin
   legendarios, `3` sin míticos, `4` sin ninguno de los dos; Enter = todos). El otro aprieta **`v`** y elige **2 Buscar salas**: pokewalk prueba
   los equipos de tu red y la sala aparece sola en la lista, con sus reglas (también escucha los avisos UDP de las
   salas). La elige con su número. Si no aparece (por ejemplo en un Wi-Fi que aísla a los equipos entre sí), **3 / `i`
   Conectar por IP**: se escribe la IP que muestra el que creó la sala. Al entrar, los dos ven las reglas
   (`Sala: 3 Pokémon · nivel 50 · sin legendarios`).
   - **Legendarios**: los de la historia, uno por juego (Mewtwo, Lugia, Rayquaza, Dialga...).
   - **Míticos**: los de evento (Mew, Celebi, Jirachi, Arceus...).
   Salen de PokeAPI (`is_legendary` / `is_mythical`). Los ultraentes y los Pokémon paradoja no vienen marcados ahí, así
   que entran siempre. En la lista por región (Tab) los legendarios llevan `★` y los míticos `✦`; si la sala no los
   deja, salen en gris.
2. **Cada uno arma su equipo en la sala**: hasta el tamaño de la sala, solo nombres o números (`pika char`; el nivel lo
   fija la sala, así que `pika:60` da error; Enter solo = los que caminan, hasta el tamaño de la sala; si no camina
   ninguno, al azar entre los que la sala permite). Un legendario o mítico que la sala no deja da error y se corrige
   ahí mismo (`Mewtwo es legendario: esta sala no los permite`), también si es uno de los que caminan. El que creó la
   sala además rechaza un equipo que no cumpla las reglas. **Handicap**:
   cada Pokémon queda al nivel de la sala ±5 al azar (entre 1 y 100). A nivel 50, ±5 es ~10% de stats: se nota, pero
   no decide la pelea (con ±10 podría ser 40 contra 60, ~50% más). La revancha sigue con las mismas reglas.
   Después **elegís los ataques de cada uno** entre todos los que puede usar a su nivel (hasta 4):

   | Tecla | Qué hace |
   |---|---|
   | `1`-`9` | marcar / desmarcar ese ataque de la página (`*` = elegido; muestra tipo y poder) |
   | `←` `→` | cambiar de página |
   | `r` | cuatro ataques al azar |
   | Enter | confirmar este Pokémon y pasar al siguiente (arranca con un set sorteado marcado: Enter lo deja así) |

3. Cuando los dos terminan, arranca la batalla. Se juega con las mismas teclas que la batalla normal (sin piloto
   automático). Cada turno los dos eligen; el turno se resuelve cuando contestaron ambos.

4. **Revancha:** al terminar, si los dos aprietan **`R`** se vuelve a la sala con la misma conexión (sin buscar la sala
   ni escribir IPs). Ahí **Enter solo repite el mismo equipo y los mismos ataques** (con la vida y los PP completos), o
   se arma otro equipo como la primera vez. Enter sale de la sala.

El que creó la sala hace de árbitro (resuelve cada turno y le manda el resultado al otro), así no se desincroniza.
Si alguien huye (`3` / `q`) o se cae la conexión, el otro lo ve y gana. Es para jugar entre amigos: los equipos los arma
cada uno, así que nada impide hacer trampa a propósito.

Si es la primera vez, el firewall de Windows va a preguntar si dejás a Python usar la red: aceptá en **redes privadas**.
La sala usa el puerto TCP 5757 y los avisos van por UDP 5758.

### Torneo en la misma red (`v` → 4)

Para jugar entre 3 y 8 en la misma red. Por ahora está **la sala del torneo**: el cuadro y las peleas llegan en la
próxima versión.

1. El organizador aprieta **`v`**, elige **4 Crear torneo** y fija las reglas como en una sala de versus (de cuántos
   Pokémon son los equipos, el nivel y si entran legendarios / míticos).
2. Los demás aprietan **`v`** → **2 Buscar salas**: el torneo aparece aparte, con cuántos hay
   (`Torneo de Ana (192.168.1.5) · 3/8 · 3 Pokémon · nivel 50`). Se elige con su número. También se puede entrar con
   **3 Conectar por IP** (la IP del organizador): pokewalk se da cuenta solo de que es un torneo.
3. En la sala todos ven la lista de jugadores y las reglas. Si alguien se va o se le corta la conexión, sale de la lista.
   Esc sale del torneo (si es el organizador, lo cierra para todos).
4. Con 3 o más, el organizador aprieta **Enter** para empezar: la sala se cierra (ya no entra nadie más) y todos
   reciben la lista final de jugadores.

Todos se conectan solo a la compu del organizador, así que tiene que poder recibir conexiones (el mismo permiso de
Windows que para crear una sala de versus). Hace falta la misma versión de pokewalk en todas las compus.

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
