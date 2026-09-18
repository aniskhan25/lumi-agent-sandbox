from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import agentconfig
from .policy import effective_limits, profile
from .sandbox import Sandbox, agent_mount_args, sandbox_policy


MANIFEST = "manifest.json"


def write_manifest(sandbox: Sandbox, site: dict[str, object]) -> Path:
    """Record what constituted this session, without recording the work itself.

    Prompts and source code are deliberately absent: the manifest exists to show
    the execution boundary, not to become a second copy of the project's data.
    """
    limits = effective_limits(site, sandbox_policy(sandbox))
    agent = agentconfig.agent_policy(site)
    provider = agent.get("provider") if isinstance(agent.get("provider"), dict) else {}

    manifest = {
        "sandbox": sandbox.task,
        "created_at": _now(),
        "harness_version": _version(),
        "harness_commit": _commit(),
        "account": sandbox.account,
        "profile": profile(site),
        "backend": str(site.get("backend", "slurm")),
        "job_execution": str(site.get("job_execution", "container")),
        "agent_image": sandbox.agent_image,
        "agent_image_sha256": image_digest(Path(sandbox.agent_image), sandbox.root),
        "effective_limits": limits,
        "policy_sha256": _digest_of(limits),
        "model_endpoint": str(provider.get("base_url", "")) or None,
        "model_provider": str(provider.get("id", "")) or None,
        "denied_tools": agentconfig.denied_tools(site),
        "mcp_servers": sorted(agentconfig.opencode_config(site)["mcp"]),  # type: ignore[arg-type]
        "mounts": agent_mount_args(sandbox) + agentconfig.config_mount(sandbox.path, site),
    }
    path = sandbox.path / "audit" / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def read_manifest(sandbox: Sandbox) -> dict[str, object]:
    path = sandbox.path / "audit" / MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def image_digest(image: Path, cache_root: Path) -> str | None:
    """SHA-256 of the agent image, cached by (path, mtime, size).

    Container images run to several GB and the same one is reused by every
    sandbox, so hashing it once per sandbox would dominate `create`.
    """
    try:
        stat = image.stat()
    except OSError:
        return None

    key = f"{image}:{stat.st_mtime_ns}:{stat.st_size}"
    cache_path = cache_root / ".image-hashes.json"
    cache = _read_cache(cache_path)
    if key in cache:
        return cache[key]

    digest = hashlib.sha256()
    with image.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    cache[key] = digest.hexdigest()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
    return cache[key]


def _read_cache(path: Path) -> dict[str, str]:
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _digest_of(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _version() -> str:
    from . import __version__

    return __version__


def _commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
