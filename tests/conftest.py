"""Shared pytest fixtures for korgalore tests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from korgalore.maildir_target import MaildirTarget
    from korgalore.pi_feed import PIFeed
    from korgalore.summarizer import SummaryCache
    from tests.digest_helpers import InboxRepo


@pytest.fixture(autouse=True)
def korgalore_logs_reach_caplog() -> Iterator[None]:
    """Let caplog see what korgalore logs.

    Importing cli.py runs click_log.basic_config(), which puts click-log's
    own handler on the korgalore logger and turns propagation off. Before
    pytest 9, caplog only listens on the root logger, so on the pytest
    that distros ship those records never reach it and caplog.text stays
    empty. Propagate for the length of the test, then put it back.
    """
    korg_logger = logging.getLogger('korgalore')
    saved = korg_logger.propagate
    korg_logger.propagate = True
    yield
    korg_logger.propagate = saved


@pytest.fixture(autouse=True)
def no_archive_lookups() -> Iterator[MagicMock]:
    """Tests never fetch messages from the real archive.

    A test that wants a fetch to work sets up the mock it gets from this
    fixture.
    """
    from korgalore.lore_feed import LoreFeed

    fetch = MagicMock(side_effect=AssertionError('no network in tests'))
    fetch.unpatched = LoreFeed.get_message_by_msgid
    with patch.object(LoreFeed, 'get_message_by_msgid', fetch):
        yield fetch


@pytest.fixture
def real_get_message(no_archive_lookups: MagicMock) -> Any:
    """The real LoreFeed.get_message_by_msgid, for the tests of that method."""
    return no_archive_lookups.unpatched


@pytest.fixture
def temp_feed_dir(tmp_path: Path) -> Path:
    """Create a temporary feed directory structure."""
    feed_dir = tmp_path / 'test-feed'
    feed_dir.mkdir()
    git_dir = feed_dir / 'git' / '0.git'
    git_dir.mkdir(parents=True)
    return feed_dir


@pytest.fixture
def mock_feed(temp_feed_dir: Path) -> 'PIFeed':
    """Create a PIFeed instance with mocked git operations."""
    from korgalore.pi_feed import PIFeed

    class TestPIFeed(PIFeed):
        """PIFeed subclass for testing that doesn't require real git repos."""

        def __init__(self, feed_dir: Path) -> None:
            super().__init__(feed_key='test-feed', feed_dir=feed_dir)
            self.feed_type = 'test'

        def get_subject_at_commit(self, epoch: int, commitish: str) -> str:
            """Mock implementation that returns a test subject."""
            return f'Test subject for {commitish}'

        def get_highest_epoch(self) -> int:
            """Mock implementation."""
            return 0

        def get_top_commit(self, epoch: int) -> str:
            """Mock implementation."""
            return 'abc123'

    return TestPIFeed(temp_feed_dir)


@pytest.fixture
def repo(tmp_path: Path) -> InboxRepo:
    """An empty public-inbox style repository for digest tests."""
    from tests.digest_helpers import InboxRepo

    return InboxRepo(tmp_path / 'lkml')


@pytest.fixture
def maildir(tmp_path: Path) -> MaildirTarget:
    from korgalore.maildir_target import MaildirTarget

    return MaildirTarget('local', str(tmp_path / 'mail'))


@pytest.fixture
def cache(tmp_path: Path) -> SummaryCache:
    from korgalore.summarizer import SummaryCache

    return SummaryCache(tmp_path / 'summaries')
