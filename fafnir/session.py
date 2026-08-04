from __future__ import annotations

import os
import stat
import time
from typing import Optional

from fafnir.crypto import CryptoError, Identity

DEFAULT_SESSION_TIMEOUT = 900
SESSION_FILE = "session"

# An age key file is ~150 bytes; anything larger is not one.
_MAX_SESSION_SIZE = 4096


class Session:
    """The unlocked identity, cached between commands.

    The cache is a plain age key file (mode ``0600``) inside the fafnir home,
    so `age -d -i <home>/session blobs/<id>.age` works by hand while a session
    is open. The passphrase itself is never stored — only the identity it
    unlocks, exactly as `age-keygen` would have written it.

    Last access is the file's mtime, refreshed on every resume, which is what
    the idle timeout is measured against.
    """

    def __init__(self, path: str, identity: Identity) -> None:
        self.path: str = path
        self.identity: Identity = identity

    @staticmethod
    def path_for(home: str) -> str:
        return os.path.join(home, SESSION_FILE)

    @classmethod
    def resume(
        cls, home: str, timeout: int = DEFAULT_SESSION_TIMEOUT
    ) -> Optional[Session]:
        """Return the active session, or None if there is none or it expired."""
        path = cls.path_for(home)
        # O_NOFOLLOW plus an fstat ownership/type check: the session file is the
        # unlocked identity, so never follow a symlink someone else planted.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                return None
            data = os.read(fd, _MAX_SESSION_SIZE)
        finally:
            os.close(fd)

        if timeout > 0 and time.time() - st.st_mtime > timeout:
            cls.lock(home)
            return None

        try:
            identity = Identity.from_text(data.decode("utf-8"))
        except (CryptoError, UnicodeDecodeError):
            # An unreadable cache must not lock the user out: drop it and let
            # the next command prompt for the passphrase.
            cls.lock(home)
            return None

        session = cls(path, identity)
        session.touch()
        return session

    @classmethod
    def start(cls, home: str, identity: Identity) -> Session:
        """Cache an unlocked identity and return the resulting session."""
        os.makedirs(home, exist_ok=True)
        path = cls.path_for(home)
        # Unlink first, then create with O_NOFOLLOW, so a pre-planted symlink
        # makes the open fail instead of writing the secret through it.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, identity.to_text().encode("utf-8"))
        finally:
            os.close(fd)
        return cls(path, identity)

    def touch(self) -> None:
        """Refresh the last-access time the idle timeout is measured against."""
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    @staticmethod
    def lock(home: str) -> None:
        """Drop the cached identity (``fafnir lock``)."""
        try:
            os.unlink(Session.path_for(home))
        except FileNotFoundError:
            pass
