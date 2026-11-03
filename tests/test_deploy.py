"""Exercise remote file installation/rollback with simulated systemd and Podman.

These do not claim to test a real container or a real Pi; the live installer does
that only after it reaches the server.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.account = self.path / "account"
        self.source = self.path / "source"
        self.bin = self.path / "bin"
        self.bin.mkdir()
        (self.source / "deploy/quadlet").mkdir(parents=True)
        (self.source / "stock_agent").mkdir()
        (self.source / "Containerfile").write_text("FROM test\n")
        (self.source / "stock_agent/__init__.py").write_text("version = 1\n")
        # Preflight is separately checked by the live installer. Model a ready host.
        (self.source / "deploy/preflight.sh").write_text("exit 0\n")
        for name in ("stock-agent.container", "stock-agent-data.volume"):
            (self.source / "deploy/quadlet" / name).write_text("new " + name)
        self.credentials = self.path / "new.env"
        self.credentials.write_text("NEW_CREDENTIALS\n")
        self.write_command(
            "podman",
            """#!/usr/bin/env python3
import os,sys
from pathlib import Path
p=Path(os.environ['DEPLOY_TEST_STATE'])
args=sys.argv[1:]
with (p/'calls').open('a') as f:f.write('podman '+' '.join(args)+'\\n')
if args[:2]==['image','exists']:
 sys.exit(0 if (p/'has_previous').exists() else 1)
if args[:2]==['container','exists']:
 sys.exit(0 if (p/'has_previous').exists() else 1)
if args[0]=='inspect':print('true')
if args[:2]==['exec','-i']:
 if (p/'fail_backup').exists():sys.exit(1)
 print('pre-deploy-test.sqlite3')
if args[0]=='build' and (p/'fail_build').exists():sys.exit(1)
if args[0]=='tag' and args[-1].endswith(':current'):
 (p/'current_image').write_text(args[1])
if 'doctor' in args and (p/'fail_smoke').exists():sys.exit(1)
""",
        )
        self.write_command(
            "systemctl",
            """#!/usr/bin/env python3
import os,sys
from pathlib import Path
with (Path(os.environ['DEPLOY_TEST_STATE'])/'calls').open('a') as f:
 f.write('systemctl '+' '.join(sys.argv[1:])+'\\n')
p=Path(os.environ['DEPLOY_TEST_STATE'])
if sys.argv[1:3]==['--user','restart']:
 counter=p/'restart_count'
 count=int(counter.read_text())+1 if counter.exists() else 1
 counter.write_text(str(count))
 if count>1 and (p/'fail_restart').exists():sys.exit(1)
""",
        )
        self.write_command(
            "journalctl",
            """#!/usr/bin/env python3
import os,sys
from pathlib import Path
with (Path(os.environ['DEPLOY_TEST_STATE'])/'calls').open('a') as f:
 f.write('journalctl '+' '.join(sys.argv[1:])+'\\n')
print('Captured service diagnostic')
""",
        )

    def write_command(self, name, text):
        script = self.bin / name
        script.write_text(text)
        script.chmod(0o755)

    def run_install(self):
        return subprocess.run(
            [
                "bash",
                str(ROOT / "deploy/install-remote.sh"),
                str(self.source),
                str(self.credentials),
                str(self.account),
            ],
            env=os.environ
            | {
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "DEPLOY_TEST_STATE": str(self.path),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )

    def old_deployment(self):
        (self.path / "has_previous").touch()
        config = self.account / ".config/stock-agent"
        units = self.account / ".config/containers/systemd"
        config.mkdir(parents=True)
        units.mkdir(parents=True)
        (config / "agent.env").write_text("OLD_CREDENTIALS\n")
        for name in ("stock-agent.container", "stock-agent-data.volume"):
            (units / name).write_text("old " + name)

    def test_success_installs_private_credentials_and_checks_delivery(self):
        self.old_deployment()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = self.account / ".config/stock-agent/agent.env"
        self.assertEqual(env.read_text(), "NEW_CREDENTIALS\n")
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        calls = (self.path / "calls").read_text()
        self.assertIn("doctor --market", calls)
        self.assertIn("doctor --send", calls)
        self.assertLess(calls.rindex("systemctl --user restart"), calls.index("doctor --send"))
        self.assertIn("podman port stock-agent", calls)
        self.assertIn("Deployment verified", result.stdout)
        self.assertEqual(list(self.account.glob(".local/share/stock-agent/backup.*")), [])

    def test_failed_smoke_restores_previous_units_credentials_and_image(self):
        self.old_deployment()
        (self.path / "fail_smoke").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        config = self.account / ".config/stock-agent"
        units = self.account / ".config/containers/systemd"
        self.assertEqual((config / "agent.env").read_text(), "OLD_CREDENTIALS\n")
        self.assertEqual((units / "stock-agent.container").read_text(), "old stock-agent.container")
        self.assertEqual(
            (self.path / "current_image").read_text(), "localhost/stock-agent:previous"
        )
        self.assertIn(
            "systemctl --user start stock-agent.service", (self.path / "calls").read_text()
        )
        self.assertNotIn("Deployment verified", result.stdout)

    def test_failed_first_deployment_cleans_new_credentials_and_units(self):
        (self.path / "fail_smoke").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.account / ".config/stock-agent/agent.env").exists())
        self.assertFalse(
            (self.account / ".config/containers/systemd/stock-agent.container").exists()
        )

    def test_failure_diagnostics_are_captured_before_rollback_removes_service(self):
        (self.path / "fail_smoke").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        calls = (self.path / "calls").read_text()
        self.assertLess(calls.index("journalctl "), calls.index("systemctl --user stop"))
        self.assertIn("Captured service diagnostic", result.stderr)
        self.assertNotIn("NEW_CREDENTIALS", result.stderr)

    def test_failed_restart_does_not_send_a_deployment_success_message(self):
        (self.path / "fail_restart").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        calls = (self.path / "calls").read_text()
        self.assertIn("doctor --market", calls)
        self.assertNotIn("doctor --send", calls)
        self.assertNotIn("Deployment verified", result.stdout)

    def test_leftover_image_without_configuration_is_not_a_previous_deployment(self):
        (self.path / "has_previous").touch()
        (self.path / "fail_smoke").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        calls = (self.path / "calls").read_text()
        self.assertNotIn("systemctl --user start stock-agent.service", calls)

    def test_failed_build_leaves_existing_service_untouched(self):
        self.old_deployment()
        (self.path / "fail_build").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.account / ".config/stock-agent/agent.env").read_text(), "OLD_CREDENTIALS\n"
        )
        self.assertNotIn("systemctl", (self.path / "calls").read_text())

    def test_failed_backup_preserves_running_release(self):
        self.old_deployment()
        (self.path / "fail_backup").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.account / ".config/stock-agent/agent.env").read_text(), "OLD_CREDENTIALS\n"
        )
        self.assertNotIn("systemctl", (self.path / "calls").read_text())

    def test_rollback_restores_database_before_previous_service_starts(self):
        self.old_deployment()
        (self.path / "fail_smoke").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        calls = (self.path / "calls").read_text()
        self.assertIn("pre-deploy-test.sqlite3", calls)
        restore = calls.index("podman run --rm --network=none")
        restart = calls.index("systemctl --user start stock-agent.service")
        self.assertLess(restore, restart)

    def test_bundled_generator_install_is_app_scoped(self):
        tools = self.source / "deploy/tools"
        tools.mkdir()
        binary = tools / "quadlet"
        binary.write_bytes(b"simulated-generator")
        import hashlib

        (tools / "SHA256SUMS").write_text(
            hashlib.sha256(binary.read_bytes()).hexdigest() + "  quadlet\n"
        )
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.account / ".local/libexec/stock-agent/quadlet"
        self.assertTrue(installed.exists())
        self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
        self.assertTrue(
            (self.account / ".config/stock-agent/quadlet/stock-agent.container").exists()
        )
        self.assertFalse(
            (self.account / ".config/containers/systemd/stock-agent.container").exists()
        )

    def test_failed_initial_bundled_install_removes_private_generator(self):
        tools = self.source / "deploy/tools"
        tools.mkdir()
        binary = tools / "quadlet"
        binary.write_bytes(b"simulated-generator")
        import hashlib

        (tools / "SHA256SUMS").write_text(
            hashlib.sha256(binary.read_bytes()).hexdigest() + "  quadlet\n"
        )
        (self.path / "fail_smoke").touch()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.account / ".local/libexec/stock-agent/quadlet").exists())
        self.assertFalse(
            (self.account / ".config/stock-agent/quadlet/stock-agent.container").exists()
        )


class SshHandoffTests(unittest.TestCase):
    def test_authentication_is_reused_and_secrets_stay_out_of_source_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            executable = path / "ssh"
            executable.write_text("""#!/usr/bin/env python3
import io,json,os,sys,tarfile
from pathlib import Path
p=Path(os.environ['DEPLOY_TEST_STATE'])
args=sys.argv[1:]
with (p/'calls').open('a') as f:f.write(json.dumps(args)+'\\n')
if '-fN' in args or '-O' in args:sys.exit(0)
if args[-1]=='bash -s -- auto':
 sys.stdin.buffer.read()
 sys.exit(0)
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as bundle:
 for item in bundle:
  if item.name=='source.tar.gz':
   data=bundle.extractfile(item).read()
   with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as source:
    (p/'source_names').write_text('\\n'.join(source.getnames()))
""")
            executable.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/deploy.sh")],
                cwd=ROOT,
                env=os.environ
                | {
                    "PATH": str(path) + os.pathsep + os.environ["PATH"],
                    "DEPLOY_TEST_STATE": str(path),
                    "DEPLOY_ENV_FILE": str(path / "unused.env"),
                    "SSH_TARGET": "test-user@host.invalid",
                    "SSH_PORT": "2222",
                    "TELEGRAM_BOT_TOKEN": "123456:" + "x" * 32,
                    "TELEGRAM_CHAT_ID": "123",
                    "ALPHA_VANTAGE_API_KEY": "test-key",
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            import json

            calls = [json.loads(line) for line in (path / "calls").read_text().splitlines()]
            self.assertIn("-fN", calls[0])
            self.assertEqual(calls[0][-1], "test-user@host.invalid")
            self.assertEqual(calls[0][calls[0].index("-p") + 1], "2222")
            self.assertEqual(calls[1][-1], "bash -s -- auto")
            self.assertIn("-O", calls[-1])
            self.assertNotIn("BatchMode=yes", str(calls))
            self.assertNotIn("StrictHostKeyChecking=no", str(calls))
            names = (path / "source_names").read_text().splitlines()
            self.assertIn("Containerfile", names)
            self.assertIn("stock_agent/api.py", names)
            self.assertNotIn(".env", names)
            self.assertNotIn(".env.deploy", names)
            self.assertFalse(any("__pycache__" in name for name in names))


class SshConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.config = self.path / "deploy.env"
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SSH_TARGET", "SSH_PORT", "SSH_IDENTITY_FILE", "SSH_CONFIG_FILE"}
        } | {"DEPLOY_ENV_FILE": str(self.config)}

    def options(self, **overrides):
        return subprocess.run(
            [
                "bash",
                "-eu",
                "-c",
                'source scripts/ssh-options.sh; printf "%s\\0" "$ssh_target" "${ssh_args[@]}"',
            ],
            cwd=ROOT,
            env=self.environment | overrides,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_local_options_preserve_spaces_without_executing_shell_expressions(self):
        marker = self.path / "must-not-exist"
        identity = f"/private key/$(touch {marker})"
        self.config.write_text(
            f'SSH_TARGET=containers@pi.invalid\nSSH_IDENTITY_FILE="{identity}"\n'
        )
        result = self.options()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.rstrip("\0").split("\0")
        self.assertEqual(arguments[0], "containers@pi.invalid")
        self.assertEqual(arguments[arguments.index("-p") + 1], "22")
        self.assertEqual(arguments[arguments.index("-i") + 1], identity)
        self.assertFalse(marker.exists())

    def test_environment_overrides_local_deployment_defaults(self):
        self.config.write_text("SSH_TARGET=containers@local.invalid\nSSH_PORT=2222\n")
        result = self.options(SSH_TARGET="other@remote.invalid", SSH_PORT="4853")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.rstrip("\0").split("\0")
        self.assertEqual(arguments[0], "other@remote.invalid")
        self.assertEqual(arguments[arguments.index("-p") + 1], "4853")

    def test_missing_or_option_like_target_is_rejected(self):
        for target in ("", "-oProxyCommand=untrusted", "user@host extra-option"):
            with self.subTest(target=target):
                result = self.options(SSH_TARGET=target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Set SSH_TARGET", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_invalid_port_is_rejected_before_ssh(self):
        for port in ("0", "65536", "-p22", "22\n23"):
            with self.subTest(port=port):
                result = self.options(SSH_TARGET="user@host.invalid", SSH_PORT=port)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SSH_PORT", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_inventory_uses_shared_options_from_outside_the_project(self):
        self.config.write_text("SSH_TARGET=inventory@host.invalid\nSSH_PORT=2222\n")
        executable = self.path / "ssh"
        executable.write_text('#!/usr/bin/env bash\nprintf "%s\\0" "$@"\n')
        executable.chmod(0o755)
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/inspect-server.sh")],
            cwd=self.path,
            env=self.environment | {"PATH": str(self.path) + os.pathsep + os.environ["PATH"]},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = result.stdout.rstrip("\0").split("\0")
        self.assertIn("inventory@host.invalid", arguments)
        self.assertEqual(arguments[arguments.index("-p") + 1], "2222")


if __name__ == "__main__":
    unittest.main()
