from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from typing import Optional

CONFIG_FILE = "config.toml"
IGNORE_FILE = "ignore"

DEFAULTS: dict[str, str] = {
    "branch": "main",
    "session-timeout": "900",
    # Just under GitHub's 100 MB hard limit, so an oversized file is refused
    # by fafnir with a clear message instead of failing at the git layer.
    "max-file-size": str(95 * 1024 * 1024),
}

# `remote` has no default (it is unset until the user configures one), but it
# is still a settable key.
VALID_KEYS: set[str] = set(DEFAULTS) | {"remote"}

# Keys whose value is written to TOML as a bare integer.
INT_KEYS: set[str] = {"session-timeout", "max-file-size"}

# Root names double as TOML bare keys and as portable identifiers inside the
# index, so keep them to a conservative character set.
ROOT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def home_dir() -> str:
    """The fafnir home: ``$XDG_CONFIG_HOME/fafnir``, else ``~/.config/fafnir``.

    Everything fafnir owns lives under it, so the CLI works from any working
    directory.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return os.path.join(xdg, "fafnir")
    return os.path.expanduser("~/.config/fafnir")


@dataclass
class Root:
    """A tracked source folder: a portable name bound to a local path.

    The index only ever stores ``(root name, relative path)``, so the store
    stays portable across machines and mount points; this binding is what makes
    a name concrete on *this* machine.
    """

    name: str
    path: str

    @property
    def available(self) -> bool:
        """False when the folder is missing (e.g. an unmounted drive)."""
        return os.path.isdir(self.path)

    def contains(self, path: str) -> bool:
        return path == self.path or path.startswith(self.path + os.sep)


def slugify(name: str) -> str:
    """Turn a folder basename into a usable root name."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug.lower() or "root"


def _quote(value: str) -> str:
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


class Config:
    """The per-machine ``config.toml``: roots, remote, branch, settings.

    Local by design and never committed — it is the only place absolute paths
    exist, and the same store cloned on another machine gets a different one.
    """

    def __init__(self, home: Optional[str] = None) -> None:
        self.home: str = home if home is not None else home_dir()
        self.path: str = os.path.join(self.home, CONFIG_FILE)
        self.ignore_path: str = os.path.join(self.home, IGNORE_FILE)
        self._data: dict[str, str] = {}
        self._roots: list[Root] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            data = tomllib.load(f)
        for key, value in data.items():
            if key == "roots":
                continue
            self._data[key] = str(value)
        for name, path in (data.get("roots") or {}).items():
            self._roots.append(Root(name=name, path=str(path)))

    def _save(self) -> None:
        lines = []
        for key, value in sorted(self._data.items()):
            if key in INT_KEYS:
                lines.append(f"{key} = {int(value)}")
            else:
                lines.append(f"{key} = {_quote(value)}")
        if self._roots:
            lines.append("")
            lines.append("[roots]")
            for root in sorted(self._roots, key=lambda r: r.name):
                lines.append(f"{root.name} = {_quote(root.path)}")

        os.makedirs(self.home, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, self.path)

    # -- settings -----------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        if key in self._data:
            return self._data[key]
        return DEFAULTS.get(key)

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(f"Missing config value: {key!r}")
        return value

    def set(self, key: str, value: str) -> None:
        if key not in VALID_KEYS:
            raise KeyError(f"Unknown config key: {key!r}")
        if key in INT_KEYS:
            try:
                number = int(value)
            except ValueError:
                raise ValueError(f"{key} must be an integer, got {value!r}")
            if number < 0:
                raise ValueError(f"{key} must not be negative")
            value = str(number)
        self._data[key] = value
        self._save()

    def settings(self) -> dict[str, str]:
        result = dict(DEFAULTS)
        result.update(self._data)
        return result

    # -- roots --------------------------------------------------------------

    def roots(self) -> list[Root]:
        return sorted(self._roots, key=lambda r: r.name)

    def root(self, name: str) -> Optional[Root]:
        for root in self._roots:
            if root.name == name:
                return root
        return None

    def root_at(self, path: str) -> Optional[Root]:
        for root in self._roots:
            if root.path == path:
                return root
        return None

    def add_root(self, root: Root) -> None:
        if not ROOT_NAME_PATTERN.match(root.name):
            raise ValueError(f"Invalid root name: {root.name!r}")
        self._roots.append(root)
        self._save()

    def remove_root(self, name: str) -> bool:
        for i, root in enumerate(self._roots):
            if root.name == name:
                del self._roots[i]
                self._save()
                return True
        return False
