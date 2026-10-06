"""Tests for tracked thread delivery tuple format.

Verifies that map_tracked_threads() produces 4-tuples consistent with
map_deliveries(), so that retry_all_failed_deliveries() and perform_pull()
can unpack them without a ValueError.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from korgalore.cli import map_tracked_threads
from korgalore.tracking import TrackedThread, TrackStatus
from tests.digest_helpers import make_ctx


def _make_tracked_thread(tmp_path: Path, labels: Optional[List[str]] = None) -> TrackedThread:
    """Create a TrackedThread with sensible defaults."""
    now = datetime.now(timezone.utc)
    return TrackedThread(
        track_id='track-abc123',
        msgid='<test@example.com>',
        subject='Test thread',
        target='local',
        labels=labels or ['INBOX'],
        lei_path=tmp_path / 'lei-test',
        created=now,
        last_update=now,
        last_new_message=now,
        status=TrackStatus.ACTIVE,
        message_count=1,
    )


def _map_tracked(tmp_path: Path, tracked: TrackedThread) -> Tuple[MagicMock, MagicMock, Dict[str, Any]]:
    """Run map_tracked_threads for one active thread.

    Returns the LeiFeed and target the delivery should be made of, and the
    resulting deliveries dict.
    """
    ctx = make_ctx(
        {
            'config': {'targets': {}, 'feeds': {}},
            'targets': {},
            'feeds': {},
            'deliveries': {},
            'data_dir': tmp_path / 'kgl-test',
        }
    )
    manifest = MagicMock()
    manifest.check_and_expire_threads.return_value = []
    manifest.get_active_threads.return_value = [tracked]
    feed = MagicMock()
    target = MagicMock()
    with (
        patch('korgalore.cli.get_tracking_manifest', return_value=manifest),
        patch('korgalore.cli.LeiFeed', return_value=feed),
        patch('korgalore.cli.get_target', return_value=target),
    ):
        map_tracked_threads(ctx)
    return feed, target, ctx.obj['deliveries']


class TestTrackedThreadDeliveryTuple:
    """Tracked thread deliveries must use the same 4-tuple as regular ones."""

    def test_tuple_unpacks_like_regular_delivery(self, tmp_path: Path) -> None:
        """The tuple must unpack as (feed, target, labels, subfolder) without error."""
        tracked = _make_tracked_thread(tmp_path)
        feed, target, deliveries = _map_tracked(tmp_path, tracked)

        # This is the exact unpacking used by retry_all_failed_deliveries
        assert list(deliveries) == [tracked.track_id]
        for delivery_name, (got_feed, got_target, labels, subfolder) in deliveries.items():
            assert delivery_name == tracked.track_id
            assert got_feed is feed
            assert got_target is target
            assert labels == ['INBOX']
            # Tracked threads do not support subfolders
            assert subfolder is None

    def test_labels_preserved(self, tmp_path: Path) -> None:
        """Labels from the tracked thread must appear in the delivery tuple."""
        tracked = _make_tracked_thread(tmp_path, labels=['patch-review', 'urgent'])
        _, _, deliveries = _map_tracked(tmp_path, tracked)

        _, _, labels, _ = deliveries[tracked.track_id]
        assert labels == ['patch-review', 'urgent']
