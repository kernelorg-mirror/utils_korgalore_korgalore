"""Tests for the progress heartbeat around long-running lei commands."""

import logging
import threading
from typing import Set
from unittest import mock

import pytest

import korgalore
from korgalore import PublicInboxError, _report_still_running, run_lei_command


@pytest.fixture
def korgalore_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture korgalore's INFO records."""
    caplog.set_level(logging.INFO, logger='korgalore')
    return caplog


def heartbeat_threads() -> Set[threading.Thread]:
    """Every heartbeat thread currently alive."""
    return {t for t in threading.enumerate() if t.name == 'lei-heartbeat'}


def wait_for_no_heartbeat(timeout: float = 5.0) -> None:
    """Wait for heartbeat threads to finish unwinding.

    run_lei_command only sets the event the threads wait on, so one can
    still be on its way out for an instant after the call returns.
    """
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if not heartbeat_threads():
            return
        deadline.wait(0.01)
    assert not heartbeat_threads(), 'heartbeat thread outlived the command'


class TestReportStillRunning:
    """Tests for the _report_still_running helper."""

    def test_reports_until_finished(self, korgalore_logs: pytest.LogCaptureFixture) -> None:
        """Keeps reporting for as long as the command runs."""
        finished = threading.Event()
        thread = threading.Thread(target=_report_still_running, args=('q', finished, 0.01))
        thread.start()
        try:
            # Several intervals' worth of waiting, then stop it.
            finished.wait(0.1)
        finally:
            finished.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        messages = [r.getMessage() for r in korgalore_logs.records]
        assert messages, 'expected at least one progress report'
        assert all('Still running lei q' in m for m in messages)

    def test_silent_when_command_is_quick(self, korgalore_logs: pytest.LogCaptureFixture) -> None:
        """Says nothing when the command finishes inside the first interval.

        Most lei calls are fast, and a heartbeat that fired for those would
        be noise in every log rather than a sign that something slow is
        underway.
        """
        finished = threading.Event()
        finished.set()

        _report_still_running('up', finished, 30.0)

        assert korgalore_logs.records == []

    def test_returns_promptly_when_finished(self) -> None:
        """Waits on the event rather than sleeping out the interval."""
        finished = threading.Event()
        thread = threading.Thread(target=_report_still_running, args=('q', finished, 30.0))
        thread.start()
        finished.set()
        thread.join(timeout=5)

        assert not thread.is_alive()


class TestRunLeiCommandHeartbeat:
    """Tests for how run_lei_command drives the heartbeat."""

    def teardown_method(self) -> None:
        """Reset user agent plus after each test."""
        korgalore._user_agent_plus = None

    def test_stops_heartbeat_when_command_returns(self) -> None:
        """The heartbeat thread is not left running after the command."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'out')
            assert run_lei_command(['q', 'term']) == (0, b'out')

        wait_for_no_heartbeat()

    def test_reports_during_a_slow_command(self, korgalore_logs: pytest.LogCaptureFixture) -> None:
        """A command that outlives the interval gets reported on.

        End to end through a real subprocess: 'lei' here is /bin/sleep, so
        the command genuinely takes longer than the interval.
        """
        with mock.patch.object(korgalore, 'LEICMD', 'sleep'):
            with mock.patch.object(korgalore, 'LEI_HEARTBEAT_INTERVAL', 0.05):
                returncode, _ = run_lei_command(['0.3'])

        assert returncode == 0
        assert any('Still running lei 0.3' in r.getMessage() for r in korgalore_logs.records)
        wait_for_no_heartbeat()

    def test_uses_the_interval_set_at_call_time(self) -> None:
        """Reads LEI_HEARTBEAT_INTERVAL when the command runs.

        Regression test: as a default argument on _report_still_running the
        interval was bound once at import, so adjusting the module value
        afterwards made no difference at all.
        """
        with mock.patch('threading.Thread') as mock_thread:
            with mock.patch('subprocess.run') as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout=b'')
                with mock.patch.object(korgalore, 'LEI_HEARTBEAT_INTERVAL', 0.25):
                    run_lei_command(['q', 'term'])

        assert mock_thread.call_args.kwargs['args'][2] == 0.25

    def test_stops_heartbeat_when_command_is_missing(self) -> None:
        """A lei that isn't installed stops the heartbeat too."""
        with mock.patch('subprocess.run', side_effect=FileNotFoundError):
            with pytest.raises(PublicInboxError):
                run_lei_command(['q', 'term'])

        wait_for_no_heartbeat()
