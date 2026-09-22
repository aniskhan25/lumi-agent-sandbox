from __future__ import annotations

import os
from pathlib import Path

import yaml


CONFIG_FILE = "lumi-agent-sandbox.yaml"
SITE_ENV = "LUMI_AGENT_SANDBOX_SITE"
SITE_PATH = "/appl/local/laifs/lumi-agent-sandbox/site.yaml"

DEFAULT_LIMITS: dict[str, object] = {
    "allowed_partitions": ["dev-g", "debug"],
    "max_nodes": 1,
    "max_gpus_per_node": 1,
    "max_time": "00:30:00",
    "max_jobs_per_session": 10,
    "max_node_hours_per_session": 2,
}

DEFAULT_JOB_OPTIONS: dict[str, object] = {
    "partition": "dev-g",
    "time": "00:15:00",
    "nodes": 1,
    "gpus_per_node": 0,
}


PROFILES = ("standard", "private")


class PolicyError(ValueError):
    pass


def profile(site: dict[str, object]) -> str:
    name = str(site.get("profile", "standard"))
    if name not in PROFILES:
        raise PolicyError(f"unknown profile {name!r}, expected one of {', '.join(PROFILES)}")
    return name


def is_private(site: dict[str, object]) -> bool:
    return profile(site) == "private"


def container_command(site: dict[str, object]) -> str:
    """`singularity` on LUMI, `apptainer` on CSC's own systems."""
    return str(site.get("container_command", "singularity"))


def gpu_flag(site: dict[str, object]) -> str:
    """`--rocm` for LUMI's AMD GPUs, `--nv` for NVIDIA (Roihu is GH200)."""
    return str(site.get("gpu_flag", "--rocm"))


def egress_enforcement_available() -> bool:
    """Whether outbound network traffic can actually be confined.

    Always false for now. Apptainer's --net needs setuid or unprivileged network
    namespaces, which LUMI users do not have, so the harness cannot enforce this
    itself; real enforcement needs an egress gateway or firewall from CSC. This
    is a probe rather than a constant so the claim has one place to become true.
    """
    return False


def require_egress_enforcement(site: dict[str, object]) -> None:
    """Fail closed rather than quietly downgrading the guarantee."""
    if site.get("require_enforced_egress") and not egress_enforcement_available():
        raise PolicyError(
            "require_enforced_egress is set but no egress enforcement backend is available; "
            "refusing to start rather than report a guarantee that does not hold"
        )


def read_yaml(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def find_site_config(explicit: str | None = None) -> Path | None:
    """First readable site config, in order of decreasing trust."""
    for candidate in (explicit, os.environ.get(SITE_ENV), SITE_PATH, CONFIG_FILE):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path
    return None


def site_config(explicit: str | None = None) -> dict[str, object]:
    path = find_site_config(explicit)
    return read_yaml(path) if path else {}


def site_limits(site: dict[str, object]) -> dict[str, object]:
    limits = dict(DEFAULT_LIMITS)
    limits.update(_mapping(site, "limits"))
    limits["max_time"] = slurm_time(limits["max_time"])
    return limits


def slurm_time(value: object) -> str:
    """A Slurm walltime, which YAML requires to be quoted.

    Bare `12:00:00` is sexagesimal in YAML 1.1 and loads as the integer 43200,
    which then reads back as 43200 *minutes* -- a limit 60x looser than written.
    Refuse the unquoted form rather than silently widening the boundary.
    """
    if not isinstance(value, str):
        raise PolicyError(f'walltime {value!r} must be quoted in YAML, e.g. "00:30:00"')
    return value


def effective_limits(site: dict[str, object], sandbox: dict[str, object]) -> dict[str, object]:
    """Site limits, narrowed by anything the sandbox policy tightens.

    A sandbox value that would loosen a site limit is ignored rather than
    rejected: the sandbox policy lives in a directory the agent can write, so
    tampering must fall back to the site limit instead of raising an error the
    agent could then work around.
    """
    limits = site_limits(site)
    requested = sandbox.get("limits")
    if not isinstance(requested, dict):
        return limits
    return {key: _narrow(key, value, requested[key]) if key in requested else value for key, value in limits.items()}


def job_defaults(site: dict[str, object], sandbox: dict[str, object]) -> dict[str, object]:
    defaults = dict(DEFAULT_JOB_OPTIONS)
    defaults.update(_mapping(site, "defaults"))
    defaults.update(_mapping(sandbox, "defaults"))
    defaults["time"] = slurm_time(defaults["time"])
    return defaults


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


def _narrow(key: str, site_value: object, sandbox_value: object) -> object:
    try:
        if key == "allowed_partitions":
            allowed = set(_strings(site_value))
            return [name for name in _strings(sandbox_value) if name in allowed]
        if key == "max_time":
            return min([str(site_value), slurm_time(sandbox_value)], key=parse_slurm_time)
        return sandbox_value if float(sandbox_value) < float(site_value) else site_value  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return site_value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {value!r}")
    return [str(item) for item in value]


def _mapping(config: dict[str, object], key: str) -> dict[str, object]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise PolicyError(f"policy {key!r} must be a mapping")
    return value
