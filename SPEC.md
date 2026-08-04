# fafnir — Specification

`fafnir` is a small Python CLI to store administrative documents in the cloud, using a
git repository as an encrypted, portable source of truth.

Status: **v1, implemented**. This document describes what fafnir does; it is kept in
sync with the code.

---

## 1. Goals & non-goals

### Goals
- Serve as the **source of truth** for all administrative documents.
- **Portable**: works on macOS and Linux (Windows not required).
- **Simple to use** from a developer's perspective (small, git-like CLI).
- **Secure**: nothing readable is ever stored in the cloud.
- **No vendor lock-in** — not on the git host, and *not on fafnir itself*.

### Non-goals (v1)
- Multi-machine / concurrent use. We assume a **single machine** uses a given fafnir
  store at a time. (The design does not preclude adding this later; `clone` + `pull`
  already work on a second machine, they are simply not concurrency-safe.)
- Hiding *how many* documents exist or their *sizes*. This metadata may leak; it is
  acceptable in a private repo.
- Windows support.
- A GUI, a daemon, or background syncing.

---

## 2. Threat model

- The git remote (e.g. a **private** GitHub repository) is **untrusted for
  confidentiality**. It must never see plaintext documents or their real filenames.
- The remote *may* observe: the number of stored blobs, their sizes, and commit
  timestamps. This is accepted (see non-goals).
- We assume the **passphrase is not cracked** and the **repository stays private**.
  Under that assumption, committing the (passphrase-protected) identity into the repo
  is acceptable.
- The local machine is trusted. Decrypted documents live in plaintext in the tracked
  source folders — that is by design (they are your working files); encryption applies
  only to the cloud/repo side.

---

## 3. Architecture — two filesystem worlds

fafnir separates two distinct places on disk:

1. **Source folders** — the real, plaintext documents, living anywhere on the machine
   (`~/Documents/admin`, `/Volumes/scans`, …). These are external *inputs*, not a git
   checkout.

2. **The fafnir home** (`/fafnir/`, see §4) — contains the git repository whose working
   tree is the **encrypted store** only: hash-named encrypted blobs plus an encrypted
   index. Your documents never live here in plaintext.

```
  source folders (plaintext)            fafnir home (encrypted git repo)
  ~/Documents/admin/*.pdf   ──push──▶   blobs/<id>.age  +  index.age
  /Volumes/scans/*.pdf      ◀──pull──   (git commit / push / pull to remote)
```

- **push**: scan source folders → detect changes → encrypt changed files into blobs →
  rewrite the index → single git commit → git push.
- **pull**: git pull → decrypt blobs → write plaintext back into the source folders.

Because the git working tree contains only opaque encrypted blobs, all human-facing
diffs are computed at the **logical layer** from the index (§6), never from git blobs.

---

## 4. The fafnir home

A single directory holds everything fafnir needs, so the CLI can be invoked from any
working directory.

- Location: `$XDG_CONFIG_HOME/fafnir` if set, else `~/.config/fafnir`.
- Contents:
  - `config.toml` — **local, per-machine, not committed**: named roots → absolute
    paths, remote, branch, threshold settings.
  - `repo/` — the git repository (working tree = the encrypted store):
    - `blobs/<id>.age` — one encrypted blob per stored file version.
    - `index.age` — the encrypted index (§6). Committed.
    - `identity.age` — the passphrase-protected age identity (§5). Committed.
  - `session` — cached unlocked identity (§5), `0600`, **not committed**.
  - `ignore` — gitignore-syntax ignore rules (§7), local.

The repository is fafnir's own — created by `init`/`clone` inside the home and holding
nothing but the encrypted store. It never shares a working tree with unrelated work, so
every operation may stage the whole tree.

---

## 5. Cryptography

fafnir writes **no hand-rolled cryptography**. It orchestrates two standard, portable
components — the same way it already depends on `git`:

- `git` — transport / versioning (CLI).
- [`age`](https://age-encryption.org) — encryption, through
  [`pyrage`](https://github.com/woodruffw/pyrage), Python bindings to `rage` (the Rust
  implementation of age).

This keeps confidentiality on a public, well-reviewed standard and guarantees
**recoverability without fafnir**: every file fafnir writes is a standard age file, so
any blob can be decrypted by hand with the stock `age -d`.

> Design note. The original spec shelled out to the `age` CLI. `pyrage` produces and
> consumes the identical file format (verified in both directions against `age` v1.2.1)
> while removing an external binary from the install path — and with it the need to
> drive age's terminal-only passphrase prompt through a pty. The "no lock-in" guarantee
> is unchanged: the artefacts, not the tool that wrote them, are what matter.

### Identity (keypair) model
- `init` generates an **age identity** (X25519 keypair).
- Each document blob is encrypted **to the public key** (recipient mode). age uses a
  fresh ephemeral key per encryption, so there is no nonce management for fafnir to get
  wrong, and per-file encryption is fast.
- The **private identity file** is itself protected with the user's **passphrase**
  (age passphrase mode → one scrypt derivation at unlock, work factor 2²⁰). It is
  stored as `identity.age` and **committed** to the repo (self-contained + recoverable;
  safe under the threat model in §2).

### Session cache (lock / unlock)
- `unlock` decrypts the identity once (prompting for the passphrase) and caches the
  unlocked identity at `<home>/session`, mode `0600`. The cache is a plain age key file,
  so `age -d -i <home>/session <blob>` works by hand while a session is open. The
  passphrase itself is never stored.
- Cached identity is cleared by `lock`, by an idle timeout (default **15 min**, measured
  from the file's mtime and refreshed on each use), or by deleting the file.
- Commands that need decryption auto-`unlock` (prompt) if no valid session exists. On a
  terminal the passphrase is read without echo; when stdin is a pipe it is read from
  there, which makes fafnir scriptable.

### Blob naming — keyed fingerprint
- Blobs are named by a **keyed** content fingerprint: `HMAC-SHA256(fp_key, plaintext)`,
  where `fp_key = HMAC-SHA256(identity secret, "fafnir/fingerprint/v1")`.
- Rationale: a *plain* content hash would let anyone with repo access confirm the
  presence of a *guessed* document (a confirmation oracle), partially defeating filename
  hiding. The keyed HMAC leaks nothing while still being deterministic.
- The fingerprint doubles as the **change-detection** primitive and gives natural
  **content deduplication** (identical content → identical blob).

---

## 6. The encrypted index (`index.age`)

The index is the pivot of the whole system. It is a single encrypted document holding,
for every tracked file:

- **root name** and **relative path** (§8) — the file's logical identity.
- **fingerprint** — `HMAC-SHA256(fp_key, plaintext)`, i.e. the blob id.
- **size** and **mtime** — used to short-circuit re-hashing on scan, and to restore
  mtime on pull.

The index also stores the set of **named roots** known to the store (so `clone` can
rebind them) and a format version. Its plaintext is JSON.

Its three jobs:
1. **Filename hiding** — real paths exist *only* inside the encrypted index; blobs are
   hash-named.
2. **Logical diffing** — `push`/`pull` change lists are computed by comparing the live
   source folders against the index (§10).
3. **Integrity** — `verify` checks blobs against index fingerprints (§11).

A blob is deleted from the working tree once **no** index entry references it (git
history still has it). Since entries are deduplicated by fingerprint, removing one of
two identical files keeps the shared blob alive.

---

## 7. Ignore rules

- fafnir honours **gitignore syntax** so users can exclude files (reuse of a familiar
  concept).
- Because the source folders are *not* inside the git tree, git's own ignore logic does
  not apply; fafnir re-implements matching using the `pathspec` library (same syntax
  git uses).
- v1 uses a single central ignore file at `<home>/ignore`, applied to every root.
- As in git, an ignored *directory* is pruned wholesale: a file cannot be re-included
  once one of its parent directories is excluded.

---

## 8. Named roots

- Each tracked top-level folder is registered as a **named root** via `config add`.
- The index stores every file as `(root-name, relative-path)`, never as an absolute
  path. This keeps the store **portable** and handles folders on **different mount
  points** (`~/Documents` and `/Volumes/scans` are separate roots).
- Per-machine `config.toml` binds each root name → a local absolute path.
- On `clone`, root names are rebound to local paths before restore.
- Constraint: tracked roots must not be nested inside one another (rejected at
  `config add`), to avoid double-tracking.
- A root that exists in the index but has no local binding is **unbound**: fafnir
  reports it and leaves its files alone, exactly as it does for an unavailable root
  (§11). This is what makes `config rm` safe — it unregisters a root without deleting
  stored history.

---

## 9. File-fidelity scope (v1)

- **Regular files only.** Symlinks are not followed and are skipped. Special files
  (devices, sockets, FIFOs) are skipped.
- **Empty directories are dropped** (consistent with git). Directories are recreated
  implicitly from file paths on restore.
- **Content + logical path** are preserved. `mtime` is restored best-effort. POSIX
  permissions are **not** preserved; restored files get standard, non-world-readable
  defaults (`0600`).
- **Large-file guard**: a single file above a configurable threshold (default **95 MB**,
  under GitHub's 100 MB hard cap) is **refused** with a clear error rather than failing
  at the git layer. (Splitting is intentionally not implemented in v1.)

---

## 10. Change detection

On scan, for each tracked file fafnir classifies it against the index:

| Condition | Classification |
|---|---|
| path not in index | **added** |
| path in index, `(size, mtime)` unchanged | unchanged (skip re-hash) |
| path in index, fingerprint differs | **modified** |
| path in index, fingerprint identical but stat drifted | unchanged; the entry's stat is refreshed the next time the index is written, so the file is not re-hashed forever |
| path in index, file missing on disk | **deleted** |
| entire root path missing on disk | **root unavailable** (see §11) |
| root in index, not bound in `config.toml` | **root unbound** (see §8) |

---

## 11. Commands

### `init`
Create the fafnir home, generate the age identity, prompt for and set the passphrase,
`git init` the repo, write an empty index, and commit. Fails if a store already exists.
Leaves a session open.

### `clone <url> [-b branch] [--root name=path]…`
Bootstrap from an **existing remote** (disaster recovery / new machine): configure the
remote, fetch the encrypted store, prompt for the passphrase, and rebind the named
roots to local paths (from `--root`, or interactively). Does **not** restore files by
itself — a subsequent `pull` performs the decrypt-and-write-back. This is the flow that
makes fafnir a real backup.

### `config add <path> [--name <name>]`
Register `<path>` as a named root (default name: the folder's own name). Normalises the
path, rejects nesting with an existing root.

### `config rm <name|path>`
Unregister a root. (Does not delete already-stored history — the root simply becomes
unbound, §8.)

### `config set <key> <value>` / `config get <key>` / `config list`
Manage `remote`, `branch`, `session-timeout` and `max-file-size`; `config list` also
shows the roots and whether each one is currently available.

### `unlock` / `lock`
Manage the session cache (§5).

### `push [-f]`
1. Scan all tracked roots and classify changes (§10).
2. If a **root path is entirely missing** (e.g. an unmounted drive), do **not** render
   its files as deletions; show a distinct **"root unavailable"** warning line and
   require explicit acknowledgement (the confirmation in step 4).
3. Display a human-readable recap (added / modified / deleted, with real filenames).
4. Confirm interactively; `-f` skips confirmation.
5. Encrypt changed files into blobs, rewrite the index, prune unreferenced blobs, and
   record everything in a **single git commit** (atomic locally). `git push`.

A file over the size threshold (§9) aborts the whole push before anything is written.
With no remote configured the changes are committed locally and the user is told so.

### `pull [-f]`
1. `git pull` (fast-forward only). Abort on divergence unless `-f`, which resets the
   local store to the remote.
2. Compute the logical change list against local source folders and display it:
   files the store has and the disk lacks are **restored**, files that differ are
   **overwritten**.
3. Confirm (this **overwrites plaintext files on disk**; `-f` skips confirmation).
4. Decrypt blobs and write plaintext back into the source folders; restore mtime
   best-effort. (The write-back is not atomic per push; `verify` can detect a partial
   restore.)

`pull` **never deletes**: a file that exists only on disk is reported as *local-only*
and left untouched — the next `push` will store it.

### `verify`
1. Every blob decrypts successfully and matches its own fingerprint.
2. Every index entry has its blob; blobs no entry references are reported.
3. Source folders vs index drift (a free dry-run / status).

Exits non-zero if the store itself is damaged (drift alone is not an error).

---

## 12. Dependencies

- Python **3.11+** (stdlib `tomllib` for the config).
- `git` CLI (transport).
- [`pyrage`](https://github.com/woodruffw/pyrage) — age encryption.
- [`pathspec`](https://github.com/cpburnz/python-pathspec) — gitignore matching.

`git` is a portable single binary available on macOS and Linux; `pyrage` ships abi3
wheels for both. Both produce standard formats recoverable without fafnir.

---

## 13. Design principles (recap)

- **Reuse git concepts** (push/pull, ignore rules, commits) rather than inventing them.
- **No hand-rolled crypto** — orchestrate `git` + `age`.
- **No lock-in, including on fafnir** — standard, off-the-shelf formats; documents are
  recoverable by hand.
- **The encrypted index is a first-class component**; all logical diffs route through it.
- **Fail safe** — never interpret an unavailable root as a mass deletion; confirm before
  any destructive action; never delete a file that only exists locally.
