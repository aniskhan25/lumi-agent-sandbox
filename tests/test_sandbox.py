import contextlib
import io
import json
import os
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lumi_agent_sandbox import agentconfig, audit, broker, firecrest, report
from lumi_agent_sandbox.cli import main
from lumi_agent_sandbox.policy import (
    PolicyError,
    effective_limits,
    parse_slurm_time,
    read_yaml,
    require_egress_enforcement,
)
from lumi_agent_sandbox.sandbox import (
    agent_mount_args,
    create_sandbox,
    destroy_sandbox,
    mount_args,
    resolve_account,
    resolve_agent_image,
    task_id,
)
from lumi_agent_sandbox.slurm import job_wrapper, merge_options

SITE = {
    "account": "project_123",
    "agent_image": "/agent.sif",
    "limits": {
        "allowed_partitions": ["dev-g", "debug"],
        "max_nodes": 1,
        "max_gpus_per_node": 1,
        "max_time": "00:30:00",
        "max_jobs_per_session": 3,
        "max_node_hours_per_session": 1,
    },
}

JOB = "#!/bin/sh\n#SBATCH --partition=dev-g\nhostname\n"

PRIVATE = {
    **SITE,
    "profile": "private",
    "agent": {
        "provider": {"id": "csc-internal", "base_url": "https://inference.csc.fi/v1", "models": ["llama-3.3"]},
        "mcp": {"lumi-docs": {"type": "local", "command": ["lumi-docs-mcp"]}},
    },
}


def sandbox_in(tmp, site=None):
    return create_sandbox("demo", Path(tmp), "project_123", "/agent.sif", site)


def write_job(sandbox, name, text=JOB):
    path = sandbox.path / "jobs" / name
    path.write_text(text, encoding="utf-8")
    return path


class SandboxTests(unittest.TestCase):
    def test_config_resolution_and_task_names_are_safe(self) -> None:
        self.assertEqual(task_id("My Test / Task"), "my-test-task")
        self.assertEqual(resolve_account(None, {"account": "project_123"}), "project_123")
        with self.assertRaisesRegex(ValueError, "account"):
            resolve_account(None, {})

        self.assertEqual(resolve_agent_image(None, {"agent_image": "/config/agent.sif"}), "/config/agent.sif")
        self.assertEqual(resolve_agent_image("/flag/agent.sif", {"agent_image": "/config/agent.sif"}), "/flag/agent.sif")

    def test_create_writes_expected_sandbox_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)

            for child in ("work", "input", "jobs", "requests", "audit"):
                self.assertTrue((sandbox.path / child).is_dir(), child)
            for wrapper in ("sbatch", "srun", "salloc", "lumi-job"):
                self.assertTrue((sandbox.path / "wrappers" / wrapper).exists(), wrapper)

            policy = read_yaml(sandbox.path / "policy.yaml")
            self.assertEqual(policy["account"], "project_123")
            self.assertEqual(policy["agent_image"], "/agent.sif")

            enter = (sandbox.path / "enter.sh").read_text(encoding="utf-8")
            self.assertIn(f"{sandbox.path}/input:/input:ro", enter)
            self.assertIn("singularity run", enter)

    def test_sandbox_policy_may_only_narrow_site_limits(self) -> None:
        loosened = {"limits": {"max_nodes": 8, "max_time": "12:00:00", "allowed_partitions": ["standard-g"]}}
        limits = effective_limits(SITE, loosened)
        self.assertEqual(limits["max_nodes"], 1)
        self.assertEqual(limits["max_time"], "00:30:00")
        self.assertEqual(limits["allowed_partitions"], [])

        tightened = {"limits": {"max_nodes": 1, "max_time": "00:05:00", "allowed_partitions": ["debug"]}}
        limits = effective_limits(SITE, tightened)
        self.assertEqual(limits["max_time"], "00:05:00")
        self.assertEqual(limits["allowed_partitions"], ["debug"])

        # A tampered value the agent could write must fall back, not raise.
        self.assertEqual(effective_limits(SITE, {"limits": "lots"})["max_nodes"], 1)
        self.assertEqual(effective_limits(SITE, {"limits": {"max_nodes": "lots"}})["max_nodes"], 1)

    def test_tampering_with_sandbox_policy_does_not_raise_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            policy = sandbox.path / "policy.yaml"
            policy.write_text(
                policy.read_text(encoding="utf-8") + "limits:\n  max_nodes: 16\n", encoding="utf-8"
            )
            write_job(sandbox, "big.sh", "#!/bin/sh\n#SBATCH --partition=dev-g\n#SBATCH --nodes=4\nhostname\n")

            with self.assertRaisesRegex(PolicyError, "exceeds max_nodes 1"):
                broker.submit(sandbox, SITE, {"script": "jobs/big.sh"}, dry_run=True)

    def test_submit_enforces_policy_and_jobs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            write_job(sandbox, "ok.sh")

            command = broker.submit(sandbox, SITE, {"script": "jobs/ok.sh"}, dry_run=True)
            self.assertIn("--account=project_123", command)
            self.assertIn(f"--output={sandbox.path}/logs/%x-%j.out", command)

            write_job(sandbox, "long.sh", "#!/bin/sh\n#SBATCH --partition=dev-g\n#SBATCH --time=0-01:00\nhostname\n")
            with self.assertRaisesRegex(PolicyError, "requested time"):
                broker.submit(sandbox, SITE, {"script": "jobs/long.sh"}, dry_run=True)

            write_job(sandbox, "array.sh", "#!/bin/sh\n#SBATCH --partition=dev-g\n#SBATCH --array=1-2\nhostname\n")
            with self.assertRaisesRegex(PolicyError, "arrays"):
                broker.submit(sandbox, SITE, {"script": "jobs/array.sh"}, dry_run=True)

            write_job(sandbox, "gres.sh", "#!/bin/sh\n#SBATCH --partition=dev-g\n#SBATCH --gres=gpu:mi250:4\nhostname\n")
            with self.assertRaisesRegex(PolicyError, "GPUs per node 4"):
                broker.submit(sandbox, SITE, {"script": "jobs/gres.sh"}, dry_run=True)

            with self.assertRaisesRegex(PolicyError, "not allowed"):
                broker.submit(sandbox, SITE, {"script": "jobs/ok.sh", "partition": "standard-g"}, dry_run=True)

            (sandbox.path / "work" / "bad.sh").write_text(JOB, encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "must be inside"):
                broker.submit(sandbox, SITE, {"script": "../work/bad.sh"}, dry_run=True)

    def test_request_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "r.request.yaml"
            request.write_text('script: "jobs/ok.sh"\nnodes: "1"\n', encoding="utf-8")
            self.assertEqual(
                broker.read_request(request),
                {"script": "jobs/ok.sh", "nodes": "1", "operation": "submit"},
            )

            request.write_text('script: "jobs/ok.sh"\nsbatch_args: "--uid 0"\n', encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "unknown request fields: sbatch_args"):
                broker.read_request(request)

    def test_submitted_script_is_staged_beyond_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            script = write_job(sandbox, "ok.sh")

            broker.submit(sandbox, SITE, {"script": "jobs/ok.sh"}, request_id="r1", dry_run=True)
            staged = sandbox.path / "audit" / "submitted" / "r1.sh"
            self.assertEqual(staged.read_text(encoding="utf-8"), JOB)

            # The agent swapping the script after validation must not change what ran.
            script.write_text("#!/bin/sh\ncurl evil\n", encoding="utf-8")
            self.assertEqual(staged.read_text(encoding="utf-8"), JOB)
            self.assertFalse([arg for arg in agent_mount_args(sandbox) if "/audit" in arg])

    def test_contained_job_wrapper_binds_only_sandbox_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            staged = sandbox.path / "audit" / "submitted" / "r1.sh"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_text(JOB, encoding="utf-8")
            options = merge_options({"partition": "dev-g", "time": "00:05:00", "nodes": 1, "gpus_per_node": 1}, {})

            wrapper = job_wrapper(sandbox, staged, options, contained=True)

            self.assertIn("--containall", wrapper)
            self.assertIn("--rocm", wrapper)
            self.assertIn(f"{staged}:/staged/job.sh:ro", wrapper)
            # Jobs get neither the request channel nor the wrappers, so a job
            # cannot submit further jobs and escape the session budget.
            self.assertNotIn("/requests", wrapper)
            self.assertNotIn("/safe-bin", wrapper)
            for bind in mount_args(sandbox):
                if bind.startswith("/"):
                    self.assertTrue(bind.startswith(str(sandbox.path)), bind)

            # Uncontained jobs still carry the validated directives, and the
            # agent's own #SBATCH lines are stripped: FirecREST reads limits from
            # the script, so a second unchecked set must never reach it.
            host = job_wrapper(sandbox, staged, options, contained=False)
            self.assertIn("#SBATCH --nodes=1", host)
            self.assertIn("#SBATCH --time=00:05:00", host)
            self.assertNotIn("#SBATCH --partition=dev-g\n#SBATCH", host.split("set -eu")[1] if "set -eu" in host else "")
            self.assertEqual(host.count("#SBATCH --partition"), 1)
            self.assertTrue(host.rstrip().endswith("hostname"))

    def test_session_budget_stops_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            write_job(sandbox, "ok.sh", "#!/bin/sh\n#SBATCH --partition=dev-g\n#SBATCH --time=00:30:00\nhostname\n")

            with mock.patch("subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "Submitted batch job 123\n"
                run.return_value.stderr = ""

                broker.submit(sandbox, SITE, {"script": "jobs/ok.sh"}, request_id="r1")
                broker.submit(sandbox, SITE, {"script": "jobs/ok.sh"}, request_id="r2")
                with self.assertRaisesRegex(PolicyError, "budget exceeded"):
                    broker.submit(sandbox, SITE, {"script": "jobs/ok.sh"}, request_id="r3")

            self.assertEqual(run.call_args.kwargs["cwd"], sandbox.path / "work")
            history = (sandbox.path / "audit" / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(history), 2)

    def test_broker_answers_a_request_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            write_job(sandbox, "ok.sh")
            request = sandbox.path / "requests" / "r1.request.yaml"
            request.write_text('script: "jobs/ok.sh"\n', encoding="utf-8")
            result = sandbox.path / "requests" / "r1.result.yaml"

            with mock.patch("subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "Submitted batch job 4242\n"
                run.return_value.stderr = ""
                broker._handle(sandbox, SITE, request, result)

            self.assertEqual(read_yaml(result), {"status": "submitted", "job_id": "4242", "detail": "submitted as 4242"})

            bad = sandbox.path / "requests" / "r2.request.yaml"
            bad.write_text('script: "jobs/missing.sh"\n', encoding="utf-8")
            bad_result = sandbox.path / "requests" / "r2.result.yaml"
            broker._handle(sandbox, SITE, bad, bad_result)
            self.assertEqual(read_yaml(bad_result)["status"], "rejected")

    def test_slurm_day_prefixed_time_parsing(self) -> None:
        self.assertEqual(parse_slurm_time("0-01:00"), 3600)
        self.assertEqual(parse_slurm_time("0-00:45"), 2700)
        self.assertEqual(parse_slurm_time("1-00"), 86400)

    def test_destroy_requires_confirmation_and_stays_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)

            with self.assertRaisesRegex(ValueError, "--yes"):
                destroy_sandbox(sandbox, yes=False)

            escaped = sandbox.__class__("..", sandbox.root, sandbox.account, sandbox.agent_image)
            with self.assertRaisesRegex(ValueError, "outside sandbox root"):
                destroy_sandbox(escaped, yes=True)

            destroy_sandbox(sandbox, yes=True)
            self.assertFalse(sandbox.path.exists())

    def test_cli_submit_resolves_relative_script_inside_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp)
            write_job(sandbox, "ok.sh")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--root", tmp, "--account", "project_123", "submit", "demo", "jobs/ok.sh", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn(f"{sandbox.path}/audit/submitted/", stdout.getvalue())


FIRECREST_SITE = {**SITE, "backend": "firecrest", "firecrest": {"url": "https://api.lumi.csc.fi/v1", "system": "lumi"}}
TOKEN = {"access_token": "test-token", "expires_in": 3600}


class FakeResponse:
    def __init__(self, payload):
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def fake_http(responses):
    """Serve queued payloads to urllib, recording the requests made."""
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        payload = responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)

    firecrest._TOKENS.clear()
    with mock.patch("urllib.request.urlopen", urlopen):
        with mock.patch.dict(os.environ, {"FIRECREST_CLIENT_ID": "robot", "FIRECREST_CLIENT_SECRET": "shh"}):
            yield calls
    firecrest._TOKENS.clear()


def http_error(status, message):
    return urllib.error.HTTPError(
        "https://api.lumi.csc.fi/v1/x", status, "err", {}, io.BytesIO(json.dumps({"message": message}).encode())
    )


class FirecrestTests(unittest.TestCase):
    def test_submit_posts_the_job_and_returns_the_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, FIRECREST_SITE)
            write_job(sandbox, "ok.sh")

            with fake_http([TOKEN, {"jobId": "6543210"}]) as calls:
                job_id = broker.submit(sandbox, FIRECREST_SITE, {"script": "jobs/ok.sh"}, request_id="r1")

            self.assertEqual(job_id, "6543210")
            token_call, submit_call = calls
            self.assertEqual(token_call.full_url, firecrest.DEFAULT_TOKEN_URL)
            self.assertIn(b"grant_type=client_credentials", token_call.data)
            self.assertEqual(submit_call.full_url, "https://api.lumi.csc.fi/v1/compute/lumi/jobs")
            self.assertEqual(submit_call.headers["Authorization"], "Bearer test-token")

            job = json.loads(submit_call.data)["job"]
            self.assertEqual(job["workingDirectory"], str(sandbox.path / "work"))
            self.assertEqual(job["account"], "project_123")
            # FirecREST has no fields for these, so they must be in the script.
            self.assertIn("#SBATCH --nodes=1", job["script"])
            self.assertIn("#SBATCH --time=00:15:00", job["script"])

    def test_status_unwraps_the_jobs_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, FIRECREST_SITE)
            with fake_http([TOKEN, {"jobs": [{"status": {"state": "RUNNING"}}]}]):
                self.assertEqual(firecrest.status(FIRECREST_SITE, sandbox, "1"), "RUNNING")

            # Newer Slurm reports state as a list.
            with fake_http([TOKEN, {"jobs": [{"status": {"state": ["CANCELLED", "COMPLETED"]}}]}]):
                self.assertEqual(firecrest.status(FIRECREST_SITE, sandbox, "1"), "CANCELLED,COMPLETED")

            with fake_http([TOKEN, {"jobs": []}]):
                self.assertEqual(firecrest.status(FIRECREST_SITE, sandbox, "1"), "UNKNOWN")

    def test_status_tolerates_a_job_not_yet_in_the_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, FIRECREST_SITE)
            responses = [TOKEN, http_error(404, "Job not found"), {"jobs": [{"status": {"state": "PENDING"}}]}]
            with mock.patch("time.sleep"):
                with fake_http(responses):
                    self.assertEqual(firecrest.status(FIRECREST_SITE, sandbox, "1"), "PENDING")

    def test_cancel_and_api_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, FIRECREST_SITE)
            with fake_http([TOKEN, None]):
                self.assertEqual(firecrest.cancel(FIRECREST_SITE, sandbox, "1"), "CANCELLED")

            with fake_http([TOKEN, http_error(403, "not your job")]):
                with self.assertRaisesRegex(RuntimeError, "FirecREST 403: not your job"):
                    firecrest.cancel(FIRECREST_SITE, sandbox, "1")

            with fake_http([TOKEN, http_error(429, "slow down")]):
                with self.assertRaisesRegex(RuntimeError, "retry after"):
                    firecrest.cancel(FIRECREST_SITE, sandbox, "1")

    def test_credentials_stay_in_the_broker_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, FIRECREST_SITE)
            write_job(sandbox, "ok.sh")
            firecrest._TOKENS.clear()

            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(PolicyError, "FIRECREST_CLIENT_ID"):
                    firecrest.status(FIRECREST_SITE, sandbox, "1")

            with fake_http([TOKEN, {"jobId": "7"}, {"jobs": [{"status": {"state": "RUNNING"}}]}]) as calls:
                broker.submit(sandbox, FIRECREST_SITE, {"script": "jobs/ok.sh"}, request_id="r1")
                firecrest.status(FIRECREST_SITE, sandbox, "7")
                # One token call for both requests: it is cached in memory only.
                self.assertEqual(len(calls), 3)

            written = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sandbox.path.rglob("*")
                if path.is_file() and path.suffix in {".sh", ".json", ".yaml", ".jsonl"}
            )
            self.assertNotIn("shh", written)
            self.assertNotIn("test-token", written)

    def test_dry_run_makes_no_http_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, FIRECREST_SITE)
            write_job(sandbox, "ok.sh")
            with fake_http([]) as calls:
                described = broker.submit(sandbox, FIRECREST_SITE, {"script": "jobs/ok.sh"}, dry_run=True)
            self.assertEqual(calls, [])
            self.assertIn("POST https://api.lumi.csc.fi/v1/compute/lumi/jobs", described)


class BackendTests(unittest.TestCase):
    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "unknown backend 'ssh'"):
            broker.backend_for({"backend": "ssh"})
        self.assertIs(broker.backend_for({}), broker.slurm)

    def test_broker_only_acts_on_jobs_it_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, SITE)
            write_job(sandbox, "ok.sh")

            # Cancelling an arbitrary id would reach the user's unrelated work.
            with self.assertRaisesRegex(PolicyError, "not submitted from this sandbox"):
                broker.cancel(sandbox, SITE, "999999")

            with mock.patch("subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "Submitted batch job 4242\n"
                run.return_value.stderr = ""
                broker.submit(sandbox, SITE, {"script": "jobs/ok.sh"}, request_id="r1")

                run.return_value.stdout = "RUNNING\n"
                self.assertEqual(broker.status(sandbox, SITE, "4242"), "RUNNING")

    def test_request_channel_carries_status_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, SITE)
            write_job(sandbox, "ok.sh")
            requests = sandbox.path / "requests"

            with mock.patch("subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "Submitted batch job 4242\n"
                run.return_value.stderr = ""
                (requests / "r1.request.yaml").write_text('script: "jobs/ok.sh"\n', encoding="utf-8")
                broker._handle(sandbox, SITE, requests / "r1.request.yaml", requests / "r1.result.yaml")

                run.return_value.stdout = "PENDING\n"
                (requests / "r2.request.yaml").write_text('operation: "status"\njob_id: "4242"\n', encoding="utf-8")
                broker._handle(sandbox, SITE, requests / "r2.request.yaml", requests / "r2.result.yaml")

            answer = read_yaml(requests / "r2.result.yaml")
            self.assertEqual(answer, {"status": "ok", "job_id": "4242", "detail": "PENDING"})

            (requests / "r3.request.yaml").write_text('operation: "cancel"\n', encoding="utf-8")
            broker._handle(sandbox, SITE, requests / "r3.request.yaml", requests / "r3.result.yaml")
            self.assertIn("needs a job_id", read_yaml(requests / "r3.result.yaml")["reason"])


class CapabilityTests(unittest.TestCase):
    def test_private_profile_locks_opencode_config(self) -> None:
        config = agentconfig.opencode_config(PRIVATE)
        self.assertEqual(config["permission"], {"webfetch": "deny", "websearch": "deny"})
        self.assertEqual(config["enabled_providers"], ["csc-internal"])
        self.assertEqual(sorted(config["mcp"]), ["lumi-docs"])

        env = agentconfig.container_env(PRIVATE)
        # Writing the config file is not enough: project config and the
        # auto-executed .opencode/plugin directory override it.
        self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        self.assertIn('"webfetch": "deny"', env["OPENCODE_PERMISSION"])

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, PRIVATE)
            written = sandbox.path / "agent" / "opencode.json"
            self.assertTrue(written.is_file())
            enter = (sandbox.path / "enter.sh").read_text(encoding="utf-8")
            self.assertIn(f"{written}:/etc/opencode/opencode.json:ro", enter)
            self.assertIn("SINGULARITYENV_OPENCODE_DISABLE_PROJECT_CONFIG=1", enter)

    def test_standard_profile_restricts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, SITE)
            self.assertFalse((sandbox.path / "agent" / "opencode.json").exists())
            self.assertEqual(agentconfig.container_env(SITE), {})
            self.assertNotIn("/etc/opencode", (sandbox.path / "enter.sh").read_text(encoding="utf-8"))

    def test_provider_without_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(PolicyError, "needs a base_url"):
            agentconfig.opencode_config({"profile": "private", "agent": {"provider": {"id": "x"}}})

    def test_enforced_egress_fails_closed(self) -> None:
        require_egress_enforcement({})
        with self.assertRaisesRegex(PolicyError, "no egress enforcement backend"):
            require_egress_enforcement({"require_enforced_egress": True})

    def test_verify_grades_host_checks_and_skips_what_it_cannot_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, PRIVATE)
            result = report.verify(sandbox, PRIVATE)

            by_name = {check["name"]: check for check in result["checks"]}
            for name in ("policy_tampering_ignored", "over_limit_rejected", "outside_jobs_rejected",
                         "unknown_fields_rejected", "audit_not_mounted", "web_tools_denied"):
                self.assertEqual(by_name[name]["status"], "pass", name)

            # No agent image here, so container probes must skip rather than pass.
            self.assertEqual(by_name["host_home"]["status"], "skipped")
            self.assertEqual(by_name["egress"]["status"], "skipped")
            self.assertEqual(result["failed"], [])
            self.assertTrue((sandbox.path / "audit" / "verification.json").is_file())
            # verify must leave no probe files behind
            self.assertEqual(list((sandbox.path / "jobs").iterdir()), [])

    def test_verify_never_passes_an_uncontained_job(self) -> None:
        site = {**SITE, "job_execution": "host"}
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, site)
            by_name = {check["name"]: check for check in report.verify(sandbox, site)["checks"]}

            # There is no wrapper to inspect here; "nothing found" must not read as a pass.
            self.assertEqual(by_name["job_binds_sandbox_only"]["status"], "fail")
            self.assertEqual(by_name["web_tools_denied"]["status"], "skipped")
            notes = " ".join(report.warnings(site, None, contained=False, private=False))
            self.assertIn("Job payloads run on the host", notes)

    def test_inspect_states_what_is_not_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, PRIVATE)
            audit.write_manifest(sandbox, PRIVATE)
            text = report.inspect(sandbox, PRIVATE)

            self.assertIn("Profile        private", text)
            self.assertIn("csc-internal via https://inference.csc.fi/v1", text)
            self.assertIn("Not enforced", text)
            self.assertIn("Outbound network traffic is not confined", text)
            self.assertIn("could launch its own OpenCode", text)
            self.assertIn("undocumented upstream", text)

    def test_manifest_records_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, PRIVATE)
            audit.write_manifest(sandbox, PRIVATE)
            manifest = audit.read_manifest(sandbox)

            self.assertEqual(manifest["profile"], "private")
            self.assertEqual(manifest["model_endpoint"], "https://inference.csc.fi/v1")
            self.assertEqual(manifest["denied_tools"], ["webfetch", "websearch"])
            self.assertEqual(manifest["mcp_servers"], ["lumi-docs"])
            self.assertEqual(manifest["effective_limits"]["max_nodes"], 1)
            self.assertTrue(manifest["policy_sha256"])
            # The image does not exist in tests, so the digest must be absent, not wrong.
            self.assertIsNone(manifest["agent_image_sha256"])
            self.assertNotIn("prompt", json.dumps(manifest))

    def test_destroy_keeps_the_audit_trail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = sandbox_in(tmp, PRIVATE)
            audit.write_manifest(sandbox, PRIVATE)

            kept = destroy_sandbox(sandbox, yes=True)

            self.assertFalse(sandbox.path.exists())
            self.assertTrue((kept / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
