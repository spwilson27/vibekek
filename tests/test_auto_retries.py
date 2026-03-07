import sys
import os
import signal
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from workflow_lib.orchestrator import Orchestrator


def _make_orchestrator(auto_retries=None, max_retries=5):
    ctx = MagicMock()
    ctx._load_state.return_value = {}
    ctx.state = {}
    orc = Orchestrator(ctx, max_retries=max_retries, auto_retries=auto_retries)
    return orc


def _make_phase(side_effects):
    phase = MagicMock()
    phase.display_name = "TestPhase"
    phase.operation = "test"
    phase.execute.side_effect = side_effects
    return phase


class TestAutoRetriesOnException:
    def test_auto_retries_skips_prompt_on_exception(self):
        """With auto_retries=2, first 2 failures auto-retry without prompting."""
        orc = _make_orchestrator(auto_retries=2, max_retries=5)
        phase = _make_phase([Exception("e1"), Exception("e2"), None])

        with patch.object(orc, '_prompt') as mock_prompt:
            orc.run_phase_with_retry(phase)

        assert phase.execute.call_count == 3
        mock_prompt.assert_not_called()

    def test_auto_retries_prompts_after_exhausted(self):
        """After auto_retries are used up, falls back to prompting."""
        orc = _make_orchestrator(auto_retries=1, max_retries=5)
        # Fail 1 (auto), fail 2 (prompt), then succeed
        phase = _make_phase([Exception("e1"), Exception("e2"), None])

        with patch.object(orc, '_prompt', return_value='') as mock_prompt:
            orc.run_phase_with_retry(phase)

        assert phase.execute.call_count == 3
        mock_prompt.assert_called_once()

    def test_no_auto_retries_always_prompts(self):
        """With no auto_retries, every failure prompts immediately."""
        orc = _make_orchestrator(auto_retries=None, max_retries=3)
        phase = _make_phase([Exception("e1"), None])

        with patch.object(orc, '_prompt', return_value='') as mock_prompt:
            orc.run_phase_with_retry(phase)

        assert phase.execute.call_count == 2
        mock_prompt.assert_called_once()


class TestAutoRetriesOnSystemExit:
    def test_auto_retries_skips_prompt_on_systemexit(self):
        """SystemExit with non-zero code also uses auto-retries."""
        orc = _make_orchestrator(auto_retries=2, max_retries=5)
        phase = _make_phase([SystemExit(1), SystemExit(1), None])

        with patch.object(orc, '_prompt') as mock_prompt:
            orc.run_phase_with_retry(phase)

        assert phase.execute.call_count == 3
        mock_prompt.assert_not_called()

    def test_systemexit_0_succeeds_immediately(self):
        """SystemExit(0) is treated as success, no retry needed."""
        orc = _make_orchestrator(auto_retries=2, max_retries=5)
        phase = _make_phase([SystemExit(0)])

        orc.run_phase_with_retry(phase)
        assert phase.execute.call_count == 1


class TestAutoRetriesCounterResetsPerPhase:
    def test_counter_resets_between_phases(self):
        """auto_failures counter resets for each new phase call."""
        orc = _make_orchestrator(auto_retries=1, max_retries=5)

        # Phase A: 1 auto-retry then succeed
        phase_a = _make_phase([Exception("e1"), None])
        with patch.object(orc, '_prompt') as mock_prompt:
            orc.run_phase_with_retry(phase_a)
        mock_prompt.assert_not_called()

        # Phase B: should also get 1 auto-retry (counter reset)
        phase_b = _make_phase([Exception("e1"), None])
        with patch.object(orc, '_prompt') as mock_prompt:
            orc.run_phase_with_retry(phase_b)
        mock_prompt.assert_not_called()

    def test_all_retries_exhausted_exits(self):
        """When both auto and manual retries exhausted, exits."""
        orc = _make_orchestrator(auto_retries=1, max_retries=2)
        phase = _make_phase([Exception("e1"), Exception("e2")])

        with patch.object(orc, '_prompt', return_value='') as mock_prompt:
            with pytest.raises(SystemExit):
                orc.run_phase_with_retry(phase)

    def test_quit_during_prompt_after_auto_retries(self):
        """User can quit when prompted after auto-retries are exhausted."""
        orc = _make_orchestrator(auto_retries=1, max_retries=5)
        phase = _make_phase([Exception("e1"), Exception("e2")])

        with patch.object(orc, '_prompt', return_value='q'):
            with pytest.raises(SystemExit):
                orc.run_phase_with_retry(phase)

        assert phase.execute.call_count == 2

    def test_continue_during_prompt_after_auto_retries(self):
        """User can continue (skip) when prompted after auto-retries."""
        orc = _make_orchestrator(auto_retries=1, max_retries=5)
        phase = _make_phase([Exception("e1"), Exception("e2")])

        with patch.object(orc, '_prompt', return_value='c'):
            orc.run_phase_with_retry(phase)

        assert phase.execute.call_count == 2


class TestGracefulShutdown:
    def test_first_sigint_sets_shutdown_flag(self):
        """First Ctrl-C sets shutdown_requested, does not exit."""
        orc = _make_orchestrator()
        orc.install_signal_handler()
        try:
            assert not orc.shutdown_requested
            os.kill(os.getpid(), signal.SIGINT)
            assert orc.shutdown_requested
        finally:
            orc.restore_signal_handler()

    def test_shutdown_prevents_next_phase(self):
        """After shutdown_requested, run_phase_with_retry exits cleanly."""
        orc = _make_orchestrator()
        orc.shutdown_requested = True
        phase = _make_phase([None])

        with pytest.raises(SystemExit) as exc_info:
            orc.run_phase_with_retry(phase)

        assert exc_info.value.code == 0
        phase.execute.assert_not_called()

    def test_current_phase_completes_before_shutdown(self):
        """A running phase completes even after shutdown is requested."""
        orc = _make_orchestrator()

        def execute_and_set_shutdown(_ctx):
            orc.shutdown_requested = True

        phase_a = MagicMock()
        phase_a.display_name = "PhaseA"
        phase_a.operation = "test"
        phase_a.execute.side_effect = execute_and_set_shutdown

        phase_b = _make_phase([None])

        # Phase A runs and sets shutdown during execution
        orc.run_phase_with_retry(phase_a)
        phase_a.execute.assert_called_once()

        # Phase B should not run
        with pytest.raises(SystemExit) as exc_info:
            orc.run_phase_with_retry(phase_b)
        assert exc_info.value.code == 0
        phase_b.execute.assert_not_called()

    def test_signal_handler_restored(self):
        """restore_signal_handler puts back the previous handler."""
        orc = _make_orchestrator()
        prev = signal.getsignal(signal.SIGINT)
        orc.install_signal_handler()
        assert signal.getsignal(signal.SIGINT) != prev
        orc.restore_signal_handler()
        assert signal.getsignal(signal.SIGINT) == prev
