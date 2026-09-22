"""What the sandbox declares (`inspect`) and what it actually does (`verify`).

These commands exist so the assurance level is legible. They are worthless if
they flatter the system, so both under-claim: `inspect` prints an explicit list
of what is *not* enforced, and `verify` reports an observation it cannot turn
into a guarantee as an observation rather than a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import agentconfig, audit, broker, firecrest
from .policy import (
    PolicyError,
    effective_limits,
    container_command,
    egress_enforcement_available,
    profile,
)
from .sandbox import Sandbox, agent_mount_args, sandbox_policy
from .slurm import job_wrapper, merge_options


PROBE = r"""
[ -e "%(home)s" ] && echo host_home=reachable || echo host_home=unreachable
[ -e "%(home)s/.ssh" ] && echo host_ssh=reachable || echo host_ssh=unreachable
touch /input/.verify 2>/dev/null && echo input_write=allowed || echo input_write=denied
rm -f /input/.verify 2>/dev/null
touch /workspace/.verify 2>/dev/null && echo work_write=allowed || echo work_write=denied
rm -f /workspace/.verify 2>/dev/null
command -v lumi-job >/dev/null 2>&1 && echo lumi_job=present || echo lumi_job=absent
sbatch --version >/dev/null 2>&1 && echo sbatch=works || echo sbatch=blocked
env | grep -Eqi '(TOKEN|API_KEY|SECRET|PASSWORD)=' && echo secrets=present || echo secrets=absent
if command -v curl >/dev/null 2>&1; then
  curl -s -m 5 -o /dev/null https://example.com 2>/dev/null && echo egress=reachable || echo egress=blocked
else
  echo egress=untested
fi
"""

LOCKDOWN_PROBE = r"""
[ -r /etc/opencode/opencode.json ] && echo managed_config=present || echo managed_config=absent
touch /etc/opencode/opencode.json 2>/dev/null && echo managed_writable=yes || echo managed_writable=no
[ -n "${OPENCODE_DISABLE_PROJECT_CONFIG:-}" ] && echo project_config=disabled || echo project_config=enabled
[ -n "${OPENCODE_PERMISSION:-}" ] && echo permission_forced=yes || echo permission_forced=no
"""

CONTAINER_EXPECTATIONS = {
    "host_home": "unreachable",
    "host_ssh": "unreachable",
    "input_write": "denied",
    "work_write": "allowed",
    "lumi_job": "present",
    "sbatch": "blocked",
    "secrets": "absent",
}

LOCKDOWN_EXPECTATIONS = {
    "managed_config": "present",
    "managed_writable": "no",
    "project_config": "disabled",
    "permission_forced": "yes",
}


def inspect(sandbox: Sandbox, site: dict[str, object], site_path: Path | None = None) -> str:
    limits = effective_limits(site, sandbox_policy(sandbox))
    manifest = audit.read_manifest(sandbox)
    contained = str(site.get("job_execution", "container")) == "container"
    private = profile(site) == "private"
    agent = agentconfig.agent_policy(site)
    provider = agent.get("provider") if isinstance(agent.get("provider"), dict) else {}

    lines = [
        f"Sandbox        {sandbox.task}",
        f"  path         {sandbox.path}",
        f"  created      {manifest.get('created_at', 'unknown')}",
        f"  harness      {manifest.get('harness_version', 'unknown')} ({manifest.get('harness_commit') or 'no commit'})",
        "",
        f"Profile        {profile(site)}",
        f"  egress enforcement requested   {'yes' if site.get('require_enforced_egress') else 'no'}",
        f"  egress enforcement available   {'yes' if egress_enforcement_available() else 'no'}",
        "",
        "Policy",
        f"  source       {site_path or 'built-in defaults'}",
        f"  trust        {_trust(site_path)}",
        f"  partitions   {', '.join(str(p) for p in limits['allowed_partitions']) or '(none)'}",
        f"  max nodes    {limits['max_nodes']}",
        f"  max GPUs     {limits['max_gpus_per_node']} per node",
        f"  max walltime {limits['max_time']}",
        f"  session cap  {limits['max_jobs_per_session']} jobs, {limits['max_node_hours_per_session']} node-hours",
        "",
        "Execution",
        f"  backend      {_backend(site)}",
        f"  job payload  {'runs inside the agent image' if contained else 'runs on the host, unsandboxed'}",
        "  agent holds an HPC credential   no",
        "",
        "Agent",
        f"  image        {sandbox.agent_image}",
        f"  image sha256 {manifest.get('agent_image_sha256') or 'not recorded'}",
        f"  model        {provider.get('id') or 'unrestricted'} via {provider.get('base_url') or 'image default'}",
        f"  denied tools {', '.join(agentconfig.denied_tools(site)) if private else 'none (standard profile)'}",
        f"  MCP servers  {', '.join(sorted(agentconfig.opencode_config(site)['mcp'])) or '(none)' if private else 'image default'}",  # type: ignore[arg-type]
        f"  project config and plugins     {'disabled' if private else 'enabled'}",
        "",
        "Not enforced",
    ]
    lines += [f"  - {warning}" for warning in warnings(site, site_path, contained, private)]
    return "\n".join(lines)


def warnings(site: dict[str, object], site_path: Path | None, contained: bool, private: bool) -> list[str]:
    notes = []
    if not egress_enforcement_available():
        notes.append(
            "Outbound network traffic is not confined. Code the agent writes, and code inside a job, "
            "can open a socket. Only the agent's own web tools are denied."
        )
    if site_path is None or _trust(site_path) != "admin-owned":
        notes.append(
            "The site policy is writable by this user, so limits are a deployment control rather than "
            "an enforced one. Against a user with their own LUMI shell only Slurm account QoS binds."
        )
    if private:
        notes.append(
            "The OpenCode lockdown binds the session `enter` starts. The agent has a shell and could "
            "launch its own OpenCode with a different environment."
        )
        notes.append(
            "OPENCODE_DISABLE_PROJECT_CONFIG and OPENCODE_PERMISSION are undocumented upstream, so an "
            "image upgrade could change their behaviour. `verify` tests them rather than assuming them."
        )
    else:
        notes.append("Standard profile: the agent's model providers, web tools and MCP servers are unrestricted.")
    if not contained:
        notes.append(
            "Job payloads run on the host. The only boundary is an advisory text scan, which any "
            "indirection defeats."
        )
    return notes


def verify(sandbox: Sandbox, site: dict[str, object]) -> dict[str, object]:
    checks = _host_checks(sandbox, site) + _container_checks(sandbox, site)
    report = {
        "sandbox": sandbox.task,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": profile(site),
        "checks": checks,
        "failed": [check["name"] for check in checks if check["status"] == "fail"],
    }
    path = sandbox.path / "audit" / "verification.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def format_verification(report: dict[str, object]) -> str:
    symbols = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP", "observed": "----"}
    width = max(len(str(check["name"])) for check in report["checks"])  # type: ignore[index]
    lines = [f"{symbols[str(c['status'])]}  {str(c['name']):<{width}}  {c['detail']}" for c in report["checks"]]  # type: ignore[index]
    failed = report["failed"]
    lines.append("")
    lines.append(f"{len(failed)} failed" if failed else "all checks passed")
    return "\n".join(lines)


def _host_checks(sandbox: Sandbox, site: dict[str, object]) -> list[dict[str, object]]:
    checks = []
    limits = effective_limits(site, {"limits": {"max_nodes": 999, "max_time": "99:00:00"}})
    checks.append(
        _check(
            "policy_tampering_ignored",
            limits["max_nodes"] == effective_limits(site, {})["max_nodes"],
            f"a sandbox policy asking for 999 nodes still yields max_nodes {limits['max_nodes']}",
        )
    )

    probe = sandbox.path / "jobs" / ".verify.sh"
    probe.write_text("#!/bin/sh\nhostname\n", encoding="utf-8")
    try:
        checks.append(_rejects("over_limit_rejected", sandbox, site, {"script": "jobs/.verify.sh", "nodes": "99"}))
        checks.append(_rejects("outside_jobs_rejected", sandbox, site, {"script": "../../../etc/passwd"}))
    finally:
        probe.unlink(missing_ok=True)

    unknown = sandbox.path / "audit" / ".verify.request.yaml"
    unknown.write_text('script: "jobs/x.sh"\nsbatch_args: "--uid 0"\n', encoding="utf-8")
    try:
        broker.read_request(unknown)
        checks.append(_check("unknown_fields_rejected", False, "an unknown request field was accepted"))
    except PolicyError as exc:
        checks.append(_check("unknown_fields_rejected", True, str(exc)))
    finally:
        unknown.unlink(missing_ok=True)

    mounts = agent_mount_args(sandbox)
    checks.append(
        _check("audit_not_mounted", not [m for m in mounts if "/audit" in m], "staged scripts are out of the agent's reach")
    )

    if str(site.get("job_execution", "container")) != "container":
        # An uncontained job has no wrapper to inspect. Reporting that as a pass
        # because nothing was found would be the worst answer available.
        checks.append(_check("job_binds_sandbox_only", False, "job_execution: host, payloads run unsandboxed"))
    else:
        staged = sandbox.path / "audit" / ".verify-wrapper.sh"
        staged.write_text("#!/bin/sh\nhostname\n", encoding="utf-8")
        try:
            options = merge_options({"partition": "dev-g", "time": "00:05:00", "nodes": 1, "gpus_per_node": 0}, {})
            wrapper = job_wrapper(sandbox, staged, options, True, site)
            binds = [word for word in wrapper.split() if ":" in word and word.startswith("/")]
            outside = [bind for bind in binds if not bind.startswith(str(sandbox.path))]
            checks.append(_check("job_binds_sandbox_only", not outside, f"{len(binds)} binds, all under the sandbox"))
        finally:
            staged.unlink(missing_ok=True)

    if profile(site) == "private":
        denied = agentconfig.opencode_config(site)["permission"]
        checks.append(
            _check(
                "web_tools_denied",
                all(denied.get(tool) == "deny" for tool in ("webfetch", "websearch")),  # type: ignore[union-attr]
                f"generated config denies {', '.join(sorted(denied))}",  # type: ignore[arg-type]
            )
        )
    else:
        checks.append(_skip("web_tools_denied", "standard profile: the agent's tools are unrestricted"))
    return checks


def _container_checks(sandbox: Sandbox, site: dict[str, object]) -> list[dict[str, object]]:
    private = profile(site) == "private"
    expectations = dict(CONTAINER_EXPECTATIONS)
    if private:
        expectations.update(LOCKDOWN_EXPECTATIONS)

    names = list(expectations) + ["egress"]
    runtime = container_command(site)
    if not shutil.which(runtime):
        return [_skip(name, f"{runtime} not available on this host") for name in names]
    if not Path(sandbox.agent_image).is_file():
        return [_skip(name, f"agent image not readable: {sandbox.agent_image}") for name in names]

    script = PROBE % {"home": Path.home()}
    if private:
        script += LOCKDOWN_PROBE

    command = [runtime, "exec", "--cleanenv", "--containall", "--pwd", "/workspace"]
    command += agent_mount_args(sandbox) + agentconfig.config_mount(sandbox.path, site)
    command += [sandbox.agent_image, "/bin/sh", "-c", script]
    result = subprocess.run(command, text=True, capture_output=True, check=False, env=_probe_env(site))
    observed = dict(line.split("=", 1) for line in result.stdout.split() if "=" in line)

    checks = []
    for name, expected in expectations.items():
        if name not in observed:
            checks.append(_skip(name, result.stderr.strip()[:120] or "probe produced no answer"))
            continue
        checks.append(_check(name, observed[name] == expected, f"{observed[name]} (expected {expected})"))

    egress = observed.get("egress", "untested")
    if egress_enforcement_available():
        checks.append(_check("egress", egress == "blocked", f"outbound HTTPS {egress}"))
    else:
        # Reported, not graded: nothing here can enforce this, so a "pass" would be a lie.
        checks.append({"name": "egress", "status": "observed", "detail": f"outbound HTTPS {egress}; not enforced"})
    return checks


def _probe_env(site: dict[str, object]) -> dict[str, str]:
    """The same SINGULARITYENV_* variables `enter` sets, so the probe sees the real session."""
    environment = dict(os.environ)
    environment.update({f"SINGULARITYENV_{k}": v for k, v in agentconfig.container_env(site).items()})
    return environment


def _rejects(name: str, sandbox: Sandbox, site: dict[str, object], request: dict[str, str]) -> dict[str, object]:
    try:
        broker.submit(sandbox, site, request, request_id=".verify", dry_run=True)
        return _check(name, False, "the request was accepted")
    except PolicyError as exc:
        return _check(name, True, str(exc))


def _check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail}


def _skip(name: str, detail: str) -> dict[str, object]:
    return {"name": name, "status": "skipped", "detail": detail}


def _backend(site: dict[str, object]) -> str:
    name = str(site.get("backend", "slurm"))
    if name != "firecrest":
        return "local sbatch"
    config = site.get("firecrest", {})
    url = config.get("url", firecrest.DEFAULT_URL) if isinstance(config, dict) else firecrest.DEFAULT_URL
    system = config.get("system", firecrest.DEFAULT_SYSTEM) if isinstance(config, dict) else firecrest.DEFAULT_SYSTEM
    return f"FirecREST {url} ({system})"


def _trust(path: Path | None) -> str:
    if path is None:
        return "built-in defaults"
    return "user-writable" if os.access(path, os.W_OK) else "admin-owned"
