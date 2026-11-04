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
| `a` | agregar: nombre, número o rango (`1-10`, máximo 10 por vez) + Enter. `Tab` (o Enter solo) abre una lista por región (Kanto, Johto...): cada uno aparece con su número de Pokédex: tipeá el número (o parte del nombre) y Enter, `←` `→` cambian de página, Backspace vuelve. Un rango como `257-260` muestra justo esos números (de cualquier región) y Enter los escribe todos (máximo 10). La lista con `Tab` también anda al cambiar todos (`c`), en batallas (`b`), info (`i`) y al armar el equipo del versus: lo que elegís se escribe en el prompt y, salvo en info, la lista sigue abierta para elegir otro (Esc vuelve al prompt). Mientras escribís en `a`/`c`, abajo se ve qué se va a agregar (`¿pikahcu?` = no lo conozco) |
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
| `b` | **batalla** por equipos (abajo): `pika char` = 1 contra 1, `pika char vs mew snor` = equipos de hasta 6, Enter solo = 3 contra 3 al azar; en la lista (`Tab`), `Tab` escribe el `vs` y lo que sigue es el rival |
| `v` | **versus contra un amigo** en la misma red (abajo, "Versus contra un amigo"), o un **torneo** de 3 a 8 (abajo, "Torneo"); con **5**, lo mismo **por internet** con un código (abajo, "Jugar por internet") |
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

Para jugar entre 3 y 8 en la misma red: **eliminación directa**, una pelea por vez, y todos los que no pelean la miran
en vivo (el organizador también juega).

1. El organizador aprieta **`v`**, elige **4 Crear torneo** y fija las reglas como en una sala de versus (de cuántos
   Pokémon son los equipos, el nivel y si entran legendarios / míticos).
2. Los demás aprietan **`v`** → **2 Buscar salas**: el torneo aparece aparte, con cuántos hay
   (`Torneo de Ana (192.168.1.5) · 3/8 · 3 Pokémon · nivel 50`). Se elige con su número. También se puede entrar con
   **3 Conectar por IP** (la IP del organizador): pokewalk se da cuenta solo de que es un torneo.
3. En la sala todos ven la lista de jugadores y las reglas. Si alguien se va o se le corta la conexión, sale de la lista.
   Esc sale del torneo (si es el organizador, lo cierra para todos).
4. Con 3 o más, el organizador aprieta **Enter** para empezar: la sala se cierra (ya no entra nadie más) y se sortea
   **el cuadro**, que ven todos: las rondas (`Cuartos`, `Semi`, `Final`), quién pasó, la pelea en curso resaltada
   (`▶ Ana vs Beto`) y quién se fue. Si no son 4 u 8, a algunos les toca **pasar directo** (bye) a la 2ª ronda: solo
   en la 1ª ronda y nunca dos byes en la misma pelea (con 5: uno pelea en la 1ª ronda y tres pasan directo).
5. El organizador aprieta **Enter** otra vez para la primera pelea; las siguientes arrancan solas unos 12 s después
   de terminada la anterior (en ese rato se ve el cuadro con el resultado y cuánto falta).
6. **Armar el equipo: 60 s.** A los dos que pelean les aparece el prompt del equipo de siempre (con las reglas de la
   sala y los segundos que quedan) y después los ataques. Si se acaba el tiempo: vale lo que estaba escrito que sirva
   (nombres o números que existen y la sala permite; lo que no, se descarta) y **el resto se completa al azar** con
   Pokémon permitidos; si estabas eligiendo ataques, quedan los sorteados. Nada escrito = todo al azar (los que
   caminan no cuentan si no apretaste Enter). El reloj lo lleva el organizador: si el equipo igual no llega (unos 20 s
   más), él mismo te arma uno al azar.
7. **En la pelea: 60 s por decisión** (más lo que tarda en verse el turno anterior). En los últimos 15 s aparece la
   cuenta regresiva (`Te quedan 12 s para elegir`, y para los que miran `Ana: 12 s para elegir`). Si se acaba,
   **perdés el turno**: tu Pokémon no hace nada y el rival juega igual (si tenías que elegir reemplazo, entra el
   primero que puede pelear). **3 turnos perdidos seguidos = perdés la pelea**; elegir algo vuelve la cuenta a 0. No
   hay piloto automático: la máquina no juega por nadie.
8. Todos los demás **miran la pelea en vivo**: la misma pantalla de batalla, sin menús, con `Mirando: Ana vs Beto` y
   lo último que pasó. El organizador arbitra todas las peleas en su compu (las juegue o no): cada uno manda solo su
   jugada y él resuelve el turno y se lo manda a todos.
9. Abandonar (Esc en el equipo, huir en la batalla) o **desconectarse = perder la pelea y quedar afuera** del
   torneo: si te tocaba pelear más adelante, tu rival **pasa sin pelear**. Si el organizador cierra pokewalk o sale
   con Esc, **el torneo se termina para todos** (`Ana cerró el torneo: se terminó para todos.`).
10. **Sin ganador** (los dos se debilitan a la vez, los dos pierden 3 turnos seguidos, o no se pudo preparar la pelea):
    se repite **una sola vez** con los mismos equipos; si otra vez no hay ganador, **una moneda** elige quién pasa y
    se avisa a todos.
11. Al final, todos ven **`¡Ana es el campeón del torneo!`** con el cuadro completo; Esc vuelve a lo de siempre.

Todos se conectan solo a la compu del organizador, así que tiene que poder recibir conexiones (el mismo permiso de
Windows que para crear una sala de versus). Hace falta la misma versión de pokewalk en todas las compus.

### Jugar por internet (sin instalar nada) (`v` → 5)

Para jugar con amigos que **no están en tu red** (cada uno en su casa), versus 1 contra 1 o torneo, sin instalar nada
más ni tocar el router: los mensajes pasan por un servidor público y gratuito.

1. El que crea aprieta **`v`** → **5 Por internet** → **1 Crear sala** o **2 Crear torneo**, y fija las reglas igual
   que en la red local (tamaño de los equipos, nivel, legendarios / míticos). Al rato (unos segundos: se conecta al
   servidor) aparece **el código de la sala**, por ejemplo **`PIKA-7F3K-92QD`**: se lo pasás a tus amigos por
   WhatsApp o lo que sea.
2. Los demás aprietan **`v`** → **5 Por internet** → **3 Unirse con un código** y lo escriben. Da igual mayúsculas,
   minúsculas, guiones o espacios (`pika7f3k92qd` sirve), y la `O` vale como `0` y la `I` / `L` como `1`. Si el código
   está mal o la sala ya se cerró: `Nadie contestó en la sala ...`. Pokewalk se da cuenta solo de si es un 1 contra 1
   o un torneo.
3. De ahí en adelante es **igual que en la red local**: la sala, el equipo, los ataques, la batalla, la revancha, el
   cuadro del torneo, los relojes y los que miran. El que creó la sala sigue siendo el árbitro. Si la sala de 1 contra
   1 ya tiene rival o el torneo ya empezó, el que llega tarde ve `La sala está llena.` / `El torneo ya empezó.`.

Cosas a saber:
- **Servidor:** `broker.hivemq.com` (MQTT sobre TLS, puerto 8883: una conexión saliente común, como abrir una web). Si
  no anda, la sala se abre sola en el de respaldo, `broker.emqx.io`, y el código empieza con `EEVE-` en vez de
  `PIKA-` (el código le dice a tus amigos a qué servidor ir). Es un servicio gratuito de terceros: **puede estar caído
  o lento un rato**; en ese caso pokewalk lo dice (`No pude conectarme al servidor de internet (broker.hivemq.com)...`)
  y hay que probar más tarde. El de respaldo perdió mensajes en ráfagas en nuestras pruebas: anda, pero es "lo que
  haya". Conectarse puede tardar hasta unos 10 s.
- **Demora:** cada mensaje tarda **~0,3-0,4 s** en llegar (va y vuelve por el servidor, que está en Europa), así que
  cada turno se ve un poquito después que en la red local. Los relojes del torneo (60 s) sobran.
- **Caídas:** como no hay una conexión directa, cada compu manda un latido cada pocos segundos; **15 s sin noticias de
  alguien = se desconectó** (pierde la pelea, como en la red local). Si se corta tu internet un momento, pokewalk
  reintenta conectarse solo durante esos 15 s.
- **Privacidad:** el código **no viaja**: de él salen el "canal" del servidor y una clave con la que **se firma cada
  mensaje** (HMAC-SHA256); los mensajes sin firma, con otra clave o repetidos se descartan, así que nadie sin el código
  puede meter jugadas ni hacerse pasar por la sala. Lo que **no** hay es cifrado: alguien mirando ese servidor público
  podría **leer** lo que pasa en la partida (nombres, equipos, jugadas). No mandes nada privado por ahí. Adivinar un
  código (40 bits al azar, con una derivación lenta a propósito) no es práctico; igual, cada sala tiene uno nuevo.
- Entre los que tienen el código se confía como en la red local (es para jugar con amigos).

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
