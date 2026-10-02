"""Shared helpers for the digest tests."""

import os
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from korgalore.digest import DigestSchedule
from korgalore.lore_feed import LoreFeed

UTC = timezone.utc
# 09:00 UTC, well after a 07:00 send time in any time zone near UTC
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
DAILY = DigestSchedule()
DNAME = 'lkml-digest'


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
