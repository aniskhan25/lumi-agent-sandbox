from __future__ import annotations

import argparse
import sys

from . import audit, broker, report
from .policy import CONFIG_FILE, PolicyError, find_site_config, require_egress_enforcement, site_config
from .sandbox import (
    create_sandbox,
    destroy_sandbox,
    enter_sandbox,
    load_sandbox,
    resolve_account,
    resolve_agent_image,
    sandbox_root,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumi-agent-sandbox")
    parser.add_argument("--root", help="sandbox root, default: /scratch/<account>/$USER/agent-sandboxes")
    parser.add_argument("--account", help=f"LUMI project/account, default: {CONFIG_FILE}")
    parser.add_argument("--agent-image", help=f"agent Singularity image, default: {CONFIG_FILE}")
    parser.add_argument("--site-config", help=f"site policy file, default: {CONFIG_FILE}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a task sandbox")
    create.add_argument("task")

    enter = subparsers.add_parser("enter", help="enter the agent container and run the broker")
    enter.add_argument("task")
    enter.add_argument("--no-broker", action="store_true", help="do not answer agent job requests")

    run_broker = subparsers.add_parser("broker", help="answer agent job requests for a task")
    run_broker.add_argument("task")

    submit = subparsers.add_parser("submit", help="validate and submit a Slurm script")
    submit.add_argument("task")
    submit.add_argument("script")
    submit.add_argument("--partition")
    submit.add_argument("--time")
    submit.add_argument("--nodes")
    submit.add_argument("--gpus")
    submit.add_argument("--dry-run", action="store_true")

    job_status = subparsers.add_parser("status", help="state of a job submitted from this sandbox")
    job_status.add_argument("task")
    job_status.add_argument("job_id")

    job_cancel = subparsers.add_parser("cancel", help="cancel a job submitted from this sandbox")
    job_cancel.add_argument("task")
    job_cancel.add_argument("job_id")

    inspect = subparsers.add_parser("inspect", help="show what this sandbox does and does not enforce")
    inspect.add_argument("task")

    check = subparsers.add_parser("verify", help="actively test the sandbox's containment")
    check.add_argument("task")

    destroy = subparsers.add_parser("destroy", help="delete a task sandbox")
    destroy.add_argument("task")
    destroy.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)

    try:
        site = site_config(args.site_config)
        if args.command in ("create", "enter", "broker"):
            # Only refuse to *start* an agent. inspect and verify must stay
            # usable, since they are how you find out why this failed.
            require_egress_enforcement(site)
        account = resolve_account(args.account, site)
        root = sandbox_root(args.root, account)

        if args.command == "create":
            agent_image = resolve_agent_image(args.agent_image, site)
            sandbox = create_sandbox(args.task, root, account, agent_image, site)
            audit.write_manifest(sandbox, site)
            print(sandbox.path)
            return 0

        sandbox = load_sandbox(args.task, root)

        if args.command == "enter":
            serve = None if args.no_broker else (lambda: broker.serve(sandbox, site))
            return enter_sandbox(sandbox, site, serve)

        if args.command == "broker":
            broker.serve(sandbox, site)
            return 0

        if args.command == "submit":
            flags = {key: getattr(args, key) for key in ("partition", "time", "nodes", "gpus")}
            request = {"script": args.script, **{k: v for k, v in flags.items() if v}}
            print(broker.submit(sandbox, site, request, dry_run=args.dry_run))
            return 0

        if args.command == "status":
            print(broker.status(sandbox, site, args.job_id))
            return 0

        if args.command == "cancel":
            print(broker.cancel(sandbox, site, args.job_id))
            return 0

        if args.command == "inspect":
            print(report.inspect(sandbox, site, find_site_config(args.site_config)))
            return 0

        if args.command == "verify":
            result = report.verify(sandbox, site)
            print(report.format_verification(result))
            return 1 if result["failed"] else 0

        if args.command == "destroy":
            kept = destroy_sandbox(sandbox, args.yes)
            if kept:
                print(f"audit trail kept at {kept}")
            return 0

    except (FileExistsError, FileNotFoundError, ValueError, PolicyError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 1
