"""Shared pytest fixtures for korgalore tests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from korgalore.pi_feed import PIFeed
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
def sample_deliveries() -> dict[str, tuple[Any, Any, list[str]]]:
    """Create sample delivery data structure matching cli.py format.

    Returns dict mapping delivery_name -> (feed, target, labels)
    """
    feeds: dict[str, tuple[Any, Any, list[str]]] = {}
    for i in range(5):
        feed = MagicMock()
        feed.feed_key = f'feed-{i % 3}'  # 3 unique feeds
        target = MagicMock()
        target.identifier = f'target-{i % 2}'
        feeds[f'delivery-{i}'] = (feed, target, [f'label-{i}'])
    return feeds


@pytest.fixture
def repo(tmp_path: Path) -> InboxRepo:
    """An empty public-inbox style repository for digest tests."""
    from tests.digest_helpers import InboxRepo

    return InboxRepo(tmp_path / 'lkml')
