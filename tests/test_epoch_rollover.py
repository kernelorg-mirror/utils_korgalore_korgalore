"""Tests for epoch rollover detection in PIFeed.

Epochs are separate git repositories in public-inbox feeds. When an epoch
fills up, a new one is created (e.g., 0.git -> 1.git). These tests verify
that the feed correctly detects rollover and retrieves commits from both
the old and new epochs.
"""

import json
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from korgalore import GitError, PublicInboxError
from korgalore.pi_feed import PIFeed
from tests.conftest import make_pi_feed


def create_feed_with_epochs(tmp_path: Path, epochs: List[int]) -> PIFeed:
    """Create a feed with the specified epoch directories.

    Epochs are found on disk; the git lookups are left for the test to mock.
    """
    feed_dir = tmp_path / 'test-feed'
    feed_dir.mkdir()
    for epoch in epochs:
        (feed_dir / 'git' / f'{epoch}.git').mkdir(parents=True)
    return make_pi_feed(feed_dir, highest_epoch=None, top_commit=None, default_branch='master')


def write_delivery_info(feed: PIFeed, delivery_name: str, epochs_data: dict[int, dict[str, str]]) -> None:
    """Write delivery info state file."""
    state_file = feed.feed_dir / f'korgalore.{delivery_name}.info'
    state: dict[str, dict[str, dict[str, str]]] = {'epochs': {}}
    for epoch_num, data in epochs_data.items():
        state['epochs'][str(epoch_num)] = {
            'last': data.get('last', 'dummy_commit'),
            'commit_date': data.get('commit_date', '2024-01-01 00:00:00 +0000'),
            'subject': data.get('subject', 'Test subject'),
            'msgid': data.get('msgid', '<test@example.com>'),
        }
    state_file.write_text(json.dumps(state, indent=2))


class TestFindEpochs:
    """Tests for epoch discovery."""

    @pytest.mark.parametrize(
        ('on_disk', 'extra_dirs', 'expected'),
        [
            pytest.param([0], [], [0], id='single'),
            pytest.param([2, 0, 1], [], [0, 1, 2], id='sorted'),
            pytest.param([0, 2, 5], [], [0, 2, 5], id='non-contiguous'),
            pytest.param([0, 1], ['not_an_epoch.git', 'random_dir'], [0, 1], id='ignores-non-epoch-dirs'),
        ],
    )
    def test_finds_epochs(self, tmp_path: Path, on_disk: List[int], extra_dirs: List[str], expected: List[int]) -> None:
        feed = create_feed_with_epochs(tmp_path, on_disk)
        for name in extra_dirs:
            (feed.feed_dir / 'git' / name).mkdir()

        assert feed.find_epochs() == expected

    @pytest.mark.parametrize('git_dir_exists', [True, False], ids=['empty-git-dir', 'missing-git-dir'])
    def test_no_epochs_raises(self, tmp_path: Path, git_dir_exists: bool) -> None:
        """No epoch directories, or no git directory at all, raises PublicInboxError."""
        feed_dir = tmp_path / 'test-feed'
        feed_dir.mkdir()
        if git_dir_exists:
            (feed_dir / 'git').mkdir()
        feed = make_pi_feed(feed_dir, highest_epoch=None, top_commit=None)

        with pytest.raises(PublicInboxError) as exc_info:
            feed.find_epochs()
        assert 'No existing epochs' in str(exc_info.value)


class TestGetHighestEpoch:
    """Tests for highest epoch detection."""

    @pytest.mark.parametrize(
        ('on_disk', 'expected'),
        [
            pytest.param([0], 0, id='single'),
            pytest.param([0, 1, 2], 2, id='multiple'),
            pytest.param([0, 5, 10], 10, id='non-contiguous'),
        ],
    )
    def test_highest_epoch(self, tmp_path: Path, on_disk: List[int], expected: int) -> None:
        assert create_feed_with_epochs(tmp_path, on_disk).get_highest_epoch() == expected


class TestGetAllCommitsInEpoch:
    """Tests for retrieving all commits in an epoch."""

    @pytest.mark.parametrize(
        ('stdout', 'expected'),
        [
            pytest.param(b'aaa111\nbbb222\nccc333', ['aaa111', 'bbb222', 'ccc333'], id='in-order'),
            pytest.param(b'', [], id='empty-epoch'),
        ],
    )
    @patch('korgalore.pi_feed.run_git_command')
    def test_returns_commits(self, mock_git: MagicMock, tmp_path: Path, stdout: bytes, expected: List[str]) -> None:
        """Commits are returned in chronological order."""
        feed = create_feed_with_epochs(tmp_path, [0])
        mock_git.return_value = (0, stdout, b'')

        assert feed.get_all_commits_in_epoch(0) == expected
        # Verify rev-list was called with --reverse
        assert '--reverse' in mock_git.call_args[0][1]

    @patch('korgalore.pi_feed.run_git_command')
    def test_git_error_raises(self, mock_git: MagicMock, tmp_path: Path) -> None:
        """Git error raises GitError."""
        feed = create_feed_with_epochs(tmp_path, [0])
        mock_git.return_value = (1, b'', b'fatal: bad revision')

        with pytest.raises(GitError):
            feed.get_all_commits_in_epoch(0)


class TestEpochRolloverDetection:
    """Tests for epoch rollover detection in get_latest_commits_for_delivery.

    The git calls always come in the same order: one cat-file -e to check
    that the delivery's last commit still exists (in the highest known
    epoch), then one rev-list for the new commits of that epoch, then, if a
    newer epoch exists on disk, one rev-list for the highest epoch. Only the
    highest epoch is read; epochs in between are skipped.
    """

    @pytest.mark.parametrize(
        ('on_disk', 'known', 'git_output', 'expected'),
        [
            # cat-file -e, rev-list epoch 0 (new commits)
            pytest.param(
                [0],
                {0: 'aaa111'},
                [b'', b'bbb222\nccc333\nddd444'],
                [(0, 'bbb222'), (0, 'ccc333'), (0, 'ddd444')],
                id='no-rollover',
            ),
            # cat-file -e, rev-list epoch 0 (nothing new)
            pytest.param([0], {0: 'aaa111'}, [b'', b''], [], id='no-rollover-no-new-commits'),
            # cat-file -e, rev-list epoch 0 (one new), rev-list epoch 1 (all commits)
            pytest.param(
                [0, 1],
                {0: 'aaa111'},
                [b'', b'bbb222', b'xxx111\nyyy222\nzzz333'],
                [(0, 'bbb222'), (1, 'xxx111'), (1, 'yyy222'), (1, 'zzz333')],
                id='rollover-both-epochs',
            ),
            # cat-file -e, rev-list epoch 0 (nothing new), rev-list epoch 1
            pytest.param(
                [0, 1],
                {0: 'aaa111'},
                [b'', b'', b'xxx111\nyyy222'],
                [(1, 'xxx111'), (1, 'yyy222')],
                id='rollover-nothing-new-in-old-epoch',
            ),
            # cat-file -e, rev-list epoch 0 (one new), rev-list epoch 1 (empty)
            pytest.param([0, 1], {0: 'aaa111'}, [b'', b'bbb222', b''], [(0, 'bbb222')], id='rollover-empty-new-epoch'),
            # Epoch 1 is missing and 2 is never read: cat-file -e, rev-list
            # epoch 0, rev-list epoch 3 (the highest)
            pytest.param(
                [0, 1, 3],
                {0: 'aaa111'},
                [b'', b'bbb222', b'new_commit'],
                [(0, 'bbb222'), (3, 'new_commit')],
                id='skipped-and-intermediate-epochs',
            ),
            # Same with nothing between the known epoch and the highest one:
            # cat-file -e, rev-list epoch 0, rev-list epoch 2
            pytest.param(
                [0, 2],
                {0: 'aaa111'},
                [b'', b'bbb222', b'new_commit'],
                [(0, 'bbb222'), (2, 'new_commit')],
                id='skipped-epoch',
            ),
            # The delivery already knows epoch 2, so epoch 2 is the only one
            # read, even though 0 and 1 exist: cat-file -e (epoch 2), rev-list
            # epoch 2
            pytest.param(
                [0, 1, 2],
                {0: 'epoch0_commit', 1: 'epoch1_commit', 2: 'aaa111'},
                [b'', b'bbb222\nccc333'],
                [(2, 'bbb222'), (2, 'ccc333')],
                id='already-on-latest-epoch',
            ),
            # Same call order as rollover-both-epochs: cat-file -e, rev-list
            # epoch 99, rev-list epoch 100
            pytest.param(
                [99, 100],
                {99: 'aaa111'},
                [b'', b'bbb222', b'xxx111'],
                [(99, 'bbb222'), (100, 'xxx111')],
                id='high-epoch-numbers',
            ),
        ],
    )
    @patch('korgalore.pi_feed.run_git_command')
    def test_latest_commits(
        self,
        mock_git: MagicMock,
        tmp_path: Path,
        on_disk: List[int],
        known: Dict[int, str],
        git_output: List[bytes],
        expected: List[Tuple[int, str]],
    ) -> None:
        feed = create_feed_with_epochs(tmp_path, on_disk)
        write_delivery_info(feed, 'delivery1', {epoch: {'last': last} for epoch, last in known.items()})
        mock_git.side_effect = [(0, out, b'') for out in git_output]

        assert feed.get_latest_commits_for_delivery('delivery1') == expected
        # No git call was left unused
        assert mock_git.call_count == len(git_output)


class TestEpochRolloverEdgeCases:
    """Edge case tests for epoch rollover."""

    @patch('korgalore.pi_feed.run_git_command')
    def test_commit_not_found_triggers_recovery(self, mock_git: MagicMock, tmp_path: Path) -> None:
        """Invalid commit triggers rebase recovery."""
        feed = create_feed_with_epochs(tmp_path, [0])
        write_delivery_info(
            feed,
            'delivery1',
            {
                0: {
                    'last': 'invalid_commit',
                    'commit_date': '2024-01-01 00:00:00 +0000',
                    'subject': 'Test subject',
                    'msgid': '<test@example.com>',
                }
            },
        )

        # Simulate commit not found, then recovery process
        mock_git.side_effect = [
            (1, b'', b''),  # cat-file -e fails (commit not found)
            (0, b'recovered_commit', b''),  # rev-list --since-as-filter finds commits
            # get_message_at_commit for matching
            (0, b'From: test@example.com\nSubject: Test subject\nMessage-ID: <test@example.com>\n\nBody', b''),
            # save_delivery_info calls
            (0, b'2024-01-01 00:00:00 +0000', b''),  # git show commit date
            (0, b'new_commit1\nnew_commit2', b''),  # rev-list from recovered commit
        ]

        result = feed.get_latest_commits_for_delivery('delivery1')

        assert len(result) == 2
        assert result[0] == (0, 'new_commit1')
