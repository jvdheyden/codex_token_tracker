import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ensure_otel_collector.sh"
FAKE_COMMAND = r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["FAKE_RUNTIME"])
settings = json.loads((root / "settings.json").read_text())
command = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps([command, *args]) + "\n")

if command == "uname":
    print(settings.get("platform", "Darwin"))
elif command == "open":
    if settings.get("launch_fails"):
        sys.exit(1)
    (root / "launched").touch()
elif command == "docker":
    if args[0] == "info":
        if settings.get("permission_denied"):
            print("permission denied", file=sys.stderr)
            sys.exit(1)
        if settings.get("online"):
            sys.exit(0)
        if (root / "launched").exists() and not settings.get("never_ready"):
            sys.exit(0)
        print("cannot connect to Docker daemon", file=sys.stderr)
        sys.exit(1)
    elif args[:2] == ["context", "inspect"]:
        print(settings.get("endpoint", "unix:///Users/test/.docker/run/docker.sock"))
    elif args[0] == "inspect":
        print(settings.get("container_state", "exited"))
    elif args[0] not in ("start", "run", "rm"):
        sys.exit("Unexpected Docker command: " + repr(args))
'''


class CollectorStartupTests(unittest.TestCase):
    def run_helper(self, settings, overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps(settings))
            for name in ("docker", "open", "uname"):
                command = root / name
                command.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
                command.chmod(0o755)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CODEX_TOKEN_TRACKER_", "DOCKER_"))}
            env.update({
                "PATH": f"{root}:/usr/bin:/bin",
                "FAKE_RUNTIME": str(root),
                "CODEX_TOKEN_TRACKER_DATA_DIR": str(root / "data"),
                "CODEX_TOKEN_TRACKER_DOCKER_START_TIMEOUT": "1",
            })
            env.update(overrides or {})
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)], env=env,
                capture_output=True, text=True, timeout=5,
            )
            calls = [json.loads(line) for line in
                     (root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        return result, calls

    def test_cold_desktop_starts_before_existing_collector(self):
        result, calls = self.run_helper({})
        launch = calls.index(["open", "-g", "-a", "Docker"])
        start = calls.index(["docker", "start", "codex-otel-collector"])
        self.assertLess(launch, start)
        self.assertIn(["docker", "info"], calls[launch + 1:start])
        self.assertEqual(result.stderr, "")

    def test_cold_desktop_creates_missing_collector(self):
        result, calls = self.run_helper({"container_state": ""})
        run = next(call for call in calls if call[:2] == ["docker", "run"])
        self.assertIn("127.0.0.1:4318:4318", run)
        self.assertIn("unless-stopped", run)
        self.assertEqual(result.stderr, "")

    def test_running_collector_needs_no_launch_or_restart(self):
        result, calls = self.run_helper({"online": True, "container_state": "running"})
        self.assertFalse(any(call[0] == "open" or call[:2] == ["docker", "start"]
                             for call in calls))
        self.assertEqual(result.stderr, "")

    def test_permission_failure_does_not_launch_desktop(self):
        result, calls = self.run_helper({"permission_denied": True})
        self.assertIn("not accessible without sudo", result.stderr)
        self.assertEqual(calls, [["docker", "info"]])

    def test_unrelated_endpoints_and_opt_out_do_not_launch_desktop(self):
        for settings, overrides in (
            ({"endpoint": "ssh://remote-host"}, {}),
            ({"endpoint": "unix:///Users/test/.orbstack/run/docker.sock"}, {}),
            ({"platform": "Linux"}, {}),
            ({}, {"CODEX_TOKEN_TRACKER_START_DOCKER": "0"}),
            ({}, {"DOCKER_HOST": "tcp://remote-host:2376"}),
        ):
            with self.subTest(settings=settings, overrides=overrides):
                result, calls = self.run_helper(settings, overrides)
                self.assertIn("not running or not reachable", result.stderr)
                self.assertFalse(any(call[0] == "open" for call in calls))

    def test_explicit_context_takes_precedence_over_docker_host(self):
        result, calls = self.run_helper({}, {
            "DOCKER_CONTEXT": "desktop-linux",
            "DOCKER_HOST": "tcp://remote-host:2376",
        })
        self.assertIn(["open", "-g", "-a", "Docker"], calls)
        self.assertEqual(result.stderr, "")

    def test_failed_launch_returns_warning_without_starting_container(self):
        result, calls = self.run_helper({"launch_fails": True})
        self.assertIn("could not launch Docker Desktop", result.stderr)
        self.assertFalse(any(call[:2] == ["docker", "start"] for call in calls))

    def test_readiness_timeout_returns_warning_without_starting_container(self):
        result, calls = self.run_helper({"never_ready": True})
        self.assertIn("did not become ready within 1s", result.stderr)
        self.assertFalse(any(call[:2] == ["docker", "start"] for call in calls))

    def test_invalid_timeout_does_not_launch_desktop(self):
        result, calls = self.run_helper({}, {"CODEX_TOKEN_TRACKER_DOCKER_START_TIMEOUT": "oops"})
        self.assertIn("must be a positive number", result.stderr)
        self.assertFalse(any(call[0] == "open" for call in calls))


if __name__ == "__main__":
    unittest.main()
