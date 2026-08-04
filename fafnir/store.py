from __future__ import annotations

import os
import re
from typing import Optional

from fafnir.crypto import CryptoError, Identity
from fafnir.model import Index

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BLOB_SUFFIX = ".age"


class StoreError(Exception):
    """Raised when the encrypted store cannot be read or written."""


class Store:
    """The encrypted store: the git working tree at ``<home>/repo``.

    Nothing readable lives here. The working tree is only hash-named blobs, the
    encrypted index, and the passphrase-protected identity — which is why the
    whole directory can be pushed to an untrusted remote, and why every
    human-facing diff has to be computed at the logical layer from the index.
    """

    REPO_DIR = "repo"
    BLOBS_DIR = "blobs"
    INDEX_FILE = "index.age"
    IDENTITY_FILE = "identity.age"

    def __init__(self, home: str) -> None:
        self.home: str = home
        self.repo_dir: str = os.path.join(home, self.REPO_DIR)
        self.blobs_dir: str = os.path.join(self.repo_dir, self.BLOBS_DIR)
        self.index_path: str = os.path.join(self.repo_dir, self.INDEX_FILE)
        self.identity_path: str = os.path.join(self.repo_dir, self.IDENTITY_FILE)

    def exists(self) -> bool:
        """True once a store has been initialized (or cloned) here."""
        return os.path.exists(self.identity_path)

    def create(self) -> None:
        os.makedirs(self.blobs_dir, exist_ok=True)

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        """Write via a temp file + rename, so a crash never truncates a file."""
        tmp = f"{path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    @staticmethod
    def _read_file(path: str, what: str) -> bytes:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as e:
            raise StoreError(f"Cannot read {what}: {e}")

    # -- identity -----------------------------------------------------------

    def write_identity(self, identity: Identity, passphrase: str) -> None:
        self._write_file(self.identity_path, identity.protect(passphrase))

    def unlock(self, passphrase: str) -> Identity:
        """Decrypt ``identity.age`` with the passphrase."""
        return Identity.unprotect(
            self._read_file(self.identity_path, self.IDENTITY_FILE), passphrase
        )

    # -- index --------------------------------------------------------------

    def read_index(self, identity: Identity) -> Index:
        raw = self._read_file(self.index_path, self.INDEX_FILE)
        try:
            return Index.from_json(identity.decrypt(raw))
        except CryptoError as e:
            raise StoreError(f"Cannot decrypt {self.INDEX_FILE}: {e}")
        except (ValueError, KeyError, TypeError) as e:
            raise StoreError(f"Corrupted {self.INDEX_FILE}: {e}")

    def write_index(self, identity: Identity, index: Index) -> None:
        self._write_file(self.index_path, identity.encrypt(index.to_json()))

    # -- blobs --------------------------------------------------------------

    def blob_path(self, fingerprint: str) -> str:
        # The index is decrypted input; a tampered entry must not be able to
        # point writes or reads outside the blobs directory.
        if not _FINGERPRINT_PATTERN.match(fingerprint):
            raise StoreError(f"Invalid blob id: {fingerprint!r}")
        return os.path.join(self.blobs_dir, fingerprint + _BLOB_SUFFIX)

    def has_blob(self, fingerprint: str) -> bool:
        return os.path.exists(self.blob_path(fingerprint))

    def blob_ids(self) -> list[str]:
        if not os.path.isdir(self.blobs_dir):
            return []
        return sorted(
            name[: -len(_BLOB_SUFFIX)]
            for name in os.listdir(self.blobs_dir)
            if name.endswith(_BLOB_SUFFIX)
        )

    def write_blob(self, identity: Identity, fingerprint: str, data: bytes) -> None:
        self._write_file(self.blob_path(fingerprint), identity.encrypt(data))

    def read_blob(self, identity: Identity, fingerprint: str) -> bytes:
        path = self.blob_path(fingerprint)
        raw = self._read_file(path, os.path.basename(path))
        try:
            return identity.decrypt(raw)
        except CryptoError as e:
            raise StoreError(f"Cannot decrypt blob {fingerprint[:12]}: {e}")

    def prune_blobs(self, keep: set[str]) -> list[str]:
        """Delete blobs no index entry references. Returns the removed ids.

        Content is deduplicated by fingerprint, so a blob is only orphaned once
        *every* entry pointing at it is gone. Git history still holds it — this
        just keeps the working tree in sync with the index.
        """
        removed = []
        for fingerprint in self.blob_ids():
            if fingerprint in keep:
                continue
            try:
                os.unlink(self.blob_path(fingerprint))
            except OSError:
                continue
            removed.append(fingerprint)
        return removed

    def size_on_disk(self) -> int:
        total = 0
        for fingerprint in self.blob_ids():
            try:
                total += os.path.getsize(self.blob_path(fingerprint))
            except OSError:
                continue
        return total


def restore_file(path: str, data: bytes, mtime: Optional[int] = None) -> None:
    """Write a decrypted document back into a source folder.

    Parent directories are created as needed (empty directories are not
    tracked, so they only exist because a file needs them). Permissions are not
    preserved by the store; restored files get non-world-readable defaults.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.fafnir-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    if mtime is not None:
        try:
            os.utime(path, (mtime, mtime))
        except OSError:
            pass
