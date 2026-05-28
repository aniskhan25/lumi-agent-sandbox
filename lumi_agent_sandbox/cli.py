from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .sandbox import (
    DEFAULT_ACCOUNT,
    DEFAULT_AGENT_IMAGE,
    account_from_env,
    archive_sandbox,
    create_sandbox,
    destroy_sandbox,
    diff_sandbox,
    enter_sandbox,
    list_sandboxes,
    load_sandbox,
    sandbox_root,
)
from .slurm import PolicyError, submit_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumi-agent-sandbox")
    parser.add_argument("--root", help="sandbox root, default: $LUMI_AGENT_SANDBOX_ROOT or /scratch/<account>/agent-sandboxes")
    parser.add_argument("--account", help=f"LUMI project/account, default: $LUMI_ACCOUNT, $PROJECT, or {DEFAULT_ACCOUNT}")
    parser.add_argument("--agent-image", default=DEFAULT_AGENT_IMAGE, help="agent Singularity image")

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a task sandbox")
    create.add_argument("task")
    create.add_argument("--force", action="store_true")

    subparsers.add_parser("list", help="list task sandboxes")

    enter = subparsers.add_parser("enter", help="enter the agent container")
    enter.add_argument("task")

    submit = subparsers.add_parser("submit", help="validate and submit a Slurm script")
    submit.add_argument("task")
    submit.add_argument("script")
    submit.add_argument("--dry-run", action="store_true")

    status = subparsers.add_parser("status", help="show Slurm queue for this account")
    status.add_argument("task")

    diff = subparsers.add_parser("diff", help="show changes in work/")
    diff.add_argument("task")

    archive = subparsers.add_parser("archive", help="archive task outputs, jobs, logs, and diff")
    archive.add_argument("task")

    destroy = subparsers.add_parser("destroy", help="delete a task sandbox")
    destroy.add_argument("task")
    destroy.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)

    try:
        account = account_from_env(args.account)
        root = sandbox_root(args.root, account)

        if args.command == "create":
            sandbox = create_sandbox(args.task, root, account, args.agent_image, args.force)
            print(sandbox.path)
            return 0

        if args.command == "list":
            for name in list_sandboxes(root):
                print(name)
            return 0

        sandbox = load_sandbox(args.task, root, account, args.agent_image)

        if args.command == "enter":
            enter_sandbox(sandbox)
            return 0

        if args.command == "submit":
            script = Path(args.script)
            if not script.is_absolute():
                script = sandbox.path / script
            print(submit_job(sandbox, script, args.dry_run))
            return 0

        if args.command == "status":
            return _status(sandbox.account)

        if args.command == "diff":
            print(diff_sandbox(sandbox), end="")
            return 0

        if args.command == "archive":
            print(archive_sandbox(sandbox))
            return 0

        if args.command == "destroy":
            destroy_sandbox(sandbox, args.yes)
            return 0

    except (FileExistsError, FileNotFoundError, ValueError, PolicyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 1


def _status(account: str) -> int:
    import subprocess

    result = subprocess.run(["squeue", "-A", account], text=True, check=False)
    return result.returncode
