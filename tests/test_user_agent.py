"""Tests for User-Agent handling across korgalore."""

import os
from typing import List, Optional
from unittest import mock
from unittest.mock import MagicMock

import pytest

import korgalore
from korgalore import (
    GitError,
    PublicInboxError,
    __version__,
    _init_git_user_agent,
    close_requests_session,
    get_requests_session,
    make_lore_node,
    run_lei_command,
)


class TestGetRequestsSession:
    """Tests for get_requests_session function."""

    def teardown_method(self) -> None:
        """Clean up session after each test."""
        close_requests_session()

    def test_returns_same_instance(self) -> None:
        """Repeated calls return the same session instance."""
        session1 = get_requests_session()
        session2 = get_requests_session()
        assert session1 is session2

    def test_session_user_agent_excludes_plus(self) -> None:
        """Session User-Agent is korgalore/version, without _user_agent_plus (no leakage to JMAP etc.)."""
        korgalore._user_agent_plus = 'should-not-appear'
        try:
            session = get_requests_session()
            assert '+should-not-appear' not in str(session.headers['User-Agent'])
            assert session.headers['User-Agent'] == f'korgalore/{__version__}'
        finally:
            korgalore._user_agent_plus = None


class TestCloseRequestsSession:
    """Tests for close_requests_session function."""

    def teardown_method(self) -> None:
        """Ensure session is closed after each test."""
        close_requests_session()

    def test_clears_global_session(self) -> None:
        """Closing session clears the global reference."""
        get_requests_session()
        assert korgalore._REQSESSION is not None
        close_requests_session()
        assert korgalore._REQSESSION is None

    def test_new_session_after_close(self) -> None:
        """New session is created after closing."""
        session1 = get_requests_session()
        close_requests_session()
        session2 = get_requests_session()
        assert session1 is not session2

    def test_close_without_session_is_safe(self) -> None:
        """Closing when no session exists does not raise."""
        close_requests_session()
        close_requests_session()  # Should not raise


class TestInitGitUserAgent:
    """Tests for _init_git_user_agent function."""

    def teardown_method(self) -> None:
        """Clean up environment after each test."""
        if 'GIT_HTTP_USER_AGENT' in os.environ:
            del os.environ['GIT_HTTP_USER_AGENT']
        korgalore._user_agent_plus = None

    @pytest.mark.parametrize(
        ('plus', 'suffix'),
        [(None, ''), ('testid', '+testid')],
        ids=['no-plus', 'with-plus'],
    )
    def test_sets_environment_variable(self, plus: Optional[str], suffix: str) -> None:
        """GIT_HTTP_USER_AGENT is git/{version} (korgalore/{version}[+plus])."""
        korgalore._user_agent_plus = plus
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'git version 2.45.0', stderr=b'')
            _init_git_user_agent()
            assert os.environ['GIT_HTTP_USER_AGENT'] == f'git/2.45.0 (korgalore/{__version__}{suffix})'

    def test_raises_git_error_if_not_found(self) -> None:
        """Raises GitError if git command not found."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(GitError) as exc_info:
                _init_git_user_agent()
            assert 'not found' in str(exc_info.value).lower()

    def test_raises_git_error_on_nonzero_return(self) -> None:
        """Raises GitError if git returns non-zero."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout=b'', stderr=b'git: error message')
            with pytest.raises(GitError) as exc_info:
                _init_git_user_agent()
            assert 'error message' in str(exc_info.value)


class TestRunLeiCommand:
    """Tests for run_lei_command function."""

    def teardown_method(self) -> None:
        """Reset user agent plus after each test."""
        korgalore._user_agent_plus = None

    @pytest.mark.parametrize(
        ('args', 'expects_ua'),
        [
            (['q', 'term', '--threads'], True),
            (['up', '/path/to/search'], True),
            (['ls-search', '-l'], False),
            (['forget-search', '/path'], False),
        ],
        ids=['q', 'up', 'ls-search', 'forget-search'],
    )
    def test_user_agent_flag_by_subcommand(self, args: List[str], expects_ua: bool) -> None:
        """--user-agent follows the subcommand for q/up, and is absent for the rest."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'')
            run_lei_command(args)

            called_cmd = mock_run.call_args[0][0]
            if expects_ua:
                # Should be: lei q --user-agent <ua> term --threads
                assert called_cmd[:4] == ['lei', args[0], '--user-agent', f'korgalore/{__version__}']
                assert called_cmd[4:] == args[1:]
            else:
                assert '--user-agent' not in called_cmd

    def test_includes_user_agent_plus(self) -> None:
        """Lei user-agent includes plus from _user_agent_plus."""
        korgalore._user_agent_plus = 'myid'
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'')
            run_lei_command(['q', 'term'])

            called_cmd = mock_run.call_args[0][0]
            ua_index = called_cmd.index('--user-agent')
            assert called_cmd[ua_index + 1] == f'korgalore/{__version__}+myid'

    def test_raises_public_inbox_error_if_not_found(self) -> None:
        """Raises PublicInboxError if lei command not found."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError()
            with pytest.raises(PublicInboxError) as exc_info:
                run_lei_command(['q', 'term'])
            assert 'not found' in str(exc_info.value).lower()

    def test_returns_returncode_and_stdout(self) -> None:
        """Returns tuple of (returncode, stdout)."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'output data')
            retcode, output = run_lei_command(['ls-search'])
            assert retcode == 0
            assert output == b'output data'

    def test_passes_stdin_to_command(self) -> None:
        """Data given as stdin reaches the lei process, for 'lei q --stdin'."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'')
            run_lei_command(['q', '--stdin'], stdin=b'l:foo AND d:2.days.ago..')

            assert mock_run.call_args.kwargs['input'] == b'l:foo AND d:2.days.ago..'

    def test_no_stdin_by_default(self) -> None:
        """Without stdin, lei gets no input, as before."""
        with mock.patch('subprocess.run') as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=b'')
            run_lei_command(['up', '/path'])

            assert mock_run.call_args.kwargs['input'] is None


class TestMakeLoreNode:
    """Tests for make_lore_node factory function."""

    def test_defaults_and_user_agent(self) -> None:
        """Defaults to lore.kernel.org/all without a cache, and sets the user agent."""
        mock_node = MagicMock()
        with mock.patch('korgalore.LoreNode.from_git_config', return_value=mock_node) as mock_fgc:
            node = make_lore_node()
            mock_fgc.assert_called_once_with('https://lore.kernel.org/all', cache_dir=None)
            mock_node.set_user_agent.assert_called_once_with('korgalore', __version__)
            assert node is mock_node
