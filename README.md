# Fafnir

A small CLI that stores your administrative documents in the cloud, using a git repository as an
encrypted, portable source of truth.

Your documents stay where they are — `~/Documents/admin`, `/Volumes/scans`, wherever. Fafnir
encrypts them into an opaque git repo it owns, and pushes that repo to a **private** GitHub repo (or
any git remote). The remote never sees a filename, a folder name, or a byte of content.

- **Source of truth** — one command to store every change, one to restore everything.
- **Nothing readable in the cloud** — hash-named [age](https://age-encryption.org) blobs plus an
  encrypted index. Real paths exist only inside that index.
- **No lock-in, including on fafnir** — blobs are standard age files: `age -d -i <identity>` gets
  your documents back without fafnir ever being installed.
- **Git-like** — `push`, `pull`, ignore rules, a single commit per change set.

## How it works

Fafnir keeps two filesystem worlds apart:

```
  source folders (plaintext)            fafnir home (encrypted git repo)
  ~/Documents/admin/*.pdf   ──push──▶   blobs/<id>.age  +  index.age
  /Volumes/scans/*.pdf      ◀──pull──   (git commit / push / pull to the remote)
```

- **push** — scan the source folders, diff them against the index, encrypt what changed into blobs,
  rewrite the index, commit once, push.
- **pull** — pull the repo, decrypt blobs, write the plaintext back into the source folders.

Everything fafnir owns lives in one directory (`$XDG_CONFIG_HOME/fafnir`, else `~/.config/fafnir`),
so the CLI works from any working directory.

Encryption is an **age keypair**: every blob is encrypted to the public key, and the private
identity is itself protected by your passphrase and committed as `identity.age`. That single
passphrase-protected file is all you need to recover the store from scratch. Blob names are a
**keyed** fingerprint — `HMAC-SHA256(key, content)` — so identical files deduplicate, changes are
cheap to detect, and nobody with repo access can confirm that a *guessed* document is in there.

## Setup

Requires Python 3.11+ and `git`, on Linux or macOS.

```sh
pip install fafnir-store
```

Or run from a checkout with [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run fafnir --help
```

Create the store, set the passphrase (asked once, no recovery if lost), and track a folder:

```sh
fafnir init
fafnir config add ~/Documents/admin
fafnir config set remote git@github.com:you/admin-store.git   # a PRIVATE repo
fafnir push
```

## Usage

```sh
fafnir push               # store every local change (shows a recap, asks to confirm)
fafnir push -f            # no confirmation
fafnir pull               # restore documents from the store
fafnir verify             # check the store decrypts, matches the index, and spot drift
```

`push` prints a plain-language recap before doing anything:

```
Changes to push:
  added     admin/tax/2025/notice.pdf
  modified  admin/insurance/policy.pdf
  deleted   admin/old/scan.pdf

3 change(s), 1.4 MB to encrypt.
Encrypt and commit these changes? [y/N]
```

### Roots

Each tracked top-level folder is a **named root**. The index stores `(root, relative path)` and
never an absolute path, which is what keeps the store portable across machines and mount points.

```sh
fafnir config add ~/Documents/admin            # root name defaults to the folder name
fafnir config add /Volumes/scans --name scans
fafnir config rm scans                         # stops tracking; stored files are kept
fafnir config list
```

If a root's folder is missing (an unplugged drive), fafnir says so and **leaves its files alone** —
an unavailable root is never mistaken for a mass deletion.

### Ignore rules

`<home>/ignore` uses **gitignore syntax** and applies to every root:

```
.DS_Store
*.tmp
drafts/
```

### Sessions

The passphrase is asked once per session (idle timeout: 15 min), then the unlocked identity is
cached in `<home>/session` (mode `0600`).

```sh
fafnir unlock            # start a session now
fafnir lock              # end it
echo -n "$PASSPHRASE" | fafnir unlock    # non-interactive (scripts, cron)
```

### Recovery on a new machine

This is the flow that makes fafnir a real backup:

```sh
fafnir clone git@github.com:you/admin-store.git   # asks for the passphrase, rebinds roots
fafnir pull                                       # decrypts everything back to disk
```

`clone` lists the roots the store knows about and asks where each one lives on this machine
(`--root name=/path` does it non-interactively).

### Recovery without fafnir

Blobs are ordinary age files. With the passphrase and a checkout of the repo:

```sh
age -d repo/identity.age > identity.txt        # your age identity
age -d -i identity.txt repo/index.age          # the index: paths, fingerprints, sizes
age -d -i identity.txt repo/blobs/<id>.age > document.pdf
```

### Config

```sh
fafnir config list
fafnir config set session-timeout 0      # 0 = never time out
fafnir config set max-file-size 52428800
```

| Config key | Default | Description |
|---|---|---|
| `remote` | _(unset)_ | Git remote URL for `push`/`pull` |
| `branch` | `main` | Branch to push to and pull from |
| `session-timeout` | `900` | Session idle timeout in seconds (`0` = no timeout) |
| `max-file-size` | `99614720` (95 MB) | A single file above this is refused, before git can choke on it |

Config lives at `<home>/config.toml`. It is **local to the machine** (it binds root names to
absolute paths) and is never committed.

## What fafnir stores

- Regular files only. Symlinks and special files are skipped; empty directories are dropped
  (like git) and recreated from file paths on restore.
- Content and logical path are preserved; `mtime` is restored best-effort. POSIX permissions are
  not preserved — restored files get non-world-readable defaults (`0600`).
- `pull` never deletes: files that exist only on disk are reported and left alone.

## Security notes

- The remote is untrusted for confidentiality: it only ever sees encrypted blobs. It *can* observe
  how many blobs exist, their sizes, and commit times — accepted, on a private repo.
- The passphrase is set once at `fafnir init`. **There is no recovery.**
- `identity.age` is committed on purpose: it is passphrase-protected (age scrypt mode, work
  factor 2²⁰) and makes the repo self-contained for disaster recovery.
- Decrypted documents live in plaintext in your source folders by design — that is what they are.
  Encryption applies to the cloud side.
- Crypto is [age](https://age-encryption.org) via [`pyrage`](https://github.com/woodruffw/pyrage)
  (bindings to `rage`, the Rust implementation). Fafnir writes no cryptography of its own.

## Releasing

Build and publish instructions live in [`publish.md`](publish.md).
