"""Shared helpers for the digest tests."""

import mailbox
import os
import re
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from functools import partial
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner, Result

from korgalore.cli import SUMMARY_CACHE_DIR, collect_digest, digest_cmd, send_digest
from korgalore.digest import DigestJob, DigestSchedule, render_digest_parts
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
        return self.add_many([(raw, when, filename)])[0]

    def add_many(self, items: Sequence[Tuple[bytes, datetime, str]]) -> List[str]:
        """Commit (raw, when, filename) triples in one git fast-import run.

        Each message is one commit, as public-inbox writes them, but a
        single git process imports them all. Returns the commit hashes.
        """
        stream = bytearray()
        for mark, (raw, when, filename) in enumerate(items, 1):
            stamp = f'{int(when.timestamp())} {when.strftime("%z") or "+0000"}'
            stream += f'commit refs/heads/master\nmark :{mark}\n'.encode()
            stream += f'author pi <pi@localhost> {stamp}\ncommitter pi <pi@localhost> {stamp}\n'.encode()
            stream += b'data 3\nmsg\n'
            if mark == 1 and self.head:
                stream += f'from {self.head}\n'.encode()
            stream += b'deleteall\n'
            stream += f'M 100644 inline {filename}\ndata {len(raw)}\n'.encode() + raw + b'\n'
        marks = self.gitdir / 'marks'
        self._git('fast-import', '--quiet', '--date-format=raw', f'--export-marks={marks}', stdin=bytes(stream))
        commits = [line.split()[1] for line in marks.read_text().splitlines()]
        marks.unlink()
        self.head = commits[-1]
        return commits

    def add_msg(self, msgid: str, when: datetime, **kwargs: Any) -> str:
        return self.add(make_raw(msgid, kwargs.pop('subject', f'[PATCH] {msgid}'), **kwargs), when)

    def add_msgs(self, *msgs: Tuple[str, datetime]) -> List[str]:
        """add_msg for several (msgid, when) pairs, in one git run."""
        return self.add_many([(make_raw(msgid, f'[PATCH] {msgid}'), when, 'm') for msgid, when in msgs])

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


@contextmanager
def small_parts(max_size: int = 1) -> Generator[None, None, None]:
    """Make send_digest split its digest at max_size bytes.

    render_digest_parts binds DIGEST_PART_MAX as a default argument, and
    the cli calls it without one, so the only way to get several parts
    out of a handful of threads is to hand the cli a different function.
    At the default of 1 every thread is a part of its own.
    """
    with patch('korgalore.cli.render_digest_parts', partial(render_digest_parts, max_size=max_size)):
        yield


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
        # The summary_instructions of each call
        self.instructions: List[Optional[str]] = []

    def summarize(self, text: str, instructions: Optional[str] = None) -> str:
        self.prompts.append(text)
        self.instructions.append(instructions)
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


def repo_with_msg(repo: InboxRepo) -> InboxRepo:
    """One message, so every digest has something to send."""
    repo.add_msg('a@x', NOW - timedelta(hours=3))
    return repo


def answered_repo(repo: InboxRepo) -> InboxRepo:
    """a@x gets an answer; lonely@x does not."""
    repo.add_many(
        [
            (make_raw('a@x', '[PATCH] a@x'), NOW - timedelta(hours=3), 'm'),
            (make_raw('r@x', '[PATCH] r@x', sender=BOB, irt='a@x'), NOW - timedelta(hours=2), 'm'),
            (make_raw('lonely@x', '[PATCH] lonely@x'), NOW - timedelta(hours=1), 'm'),
        ]
    )
    return repo


# Short names for the functions in korgalore.cli that kgl digest calls
DIGEST_CLI_FUNCTIONS = {
    'map': 'map_deliveries',
    'lock': 'lock_all_feeds',
    'unlock': 'unlock_all_feeds',
    'update': 'update_all_feeds',
    'estimate': 'run_digest_estimates',
    'due': 'run_due_digests',
    'work': 'run_digest_worker',
    'close': 'close_requests_session',
}


@contextmanager
def digest_cli_env(*names: str) -> Generator[Dict[str, MagicMock], None, None]:
    """Replace the named korgalore.cli functions with mocks, and yield them by short name."""
    mocks = {name: MagicMock() for name in names}
    with ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch(f'korgalore.cli.{DIGEST_CLI_FUNCTIONS[name]}', mock))
        yield mocks


def invoke_digest(obj: Dict[str, Any], *args: str) -> Result:
    """Run "kgl digest" with the given ctx.obj."""
    return CliRunner().invoke(digest_cmd, list(args), obj=obj)
