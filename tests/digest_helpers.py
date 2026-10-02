"""Shared helpers for the digest tests."""

from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import click


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
