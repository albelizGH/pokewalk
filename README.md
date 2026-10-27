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
o restaurá la copia `settings.json.bak-pokewalk-*`. El caché de sprites está en `%LOCALAPPDATA%\pokewalk`.

## Créditos

- Sprites: [PMDCollab/SpriteCollab](https://github.com/PMDCollab/SpriteCollab) (se descargan al usarlo, no están en este repo).
- Nombres y números: [PokéAPI](https://github.com/PokeAPI/pokeapi).
- Inspirado en [vscode-pokemon](https://github.com/jakobhoeg/vscode-pokemon).

Proyecto de fans sin fines de lucro. Pokémon y sus personajes son marcas de Nintendo, Creatures Inc. y GAME FREAK.
