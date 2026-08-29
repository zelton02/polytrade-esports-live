import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/test.yml"
DEPLOY_SCRIPT = ROOT / "deploy/deploy.sh"
SYNC_SCRIPT = ROOT / "deploy/sync-production.sh"


class DeploymentWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text()
        cls.deploy_script = DEPLOY_SCRIPT.read_text()
        cls.sync_script = SYNC_SCRIPT.read_text()

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
        self.assertIn("bash deploy/sync-production.sh", self.workflow)
        self.assertIn("--dry-run", self.sync_script)
        self.assertIn("--delete-delay", self.sync_script)
        self.assertNotIn("--delete-excluded", self.sync_script)
        for protected in ("protect /.env", "protect /data/***", "protect backups/***"):
            self.assertIn(protected, self.sync_script)
        for exclusion in ("'/.env'", "'/data/'", "'*.sqlite3'", "'backups/'"):
            self.assertIn("--exclude=%s" % exclusion, self.sync_script)

    def test_backup_finishes_before_source_sync_and_is_reverified(self):
        backup = self.workflow.index("Back up production SQLite before code sync")
        sync = self.workflow.index("Safely synchronize production source")
        deploy = self.workflow.index("Deploy and verify production")
        self.assertLess(backup, sync)
        self.assertLess(sync, deploy)
        self.assertIn("--pre-deploy-backup '$BACKUP_PATH'", self.workflow)
        self.assertIn(".backup '$backup_path'", self.deploy_script)
        self.assertIn("PRAGMA integrity_check;", self.deploy_script)

    def test_v2_cutover_and_shadow_service_are_deployment_contracts(self):
        self.assertIn(
            "SERVICES=(collector executor priors shadow dashboard)",
            self.deploy_script,
        )
        self.assertIn("a.name='execution-paper'", self.deploy_script)
        self.assertIn("wait for a clean v2 cohort cutover", self.deploy_script)
        self.assertIn("schema_version is $SCHEMA_VERSION instead of 8", self.deploy_script)
        self.assertIn("'execution-paper-v2'", self.deploy_script)

    def test_source_hash_ignores_python_bytecode(self):
        self.assertIn("! -name '*.py[cod]'", self.deploy_script)
        self.assertIn("-not -path '*/__pycache__/*'", self.deploy_script)


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
            docker_log = root / "docker-calls"
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\nexit 0\n"
            )
            docker.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "PROJECT_ROOT": str(root),
                    "PATH": "%s:/usr/bin:/bin" % fake_bin,
                    "DOCKER_LOG": str(docker_log),
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
            calls = docker_log.read_text() if docker_log.exists() else ""
            self.assertNotIn("build", calls)
            self.assertNotIn("compose up", calls)
            self.assertEqual(list((data / "backups").glob("*.sqlite3")), [])


class DeploymentCutoverSafetyTests(unittest.TestCase):
    def test_legacy_open_position_stops_before_docker(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data = root / "data"
            fake_bin = root / "bin"
            data.mkdir()
            fake_bin.mkdir()
            database = data / "esports_live.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE paper_accounts(
                        account_id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE paper_positions(
                        account_id INTEGER NOT NULL,
                        shares REAL NOT NULL
                    );
                    CREATE TABLE paper_orders(
                        account_id INTEGER NOT NULL,
                        status TEXT NOT NULL
                    );
                    INSERT INTO paper_accounts(account_id, name)
                    VALUES(1, 'execution-paper');
                    INSERT INTO paper_positions(account_id, shares)
                    VALUES(1, 2.5);
                    """
                )

            docker_log = root / "docker-calls"
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\nexit 0\n"
            )
            docker.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "PROJECT_ROOT": str(root),
                    "PATH": "%s:/usr/bin:/bin" % fake_bin,
                    "DOCKER_LOG": str(docker_log),
                }
            )

            result = subprocess.run(
                ["bash", str(DEPLOY_SCRIPT)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "execution-paper still has 1 open positions and 0 active orders",
                result.stderr,
            )
            calls = docker_log.read_text() if docker_log.exists() else ""
            self.assertNotIn("build", calls)
            self.assertNotIn("compose up", calls)


class BackupWriterPauseTests(unittest.TestCase):
    """The backup must pause the writers, and must always restart them."""

    def _sandbox(self, root, sqlite_body):
        data = root / "data"
        fake_bin = root / "bin"
        data.mkdir()
        fake_bin.mkdir()

        docker_log = root / "docker-calls"
        docker = fake_bin / "docker"
        # Report every writer as an existing, running container so pause_writers
        # has something to stop.
        docker.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\n"
            "if [ \"$1\" = compose ] && [ \"$2\" = ps ]; then echo \"container-$4\"; exit 0; fi\n"
            "if [ \"$1\" = inspect ]; then echo true; exit 0; fi\n"
            "exit 0\n"
        )
        docker.chmod(0o755)

        if sqlite_body is not None:
            sqlite = fake_bin / "sqlite3"
            sqlite.write_text(sqlite_body)
            sqlite.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "PROJECT_ROOT": str(root),
                "PATH": "%s:/usr/bin:/bin" % fake_bin,
                "DOCKER_LOG": str(docker_log),
            }
        )
        return data, docker_log, environment

    def test_failed_backup_still_restarts_every_paused_writer(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data, docker_log, environment = self._sandbox(
                root, "#!/bin/sh\nexit 23\n"
            )
            (data / "esports_live.sqlite3").write_bytes(b"not-used-by-the-fake")

            result = subprocess.run(
                ["bash", str(DEPLOY_SCRIPT), "--backup-only"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SQLite .backup failed", result.stderr)
            calls = docker_log.read_text()
            stop = "compose stop collector executor priors shadow"
            start = "compose start collector executor priors shadow"
            self.assertIn(stop, calls)
            self.assertIn(start, calls)
            self.assertLess(calls.index(stop), calls.index(start))
            self.assertNotIn("build", calls)
            self.assertNotIn("compose up", calls)

    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 is not installed")
    def test_successful_backup_pauses_then_resumes_the_writers(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data, docker_log, environment = self._sandbox(root, None)
            with sqlite3.connect(data / "esports_live.sqlite3") as connection:
                connection.execute("CREATE TABLE metadata(key TEXT, value TEXT)")

            result = subprocess.run(
                ["bash", str(DEPLOY_SCRIPT), "--backup-only"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = docker_log.read_text()
            stop = "compose stop collector executor priors shadow"
            start = "compose start collector executor priors shadow"
            self.assertLess(calls.index(stop), calls.index(start))
            self.assertEqual(
                len(list((data / "backups").glob("*.pre-deploy.sqlite3"))), 1
            )


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class ProductionSyncSafetyTests(unittest.TestCase):
    def test_obsolete_code_is_deleted_but_production_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "source"
            destination = root / "destination"
            (source / "src").mkdir(parents=True)
            (destination / "src" / "__pycache__").mkdir(parents=True)
            (destination / "data" / "backups").mkdir(parents=True)
            (source / "src" / "current.py").write_text("current\n")
            (destination / "src" / "obsolete.py").write_text("obsolete\n")
            (destination / "src" / "__pycache__" / "old.pyc").write_bytes(b"old")
            (destination / ".env").write_text("DASHBOARD_SECRET=preserve\n")
            database = destination / "data" / "esports_live.sqlite3"
            database.write_bytes(b"production-db")
            backup = destination / "data" / "backups" / "before.sqlite3"
            backup.write_bytes(b"production-backup")

            result = subprocess.run(
                ["bash", str(SYNC_SCRIPT), str(source), str(destination)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((destination / "src" / "current.py").is_file())
            self.assertFalse((destination / "src" / "obsolete.py").exists())
            self.assertFalse((destination / "src" / "__pycache__").exists())
            self.assertEqual((destination / ".env").read_text(), "DASHBOARD_SECRET=preserve\n")
            self.assertEqual(database.read_bytes(), b"production-db")
            self.assertEqual(backup.read_bytes(), b"production-backup")


if __name__ == "__main__":
    unittest.main()
