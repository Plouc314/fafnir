from __future__ import annotations

import argparse
import sys

from fafnir import commands
from fafnir.config import Config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fafnir", description="Encrypted, git-backed store for administrative documents"
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    init_p = sub.add_parser("init", help="Create a new store and set the passphrase")
    init_p.set_defaults(func=commands.cmd_init)

    clone_p = sub.add_parser("clone", help="Bootstrap a store from an existing remote")
    clone_p.add_argument("url", help="Git remote URL of the store")
    clone_p.add_argument("-b", "--branch", default=None, help="Branch to clone")
    clone_p.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Bind a stored root to a local path (repeatable)",
    )
    clone_p.set_defaults(func=commands.cmd_clone)

    unlock_p = sub.add_parser("unlock", help="Start a session (caches the identity)")
    unlock_p.set_defaults(func=commands.cmd_unlock)

    lock_p = sub.add_parser("lock", help="End the current session")
    lock_p.set_defaults(func=commands.cmd_lock)

    push_p = sub.add_parser("push", help="Encrypt local changes and push the store")
    push_p.add_argument("-f", "--force", action="store_true", help="Skip confirmation")
    push_p.set_defaults(func=commands.cmd_push)

    pull_p = sub.add_parser("pull", help="Pull the store and restore documents")
    pull_p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Skip confirmation and discard local divergence",
    )
    pull_p.set_defaults(func=commands.cmd_pull)

    verify_p = sub.add_parser("verify", help="Check the store's integrity and drift")
    verify_p.set_defaults(func=commands.cmd_verify)

    config_p = sub.add_parser("config", help="Manage roots and settings")
    config_p.set_defaults(func=lambda *_: config_p.print_help())
    config_sub = config_p.add_subparsers(dest="config_command", metavar="subcommand")

    cfg_add = config_sub.add_parser("add", help="Track a folder as a named root")
    cfg_add.add_argument("path")
    cfg_add.add_argument("-n", "--name", default=None, help="Root name (default: folder name)")
    cfg_add.set_defaults(func=commands.cmd_config_add)

    cfg_rm = config_sub.add_parser("rm", help="Stop tracking a root")
    cfg_rm.add_argument("root", metavar="name|path")
    cfg_rm.set_defaults(func=commands.cmd_config_rm)

    cfg_set = config_sub.add_parser("set", help="Set a config value")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg_set.set_defaults(func=commands.cmd_config_set)

    cfg_get = config_sub.add_parser("get", help="Get a config value")
    cfg_get.add_argument("key")
    cfg_get.set_defaults(func=commands.cmd_config_get)

    cfg_list = config_sub.add_parser("list", help="Show roots, remote, branch and settings")
    cfg_list.set_defaults(func=commands.cmd_config_list)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return

    try:
        func(args, Config())
    except KeyboardInterrupt:
        # Every command prompts at some point; Ctrl-C should read as "aborted",
        # not as a crash.
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
