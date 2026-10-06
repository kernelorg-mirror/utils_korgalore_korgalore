"""Tests for kgl digest --estimate, which reports without calling a model.

The numbers themselves are tested against real runs in test_summarizer.py.
These tests check which messages the estimate looks at, and that it
leaves no trace: no job, no saved state, no cached summaries.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from korgalore.cli import estimate_digest, run_digest_estimates, summarize_digest_job
from korgalore.digest import DigestSchedule
from korgalore.maildir_target import MaildirTarget
from korgalore.summarizer import SummaryCache, SummaryRun
from tests.digest_helpers import (
    BOB,
    DNAME,
    NOW,
    InboxRepo,
    RecordingSummarizer,
    answered_repo,
    cache_of,
    collect,
    job_of,
    send,
    summarized_ctx,
)

SUMMARIZED = DigestSchedule(summarizer='local')


@pytest.fixture
def repo(repo: InboxRepo) -> InboxRepo:
    """a@x gets an answer; lonely@x does not."""
    return answered_repo(repo)


def estimate(
    repo: InboxRepo,
    cache: SummaryCache,
    fake: Optional[RecordingSummarizer] = None,
    schedule: DigestSchedule = SUMMARIZED,
    **kwargs: Any,
) -> List[str]:
    kwargs.setdefault('now', NOW)
    return estimate_digest(DNAME, repo.feed(), schedule, fake, cache, **kwargs)


class TestEstimateDigest:
    def test_next_digest(self, repo: InboxRepo, cache: SummaryCache) -> None:
        lines = estimate(repo, cache, RecordingSummarizer())
        assert lines == [
            f'Digest {DNAME} (due now)',
            '  2 threads, 3 messages',
            '  Summarizer fake, model qwen3:32b, on this machine',
            '  No summary needed: 1, cached: 0, over max_summaries: 0',
            '  Model calls: 1 (0 build on an earlier summary)',
            lines[5],
        ]
        assert lines[5].startswith('  Input: ')

    def test_leaves_no_trace(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        estimate(repo, cache, fake)

        assert fake.prompts == []
        assert not job_of(repo).exists()
        assert repo.feed().load_digest_sent(DNAME) is None
        assert not cache.path.exists() or not any(cache.path.iterdir())
        # The real digest still has everything
        assert collect(repo)
        assert len(job_of(repo).messages()) == 3

    def test_not_due_counts_up_to_now(self, repo: InboxRepo, cache: SummaryCache, maildir: MaildirTarget) -> None:
        send(repo, maildir)
        repo.add_msg('new@x', NOW + timedelta(hours=1))
        lines = estimate(repo, cache, RecordingSummarizer(), now=NOW + timedelta(hours=2))

        assert lines[0] == f'Digest {DNAME} (not due yet, counting up to now)'
        assert lines[1] == '  1 threads, 1 messages'

    def test_waiting_job_is_what_the_worker_sends(self, repo: InboxRepo, cache: SummaryCache) -> None:
        assert collect(repo)
        repo.add_msg('late@x', NOW + timedelta(hours=1))
        lines = estimate(repo, cache, RecordingSummarizer(), now=NOW + timedelta(hours=2))

        assert lines[0] == f'Digest {DNAME} (waiting for the digest worker)'
        # late@x goes into the next digest, not this one
        assert lines[1] == '  2 threads, 3 messages'

    def test_summarized_job(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        assert collect(repo)
        summarize_digest_job(DNAME, SUMMARIZED, job_of(repo), SummaryRun(fake, cache), now=NOW)

        assert estimate(repo, cache, fake) == [f'Digest {DNAME}: already summarized, waiting to be sent']

    def test_worker_is_on_it(self, repo: InboxRepo, cache: SummaryCache) -> None:
        with job_of(repo).locked():
            lines = estimate(repo, cache, RecordingSummarizer())
        assert lines == [f'Digest {DNAME}: the digest worker is working on it now']

    @pytest.mark.parametrize(
        ('cached', 'extra_thread', 'schedule', 'counts', 'calls'),
        [
            pytest.param(
                True, False, SUMMARIZED, 'cached: 1, over max_summaries: 0', 0, id='cached-summaries-cost-nothing'
            ),
            # A summary made without the instructions is not counted as cached
            pytest.param(
                True,
                False,
                DigestSchedule(summarizer='local', summary_instructions='Tell me if anyone sounds upset.'),
                'cached: 0, over max_summaries: 0',
                1,
                id='summary-instructions',
            ),
            pytest.param(
                False,
                True,
                DigestSchedule(summarizer='local', max_summaries=1),
                'cached: 0, over max_summaries: 1',
                1,
                id='max-summaries',
            ),
        ],
    )
    def test_summary_counts(
        self,
        repo: InboxRepo,
        cache: SummaryCache,
        cached: bool,
        extra_thread: bool,
        schedule: DigestSchedule,
        counts: str,
        calls: int,
    ) -> None:
        if cached:
            cache.store('a@x', 'qwen3:32b', 'from before', ['a@x', 'r@x'], NOW)
        if extra_thread:
            repo.add_msg('b@x', NOW - timedelta(minutes=30))
            repo.add_msg('rb@x', NOW - timedelta(minutes=20), sender=BOB, irt='b@x')
        lines = estimate(repo, cache, RecordingSummarizer(), schedule=schedule)

        assert f'  No summary needed: 1, {counts}' in lines
        assert f'  Model calls: {calls} (0 build on an earlier summary)' in lines
        assert any(line.startswith('  Input:') for line in lines) == (calls > 0)

    def test_remote_summarizer_stands_out(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        fake.is_local = False
        assert '  Summarizer fake, model qwen3:32b, NOT on this machine' in estimate(repo, cache, fake)

    def test_cut_prompts(self, repo: InboxRepo, cache: SummaryCache) -> None:
        fake = RecordingSummarizer()
        fake.max_input_chars = 100
        lines = estimate(repo, cache, fake)
        assert lines[-1].startswith('  Cut to max_input_chars (100): 1 prompts, ')

    def test_plain_digest(self, repo: InboxRepo, cache: SummaryCache) -> None:
        lines = estimate(repo, cache, None, schedule=DigestSchedule())
        assert lines == [f'Digest {DNAME} (due now)', '  2 threads, 3 messages', '  Plain digest, nothing to summarize']


class TestRunEstimates:
    def test_prints_each_digest(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = RecordingSummarizer()
        ctx = summarized_ctx(tmp_path, repo, maildir, fake)
        run_digest_estimates(ctx, [DNAME], now=NOW)

        out = capsys.readouterr().out
        assert out.startswith(f'Digest {DNAME} (')
        assert '  Model calls: 1 ' in out
        assert fake.prompts == []
        assert not cache_of(ctx).path.exists() or not any(cache_of(ctx).path.iterdir())

    def test_bozofilter(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx = summarized_ctx(tmp_path, repo, maildir, RecordingSummarizer())
        ctx.obj['bozofilter'] = {'bob@x'}
        run_digest_estimates(ctx, [DNAME], now=NOW)
        # Bob's answer is left out, as in the real digest
        assert '  2 threads, 2 messages' in capsys.readouterr().out

    def test_failure_moves_on(
        self, tmp_path: Path, repo: InboxRepo, maildir: MaildirTarget, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = summarized_ctx(tmp_path, repo, maildir, RecordingSummarizer())
        ctx.obj['deliveries']['other'] = ctx.obj['deliveries'][DNAME]
        ctx.obj['digest_schedules']['other'] = SUMMARIZED
        with patch('korgalore.cli.estimate_digest', side_effect=[OSError('disk on fire'), ['fine']]) as est:
            run_digest_estimates(ctx, [DNAME, 'other'], now=NOW)
        assert est.call_count == 2
        assert 'Could not estimate digest lkml-digest: disk on fire' in caplog.text
