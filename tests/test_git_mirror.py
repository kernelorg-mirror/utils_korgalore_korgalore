"""Tests for git mirror failover via url.insteadOf."""

import json
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from korgalore import run_git_command
from korgalore.lore_feed import LoreFeed
from tests.feed_helpers import manifest_response

MIRROR = 'https://tor.lore.kernel.org'
CANONICAL = 'https://lore.kernel.org'


class TestRunGitCommandConfig:
    """Tests for the git_config parameter in run_git_command."""

    @pytest.mark.parametrize(
        ('git_dir', 'args', 'git_config', 'expected'),
        [
            pytest.param(None, ['status'], None, ['git', 'status'], id='none'),
            pytest.param(None, ['status'], {}, ['git', 'status'], id='empty'),
            # -c key=value goes before --git-dir and the subcommand
            pytest.param(
                '/some/dir',
                ['fetch', 'origin'],
                {'url.https://mirror/.insteadOf': 'https://canonical/'},
                [
                    'git',
                    '-c',
                    'url.https://mirror/.insteadOf=https://canonical/',
                    '--git-dir',
                    '/some/dir',
                    'fetch',
                    'origin',
                ],
                id='single',
            ),
            # One -c per entry, all before the subcommand
            pytest.param(
                None,
                ['clone', 'url'],
                {'key1': 'val1', 'key2': 'val2'},
                ['git', '-c', 'key1=val1', '-c', 'key2=val2', 'clone', 'url'],
                id='multiple',
            ),
        ],
    )
    def test_command_line(
        self, git_dir: Optional[str], args: list[str], git_config: Optional[Dict[str, str]], expected: list[str]
    ) -> None:
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b'', stderr=b'')
            run_git_command(git_dir, args, git_config=git_config)

        assert mock_run.call_args[0][0] == expected


class TestGitMirrorConfig:
    """Tests for LoreFeed._git_mirror_config()."""

    @pytest.mark.parametrize(
        ('origins', 'expected'),
        [
            pytest.param(
                [MIRROR, CANONICAL],
                {f'url.{MIRROR}/.insteadOf': f'{CANONICAL}/'},
                id='fastest-is-a-mirror',
            ),
            pytest.param([CANONICAL, MIRROR], {}, id='fastest-is-canonical'),
            pytest.param([CANONICAL], {}, id='single-origin'),
            pytest.param([], {}, id='no-origins'),
        ],
    )
    def test_mirror_config(self, origins: list[str], expected: Dict[str, str]) -> None:
        mock_node = MagicMock()
        mock_node.origins = origins
        mock_node.canonical_origin = CANONICAL
        feed = LoreFeed.__new__(LoreFeed)
        feed._node = mock_node

        assert feed._git_mirror_config() == expected


class TestCloneEpochMirror:
    """Tests for mirror config being passed to clone_epoch git commands."""

    @pytest.mark.parametrize(
        ('origins', 'expected_config'),
        [
            pytest.param(
                ['https://sea.lore.kernel.org', CANONICAL],
                {'url.https://sea.lore.kernel.org/.insteadOf': f'{CANONICAL}/'},
                id='mirror-is-fastest',
            ),
            pytest.param([CANONICAL, MIRROR], {}, id='canonical-is-fastest'),
        ],
    )
    def test_clone_fallback_also_gets_mirror_config(
        self, tmp_path: Path, origins: list[str], expected_config: Dict[str, str]
    ) -> None:
        """A shallow clone that fails and retries with --depth=1 reuses the mirror config."""
        mock_node = MagicMock()
        mock_node.origins = origins
        mock_node.canonical_origin = CANONICAL

        feed_dir = tmp_path / 'test-feed'
        feed_dir.mkdir()
        feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        with patch('korgalore.lore_feed.run_git_command') as mock_git:
            # First call (shallow) fails, second call (--depth=1) succeeds
            mock_git.side_effect = [(128, b'', b'shallow error'), (0, b'', b'')]
            feed.clone_epoch(0, shallow=True)
            assert mock_git.call_count == 2
            # Both calls should have the mirror config
            for c in mock_git.call_args_list:
                assert c[1]['git_config'] == expected_config


class TestUpdateFeedMirror:
    """Tests for mirror config being passed to update_feed git fetch."""

    def test_fetch_fallback_to_depth_one_on_shallow_failure(self, tmp_path: Path) -> None:
        """When --shallow-since fetch fails (dormant list), retry with --depth=1."""
        mock_node = MagicMock()
        mock_node.origins = ['https://sea.lore.kernel.org', CANONICAL]
        mock_node.canonical_origin = CANONICAL

        feed_dir = tmp_path / 'test-feed'
        (feed_dir / 'git' / '0.git').mkdir(parents=True)
        feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        # Minimal feed state so update_feed doesn't try to init
        feed_state = {
            'epochs': {'0': {'latest_commit': 'abc123'}},
            'last_update': '2026-01-01T00:00:00',
            'update_successful': True,
        }
        (feed_dir / 'korgalore.feed').write_text(json.dumps(feed_state))
        epochs_info = [{'epoch': 0, 'path': '/lkml/git/0.git', 'fpr': 'abc'}]
        (feed_dir / 'epochs.json').write_text(json.dumps(epochs_info))
        # The manifest has the same epoch with a new fingerprint: fetch, not clone
        mock_node.request.return_value = manifest_response({'/lkml/git/0.git': {'fingerprint': 'changed'}})

        expected_config = {'url.https://sea.lore.kernel.org/.insteadOf': f'{CANONICAL}/'}

        with (
            patch('korgalore.lore_feed.run_git_command') as mock_git,
            patch.object(feed, 'feed_updated', return_value=True),
            patch.object(feed, 'save_feed_state'),
        ):
            # First fetch (--shallow-since) fails, second fetch (--depth=1) succeeds
            mock_git.side_effect = [
                (128, b'', b'fatal: error processing shallow info: 4'),
                (0, b'', b''),
            ]
            feed.update_feed()

            fetch_calls = [c for c in mock_git.call_args_list if len(c[0]) >= 2 and 'fetch' in c[0][1]]
            assert len(fetch_calls) == 2
            # First call uses --shallow-since
            assert '--shallow-since=1.week.ago' in fetch_calls[0][0][1]
            # Second call uses --depth=1
            assert '--depth=1' in fetch_calls[1][0][1]
            assert '--shallow-since=1.week.ago' not in fetch_calls[1][0][1]
            # Both calls reuse the mirror config
            for c in fetch_calls:
                assert c[1]['git_config'] == expected_config
