"""Tests for public-inbox manifest fetching.

Manifests describe the epochs an archive is split into, and both
LoreFeed.get_manifest() and LoreFeed.validate_public_inbox_url() go
through the same _fetch_manifest() helper. The validate side is covered
in test_subscribe.py; these tests cover get_manifest(), which used to
let a corrupt gzip escape as BadGzipFile and returned an empty dict for
an empty manifest.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from korgalore import RemoteError
from korgalore.lore_feed import LoreFeed
from tests.feed_helpers import gzipped_response, manifest_response


def _make_feed(feed_dir: Path, response: MagicMock) -> tuple[LoreFeed, MagicMock]:
    """Create a LoreFeed whose node returns the given response.

    The mock node is returned alongside the feed so assertions do not
    have to reach into the feed's private attribute, which is typed as a
    real LoreNode.
    """
    mock_node = MagicMock()
    mock_node.request.return_value = response
    feed = LoreFeed('test', feed_dir, 'https://lore.kernel.org/lkml', lore_node=mock_node)
    return feed, mock_node


class TestGetManifest:
    """LoreFeed.get_manifest() reports remote problems as RemoteError."""

    @pytest.mark.parametrize(
        'feed_url',
        [
            pytest.param('https://lore.kernel.org/lkml', id='plain-url'),
            # A trailing slash still yields one separator
            pytest.param('https://lore.kernel.org/lkml/', id='trailing-slash'),
        ],
    )
    def test_returns_parsed_manifest(self, tmp_path: Path, feed_url: str) -> None:
        """A valid manifest comes back as a dict, fetched from manifest.js.gz."""
        manifest = {
            '/lkml/git/0.git': {'fingerprint': 'abc123'},
            '/lkml/git/1.git': {'fingerprint': 'def456'},
        }
        mock_node = MagicMock()
        mock_node.request.return_value = manifest_response(manifest)
        feed = LoreFeed('test', tmp_path, feed_url, lore_node=mock_node)

        result = feed.get_manifest()

        assert result == manifest
        mock_node.request.assert_called_once_with('GET', 'https://lore.kernel.org/lkml/manifest.js.gz')

    @pytest.mark.parametrize(
        'response',
        [
            pytest.param(MagicMock(content=b'<html>404 Not Found</html>'), id='corrupt-gzip'),
            pytest.param(gzipped_response(b'{"/lkml/git/0.git": '), id='truncated-json'),
        ],
    )
    def test_unparseable_manifest_raises_remote_error(self, tmp_path: Path, response: MagicMock) -> None:
        """Bad gzip, or valid gzip around broken JSON, is a remote problem, not a crash."""
        feed, _mock_node = _make_feed(tmp_path, response)

        with pytest.raises(RemoteError, match='Failed to parse manifest'):
            feed.get_manifest()

    def test_empty_manifest_raises_remote_error(self, tmp_path: Path) -> None:
        """An empty manifest means a broken server, not an archive with no epochs."""
        feed, _mock_node = _make_feed(tmp_path, manifest_response({}))

        with pytest.raises(RemoteError, match='Empty manifest'):
            feed.get_manifest()

    @pytest.mark.parametrize('failure', ['http-status', 'transport'])
    def test_fetch_failure_raises_remote_error(self, tmp_path: Path, failure: str) -> None:
        """A non-2xx response or a transport failure is surfaced as RemoteError, naming the URL."""
        mock_node = MagicMock()
        if failure == 'http-status':
            mock_node.request.return_value.raise_for_status.side_effect = Exception('503 Server Error')
        else:
            mock_node.request.side_effect = Exception('Connection refused')
        feed = LoreFeed('test', tmp_path, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        with pytest.raises(
            RemoteError, match=re.escape('Failed to fetch manifest from https://lore.kernel.org/lkml/manifest')
        ):
            feed.get_manifest()
