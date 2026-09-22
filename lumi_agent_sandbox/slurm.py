from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from .policy import PolicyError, container_command, gpu_flag, parse_slurm_time
from .sandbox import Sandbox, mount_args


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


def merge_options(
    defaults: dict[str, object],
    directives: dict[str, str],
    request: dict[str, str] | None = None,
) -> dict[str, object]:
    """Explicit request flags beat #SBATCH directives, which beat policy defaults."""
    request = request or {}
    gpus = request.get("gpus")
    return {
        "partition": request.get("partition") or directives.get("partition") or str(defaults["partition"]),
        "time": request.get("time") or directives.get("time") or str(defaults["time"]),
        "nodes": int(request.get("nodes") or directives.get("nodes") or defaults["nodes"]),  # type: ignore[arg-type]
        "gpus_per_node": int(gpus) if gpus else _requested_gpus(directives, defaults),
    }


def validate_options(
    limits: dict[str, object],
    options: dict[str, object],
    directives: dict[str, str],
    account: str,
) -> None:
    requested_account = directives.get("account")
    if requested_account and requested_account != account:
        raise PolicyError(f"job account {requested_account!r} does not match sandbox account {account!r}")

    allowed = set(str(name) for name in limits["allowed_partitions"])  # type: ignore[union-attr]
    if options["partition"] not in allowed:
        raise PolicyError(f"partition {options['partition']!r} is not allowed")

    if parse_slurm_time(str(options["time"])) > parse_slurm_time(str(limits["max_time"])):
        raise PolicyError(f"requested time {options['time']} exceeds max_time {limits['max_time']}")

    if int(options["nodes"]) > int(limits["max_nodes"]):  # type: ignore[arg-type]
        raise PolicyError(f"requested nodes {options['nodes']} exceeds max_nodes {limits['max_nodes']}")

    if int(options["gpus_per_node"]) > int(limits["max_gpus_per_node"]):  # type: ignore[arg-type]
        raise PolicyError(
            f"requested GPUs per node {options['gpus_per_node']} exceeds "
            f"max_gpus_per_node {limits['max_gpus_per_node']}"
        )

    if "array" in directives:
        raise PolicyError("job arrays are not allowed")


def node_hours(options: dict[str, object]) -> float:
    return int(options["nodes"]) * parse_slurm_time(str(options["time"])) / 3600  # type: ignore[arg-type]


def job_wrapper(
    sandbox: Sandbox,
    staged: Path,
    options: dict[str, object],
    contained: bool,
    site: dict[str, object] | None = None,
) -> str:
    """The script actually submitted.

    Under `contained` the payload runs inside the agent image with the same
    mounts the agent had, so $HOME, other projects and the wider /scratch are
    structurally unreachable rather than filtered out of the script text. The
    container sees no SLURM_* variables, which is the cost of that isolation.

    The validated directives are written into the script rather than passed as
    submission flags, because FirecREST's job model has no fields for walltime,
    nodes or GPUs -- there the script is the only place limits can be stated.
    """
    site = site or {}
    directives = sbatch_directives(sandbox, options)
    if contained:
        args = [container_command(site), "exec", "--cleanenv", "--containall", "--pwd", "/workspace"]
        if int(options["gpus_per_node"]) > 0:  # type: ignore[arg-type]
            args.append(gpu_flag(site))
        args += mount_args(sandbox)
        args += ["--bind", f"{staged}:/staged/job.sh:ro", sandbox.agent_image, "/bin/sh", "/staged/job.sh"]
        body = f"exec srun {shlex.join(args)}"
    else:
        body = _strip_directives(staged.read_text(encoding="utf-8"))
    return f"#!/bin/bash\n{directives}\nset -eu\n{body}\n"


def sbatch_directives(sandbox: Sandbox, options: dict[str, object]) -> str:
    logs = sandbox.path / "logs"
    lines = [
        f"#SBATCH --account={sandbox.account}",
        f"#SBATCH --job-name={sandbox.task}",
        f"#SBATCH --partition={options['partition']}",
        f"#SBATCH --time={options['time']}",
        f"#SBATCH --nodes={options['nodes']}",
        f"#SBATCH --output={logs}/%x-%j.out",
        f"#SBATCH --error={logs}/%x-%j.err",
    ]
    if int(options["gpus_per_node"]) > 0:  # type: ignore[arg-type]
        lines.append(f"#SBATCH --gpus-per-node={options['gpus_per_node']}")
    return "\n".join(lines)


def _strip_directives(text: str) -> str:
    """Drop the agent's own #SBATCH lines and shebang.

    They were validated as *input* to work out the effective options; letting
    them through as well would hand the scheduler a second, unchecked set.
    """
    lines = [line for line in text.splitlines() if not line.strip().startswith("#SBATCH")]
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def submit(
    site: dict[str, object],
    sandbox: Sandbox,
    options: dict[str, object],
    script: Path,
    dry_run: bool = False,
) -> str:
    """Backend entry point. Mirrored by firecrest.submit; see broker.backend_for."""
    command = sbatch_command(sandbox, sandbox.account, options, script)
    if dry_run:
        return shlex.join(command)
    return job_id_from(run_sbatch(command, cwd=sandbox.path / "work"))


def job_id_from(output: str) -> str:
    """sbatch prints 'Submitted batch job 12345'."""
    return output.split()[-1] if output else ""


def status(site: dict[str, object], sandbox: Sandbox, job_id: str) -> str:
    queued = _run(["squeue", "-h", "-j", job_id, "-o", "%T"])
    if queued:
        return queued
    # squeue forgets a job once it leaves the queue; sacct still remembers.
    finished = _run(["sacct", "-n", "-X", "-j", job_id, "-o", "State"])
    return finished.split()[0] if finished else "UNKNOWN"


def cancel(site: dict[str, object], sandbox: Sandbox, job_id: str) -> str:
    result = subprocess.run(["scancel", job_id], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "scancel failed")
    return "CANCELLED"


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def sbatch_command(sandbox: Sandbox, account: str, options: dict[str, object], script: Path) -> list[str]:
    logs = sandbox.path / "logs"
    command = [
        "sbatch",
        f"--account={account}",
        f"--partition={options['partition']}",
        f"--time={options['time']}",
        f"--nodes={options['nodes']}",
        f"--output={logs}/%x-%j.out",
        f"--error={logs}/%x-%j.err",
    ]
    if int(options["gpus_per_node"]) > 0:  # type: ignore[arg-type]
        command.append(f"--gpus-per-node={options['gpus_per_node']}")
    command.append(str(script))
    return command


def run_sbatch(command: list[str], cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "sbatch failed")
    return result.stdout.strip()


def reject_outside_paths(sandbox: Sandbox, script_text: str) -> None:
    """Advisory text scan, used only when jobs run uncontained on the host.

    This cannot be sound -- /tmp, $SCRATCH or any indirection walks past it.
    Prefer contained execution, where the boundary is a real one.
    """
    if re.search(r"(^|[\s=:])(\$HOME|\$\{HOME\}|~)(/|\s|$)", script_text):
        raise PolicyError("job script must not reference the user's home directory")

    allowed = sandbox.path.resolve()
    for match in re.finditer(r"(?<![\w.-])(/(?:scratch|pfs|project|users|home)(?:/[^\s'\";]*)?)", script_text):
        path = Path(match.group(1))
        try:
            path.resolve().relative_to(allowed)
        except ValueError as exc:
            raise PolicyError(f"job script references path outside sandbox: {path}") from exc

    for risky in ("rm -rf /", "chmod -R 777 /"):
        if risky in script_text:
            raise PolicyError(f"job script contains risky command: {risky}")


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
    return int(defaults.get("gpus_per_node", 0))  # type: ignore[arg-type]
