from __future__ import annotations

import json
import os
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import firecrest, slurm
from .policy import PolicyError, effective_limits, job_defaults
from .sandbox import Sandbox, sandbox_policy
from .slurm import (
    job_wrapper,
    merge_options,
    node_hours,
    parse_sbatch_directives,
    reject_outside_paths,
    validate_options,
)


REQUEST_SUFFIX = ".request.yaml"
RESULT_SUFFIX = ".result.yaml"
REQUEST_FIELDS = frozenset({"operation", "script", "partition", "time", "nodes", "gpus", "job_id"})
OPERATIONS = ("submit", "status", "cancel")
BACKENDS = {"slurm": slurm, "firecrest": firecrest}
POLL_SECONDS = 1.0


def backend_for(site: dict[str, object]):
    """Pick the execution backend.

    A dict of modules rather than a class hierarchy: there are two
    implementations and one dispatch point, so an interface would be a layer
    with nothing to hold. Extract one if a third backend ever appears.
    """
    name = str(site.get("backend", "slurm"))
    if name not in BACKENDS:
        raise PolicyError(f"unknown backend {name!r}, expected one of {', '.join(sorted(BACKENDS))}")
    return BACKENDS[name]


def serve(sandbox: Sandbox, site: dict[str, object]) -> None:
    """Poll the request channel until interrupted."""
    signal.signal(signal.SIGTERM, _raise_interrupt)
    requests = sandbox.path / "requests"
    _log(sandbox, f"broker watching {requests} via {site.get('backend', 'slurm')}")
    try:
        while True:
            for request in sorted(requests.glob(f"*{REQUEST_SUFFIX}")):
                result = request.with_name(request.name[: -len(REQUEST_SUFFIX)] + RESULT_SUFFIX)
                if not result.exists():
                    _handle(sandbox, site, request, result)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        _log(sandbox, "broker stopped")


def submit(
    sandbox: Sandbox,
    site: dict[str, object],
    request: dict[str, str],
    request_id: str | None = None,
    dry_run: bool = False,
) -> str:
    """Validate a submission request and hand it to the backend.

    Shared by the broker loop and the host-side `submit` command so both enforce
    exactly the same limits.
    """
    request_id = request_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.getpid()}"
    policy = sandbox_policy(sandbox)
    limits = effective_limits(site, policy)
    defaults = job_defaults(site, policy)
    contained = str(site.get("job_execution", "container")) == "container"

    # Submit a copy the agent cannot reach, so the script cannot change between
    # validation and the backend reading it.
    staged = _stage(sandbox, request_id, resolve_script(sandbox, request["script"]))
    text = staged.read_text(encoding="utf-8")

    directives = parse_sbatch_directives(text)
    options = merge_options(defaults, directives, request)
    validate_options(limits, options, directives, sandbox.account)
    if not contained:
        reject_outside_paths(sandbox, text)
    _check_budget(sandbox, limits, options)

    wrapper = staged.with_name(f"{request_id}.job.sh")
    wrapper.write_text(job_wrapper(sandbox, staged, options, contained), encoding="utf-8")

    job_id = backend_for(site).submit(site, sandbox, options, wrapper, dry_run)
    if not dry_run:
        _record(sandbox, request_id, options, job_id)
    return job_id


def status(sandbox: Sandbox, site: dict[str, object], job_id: str) -> str:
    _own_job(sandbox, job_id)
    return backend_for(site).status(site, sandbox, job_id)


def cancel(sandbox: Sandbox, site: dict[str, object], job_id: str) -> str:
    _own_job(sandbox, job_id)
    return backend_for(site).cancel(site, sandbox, job_id)


def resolve_script(sandbox: Sandbox, name: str) -> Path:
    """Map a name the agent used onto a real path inside jobs/."""
    jobs = (sandbox.path / "jobs").resolve()
    if not name:
        raise PolicyError("request must name a script")

    candidate = Path(name)
    if name.startswith("/jobs/"):
        candidate = jobs / name[len("/jobs/") :]
    elif not candidate.is_absolute():
        candidate = (sandbox.path if candidate.parts[:1] == ("jobs",) else jobs) / candidate

    script = candidate.resolve()
    if jobs not in script.parents:
        raise PolicyError(f"job script must be inside {jobs}")
    if not script.is_file():
        raise PolicyError(f"job script not found: {name}")
    return script


def read_request(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PolicyError("request must be a mapping")
    unknown = set(data) - REQUEST_FIELDS
    if unknown:
        raise PolicyError(f"unknown request fields: {', '.join(sorted(unknown))}")

    request = {key: str(value) for key, value in data.items()}
    request.setdefault("operation", "submit")
    if request["operation"] not in OPERATIONS:
        raise PolicyError(f"unknown operation {request['operation']!r}, expected one of {', '.join(OPERATIONS)}")
    if request["operation"] == "submit" and "script" not in request:
        raise PolicyError("submit needs a script")
    if request["operation"] != "submit" and "job_id" not in request:
        raise PolicyError(f"{request['operation']} needs a job_id")
    return request


def _handle(sandbox: Sandbox, site: dict[str, object], request: Path, result: Path) -> None:
    request_id = request.name[: -len(REQUEST_SUFFIX)]
    try:
        asked = read_request(request)
        if asked["operation"] == "submit":
            job_id = submit(sandbox, site, asked, request_id)
            answer = {"status": "submitted", "job_id": job_id, "detail": f"submitted as {job_id}"}
        elif asked["operation"] == "status":
            answer = {"status": "ok", "job_id": asked["job_id"], "detail": status(sandbox, site, asked["job_id"])}
        else:
            answer = {"status": "ok", "job_id": asked["job_id"], "detail": cancel(sandbox, site, asked["job_id"])}
    except PolicyError as exc:
        answer = {"status": "rejected", "reason": str(exc)}
    except Exception as exc:  # the broker must outlive any single bad request
        answer = {"status": "error", "reason": str(exc)}

    _write_result(result, answer)
    _log(sandbox, f"{request_id}: {answer['status']} {answer.get('reason', answer.get('detail', ''))}".strip())


def _own_job(sandbox: Sandbox, job_id: str) -> None:
    """Only act on jobs this sandbox submitted.

    Without this the agent could ask the broker to cancel any job belonging to
    the user, including work that has nothing to do with the sandbox.
    """
    if job_id not in {str(entry.get("job_id")) for entry in _history(sandbox)}:
        raise PolicyError(f"job {job_id} was not submitted from this sandbox")


def _stage(sandbox: Sandbox, request_id: str, script: Path) -> Path:
    staged_dir = sandbox.path / "audit" / "submitted"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = staged_dir / f"{request_id}.sh"
    shutil.copyfile(script, staged)
    return staged


def _check_budget(sandbox: Sandbox, limits: dict[str, object], options: dict[str, object]) -> None:
    history = _history(sandbox)
    max_jobs = int(limits["max_jobs_per_session"])  # type: ignore[arg-type]
    if len(history) >= max_jobs:
        raise PolicyError(f"session limit of {max_jobs} jobs reached")

    budget = float(limits["max_node_hours_per_session"])  # type: ignore[arg-type]
    total = sum(float(entry.get("node_hours", 0)) for entry in history) + node_hours(options)
    if total > budget:
        raise PolicyError(f"session budget exceeded: {total:.2f} of {budget} node-hours")


def _history(sandbox: Sandbox) -> list[dict[str, object]]:
    path = sandbox.path / "audit" / "jobs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _record(sandbox: Sandbox, request_id: str, options: dict[str, object], job_id: str) -> None:
    entry = {
        "request_id": request_id,
        "submitted_at": _now(),
        "job_id": job_id,
        "node_hours": node_hours(options),
        **options,
    }
    path = sandbox.path / "audit" / "jobs.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _write_result(path: Path, answer: dict[str, object]) -> None:
    # Rename into place so lumi-job never reads a half-written result.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(answer, sort_keys=False), encoding="utf-8")
    temporary.replace(path)


def _log(sandbox: Sandbox, message: str) -> None:
    path = sandbox.path / "audit" / "broker.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{_now()} {message}\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt
