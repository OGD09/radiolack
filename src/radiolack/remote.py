"""Remote access: phones and tablets on the local network control Radiolack.

Remote access is off at each start. Once this computer's interface turns it
on, the server (server.py) also listens over HTTPS, on a fixed port, on this
computer's private IPv4 addresses. A device installs the certificate of
Radiolack's local authority once, then enters the access code once: it then
holds a session cookie, and with it the interface, the stream and the audio.

This module keeps what remote access needs between starts, in remote.json in
the settings folder (store.py): the port, the access code and the wrong codes
entered since it was made, the paired devices and the local authority. It
also makes the certificates and finds the addresses to listen on.

The local authority is a root certificate whose private key is discarded as
soon as it has signed one intermediate certificate. Name constraints limit
that intermediate to private IPv4 addresses and local names (``.local``,
``home.arpa``, ``.internal``), and its extended key usage to TLS servers: a
device that trusts the root trusts nothing else, even should someone take the
intermediate's key from the settings folder. The intermediate signs a new
server certificate, for the addresses in use, at each turn-on.

A session is a random token in an HttpOnly cookie, of which this computer
keeps only the SHA-256 hash. A device stays paired until the user forgets
it, makes a new code, or leaves it unused for 90 days. Ten wrong codes block
the code until the user makes a new one.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REMOTE_FILE = "remote.json"
DEFAULT_PORT = 8443
MIN_PORT = 1024                 # lower ports need administrator rights
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # no 0 or O, no 1 or I: nothing to misread
CODE_LENGTH = 8                 # 40 bits
MAX_WRONG_CODES = 10            # then the code no longer works, until a new one is made
DEVICE_EXPIRY_S = 90 * 24 * 3600
MAX_DEVICES = 20
SESSION_COOKIE = "__Host-radiolack"     # sent back only over HTTPS, to this host, for every path
SESSION_MAX_AGE_S = 400 * 24 * 3600     # the longest browsers keep a cookie
# What remote access listens on and what its authority certifies: the private
# IPv4 ranges, the shared range that Tailscale uses, link-local addresses and
# loopback, which lets the tests verify certificates on 127.0.0.1.
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"))
LOCAL_DOMAINS = ("local", "home.arpa", "internal")
# The order addresses are offered in: home networks first, VPNs last.
_ADDRESS_ORDER = tuple(ipaddress.IPv4Network(network) for network in (
    "192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8"))

_AUTHORITY_DAYS = 20 * 365
_SERVER_DAYS = 397              # Apple accepts at most 825 days, some browsers 398
_CLOCK_SKEW = datetime.timedelta(days=1)    # a device's clock may lag this computer's
_SEEN_STEP_S = 3600             # a device's latest use is noted to the hour
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")   # one DNS label
_DEVICE_ID = re.compile(r"[0-9a-f]{8}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_TOKEN = 100                # a session token has 43 characters
_MAX_LABEL = 60
_PEM_CERTIFICATE = "-----BEGIN CERTIFICATE-----"
_PEM_KEY = "-----BEGIN PRIVATE KEY-----"
# Linux ioctl requests and interface flags (<linux/sockios.h>, <net/if.h>).
_SIOCGIFFLAGS, _SIOCGIFADDR = 0x8913, 0x8915
_IFF_UP, _IFF_LOOPBACK, _IFF_RUNNING = 0x1, 0x8, 0x40

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Authority:
    """The local authority: the root certificate that devices install, and the
    intermediate certificate and key that sign the server's certificates, in
    PEM. The root's key no longer exists."""

    root: str
    certificate: str
    key: str

    def root_der(self) -> bytes:
        return ssl.PEM_cert_to_DER_cert(self.root)

    @property
    def fingerprint(self) -> str:
        """The root's SHA-256 fingerprint as devices show it: hex bytes, such as "3A 4B …"."""
        digest = hashlib.sha256(self.root_der()).hexdigest().upper()
        return " ".join(digest[i:i + 2] for i in range(0, len(digest), 2))


@dataclasses.dataclass
class Device:
    """A paired device. ``session`` is the SHA-256 hash of its session token;
    ``paired`` and ``seen``, when it was paired and last used, in Unix time."""

    id: str
    label: str
    session: str
    paired: int
    seen: int


class RemoteAccess:
    """What remote access keeps between starts, and its state while on.

    ``restore`` reads what remote.json held, and ``stored`` gives what it
    should hold. The server's remote listener notes when it starts and stops
    listening, and which devices are connected. ``on_change`` is called at
    every change, for the interfaces and for saving. Everything runs in the
    server's event loop."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.port = DEFAULT_PORT
        self._code: str | None = None       # made at the first turn-on
        self._wrong = 0                     # wrong codes entered since the code was made
        self._devices: dict[str, Device] = {}
        self.authority: Authority | None = None     # made at the first turn-on
        # While on: where devices reach Radiolack, and the devices connected.
        self.on = False
        self.urls: list[str] = []
        self.problem = ""                   # why remote access could not turn on, or ""
        self._connected: Counter[str] = Counter()
        self.on_change: Callable[[], None] | None = None

    @property
    def blocked(self) -> bool:
        """Too many wrong codes: the code no longer works until a new one is made."""
        return self._wrong >= MAX_WRONG_CODES

    def describe(self) -> dict[str, Any]:
        """The state that this computer's interfaces show (docs/design/protocol.md):
        the code only while on."""
        devices = sorted(self._devices.values(), key=lambda device: (device.paired, device.id))
        return {
            "on": self.on,
            "port": self.port,
            "urls": list(self.urls),
            "code": format_code(self._code) if self.on and self._code is not None else None,
            "blocked": self.blocked,
            "devices": [{"id": device.id, "label": device.label, "paired": device.paired, "seen": device.seen,
                         "connected": self._connected[device.id] > 0} for device in devices],
            "fingerprint": self.authority.fingerprint if self.authority is not None else None,
            "problem": self.problem,
        }

    def stored(self) -> dict[str, Any]:
        """What remote.json holds: never whether remote access is on."""
        authority = self.authority
        return {
            "port": self.port,
            "code": self._code,
            "wrong_codes": self._wrong,
            "devices": [dataclasses.asdict(device) for device in self._devices.values()],
            "authority": dataclasses.asdict(authority) if authority is not None else None,
        }

    def restore(self, saved: Mapping[str, Any]) -> None:
        """Start from what remote.json held, leaving out what is not valid: a
        code with an unreadable count of wrong codes stays blocked."""
        if not saved:
            return
        port = saved.get("port", DEFAULT_PORT)
        if _whole(port) and MIN_PORT <= port <= 65535:
            self.port = port
        else:
            log.warning("the saved remote access port is not used")
        code = saved.get("code")
        if isinstance(code, str) and len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET):
            self._code = code
            wrong = saved.get("wrong_codes")
            if _whole(wrong) and wrong >= 0:
                self._wrong = min(wrong, MAX_WRONG_CODES)
            else:
                log.warning("the saved count of wrong access codes is not valid: the code is blocked")
                self._wrong = MAX_WRONG_CODES
        elif code is not None:
            log.warning("the saved access code is not valid: a new one will be made")
        devices = saved.get("devices", [])
        for entry in devices if isinstance(devices, list) else []:
            device = _saved_device(entry)
            if device is not None and len(self._devices) < MAX_DEVICES:
                self._devices[device.id] = device
        if not isinstance(devices, list) or len(self._devices) < len(devices):
            log.warning("some saved remote devices are not used")
        authority = saved.get("authority")
        if authority is not None:
            self.authority = _saved_authority(authority)
            if self.authority is None:
                log.warning("the saved local authority is not valid: a new one will be made, "
                            "which devices must install again")

    def prepare(self, host: str) -> Authority:
        """Before turning on: make the code and the authority if there are none
        yet, and forget the devices unused for too long."""
        changed = self._expire()
        if self._code is None:
            self._code, self._wrong = new_code(), 0
            changed = True
        if self.authority is None:
            self.authority = create_authority(host)
            changed = True
        if changed:
            self._changed()
        return self.authority

    def set_port(self, port: object) -> None:
        """Listen on ``port`` at the next turn-on. Raises ValueError when refused."""
        if self.on:
            raise ValueError("the port changes only while remote access is off")
        if not _whole(port) or not MIN_PORT <= port <= 65535:
            raise ValueError(f"the port must be a whole number from {MIN_PORT} to 65535")
        if port != self.port:
            self.port = port
            self._changed()

    def renew_code(self) -> list[str]:
        """A new code, which no wrong code has been tried against: every device
        is forgotten and must enter it. Returns the forgotten devices' ids."""
        forgotten = list(self._devices)
        self._code, self._wrong = new_code(), 0
        self._devices.clear()
        self._changed()
        return forgotten

    def forget(self, device_id: object) -> None:
        """Forget a device: it must enter the code again. Raises ValueError for
        an unknown device."""
        if not isinstance(device_id, str) or device_id not in self._devices:
            raise ValueError("no such device: it may have been forgotten already")
        del self._devices[device_id]
        self._changed()

    def check_code(self, text: object) -> bool:
        """Whether ``text`` is the access code, ignoring case, spaces and dashes.
        A wrong code counts towards the block; a blocked code is always wrong."""
        if self.blocked or self._code is None:
            return False
        given = re.sub(r"[\s-]", "", text).upper() if isinstance(text, str) else ""
        if hmac.compare_digest(given.encode("utf-8"), self._code.encode("ascii")):
            return True
        self._wrong += 1
        self._changed()
        return False

    def pair(self, label: str) -> str:
        """Pair a device that entered the right code: returns its session token.
        Raises ValueError when too many devices are paired."""
        self._expire()
        if len(self._devices) >= MAX_DEVICES:
            raise ValueError("too many devices are paired: forget one in Radiolack's settings on the computer")
        token = secrets.token_urlsafe(32)
        device_id = secrets.token_hex(4)
        while device_id in self._devices:
            device_id = secrets.token_hex(4)
        now = int(self._clock())
        self._devices[device_id] = Device(device_id, label[:_MAX_LABEL], _digest(token), now, now)
        self._changed()
        return token

    def session(self, token: str | None) -> Device | None:
        """The paired device whose session ``token`` is, or None."""
        if not token or len(token) > _MAX_TOKEN:
            return None
        digest = _digest(token)
        for device in self._devices.values():
            if hmac.compare_digest(device.session, digest):
                if self._clock() - device.seen > DEVICE_EXPIRY_S:
                    self._expire()
                    return None
                return device
        return None

    def connected(self, device: Device) -> None:
        """A device opened a stream."""
        self._connected[device.id] += 1
        self._seen(device)
        self._changed()

    def disconnected(self, device: Device) -> None:
        """A device's stream closed."""
        self._connected[device.id] -= 1
        if self._connected[device.id] <= 0:
            del self._connected[device.id]
        self._seen(device)
        self._changed()

    def started(self, urls: Sequence[str]) -> None:
        """Remote access listens: devices open one of ``urls``."""
        self.on, self.urls, self.problem = True, list(urls), ""
        self._changed()

    def stopped(self, problem: str = "") -> None:
        """Remote access no longer listens, for ``problem`` when it could not
        turn on."""
        self.on, self.urls, self.problem = False, [], problem
        self._connected.clear()
        self._changed()

    def _seen(self, device: Device) -> None:
        now = int(self._clock())
        if now - device.seen >= _SEEN_STEP_S:
            device.seen = now

    def _expire(self) -> bool:
        """Forget the devices unused for too long; returns whether any was."""
        now = self._clock()
        expired = [key for key, device in self._devices.items()
                   if now - device.seen > DEVICE_EXPIRY_S and not self._connected[key]]
        for key in expired:
            del self._devices[key]
        if expired:
            self._changed()
        return bool(expired)

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    """The code as the interface shows it: two groups of four."""
    return f"{code[:4]}-{code[4:]}"


def host_name() -> str:
    """This computer's name as mDNS announces it, without ``.local``: the first
    label of its host name, or "" when that cannot be a DNS label."""
    name = socket.gethostname().split(".")[0].lower()
    return name if _LABEL.fullmatch(name) else ""


def remote_urls(host: str, addresses: Sequence[str], port: int) -> list[str]:
    """Where devices reach remote access: by each address, then by this
    computer's mDNS name, which also leads to its IPv6 addresses, if any,
    where remote access does not listen: an iPhone then fails to open it."""
    return [f"https://{name}:{port}/" for name in list(addresses) + ([f"{host}.local"] if host else [])]


def is_private(address: str) -> bool:
    """Whether ``address`` lies in one of the networks remote access serves."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return isinstance(ip, ipaddress.IPv4Address) and any(ip in network for network in PRIVATE_NETWORKS)


def private_addresses() -> list[str]:
    """This computer's private IPv4 addresses, on the network interfaces that
    are up: where remote access listens. Home networks' ranges come first,
    as a VPN's address is of little use to a phone on the home network."""
    found = set(_interface_addresses())
    primary = _default_route_address()      # the only one found where ioctl cannot list them
    if primary is not None:
        found.add(primary)

    def rank(address: str) -> tuple[int, ipaddress.IPv4Address]:
        ip = ipaddress.IPv4Address(address)
        return next(index for index, network in enumerate(_ADDRESS_ORDER) if ip in network), ip

    return sorted((address for address in found if is_private(address)
                   and not ipaddress.ip_address(address).is_loopback), key=rank)


def _default_route_address() -> str | None:
    """The address this computer sends from by default, or None without a route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))     # TEST-NET-1: connecting a UDP socket sends nothing
            return probe.getsockname()[0]
        except OSError:
            return None


def _interface_addresses() -> list[str]:
    """The primary IPv4 address of each network interface that is up, apart
    from loopback; on Linux only, where ioctl reads them."""
    if not sys.platform.startswith("linux"):
        return []
    import fcntl     # POSIX only

    addresses = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in socket.if_nameindex():
            request = struct.pack("256s", name.encode("utf-8")[:15])    # struct ifreq
            try:
                flags = struct.unpack_from("H", fcntl.ioctl(probe.fileno(), _SIOCGIFFLAGS, request), 16)[0]
                if flags & _IFF_LOOPBACK or flags & (_IFF_UP | _IFF_RUNNING) != _IFF_UP | _IFF_RUNNING:
                    continue
                addresses.append(socket.inet_ntoa(fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, request)[20:24]))
            except OSError:
                continue        # gone, or without an IPv4 address
    return addresses


def device_label(user_agent: str) -> str:
    """A device's name for the list of paired devices, such as "iPhone ·
    Safari", from its browser's User-Agent: only known words, never the
    header's own text."""
    agent = user_agent[:1000]
    kinds = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android device"), ("Macintosh", "Mac"),
             ("Windows", "Windows computer"), ("CrOS", "Chromebook"), ("Linux", "Linux computer"),
             ("X11", "computer"))
    browsers = (("Firefox/", "Firefox"), ("FxiOS/", "Firefox"), ("EdgiOS/", "Edge"), ("Edg/", "Edge"),
                ("EdgA/", "Edge"), ("OPR/", "Opera"), ("CriOS/", "Chrome"), ("Chrome/", "Chrome"),
                ("Safari/", "Safari"))
    kind = next((name for word, name in kinds if word in agent), "Device")
    browser = next((name for word, name in browsers if word in agent), None)
    return f"{kind} \N{MIDDLE DOT} {browser}" if browser else kind


def create_authority(host: str, now: datetime.datetime | None = None) -> Authority:
    """A new local authority for this computer, named after ``host``: the root
    signs the intermediate, then its key is dropped."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID

    now = now or datetime.datetime.now(datetime.timezone.utc)
    start, end = now - _CLOCK_SKEW, now + datetime.timedelta(days=_AUTHORITY_DAYS)
    owner = f"Radiolack on {host}" if host else "Radiolack"
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name(owner)
    root = (x509.CertificateBuilder()
            .subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(_key_usage(certificate_signing=True), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    permitted = ([x509.IPAddress(network) for network in PRIVATE_NETWORKS]
                 + [x509.DNSName(domain) for domain in LOCAL_DOMAINS])
    intermediate = (x509.CertificateBuilder()
                    .subject_name(_name(f"{owner}: server certificates")).issuer_name(root_name)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(start).not_valid_after(end)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .add_extension(_key_usage(certificate_signing=True), critical=True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None),
                                   critical=True)
                    .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                                   critical=False)
                    .sign(root_key, hashes.SHA256()))
    del root_key        # never written anywhere: nothing else can be signed by the root
    return Authority(root=_pem(root), certificate=_pem(intermediate), key=_key_pem(key))


def issue_server_certificate(authority: Authority, names: Sequence[str], addresses: Sequence[str],
                             now: datetime.datetime | None = None) -> tuple[bytes, bytes]:
    """A new key and certificate for the server, for DNS ``names`` and IP
    ``addresses``, signed by the authority's intermediate: returns the chain,
    server certificate first, and the key, in PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID

    now = now or datetime.datetime.now(datetime.timezone.utc)
    issuer, issuer_key = _intermediate(authority)
    key = ec.generate_private_key(ec.SECP256R1())
    alternatives = ([x509.DNSName(name) for name in names]
                    + [x509.IPAddress(ipaddress.ip_address(address)) for address in addresses])
    certificate = (x509.CertificateBuilder()
                   .subject_name(_name("Radiolack")).issuer_name(issuer.subject)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - _CLOCK_SKEW).not_valid_after(now + datetime.timedelta(days=_SERVER_DAYS))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(_key_usage(certificate_signing=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                                  critical=False)
                   .sign(issuer_key, hashes.SHA256()))
    chain = (_pem(certificate) + authority.certificate).encode("ascii")
    return chain, _key_pem(key).encode("ascii")


def server_context(chain: bytes, key: bytes) -> ssl.SSLContext:
    """A TLS server context for the certificate ``chain`` and its ``key``, in
    PEM. Python reads them only from a file: on Linux an anonymous file in
    memory, elsewhere a private temporary file, removed at once."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    content = key + chain
    if hasattr(os, "memfd_create"):
        with open(os.memfd_create("radiolack-tls", os.MFD_CLOEXEC), "wb") as memory:
            memory.write(content)
            memory.flush()
            with contextlib.suppress(OSError):   # such as without /proc: a temporary file instead
                context.load_cert_chain(f"/proc/self/fd/{memory.fileno()}")
                return context
    with tempfile.TemporaryDirectory(prefix="radiolack-tls-") as folder:     # readable by this user only
        path = Path(folder) / "server.pem"
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            file.write(content)
        context.load_cert_chain(path)
    return context


def _intermediate(authority: Authority):
    """The authority's intermediate certificate and key, read: raises
    ValueError when they cannot be read or do not belong together."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    certificate = x509.load_pem_x509_certificate(authority.certificate.encode("ascii"))
    key = serialization.load_pem_private_key(authority.key.encode("ascii"), password=None)
    public = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if key.public_key().public_bytes(*public) != certificate.public_key().public_bytes(*public):
        raise ValueError("the intermediate key does not belong to its certificate")
    return certificate, key


def _name(common_name: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Radiolack")])


def _key_usage(certificate_signing: bool):
    from cryptography import x509

    return x509.KeyUsage(digital_signature=not certificate_signing, content_commitment=False,
                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                         key_cert_sign=certificate_signing, crl_sign=certificate_signing,
                         encipher_only=False, decipher_only=False)


def _pem(certificate) -> str:
    from cryptography.hazmat.primitives import serialization

    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _key_pem(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _saved_device(entry: object) -> Device | None:
    if not isinstance(entry, dict):
        return None
    with contextlib.suppress(KeyError, TypeError, ValueError):
        device = Device(**{field.name: entry[field.name] for field in dataclasses.fields(Device)})
        if (isinstance(device.id, str) and _DEVICE_ID.fullmatch(device.id)
                and isinstance(device.label, str) and 0 < len(device.label) <= _MAX_LABEL
                and isinstance(device.session, str) and _DIGEST.fullmatch(device.session)
                and _whole(device.paired) and _whole(device.seen)):
            return device
    return None


def _saved_authority(saved: object) -> Authority | None:
    if not isinstance(saved, dict):
        return None
    root, certificate, key = (saved.get(name) for name in ("root", "certificate", "key"))
    if not (isinstance(root, str) and root.startswith(_PEM_CERTIFICATE)
            and isinstance(certificate, str) and certificate.startswith(_PEM_CERTIFICATE)
            and isinstance(key, str) and key.startswith(_PEM_KEY)):
        return None
    authority = Authority(root=root, certificate=certificate, key=key)
    try:
        authority.root_der()
        _intermediate(authority)
    except ImportError:
        pass        # turning on, which needs cryptography, says so
    except Exception:   # whatever the file holds: cryptography raises more than ValueError
        return None
    return authority
