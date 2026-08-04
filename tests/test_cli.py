"""End-to-end integration tests for the fafnir CLI.

Each test runs commands as a real subprocess against a fresh, isolated store: a
temporary HOME and XDG_CONFIG_HOME (so the fafnir home, its config and its
session live under them) and temporary source folders. Nothing is mocked —
these exercise argument parsing, the age layer, the on-disk store, git, and
session handling together.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSPHRASE = "hunter2"
AGE_MAGIC = b"age-encryption.org/v1"


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.home = tempfile.mkdtemp(prefix="fafnir-test-")
        self.docs = os.path.join(self.home, "docs")
        os.makedirs(self.docs)
        self.fafnir_home = os.path.join(self.home, ".config", "fafnir")
        self.repo = os.path.join(self.fafnir_home, "repo")
        self.blobs = os.path.join(self.repo, "blobs")
        self.session_path = os.path.join(self.fafnir_home, "session")

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def fafnir(
        self, *args: str, stdin: Optional[str] = None, home: Optional[str] = None
    ) -> subprocess.CompletedProcess[str]:
        """Run `fafnir <args>`. `stdin` is fed to prompts (passphrase, y/N).

        `home` overrides HOME for this invocation, which simulates running on a
        different machine (used by the recovery tests). A git committer identity
        is injected so commits work even when no global gitconfig is present.
        """
        root = home or self.home
        env = dict(
            os.environ,
            HOME=root,
            XDG_CONFIG_HOME=os.path.join(root, ".config"),
            GIT_AUTHOR_NAME="fafnir-test",
            GIT_AUTHOR_EMAIL="fafnir@test.invalid",
            GIT_COMMITTER_NAME="fafnir-test",
            GIT_COMMITTER_EMAIL="fafnir@test.invalid",
        )
        return subprocess.run(
            [sys.executable, "-m", "fafnir", *args],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
        )

    def init_store(self, home: Optional[str] = None) -> None:
        r = self.fafnir("init", stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def track_docs(self) -> None:
        r = self.fafnir("config", "add", self.docs)
        self.assertEqual(r.returncode, 0, r.stderr)

    def write(self, relpath: str, content: str, root: Optional[str] = None) -> str:
        path = os.path.join(root or self.docs, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def read(self, relpath: str, root: Optional[str] = None) -> str:
        with open(os.path.join(root or self.docs, relpath)) as f:
            return f.read()

    def blob_count(self) -> int:
        return len([n for n in os.listdir(self.blobs) if n.endswith(".age")])

    def bare_remote(self) -> str:
        remote = tempfile.mkdtemp(prefix="fafnir-remote-")
        self.addCleanup(shutil.rmtree, remote, ignore_errors=True)
        subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
        return remote

    def other_home(self, prefix: str = "fafnir-other-") -> str:
        home = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home


class InitTests(CliTestCase):
    def test_init_creates_store(self) -> None:
        self.init_store()
        self.assertTrue(os.path.exists(os.path.join(self.repo, "identity.age")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "index.age")))
        self.assertTrue(os.path.exists(os.path.join(self.fafnir_home, "ignore")))
        # init leaves a session open, and the git repo has its first commit.
        self.assertTrue(os.path.exists(self.session_path))
        self.assertEqual(os.stat(self.session_path).st_mode & 0o777, 0o600)
        log = subprocess.run(
            ["git", "-C", self.repo, "log", "--oneline"], capture_output=True, text=True
        )
        self.assertIn("Initialize store", log.stdout)

    def test_init_twice_fails(self) -> None:
        self.init_store()
        r = self.fafnir("init", stdin=f"{PASSPHRASE}\n{PASSPHRASE}\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)

    def test_init_passphrase_mismatch_fails(self) -> None:
        r = self.fafnir("init", stdin="abc\nxyz\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("do not match", r.stderr)
        self.assertFalse(os.path.exists(self.repo))

    def test_init_empty_passphrase_fails(self) -> None:
        r = self.fafnir("init", stdin="\n\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("empty", r.stderr)

    def test_commands_without_store_fail(self) -> None:
        r = self.fafnir("push")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No fafnir store", r.stderr)


class StoreFormatTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()
        self.track_docs()
        self.write("tax/2025/notice-from-the-office.pdf", "my taxable income is 42")
        self.assertEqual(self.fafnir("push", "-f").returncode, 0)

    def test_nothing_readable_is_stored(self) -> None:
        """Neither content nor real filenames may appear anywhere in the repo."""
        secrets = [b"my taxable income is 42", b"notice-from-the-office", b"tax"]
        for dirpath, _, filenames in os.walk(self.repo):
            for name in filenames:
                path = os.path.join(dirpath, name)
                self.assertNotIn(
                    "notice-from-the-office", name, f"real filename leaked in {path}"
                )
                with open(path, "rb") as f:
                    raw = f.read()
                for secret in secrets:
                    self.assertNotIn(secret, raw, f"{secret!r} leaked in {path}")

    def test_blobs_are_age_files(self) -> None:
        names = os.listdir(self.blobs)
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^[0-9a-f]{64}\.age$")
        for path in (
            os.path.join(self.blobs, names[0]),
            os.path.join(self.repo, "index.age"),
            os.path.join(self.repo, "identity.age"),
        ):
            with open(path, "rb") as f:
                self.assertTrue(f.read(len(AGE_MAGIC)) == AGE_MAGIC, path)

    def test_config_is_local_and_not_committed(self) -> None:
        tracked = subprocess.run(
            ["git", "-C", self.repo, "ls-files"], capture_output=True, text=True
        ).stdout.split()
        self.assertNotIn("config.toml", tracked)
        self.assertIn("index.age", tracked)
        self.assertIn("identity.age", tracked)
        self.assertTrue(any(f.startswith("blobs/") for f in tracked))


class ConfigTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()

    def test_add_and_list_root(self) -> None:
        self.track_docs()
        out = self.fafnir("config", "list").stdout
        self.assertIn("docs", out)
        self.assertIn(self.docs, out)

    def test_add_root_with_explicit_name(self) -> None:
        r = self.fafnir("config", "add", self.docs, "--name", "papers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("papers", self.fafnir("config", "list").stdout)

    def test_nested_root_rejected(self) -> None:
        self.track_docs()
        nested = os.path.join(self.docs, "sub")
        os.makedirs(nested)
        r = self.fafnir("config", "add", nested)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nested", r.stderr)

    def test_parent_of_existing_root_rejected(self) -> None:
        self.track_docs()
        r = self.fafnir("config", "add", self.home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nested", r.stderr)

    def test_duplicate_root_rejected(self) -> None:
        self.track_docs()
        r = self.fafnir("config", "add", self.docs)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already tracked", r.stderr)

    def test_add_non_directory_rejected(self) -> None:
        path = self.write("a.txt", "x")
        r = self.fafnir("config", "add", path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not a directory", r.stderr)

    def test_rm_root_by_name_and_path(self) -> None:
        self.track_docs()
        self.assertEqual(self.fafnir("config", "rm", "docs").returncode, 0)
        self.assertIn("(none)", self.fafnir("config", "list").stdout)
        self.track_docs()
        self.assertEqual(self.fafnir("config", "rm", self.docs).returncode, 0)
        self.assertIn("(none)", self.fafnir("config", "list").stdout)

    def test_set_get_settings(self) -> None:
        self.assertEqual(self.fafnir("config", "set", "session-timeout", "60").returncode, 0)
        self.assertEqual(self.fafnir("config", "get", "session-timeout").stdout.strip(), "60")
        self.assertIn("session-timeout = 60", self.fafnir("config", "list").stdout)

    def test_set_unknown_key_fails(self) -> None:
        r = self.fafnir("config", "set", "bogus", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Unknown config key", r.stderr)

    def test_set_non_integer_value_fails(self) -> None:
        r = self.fafnir("config", "set", "max-file-size", "big")
        self.assertEqual(r.returncode, 1)
        self.assertIn("must be an integer", r.stderr)

    def test_settings_survive_a_round_trip(self) -> None:
        self.track_docs()
        self.fafnir("config", "set", "remote", "git@example.invalid:me/store.git")
        out = self.fafnir("config", "list").stdout
        self.assertIn("git@example.invalid:me/store.git", out)
        self.assertIn("docs", out)


class PushTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()
        self.track_docs()

    def test_push_adds_files(self) -> None:
        self.write("a.pdf", "alpha")
        self.write("tax/b.pdf", "beta")
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added", r.stdout)
        self.assertIn("docs/tax/b.pdf", r.stdout)
        self.assertEqual(self.blob_count(), 2)

    def test_push_is_idempotent(self) -> None:
        self.write("a.pdf", "alpha")
        self.fafnir("push", "-f")
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nothing to push", r.stdout)
        self.assertEqual(self.blob_count(), 1)

    def test_modified_and_deleted(self) -> None:
        self.write("a.pdf", "alpha")
        self.write("b.pdf", "beta")
        self.fafnir("push", "-f")
        self.write("a.pdf", "alpha v2")
        os.unlink(os.path.join(self.docs, "b.pdf"))
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("modified", r.stdout)
        self.assertIn("deleted", r.stdout)
        # The old and orphaned blobs are pruned from the working tree.
        self.assertEqual(self.blob_count(), 1)

    def test_identical_content_is_deduplicated(self) -> None:
        self.write("a.pdf", "same bytes")
        self.write("copies/b.pdf", "same bytes")
        self.fafnir("push", "-f")
        self.assertEqual(self.blob_count(), 1)
        # ...and removing one copy keeps the blob alive for the other.
        os.unlink(os.path.join(self.docs, "a.pdf"))
        self.fafnir("push", "-f")
        self.assertEqual(self.blob_count(), 1)

    def test_touching_a_file_is_not_a_change(self) -> None:
        self.write("a.pdf", "alpha")
        self.fafnir("push", "-f")
        os.utime(os.path.join(self.docs, "a.pdf"), (0, 0))
        r = self.fafnir("push", "-f")
        self.assertIn("Nothing to push", r.stdout)

    def test_ignore_rules_are_honoured(self) -> None:
        with open(os.path.join(self.fafnir_home, "ignore"), "a") as f:
            f.write("*.tmp\ndrafts/\n")
        self.write("keep.pdf", "keep")
        self.write("skip.tmp", "skip")
        self.write("drafts/skip.pdf", "skip")
        r = self.fafnir("push", "-f")
        self.assertIn("docs/keep.pdf", r.stdout)
        self.assertNotIn("skip", r.stdout)
        self.assertEqual(self.blob_count(), 1)

    def test_oversized_file_is_refused(self) -> None:
        self.fafnir("config", "set", "max-file-size", "1024")
        self.write("small.pdf", "ok")
        self.write("huge.pdf", "x" * 2048)
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 1)
        self.assertIn("docs/huge.pdf", r.stderr)
        self.assertIn("over the", r.stderr)
        # Nothing was stored: the push is refused as a whole.
        self.assertEqual(self.blob_count(), 0)

    def test_unavailable_root_is_not_a_deletion(self) -> None:
        other = os.path.join(self.home, "volume")
        os.makedirs(other)
        self.write("x.pdf", "on the drive", root=other)
        self.fafnir("config", "add", other, "--name", "scans")
        self.fafnir("push", "-f")
        self.assertEqual(self.blob_count(), 1)

        shutil.rmtree(other)  # the drive is unmounted
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nothing to push", r.stdout)
        self.assertIn("unavailable", r.stderr)
        self.assertNotIn("deleted", r.stdout)
        self.assertEqual(self.blob_count(), 1)

    def test_unbound_root_is_reported_not_deleted(self) -> None:
        """`config rm` unregisters a root; it does not delete stored history."""
        other = os.path.join(self.home, "other")
        os.makedirs(other)
        self.write("a.pdf", "alpha")
        self.write("b.pdf", "beta", root=other)
        self.fafnir("config", "add", other, "--name", "other")
        self.fafnir("push", "-f")
        self.assertEqual(self.blob_count(), 2)

        self.assertEqual(self.fafnir("config", "rm", "docs").returncode, 0)
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not bound on this machine", r.stderr)
        self.assertNotIn("deleted", r.stdout)
        self.assertEqual(self.blob_count(), 2)

    def test_push_without_roots_fails(self) -> None:
        self.fafnir("config", "rm", "docs")
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No roots configured", r.stderr)

    def test_confirmation_is_required_without_force(self) -> None:
        self.write("a.pdf", "alpha")
        r = self.fafnir("push", stdin="n\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Aborted", r.stdout)
        self.assertEqual(self.blob_count(), 0)

        r = self.fafnir("push", stdin="y\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.blob_count(), 1)

    def test_push_without_remote_commits_locally(self) -> None:
        self.write("a.pdf", "alpha")
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("committed locally only", r.stderr)

    def test_push_to_remote(self) -> None:
        remote = self.bare_remote()
        self.fafnir("config", "set", "remote", remote)
        self.write("a.pdf", "alpha")
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Pushed to the remote", r.stdout)
        refs = subprocess.run(
            ["git", "-C", remote, "log", "--oneline", "main"],
            capture_output=True,
            text=True,
        )
        self.assertIn("Update store", refs.stdout)


class FidelityTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()
        self.track_docs()

    def test_symlinks_are_skipped(self) -> None:
        self.write("real.pdf", "real")
        os.symlink(os.path.join(self.docs, "real.pdf"), os.path.join(self.docs, "link.pdf"))
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(self.blob_count(), 1)

    def test_symlinked_directories_are_not_followed(self) -> None:
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        self.write("secret.pdf", "not mine", root=outside)
        os.symlink(outside, os.path.join(self.docs, "linked"))
        self.write("real.pdf", "real")
        r = self.fafnir("push", "-f")
        self.assertEqual(self.blob_count(), 1)
        self.assertNotIn("linked", r.stdout)

    def test_empty_directories_are_dropped(self) -> None:
        os.makedirs(os.path.join(self.docs, "empty"))
        self.write("a.pdf", "alpha")
        self.fafnir("push", "-f")
        shutil.rmtree(self.docs)
        os.makedirs(self.docs)
        self.fafnir("pull", "-f")
        self.assertFalse(os.path.exists(os.path.join(self.docs, "empty")))
        self.assertTrue(os.path.exists(os.path.join(self.docs, "a.pdf")))

    def test_unicode_and_spaces_in_paths(self) -> None:
        """Administrative documents are full of accents, spaces and quotes."""
        name = "impôts/déclaration d'impôt 2025.pdf"
        self.write(name, "montant dû: 1234 CHF")
        r = self.fafnir("push", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(name, r.stdout)

        os.unlink(os.path.join(self.docs, name))
        self.assertEqual(self.fafnir("pull", "-f").returncode, 0)
        self.assertEqual(self.read(name), "montant dû: 1234 CHF")

    def test_restored_files_are_not_world_readable(self) -> None:
        self.write("a.pdf", "alpha")
        self.fafnir("push", "-f")
        os.unlink(os.path.join(self.docs, "a.pdf"))
        self.fafnir("pull", "-f")
        mode = os.stat(os.path.join(self.docs, "a.pdf")).st_mode & 0o777
        self.assertEqual(mode & 0o007, 0, oct(mode))


class PullTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()
        self.track_docs()
        self.write("a.pdf", "alpha")
        self.write("tax/b.pdf", "beta")
        self.assertEqual(self.fafnir("push", "-f").returncode, 0)

    def test_pull_restores_deleted_files(self) -> None:
        shutil.rmtree(os.path.join(self.docs, "tax"))
        os.unlink(os.path.join(self.docs, "a.pdf"))
        r = self.fafnir("pull", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("a.pdf"), "alpha")
        self.assertEqual(self.read("tax/b.pdf"), "beta")

    def test_pull_overwrites_local_modifications(self) -> None:
        self.write("a.pdf", "locally mangled")
        r = self.fafnir("pull", "-f")
        self.assertIn("overwrite", r.stdout)
        self.assertEqual(self.read("a.pdf"), "alpha")

    def test_pull_never_deletes_local_only_files(self) -> None:
        self.write("unpushed.txt", "brand new")
        r = self.fafnir("pull", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Local-only", r.stdout)
        self.assertEqual(self.read("unpushed.txt"), "brand new")

    def test_pull_restores_mtime(self) -> None:
        path = os.path.join(self.docs, "a.pdf")
        mtime = int(os.stat(path).st_mtime)
        os.unlink(path)
        self.fafnir("pull", "-f")
        self.assertEqual(int(os.stat(path).st_mtime), mtime)
        # A restored file must not look modified on the next scan.
        self.assertIn("Nothing to push", self.fafnir("push", "-f").stdout)

    def test_pull_is_idempotent(self) -> None:
        r = self.fafnir("pull", "-f")
        self.assertIn("Nothing to restore", r.stdout)

    def test_confirmation_is_required_without_force(self) -> None:
        self.write("a.pdf", "locally mangled")
        r = self.fafnir("pull", stdin="n\n")
        self.assertIn("Aborted", r.stdout)
        self.assertEqual(self.read("a.pdf"), "locally mangled")


class SessionTests(CliTestCase):
    def test_session_is_reused_without_passphrase(self) -> None:
        self.init_store()  # init opens a session
        self.track_docs()
        self.write("a.pdf", "alpha")
        r = self.fafnir("push", "-f")  # no passphrase on stdin
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_lock_then_reprompt(self) -> None:
        self.init_store()
        self.track_docs()
        self.assertEqual(self.fafnir("lock").returncode, 0)
        self.assertFalse(os.path.exists(self.session_path))
        r = self.fafnir("push", "-f", stdin=f"{PASSPHRASE}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.session_path))

    def test_wrong_passphrase_fails_and_leaves_no_session(self) -> None:
        self.init_store()
        self.fafnir("lock")
        r = self.fafnir("verify", stdin="wrong-passphrase\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Incorrect passphrase", r.stderr)
        self.assertFalse(os.path.exists(self.session_path))

    def test_unlock_command(self) -> None:
        self.init_store()
        self.fafnir("lock")
        r = self.fafnir("unlock", stdin=f"{PASSPHRASE}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Unlocked", r.stdout)
        self.assertTrue(os.path.exists(self.session_path))

    def test_session_expires_after_the_idle_timeout(self) -> None:
        self.init_store()
        self.fafnir("config", "set", "session-timeout", "1")
        time.sleep(1.2)
        # Expired: without a passphrase on stdin the command cannot proceed.
        r = self.fafnir("verify", stdin="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No passphrase", r.stderr)
        self.assertFalse(os.path.exists(self.session_path))

    def test_session_holds_a_usable_age_identity(self) -> None:
        """The cache is a plain age key file, so `age -i` works by hand."""
        self.init_store()
        with open(self.session_path) as f:
            content = f.read()
        self.assertIn("AGE-SECRET-KEY-1", content)
        self.assertIn("# public key: age1", content)


class VerifyTests(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.init_store()
        self.track_docs()
        self.write("a.pdf", "alpha")
        self.fafnir("push", "-f")

    def test_verify_passes_on_a_clean_store(self) -> None:
        r = self.fafnir("verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("All blobs decrypt and match the index", r.stdout)
        self.assertIn("Source folders match the store", r.stdout)

    def test_verify_detects_a_corrupted_blob(self) -> None:
        blob = os.path.join(self.blobs, os.listdir(self.blobs)[0])
        with open(blob, "r+b") as f:
            f.seek(-1, os.SEEK_END)
            f.write(b"\x00")
        r = self.fafnir("verify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot decrypt blob", r.stderr)

    def test_verify_detects_a_missing_blob(self) -> None:
        os.unlink(os.path.join(self.blobs, os.listdir(self.blobs)[0]))
        r = self.fafnir("verify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Missing blob for docs/a.pdf", r.stderr)

    def test_verify_reports_drift(self) -> None:
        self.write("b.pdf", "beta")
        os.unlink(os.path.join(self.docs, "a.pdf"))
        r = self.fafnir("verify")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Drift vs the source folders", r.stdout)
        self.assertIn("docs/b.pdf", r.stdout)
        self.assertIn("docs/a.pdf", r.stdout)


class RecoveryTests(CliTestCase):
    """The flow that makes fafnir a real backup: a bare machine, a remote."""

    def setUp(self) -> None:
        super().setUp()
        self.remote = self.bare_remote()
        self.init_store()
        self.track_docs()
        self.fafnir("config", "set", "remote", self.remote)
        self.write("a.pdf", "alpha")
        self.write("tax/2025/notice.pdf", "my taxable income is 42")
        self.assertEqual(self.fafnir("push", "-f").returncode, 0)

    def test_clone_and_pull_on_a_fresh_machine(self) -> None:
        home2 = self.other_home()
        restore = os.path.join(home2, "restored")
        os.makedirs(restore)

        r = self.fafnir(
            "clone", self.remote, "--root", f"docs={restore}",
            stdin=f"{PASSPHRASE}\n", home=home2,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 file(s)", r.stdout)
        # clone alone restores nothing: pull does the decrypt-and-write-back.
        self.assertEqual(os.listdir(restore), [])

        r = self.fafnir("pull", "-f", home=home2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("a.pdf", root=restore), "alpha")
        self.assertEqual(
            self.read("tax/2025/notice.pdf", root=restore), "my taxable income is 42"
        )

    def test_clone_with_a_wrong_passphrase_fails(self) -> None:
        home2 = self.other_home()
        r = self.fafnir("clone", self.remote, stdin="not-the-passphrase\n", home=home2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Incorrect passphrase", r.stderr)

    def test_clone_onto_an_existing_store_fails(self) -> None:
        r = self.fafnir("clone", self.remote, stdin=f"{PASSPHRASE}\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stderr)

    def test_clone_rejects_a_repo_that_is_not_a_store(self) -> None:
        empty = self.bare_remote()
        subprocess.run(
            ["git", "-C", empty, "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, capture_output=True,
        )
        home2 = self.other_home()
        r = self.fafnir("clone", empty, home=home2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot fetch branch", r.stderr)

    def test_diverged_store_requires_force_and_loses_nothing(self) -> None:
        home2 = self.other_home()
        restore = os.path.join(home2, "restored")
        os.makedirs(restore)
        self.fafnir(
            "clone", self.remote, "--root", f"docs={restore}",
            stdin=f"{PASSPHRASE}\n", home=home2,
        )
        self.fafnir("pull", "-f", home=home2)

        # Machine 2 stores a document, moving the remote forward.
        self.write("from-two.pdf", "two", root=restore)
        self.assertEqual(self.fafnir("push", "-f", home=home2).returncode, 0)

        # Machine 1 commits its own document without having pulled first: the
        # histories have diverged, so the push is rejected.
        self.write("from-one.pdf", "one")
        self.assertEqual(self.fafnir("push", "-f").returncode, 1)

        r = self.fafnir("pull")
        self.assertEqual(r.returncode, 1)
        self.assertIn("diverged", r.stderr)

        # -f discards the local commit in favour of the remote...
        r = self.fafnir("pull", "-f")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("from-two.pdf"), "two")
        # ...but never the local file, which the next push stores for real.
        self.assertEqual(self.read("from-one.pdf"), "one")
        self.assertEqual(self.fafnir("push", "-f").returncode, 0)
        # Both machines' documents are now in the store, with no drift left.
        out = self.fafnir("verify").stdout
        self.assertIn("4 tracked file(s)", out)
        self.assertIn("Source folders match the store", out)

    def test_changes_round_trip_between_machines(self) -> None:
        home2 = self.other_home()
        restore = os.path.join(home2, "restored")
        os.makedirs(restore)
        self.fafnir(
            "clone", self.remote, "--root", f"docs={restore}",
            stdin=f"{PASSPHRASE}\n", home=home2,
        )
        self.fafnir("pull", "-f", home=home2)

        # Machine 1 stores a new document...
        self.write("later.pdf", "written later")
        self.assertEqual(self.fafnir("push", "-f").returncode, 0)

        # ...machine 2 pulls it.
        r = self.fafnir("pull", "-f", home=home2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("later.pdf", root=restore), "written later")


if __name__ == "__main__":
    unittest.main()
