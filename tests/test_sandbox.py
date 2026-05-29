import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lumi_agent_sandbox.cli import main
from lumi_agent_sandbox.sandbox import create_sandbox, destroy_sandbox, read_policy, resolve_account, resolve_agent_image, task_id
from lumi_agent_sandbox.slurm import PolicyError, parse_slurm_time, submit_job


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
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")

            self.assertTrue((sandbox.path / "work").is_dir())
            self.assertTrue((sandbox.path / "input").is_dir())
            self.assertTrue((sandbox.path / "enter.sh").exists())
            self.assertTrue((sandbox.path / "wrappers" / "sbatch").exists())
            self.assertTrue((sandbox.path / "wrappers" / "srun").exists())

            policy = read_policy(sandbox.path / "policy.yaml")
            self.assertEqual(policy["account"], "project_123")
            self.assertEqual(policy["agent_image"], "/agent.sif")

            enter = (sandbox.path / "enter.sh").read_text(encoding="utf-8")
            self.assertIn('--bind "$SANDBOX/input:/input:ro"', enter)
            self.assertIn("singularity run", enter)

    def test_submit_enforces_policy_and_jobs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            ok = sandbox.path / "jobs" / "ok.sh"
            ok.write_text("#!/bin/sh\n#SBATCH --partition=dev-g\nhostname\n", encoding="utf-8")

            command = submit_job(sandbox, ok, dry_run=True)

            self.assertIn("--account=project_123", command)
            self.assertIn(f"--output={sandbox.path}/logs/%x-%j.out", command)

            too_long = sandbox.path / "jobs" / "too-long.sh"
            too_long.write_text("#!/bin/sh\n#SBATCH --partition=dev-g\n#SBATCH --time=0-01:00\nhostname\n", encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "requested time"):
                submit_job(sandbox, too_long, dry_run=True)

            outside = sandbox.path / "jobs" / "outside.sh"
            outside.write_text("#!/bin/sh\npython /pfs/lustrep4/scratch/project_123/real-repo/train.py\n", encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "outside sandbox"):
                submit_job(sandbox, outside, dry_run=True)

            bad_location = sandbox.path / "work" / "bad.sh"
            bad_location.write_text("#!/bin/sh\nhostname\n", encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "job script must be inside"):
                submit_job(sandbox, bad_location, dry_run=True)

    def test_submit_runs_sbatch_from_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "ok.sh"
            script.write_text("#!/bin/sh\n#SBATCH --partition=dev-g\nhostname\n", encoding="utf-8")

            with mock.patch("subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "Submitted batch job 123\n"
                run.return_value.stderr = ""

                submit_job(sandbox, script)

            self.assertEqual(run.call_args.kwargs["cwd"], sandbox.path / "work")

    def test_slurm_day_prefixed_time_parsing(self) -> None:
        self.assertEqual(parse_slurm_time("0-01:00"), 3600)
        self.assertEqual(parse_slurm_time("0-00:45"), 2700)
        self.assertEqual(parse_slurm_time("1-00"), 86400)

    def test_destroy_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")

            with self.assertRaisesRegex(ValueError, "--yes"):
                destroy_sandbox(sandbox, yes=False)

            destroy_sandbox(sandbox, yes=True)
            self.assertFalse(sandbox.path.exists())

    def test_cli_submit_resolves_relative_script_inside_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "ok.sh"
            script.write_text("#!/bin/sh\n#SBATCH --partition=dev-g\nhostname\n", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--root", tmp, "--account", "project_123", "submit", "demo", "jobs/ok.sh", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn(f"{sandbox.path}/jobs/ok.sh", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
