import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lumi_agent_sandbox.cli import main
from lumi_agent_sandbox.sandbox import account_from_env, create_sandbox, destroy_sandbox, read_policy, sandbox_root, task_id
from lumi_agent_sandbox.slurm import PolicyError, parse_sbatch_directives, submit_job


class SandboxTests(unittest.TestCase):
    def test_task_id_is_directory_safe(self) -> None:
        self.assertEqual(task_id("My Test / Task"), "my-test-task")

    def test_default_account_matches_lumi_project(self) -> None:
        self.assertEqual(account_from_env(None), "project_462000131")

    def test_default_root_uses_user_directory(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "LUMI_AGENT_SANDBOX_ROOT"}
        env["USER"] = "anisrahm"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                sandbox_root(None, "project_462000131"),
                Path("/scratch/project_462000131/anisrahm/agent-sandboxes"),
            )

    def test_create_writes_policy_and_enter_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")

            self.assertTrue((sandbox.path / "work").is_dir())
            self.assertTrue((sandbox.path / "input").is_dir())
            self.assertTrue((sandbox.path / "enter.sh").exists())

            policy = read_policy(sandbox.path / "policy.yaml")
            self.assertEqual(policy["account"], "project_123")
            self.assertEqual(policy["agent_image"], "/agent.sif")
            self.assertEqual(policy["allowed_partitions"], ["debug", "dev-g"])

            enter = (sandbox.path / "enter.sh").read_text(encoding="utf-8")
            self.assertIn("--no-home", enter)
            self.assertIn("--cleanenv", enter)
            self.assertIn('--bind "$SANDBOX/input:/input:ro"', enter)
            self.assertTrue((sandbox.path / "wrappers" / "sbatch").exists())
            self.assertTrue((sandbox.path / "wrappers" / "srun").exists())

    def test_submit_dry_run_accepts_small_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "ok.sh"
            script.write_text(
                """#!/bin/sh
#SBATCH --partition=dev-g
#SBATCH --time=00:10:00
#SBATCH --nodes=1
#SBATCH --gpus-per-node=1
srun singularity exec /runtime.sif python train.py
""",
                encoding="utf-8",
            )

            command = submit_job(sandbox, script, dry_run=True)

            self.assertTrue(command.startswith("sbatch --account=project_123"))
            self.assertIn("--partition=dev-g", command)
            self.assertIn("--time=00:10:00", command)
            self.assertIn("--nodes=1", command)
            self.assertIn("--gpus-per-node=1", command)
            self.assertIn(str(script), command)

    def test_submit_rejects_large_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "large.sh"
            script.write_text(
                """#!/bin/sh
#SBATCH --partition=dev-g
#SBATCH --time=02:00:00
#SBATCH --nodes=2
hostname
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PolicyError, "requested time"):
                submit_job(sandbox, script, dry_run=True)

    def test_submit_rejects_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "outside.sh"
            script.write_text(
                """#!/bin/sh
#SBATCH --partition=dev-g
python /scratch/project_123/real-repo/train.py
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PolicyError, "outside sandbox"):
                submit_job(sandbox, script, dry_run=True)

    def test_submit_requires_jobs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "work" / "bad.sh"
            script.write_text("#!/bin/sh\nhostname\n", encoding="utf-8")

            with self.assertRaisesRegex(PolicyError, "job script must be inside"):
                submit_job(sandbox, script, dry_run=True)

    def test_parse_sbatch_directives_supports_short_and_long_forms(self) -> None:
        options = parse_sbatch_directives(
            """#!/bin/sh
#SBATCH -A project_123
#SBATCH -p dev-g
#SBATCH --time 00:10:00
#SBATCH --gres=gpu:1
"""
        )

        self.assertEqual(options["account"], "project_123")
        self.assertEqual(options["partition"], "dev-g")
        self.assertEqual(options["time"], "00:10:00")
        self.assertEqual(options["gres"], "gpu:1")

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
            script.write_text("#!/bin/sh\nhostname\n", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--root", tmp, "--account", "project_123", "submit", "demo", "jobs/ok.sh", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn(f"{sandbox.path}/jobs/ok.sh", stdout.getvalue())
            self.assertIn("--partition=debug", stdout.getvalue())
            self.assertNotIn("--gpus-per-node", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
