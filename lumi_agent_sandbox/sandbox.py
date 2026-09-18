from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import agentconfig
from .policy import CONFIG_FILE, DEFAULT_JOB_OPTIONS, read_yaml


TASK_RE = re.compile(r"[^a-zA-Z0-9._-]+")
SANDBOX_DIRS = ("work", "input", "output", "jobs", "logs", "requests", "audit", "agent", "state/home", "wrappers")


@dataclass(frozen=True)
class Sandbox:
    task: str
    root: Path
    account: str
    agent_image: str

    @property
    def path(self) -> Path:
        return self.root / self.task


def task_id(name: str) -> str:
    cleaned = TASK_RE.sub("-", name.strip()).strip(".-_").lower()
    if not cleaned:
        raise ValueError("task name must contain at least one letter or number")
    return cleaned


def resolve_account(value: str | None, config: dict[str, object] | None = None) -> str:
    account = value or (config or {}).get("account")
    if not account:
        raise ValueError(f"provide --account or add account to {CONFIG_FILE}")
    return str(account)


def resolve_agent_image(value: str | None, config: dict[str, object] | None = None) -> str:
    image = value or (config or {}).get("agent_image")
    if not image:
        raise ValueError(f"provide --agent-image or add agent_image to {CONFIG_FILE}")
    return str(image)


def sandbox_root(value: str | None, account: str) -> Path:
    if value:
        return Path(value).expanduser()

    user = os.environ.get("USER")
    if not user:
        raise ValueError("provide --root or set USER")
    return Path(f"/scratch/{account}/{user}/agent-sandboxes")


def create_sandbox(
    name: str,
    root: Path,
    account: str,
    agent_image: str,
    site: dict[str, object] | None = None,
) -> Sandbox:
    site = site or {}
    sandbox = Sandbox(task_id(name), root.resolve(), account, agent_image)
    if sandbox.path.exists():
        raise FileExistsError(f"sandbox already exists: {sandbox.path}")

    for child in SANDBOX_DIRS:
        (sandbox.path / child).mkdir(parents=True, exist_ok=True)

    _write_policy(sandbox)
    agentconfig.write_config(sandbox.path, site)
    write_enter_script(sandbox, site)
    _write_command_wrappers(sandbox)
    return sandbox


def load_sandbox(name: str, root: Path) -> Sandbox:
    task = task_id(name)
    policy_path = root / task / "policy.yaml"
    if not policy_path.exists():
        raise FileNotFoundError(f"sandbox not found: {root / task}")
    policy = read_yaml(policy_path)
    return Sandbox(task, root.resolve(), str(policy["account"]), str(policy["agent_image"]))


def sandbox_policy(sandbox: Sandbox) -> dict[str, object]:
    """The sandbox's own policy. Agent-writable, so it may only narrow site limits."""
    return read_yaml(sandbox.path / "policy.yaml")


def mount_args(sandbox: Sandbox) -> list[str]:
    """Bind mounts shared by the agent container and by submitted jobs."""
    path = sandbox.path
    return [
        "--home", f"{path}/state/home:/home/agent",
        "--bind", f"{path}/work:/workspace",
        "--bind", f"{path}/input:/input:ro",
        "--bind", f"{path}/output:/output",
        "--bind", f"{path}/logs:/logs",
    ]


def agent_mount_args(sandbox: Sandbox) -> list[str]:
    """The agent also gets jobs/, the request channel, and the /safe-bin wrappers.

    Jobs deliberately get none of these: a job that could write to requests/
    would be able to submit further jobs and escape the session budget.
    """
    path = sandbox.path
    return mount_args(sandbox) + [
        "--bind", f"{path}/jobs:/jobs",
        "--bind", f"{path}/requests:/requests",
        "--bind", f"{path}/wrappers:/safe-bin:ro",
    ]


def enter_sandbox(
    sandbox: Sandbox,
    site: dict[str, object] | None = None,
    serve: Callable[[], None] | None = None,
) -> int:
    """Run the agent container in the foreground.

    `serve` is the broker loop; it runs as a child for exactly as long as the
    container does, so the agent's request channel is answered only while a
    session is actually open. It is injected rather than imported to keep the
    dependency pointing one way.
    """
    site = site or {}
    agentconfig.write_config(sandbox.path, site)
    script = write_enter_script(sandbox, site)
    pid = _fork(serve) if serve else 0
    try:
        return subprocess.run(["/bin/sh", str(script)], check=False).returncode
    finally:
        if pid:
            _terminate(pid)


def _fork(serve: Callable[[], None]) -> int:
    pid = os.fork()
    if pid:
        return pid
    try:
        serve()
    finally:
        os._exit(0)


def _terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def destroy_sandbox(sandbox: Sandbox, yes: bool) -> Path | None:
    if not yes:
        raise ValueError("destroy requires --yes")
    root = sandbox.root.resolve()
    target = sandbox.path.resolve()
    if root == target or root not in target.parents:
        raise ValueError(f"refusing to delete path outside sandbox root: {target}")
    kept = archive_audit(sandbox)
    shutil.rmtree(target)
    return kept


def archive_audit(sandbox: Sandbox) -> Path | None:
    """Copy the audit trail out before the sandbox goes.

    An audit record that is deleted along with the thing it describes is not an
    audit record, so the manifest, job log and verification results outlive it.
    """
    audit = sandbox.path / "audit"
    if not audit.is_dir() or not any(audit.iterdir()):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    destination = sandbox.root / ".audit" / f"{sandbox.task}-{stamp}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(audit, destination)
    return destination


def _write_policy(sandbox: Sandbox) -> None:
    data = {
        "account": sandbox.account,
        "agent_image": sandbox.agent_image,
        "defaults": dict(DEFAULT_JOB_OPTIONS),
    }
    header = (
        "# Per-sandbox policy.\n"
        "# A 'limits:' section here may only narrow the site limits; any value that\n"
        f"# would loosen them is ignored. Site limits live in {CONFIG_FILE}.\n"
    )
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    (sandbox.path / "policy.yaml").write_text(header + body, encoding="utf-8")


def write_enter_script(sandbox: Sandbox, site: dict[str, object] | None = None) -> Path:
    site = site or {}
    mounts = _shell_args(agent_mount_args(sandbox) + agentconfig.config_mount(sandbox.path, site))
    environment = {"SINGULARITYENV_PREPEND_PATH": "/safe-bin"}
    environment.update({f"SINGULARITYENV_{k}": v for k, v in agentconfig.container_env(site).items()})
    exports = " \\\n  ".join(f"{key}={shlex.quote(value)}" for key, value in environment.items())
    script = f"""#!/bin/sh
set -eu

AGENT_IMAGE={shlex.quote(sandbox.agent_image)}

if [ ! -r "$AGENT_IMAGE" ]; then
  echo "Agent image not found or not readable: $AGENT_IMAGE" >&2
  echo "Add agent_image to {CONFIG_FILE} or recreate with --agent-image /path/to/agent.sif." >&2
  exit 2
fi

exec env \\
  {exports} \\
  singularity run \\
  --cleanenv \\
  --containall \\
  --pwd /workspace \\
  {mounts} \\
  "$AGENT_IMAGE"
"""
    path = sandbox.path / "enter.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_command_wrappers(sandbox: Sandbox) -> None:
    blocked = """#!/bin/sh
echo "Direct Slurm commands are not available in the sandbox." >&2
echo "Use: lumi-job submit jobs/<script.sh>" >&2
exit 2
"""
    for name in ("sbatch", "srun", "salloc"):
        _write_wrapper(sandbox, name, blocked)
    _write_wrapper(sandbox, "lumi-job", LUMI_JOB)


def _shell_args(args: list[str]) -> str:
    """Render flag/value pairs one per line for a generated shell script."""
    pairs = [f"{args[i]} {shlex.quote(args[i + 1])}" for i in range(0, len(args), 2)]
    return " \\\n  ".join(pairs)


def _write_wrapper(sandbox: Sandbox, name: str, script: str) -> None:
    path = sandbox.path / "wrappers" / name
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


LUMI_JOB = r"""#!/bin/sh
# Ask the host-side broker to act on a Slurm job. No credentials live here:
# this writes a request into /requests and waits for the broker's result.
set -eu

REQUESTS=/requests
TIMEOUT=120

usage() {
  echo "usage: lumi-job submit <script> [--partition P] [--time T] [--nodes N] [--gpus N]" >&2
  echo "       lumi-job status <job-id>" >&2
  echo "       lumi-job cancel <job-id>" >&2
  exit 2
}

safe() {
  case "$2" in
    "" | *[!A-Za-z0-9._/:-]*) echo "error: unsafe value for $1: $2" >&2; exit 2 ;;
  esac
}

[ $# -ge 2 ] || usage
OPERATION=$1
shift

PARTITION=""
TIME=""
NODES=""
GPUS=""
SCRIPT=""
JOB_ID=""

case $OPERATION in
  submit)
    SCRIPT=$1
    shift
    safe script "$SCRIPT"
    while [ $# -gt 0 ]; do
      [ $# -ge 2 ] || usage
      case $1 in
        --partition) PARTITION=$2 ;;
        --time) TIME=$2 ;;
        --nodes) NODES=$2 ;;
        --gpus) GPUS=$2 ;;
        *) usage ;;
      esac
      safe "$1" "$2"
      shift 2
    done
    ;;
  status | cancel)
    JOB_ID=$1
    shift
    [ $# -eq 0 ] || usage
    safe job_id "$JOB_ID"
    ;;
  *)
    usage
    ;;
esac

if [ ! -d "$REQUESTS" ]; then
  echo "error: no request channel at $REQUESTS" >&2
  exit 1
fi

ID="$(date -u +%Y%m%dT%H%M%S)-$$"
REQUEST="$REQUESTS/$ID.request.yaml"
RESULT="$REQUESTS/$ID.result.yaml"

{
  echo "operation: \"$OPERATION\""
  [ -z "$SCRIPT" ] || echo "script: \"$SCRIPT\""
  [ -z "$JOB_ID" ] || echo "job_id: \"$JOB_ID\""
  [ -z "$PARTITION" ] || echo "partition: \"$PARTITION\""
  [ -z "$TIME" ] || echo "time: \"$TIME\""
  [ -z "$NODES" ] || echo "nodes: \"$NODES\""
  [ -z "$GPUS" ] || echo "gpus: \"$GPUS\""
} > "$REQUEST.tmp"
mv "$REQUEST.tmp" "$REQUEST"

elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
  if [ -f "$RESULT" ]; then
    cat "$RESULT"
    if grep -qE '^status: (rejected|error)' "$RESULT"; then
      exit 1
    fi
    exit 0
  fi
  sleep 1
  elapsed=$((elapsed + 1))
done

echo "error: no response from the broker after ${TIMEOUT}s; is it running?" >&2
exit 1
"""
