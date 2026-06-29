"""Generate a self-signed TLS cert for LAN use. No openssl needed.

Usage:
    python scripts/gen_cert.py [hostname_or_ip ...]

Defaults to localhost + 127.0.0.1 + this machine's hostname.
Outputs data/cert.pem and data/key.pem.
"""
import datetime
import ipaddress
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def san_entries(names):
    entries = []
    for n in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            entries.append(x509.DNSName(n))
    return entries


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    extra = sys.argv[1:]
    host = socket.gethostname()
    names = ["localhost", "127.0.0.1", host] + extra
    # de-dupe, keep order
    names = list(dict.fromkeys(n for n in names if n))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, host)]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_entries(names)), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    (DATA_DIR / "key.pem").write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (DATA_DIR / "cert.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    print("Wrote:")
    print(" ", DATA_DIR / "cert.pem")
    print(" ", DATA_DIR / "key.pem")
    print("Valid for hosts/IPs:", ", ".join(names))


if __name__ == "__main__":
    main()
