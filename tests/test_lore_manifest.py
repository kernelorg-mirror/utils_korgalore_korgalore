"""Tests for public-inbox manifest fetching.

Manifests describe the epochs an archive is split into, and both
LoreFeed.get_manifest() and LoreFeed.validate_public_inbox_url() go
through the same _fetch_manifest() helper. The validate side is covered
in test_subscribe.py; these tests cover get_manifest(), which used to
let a corrupt gzip escape as BadGzipFile and returned an empty dict for
an empty manifest.
"""

import gzip
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from korgalore import RemoteError
from korgalore.lore_feed import LoreFeed


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


def _gzipped_response(manifest_data: Dict[str, Any]) -> MagicMock:
    """Create a mock response carrying a gzipped manifest."""
    response = MagicMock()
    response.content = gzip.compress(json.dumps(manifest_data).encode())
    response.raise_for_status = MagicMock()
    return response


class TestGetManifest:
    """LoreFeed.get_manifest() reports remote problems as RemoteError."""

    def test_returns_parsed_manifest(self, tmp_path: Path) -> None:
        """A valid manifest comes back as a dict, fetched from manifest.js.gz."""
        manifest = {
            '/lkml/git/0.git': {'fingerprint': 'abc123'},
            '/lkml/git/1.git': {'fingerprint': 'def456'},
        }
        feed, mock_node = _make_feed(tmp_path, _gzipped_response(manifest))

        result = feed.get_manifest()

        assert result == manifest
        mock_node.request.assert_called_once_with('GET', 'https://lore.kernel.org/lkml/manifest.js.gz')

    def test_trailing_slash_does_not_double_up(self, tmp_path: Path) -> None:
        """A feed URL with a trailing slash still yields one separator."""
        mock_node = MagicMock()
        mock_node.request.return_value = _gzipped_response({'/lkml/git/0.git': {}})
        feed = LoreFeed('test', tmp_path, 'https://lore.kernel.org/lkml/', lore_node=mock_node)

        feed.get_manifest()

        mock_node.request.assert_called_once_with('GET', 'https://lore.kernel.org/lkml/manifest.js.gz')

    def test_corrupt_gzip_raises_remote_error(self, tmp_path: Path) -> None:
        """Content that is not valid gzip is a remote problem, not a crash."""
        response = MagicMock()
        response.content = b'<html>404 Not Found</html>'
        response.raise_for_status = MagicMock()
        feed, _mock_node = _make_feed(tmp_path, response)

        with pytest.raises(RemoteError, match='Failed to parse manifest'):
            feed.get_manifest()

    def test_truncated_json_raises_remote_error(self, tmp_path: Path) -> None:
        """Valid gzip wrapping broken JSON is also a RemoteError."""
        response = MagicMock()
        response.content = gzip.compress(b'{"/lkml/git/0.git": ')
        response.raise_for_status = MagicMock()
        feed, _mock_node = _make_feed(tmp_path, response)

        with pytest.raises(RemoteError, match='Failed to parse manifest'):
            feed.get_manifest()

    def test_empty_manifest_raises_remote_error(self, tmp_path: Path) -> None:
        """An empty manifest means a broken server, not an archive with no epochs."""
        feed, _mock_node = _make_feed(tmp_path, _gzipped_response({}))

        with pytest.raises(RemoteError, match='Empty manifest'):
            feed.get_manifest()

    def test_http_error_raises_remote_error(self, tmp_path: Path) -> None:
        """A non-2xx response is surfaced as RemoteError, naming the URL."""
        response = MagicMock()
        response.raise_for_status.side_effect = Exception('503 Server Error')
        feed, _mock_node = _make_feed(tmp_path, response)

        with pytest.raises(RemoteError, match='Failed to fetch manifest from https://lore.kernel.org/lkml/manifest'):
            feed.get_manifest()

    def test_request_failure_raises_remote_error(self, tmp_path: Path) -> None:
        """A transport-level failure is surfaced as RemoteError."""
        mock_node = MagicMock()
        mock_node.request.side_effect = Exception('Connection refused')
        feed = LoreFeed('test', tmp_path, 'https://lore.kernel.org/lkml', lore_node=mock_node)

        with pytest.raises(RemoteError, match='Failed to fetch manifest'):
            feed.get_manifest()
