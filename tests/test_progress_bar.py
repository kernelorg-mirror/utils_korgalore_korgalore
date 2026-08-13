"""Tests for progress bar suppression across Click versions.

korgalore hides its progress bars by handing Click a throwaway stream rather
than by passing ``hidden=True``, which only exists in Click 8.3.0 and newer.
These tests pin the behaviour we rely on, so that a Click release changing how
a non-terminal output stream is treated fails here instead of dumping bar
noise into somebody's debug log.
"""

import io

import click
import pytest

from korgalore.cli import progress_file


def test_progress_file_visible() -> None:
    # None means "use Click's default", i.e. stdout.
    assert progress_file(False) is None


def test_progress_file_hidden() -> None:
    stream = progress_file(True)
    assert isinstance(stream, io.StringIO)
    # A fresh stream every call: two concurrent bars must not share one.
    assert progress_file(True) is not stream


def test_hidden_bar_writes_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    items = list(range(5))
    seen = []
    with click.progressbar(items, label='Hiding', show_pos=True, file=progress_file(True)) as bar:
        for item in bar:
            seen.append(item)

    # The iteration must still work -- hiding the bar is not skipping the work.
    assert seen == items
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ''


def test_hidden_bar_swallows_the_label() -> None:
    # Click's non-terminal path echoes the label once. Directing it at our own
    # stream is what keeps it off the user's terminal, so check it landed here.
    stream = io.StringIO()
    with click.progressbar(range(3), label='Hiding', file=stream) as bar:
        for _ in bar:
            pass

    assert 'Hiding' in stream.getvalue()
