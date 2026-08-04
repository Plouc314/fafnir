from __future__ import annotations

import hashlib
import hmac

import pyrage
from pyrage import passphrase as age_passphrase
from pyrage import x25519

# Domain separator for the keyed fingerprint. Bumping it changes every blob id.
FINGERPRINT_CONTEXT = b"fafnir/fingerprint/v1"

PUBLIC_KEY_COMMENT = "# public key: "

# Files are fingerprinted by streaming, so a large document never has to be
# held in memory twice.
CHUNK_SIZE = 1024 * 1024


class CryptoError(Exception):
    """Raised when key parsing, encryption, or decryption fails."""


class Identity:
    """An unlocked age identity (X25519 keypair) and everything derived from it.

    fafnir writes no cryptography of its own: encryption is the age format via
    ``pyrage`` (bindings to rage, the Rust age implementation), so every blob it
    writes can also be decrypted by hand with the stock ``age`` CLI.

    Blobs are encrypted *to the public key*. age uses a fresh ephemeral key per
    encryption, so there is no nonce state for fafnir to get wrong and per-file
    encryption stays fast.
    """

    def __init__(self, secret: str) -> None:
        self.secret: str = secret.strip()
        try:
            self._identity = x25519.Identity.from_str(self.secret)
        except Exception:
            raise CryptoError("Invalid age identity")
        self.recipient: str = str(self._identity.to_public())
        self._recipient = x25519.Recipient.from_str(self.recipient)
        # A *plain* content hash would let anyone with repo access confirm that
        # a guessed document is stored (a confirmation oracle), which defeats
        # filename hiding. Keying the hash with a value only the identity holder
        # can derive leaks nothing while staying deterministic.
        self._fingerprint_key: bytes = hmac.new(
            self.secret.encode("utf-8"), FINGERPRINT_CONTEXT, hashlib.sha256
        ).digest()

    @classmethod
    def generate(cls) -> Identity:
        return cls(str(x25519.Identity.generate()))

    # -- fingerprints -------------------------------------------------------

    def fingerprint(self, data: bytes) -> str:
        """The blob id of ``data``: ``HMAC-SHA256(fp_key, plaintext)``."""
        return hmac.new(self._fingerprint_key, data, hashlib.sha256).hexdigest()

    def fingerprint_file(self, path: str) -> str:
        mac = hmac.new(self._fingerprint_key, digestmod=hashlib.sha256)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                mac.update(chunk)
        return mac.hexdigest()

    # -- encryption ---------------------------------------------------------

    def encrypt(self, data: bytes) -> bytes:
        try:
            return pyrage.encrypt(data, [self._recipient])
        except Exception as e:
            raise CryptoError(f"Encryption failed: {e}")

    def decrypt(self, data: bytes) -> bytes:
        try:
            return pyrage.decrypt(data, [self._identity])
        except Exception:
            raise CryptoError("Decryption failed: not encrypted to this identity")

    # -- identity file ------------------------------------------------------

    def to_text(self) -> str:
        """The identity in the standard age key-file format."""
        return f"{PUBLIC_KEY_COMMENT}{self.recipient}\n{self.secret}\n"

    @classmethod
    def from_text(cls, text: str) -> Identity:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return cls(line)
        raise CryptoError("No age identity found")

    def protect(self, passphrase: str) -> bytes:
        """Encrypt the identity file with a passphrase (age scrypt mode).

        The result is what gets committed as ``identity.age``: recoverable
        anywhere with the passphrase alone, and readable by ``age -d``.
        """
        try:
            return age_passphrase.encrypt(self.to_text().encode("utf-8"), passphrase)
        except Exception as e:
            raise CryptoError(f"Could not protect the identity: {e}")

    @classmethod
    def unprotect(cls, blob: bytes, passphrase: str) -> Identity:
        try:
            text = age_passphrase.decrypt(blob, passphrase)
        except Exception:
            raise CryptoError("Incorrect passphrase (or the identity file is corrupt)")
        return cls.from_text(text.decode("utf-8"))
