"""Tests for sending digests: state, scheduling, splitting and the job stages.

The send_digest tests use a real public-inbox style git repository and a
real maildir, so they check what ends up on disk, not which mocks were
called. Each "run" builds a new feed object, the same way every kgl run
does, so no cache can hide a bug.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import liblore
from korgalore import StateError
from korgalore.cli import look_up_root_subjects, render_digest_job, run_digest_worker, send_digest, summarize_digest_job
from korgalore.digest import DigestJob, DigestSchedule
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from korgalore.maildir_target import MaildirTarget
from korgalore.summarizer import SummaryCache, SummaryRun
from tests.digest_helpers import (
    DAILY,
    DNAME,
    NOW,
    FlakyTarget,
    InboxRepo,
    RecordingSummarizer,
    collect,
    delivered,
    digest_text,
    job_of,
    make_raw,
    mkmsg,
    part_text,
    send,
    send_parts,
    small_parts,
    worker_ctx,
)


class TestSendDigest:
    def test_first_run_backfills_one_period(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('old@x', NOW - timedelta(days=3))
        repo.add_msg('recent@x', NOW - timedelta(hours=12))
        repo.add_msg('fresh@x', NOW - timedelta(hours=1))

        digest = send(repo, maildir)

        assert digest is not None
        [msg] = delivered(maildir)
        assert msg['Message-ID'] == digest['Message-ID']
        assert msg.get_content_type() == 'multipart/alternative'
        text = digest_text(msg)
        assert 'recent@x' in text
        assert 'fresh@x' in text
        assert 'old@x' not in text
        # Links go to the feed's own archive
        html = part_text(msg, 'html')
        assert 'https://lore.kernel.org/lkml/fresh@x/' in html

        feed = repo.feed()
        assert feed.load_digest_sent(DNAME) == NOW
        assert feed.load_delivery_info(DNAME)['epochs']['0']['last'] == repo.head

    def test_not_due_sends_nothing(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        repo.add_msg('b@x', NOW + timedelta(minutes=30))

        assert send(repo, maildir, now=NOW + timedelta(hours=1)) is None
        assert len(delivered(maildir)) == 1
        assert repo.feed().load_digest_sent(DNAME) == NOW

    def test_next_digest_has_only_new_messages(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('b@x', tomorrow - timedelta(hours=2))

        digest = send(repo, maildir, now=tomorrow)

        assert digest is not None
        text = digest_text(digest)
        assert 'b@x' in text
        assert 'a@x' not in text
        assert repo.feed().load_digest_sent(DNAME) == tomorrow

    def test_reply_to_old_thread_is_an_update(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('root@x', NOW - timedelta(hours=2), subject='[PATCH] mm: fix it')
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('reply@x', tomorrow - timedelta(hours=2), subject='Re: [PATCH] mm: fix it', irt='root@x')

        text = digest_text(send(repo, maildir, now=tomorrow))

        assert '1 continuing' in text
        assert 'mm: fix it' in text
        # A continuing thread links to its root, where the whole thread is
        assert 'https://lore.kernel.org/lkml/root@x/' in text

    def test_empty_period_is_skipped_but_recorded(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)

        assert send(repo, maildir, now=tomorrow) is None
        assert len(delivered(maildir)) == 1
        # Recorded as sent, so we don't check again until the next slot
        assert repo.feed().load_digest_sent(DNAME) == tomorrow

    def test_empty_feed(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        assert send(repo, maildir) is None
        assert repo.feed().load_digest_sent(DNAME) == NOW
        assert not repo.feed().has_delivery_pointer(DNAME)

    @pytest.mark.parametrize('old_message', [False, True], ids=['empty-feed', 'nothing-in-the-period'])
    def test_send_empty_sends_once(self, repo: InboxRepo, maildir: MaildirTarget, old_message: bool) -> None:
        if old_message:
            repo.add_msg('a@x', NOW - timedelta(days=3))
        sched = DigestSchedule(send_empty=True)
        assert send(repo, maildir, schedule=sched) is not None
        assert send(repo, maildir, now=NOW + timedelta(minutes=10), schedule=sched) is None
        assert len(delivered(maildir)) == 1

    def test_feed_gets_first_messages(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        send(repo, maildir)  # empty, no pointer saved
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('a@x', tomorrow - timedelta(hours=1))
        # Sent before the last digest, so it was in that (empty) period
        repo.add_msg('old@x', NOW - timedelta(hours=1))

        text = digest_text(send(repo, maildir, now=tomorrow))

        assert 'a@x' in text
        assert 'old@x' not in text
        assert repo.feed().has_delivery_pointer(DNAME)

    @pytest.mark.parametrize('new_message', [True, False], ids=['new-message', 'empty-digest'])
    def test_force_when_not_due(self, repo: InboxRepo, maildir: MaildirTarget, new_message: bool) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        later = NOW + timedelta(hours=1)
        if new_message:
            repo.add_msg('b@x', later - timedelta(minutes=5))

        digest = send(repo, maildir, now=later, force=True)

        assert digest is not None
        if new_message:
            assert 'b@x' in digest_text(digest)
        assert len(delivered(maildir)) == 2
        assert repo.feed().load_digest_sent(DNAME) == later

    @pytest.mark.parametrize('nice_message', [True, False], ids=['bozo-dropped', 'only-bozos-is-empty'])
    def test_bozofilter(self, repo: InboxRepo, maildir: MaildirTarget, nice_message: bool) -> None:
        if nice_message:
            repo.add_msg('nice@x', NOW - timedelta(hours=2))
        repo.add_msg('bozo@x', NOW - timedelta(hours=1), sender='Bozo <bozo@example.com>')

        digest = send(repo, maildir, bozofilter={'bozo@example.com'})

        if nice_message:
            text = digest_text(digest)
            assert 'nice@x' in text
            assert 'bozo@x' not in text
        else:
            assert digest is None
            assert delivered(maildir) == []

    def test_deleted_message_is_skipped(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=2))
        # public-inbox records a removal as a commit with a 'd' file
        last = repo.add(make_raw('a@x', 'gone'), NOW - timedelta(hours=1), filename='d')

        digest = send(repo, maildir)

        assert digest is not None
        # The pointer still moves past the deletion
        assert repo.feed().load_delivery_info(DNAME)['epochs']['0']['last'] == last

    def test_target_failure_keeps_state(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        send(repo, maildir)
        tomorrow = NOW + timedelta(days=1)
        repo.add_msg('b@x', tomorrow - timedelta(hours=1))
        broken = MagicMock()
        broken.import_message.side_effect = RuntimeError('server said no')

        with pytest.raises(RuntimeError):
            send(repo, broken, now=tomorrow)
        assert repo.feed().load_digest_sent(DNAME) == NOW

        # The next run sends the same messages
        digest = send(repo, maildir, now=tomorrow + timedelta(minutes=10))
        assert digest is not None
        assert 'b@x' in digest_text(digest)

    def test_target_gets_labels_and_names(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        target = MagicMock()

        parts = send_digest(DNAME, repo.feed(), target, ['digests'], 'lists/lkml', DAILY, now=NOW)

        assert len(parts) == 1
        digest = parts[0]
        target.connect.assert_called_once()
        raw = target.import_message.call_args.args[0]
        assert raw == digest.as_bytes()
        assert target.import_message.call_args.kwargs == {
            'labels': ['digests'],
            'feed_name': 'lkml',
            'delivery_name': DNAME,
            'subfolder': 'lists/lkml',
        }

    def test_message_mode_state_survives(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Turning a message delivery into a digest keeps working."""
        repo.add_msg('a@x', NOW - timedelta(hours=1))
        feed = repo.feed()
        feed.save_delivery_info(DNAME)  # what a message delivery leaves behind
        assert feed.load_digest_sent(DNAME) is None

        assert send(repo, maildir) is not None


def add_threads(repo: InboxRepo, count: int) -> None:
    """count one-message threads, all inside the first digest period."""
    repo.add_msgs(*((f't{n}@x', NOW - timedelta(hours=8) + timedelta(minutes=n)) for n in range(count)))


class TestSplitDelivery:
    def test_parts_are_sent_in_order(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        add_threads(repo, 4)
        with small_parts():
            parts = send_parts(repo, maildir)

        assert len(parts) > 1
        subjects = [str(msg['Subject']) for msg in delivered(maildir)]
        assert subjects == [str(msg['Subject']) for msg in parts]
        total = len(parts)
        assert [str(msg['X-Korgalore-Digest-Part']) for msg in parts] == [f'{n}/{total}' for n in range(1, total + 1)]
        assert repo.feed().load_digest_sent(DNAME) == NOW
        assert not job_of(repo).path.exists()

    def test_failed_part_is_resumed(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        add_threads(repo, 4)
        flaky = FlakyTarget(maildir, fail_on=[3])

        with small_parts(), pytest.raises(RuntimeError):
            send_parts(repo, flaky)
        assert len(delivered(maildir)) == 2
        # State is not saved until the last part is in
        assert repo.feed().load_digest_sent(DNAME) is None
        assert job_of(repo).exists()

        # A new message arrives before the next run: it waits for the next digest
        repo.add_msg('late@x', NOW + timedelta(minutes=30))
        with small_parts():
            rest = send_parts(repo, maildir, now=NOW + timedelta(hours=1))

        everything = delivered(maildir)
        total = int(str(everything[0]['X-Korgalore-Digest-Part']).split('/')[1])
        assert total == 4
        assert len(rest) == total - 2
        # Every part arrived exactly once, and all of them are one digest
        numbers = sorted(int(str(msg['X-Korgalore-Digest-Part']).split('/')[0]) for msg in everything)
        assert numbers == list(range(1, total + 1))
        assert len({msg['Message-ID'] for msg in everything}) == total
        assert not any('late@x' in digest_text(msg) for msg in everything)
        # The digest period ends when it was rendered, not when the last part went out
        assert repo.feed().load_digest_sent(DNAME) == NOW
        assert not job_of(repo).path.exists()

        tomorrow = NOW + timedelta(days=1)
        nxt = send_parts(repo, maildir, now=tomorrow)
        assert len(nxt) == 1
        assert 'late@x' in digest_text(nxt[0])
        assert 't0@x' not in digest_text(nxt[0])

    def test_resume_comes_before_schedule(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Left-over parts go out on the next run, even when no digest is due."""
        add_threads(repo, 4)
        with small_parts():
            with pytest.raises(RuntimeError):
                send_parts(repo, FlakyTarget(maildir, fail_on=[1]))
            assert delivered(maildir) == []
            assert len(send_parts(repo, maildir, now=NOW + timedelta(minutes=5))) == 4

    def test_job_already_delivered(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """We stopped after saving state but before removing the job."""
        add_threads(repo, 3)
        send_parts(repo, maildir)
        job = job_of(repo)
        job.create([], {'period_end': NOW.isoformat(), 'pointer': None})
        job.write_parts([])

        assert send_parts(repo, maildir, now=NOW + timedelta(minutes=5)) == []
        assert not job.path.exists()
        assert len(delivered(maildir)) == 1

    def test_bad_job_file(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        job = job_of(repo)
        job.path.mkdir()
        (job.path / DigestJob.JOB_FILE).write_text('{not json')
        with pytest.raises(StateError):
            send_parts(repo, maildir)

    def test_unknown_stage(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        job = job_of(repo)
        job.path.mkdir()
        (job.path / DigestJob.JOB_FILE).write_text(json.dumps({'stage': 'summarizing', 'period_end': NOW.isoformat()}))
        with pytest.raises(StateError, match='summarizing'):
            send_parts(repo, maildir)


class TestJobStages:
    """Each stage of a digest job, and resuming between them."""

    def test_collect_copies_messages_and_pointer(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        top = repo.add_msg('b@x', NOW - timedelta(hours=2))

        assert collect(repo)

        job = job_of(repo)
        state = job.load()
        assert state['stage'] == DigestJob.COLLECTED
        assert len(job.messages()) == 2
        assert state['pointer']['entry']['last'] == top
        assert state['pointer']['entry']['msgid'] == '<b@x>'
        # Nothing has been summarized yet
        assert job.summaries(state) == {}
        assert state['period_end'] == NOW.isoformat()
        # Nothing is saved until the digest is delivered
        assert repo.feed().load_digest_sent(DNAME) is None

    def test_empty_period_makes_no_job(self, repo: InboxRepo) -> None:
        repo.add_msg('old@x', NOW - timedelta(days=3))

        assert not collect(repo)

        assert not job_of(repo).path.exists()
        assert repo.feed().load_digest_sent(DNAME) == NOW

    @pytest.mark.parametrize('via_worker', [False, True], ids=['send', 'worker'])
    def test_later_stages_read_no_git(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, via_worker: bool
    ) -> None:
        """After collect, the feed repositories are not needed at all.

        This is what lets a slow stage run without holding the feed lock,
        while kgl pull fetches and rewrites the feed.
        """
        top = repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        repo.feed_dir.joinpath('git').rename(repo.feed_dir / 'git-away')

        if via_worker:
            assert DNAME in run_digest_worker(worker_ctx(tmp_path, repo, maildir), [DNAME])
        else:
            assert send(repo, maildir, now=NOW + timedelta(minutes=5)) is not None
        [digest] = delivered(maildir)
        assert 'a@x' in digest_text(digest)
        repo.feed_dir.joinpath('git-away').rename(repo.feed_dir / 'git')
        feed = repo.feed()
        assert feed.load_digest_sent(DNAME) == NOW
        assert feed.get_delivery_info_for_epoch(DNAME, 0)['last'] == top
        assert not job_of(repo).path.exists()

    def test_collected_job_keeps_its_period(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """Messages that arrive after collect wait for the next digest."""
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        repo.add_msg('late@x', NOW + timedelta(minutes=30))

        digest = send(repo, maildir, now=NOW + timedelta(hours=1))

        assert digest is not None
        assert 'a@x' in digest_text(digest)
        assert 'late@x' not in digest_text(digest)
        nxt = send(repo, maildir, now=NOW + timedelta(days=1))
        assert nxt is not None
        assert 'late@x' in digest_text(nxt)
        assert 'a@x' not in digest_text(nxt)

    def test_render_drops_the_messages(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)

        render_digest_job(DNAME, repo.feed(), DAILY, job, now=NOW)

        assert job.load()['stage'] == DigestJob.RENDERED
        assert len(job.pending()) == 1
        assert not job.messages_dir.exists()

    @pytest.mark.parametrize(
        ('collected', 'part'),
        [
            # Without a job file, the job was never finished, so start again
            pytest.param(False, '0001', id='no-job-file'),
            # A render that stopped half way left parts, but the job is still collected
            pytest.param(True, '0007', id='collected'),
        ],
    )
    def test_half_written_parts_are_thrown_away(
        self, repo: InboxRepo, maildir: MaildirTarget, collected: bool, part: str
    ) -> None:
        add_threads(repo, 3)
        if collected:
            assert collect(repo)
        job = job_of(repo)
        job.parts_dir.mkdir(parents=True, exist_ok=True)
        (job.parts_dir / f'{part}.eml').write_bytes(b'Subject: half-written\n\n')

        (digest,) = send_parts(repo, maildir, now=NOW + timedelta(minutes=5))

        assert 'half-written' not in str(digest['Subject'])
        assert [str(msg['Subject']) for msg in delivered(maildir)] == [str(digest['Subject'])]

    def test_bozofilter_is_applied_at_collect(self, repo: InboxRepo) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        repo.add_msg('b@x', NOW - timedelta(hours=2), sender='Bozo <bozo@example.com>')

        assert collect(repo, bozofilter={'bozo@example.com'})

        messages = job_of(repo).messages()
        assert len(messages) == 1
        assert b'bozo@example.com' not in messages[0]


def add_review(repo: InboxRepo, cover: str, subject: str, when: datetime = NOW - timedelta(hours=2)) -> None:
    """A review of patch 2 of a series whose cover letter is older than the period."""
    msgid = f'review-{cover}'
    repo.add(mkmsg(msgid, f'Re: {subject}', refs=[cover, f'p2-{cover}'], irt=f'p2-{cover}').as_bytes(), when)


class TestRootLookup:
    """The collect stage looks up the cover letters of continuing series."""

    def test_digest_is_named_after_the_cover(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, no_archive_lookups: MagicMock
    ) -> None:
        add_review(repo, 'cover@x', '[PATCH v3 2/7] mm: use the tail pointer')
        no_archive_lookups.side_effect = None
        no_archive_lookups.return_value = make_raw('cover@x', '[PATCH v3 0/7] mm: frobnicate the widgets')

        assert collect(repo)
        assert job_of(repo).load()['root_subjects'] == {'cover@x': '[PATCH v3 0/7] mm: frobnicate the widgets'}
        no_archive_lookups.assert_called_once_with('cover@x')
        # The summarizer is told the cover subject too
        fake = RecordingSummarizer()
        summarize_digest_job(DNAME, DAILY, job_of(repo), SummaryRun(fake, SummaryCache(tmp_path / 'cache')), now=NOW)
        (prompt,) = fake.prompts
        assert '[PATCH v3 0/7] mm: frobnicate the widgets' in prompt
        digest = send(repo, maildir)

        text = digest_text(digest)
        assert '\n[PATCH v3 0/7] mm: frobnicate the widgets\n' in text
        assert 'on 2/7' in text

    def test_first_failure_stops_the_lookups(
        self, repo: InboxRepo, maildir: MaildirTarget, no_archive_lookups: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        add_review(repo, 'one@x', '[PATCH 2/3] mm: first series')
        add_review(repo, 'two@x', '[PATCH 2/3] net: second series')
        no_archive_lookups.side_effect = liblore.RemoteError('503 Service Unavailable')

        digest = send(repo, maildir)

        assert no_archive_lookups.call_count == 1
        assert 'cannot look up' in caplog.text
        # The digest still goes out, with the names it had before
        text = digest_text(digest)
        assert '\n[PATCH 2/3] mm: first series\n' in text
        assert '\n[PATCH 2/3] net: second series\n' in text

    def test_root_that_is_a_reply_is_left_out(self, repo: InboxRepo, no_archive_lookups: MagicMock) -> None:
        add_review(repo, 'cover@x', '[PATCH v8 16/23] dma-buf: routing')
        no_archive_lookups.side_effect = None
        no_archive_lookups.return_value = make_raw('cover@x', 'Re: [PATCH v8 16/23] dma-buf: routing')

        assert collect(repo)

        no_archive_lookups.assert_called_once_with('cover@x')
        assert job_of(repo).load()['root_subjects'] == {}

    def test_only_patch_titles_are_looked_up(self, repo: InboxRepo) -> None:
        """A thread already named after its cover letter needs no lookup (the fixture fails on any)."""
        add_review(repo, 'cover@x', '[PATCH v3 0/7] mm: frobnicate the widgets')
        repo.add_msg('new@x', NOW - timedelta(hours=1), subject='[PATCH 2/2] mm: a new series')

        assert collect(repo)

        assert job_of(repo).load()['root_subjects'] == {}

    def test_lei_feeds_do_not_look_up(self) -> None:
        feed = MagicMock(spec=LeiFeed)
        raw = mkmsg('review@x', 'Re: [PATCH 2/3] mm: a patch', refs=['cover@x', 'p2@x']).as_bytes()

        assert look_up_root_subjects(DNAME, feed, [raw]) == {}

    def test_job_without_root_subjects(self, repo: InboxRepo, maildir: MaildirTarget) -> None:
        """A job collected by an older version still renders."""
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)
        state = job.load()
        del state['root_subjects']
        (job.path / DigestJob.JOB_FILE).write_text(json.dumps(state))

        digest = send(repo, maildir, now=NOW + timedelta(minutes=5))

        assert 'a@x' in digest_text(digest)

    @pytest.mark.parametrize('bad', [{'cover@x': 7}, ['cover@x']], ids=['not-a-subject', 'not-a-map'])
    def test_bad_root_subjects(self, repo: InboxRepo, maildir: MaildirTarget, bad: Any) -> None:
        repo.add_msg('a@x', NOW - timedelta(hours=3))
        assert collect(repo)
        job = job_of(repo)
        state = job.load()
        state['root_subjects'] = bad
        (job.path / DigestJob.JOB_FILE).write_text(json.dumps(state))

        with pytest.raises(StateError, match='root_subjects'):
            send_parts(repo, maildir, now=NOW + timedelta(minutes=5))

    def test_lore_feed_fetches_through_its_node(self, tmp_path: Path, real_get_message: Any) -> None:
        node = MagicMock()
        node.get_message_by_msgid.return_value = b'Subject: hi\n\n'
        feed = LoreFeed('lkml', tmp_path, 'https://lore.kernel.org/lkml', lore_node=node)

        assert real_get_message(feed, 'cover@x') == b'Subject: hi\n\n'
        node.get_message_by_msgid.assert_called_once_with('cover@x')
