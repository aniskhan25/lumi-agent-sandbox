import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lumi_agent_sandbox.cli import main
from lumi_agent_sandbox.sandbox import (
    create_sandbox,
    destroy_sandbox,
    load_config,
    load_sandbox,
    read_policy,
    resolve_account,
    resolve_agent_image,
    resolve_agent_image_override,
    sandbox_root,
    task_id,
)
from lumi_agent_sandbox.slurm import PolicyError, parse_sbatch_directives, parse_slurm_time, submit_job


class SandboxTests(unittest.TestCase):
    def test_task_id_is_directory_safe(self) -> None:
        self.assertEqual(task_id("My Test / Task"), "my-test-task")

    def test_account_comes_from_config(self) -> None:
        self.assertEqual(resolve_account(None, {"account": "project_123"}), "project_123")

    def test_default_root_uses_user_directory(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "LUMI_AGENT_SANDBOX_ROOT"}
        env["USER"] = "anisrahm"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                sandbox_root(None, "project_462000131"),
                Path("/scratch/project_462000131/anisrahm/agent-sandboxes"),
            )

    def test_agent_image_comes_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"LUMI_AGENT_IMAGE": "/env/agent.sif"}, clear=True):
            self.assertEqual(resolve_agent_image(None, {"agent_image": "/config/agent.sif"}), "/env/agent.sif")
            self.assertEqual(resolve_agent_image_override(None), "/env/agent.sif")

    def test_agent_image_comes_from_config(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_agent_image(None, {"agent_image": "/config/agent.sif"}), "/config/agent.sif")

    def test_missing_agent_image_reports_config_file(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "lumi-agent-sandbox.yaml"):
                resolve_agent_image(None, {})

    def test_load_config_reads_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "lumi-agent-sandbox.yaml").write_text("account: project_123\nagent_image: /agent.sif\n", encoding="utf-8")

            current = Path.cwd()
            try:
                os.chdir(root)
                config = load_config()
            finally:
                os.chdir(current)

            self.assertEqual(config["account"], "project_123")
            self.assertEqual(config["agent_image"], "/agent.sif")

    def test_empty_agent_image_writes_clear_enter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "")
            policy = read_policy(sandbox.path / "policy.yaml")
            enter = (sandbox.path / "enter.sh").read_text(encoding="utf-8")

            self.assertEqual(policy["agent_image"], "")
            self.assertIn("No agent image configured", enter)

    def test_cli_agent_image_overrides_empty_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            create_sandbox("demo", Path(tmp), "project_123", "")

            sandbox = load_sandbox("demo", Path(tmp), "project_123", "/agent.sif")

            self.assertEqual(sandbox.agent_image, "/agent.sif")

    def test_load_preserves_policy_image_without_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            create_sandbox("demo", Path(tmp), "project_123", "/policy.sif")

            sandbox = load_sandbox("demo", Path(tmp), "project_123", None)

            self.assertEqual(sandbox.agent_image, "/policy.sif")

    def test_create_writes_policy_and_enter_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")

            self.assertTrue((sandbox.path / "work").is_dir())
            self.assertTrue((sandbox.path / "input").is_dir())
            self.assertTrue((sandbox.path / "enter.sh").exists())
            self.assertTrue((sandbox.path / "shell.sh").exists())

            policy = read_policy(sandbox.path / "policy.yaml")
            self.assertEqual(policy["account"], "project_123")
            self.assertEqual(policy["agent_image"], "/agent.sif")
            self.assertEqual(policy["allowed_partitions"], ["small", "standard", "dev-g", "small-g", "standard-g"])
            self.assertNotIn("allowed_paths", policy)
            self.assertFalse((sandbox.path / "manifests").exists())

            enter = (sandbox.path / "enter.sh").read_text(encoding="utf-8")
            self.assertIn('--home "$SANDBOX/state/home:/home/agent"', enter)
            self.assertIn("--cleanenv", enter)
            self.assertIn("singularity run", enter)
            self.assertNotIn("SINGULARITYENV_HOME", enter)
            self.assertIn("SINGULARITYENV_PREPEND_PATH=/safe-bin", enter)
            self.assertNotIn(" opencode", enter)
            self.assertIn('--bind "$SANDBOX/input:/input:ro"', enter)
            self.assertTrue((sandbox.path / "wrappers" / "sbatch").exists())
            self.assertTrue((sandbox.path / "wrappers" / "srun").exists())

            shell = (sandbox.path / "shell.sh").read_text(encoding="utf-8")
            self.assertIn("singularity exec", shell)
            self.assertIn("/bin/sh", shell)
            self.assertIn("PATH=/safe-bin:/usr/local/bin:/usr/bin:/bin", shell)
            self.assertIn('--home "$SANDBOX/state/home:/home/agent"', shell)

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

    def test_submit_rejects_day_prefixed_large_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "large.sh"
            script.write_text(
                """#!/bin/sh
#SBATCH --partition=dev-g
#SBATCH --time=0-01:00
hostname
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PolicyError, "requested time"):
                submit_job(sandbox, script, dry_run=True)

    def test_parse_slurm_day_prefixed_times(self) -> None:
        self.assertEqual(parse_slurm_time("0-01:00"), 3600)
        self.assertEqual(parse_slurm_time("0-00:45"), 2700)
        self.assertEqual(parse_slurm_time("1-00"), 86400)

    def test_submit_ignores_script_log_directives_because_cli_forces_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = create_sandbox("demo", Path(tmp), "project_123", "/agent.sif")
            script = sandbox.path / "jobs" / "logs.sh"
            script.write_text(
                """#!/bin/sh
#SBATCH --partition=dev-g
#SBATCH --output=relative.out
#SBATCH --error=relative.err
hostname
""",
                encoding="utf-8",
            )

            command = submit_job(sandbox, script, dry_run=True)

            self.assertIn(f"--output={sandbox.path}/logs/%x-%j.out", command)
            self.assertIn(f"--error={sandbox.path}/logs/%x-%j.err", command)

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
            script.write_text("#!/bin/sh\n#SBATCH --partition=dev-g\nhostname\n", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--root", tmp, "--account", "project_123", "submit", "demo", "jobs/ok.sh", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn(f"{sandbox.path}/jobs/ok.sh", stdout.getvalue())
            self.assertNotIn("--gpus-per-node", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
