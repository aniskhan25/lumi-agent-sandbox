from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


CONFIG_FILE = "lumi-agent-sandbox.yaml"
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


def load_config() -> dict[str, object]:
    path = Path(CONFIG_FILE)
    return read_policy(path) if path.exists() else {}


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


def create_sandbox(name: str, root: Path, account: str, agent_image: str) -> Sandbox:
    sandbox = Sandbox(task_id(name), root.resolve(), account, agent_image)
    if sandbox.path.exists():
        raise FileExistsError(f"sandbox already exists: {sandbox.path}")

    for child in ("work", "input", "output", "jobs", "logs", "state/home", "wrappers"):
        (sandbox.path / child).mkdir(parents=True, exist_ok=True)

    _write_policy(sandbox)
    _write_enter_script(sandbox)
    _write_command_wrappers(sandbox)
    return sandbox


def load_sandbox(name: str, root: Path) -> Sandbox:
    task = task_id(name)
    policy_path = root / task / "policy.yaml"
    if not policy_path.exists():
        raise FileNotFoundError(f"sandbox not found: {root / task}")
    policy = read_policy(policy_path)
    return Sandbox(task, root.resolve(), str(policy["account"]), str(policy["agent_image"]))


def enter_sandbox(sandbox: Sandbox) -> None:
    script = _write_enter_script(sandbox)
    os.execv("/bin/sh", ["/bin/sh", str(script)])


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

allowed_partitions:
  - dev-g
  - debug
"""
    (sandbox.path / "policy.yaml").write_text(policy, encoding="utf-8")


def _write_enter_script(sandbox: Sandbox) -> Path:
    script = f"""#!/bin/sh
set -eu

SANDBOX={_sh_quote(str(sandbox.path))}
AGENT_IMAGE={_sh_quote(sandbox.agent_image)}

if [ -z "$AGENT_IMAGE" ]; then
  echo "No agent image configured. Add agent_image to lumi-agent-sandbox.yaml or recreate with --agent-image /path/to/agent.sif." >&2
  exit 2
fi

if [ ! -r "$AGENT_IMAGE" ]; then
  echo "Agent image not found or not readable: $AGENT_IMAGE" >&2
  echo "Add agent_image to lumi-agent-sandbox.yaml or recreate with --agent-image /path/to/agent.sif." >&2
  exit 2
fi

exec env \\
  SINGULARITYENV_PREPEND_PATH=/safe-bin \\
  singularity run \\
  --cleanenv \\
  --containall \\
  --home "$SANDBOX/state/home:/home/agent" \\
  --pwd /workspace \\
  --bind "$SANDBOX/work:/workspace" \\
  --bind "$SANDBOX/input:/input:ro" \\
  --bind "$SANDBOX/output:/output" \\
  --bind "$SANDBOX/jobs:/jobs" \\
  --bind "$SANDBOX/logs:/logs" \\
  --bind "$SANDBOX/wrappers:/safe-bin:ro" \\
  "$AGENT_IMAGE"
"""
    path = sandbox.path / "enter.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_command_wrappers(sandbox: Sandbox) -> None:
    script = """#!/bin/sh
echo "Use 'lumi-agent-sandbox submit <task> jobs/<script.sh>' from the host shell." >&2
exit 2
"""
    for name in ("sbatch", "srun", "salloc"):
        path = sandbox.path / "wrappers" / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)


def _parse_scalar(value: str) -> object:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if value.isdecimal():
        return int(value)
    return value


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
