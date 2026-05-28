from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from .sandbox import Sandbox, read_policy


class PolicyError(ValueError):
    pass


def submit_job(sandbox: Sandbox, script: Path, dry_run: bool = False) -> str:
    script = script.resolve()
    jobs_dir = (sandbox.path / "jobs").resolve()
    if jobs_dir not in script.parents:
        raise PolicyError(f"job script must be inside {jobs_dir}")
    if not script.exists():
        raise FileNotFoundError(script)

    policy = read_policy(sandbox.path / "policy.yaml")
    text = script.read_text(encoding="utf-8")
    directives = parse_sbatch_directives(text)
    validate_job(sandbox, policy, directives, text)
    submit_options = effective_submit_options(policy, directives)

    logs = sandbox.path / "logs"
    command = [
        "sbatch",
        f"--account={policy['account']}",
        f"--partition={submit_options['partition']}",
        f"--time={submit_options['time']}",
        f"--nodes={submit_options['nodes']}",
        f"--output={logs}/%x-%j.out",
        f"--error={logs}/%x-%j.err",
    ]
    if submit_options["gpus_per_node"] > 0:
        command.append(f"--gpus-per-node={submit_options['gpus_per_node']}")
    command.append(str(script))
    if dry_run:
        return shlex.join(command)

    result = subprocess.run(command, cwd=sandbox.path / "work", text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "sbatch failed")
    return result.stdout.strip()


def parse_sbatch_directives(text: str) -> dict[str, str]:
    options: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#SBATCH"):
            continue
        parts = shlex.split(stripped[len("#SBATCH") :].strip(), comments=True)
        index = 0
        while index < len(parts):
            part = parts[index]
            if "=" in part:
                key, value = part.split("=", 1)
            else:
                key = part
                value = parts[index + 1] if index + 1 < len(parts) and not parts[index + 1].startswith("-") else "true"
                if value != "true":
                    index += 1
            options[_normal_option(key)] = value
            index += 1
    return options


def validate_job(sandbox: Sandbox, policy: dict[str, object], options: dict[str, str], script_text: str) -> None:
    limits = _dict(policy, "limits")
    defaults = _dict(policy, "defaults")
    allowed_partitions = set(_list(policy, "allowed_partitions"))

    account = options.get("account")
    if account and account != str(policy.get("account")):
        raise PolicyError(f"job account {account!r} does not match sandbox account {policy.get('account')!r}")

    partition = options.get("partition") or str(defaults.get("partition", ""))
    if partition not in allowed_partitions:
        raise PolicyError(f"partition {partition!r} is not allowed")

    requested_time = options.get("time") or str(defaults.get("time", "00:15:00"))
    if parse_slurm_time(requested_time) > parse_slurm_time(str(limits.get("max_time", "00:30:00"))):
        raise PolicyError(f"requested time {requested_time} exceeds max_time {limits.get('max_time')}")

    nodes = int(options.get("nodes") or defaults.get("nodes", 1))
    if nodes > int(limits.get("max_nodes", 1)):
        raise PolicyError(f"requested nodes {nodes} exceeds max_nodes {limits.get('max_nodes')}")

    gpus = _requested_gpus(options, defaults)
    if gpus > int(limits.get("max_gpus_per_node", 0)):
        raise PolicyError(f"requested GPUs per node {gpus} exceeds max_gpus_per_node {limits.get('max_gpus_per_node')}")

    array_size = _array_size(options.get("array"))
    if array_size > int(limits.get("max_array_size", 1)):
        raise PolicyError(f"array size {array_size} exceeds max_array_size {limits.get('max_array_size')}")

    _reject_obvious_outside_paths(sandbox, script_text)


def effective_submit_options(policy: dict[str, object], options: dict[str, str]) -> dict[str, object]:
    defaults = _dict(policy, "defaults")
    return {
        "partition": options.get("partition") or str(defaults.get("partition", "")),
        "time": options.get("time") or str(defaults.get("time", "00:15:00")),
        "nodes": int(options.get("nodes") or defaults.get("nodes", 1)),
        "gpus_per_node": _requested_gpus(options, defaults),
    }


def parse_slurm_time(value: str) -> int:
    original = value
    has_days = "-" in value
    days = 0
    if has_days:
        day_text, value = value.split("-", 1)
        days = int(day_text)

    parts = [int(part) for part in value.split(":")]
    if has_days and len(parts) == 1:
        hours, minutes, seconds = parts[0], 0, 0
    elif has_days and len(parts) == 2:
        hours, minutes, seconds = parts[0], parts[1], 0
    elif has_days and len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 1:
        hours, minutes, seconds = 0, parts[0], 0
    elif len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise PolicyError(f"invalid Slurm time: {original!r}")
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _normal_option(key: str) -> str:
    aliases = {
        "-A": "account",
        "--account": "account",
        "-p": "partition",
        "--partition": "partition",
        "-t": "time",
        "--time": "time",
        "-N": "nodes",
        "--nodes": "nodes",
        "--gpus-per-node": "gpus_per_node",
        "--gpus": "gpus_per_node",
        "--gres": "gres",
        "-a": "array",
        "--array": "array",
    }
    return aliases.get(key, key.lstrip("-").replace("-", "_"))


def _requested_gpus(options: dict[str, str], defaults: dict[str, object]) -> int:
    if "gpus_per_node" in options:
        return int(options["gpus_per_node"])
    gres = options.get("gres", "")
    match = re.search(r"gpu(?::[^:,]+)?:(\d+)", gres)
    if match:
        return int(match.group(1))
    return int(defaults.get("gpus_per_node", 0))


def _array_size(value: str | None) -> int:
    if not value:
        return 1
    value = value.split("%", 1)[0]
    total = 0
    for part in value.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            total += int(end) - int(start) + 1
        else:
            total += 1
    return total


def _reject_obvious_outside_paths(sandbox: Sandbox, script_text: str) -> None:
    if re.search(r"(^|[\s=:])(\$HOME|\$\{HOME\}|~)(/|\s|$)", script_text):
        raise PolicyError("job script must not reference the user's home directory")

    allowed = sandbox.path.resolve()
    for match in re.finditer(r"(?<![\w.-])(/(?:scratch|pfs|project|users|home)(?:/[^\s'\";]*)?)", script_text):
        path = Path(match.group(1))
        try:
            path.resolve().relative_to(allowed)
        except ValueError as exc:
            raise PolicyError(f"job script references path outside sandbox: {path}") from exc

    risky = ("rm -rf /", "chmod -R 777 /")
    for text in risky:
        if text in script_text:
            raise PolicyError(f"job script contains risky command: {text}")


def _dict(policy: dict[str, object], key: str) -> dict[str, object]:
    value = policy.get(key, {})
    if not isinstance(value, dict):
        raise PolicyError(f"policy {key!r} must be a mapping")
    return value


def _list(policy: dict[str, object], key: str) -> list[str]:
    value = policy.get(key, [])
    if not isinstance(value, list):
        raise PolicyError(f"policy {key!r} must be a list")
    return [str(item) for item in value]
