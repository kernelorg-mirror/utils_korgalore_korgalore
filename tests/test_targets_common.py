"""Behaviour shared by the simple local targets: dummy, pipe and maildir."""

from pathlib import Path
from typing import Callable, Union

import pytest

from korgalore.dummy_target import DummyTarget
from korgalore.maildir_target import MaildirTarget
from korgalore.pipe_target import PipeTarget

SimpleTarget = Union[DummyTarget, PipeTarget, MaildirTarget]
Factory = Callable[[Path], SimpleTarget]


def make_dummy(tmp_path: Path) -> SimpleTarget:
    return DummyTarget('test-id')


def make_pipe(tmp_path: Path) -> SimpleTarget:
    # `true` ignores both its input and any label arguments
    return PipeTarget('test-id', 'true')


def make_maildir(tmp_path: Path) -> SimpleTarget:
    return MaildirTarget('test-id', str(tmp_path / 'mail'))


@pytest.fixture(params=[make_dummy, make_pipe, make_maildir], ids=['dummy', 'pipe', 'maildir'])
def target(request: pytest.FixtureRequest, tmp_path: Path) -> SimpleTarget:
    factory: Factory = request.param
    return factory(tmp_path)


def test_identifier_stored(target: SimpleTarget) -> None:
    assert target.identifier == 'test-id'


def test_connect_does_not_raise(target: SimpleTarget) -> None:
    target.connect()


def test_import_message_accepts_labels(target: SimpleTarget) -> None:
    """Labels and delivery context are accepted and have no effect on delivery."""
    target.connect()
    target.import_message(
        b'From: test@example.com\n\nBody',
        ['some-label'],
        feed_name='feed',
        delivery_name='delivery',
        subfolder=None,
    )


def test_dummy_import_discards_and_returns_none() -> None:
    result = DummyTarget('test').import_message(
        raw_message=b'From: test@example.com\n\nBody',
        labels=['some-label'],
        feed_name='feed',
        delivery_name='delivery',
        subfolder='202609',
    )
    assert result is None
