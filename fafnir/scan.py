from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

import pathspec

from fafnir.config import Root
from fafnir.crypto import Identity
from fafnir.model import Entry, Index

IGNORE_HEADER = """\
# fafnir ignore rules — gitignore syntax, applied to every tracked root.
# Paths are matched relative to each root, e.g.:
#
#   *.tmp
#   .DS_Store
#   drafts/
"""


class Ignore:
    """Exclusion rules in gitignore syntax.

    The source folders are not inside the git tree, so git's own ignore logic
    never sees them; `pathspec` re-implements the exact same matching.
    """

    def __init__(self, patterns: list[str]) -> None:
        self._spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    @classmethod
    def load(cls, path: str) -> Ignore:
        if not os.path.exists(path):
            return cls([])
        with open(path) as f:
            return cls(f.read().splitlines())

    def matches(self, path: str, is_dir: bool = False) -> bool:
        return self._spec.match_file(path + "/" if is_dir else path)


class SkipReason(Enum):
    SYMLINK = "symlink"
    SPECIAL = "special file"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too large"


@dataclass
class Skipped:
    """A path fafnir refuses to store, and why (see the file-fidelity scope)."""

    root: str
    path: str
    reason: SkipReason
    size: int = 0

    @property
    def logical_path(self) -> str:
        return f"{self.root}/{self.path}"


@dataclass
class ScannedFile:
    """A regular file found in a source folder."""

    root: Root
    path: str
    size: int
    mtime: int

    @property
    def abs_path(self) -> str:
        return os.path.join(self.root.path, self.path)


class ChangeKind(Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass
class Change:
    """One logical difference between the source folders and the index.

    Both sides are kept: ``scanned`` is what is on disk now, ``stored`` is what
    the index holds. push uses ``scanned`` (encrypt what is on disk), pull uses
    ``stored`` (write back what the store holds).
    """

    kind: ChangeKind
    root: str
    path: str
    abs_path: str
    scanned: Optional[Entry] = None
    stored: Optional[Entry] = None

    @property
    def logical_path(self) -> str:
        return f"{self.root}/{self.path}"

    @property
    def size(self) -> int:
        entry = self.scanned or self.stored
        return entry.size if entry else 0


@dataclass
class ChangeSet:
    """The full result of a scan, ready to be rendered or applied."""

    changes: list[Change] = field(default_factory=list)
    # A root whose folder is missing (e.g. an unmounted drive). Its files are
    # never reported as deletions — that is the one mistake that would wipe the
    # store on a bad day.
    unavailable: list[Root] = field(default_factory=list)
    # Roots the store knows about that this machine has not bound to a path.
    unbound: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    # Files whose content is unchanged but whose (size, mtime) drifted; folding
    # them into the index keeps the next scan from re-hashing them.
    refreshed: list[Entry] = field(default_factory=list)

    def of(self, kind: ChangeKind) -> list[Change]:
        return [c for c in self.changes if c.kind is kind]

    @property
    def added(self) -> list[Change]:
        return self.of(ChangeKind.ADDED)

    @property
    def modified(self) -> list[Change]:
        return self.of(ChangeKind.MODIFIED)

    @property
    def deleted(self) -> list[Change]:
        return self.of(ChangeKind.DELETED)

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def oversize(self) -> list[Skipped]:
        return [s for s in self.skipped if s.reason is SkipReason.TOO_LARGE]

    def upload_size(self) -> int:
        return sum(c.size for c in self.added + self.modified)


class Scanner:
    """Walks the source folders and classifies them against the index."""

    def __init__(
        self,
        roots: list[Root],
        ignore: Ignore,
        identity: Identity,
        max_file_size: int = 0,
    ) -> None:
        self.roots: list[Root] = roots
        self.ignore: Ignore = ignore
        self.identity: Identity = identity
        self.max_file_size: int = max_file_size

    def walk(self, root: Root) -> Iterator[Union[ScannedFile, Skipped]]:
        """Yield every storable file under ``root``, plus what was skipped."""
        for dirpath, dirnames, filenames in os.walk(root.path):
            rel_dir = os.path.relpath(dirpath, root.path)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")

            keep = []
            for name in sorted(dirnames):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if os.path.islink(os.path.join(dirpath, name)):
                    yield Skipped(root.name, rel, SkipReason.SYMLINK)
                    continue
                # Pruning an ignored directory matches git: a file cannot be
                # re-included once one of its parent directories is excluded.
                if self.ignore.matches(rel, is_dir=True):
                    continue
                keep.append(name)
            dirnames[:] = keep

            for name in sorted(filenames):
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if self.ignore.matches(rel):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    yield Skipped(root.name, rel, SkipReason.UNREADABLE)
                    continue
                if stat.S_ISLNK(st.st_mode):
                    yield Skipped(root.name, rel, SkipReason.SYMLINK)
                elif not stat.S_ISREG(st.st_mode):
                    yield Skipped(root.name, rel, SkipReason.SPECIAL)
                else:
                    yield ScannedFile(root, rel, st.st_size, int(st.st_mtime))

    def diff(self, index: Index) -> ChangeSet:
        """Classify every tracked file against the index."""
        result = ChangeSet()
        available: dict[str, Root] = {}
        seen: set[tuple[str, str]] = set()

        for root in self.roots:
            if not root.available:
                result.unavailable.append(root)
                continue
            available[root.name] = root

            for item in self.walk(root):
                if isinstance(item, Skipped):
                    result.skipped.append(item)
                    continue
                seen.add((root.name, item.path))
                entry = index.find(root.name, item.path)
                if entry is not None and entry.stat_matches(item.size, item.mtime):
                    continue
                if self.max_file_size and item.size > self.max_file_size:
                    result.skipped.append(
                        Skipped(root.name, item.path, SkipReason.TOO_LARGE, item.size)
                    )
                    continue

                fingerprint = self.identity.fingerprint_file(item.abs_path)
                scanned = Entry(
                    root=root.name,
                    path=item.path,
                    fingerprint=fingerprint,
                    size=item.size,
                    mtime=item.mtime,
                )
                if entry is None:
                    result.changes.append(
                        Change(
                            ChangeKind.ADDED,
                            root.name,
                            item.path,
                            item.abs_path,
                            scanned=scanned,
                        )
                    )
                elif entry.fingerprint != fingerprint:
                    result.changes.append(
                        Change(
                            ChangeKind.MODIFIED,
                            root.name,
                            item.path,
                            item.abs_path,
                            scanned=scanned,
                            stored=entry,
                        )
                    )
                else:
                    result.refreshed.append(scanned)

        configured = {root.name for root in self.roots}
        for entry in index.entries:
            if (entry.root, entry.path) in seen:
                continue
            if entry.root not in configured:
                if entry.root not in result.unbound:
                    result.unbound.append(entry.root)
                continue
            root = available.get(entry.root)
            if root is None:
                # The root exists in the config but its folder is missing: the
                # file is not gone, the drive is.
                continue
            result.changes.append(
                Change(
                    ChangeKind.DELETED,
                    entry.root,
                    entry.path,
                    os.path.join(root.path, entry.path),
                    stored=entry,
                )
            )
        return result
