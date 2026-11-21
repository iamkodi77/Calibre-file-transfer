"""Calibre smart-device server: discovery, handshake and binary push transfers.

The wire protocol is a decimal byte length immediately followed by a JSON array.
The server initiates commands; compatible device apps connect as clients.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import queue
import select
import socket
import threading
import time
import uuid
from pathlib import Path

from .storage import Library, now_iso

log = logging.getLogger(__name__)
DISCOVERY_PORTS = (54982, 48123, 39001, 44044, 59678)
OK, SET_DEVICE_INFO, GET_DEVICE_INFO, SEND_BOOK, GET_INITIALIZATION_INFO = 0, 1, 3, 8, 9
NOOP, DISPLAY_MESSAGE, SET_LIBRARY_INFO, ERROR = 12, 17, 19, 20
MAX_FRAME = 8 * 1024 * 1024
DEFAULT_EXTENSIONS = ["epub", "pdf", "mobi", "azw3", "fb2", "cbz", "cbr", "txt", "html", "docx", "rtf", "jpg", "jpeg", "png", "gif", "webp", "zip"]


def local_ipv4_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str):
                    try:
                        addr = ipaddress.ip_address(ip.ip)
                        if addr.version == 4 and not addr.is_loopback and not addr.is_unspecified and not addr.is_link_local:
                            addresses.append(str(addr))
                    except ValueError:
                        pass
    except ImportError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No packet is sent; this asks the OS which interface would route traffic.
            sock.connect(("192.0.2.1", 80))
            addresses.insert(0, sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    return list(dict.fromkeys(ip for ip in addresses if not ip.startswith("127."))) or ["127.0.0.1"]


def send_frame(sock: socket.socket, opcode: int, argument: dict) -> None:
    # ASCII escaping means the prefix is also correct for non-ASCII metadata.
    body = json.dumps([opcode, argument], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    sock.sendall(str(len(body)).encode("ascii") + body)


def _read_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        data = sock.recv(min(length - len(chunks), 65536))
        if not data:
            raise ConnectionError("The device disconnected.")
        chunks.extend(data)
    return bytes(chunks)


def receive_frame(sock: socket.socket) -> tuple[int, dict]:
    prefix = bytearray()
    while True:
        byte = _read_exact(sock, 1)
        if byte == b"[":
            break
        if not byte.isdigit() or len(prefix) >= 8:
            raise ValueError("Invalid Calibre frame length")
        prefix.extend(byte)
    if not prefix:
        raise ValueError("Missing Calibre frame length")
    length = int(prefix)
    if length < 2 or length > MAX_FRAME:
        raise ValueError("Invalid Calibre frame size")
    data = json.loads((b"[" + _read_exact(sock, length - 1)).decode("utf-8"))
    if not isinstance(data, list) or len(data) != 2 or not isinstance(data[0], int) or not isinstance(data[1], dict):
        raise ValueError("Invalid Calibre message")
    return data[0], data[1]


class DeviceRejected(Exception):
    pass


class DeviceSession(threading.Thread):
    def __init__(self, service: "WirelessService", sock: socket.socket, peer: tuple):
        super().__init__(name="wireless-device", daemon=True)
        self.service = service
        self.library = service.library
        self.sock = sock
        self.peer = peer
        self.id = str(uuid.uuid4())
        self.name = "Wireless reader"
        self.app_name = ""
        self.model = ""
        self.extensions: list[str] = []
        self.info: dict = {}
        self.connected_at = now_iso()
        self.jobs: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        self.online = False
        self.closing = False
        self.current_transfer = None
        self.packet_length = 4096
        self.sock.settimeout(15)

    def _request(self, opcode: int, data: dict) -> tuple[int, dict]:
        send_frame(self.sock, opcode, data)
        return receive_frame(self.sock)

    def _expect_ok(self, opcode: int, data: dict) -> dict:
        response, result = self._request(opcode, data)
        if response == ERROR:
            raise DeviceRejected(str(result.get("message") or "The reader rejected the request."))
        if response != OK:
            raise ConnectionError("Unexpected response from the reader.")
        return result

    def handshake(self) -> None:
        challenge = now_iso() if self.service.password else ""
        # Include extensions already in this library. The device still has the
        # final say about which formats it will accept.
        valid_extensions = list(dict.fromkeys(DEFAULT_EXTENSIONS + [f["extension"] for f in self.library.list_files() if f["extension"]]))
        info = self._expect_ok(GET_INITIALIZATION_INFO, {
            "serverProtocolVersion": 1,
            "validExtensions": valid_extensions,
            "passwordChallenge": challenge,
            "currentLibraryName": self.library.setting("library_name"),
            "currentLibraryUUID": self.library.setting("library_uuid"),
            "pubdateFormat": "MMM yyyy", "timestampFormat": "dd MMM yyyy",
            "lastModifiedFormat": "dd MMM yyyy", "calibre_version": [7, 0, 0],
            "canSupportUpdateBooks": False, "canSupportLpathChanges": True,
        })
        if not info.get("versionOK", False):
            raise ConnectionError("The reader does not support this protocol version.")
        if self.service.password:
            expected = hashlib.sha1((self.service.password + challenge).encode("utf-8")).hexdigest()
            actual = str(info.get("passwordHash", ""))
            if not hmac.compare_digest(expected, actual):
                try:
                    self._request(DISPLAY_MESSAGE, {"messageKind": 1, "currentLibraryName": self.library.setting("library_name"),
                                                    "currentLibraryUUID": self.library.setting("library_uuid")})
                except (OSError, ValueError, ConnectionError):
                    pass
                raise ConnectionError("Wireless password did not match.")
        if not info.get("canReceiveBookBinary", False) or not info.get("canStreamBooks", False):
            raise ConnectionError("This reader does not support binary wireless transfers.")
        try:
            packet_length = int(info.get("maxBookContentPacketLen", 4096))
        except (TypeError, ValueError):
            raise ConnectionError("The reader reported an invalid packet size.") from None
        if packet_length <= 0:
            raise ConnectionError("The reader reported an invalid packet size.")
        self.packet_length = min(packet_length, 1024 * 1024)
        self.info = info
        self.app_name = str(info.get("appName") or "Reader")[:120]
        self.model = str(info.get("deviceKind") or "")[:120]
        self.name = str(info.get("deviceName") or self.model or self.app_name or "Wireless reader")[:120]
        raw_extensions = info.get("acceptedExtensions")
        if isinstance(raw_extensions, list):
            self.extensions = list(dict.fromkeys(str(x).lower().lstrip(".") for x in raw_extensions if isinstance(x, str) and x))[:256]
        device_info = self._expect_ok(GET_DEVICE_INFO, {})
        drive = device_info.get("device_info")
        if not isinstance(drive, dict):
            drive = {}
        drive = dict(drive)
        drive.setdefault("device_store_uuid", str(uuid.uuid4()))
        drive.setdefault("device_name", self.name)
        drive.update({"location_code": "main", "last_library_uuid": self.library.setting("library_uuid"),
                      "calibre_version": "7.0.0", "date_last_connected": now_iso(), "prefix": ""})
        self._expect_ok(SET_DEVICE_INFO, drive)
        if info.get("canAcceptLibraryInfo"):
            self._expect_ok(SET_LIBRARY_INFO, {"libraryName": self.library.setting("library_name"),
                                               "libraryUuid": self.library.setting("library_uuid"),
                                               "fieldMetadata": {}, "otherInfo": {"id_link_rules": {}}})

    def public(self) -> dict:
        with self.lock:
            return {"id": self.id, "name": self.name, "app_name": self.app_name, "model": self.model,
                    "address": self.peer[0], "connected_at": self.connected_at, "accepted_extensions": self.extensions,
                    "busy": self.current_transfer is not None, "closing": self.closing}

    def accepts(self, file: dict) -> str | None:
        if file["size"] == 0:
            return "Empty files can be downloaded from the web catalog, but cannot be pushed to this reader."
        if self.extensions and file["extension"].lower() not in self.extensions:
            ext = "." + file["extension"] if file["extension"] else "files without an extension"
            return f"This reader did not report support for {ext}. Try the web catalog instead."
        return None

    def submit(self, file: dict) -> dict:
        with self.lock:
            if not self.online or self.closing:
                raise ConnectionError("The reader is no longer connected.")
            job = self.library.create_transfer(file, self.id, self.name)
            self.jobs.put(job["id"])
            return job

    def disconnect(self) -> None:
        with self.lock:
            if self.online and not self.closing:
                self.closing = True
                self.jobs.put(None)

    def force_close(self) -> None:
        with self.lock:
            self.closing = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    @staticmethod
    def _device_filename(file: dict, use_uuid: bool) -> str:
        if use_uuid and file["extension"]:
            return file["id"] + "." + file["extension"]
        # Relative single-component name; never send a path that can escape the inbox.
        name = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in file["filename"])
        return name.lstrip(".").strip() or (file["id"] + "." + file["extension"])

    def _send_transfer(self, job_id: str) -> None:
        job = self.library.get_transfer(job_id)
        if not job:
            return
        file = self.library.get_file(job["file_id"])
        if not file:
            self.library.update_transfer(job_id, "failed", error="The file was removed from the library.")
            return
        reason = self.accepts(file)
        if reason:
            self.library.update_transfer(job_id, "failed", error=reason)
            return
        try:
            path = self.library.file_path(file["id"])
            with path.open("rb") as stream:
                if path.stat().st_size != file["size"]:
                    raise DeviceRejected("The stored file has changed on disk.")
                name = self._device_filename(file, bool(self.info.get("useUuidFileNames")))
                metadata = {"uuid": file["id"], "lpath": name, "title": file["title"],
                            "authors": [file["author"] or "Unknown"], "author_sort": file["author"] or "Unknown",
                            "title_sort": file["title"], "size": file["size"], "tags": [], "series": None,
                            "series_index": 1.0, "last_modified": file["updated_at"], "timestamp": file["added_at"],
                            "pubdate": "None", "identifiers": {}, "user_metadata": {}, "thumbnail": None}
                self.library.update_transfer(job_id, "sending", 0)
                args = {"lpath": name, "length": file["size"], "metadata": metadata, "thisBook": 0, "totalBooks": 1,
                        "willStreamBooks": True, "willStreamBinary": True,
                        "wantsSendOkToSendbook": bool(self.info.get("canSendOkToSendbook")), "canSupportLpathChanges": True}
                if self.info.get("canSendOkToSendbook"):
                    self._expect_ok(SEND_BOOK, args)
                else:
                    send_frame(self.sock, SEND_BOOK, args)
                sent = 0
                last_update = time.monotonic()
                while True:
                    chunk = stream.read(self.packet_length)
                    if not chunk:
                        break
                    self.sock.sendall(chunk)
                    sent += len(chunk)
                    if time.monotonic() - last_update > 0.4:
                        self.library.update_transfer(job_id, "sending", sent)
                        last_update = time.monotonic()
                if sent != file["size"]:
                    raise ConnectionError("The stored file changed during transfer.")
                # This response is a protocol barrier: a compliant client has consumed
                # the binary body and returned to JSON mode before it replies.
                self._expect_ok(NOOP, {})
                self.library.update_transfer(job_id, "sent", sent)
        except DeviceRejected as exc:
            self.library.update_transfer(job_id, "failed", error=str(exc))
        except FileNotFoundError:
            self.library.update_transfer(job_id, "failed", error="The stored file is missing.")
        except (OSError, ValueError, ConnectionError) as exc:
            self.library.update_transfer(job_id, "failed", error=str(exc) or "The reader disconnected during transfer.")
            raise
        except Exception:
            self.library.update_transfer(job_id, "failed", error="An unexpected error interrupted the transfer.")
            raise

    def run(self) -> None:
        registered = False
        try:
            self.handshake()
            with self.lock:
                if self.closing or self.service.stopping.is_set():
                    return
                self.online = True
            self.service.register(self)
            registered = True
            self.library.log_event("device", "Reader connected", self.name)
            last_heartbeat = time.monotonic()
            while not self.service.stopping.is_set():
                try:
                    job_id = self.jobs.get(timeout=1.0)
                except queue.Empty:
                    ready, _, _ = select.select([self.sock], [], [], 0)
                    if ready:
                        # Devices normally send a final NOOP just before closing.
                        receive_frame(self.sock)
                        break
                    if time.monotonic() - last_heartbeat >= 25:
                        self._expect_ok(NOOP, {})
                        last_heartbeat = time.monotonic()
                    continue
                if job_id is None:
                    try:
                        self._expect_ok(NOOP, {"ejecting": True})
                    except (OSError, ValueError, ConnectionError, DeviceRejected):
                        pass
                    break
                with self.lock:
                    self.current_transfer = job_id
                try:
                    self._send_transfer(job_id)
                finally:
                    with self.lock:
                        self.current_transfer = None
                last_heartbeat = time.monotonic()
        except (OSError, ValueError, ConnectionError, DeviceRejected) as exc:
            log.info("Wireless reader %s disconnected: %s", self.peer[0], exc)
        except Exception:
            log.exception("Unexpected error handling wireless reader %s", self.peer[0])
        finally:
            with self.lock:
                self.online = False
                self.closing = True
            self.force_close()
            self.service.unregister(self)
            if registered:
                self.library.log_event("device", "Reader disconnected", self.name)
            while True:
                try:
                    job_id = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if job_id:
                    self.library.update_transfer(job_id, "failed", error="The reader disconnected before this transfer started.")


class WirelessService:
    def __init__(self, library: Library, host: str = "0.0.0.0", port: int = 9090, http_port: int = 8000,
                 advertise_host: str | None = None, password: str = "", discovery_ports=DISCOVERY_PORTS,
                 publish_mdns: bool = True):
        self.library = library
        self.host, self.port, self.http_port = host, port, http_port
        if advertise_host:
            self.advertise_host = advertise_host
        elif host not in ("", "0.0.0.0"):
            self.advertise_host = socket.gethostbyname(host)
        else:
            self.advertise_host = local_ipv4_addresses()[0]
        self.password = password
        self.discovery_ports = discovery_ports
        self.publish_mdns = publish_mdns
        self.stopping = threading.Event()
        self.lock = threading.RLock()
        self.sessions: dict[str, DeviceSession] = {}
        self.pending_sessions: set[DeviceSession] = set()
        self.server_socket: socket.socket | None = None
        self.udp_sockets: list[socket.socket] = []
        self.running = False
        self.error = ""
        self.mdns_state = "starting" if publish_mdns else "disabled"
        self.zeroconf = None
        self.mdns_services = []
        self.mdns_thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(16)
            server.settimeout(1)
            self.server_socket = server
            self.port = server.getsockname()[1]
            self.running = True
        except OSError as exc:
            self.error = str(exc)
            log.error("Could not start wireless server: %s", exc)
            try:
                server.close()
            except (OSError, UnboundLocalError):
                pass
            return False
        for port in self.discovery_ports:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                udp.bind((self.host, port))
                self.udp_sockets.append(udp)
            except OSError as exc:
                log.info("Discovery port %s unavailable: %s", port, exc)
                udp.close()
        threading.Thread(target=self._accept_loop, name="wireless-listener", daemon=True).start()
        if self.udp_sockets:
            threading.Thread(target=self._discovery_loop, name="wireless-discovery", daemon=True).start()
        if self.publish_mdns:
            self.mdns_thread = threading.Thread(target=self._publish_mdns, name="mdns-publisher", daemon=True)
            self.mdns_thread.start()
        log.info("Wireless device server listening on %s:%s", self.host, self.port)
        return True

    def _accept_loop(self):
        while not self.stopping.is_set():
            try:
                sock, peer = self.server_socket.accept()
                session = DeviceSession(self, sock, peer)
                with self.lock:
                    if self.stopping.is_set():
                        sock.close()
                        break
                    self.pending_sessions.add(session)
                session.start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _discovery_loop(self):
        host_label = socket.gethostname().partition(".")[0].replace(";", "-")
        message = f"calibre wireless device client (on {host_label});{self.http_port},{self.port}".encode("utf-8")
        while not self.stopping.is_set():
            try:
                ready, _, _ = select.select(self.udp_sockets, [], [], 1)
                for sock in ready:
                    _, remote = sock.recvfrom(1024)
                    sock.sendto(message, remote)
            except (OSError, ValueError):
                break

    def _publish_mdns(self):
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
            addresses = [socket.inet_aton(self.advertise_host)]
            hostname = socket.gethostname().partition(".")[0].replace(" ", "-")[:40] or "leafline"
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            services = [
                ServiceInfo("_calibresmartdeviceapp._tcp.local.", f"calibre smart device client on {hostname}._calibresmartdeviceapp._tcp.local.",
                            addresses=addresses, port=self.port, properties={}, server=f"{hostname}.local."),
                ServiceInfo("_calibre._tcp.local.", f"Books in Leafline on {hostname}._calibre._tcp.local.",
                            addresses=addresses, port=self.http_port, properties={"path": "/opds"}, server=f"{hostname}.local."),
            ]
            # Keep the registration and shutdown synchronized so stopping while
            # zeroconf is probing cannot leak a service/thread.
            with self.lock:
                if self.stopping.is_set():
                    zc.close()
                    return
                self.zeroconf = zc
            for info in services:
                if self.stopping.is_set():
                    break
                # Calibre's historic service name exceeds the mDNS 15-byte
                # limit; Calibre itself registers it with strict=False.
                zc.register_service(info, allow_name_change=True, strict=False)
                with self.lock:
                    self.mdns_services.append(info)
            self.mdns_state = "available" if not self.stopping.is_set() else "stopped"
        except Exception as exc:
            self.mdns_state = "unavailable"
            log.warning("mDNS discovery unavailable (UDP/manual connection still work): %s", exc)

    def register(self, session: DeviceSession):
        with self.lock:
            self.pending_sessions.discard(session)
            if not self.stopping.is_set():
                self.sessions[session.id] = session

    def unregister(self, session: DeviceSession):
        with self.lock:
            self.sessions.pop(session.id, None)
            self.pending_sessions.discard(session)

    def devices(self) -> list[dict]:
        with self.lock:
            sessions = list(self.sessions.values())
        return [session.public() for session in sessions if session.online]

    def device(self, device_id: str) -> DeviceSession | None:
        with self.lock:
            return self.sessions.get(device_id)

    def status(self) -> dict:
        return {"running": self.running and not self.stopping.is_set(), "port": self.port,
                "host": self.advertise_host, "discovery_ports": [s.getsockname()[1] for s in self.udp_sockets if s.fileno() != -1],
                "mdns": self.mdns_state, "error": self.error, "password_enabled": bool(self.password)}

    def stop(self):
        self.stopping.set()
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
        for sock in self.udp_sockets:
            try:
                sock.close()
            except OSError:
                pass
        with self.lock:
            sessions = list(self.sessions.values()) + list(self.pending_sessions)
        for session in sessions:
            session.force_close()
        for session in sessions:
            if session is not threading.current_thread() and session.is_alive():
                session.join(timeout=2)
        # Let an in-flight mDNS registration finish before unregistering it.
        if self.mdns_thread and self.mdns_thread is not threading.current_thread():
            self.mdns_thread.join(timeout=5)
        with self.lock:
            zc = self.zeroconf
            services = list(self.mdns_services)
        if zc:
            for info in services:
                try:
                    zc.unregister_service(info)
                except Exception:
                    pass
            try:
                zc.close()
            except Exception:
                pass
