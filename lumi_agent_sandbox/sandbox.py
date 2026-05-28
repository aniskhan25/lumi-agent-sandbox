from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_AGENT_IMAGE = "/appl/local/laifs/agents/sif/opencode.sif"
DEFAULT_ACCOUNT = "project_462000131"
TASK_RE = re.compile(r"[^a-zA-Z0-9._-]+")


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


def account_from_env(value: str | None) -> str:
    return value or os.environ.get("LUMI_ACCOUNT") or os.environ.get("PROJECT") or DEFAULT_ACCOUNT


def agent_image_from_env(value: str | None) -> str:
    return value or os.environ.get("LUMI_AGENT_IMAGE") or os.environ.get("LUMI_AGENT_SIF") or DEFAULT_AGENT_IMAGE


def sandbox_root(value: str | None, account: str) -> Path:
    root = value or os.environ.get("LUMI_AGENT_SANDBOX_ROOT")
    if root:
        return Path(root).expanduser()

    user = os.environ.get("USER")
    if not user:
        raise ValueError("provide --root or set USER")
    return Path(f"/scratch/{account}/{user}/agent-sandboxes")


def create_sandbox(name: str, root: Path, account: str, agent_image: str, force: bool = False) -> Sandbox:
    sandbox = Sandbox(task_id(name), root.resolve(), account, agent_image)
    if sandbox.path.exists() and not force:
        raise FileExistsError(f"sandbox already exists: {sandbox.path}")

    for child in ("work", "input", "output", "jobs", "logs", "state/home", "wrappers", "manifests"):
        (sandbox.path / child).mkdir(parents=True, exist_ok=True)

    _write_policy(sandbox)
    _write_enter_script(sandbox)
    _write_command_wrappers(sandbox)
    return sandbox


def list_sandboxes(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "policy.yaml").exists())


def load_sandbox(name: str, root: Path, account: str, agent_image: str | None = None) -> Sandbox:
    task = task_id(name)
    policy_path = root / task / "policy.yaml"
    if not policy_path.exists():
        raise FileNotFoundError(f"sandbox not found: {root / task}")
    policy = read_policy(policy_path)
    return Sandbox(task, root.resolve(), str(policy.get("account") or account), agent_image or str(policy.get("agent_image") or ""))


def enter_sandbox(sandbox: Sandbox) -> None:
    _write_enter_script(sandbox)
    script = sandbox.path / "enter.sh"
    if not script.exists():
        raise FileNotFoundError(f"missing enter script: {script}")
    os.execv("/bin/sh", ["/bin/sh", str(script)])


def diff_sandbox(sandbox: Sandbox) -> str:
    work = sandbox.path / "work"
    if (work / ".git").exists():
        status = subprocess.run(["git", "-C", str(work), "status", "--short"], text=True, capture_output=True, check=False)
        diff = subprocess.run(["git", "-C", str(work), "diff"], text=True, capture_output=True, check=False)
        return f"git status --short\n{status.stdout}\ngit diff\n{diff.stdout}"

    files = sorted(path.relative_to(work) for path in work.rglob("*") if path.is_file())
    if not files:
        return "work/ is empty\n"
    return "files in work/\n" + "\n".join(str(path) for path in files) + "\n"


def archive_sandbox(sandbox: Sandbox) -> Path:
    archive_dir = sandbox.root / "_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{sandbox.task}-{_timestamp()}.tar.gz"

    diff_path = sandbox.path / "manifests" / "diff.patch"
    diff_path.write_text(diff_sandbox(sandbox), encoding="utf-8")

    with tarfile.open(archive_path, "w:gz") as archive:
        for name in ("policy.yaml", "jobs", "logs", "output", "manifests"):
            path = sandbox.path / name
            if path.exists():
                archive.add(path, arcname=f"{sandbox.task}/{name}")
    return archive_path


def destroy_sandbox(sandbox: Sandbox, yes: bool) -> None:
    if not yes:
        raise ValueError("destroy requires --yes")
    root = sandbox.root.resolve()
    target = sandbox.path.resolve()
    if root == target or root not in target.parents:
        raise ValueError(f"refusing to delete path outside sandbox root: {target}")
    shutil.rmtree(target)


def read_policy(path: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    section: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue

        if not line.startswith(" "):
            key, sep, value = line.partition(":")
            if not sep:
                raise ValueError(f"invalid policy line: {raw_line}")
            section = key.strip()
            data[section] = _parse_scalar(value.strip()) if value.strip() else {}
            continue

        if section is None:
            raise ValueError(f"policy entry without section: {raw_line}")

        item = line.strip()
        if item.startswith("- "):
            if not isinstance(data.get(section), list):
                data[section] = []
            data[section].append(_parse_scalar(item[2:].strip()))  # type: ignore[union-attr]
            continue

        key, sep, value = item.partition(":")
        if not sep:
            raise ValueError(f"invalid policy line: {raw_line}")
        if not isinstance(data.get(section), dict):
            data[section] = {}
        data[section][key.strip()] = _parse_scalar(value.strip())  # type: ignore[index]

    return data


def _write_policy(sandbox: Sandbox) -> None:
    policy = f"""account: {sandbox.account}
agent_image: {_yaml_quote(sandbox.agent_image)}

defaults:
  partition: dev-g
  time: "00:15:00"
  nodes: 1
  gpus_per_node: 0

limits:
  max_time: "00:30:00"
  max_nodes: 1
  max_gpus_per_node: 1
  max_array_size: 1

allowed_partitions:
  - small
  - standard
  - dev-g
  - small-g
  - standard-g

allowed_paths:
  - {sandbox.path}
"""
    (sandbox.path / "policy.yaml").write_text(policy, encoding="utf-8")


def _write_enter_script(sandbox: Sandbox) -> None:
    script = f"""#!/bin/sh
set -eu

SANDBOX={_sh_quote(str(sandbox.path))}
AGENT_IMAGE={_sh_quote(sandbox.agent_image)}

if [ -z "$AGENT_IMAGE" ]; then
  echo "No agent image configured. Set LUMI_AGENT_IMAGE or recreate with --agent-image /path/to/agent.sif." >&2
  exit 2
fi

if [ ! -r "$AGENT_IMAGE" ]; then
  echo "Agent image not found or not readable: $AGENT_IMAGE" >&2
  echo "Set LUMI_AGENT_IMAGE or recreate with --agent-image /path/to/agent.sif." >&2
  exit 2
fi

exec env \\
  SINGULARITYENV_HOME=/home/agent \\
  SINGULARITYENV_PREPEND_PATH=/safe-bin \\
  singularity run \\
  --cleanenv \\
  --containall \\
  --no-home \\
  --pwd /workspace \\
  --bind "$SANDBOX/work:/workspace" \\
  --bind "$SANDBOX/input:/input:ro" \\
  --bind "$SANDBOX/output:/output" \\
  --bind "$SANDBOX/jobs:/jobs" \\
  --bind "$SANDBOX/logs:/logs" \\
  --bind "$SANDBOX/state/home:/home/agent" \\
  --bind "$SANDBOX/wrappers:/safe-bin:ro" \\
  "$AGENT_IMAGE"
"""
    path = sandbox.path / "enter.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _write_command_wrappers(sandbox: Sandbox) -> None:
    script = """#!/bin/sh
echo "Use 'lumi-agent-sandbox submit <task> jobs/<script.sh>' from the host shell." >&2
exit 2
"""
    for name in ("sbatch", "srun", "salloc", "safe-sbatch"):
        path = sandbox.path / "wrappers" / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def _parse_scalar(value: str) -> object:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if value.isdecimal():
        return int(value)
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
