"""A software passkey for tests: builds real WebAuthn responses (P-256 / ES256, "none" attestation), so the API's
checks run through py_webauthn's real verification, signatures included."""
import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftKey:
    def __init__(self, rp_id: str, origin: str, user_verified: bool = True):
        self.rp_id, self.origin, self.uv = rp_id, origin, user_verified
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)      # UP, UV, AT

    def _client(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str | None = None) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([self._flags(True)]) + struct.pack(">I", 0)
                + b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(self._client("webauthn.create", options["challenge"], origin)),
                             "attestationObject": b64(att)}}

    def get(self, options: dict, origin: str | None = None, key=None) -> dict:
        self.count += 1
        auth = hashlib.sha256(self.rp_id.encode()).digest() + bytes([self._flags(False)]) + struct.pack(">I", self.count)
        client = self._client("webauthn.get", options["challenge"], origin)
        sig = (key or self.key).sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.cred_id), "rawId": b64(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64(client), "authenticatorData": b64(auth), "signature": b64(sig),
                             "userHandle": None}}
