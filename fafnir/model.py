from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

INDEX_VERSION = 1


@dataclass
class Entry:
    """One tracked file version, as recorded in the index.

    ``root``/``path`` is the file's logical identity (never an absolute path);
    ``fingerprint`` is both the blob id and the change-detection primitive;
    ``size``/``mtime`` short-circuit re-hashing on scan and restore the mtime on
    pull.
    """

    root: str
    path: str
    fingerprint: str
    size: int
    mtime: int

    @property
    def logical_path(self) -> str:
        return f"{self.root}/{self.path}"

    def stat_matches(self, size: int, mtime: int) -> bool:
        return self.size == size and self.mtime == mtime


class Index:
    """The decrypted store index — the pivot of the whole system.

    Real paths exist only here (blobs are hash-named), every logical diff is
    computed against it, and ``verify`` checks blobs against its fingerprints.
    It also remembers the named roots the store knows about, so ``clone`` can
    rebind them on a fresh machine.
    """

    def __init__(
        self,
        entries: Optional[Iterable[Entry]] = None,
        roots: Optional[Iterable[str]] = None,
        version: int = INDEX_VERSION,
    ) -> None:
        self.version: int = version
        self.roots: list[str] = sorted(set(roots or []))
        # Keyed by logical identity: a scan does one lookup per file, so this
        # must not be a linear search.
        self._entries: dict[tuple[str, str], Entry] = {}
        for entry in entries or []:
            self.put(entry)

    @property
    def entries(self) -> list[Entry]:
        return sorted(self._entries.values(), key=lambda e: (e.root, e.path))

    def find(self, root: str, path: str) -> Optional[Entry]:
        return self._entries.get((root, path))

    def put(self, entry: Entry) -> None:
        self._entries[(entry.root, entry.path)] = entry
        if entry.root not in self.roots:
            self.roots = sorted(self.roots + [entry.root])

    def remove(self, root: str, path: str) -> bool:
        return self._entries.pop((root, path), None) is not None

    def for_root(self, name: str) -> list[Entry]:
        return [e for e in self.entries if e.root == name]

    def fingerprints(self) -> set[str]:
        return {e.fingerprint for e in self._entries.values()}

    def set_roots(self, names: Iterable[str]) -> None:
        """Record the named roots known to the store (config + in-use names)."""
        self.roots = sorted(set(names) | {e.root for e in self._entries.values()})

    def to_json(self) -> bytes:
        data = {
            "version": self.version,
            "roots": self.roots,
            "entries": [
                {
                    "root": e.root,
                    "path": e.path,
                    "fingerprint": e.fingerprint,
                    "size": e.size,
                    "mtime": e.mtime,
                }
                for e in self.entries
            ],
        }
        return json.dumps(data).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> Index:
        obj = json.loads(data.decode("utf-8"))
        version = int(obj.get("version", INDEX_VERSION))
        if version > INDEX_VERSION:
            raise ValueError(
                f"Unsupported index version {version}; upgrade fafnir to read this store"
            )
        entries = [
            Entry(
                root=e["root"],
                path=e["path"],
                fingerprint=e["fingerprint"],
                size=int(e["size"]),
                mtime=int(e["mtime"]),
            )
            for e in obj.get("entries", [])
        ]
        return cls(entries=entries, roots=obj.get("roots", []), version=version)
