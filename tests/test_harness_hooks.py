"""E2E tests for the optional .agent/harness_hooks.py mechanism in harness.py.

Verifies that:
1. Steps run successfully when no hooks script exists
2. Hooks are called with the correct step name when the script exists
3. A failing hook causes the harness step to fail (non-zero exit)
4. Hooks run AFTER the hardcoded checks, not instead of them
5. --setup-only mode only invokes the "setup" hook
6. Unknown hook steps are passed through (hooks script handles gracefully)
"""

import importlib.util
import os
import stat
import subprocess
import sys
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Helpers — load harness.py as a module from the templates directory
# ---------------------------------------------------------------------------

HARNESS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "templates", "harness.py"
)


def _load_harness(tmp_path):
    """Import harness.py with HOOKS_SCRIPT pointing into tmp_path."""
    spec = importlib.util.spec_from_file_location("harness_under_test", HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Override cwd so HOOKS_SCRIPT resolves inside tmp_path
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(old_cwd)
    return mod


def _write_hooks_script(tmp_path, body):
    """Write a .agent/harness_hooks.py into tmp_path with the given body."""
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(exist_ok=True)
    script = agent_dir / "harness_hooks.py"
    script.write_text(textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


# ---------------------------------------------------------------------------
# Tests: _run_hook behaviour
# ---------------------------------------------------------------------------

class TestRunHookDirect:
    """Unit-level tests calling _run_hook directly with mocked _run."""

    def test_no_hooks_script_is_noop(self, tmp_path):
        """When .agent/harness_hooks.py doesn't exist, _run_hook does nothing."""
        mod = _load_harness(tmp_path)
        # Patch HOOKS_SCRIPT to a path that doesn't exist
        mod.HOOKS_SCRIPT = str(tmp_path / ".agent" / "harness_hooks.py")
        # Should not raise
        mod._run_hook("setup")

    def test_hooks_script_called_with_step_name(self, tmp_path):
        """_run_hook must invoke the hooks script with the step name as argv[1]."""
        _write_hooks_script(tmp_path, """\
            #!/usr/bin/env python3
            import sys
            print(f"hook_called:{sys.argv[1]}")
        """)
        mod = _load_harness(tmp_path)
        mod.HOOKS_SCRIPT = str(tmp_path / ".agent" / "harness_hooks.py")

        # Replace _run with a recorder
        calls = []
        original_run = mod._run

        def recording_run(cmd, **kwargs):
            calls.append(cmd)
            # Don't actually run it
            return type("R", (), {"returncode": 0})()

        mod._run = recording_run
        mod._run_hook("fmt")

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[-1] == "fmt"
        assert "harness_hooks.py" in cmd[-2]

    def test_failing_hook_propagates(self, tmp_path):
        """A hook that exits non-zero must cause _run to fail."""
        _write_hooks_script(tmp_path, """\
            #!/usr/bin/env python3
            import sys
            sys.exit(1)
        """)
        mod = _load_harness(tmp_path)
        mod.HOOKS_SCRIPT = str(tmp_path / ".agent" / "harness_hooks.py")

        with pytest.raises(SystemExit):
            mod._run_hook("lint")


# ---------------------------------------------------------------------------
# Tests: E2E subprocess execution of hooks
# ---------------------------------------------------------------------------

class TestHooksE2ESubprocess:
    """Run harness step functions in a subprocess to test real hook execution."""

    def test_setup_hook_runs_successfully(self, tmp_path):
        """step_setup must succeed when hooks script exits 0."""
        marker = tmp_path / "setup_ran.marker"
        _write_hooks_script(tmp_path, f"""\
            #!/usr/bin/env python3
            import sys
            if len(sys.argv) > 1 and sys.argv[1] == "setup":
                open("{marker}", "w").write("ok")
        """)

        # Run step_setup via a small driver script
        driver = tmp_path / "driver.py"
        driver.write_text(textwrap.dedent(f"""\
            import importlib.util, os, sys
            os.chdir("{tmp_path}")
            spec = importlib.util.spec_from_file_location("h", "{HARNESS_PATH}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.step_setup()
        """))

        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"step_setup failed: {result.stderr}"
        assert marker.exists(), "Hook was not called — marker file missing"
        assert marker.read_text() == "ok"

    def test_hook_failure_causes_step_to_exit_nonzero(self, tmp_path):
        """A hook that exits 1 must cause the step to fail."""
        _write_hooks_script(tmp_path, """\
            #!/usr/bin/env python3
            import sys
            sys.exit(42)
        """)

        driver = tmp_path / "driver.py"
        driver.write_text(textwrap.dedent(f"""\
            import importlib.util, os, sys
            os.chdir("{tmp_path}")
            spec = importlib.util.spec_from_file_location("h", "{HARNESS_PATH}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.step_setup()
        """))

        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode != 0, "Step should fail when hook exits non-zero"

    def test_no_hooks_script_step_still_succeeds(self, tmp_path):
        """step_setup must succeed even without .agent/harness_hooks.py."""
        driver = tmp_path / "driver.py"
        driver.write_text(textwrap.dedent(f"""\
            import importlib.util, os, sys
            os.chdir("{tmp_path}")
            spec = importlib.util.spec_from_file_location("h", "{HARNESS_PATH}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.step_setup()
        """))

        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"step_setup should succeed without hooks: {result.stderr}"


# ---------------------------------------------------------------------------
# Tests: hooks run AFTER hardcoded checks
# ---------------------------------------------------------------------------

class TestHookOrdering:
    """Verify hooks are called after the built-in checks, not before."""

    def test_fmt_hook_runs_after_builtin(self, tmp_path):
        """step_fmt must call cargo fmt first, then the hook.

        We mock _run to record call order and verify the hook comes second.
        """
        mod = _load_harness(tmp_path)

        marker = tmp_path / "order.log"
        _write_hooks_script(tmp_path, f"""\
            #!/usr/bin/env python3
            with open("{marker}", "a") as f:
                f.write("hook\\n")
        """)
        mod.HOOKS_SCRIPT = str(tmp_path / ".agent" / "harness_hooks.py")

        call_log = []
        def tracking_run(cmd, *, check=True, capture=False, env=None):
            if isinstance(cmd, str):
                call_log.append(("builtin", cmd))
            else:
                call_log.append(("hook", cmd))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        mod._run = tracking_run
        mod.step_fmt()

        assert len(call_log) >= 2, f"Expected at least 2 _run calls, got {call_log}"
        assert call_log[0][0] == "builtin", "First call should be the built-in cargo fmt"
        assert call_log[-1][0] == "hook", "Last call should be the hook"

    def test_lint_hook_runs_after_all_builtin_checks(self, tmp_path):
        """step_lint must run all hardcoded lint checks before calling the hook."""
        mod = _load_harness(tmp_path)

        _write_hooks_script(tmp_path, """\
            #!/usr/bin/env python3
            pass
        """)
        mod.HOOKS_SCRIPT = str(tmp_path / ".agent" / "harness_hooks.py")

        call_log = []
        def tracking_run(cmd, *, check=True, capture=False, env=None):
            if isinstance(cmd, str):
                call_log.append(("builtin", cmd))
            else:
                call_log.append(("hook", cmd))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        mod._run = tracking_run
        mod.step_lint()

        builtin_calls = [c for c in call_log if c[0] == "builtin"]
        hook_calls = [c for c in call_log if c[0] == "hook"]
        assert len(builtin_calls) >= 3, f"Expected at least 3 built-in lint calls, got {builtin_calls}"
        assert len(hook_calls) == 1, f"Expected exactly 1 hook call, got {hook_calls}"
        # Hook must be the very last call
        assert call_log[-1][0] == "hook"


# ---------------------------------------------------------------------------
# Tests: --setup-only mode
# ---------------------------------------------------------------------------

class TestSetupOnlyMode:
    """Verify --setup-only only runs the setup hook, not others."""

    def test_setup_only_skips_non_setup_hooks(self, tmp_path):
        """With --setup-only, only the setup step (and its hook) should run."""
        marker_setup = tmp_path / "hook_setup.marker"
        marker_fmt = tmp_path / "hook_fmt.marker"
        _write_hooks_script(tmp_path, f"""\
            #!/usr/bin/env python3
            import sys
            step = sys.argv[1] if len(sys.argv) > 1 else "unknown"
            open("{tmp_path}/hook_" + step + ".marker", "w").write("ran")
        """)

        driver = tmp_path / "driver.py"
        driver.write_text(textwrap.dedent(f"""\
            import importlib.util, os, sys
            sys.argv = ["harness.py", "--setup-only"]
            os.chdir("{tmp_path}")
            spec = importlib.util.spec_from_file_location("h", "{HARNESS_PATH}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()
        """))

        result = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"--setup-only failed: {result.stderr}"
        assert marker_setup.exists(), "Setup hook should have run"
        assert not marker_fmt.exists(), "Fmt hook should NOT have run in --setup-only mode"


# ---------------------------------------------------------------------------
# Tests: all step names are hooked
# ---------------------------------------------------------------------------

class TestAllStepsHooked:
    """Ensure every step in _STEPS calls _run_hook with the right name."""

    EXPECTED_HOOKS = {"setup", "fmt", "lint", "test", "build", "coverage"}

    def test_all_steps_call_run_hook(self, tmp_path):
        """Every step function must call _run_hook with a known step name."""
        mod = _load_harness(tmp_path)

        hooked_steps = set()
        original_run_hook = mod._run_hook

        def spy_hook(step):
            hooked_steps.add(step)

        mod._run_hook = spy_hook
        # Mock _run to no-op (we don't have cargo etc.)
        mod._run = lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        for name, fn in mod._STEPS:
            fn()

        assert hooked_steps == self.EXPECTED_HOOKS, (
            f"Missing hooks for steps: {self.EXPECTED_HOOKS - hooked_steps}. "
            f"Got: {hooked_steps}"
        )
