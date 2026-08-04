from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from abc import ABC, abstractmethod
from typing import Optional


class RemoteError(Exception):
    """Raised when a git operation cannot be completed."""


class Remote(ABC):
    """A backend that versions and syncs the encrypted store."""

    @abstractmethod
    def initialize(self) -> None:
        ...

    @abstractmethod
    def clone(self) -> None:
        ...

    @abstractmethod
    def commit(self, message: str) -> bool:
        ...

    @abstractmethod
    def push(self) -> None:
        ...

    @abstractmethod
    def pull(self, force: bool = False) -> None:
        ...


class GitRemote(Remote):
    """Versions the store with git and syncs it to a remote.

    Unlike a vault file dropped into someone else's repository, this repository
    is fafnir's own: it is created inside the fafnir home and contains nothing
    but the encrypted store, so every operation can stage the whole tree and no
    unrelated working-tree state has to be preserved.

    fafnir never touches git credentials — it assumes the user's existing git
    setup can reach the remote.
    """

    def __init__(self, repo_dir: str, url: Optional[str], branch: str) -> None:
        self.repo_dir: str = repo_dir
        self.url: Optional[str] = url
        self.branch: str = branch

    # -- git plumbing -------------------------------------------------------

    @staticmethod
    def _ensure_git() -> None:
        if shutil.which("git") is None:
            raise RemoteError("git is not installed; it is required by fafnir")

    @staticmethod
    def _log(message: str) -> None:
        """Echo git activity to stderr so the user can audit what ran."""
        print(message, file=sys.stderr)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        self._log(f"$ git -C {self.repo_dir} {' '.join(args)}")
        result = subprocess.run(
            ["git", "-C", self.repo_dir, *args],
            capture_output=True,
            text=True,
        )
        # Log output only on success. A non-zero exit is either an expected
        # probe result (check=False) or a real failure surfaced below as a
        # RemoteError, so its output would just be confusing noise here.
        output = (result.stdout + result.stderr).strip()
        if output and result.returncode == 0:
            self._log(textwrap.indent(output, "  "))
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RemoteError(message or f"git {args[0]} failed")
        return result

    def _ref_exists(self, ref: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0

    def _has_commits(self) -> bool:
        return self._ref_exists("HEAD")

    def _require_url(self) -> str:
        if not self.url:
            raise RemoteError(
                "No remote configured. Run `fafnir config set remote <url>`."
            )
        return self.url

    def _sync_origin(self) -> None:
        if not self.url:
            return
        existing = self._git("remote", "get-url", "origin", check=False)
        if existing.returncode != 0:
            self._git("remote", "add", "origin", self.url)
        elif existing.stdout.strip() != self.url:
            self._git("remote", "set-url", "origin", self.url)

    # -- operations ---------------------------------------------------------

    def initialize(self) -> None:
        """Create the repository if needed and point it at the remote."""
        self._ensure_git()
        os.makedirs(self.repo_dir, exist_ok=True)
        # Check for `.git` directly rather than asking git: the fafnir home may
        # itself sit inside a repository (a dotfiles checkout), and `rev-parse
        # --is-inside-work-tree` would happily report that one.
        if not os.path.exists(os.path.join(self.repo_dir, ".git")):
            self._git("init")
            # Name the unborn branch without relying on `git init -b`.
            self._git("symbolic-ref", "HEAD", f"refs/heads/{self.branch}")
        self._sync_origin()

    def clone(self) -> None:
        """Bootstrap the store from an existing remote."""
        self._require_url()
        self.initialize()
        fetch = self._git("fetch", "origin", self.branch, check=False)
        if fetch.returncode != 0:
            raise RemoteError(
                f"Cannot fetch branch '{self.branch}' from {self.url}: "
                + (fetch.stderr.strip() or "unknown error")
            )
        self._git("checkout", "-B", self.branch, f"origin/{self.branch}")

    def commit(self, message: str) -> bool:
        """Record the whole encrypted store in one commit. False if unchanged."""
        self._ensure_git()
        self._git("add", "-A", "--", ".")
        # `diff --cached --quiet` exits non-zero when something is staged; on a
        # repo with no commits yet it compares against the empty tree.
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._git("commit", "-m", message)
        return True

    def push(self) -> None:
        self._ensure_git()
        self._require_url()
        if not self._has_commits():
            raise RemoteError("Nothing to push: the store has no commits yet")
        self._sync_origin()
        self._git("push", "-u", "origin", self.branch)

    def pull(self, force: bool = False) -> None:
        self._ensure_git()
        self._require_url()
        self._sync_origin()
        fetch = self._git("fetch", "origin", self.branch, check=False)
        if fetch.returncode != 0:
            raise RemoteError(
                f"Cannot fetch branch '{self.branch}' from {self.url}: "
                + (fetch.stderr.strip() or "unknown error")
            )
        remote_ref = f"origin/{self.branch}"
        if not self._ref_exists(remote_ref):
            raise RemoteError(f"The remote has no branch '{self.branch}'")

        if force or not self._has_commits():
            # Nothing local worth keeping (or the user asked to discard it).
            self._git("reset", "--hard", remote_ref)
            return
        merge = self._git("merge", "--ff-only", remote_ref, check=False)
        if merge.returncode != 0:
            raise RemoteError(
                "The local store has diverged from the remote (or has uncommitted "
                "changes); re-run with -f to discard the local state"
            )
