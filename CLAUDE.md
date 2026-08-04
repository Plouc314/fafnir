# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Fafnir stores administrative documents in a git repository, encrypted end-to-end: the working tree
is nothing but hash-named [age](https://age-encryption.org) blobs and an encrypted index, so a
private GitHub repo can hold every document without ever seeing a filename or a byte of content.
Design priorities, in order: never lose or leak data, recoverability without fafnir (the blobs are
standard age files), git-like ergonomics, and a small auditable codebase. See `SPEC.md` for the
authoritative spec and `README.md` for the user-facing overview.

## Commands

```bash
uv sync                              # install (deps + the fafnir entry point)
uv run python -m fafnir <args>       # run the CLI from source
uv run python -m unittest discover tests   # run the full test suite (~90 s)
uv run python -m unittest tests.test_cli.PushTests.test_push_adds_files   # run a single test
```

Tests are **end-to-end integration tests only** (`tests/test_cli.py`); there are no unit tests by
design (`conventions.md`). Each test runs `python -m fafnir` as a real subprocess with a temporary
`HOME` and `XDG_CONFIG_HOME`, so it exercises argument parsing, the age layer, the on-disk store,
git, and session handling together. The suite is slow because every `init`/unlock pays a real
scrypt derivation (~1.2 s, work factor 2²⁰) — that cost is the point, don't tune it away.

## Architecture

Two filesystem worlds, and the code never confuses them: **source folders** hold the user's
plaintext documents and are external inputs; the **fafnir home** (`$XDG_CONFIG_HOME/fafnir`, else
`~/.config/fafnir`) holds the git repo whose working tree is the encrypted store.

The flow for every command is: `cli.py` (argparse) → a `cmd_*` function in `commands.py` →
`Context.open()` (session + store + index) → `Scanner.diff()` against the index → apply, commit,
sync.

- **`cli.py`** — builds the argparse tree; each subcommand sets `func` to a `commands.cmd_*`
  handler. `main()` calls `func(args, Config())` and turns Ctrl-C into exit 130.
- **`commands.py`** — one `cmd_*(args, config)` per command, plus the shared plumbing: `fail()`
  (print to stderr, exit 1), `prompt_passphrase()`, `confirm()`, the change-set renderers, and
  `Context`, which bundles config + store + session + index for every store-touching command.
  Handlers are the *only* layer that prints or exits.
- **`crypto.py`** — the only place that touches `pyrage`. `Identity` wraps an age X25519 keypair:
  `encrypt`/`decrypt` (recipient mode, fresh ephemeral key per blob — no nonce state to manage),
  `protect`/`unprotect` (age passphrase mode for `identity.age`), and the keyed fingerprint
  `HMAC-SHA256(fp_key, plaintext)` where `fp_key` is derived from the secret key. The keying is
  deliberate: a plain content hash would be a confirmation oracle for guessed documents.
- **`store.py`** — the encrypted store at `<home>/repo`: blob paths, index read/write, identity
  read/write, orphan pruning. All writes go through a temp file + `os.replace`. `blob_path()`
  validates the fingerprint is 64 hex chars — the index is decrypted input and must not be able to
  steer a write out of `blobs/`. `restore_file()` is the write-back into a source folder.
- **`model.py`** — pure data: `Entry` (root, path, fingerprint, size, mtime) and `Index`, with JSON
  (de)serialization. `Index` keeps a dict keyed by `(root, path)` because a scan does one lookup per
  file; `entries` returns the sorted list.
- **`scan.py`** — walking and classification: `Ignore` (gitignore syntax via `pathspec`, since
  git's own ignore logic never sees the source folders), `Scanner.walk()` (regular files only;
  symlinks, special files and oversized files are recorded as `Skipped`), and `Scanner.diff()`
  producing a `ChangeSet`. Each `Change` carries **both** sides — `scanned` (what is on disk) and
  `stored` (what the index holds) — which is why push and pull can share one diff: push encrypts
  `scanned`, pull writes back `stored`.
- **`session.py`** — the unlocked identity cached at `<home>/session` (mode 0600) as a plain age
  key file, so `age -d -i <home>/session <blob>` works by hand. The passphrase is never stored.
  Last access is the file's mtime; `resume()` enforces the idle timeout and refreshes it. Opened
  with `O_NOFOLLOW` plus an `fstat` owner/type check, and recreated rather than opened in place.
- **`config.py`** — `config.toml` in the fafnir home (read with `tomllib`, written by hand: only
  strings and ints are ever emitted). Local by design and never committed, because it is the only
  place absolute paths exist. Holds the settings (`VALID_KEYS`, defaults merged on read) and the
  `[roots]` table of name → path bindings.
- **`remote.py`** — git-backed sync. A `Remote` ABC with a single `GitRemote` implementation that
  shells out to `git`. Unlike mimir's, this repo is fafnir's own, so operations can stage the whole
  tree and no unrelated working-tree state needs preserving. It checks for `.git` directly instead
  of asking git, because the fafnir home may itself sit inside a dotfiles repo. Every git
  invocation is echoed to stderr for auditability. Auth is left entirely to the user's git setup.

## Invariants worth protecting

These are the behaviours the test suite exists to pin down; break one and the tool loses data:

- **An unavailable root is never a deletion.** A missing root folder (unmounted drive) or a root
  that is in the index but unbound on this machine is warned about and skipped, never diffed into
  deletions.
- **`pull` never deletes.** Files that exist only on disk are reported as local-only and left alone.
- **The store is committed in one commit per push**, index and blobs together, and the index is
  written *before* blobs are pruned.
- **Nothing readable reaches the repo.** `StoreFormatTests.test_nothing_readable_is_stored` walks
  the whole repo and asserts neither content nor real filenames appear anywhere.
- **Blobs stay hand-decryptable** with the stock `age` CLI. Any change to the crypto layer must
  keep `age -d repo/identity.age` → `age -d -i identity.txt repo/blobs/<id>.age` working.

## Conventions (from `conventions.md`)

- `from __future__ import annotations` in every module; builtin generics (`list[str]`) but
  `Optional`/`Union` from `typing`, matching the sibling project mimir.
- All imports at the top, no deferred/inline imports.
- Model every domain concept as a class/dataclass — no bare dicts or tuples for structured data.
- Two external dependencies: `pyrage` and `pathspec`. Don't add more; everything else is stdlib.
- No mutable module-level globals; constants are `UPPER_SNAKE_CASE`.
- Only `commands.py` prints or exits; lower layers raise `CryptoError`/`StoreError`/`RemoteError`.
