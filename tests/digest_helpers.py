"""Shared helpers for the digest tests."""

import mailbox
import os
import re
import subprocess
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import click

from korgalore.cli import SUMMARY_CACHE_DIR, collect_digest, send_digest
from korgalore.digest import DigestJob, DigestSchedule
from korgalore.lore_feed import LoreFeed
from korgalore.maildir_target import MaildirTarget
from korgalore.summarizer import DEFAULT_MAX_INPUT_CHARS, SummarizerError, SummaryCache

UTC = timezone.utc
# 09:00 UTC, well after a 07:00 send time in any time zone near UTC
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
DAILY = DigestSchedule()
# Any summarizer name makes the worker send the digest
SLOW = DigestSchedule(summarizer='local')
DNAME = 'lkml-digest'
ALICE = 'Alice <alice@x>'
BOB = 'Bob <bob@x>'


def mkmsg(
    msgid: Optional[str],
    subject: str,
    body: str = 'Some text.\n',
    refs: Optional[List[str]] = None,
    irt: Optional[str] = None,
    sender: str = 'P. Author <p@example.org>',
    date: Optional[str] = 'Thu, 01 Oct 2026 09:12:00 +0200',
) -> EmailMessage:
    """Build a message with the threading headers we need."""
    msg = EmailMessage()
    msg['From'] = sender
    msg['Subject'] = subject
    if msgid:
        msg['Message-ID'] = f'<{msgid}>'
    if refs:
        msg['References'] = ' '.join(f'<{ref}>' for ref in refs)
    if irt:
        msg['In-Reply-To'] = f'<{irt}>'
    if date:
        msg['Date'] = date
    msg.set_content(body)
    return msg


def make_raw(msgid: str, subject: str, sender: str = 'P. Author <p@example.org>', irt: Optional[str] = None) -> bytes:
    lines = [
        f'From: {sender}',
        f'Subject: {subject}',
        f'Message-ID: <{msgid}>',
        'Date: Thu, 01 Oct 2026 08:00:00 +0000',
    ]
    if irt:
        lines += [f'In-Reply-To: <{irt}>', f'References: <{irt}>']
    return ('\n'.join(lines) + '\n\nSome text.\n').encode()


class InboxRepo:
    """A bare public-inbox v2 epoch that we can add messages to.

    Each commit has an 'm' file (a message) or a 'd' file (a deletion),
    and a commit date we choose, since digests select by commit date.
    """

    def __init__(self, feed_dir: Path, epoch: int = 0) -> None:
        self.feed_dir = feed_dir
        self.gitdir = feed_dir / 'git' / f'{epoch}.git'
        self.gitdir.mkdir(parents=True)
        self._git('init', '--bare', '--initial-branch=master', '.')
        self.head: Optional[str] = None

    def _git(self, *args: str, stdin: Optional[bytes] = None, env: Optional[Dict[str, str]] = None) -> str:
        full_env = dict(os.environ, **(env or {}))
        result = subprocess.run(
            ['git', '-C', str(self.gitdir), *args], input=stdin, capture_output=True, check=True, env=full_env
        )
        return result.stdout.decode().strip()

    def add(self, raw: bytes, when: datetime, filename: str = 'm') -> str:
        blob = self._git('hash-object', '-w', '--stdin', stdin=raw)
        tree = self._git('mktree', stdin=f'100644 blob {blob}\t{filename}\n'.encode())
        parent = ['-p', self.head] if self.head else []
        stamp = when.isoformat()
        env = {
            'GIT_AUTHOR_NAME': 'pi',
            'GIT_AUTHOR_EMAIL': 'pi@localhost',
            'GIT_COMMITTER_NAME': 'pi',
            'GIT_COMMITTER_EMAIL': 'pi@localhost',
            'GIT_AUTHOR_DATE': stamp,
            'GIT_COMMITTER_DATE': stamp,
        }
        commit = self._git('commit-tree', tree, *parent, '-m', 'msg', env=env)
        self._git('update-ref', 'refs/heads/master', commit)
        self.head = commit
        return commit

    def add_msg(self, msgid: str, when: datetime, **kwargs: Any) -> str:
        return self.add(make_raw(msgid, kwargs.pop('subject', f'[PATCH] {msgid}'), **kwargs), when)

    def feed(self) -> LoreFeed:
        return LoreFeed('lkml', self.feed_dir, 'https://lore.kernel.org/lkml')


class ShallowCopy:
    """A shallow mirror clone of an InboxRepo, like a lore feed has.

    Lore feeds fetch with --shallow-since and --update-shallow, so the cut
    moves forward on every fetch. The dates here are absolute, because the
    commits in InboxRepo have made-up dates.
    """

    def __init__(self, upstream: InboxRepo, feed_dir: Path, since: datetime) -> None:
        self.upstream = upstream
        self.feed_dir = feed_dir
        self.gitdir = feed_dir / 'git' / '0.git'
        self.gitdir.parent.mkdir(parents=True)
        subprocess.run(
            [
                'git',
                'clone',
                '--quiet',
                '--mirror',
                f'--shallow-since={since.isoformat()}',
                f'file://{upstream.gitdir}',
                str(self.gitdir),
            ],
            capture_output=True,
            check=True,
        )

    def fetch(self, since: datetime) -> None:
        subprocess.run(
            [
                'git',
                '-C',
                str(self.gitdir),
                'fetch',
                '--quiet',
                f'--shallow-since={since.isoformat()}',
                '--update-shallow',
                'origin',
            ],
            capture_output=True,
            check=True,
        )

    def feed(self) -> LoreFeed:
        return LoreFeed('lkml', self.feed_dir, 'https://lore.kernel.org/lkml')


def make_ctx(obj: Dict[str, Any]) -> click.Context:
    """A click context with the given ctx.obj, like the cli functions get."""
    ctx = click.Context(click.Command('test'))
    ctx.obj = obj
    return ctx


# <seconds>.M<microseconds>P<pid>Q<count>.<host>, as mailbox.Maildir names
# its files. None of the numbers are zero-padded, so the keys have to be
# sorted as numbers: as text, M12000 sorts before M999.
MAILDIR_KEY = re.compile(r'^(\d+)\.M(\d+)P\d+Q(\d+)\.')


def maildir_order(key: str) -> Tuple[int, ...]:
    match = MAILDIR_KEY.match(key)
    assert match is not None, key
    return tuple(int(part) for part in match.groups())


def delivered(target: MaildirTarget) -> List[EmailMessage]:
    """The messages in the maildir, parsed, in the order they were written."""
    md = mailbox.Maildir(target.maildir_path, create=False)
    parser = BytesParser(_class=EmailMessage, policy=policy.default)
    return [parser.parsebytes(md.get_bytes(key)) for key in sorted(md.iterkeys(), key=maildir_order)]


def part_text(msg: Optional[EmailMessage], subtype: str) -> str:
    """The text of one alternative ('plain' or 'html') of a digest."""
    assert msg is not None
    body = msg.get_body(preferencelist=(subtype,))
    assert body is not None
    content = body.get_content()
    assert isinstance(content, str)
    return content


def digest_text(msg: Optional[EmailMessage]) -> str:
    return part_text(msg, 'plain')


def digest_html(msg: Optional[EmailMessage]) -> str:
    return part_text(msg, 'html')


def job_of(repo: InboxRepo) -> DigestJob:
    return DigestJob(repo.feed().get_digest_job_dir(DNAME))


def send_parts(repo: InboxRepo, target: Any, now: datetime = NOW, **kwargs: Any) -> List[EmailMessage]:
    sched = kwargs.pop('schedule', DAILY)
    return send_digest(DNAME, repo.feed(), target, ['digests'], None, sched, now=now, **kwargs)


def send(repo: InboxRepo, target: Any, now: datetime = NOW, **kwargs: Any) -> Optional[EmailMessage]:
    """Send a digest that fits in one message; None when nothing was sent."""
    parts = send_parts(repo, target, now=now, **kwargs)
    assert len(parts) <= 1
    return parts[0] if parts else None


def collect(repo: InboxRepo, now: datetime = NOW, **kwargs: Any) -> bool:
    return collect_digest(DNAME, repo.feed(), DAILY, job_of(repo), now=now, **kwargs)


def worker_ctx(tmp_path: Path, repo: InboxRepo, target: Any, schedule: DigestSchedule = SLOW) -> click.Context:
    """The context of a kgl run with one digest delivery."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir(exist_ok=True)
    return make_ctx(
        {
            'data_dir': data_dir,
            'deliveries': {DNAME: (repo.feed(), target, ['digests'], None)},
            'digest_schedules': {DNAME: schedule},
            'bozofilter': set(),
        }
    )


def summarized_ctx(tmp_path: Path, repo: InboxRepo, target: Any, fake: 'RecordingSummarizer') -> click.Context:
    ctx = worker_ctx(tmp_path, repo, target)
    ctx.obj['summarizers'] = {'local': fake}
    return ctx


def cache_of(ctx: click.Context) -> SummaryCache:
    return SummaryCache(ctx.obj['data_dir'] / SUMMARY_CACHE_DIR)


class RecordingSummarizer:
    """Answers "summary N" and keeps every prompt it was given."""

    name = 'fake'
    max_input_chars = DEFAULT_MAX_INPUT_CHARS
    allow_private_feeds = False
    is_local = True

    def __init__(self, model: str = 'qwen3:32b', fail: bool = False, fail_calls: Sequence[int] = ()) -> None:
        self.model = model
        self.fail = fail
        # 1-based numbers of the calls that fail
        self.fail_calls = fail_calls
        self.prompts: List[str] = []

    def summarize(self, text: str) -> str:
        self.prompts.append(text)
        if self.fail or len(self.prompts) in self.fail_calls:
            raise SummarizerError('server said no')
        return f'summary {len(self.prompts)}'


class FlakyTarget:
    """Delivers to a maildir, but fails on chosen calls to import_message."""

    def __init__(self, maildir: MaildirTarget, fail_on: List[int]) -> None:
        self.identifier = 'flaky'
        self.maildir = maildir
        self.fail_on = fail_on
        self.calls = 0

    def connect(self) -> None:
        self.maildir.connect()

    def import_message(self, raw: bytes, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError('server said no')
        return self.maildir.import_message(raw, **kwargs)
