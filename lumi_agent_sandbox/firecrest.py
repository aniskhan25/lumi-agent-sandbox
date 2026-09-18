"""FirecREST execution backend.

Mirrors the slurm module's submit/status/cancel so broker.backend_for can pick
between them. Selected with `backend: firecrest` in the site policy.

Targets FirecREST v2 (server 2.6.0) as deployed by CSC at
https://api.lumi.csc.fi/v1 -- note the /v1 there is CSC's API generation, not
FirecREST v1, which is not deployed. Job submission is synchronous: the POST
returns the real Slurm job id, with none of the v1 task-polling indirection.

Written against urllib rather than pyfirecrest deliberately. The surface needed
here is four calls, while pyfirecrest would take this package from one
dependency to roughly fifteen. Revisit that if bulk data staging through S3 is
ever needed, which is the genuinely fiddly part of the API.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .policy import PolicyError
from .sandbox import Sandbox


DEFAULT_URL = "https://api.lumi.csc.fi/v1"
DEFAULT_SYSTEM = "lumi"
DEFAULT_TOKEN_URL = "https://user-auth.csc.fi/idp/profile/oidc/token"
CLIENT_ID_ENV = "FIRECREST_CLIENT_ID"
CLIENT_SECRET_ENV = "FIRECREST_CLIENT_SECRET"

HTTP_TIMEOUT = 30
TOKEN_MARGIN = 30
NOT_FOUND_RETRIES = 3
NOT_FOUND_WAIT = 2.0

# Tokens live in the broker process only, never on disk and never inside the
# sandbox. Keyed by (token endpoint, client) so a credential change takes effect.
_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}


class FirecrestError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"FirecREST {status}: {message}")
        self.status = status
        self.message = message


def submit(
    site: dict[str, object],
    sandbox: Sandbox,
    options: dict[str, object],
    script: Path,
    dry_run: bool = False,
) -> str:
    config = _config(site)
    url = f"{config['url']}/compute/{config['system']}/jobs"
    job = {
        "name": sandbox.task,
        "account": sandbox.account,
        "partition": str(options["partition"]),
        "workingDirectory": str(sandbox.path / "work"),
        # Everything else -- walltime, nodes, GPUs, log paths -- rides in the
        # wrapper's #SBATCH block. FirecREST's job model has no fields for them,
        # so the validated directives must be in the script itself.
        "script": script.read_text(encoding="utf-8"),
    }
    if dry_run:
        return f"POST {url} account={job['account']} partition={job['partition']} cwd={job['workingDirectory']}"

    payload = _call(config, "POST", url, body={"job": job})
    job_id = str(payload.get("jobId") or "")
    if not job_id:
        raise RuntimeError(f"FirecREST accepted the job but returned no jobId: {payload}")
    return job_id


def status(site: dict[str, object], sandbox: Sandbox, job_id: str) -> str:
    config = _config(site)
    url = f"{config['url']}/compute/{config['system']}/jobs/{job_id}"

    for attempt in range(NOT_FOUND_RETRIES):
        try:
            payload = _call(config, "GET", url)
            break
        except FirecrestError as exc:
            # A just-submitted job is not in the scheduler database yet. We only
            # ask about jobs this sandbox submitted, so retry briefly.
            if exc.status != 404:
                raise
            if attempt == NOT_FOUND_RETRIES - 1:
                return "UNKNOWN"
            time.sleep(NOT_FOUND_WAIT)

    jobs = payload.get("jobs") or []
    if not jobs:
        return "UNKNOWN"
    return _state(jobs[0].get("status") or {})


def cancel(site: dict[str, object], sandbox: Sandbox, job_id: str) -> str:
    config = _config(site)
    _call(config, "DELETE", f"{config['url']}/compute/{config['system']}/jobs/{job_id}")
    return "CANCELLED"


def _state(status_block: dict[str, object]) -> str:
    """Slurm's state, which newer versions report as a list rather than a string."""
    state = status_block.get("state", "UNKNOWN")
    return ",".join(str(part) for part in state) if isinstance(state, list) else str(state)


def _config(site: dict[str, object]) -> dict[str, str]:
    config = site.get("firecrest", {})
    if not isinstance(config, dict):
        raise PolicyError("site 'firecrest' must be a mapping")
    return {
        "url": str(config.get("url", DEFAULT_URL)).rstrip("/"),
        "system": str(config.get("system", DEFAULT_SYSTEM)),
        "token_url": str(config.get("token_url", DEFAULT_TOKEN_URL)),
    }


def _call(config: dict[str, str], method: str, url: str, body: dict[str, object] | None = None) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {_token(config)}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    return _request(url, data=data, headers=headers, method=method)


def _token(config: dict[str, str]) -> str:
    client_id = os.environ.get(CLIENT_ID_ENV, "")
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "")
    if not client_id or not client_secret:
        raise PolicyError(
            f"set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} in the broker's environment; "
            "the agent container never sees them"
        )

    key = (config["token_url"], client_id)
    cached = _TOKENS.get(key)
    if cached and cached[1] > time.time():
        return cached[0]

    form = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "openid",
        }
    ).encode()
    payload = _request(
        config["token_url"],
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("the token endpoint returned no access_token")

    _TOKENS[key] = (token, time.time() + float(payload.get("expires_in", 300)) - TOKEN_MARGIN)
    return token


def _request(
    url: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> dict[str, object]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise _error(exc) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"FirecREST unreachable: {exc.reason}") from None


def _error(exc: urllib.error.HTTPError) -> FirecrestError:
    try:
        payload = json.loads(exc.read())
        message = str(payload.get("message") or payload)
    except (ValueError, OSError):
        message = exc.reason or "request failed"
    finally:
        exc.close()
    if exc.status == 429:
        message = f"{message} (retry after {exc.headers.get('Retry-After', '10')}s)"
    return FirecrestError(exc.status or 0, message)
