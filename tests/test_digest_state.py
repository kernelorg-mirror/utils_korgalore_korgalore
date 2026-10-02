"""Tests for the delivery state that digests keep in the feed."""

import json
from datetime import timedelta

import pytest

from korgalore import StateError
from tests.digest_helpers import DNAME, NOW, InboxRepo


class TestDigestState:
    def test_round_trip_keeps_pointer(self, repo: InboxRepo) -> None:
        commit = repo.add_msg('a@x', NOW)
        feed = repo.feed()
        feed.save_delivery_info(DNAME, 0, commit, digest_sent=NOW)

        info = feed.load_delivery_info(DNAME)
        assert info['epochs']['0']['last'] == commit
        assert info['digest'] == {'last_sent': NOW.isoformat()}
        assert feed.load_digest_sent(DNAME) == NOW

    def test_message_save_keeps_digest_state(self, repo: InboxRepo) -> None:
        commit = repo.add_msg('a@x', NOW)
        feed = repo.feed()
        feed.save_delivery_info(DNAME, 0, commit, digest_sent=NOW)
        feed.save_delivery_info(DNAME, 0, commit)
        assert feed.load_digest_sent(DNAME) == NOW

    def test_missing(self, repo: InboxRepo) -> None:
        assert repo.feed().load_digest_sent('nope') is None

    def test_corrupt(self, repo: InboxRepo) -> None:
        state = repo.feed_dir / f'korgalore.{DNAME}.info'
        state.write_text(json.dumps({'epochs': {}, 'digest': {'last_sent': 'yesterday'}}))
        with pytest.raises(StateError):
            repo.feed().load_digest_sent(DNAME)

    def test_commits_since(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(days=2))
        second = repo.add_msg('b@x', NOW - timedelta(hours=2))
        third = repo.add_msg('c@x', NOW - timedelta(hours=1))
        assert repo.feed().get_commits_since(NOW - timedelta(days=1)) == [(0, second), (0, third)]
        assert repo.feed().get_commits_since(NOW) == []

    def test_commits_since_empty_repo(self, repo: InboxRepo) -> None:
        assert repo.feed().get_commits_since(NOW) == []

    def test_commits_since_spans_epochs(self, repo: InboxRepo) -> None:
        """A list that rolled over to a new epoch inside the window loses nothing."""
        repo.add_msg('old@x', NOW - timedelta(days=2))
        last_in_old = repo.add_msg('a@x', NOW - timedelta(hours=3))
        new_epoch = InboxRepo(repo.feed_dir, epoch=1)
        first_in_new = new_epoch.add_msg('b@x', NOW - timedelta(hours=2))
        assert repo.feed().get_commits_since(NOW - timedelta(days=1)) == [(0, last_in_old), (1, first_in_new)]
        assert repo.feed().get_commits_since(NOW - timedelta(hours=2, minutes=30)) == [(1, first_in_new)]
