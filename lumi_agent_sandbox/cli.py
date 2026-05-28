from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .sandbox import (
    DEFAULT_ACCOUNT,
    DEFAULT_AGENT_IMAGE,
    agent_image_from_env,
    agent_image_override_from_env,
    account_from_env,
    create_sandbox,
    destroy_sandbox,
    enter_sandbox,
    load_sandbox,
    sandbox_root,
    shell_sandbox,
)
from .slurm import PolicyError, submit_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumi-agent-sandbox")
    parser.add_argument("--root", help="sandbox root, default: $LUMI_AGENT_SANDBOX_ROOT or /scratch/<account>/$USER/agent-sandboxes")
    parser.add_argument("--account", help=f"LUMI project/account, default: $LUMI_ACCOUNT, $PROJECT, or {DEFAULT_ACCOUNT}")
    parser.add_argument("--agent-image", help=f"agent Singularity image, default: $LUMI_AGENT_IMAGE, $LUMI_AGENT_SIF, or {DEFAULT_AGENT_IMAGE}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a task sandbox")
    create.add_argument("task")
    create.add_argument("--force", action="store_true")

    enter = subparsers.add_parser("enter", help="enter the agent container")
    enter.add_argument("task")

    shell = subparsers.add_parser("shell", help="open a shell in the sandbox container")
    shell.add_argument("task")

    submit = subparsers.add_parser("submit", help="validate and submit a Slurm script")
    submit.add_argument("task")
    submit.add_argument("script")
    submit.add_argument("--dry-run", action="store_true")

    destroy = subparsers.add_parser("destroy", help="delete a task sandbox")
    destroy.add_argument("task")
    destroy.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)

    try:
        account = account_from_env(args.account)
        root = sandbox_root(args.root, account)

        if args.command == "create":
            agent_image = agent_image_from_env(args.agent_image)
            sandbox = create_sandbox(args.task, root, account, agent_image, args.force)
            print(sandbox.path)
            return 0

        sandbox = load_sandbox(args.task, root, account, agent_image_override_from_env(args.agent_image))

        if args.command == "enter":
            enter_sandbox(sandbox)
            return 0

        if args.command == "shell":
            shell_sandbox(sandbox)
            return 0

        if args.command == "submit":
            script = Path(args.script)
            if not script.is_absolute():
                script = sandbox.path / script
            print(submit_job(sandbox, script, args.dry_run))
            return 0

        if args.command == "destroy":
            destroy_sandbox(sandbox, args.yes)
            return 0

    except (FileExistsError, FileNotFoundError, ValueError, PolicyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 1
