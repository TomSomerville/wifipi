"""Node identity: keys, node_id, and announces.

A node_id is the truncated hash of a public key, so an address is not assigned
by anyone -- generating a key *is* getting an address. That is what lets nodes
meet with no registry, no coordinator and no prior contact.

    node_id = SHA-256(ed25519_public_key)[:16]

An announce carries the keys and is signed. A receiver checks two things:

    1. SHA-256(ed25519_pub)[:16] == the src in the header
       -> this key really does produce this address
    2. the signature verifies against that key
       -> the sender really holds the private half

Check 1 alone is not enough, because public keys are public: anyone could copy
one out of an announce they overheard. Check 2 is the part that cannot be
faked without the private key.

Freshness comes from 10 random bytes inside the signature, so no two announces
are identical -- the same approach Reticulum takes. That does not stop a
verbatim replay on its own; the dedup cache flooding already needs is what
catches those. Deliberately no counter: it would have to be persisted as
carefully as the private key, and a reset would get every announce rejected.
"""

import hashlib
import os
import stat

from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.exceptions import InvalidSignature

ID_LEN = 16
KEY_LEN = 32
RANDOM_LEN = 10
SIG_LEN = 64

# ed25519 pub | x25519 pub | random | signature | app_data
ANNOUNCE_MIN = KEY_LEN * 2 + RANDOM_LEN + SIG_LEN


class BadAnnounce(Exception):
    """Failed verification. Never trust the contents after this."""


def node_id_from_key(ed25519_pub: bytes) -> bytes:
    return hashlib.sha256(ed25519_pub).digest()[:ID_LEN]


class Identity:
    """This node's keys. The private halves never leave the device."""

    def __init__(self, sign_key: ed25519.Ed25519PrivateKey,
                 encrypt_key: x25519.X25519PrivateKey):
        self._sign = sign_key
        self._encrypt = encrypt_key
        self.sign_pub = sign_key.public_key().public_bytes_raw()
        self.encrypt_pub = encrypt_key.public_key().public_bytes_raw()
        self.node_id = node_id_from_key(self.sign_pub)

    # ---- lifecycle ----

    @classmethod
    def generate(cls):
        return cls(ed25519.Ed25519PrivateKey.generate(),
                   x25519.X25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path):
        """Keys persist, so a node keeps its address across restarts."""
        if os.path.exists(path):
            return cls.load(path)
        ident = cls.generate()
        ident.save(path)
        return ident

    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            blob = f.read()
        if len(blob) != KEY_LEN * 2:
            raise ValueError(f"{path}: expected {KEY_LEN * 2} bytes, got {len(blob)}")
        return cls(
            ed25519.Ed25519PrivateKey.from_private_bytes(blob[:KEY_LEN]),
            x25519.X25519PrivateKey.from_private_bytes(blob[KEY_LEN:]),
        )

    def save(self, path):
        blob = (self._sign.private_bytes_raw()
                + self._encrypt.private_bytes_raw())
        # Create with 0600 from the start; writing then chmod leaves a window
        # where the private key is world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)

    # ---- signing ----

    def sign(self, data: bytes) -> bytes:
        return self._sign.sign(data)

    def shared_secret(self, peer_x25519_pub: bytes) -> bytes:
        """Raw ECDH output. Run it through a KDF before using as a key."""
        peer = x25519.X25519PublicKey.from_public_bytes(peer_x25519_pub)
        return self._encrypt.exchange(peer)

    # ---- announces ----

    def build_announce(self, app_data: bytes = b"") -> bytes:
        """Payload for a TYPE_ANNOUNCE packet sent with src = self.node_id."""
        random = os.urandom(RANDOM_LEN)
        signed = _announce_signed_bytes(
            self.node_id, self.sign_pub, self.encrypt_pub, random, app_data)
        sig = self.sign(signed)
        return self.sign_pub + self.encrypt_pub + random + sig + app_data

    def __repr__(self):
        return f"<Identity {self.node_id.hex()}>"


def _announce_signed_bytes(src, sign_pub, encrypt_pub, random, app_data):
    # src is included so a valid announce cannot be lifted onto another
    # address, and app_data so it cannot be tampered with in flight.
    return src + sign_pub + encrypt_pub + random + app_data


def parse_announce(src: bytes, payload: bytes) -> dict:
    """Verify an announce. Raises BadAnnounce if anything fails."""
    if len(payload) < ANNOUNCE_MIN:
        raise BadAnnounce(f"short announce: {len(payload)} < {ANNOUNCE_MIN}")

    off = 0
    sign_pub = payload[off:off + KEY_LEN]; off += KEY_LEN
    encrypt_pub = payload[off:off + KEY_LEN]; off += KEY_LEN
    random = payload[off:off + RANDOM_LEN]; off += RANDOM_LEN
    sig = payload[off:off + SIG_LEN]; off += SIG_LEN
    app_data = payload[off:]

    # 1. does this key actually produce the address being claimed?
    if node_id_from_key(sign_pub) != src:
        raise BadAnnounce("node_id does not match the announced key")

    # 2. does the sender hold the private half?
    signed = _announce_signed_bytes(src, sign_pub, encrypt_pub, random, app_data)
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(sign_pub).verify(sig, signed)
    except InvalidSignature:
        raise BadAnnounce("signature does not verify")

    return {"node_id": src, "sign_pub": sign_pub,
            "encrypt_pub": encrypt_pub, "app_data": app_data}
