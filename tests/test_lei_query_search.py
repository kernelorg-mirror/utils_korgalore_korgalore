"""Tests for create_lei_query_search.

Newer public-inbox turns every `lei q' argument that contains whitespace
into a phrase search. A whole query such as
`l:linux-doc.vger.kernel.org AND d:30.days.ago..' passed as one argument
becomes `l:"linux-doc.vger.kernel.org AND d:30.days.ago.."' and matches
nothing, without any error. These tests make sure the query goes to lei
on stdin instead, where lei uses it as written.
"""

from pathlib import Path
from typing import List, Optional, Tuple
from unittest import mock

import pytest

from korgalore.tracking import create_lei_query_search

QUERY = '(dfn:Documentation/ OR dfn:Documentation/process/) AND d:30.days.ago..'


def run_search(tmp_path: Path, threads: bool = False) -> Tuple[List[str], Optional[bytes]]:
    """Run create_lei_query_search and return the lei args and stdin it used."""
    with mock.patch('korgalore.tracking.run_lei_command', return_value=(0, b'')) as mock_lei:
        create_lei_query_search(QUERY, tmp_path / 'lei' / 'search', threads=threads)
    mock_lei.assert_called_once()
    args: List[str] = mock_lei.call_args.args[0]
    stdin: Optional[bytes] = mock_lei.call_args.kwargs.get('stdin')
    return args, stdin


class TestCreateLeiQuerySearch:
    """Handing lei a query it does not re-quote."""

    def test_query_goes_to_stdin_only(self, tmp_path: Path) -> None:
        args, stdin = run_search(tmp_path)

        assert args[:2] == ['q', '--stdin']
        assert stdin == QUERY.encode()
        # lei refuses a command-line query together with --stdin
        assert not any(QUERY in arg or 'dfn:' in arg for arg in args)
        # Any argument with a space would be at risk of phrase quoting
        assert all(' ' not in arg for arg in args)

    def test_output_and_source(self, tmp_path: Path) -> None:
        output = tmp_path / 'lei' / 'search'
        args, _ = run_search(tmp_path)

        assert args[args.index('--only') + 1] == 'https://lore.kernel.org/all'
        assert args[args.index('-o') + 1] == f'v2:{output}'

    @pytest.mark.parametrize('threads', [True, False], ids=['with-threads', 'without-threads'])
    def test_threads_flag(self, tmp_path: Path, threads: bool) -> None:
        assert ('--threads' in run_search(tmp_path, threads=threads)[0]) is threads

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        run_search(tmp_path)

        assert (tmp_path / 'lei').is_dir()
