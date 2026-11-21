"""Exercise the Unix launcher without making network requests."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("sh")


@unittest.skipUnless(SHELL and os.name != "nt", "A POSIX shell is required")
class ShellLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leafline launcher ")
        self.base = Path(self.temp.name)
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        shutil.copy2(ROOT / "run.sh", self.project / "run.sh")
        (self.project / "app.py").write_text("# fake application\n")
        (self.project / "requirements.txt").write_text("example-package\n")
        self.fake_bin = self.base / "fake bin"
        self.fake_bin.mkdir()
        self.log = self.base / "calls.log"
        fake_python = self.fake_bin / "python3"
        fake_python.write_text('''#!/bin/sh
if [ "$1" = "-c" ]; then
    [ "${TEST_FAIL_VERSION:-0}" != "1" ]
    exit $?
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    echo "venv:$3" >> "$TEST_LOG"
    if [ "${TEST_FAIL_VENV:-0}" = "1" ]; then exit 1; fi
    mkdir -p "$3/bin"
    cp "$0" "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    printf 'pip:%s:%s:%s\n' "$3" "$4" "$5" >> "$TEST_LOG"
    [ "${TEST_FAIL_PIP:-0}" != "1" ]
    exit $?
fi
printf 'app-cwd:%s\n' "$PWD" >> "$TEST_LOG"
printf 'app-arg:%s\n' "$@" >> "$TEST_LOG"
exit "${TEST_APP_EXIT:-0}"
''')
        fake_python.chmod(0o755)
        # Ensure version-check failures do not fall through to a real Python.
        for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python"):
            shutil.copy2(fake_python, self.fake_bin / name)
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.fake_bin}{os.pathsep}{self.env.get('PATH', '')}", TEST_LOG=str(self.log))

    def tearDown(self):
        self.temp.cleanup()

    def run_launcher(self, *args, **env):
        return subprocess.run([SHELL, str(self.project / "run.sh"), *args], cwd=self.base,
                              env={**self.env, **env}, text=True, capture_output=True, timeout=15)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_creates_environment_installs_and_forwards_arguments_from_another_directory(self):
        result = self.run_launcher("--port", "8080", "--data-dir", "a folder")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn(f"venv:{self.project / '.venv'}", calls)
        self.assertIn(f"pip:install:-r:{self.project / 'requirements.txt'}", calls)
        self.assertIn(f"app-cwd:{self.project}", calls)
        self.assertIn("app-arg:--port\napp-arg:8080\napp-arg:--data-dir\napp-arg:a folder", calls)

    def test_reuses_environment_and_checks_requirements_on_later_runs(self):
        self.assertEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.run_launcher().returncode, 0)
        self.assertEqual(self.calls().count("venv:"), 1)
        self.assertEqual(self.calls().count("pip:install:"), 2)

    def test_failed_environment_creation_reports_a_fix(self):
        result = self.run_launcher(TEST_FAIL_VENV="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not create .venv", result.stderr)
        self.assertIn("python3-venv", result.stderr)
        self.assertNotIn("app-cwd:", self.calls())

    def test_failed_install_does_not_start_application(self):
        result = self.run_launcher(TEST_FAIL_PIP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requirements could not be installed", result.stderr)
        self.assertNotIn("app-cwd:", self.calls())

    def test_missing_suitable_python_has_an_actionable_error(self):
        result = self.run_launcher(TEST_FAIL_VERSION="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.10 or newer is required", result.stderr)
        self.assertFalse((self.project / ".venv").exists())

    def test_broken_environment_is_not_overwritten(self):
        (self.project / ".venv").mkdir()
        marker = self.project / ".venv" / "keep-me"
        marker.write_text("preserve")
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Remove or rename .venv", result.stderr)
        self.assertEqual(marker.read_text(), "preserve")

    def test_application_exit_code_is_preserved(self):
        result = self.run_launcher(TEST_APP_EXIT="23")
        self.assertEqual(result.returncode, 23)


if __name__ == "__main__":
    unittest.main()
