from __future__ import annotations

import argparse
import getpass
import os
import sys
from dataclasses import dataclass, replace
from typing import NoReturn, Optional

from fafnir.config import Config, Root, VALID_KEYS, slugify
from fafnir.crypto import CryptoError, Identity
from fafnir.model import Index
from fafnir.remote import GitRemote, Remote, RemoteError
from fafnir.scan import (
    IGNORE_HEADER,
    Change,
    ChangeKind,
    ChangeSet,
    Ignore,
    Scanner,
    SkipReason,
)
from fafnir.session import Session
from fafnir.store import Store, StoreError, restore_file

COMMIT_MESSAGE = "Update store"


def fail(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Prompts and rendering
# ---------------------------------------------------------------------------

def prompt_passphrase(prompt: str = "Passphrase: ") -> str:
    """Ask for the passphrase, or read it from stdin when not on a terminal.

    The stdin path is what makes fafnir scriptable (`echo pw | fafnir unlock`)
    and testable; on a terminal the passphrase is never echoed.
    """
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    print(prompt, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        fail("No passphrase supplied on stdin")
    return line.rstrip("\n")


def confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _print_changes(changes: list[Change], label: str, width: int) -> None:
    for change in sorted(changes, key=lambda c: c.logical_path):
        print(f"  {label:<{width}}  {change.logical_path}")


def _print_warnings(result: ChangeSet, index: Index) -> None:
    """Report everything that was seen but deliberately not acted upon."""
    for root in result.unavailable:
        stored = len(index.for_root(root.name))
        print(
            f"! Root '{root.name}' is unavailable at {root.path} — "
            f"{stored} stored file(s) left untouched.",
            file=sys.stderr,
        )
    for name in result.unbound:
        stored = len(index.for_root(name))
        print(
            f"! Root '{name}' holds {stored} stored file(s) but is not bound on this "
            f"machine — run `fafnir config add <path> --name {name}`.",
            file=sys.stderr,
        )
    counts: dict[str, int] = {}
    for skip in result.skipped:
        if skip.reason is SkipReason.TOO_LARGE:
            continue
        counts[skip.reason.value] = counts.get(skip.reason.value, 0) + 1
    if counts:
        summary = ", ".join(f"{n} {reason}(s)" for reason, n in sorted(counts.items()))
        print(f"! Skipped {summary}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _store(config: Config) -> Store:
    store = Store(config.home)
    if not store.exists():
        fail(
            f"No fafnir store at {config.home}. "
            "Run `fafnir init` or `fafnir clone <url>` first."
        )
    return store


def _unlock(config: Config, store: Store) -> Session:
    """Resume the session, or prompt for the passphrase and start one."""
    timeout = int(config.require("session-timeout"))
    session = Session.resume(config.home, timeout)
    if session is not None:
        return session
    try:
        identity = store.unlock(prompt_passphrase())
    except (CryptoError, StoreError) as e:
        fail(str(e))
    return Session.start(config.home, identity)


def _remote(config: Config, store: Store) -> Remote:
    return GitRemote(store.repo_dir, config.get("remote"), config.require("branch"))


@dataclass
class Context:
    """The unlocked state every store-touching command needs."""

    config: Config
    store: Store
    session: Session
    index: Index

    @property
    def identity(self) -> Identity:
        return self.session.identity

    @classmethod
    def open(cls, config: Config) -> Context:
        store = _store(config)
        session = _unlock(config, store)
        try:
            index = store.read_index(session.identity)
        except StoreError as e:
            fail(str(e))
        return cls(config, store, session, index)

    def scanner(self) -> Scanner:
        return Scanner(
            roots=self.config.roots(),
            ignore=Ignore.load(self.config.ignore_path),
            identity=self.identity,
            max_file_size=int(self.config.require("max-file-size")),
        )


# ---------------------------------------------------------------------------
# Store lifecycle
# ---------------------------------------------------------------------------

def _prompt_new_passphrase() -> str:
    passphrase = prompt_passphrase("Passphrase: ")
    confirmation = prompt_passphrase("Confirm passphrase: ")
    if passphrase != confirmation:
        fail("Passphrases do not match.")
    if not passphrase:
        fail("Passphrase cannot be empty.")
    return passphrase


def _write_ignore_file(config: Config) -> None:
    if os.path.exists(config.ignore_path):
        return
    os.makedirs(config.home, exist_ok=True)
    with open(config.ignore_path, "w") as f:
        f.write(IGNORE_HEADER)


def cmd_init(args: argparse.Namespace, config: Config) -> None:
    store = Store(config.home)
    if store.exists():
        fail(f"A fafnir store already exists at {config.home}")

    passphrase = _prompt_new_passphrase()
    identity = Identity.generate()

    store.create()
    store.write_identity(identity, passphrase)
    store.write_index(identity, Index())
    _write_ignore_file(config)

    remote = _remote(config, store)
    try:
        remote.initialize()
        remote.commit("Initialize store")
    except RemoteError as e:
        fail(str(e))

    Session.start(config.home, identity)
    print(f"Store created at {config.home}")
    print("Next: `fafnir config add <folder>`, then `fafnir push`.")


def _rebind_roots(config: Config, index: Index, bindings: list[str]) -> None:
    """Bind the store's named roots to local paths on this machine."""
    given: dict[str, str] = {}
    for binding in bindings:
        name, sep, path = binding.partition("=")
        if not sep or not name or not path:
            fail(f"Invalid --root binding: {binding!r} (expected name=path)")
        given[name] = path

    for name in index.roots:
        if config.root(name) is not None:
            continue
        path = given.pop(name, None)
        if path is None:
            if not sys.stdin.isatty():
                continue
            count = len(index.for_root(name))
            try:
                path = input(f"Local path for root '{name}' ({count} files, empty to skip): ")
            except EOFError:
                path = ""
            if not path.strip():
                continue
        _bind_root(config, path, name)

    for name in given:
        print(f"! The store has no root named '{name}'.", file=sys.stderr)


def cmd_clone(args: argparse.Namespace, config: Config) -> None:
    store = Store(config.home)
    if store.exists():
        fail(f"A fafnir store already exists at {config.home}")

    os.makedirs(config.home, exist_ok=True)
    config.set("remote", args.url)
    if args.branch:
        config.set("branch", args.branch)
    _write_ignore_file(config)

    remote = GitRemote(store.repo_dir, args.url, config.require("branch"))
    try:
        remote.clone()
    except RemoteError as e:
        fail(str(e))
    if not store.exists():
        fail(f"{args.url} does not contain a fafnir store (no {Store.IDENTITY_FILE})")

    try:
        identity = store.unlock(prompt_passphrase())
    except (CryptoError, StoreError) as e:
        fail(str(e))
    Session.start(config.home, identity)

    try:
        index = store.read_index(identity)
    except StoreError as e:
        fail(str(e))

    print(f"Store cloned to {config.home}: {len(index.entries)} file(s).")
    _rebind_roots(config, index, args.root)
    bound = [r.name for r in config.roots()]
    if bound:
        print("Run `fafnir pull` to restore the documents.")
    else:
        print(
            "No roots bound yet. Run `fafnir config add <path> --name <root>` "
            "for each root, then `fafnir pull`."
        )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def cmd_unlock(args: argparse.Namespace, config: Config) -> None:
    store = _store(config)
    _unlock(config, store)
    timeout = int(config.require("session-timeout"))
    if timeout > 0:
        print(f"Unlocked. The session expires after {timeout}s of inactivity.")
    else:
        print("Unlocked.")


def cmd_lock(args: argparse.Namespace, config: Config) -> None:
    Session.lock(config.home)
    print("Locked.")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _bind_root(config: Config, path: str, name: Optional[str]) -> Root:
    absolute = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.isdir(absolute):
        fail(f"Not a directory: {absolute}")

    existing = config.root_at(absolute)
    if existing is not None:
        fail(f"{absolute} is already tracked as root '{existing.name}'")

    root = Root(name=name or slugify(os.path.basename(absolute)), path=absolute)
    if config.root(root.name) is not None:
        fail(
            f"A root named '{root.name}' already exists; "
            "choose another with --name"
        )
    # Nested roots would store the same file under two logical paths.
    for other in config.roots():
        if other.contains(absolute) or root.contains(other.path):
            fail(f"{absolute} is nested with root '{other.name}' ({other.path})")

    try:
        config.add_root(root)
    except ValueError as e:
        fail(str(e))
    print(f"Root '{root.name}' -> {root.path}")
    return root


def cmd_config_add(args: argparse.Namespace, config: Config) -> None:
    _bind_root(config, args.path, args.name)


def cmd_config_rm(args: argparse.Namespace, config: Config) -> None:
    target = args.root
    root = config.root(target)
    if root is None:
        root = config.root_at(os.path.realpath(os.path.abspath(os.path.expanduser(target))))
    if root is None:
        fail(f"No such root: {target!r}")
    config.remove_root(root.name)
    print(f"Root '{root.name}' unregistered (stored files are kept).")


def cmd_config_set(args: argparse.Namespace, config: Config) -> None:
    try:
        config.set(args.key, args.value)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(f"Valid keys: {', '.join(sorted(VALID_KEYS))}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        fail(str(e))


def cmd_config_get(args: argparse.Namespace, config: Config) -> None:
    value = config.get(args.key)
    if value is None:
        print(f"Unknown or unset config key: {args.key!r}", file=sys.stderr)
        sys.exit(1)
    print(value)


def cmd_config_list(args: argparse.Namespace, config: Config) -> None:
    print(f"home = {config.home}")
    for key, value in sorted(config.settings().items()):
        print(f"{key} = {value}")
    roots = config.roots()
    print("\nroots:")
    if not roots:
        print("  (none)")
        return
    width = max(len(r.name) for r in roots)
    for root in roots:
        suffix = "" if root.available else "  (unavailable)"
        print(f"  {root.name:<{width}}  {root.path}{suffix}")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def _report_push(result: ChangeSet, index: Index) -> None:
    _print_warnings(result, index)
    if result.is_empty:
        return
    print("Changes to push:")
    for kind in (ChangeKind.ADDED, ChangeKind.MODIFIED, ChangeKind.DELETED):
        _print_changes(result.of(kind), kind.value, len("modified"))
    upload = result.upload_size()
    print(
        f"\n{len(result.changes)} change(s), {human_size(upload)} to encrypt."
    )


def _apply_push(ctx: Context, result: ChangeSet) -> None:
    index = ctx.index
    for change in result.added + result.modified:
        try:
            with open(change.abs_path, "rb") as f:
                data = f.read()
        except OSError as e:
            fail(f"Cannot read {change.logical_path}: {e}")
        # Fingerprint the bytes actually stored, not the ones seen at scan time,
        # so a blob id always matches its blob even if the file changed since.
        entry = replace(change.scanned, fingerprint=ctx.identity.fingerprint(data), size=len(data))
        if not ctx.store.has_blob(entry.fingerprint):
            ctx.store.write_blob(ctx.identity, entry.fingerprint, data)
        index.put(entry)

    for change in result.deleted:
        index.remove(change.root, change.path)

    # Content-identical files whose stat drifted: folding them in now saves a
    # re-hash on the next scan, and costs nothing since the index is rewritten.
    for entry in result.refreshed:
        index.put(entry)

    index.set_roots(root.name for root in ctx.config.roots())
    ctx.store.write_index(ctx.identity, index)
    ctx.store.prune_blobs(index.fingerprints())


def cmd_push(args: argparse.Namespace, config: Config) -> None:
    ctx = Context.open(config)
    if not config.roots():
        fail("No roots configured. Run `fafnir config add <folder>`.")

    result = ctx.scanner().diff(ctx.index)
    if result.oversize:
        limit = human_size(int(config.require("max-file-size")))
        for skip in result.oversize:
            print(
                f"Error: {skip.logical_path} is {human_size(skip.size)}, over the "
                f"{limit} limit",
                file=sys.stderr,
            )
        fail(
            "refusing to store oversized file(s); exclude them in "
            f"{config.ignore_path} or raise `max-file-size`"
        )

    _report_push(result, ctx.index)
    remote = _remote(config, ctx.store)

    if result.is_empty:
        print("Nothing to push.")
    else:
        if not args.force and not confirm("Encrypt and commit these changes?"):
            print("Aborted.")
            return
        _apply_push(ctx, result)
        try:
            remote.commit(COMMIT_MESSAGE)
        except RemoteError as e:
            fail(str(e))
        print(f"Committed {len(result.changes)} change(s).")

    if not config.get("remote"):
        print(
            "No remote configured; the store is committed locally only. "
            "Set one with `fafnir config set remote <url>`.",
            file=sys.stderr,
        )
        return
    try:
        remote.push()
    except RemoteError as e:
        fail(str(e))
    print("Pushed to the remote.")


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _report_pull(result: ChangeSet, index: Index) -> list[Change]:
    """Render the pull plan and return the changes to write back."""
    _print_warnings(result, index)
    # The store is the source of truth: what it holds and the disk does not is
    # restored, what differs is overwritten. Files only on disk are left alone —
    # pull never deletes; the next push will store them.
    restore = result.deleted
    overwrite = result.modified
    if restore or overwrite:
        print("Changes to apply:")
        _print_changes(restore, "restore", len("overwrite"))
        _print_changes(overwrite, "overwrite", len("overwrite"))
        total = sum(c.stored.size for c in restore + overwrite)
        print(f"\n{len(restore) + len(overwrite)} file(s), {human_size(total)} to write.")
    if result.added:
        print("\nLocal-only files (left untouched):")
        _print_changes(result.added, "local", len("local"))
    return restore + overwrite


def cmd_pull(args: argparse.Namespace, config: Config) -> None:
    store = _store(config)
    session = _unlock(config, store)
    remote = _remote(config, store)

    if config.get("remote"):
        try:
            remote.pull(force=args.force)
        except RemoteError as e:
            fail(str(e))
    else:
        print(
            "No remote configured; restoring from the local store.", file=sys.stderr
        )

    try:
        index = store.read_index(session.identity)
    except StoreError as e:
        fail(str(e))

    ctx = Context(config, store, session, index)
    result = ctx.scanner().diff(index)
    pending = _report_pull(result, index)
    if not pending:
        print("Nothing to restore.")
        return

    if not args.force and not confirm("Write these files to disk?"):
        print("Aborted.")
        return

    roots = {root.name: root for root in config.roots()}
    written = 0
    for change in pending:
        entry = change.stored
        root = roots.get(entry.root)
        if root is None or not root.available:
            continue
        try:
            data = store.read_blob(session.identity, entry.fingerprint)
        except StoreError as e:
            fail(f"{e} (needed for {entry.logical_path})")
        if session.identity.fingerprint(data) != entry.fingerprint:
            fail(f"Blob for {entry.logical_path} does not match the index")
        try:
            restore_file(os.path.join(root.path, entry.path), data, entry.mtime)
        except OSError as e:
            fail(f"Cannot write {entry.logical_path}: {e}")
        written += 1
    print(f"Restored {written} file(s).")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace, config: Config) -> None:
    ctx = Context.open(config)
    problems: list[str] = []

    blobs = ctx.store.blob_ids()
    for fingerprint in blobs:
        try:
            data = ctx.store.read_blob(ctx.identity, fingerprint)
        except StoreError as e:
            problems.append(str(e))
            continue
        if ctx.identity.fingerprint(data) != fingerprint:
            problems.append(f"Blob {fingerprint[:12]} does not match its fingerprint")

    referenced = ctx.index.fingerprints()
    for entry in ctx.index.entries:
        try:
            present = ctx.store.has_blob(entry.fingerprint)
        except StoreError as e:
            problems.append(f"{entry.logical_path}: {e}")
            continue
        if not present:
            problems.append(f"Missing blob for {entry.logical_path}")

    orphans = [f for f in blobs if f not in referenced]
    print(
        f"Store: {len(blobs)} blob(s), {human_size(ctx.store.size_on_disk())}, "
        f"{len(ctx.index.entries)} tracked file(s)."
    )
    if orphans:
        print(f"{len(orphans)} blob(s) not referenced by the index.")

    if problems:
        for problem in problems:
            print(f"! {problem}", file=sys.stderr)
    else:
        print("All blobs decrypt and match the index.")

    result = ctx.scanner().diff(ctx.index)
    print()
    if result.is_empty:
        print("Source folders match the store.")
    else:
        print("Drift vs the source folders:")
        for kind in (ChangeKind.ADDED, ChangeKind.MODIFIED, ChangeKind.DELETED):
            _print_changes(result.of(kind), kind.value, len("modified"))
    _print_warnings(result, ctx.index)

    if problems:
        sys.exit(1)
