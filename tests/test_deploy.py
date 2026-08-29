import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/test.yml"
DEPLOY_SCRIPT = ROOT / "deploy/deploy.sh"


class DeploymentWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text()
        cls.deploy_script = DEPLOY_SCRIPT.read_text()

    def test_only_main_pushes_can_deploy_after_every_test_job(self):
        self.assertIn("branches:\n      - main", self.workflow)
        self.assertIn("pull_request:", self.workflow)
        self.assertIn(
            "if: github.event_name == 'push' && github.ref == 'refs/heads/main'",
            self.workflow,
        )
        deploy_job = self.workflow.split("\n  deploy:\n", 1)[1]
        for dependency in ("unittest", "dashboard-render", "deployment-static"):
            self.assertIn("      - %s" % dependency, deploy_job)
        self.assertIn("group: polytrade-esports-production", deploy_job)
        self.assertIn("cancel-in-progress: false", deploy_job)

    def test_ssh_and_rsync_keep_production_secrets_and_data_out_of_source(self):
        for secret in (
            "PROD_SSH_HOST",
            "PROD_SSH_USER",
            "PROD_SSH_PRIVATE_KEY",
            "PROD_SSH_KNOWN_HOSTS",
        ):
            self.assertIn("secrets.%s" % secret, self.workflow)
        self.assertIn("StrictHostKeyChecking yes", self.workflow)
        self.assertNotIn("StrictHostKeyChecking=no", self.workflow)
        self.assertNotIn("--delete", self.workflow)
        for exclusion in ("'/.env'", "'/data/'", "'*.sqlite3'", "'backups/'"):
            self.assertIn("--exclude=%s" % exclusion, self.workflow)

    def test_backup_finishes_before_source_sync_and_is_reverified(self):
        backup = self.workflow.index("Back up production SQLite before code sync")
        sync = self.workflow.index("Safely synchronize production source")
        deploy = self.workflow.index("Deploy and verify production")
        self.assertLess(backup, sync)
        self.assertLess(sync, deploy)
        self.assertIn("--pre-deploy-backup '$BACKUP_PATH'", self.workflow)
        self.assertIn(".backup '$backup_path'", self.deploy_script)
        self.assertIn("PRAGMA integrity_check;", self.deploy_script)


class DeploymentBackupFailureTests(unittest.TestCase):
    def test_sqlite_backup_failure_stops_before_docker(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data = root / "data"
            fake_bin = root / "bin"
            data.mkdir()
            fake_bin.mkdir()
            (data / "esports_live.sqlite3").write_bytes(b"not-used-by-the-fake")

            sqlite = fake_bin / "sqlite3"
            sqlite.write_text("#!/bin/sh\nexit 23\n")
            sqlite.chmod(0o755)
            docker_marker = root / "docker-was-called"
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf called > \"$DOCKER_MARKER\"\nexit 0\n"
            )
            docker.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PROJECT_ROOT": str(root),
                    "PATH": "%s:/usr/bin:/bin" % fake_bin,
                    "DOCKER_MARKER": str(docker_marker),
                }
            )
            result = subprocess.run(
                ["bash", str(DEPLOY_SCRIPT), "--backup-only"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SQLite .backup failed; deployment has not started", result.stderr)
            self.assertFalse(docker_marker.exists())
            self.assertEqual(list((data / "backups").glob("*.sqlite3")), [])


if __name__ == "__main__":
    unittest.main()
