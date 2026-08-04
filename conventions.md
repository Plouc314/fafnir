# Coding Conventions

## Imports

All imports are declared at the top of the file, in the following order, separated by a blank line:

1. Standard library
2. Third-party (`pyrage`, `pathspec`)
3. Local modules

No inline or deferred imports.

## Type Annotations

All function signatures (parameters and return types) and class attributes carry type annotations.

Target: **Python 3.11** (the floor is `tomllib`, nothing else). Annotation style is kept identical
to its sibling project [mimir](https://github.com/Plouc314/mimir), so the two codebases read the
same:

- Use built-in generic types: `list[str]`, `dict[str, int]`, `tuple[str, ...]`
- Use `Optional[X]` and `Union[X, Y]` from `typing`
- Use `from __future__ import annotations` at the top of every module

```python
from __future__ import annotations

from typing import Optional

def root(self, name: str) -> Optional[Root]:
    ...
```

## Classes

Define a class for each distinct concept in the domain (e.g. `Identity`, `Index`, `Entry`, `Root`,
`Change`, `Session`, `Store`). Avoid bare dicts or tuples to represent structured data — use a class
or `dataclass` instead. Dictionaries are for lookups, not for records.

## Global Variables

No mutable global state. Module-level constants are allowed and should be named in
`UPPER_SNAKE_CASE`:

```python
INDEX_VERSION = 1
DEFAULT_SESSION_TIMEOUT = 900
FINGERPRINT_CONTEXT = b"fafnir/fingerprint/v1"
```

Paths that depend on the environment (the fafnir home) are resolved by a function, not frozen in a
module constant, so a process always sees the environment it was started with.

## Errors

Each layer raises its own exception type (`CryptoError`, `StoreError`, `RemoteError`) and the
command layer is the only place that prints and exits. Command handlers print to stderr and
`sys.exit(1)`; they never raise past `main()`.

## Destructive operations

Anything that writes to the user's source folders or drops stored data asks for confirmation first,
and `-f` is the only way to skip it. A condition fafnir cannot distinguish from data loss (an
unavailable root, a file that exists only locally) is reported and skipped — never acted upon.

## Testing

No unit tests. The test suite consists of **integration tests** that exercise the CLI end-to-end:
they invoke commands against a real (temporary) store and real source folders, and assert on the
output and the resulting on-disk state. Each test sets up its own isolated environment (temp HOME,
temp `XDG_CONFIG_HOME`, fresh store) and tears it down after.
