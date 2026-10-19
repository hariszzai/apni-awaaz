"""Self-signed HTTPS certificate for testing on phones over the local network.

Phone browsers only allow the microphone on https pages. The certificate covers localhost
and this laptop's current network IP, and is remade when that IP changes (e.g. a new hotspot).
Browsers will warn that it is not trusted; tap "Advanced" -> "Proceed" once per phone.
"""
import datetime
import ipaddress
import json
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALID_DAYS = 60


def lan_ip() -> str | None:
    """The IP other devices on the same Wi-Fi/hotspot use to reach this laptop."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the route
            return s.getsockname()[0]
    except OSError:
        return None


def ensure_cert(folder: Path) -> tuple[Path, Path, str | None]:
    """Return (cert, key, lan_ip), creating a new certificate if needed."""
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, info_path = folder / "cert.pem", folder / "key.pem", folder / "info.json"
    ip = lan_ip()

    if cert_path.exists() and key_path.exists() and info_path.exists():
        info = json.loads(info_path.read_text())
        expires = datetime.datetime.fromisoformat(info["expires"])
        if info.get("ip") == ip and expires > datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1):
            return cert_path, key_path, ip

    key = ec.generate_private_key(ec.SECP256R1())
    names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip:
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apni Awaaz local test")])
    now = datetime.datetime.now(datetime.UTC)
    expires = now + datetime.timedelta(days=VALID_DAYS)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(expires)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    info_path.write_text(json.dumps({"ip": ip, "expires": expires.isoformat()}))
    return cert_path, key_path, ip
