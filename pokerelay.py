"""
pokerelay - versus y torneos por internet sin instalar nada ni tocar el router.

Cómo funciona:
  * Todos se conectan (conexión SALIENTE, TLS por el puerto 8883) a un servidor MQTT público y gratuito
    (broker.hivemq.com; si no anda, broker.emqx.io). Nadie abre puertos: el servidor reparte los mensajes.
  * Quien crea la sala recibe un código (ej. PIKA-7F3K-92QD: 8 caracteres al azar = 40 bits, más el prefijo que
    dice en qué servidor está la sala). Del código salen, con PBKDF2 (lento a propósito, para que adivinar
    códigos cueste caro), el tema (topic) donde se habla y la clave con que se firma cada mensaje (HMAC-SHA256).
    El código no viaja nunca.
  * Todos comparten un solo tema: cada mensaje lleva quién lo manda (`f`), un número de orden por remitente (`s`,
    para descartar repetidos y reenvíos viejos) y a quién va cada parte (`to`: ids o "*" = todos). El que creó
    la sala tiene el id "h" (es el árbitro, como en la red local: estrella); los invitados solo escuchan lo que
    manda "h" para ellos o para todos.
  * `RelayLink` se porta como `pokenet.Link` (send / poll / close / alive, el {"t": "_closed"}), así la sala, el
    torneo y las peleas andan igual que en la red local. Como no hay una conexión TCP con cada uno, la caída se
    detecta por latidos: DEAD_AFTER s sin noticias = se cortó. Cerrar le avisa al otro.

Límite aceptado: los mensajes no van cifrados (la biblioteca estándar de Python no trae AES). Alguien que esté
mirando ese servidor público podría leer las jugadas y los nombres; lo que no puede es meter mensajes falsos ni
hacerse pasar por la sala sin el código.
"""
import hashlib
import hmac
import json
import queue
import secrets
import select
import socket
import ssl
import struct
import threading
import time

# (servidor, puerto TLS, prefijo del código). El primero que conecte es donde se abre la sala; el prefijo del
# código le dice al invitado a cuál ir. broker.emqx.io perdió mensajes en ráfagas de 50/s en las pruebas: queda
# de respaldo (mandamos con pausa y en lotes, así que debería alcanzar).
BROKERS = [("broker.hivemq.com", 8883, "PIKA"), ("broker.emqx.io", 8883, "EEVE")]
TOPIC_ROOT = "pokewalk/r1/"
SALT = b"pokewalk-relay-1"
KDF_ROUNDS = 200_000       # PBKDF2: ~0,1 s una vez por sala; adivinar códigos por fuerza bruta se vuelve carísimo
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford: sin I, L, O, U (al leer: O = 0, I y L = 1)
CODE_CHARS = 8             # 8 x 5 bits = 40 bits al azar
HOST = "h"                 # id del que creó la sala (el árbitro)
CONNECT_TIMEOUT = 15.0     # s para conectar con un servidor (hivemq a veces tarda ~7 s en aceptar la conexión)
KEEPALIVE = 60             # s (MQTT); igual mandamos latidos mucho más seguido
GAP = 0.05                 # s mínimos entre dos publicaciones de esta compu (lo que llegue en el medio va en lote)
BATCH_MAX = 16_000         # bytes por publicación (un mensaje más grande va solo)
MAX_PACKET = 1 << 20       # tope de un paquete que aceptamos del servidor
PING_EVERY = 4.0           # s sin mandar nada para mandar un latido
DEAD_AFTER = 15.0          # s sin noticias de alguien (o del servidor) para darlo por perdido
ANSWER_SECS = 12.0         # s que espera un invitado a que la sala le conteste el saludo
CLOSE_WAIT = 1.0           # s que se espera, al cerrar, a que salgan los últimos mensajes


class RelayError(Exception):
    """No se pudo conectar: el texto ya está listo para mostrarlo."""


# --------------------------------------------------------------------------- el código de la sala
def new_code(broker=0, rng=secrets):
    """Código nuevo para una sala en BROKERS[broker]: "PIKA-7F3K-92QD"."""
    body = "".join(rng.choice(ALPHABET) for _ in range(CODE_CHARS))
    return f"{BROKERS[broker][2]}-{body[:4]}-{body[4:]}"


def parse_code(text):
    """Lo que escribió el invitado -> el código como lo muestra la sala, o None si no es uno. Da igual mayúsculas,
    guiones y espacios; O se lee como 0, I y L como 1. Sin prefijo se asume el primer servidor."""
    s = "".join(c for c in str(text).upper() if c.isalnum())
    prefix = BROKERS[0][2]
    if len(s) == 4 + CODE_CHARS:
        prefix = next((b[2] for b in BROKERS if s.startswith(b[2])), None)
        s = s[4:]
    if prefix is None or len(s) != CODE_CHARS:
        return None
    s = s.translate(str.maketrans("OIL", "011"))
    if any(c not in ALPHABET for c in s):
        return None
    return f"{prefix}-{s[:4]}-{s[4:]}"


def broker_of(code):
    """Índice en BROKERS del servidor de ese código (ya pasado por `parse_code`)."""
    return next(i for i, b in enumerate(BROKERS) if code.startswith(b[2] + "-"))


def derive(code):
    """(tema, clave) de un código: los dos salen de PBKDF2, así que ver el tema no da la clave."""
    raw = hashlib.pbkdf2_hmac("sha256", code.encode(), SALT, KDF_ROUNDS, 48)
    return TOPIC_ROOT + raw[:16].hex(), raw[16:]


def seal(key, obj):
    """Mensaje firmado: 32 bytes de HMAC-SHA256 + el JSON."""
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).digest() + body


def unseal(key, data):
    """El dict de un mensaje firmado con esa clave, o None (firma mala, roto, otra sala): se descarta sin más."""
    if len(data) < 33:
        return None
    sig, body = data[:32], data[32:]
    if not hmac.compare_digest(sig, hmac.new(key, body, hashlib.sha256).digest()):
        return None
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- MQTT 3.1.1 mínimo (QoS 0)
def enc_len(n):
    """Largo restante de MQTT: 7 bits por byte, el bit alto dice "sigue"."""
    if not 0 <= n <= 268_435_455:
        raise ValueError("paquete demasiado grande")
    out = b""
    while True:
        b, n = n % 128, n // 128
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def dec_len(buf, i=1):
    """(largo, índice donde empieza el cuerpo) leyendo desde buf[i], o None si faltan bytes."""
    mult, n = 1, 0
    for k in range(4):
        if i + k >= len(buf):
            return None
        b = buf[i + k]
        n += (b & 127) * mult
        if not b & 128:
            return n, i + k + 1
        mult *= 128
    raise ValueError("largo de paquete inválido")


def _s(x):
    x = x.encode("utf-8")
    return struct.pack("!H", len(x)) + x


def packet(kind, body=b""):
    return bytes([kind]) + enc_len(len(body)) + body


def split_packets(buf):
    """Paquetes completos al principio de `buf`: ([(tipo, cuerpo)], lo que sobra)."""
    out = []
    while buf:
        head = dec_len(buf)
        if not head:
            break
        n, start = head
        if n > MAX_PACKET:
            raise ValueError("paquete demasiado grande")
        if len(buf) < start + n:
            break
        out.append((buf[0], buf[start:start + n]))
        buf = buf[start + n:]
    return out, buf


def publish_packet(topic, payload):
    return packet(0x30, _s(topic) + payload)


def parse_publish(kind, body):
    """(tema, contenido) de un PUBLISH (QoS 0 o con id de paquete si viniera con QoS > 0)."""
    tl = struct.unpack("!H", body[:2])[0]
    topic = body[2:2 + tl].decode("utf-8", "replace")
    rest = body[2 + tl:]
    return topic, (rest[2:] if (kind >> 1) & 3 else rest)


def dial(broker, topic):
    """Conecta por TLS, hace CONNECT (sesión limpia) y SUBSCRIBE al tema. Devuelve el socket listo."""
    host, port, _ = broker
    raw = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.sendall(packet(0x10, _s("MQTT") + bytes([4, 2]) + struct.pack("!H", KEEPALIVE)
                            + _s("pw" + secrets.token_hex(8))))
        buf, subbed = b"", False
        while not subbed:
            data = sock.recv(4096)
            if not data:
                raise ConnectionError("el servidor cortó")
            pkts, buf = split_packets(buf + data)
            for kind, body in pkts:
                if kind == 0x20:                    # CONNACK; recién ahí SUBSCRIBE (mandarlo junto demora a hivemq)
                    if len(body) < 2 or body[1] != 0:
                        raise ConnectionError(f"el servidor no aceptó la conexión (código {body[1:2].hex()})")
                    sock.sendall(packet(0x82, struct.pack("!H", 1) + _s(topic) + b"\x00"))
                elif kind == 0x90:
                    if body[2:3] == b"\x80":
                        raise ConnectionError("el servidor no dejó suscribirse")
                    subbed = True
        return sock, buf
    except BaseException:
        raw.close()
        raise


def _why(e):
    if isinstance(e, socket.timeout):
        return "no contestó a tiempo"
    if isinstance(e, socket.gaierror):
        return "no encontré el servidor (¿hay internet?)"
    return str(e) or e.__class__.__name__


def fail_text(host, e, fallback=None):
    txt = f"No pude conectarme al servidor de internet ({host}): {_why(e)}."
    if fallback:
        txt += f" Probé también con {fallback} y tampoco."
    return txt + " Revisá tu conexión; es un servicio gratuito y puede estar caído un rato: probá de nuevo en unos minutos."


# --------------------------------------------------------------------------- la sala en el servidor
class Bus:
    """Una conexión MQTT de esta compu, suscripta al tema de la sala. Un solo hilo maneja el socket: lee, reparte
    lo que llega a cada `RelayLink`, manda lo encolado (en lotes, con pausa GAP), manda latidos y, si se corta,
    reintenta conectar durante DEAD_AFTER s. Se cierra solo cuando ya nadie lo usa (sala cerrada y sin links)."""

    def __init__(self, code, keys, sid, broker, sock, buf=b"", start=True):
        """`keys` = derive(code) (ya calculado para conectar); `sid` = HOST o el id al azar de un invitado.
        `start=False` no arranca el hilo (pruebas: se maneja a mano con `_flush` / `_receive`)."""
        self.code, self.sid, self.broker = code, sid, broker
        self.topic, self.key = keys
        self.sock, self._inbuf = sock, buf
        self.lock = threading.Lock()
        self.out = []              # [(destinos, mensaje)] por mandar
        self.seq = 0
        self.last_seq = {}         # remitente -> último número visto (lo que no sea mayor se descarta)
        self.links = {}            # remitente -> RelayLink
        self.dead = set()          # remitentes que ya se cerraron: se ignoran
        self.fresh = []            # links nuevos para `accept` (solo la sala)
        self.accepting = False
        self.refuse = None         # con la sala cerrada, qué se le contesta a un saludo nuevo ("full" / "started")
        self.room_open = False
        self.synced = sid == HOST  # invitado: hasta que la sala le hable a él, no le cree nada (anti reenvíos)
        self.alive, self.closing = True, False
        self.done = threading.Event()
        self.last_pub = self.last_out = 0.0
        self.last_in = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        if start:
            self._thread.start()
        else:
            self.done.set()

    # --- desde el hilo principal
    def post(self, to, msg):
        with self.lock:
            if self.alive and not self.closing:
                self.out.append(([to] if isinstance(to, str) else list(to), msg))

    def drop(self, link):
        """Un link se cerró: se olvida (y se cierra el bus si era el último usuario)."""
        with self.lock:
            if self.links.get(link.peer) is link:
                del self.links[link.peer]
                self.dead.add(link.peer)
        self._maybe_close()

    def close_room(self):
        with self.lock:
            self.accepting, self.room_open = False, False
        self._maybe_close()

    def _maybe_close(self):
        with self.lock:
            if self.room_open or self.links or self.closing:
                return
            self.closing = True
        if threading.current_thread() is not self._thread:
            self.done.wait(CLOSE_WAIT)       # que salgan los "bye" antes de que se vaya el programa

    def kill(self):
        """Corta de golpe, sin avisar (para pruebas: como desenchufar la compu)."""
        with self.lock:
            self.alive, self.closing = False, True
        try:
            self.sock.close()
        except OSError:
            pass

    # --- el hilo del socket
    def _run(self):
        try:
            while self.alive:
                if self.closing:
                    try:
                        self._flush(final=True)
                        self._send(packet(0xE0))
                    except (OSError, ValueError):
                        pass
                    break
                try:
                    self._step()
                except Exception:                    # cortes, TLS, paquetes rotos: reconectar
                    if not self.alive or self.closing or not self._reconnect():
                        break
        finally:
            dying = self.alive and not self.closing
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            if dying:                                # se cortó el servidor: todos los links se enteran
                with self.lock:
                    links = list(self.links.values())
                for link in links:
                    link._lost()
            self.done.set()

    def _step(self):
        sock = self.sock
        if sock.pending() or select.select([sock], [], [], 0.02)[0]:
            data = sock.recv(65536)
            if not data:
                raise ConnectionError("el servidor cortó")
            self.last_in = time.time()
            pkts, self._inbuf = split_packets(self._inbuf + data)
            for kind, body in pkts:
                if kind >> 4 == 3:
                    try:
                        topic, payload = parse_publish(kind, body)
                    except (ValueError, struct.error):
                        continue
                    self._receive(topic, payload)
        now = time.time()
        if now - self.last_in > DEAD_AFTER:          # ni nuestros propios latidos vuelven: conexión muerta
            raise ConnectionError("el servidor dejó de contestar")
        if now - self.last_pub > PING_EVERY:
            self.post("*", {"t": "ping"})
        if now - self.last_pub >= GAP:
            self._flush()
        if now - self.last_out > KEEPALIVE / 2:
            self._send(packet(0xC0))

    def _send(self, data):
        self.sock.sendall(data)
        self.last_out = time.time()

    def _flush(self, final=False):
        """Manda lo encolado: un lote (o varios, al cerrar). Mensajes iguales seguidos (un broadcast) van una sola
        vez con todos los destinos."""
        while True:
            with self.lock:
                items, self.out = self.out, []
            if not items:
                return
            batch, size, rest = [], 0, []
            for i, (to, msg) in enumerate(items):
                text = json.dumps(msg, separators=(",", ":"))
                if batch and batch[-1][2] == text:
                    batch[-1][0].extend(t for t in to if t not in batch[-1][0])
                    continue
                if batch and size + len(text) > BATCH_MAX:
                    rest = items[i:]
                    break
                batch.append([list(to), msg, text])
                size += len(text) + 20
            if rest:
                with self.lock:
                    self.out = rest + self.out
            self.seq += 1
            env = {"f": self.sid, "s": self.seq, "b": [[to, msg] for to, msg, _ in batch]}
            self._send(publish_packet(self.topic, seal(self.key, env)))
            self.last_pub = time.time()
            if not final:
                return

    def _reconnect(self):
        """Se cortó con el servidor: reintenta (el mismo, que es donde está la sala) durante DEAD_AFTER s."""
        try:
            self.sock.close()
        except OSError:
            pass
        end = time.time() + DEAD_AFTER
        while self.alive and not self.closing and time.time() < end:
            try:
                self.sock, self._inbuf = dial(self.broker, self.topic)
                self.last_in = self.last_pub = time.time()
                return True
            except (OSError, ValueError, ConnectionError):
                time.sleep(1.0)
        return False

    def _receive(self, topic, payload):
        if topic != self.topic:
            return
        env = unseal(self.key, payload)
        if not env:
            return
        f, seq, items = env.get("f"), env.get("s"), env.get("b")
        if not (isinstance(f, str) and 0 < len(f) <= 20 and isinstance(seq, int) and isinstance(items, list)):
            return
        if f == self.sid:
            return                                   # lo nuestro, que vuelve
        with self.lock:
            if seq <= self.last_seq.get(f, 0):
                return                               # repetido o reenvío viejo
            self.last_seq[f] = seq
        mine = []
        for it in items:
            if not (isinstance(it, list) and len(it) == 2 and isinstance(it[0], list) and isinstance(it[1], dict)):
                continue
            to, msg = it
            if self.sid in to:
                self.synced = True
            if self.synced and (self.sid in to or "*" in to):
                mine.append(msg)
        if self.sid == HOST:
            self._at_host(f, mine)
        elif f == HOST and self.synced:
            link = self.links.get(HOST)
            if link:
                link.last_rx = time.time()          # cualquier cosa de la sala dice que sigue ahí
                if mine:
                    link._deliver(mine)

    def _at_host(self, f, msgs):
        with self.lock:
            link = self.links.get(f)
            if link is None:
                if f in self.dead or not any(m.get("t") == "hello" for m in msgs):
                    return
                if not self.accepting:               # sala cerrada: se le contesta como en la red local
                    self.dead.add(f)
                    if self.refuse:
                        self.out.append(([f], {"t": "bye", "why": self.refuse}))
                    self.out.append(([f], {"t": "_closed"}))
                    return
                link = self.links[f] = RelayLink(self, f)
                self.fresh.append(link)
        link.last_rx = time.time()
        if msgs:
            link._deliver(msgs)


class RelayLink:
    """Como `pokenet.Link`, pero por el servidor de internet, con un solo otro (`peer`)."""

    def __init__(self, bus, peer):
        self.bus, self.peer = bus, peer
        self.q = queue.Queue()
        self.alive = True
        self.heard = False             # ya llegó algo del otro
        self.born = self.last_rx = self.last_tx = time.time()

    def _deliver(self, msgs):
        self.last_rx, self.heard = time.time(), True
        for m in msgs:
            if m.get("t") == "_closed":
                self._lost(notify=False)
                return
            self.q.put(m)

    def _lost(self, notify=False):
        if not self.alive:
            return
        if notify:
            self.bus.post(self.peer, {"t": "_closed"})
        self.alive = False
        self.q.put({"t": "_closed"})
        self.bus.drop(self)

    def send(self, obj):
        if not self.alive:
            return False
        self.bus.post(self.peer, obj)
        self.last_tx = time.time()
        return True

    def unanswered(self):
        """Invitado: ¿la sala no contestó nada en ANSWER_SECS? (código mal escrito o sala cerrada)."""
        return not self.heard and time.time() - self.born > ANSWER_SECS

    def poll(self):
        """Mensajes que llegaron desde la última vez. Un {"t": "_closed"} avisa que se cortó."""
        if self.alive and self.heard and time.time() - self.last_rx > DEAD_AFTER:
            self._lost(notify=True)              # por si el otro sigue ahí pero no lo oímos: que se entere
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return [m for m in out if m.get("t") != "ping"]

    def close(self, why=None):
        """Cierra; con `why` le avisa antes al otro por qué (como el "bye" de la red local)."""
        if self.alive:
            if why:
                self.bus.post(self.peer, {"t": "bye", "why": why})
            self.bus.post(self.peer, {"t": "_closed"})
        self.alive = False
        self.bus.drop(self)


class RelayRoom:
    """La sala por internet del que la crea: lo que `pokenet` usa de `RoomHost` (accept / set_tour / close) más
    `code`. `refuse` = qué se le contesta al que llega con la sala cerrada ("full" en 1 contra 1, "started" en
    un torneo)."""

    ips = ()

    def __init__(self, bus, refuse):
        self.bus, self.code, self.where = bus, bus.code, bus.broker[0]
        with bus.lock:
            bus.accepting, bus.room_open, bus.refuse = True, True, refuse

    def accept(self):
        with self.bus.lock:
            return self.bus.fresh.pop(0) if self.bus.fresh else None

    def set_tour(self, tour):
        pass

    def close(self):
        self.bus.close_room()


def open_room(refuse, brokers=None):
    """Abre una sala en el primer servidor que conecte. RelayError con el texto para mostrar si no anda ninguno."""
    brokers = list(range(len(BROKERS))) if brokers is None else brokers
    first = None
    for i in brokers:
        code = new_code(i)
        topic, key = derive(code)
        try:
            sock, buf = dial(BROKERS[i], topic)
        except (OSError, ValueError, ConnectionError) as e:
            first = first or (BROKERS[i][0], e)
            continue
        return RelayRoom(Bus(code, (topic, key), HOST, BROKERS[i], sock, buf), refuse)
    others = [BROKERS[i][0] for i in brokers[1:]]
    raise RelayError(fail_text(first[0], first[1], ", ".join(others) or None))


def join(code):
    """Entra a la sala de ese código (ya pasado por `parse_code`): el RelayLink con la sala ("h")."""
    broker = BROKERS[broker_of(code)]
    topic, key = derive(code)
    try:
        sock, buf = dial(broker, topic)
    except (OSError, ValueError, ConnectionError) as e:
        raise RelayError(fail_text(broker[0], e)) from None
    bus = Bus(code, (topic, key), secrets.token_hex(6), broker, sock, buf)
    link = RelayLink(bus, HOST)
    with bus.lock:
        bus.links[HOST] = link
    return link
