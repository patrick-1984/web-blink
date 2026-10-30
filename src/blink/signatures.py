"""Detached Ed25519 verification against a separately pinned public key.

Signatures authenticate a catalog digest, never local grants or execution approval.
No private signing key is read or stored by this module.
"""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .errors import BlinkError


def public_key_fingerprint(public_key_hex):
    if not isinstance(public_key_hex, str) or not re.fullmatch(r"[0-9a-f]{64}", public_key_hex):
        raise BlinkError("signature_invalid", "Ed25519 public keys require 64 lowercase hexadecimal characters")
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()


def signature_capability():
    return {"algorithm": "Ed25519", "backend": "openssl",
            "available": shutil.which("openssl") is not None}


def verify_catalog_signature(public_key_hex, signature_hex, catalog_sha256):
    for value, size in ((public_key_hex, 64), (signature_hex, 128), (catalog_sha256, 64)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(size) + r"}", value):
            raise BlinkError("signature_invalid", "Invalid Ed25519 key, signature or catalog digest encoding")
    key = bytes.fromhex(public_key_hex)
    payload = ("web-blink:catalog-sha256:" + catalog_sha256).encode("ascii")
    with tempfile.TemporaryDirectory(prefix="blink-publisher-") as directory:
        root = Path(directory)
        # RFC 8410 SubjectPublicKeyInfo: Ed25519 OID, no parameters, 32-byte key.
        (root / "key.der").write_bytes(bytes.fromhex("302a300506032b6570032100") + key)
        (root / "signature").write_bytes(bytes.fromhex(signature_hex))
        (root / "payload").write_bytes(payload)
        try:
            result = subprocess.run([
                "openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
                "-inkey", str(root / "key.der"), "-sigfile", str(root / "signature"),
                "-rawin", "-in", str(root / "payload")], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BlinkError("signature_unavailable", "OpenSSL Ed25519 verification is unavailable") from exc
    if result.returncode:
        raise BlinkError("signature_invalid", "Publisher signature does not match the pinned catalog digest")
    return {"algorithm": "Ed25519", "keyFingerprint": public_key_fingerprint(public_key_hex),
            "catalogSha256": catalog_sha256}
