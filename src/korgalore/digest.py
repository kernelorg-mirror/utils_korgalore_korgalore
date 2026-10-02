"""Daily digests: group a feed's new messages into threads and report on them."""

import email.utils
import html
import json
import logging
import os
import re
import shutil
import textwrap
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from email.message import EmailMessage
from enum import Enum
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from typing import Any, Dict, Generator, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote

from liblore.utils import clean_header, get_clean_msgid, minimize_thread, msg_get_author, msg_get_payload

from korgalore import ConfigurationError, StateError

logger = logging.getLogger('korgalore')

# How many file names to list in a diff marker before saying "and N more"
MARKER_MAX_FILES = 10

# Lines that can start a diff. A plain "---" line is the format-patch
# separator before the diffstat, so "--- " only counts when "+++ " follows.
_DIFF_GIT_RE = re.compile(r'^diff --git a/(.+?) b/(.+)$')
_DIFF_START_RE = re.compile(r'^diff ')
_MINUS_FILE_RE = re.compile(r'^--- (\S+)')
_PLUS_FILE_RE = re.compile(r'^\+\+\+ (\S+)')
_INDEX_RE = re.compile(r'^Index: \S')
_INDEX_RULE_RE = re.compile(r'^={10,}$')

# Lines that can appear inside a diff, after it has started
_DIFF_BODY_PREFIXES = (
    ' ',
    '+',
    '-',
    '@@',
    '\\',
    'diff ',
    'index ',
    'Index: ',
    '====',
    'new file mode ',
    'deleted file mode ',
    'old mode ',
    'new mode ',
    'similarity index ',
    'dissimilarity index ',
    'rename from ',
    'rename to ',
    'copy from ',
    'copy to ',
    'Binary files ',
    'GIT binary patch',
)
_BINARY_BLOCK_RE = re.compile(r'^(literal|delta) \d+$')


def _starts_diff(lines: List[str], idx: int) -> bool:
    """Return True if a diff starts at lines[idx]."""
    line = lines[idx]
    nextline = lines[idx + 1] if idx + 1 < len(lines) else ''
    if _DIFF_START_RE.match(line):
        return True
    if _MINUS_FILE_RE.match(line) and _PLUS_FILE_RE.match(nextline):
        return True
    return bool(_INDEX_RE.match(line) and _INDEX_RULE_RE.match(nextline))


def _strip_path_prefix(path: str) -> str:
    """Drop the a/ or b/ prefix that git and most tools add to diff paths."""
    if path.startswith(('a/', 'b/')):
        return path[2:]
    return path


def _diff_files(region: List[str]) -> List[str]:
    """Return the files a diff region touches, in order, without duplicates."""
    files: List[str] = []
    minus_path: Optional[str] = None
    for line in region:
        path: Optional[str] = None
        gmatch = _DIFF_GIT_RE.match(line)
        if gmatch:
            path = gmatch.group(2)
        else:
            mmatch = _MINUS_FILE_RE.match(line)
            pmatch = _PLUS_FILE_RE.match(line)
            if mmatch:
                minus_path = mmatch.group(1)
            elif pmatch:
                # A deleted file has "+++ /dev/null", so use the old name
                plus_path: str = pmatch.group(1)
                if plus_path == '/dev/null' and minus_path:
                    plus_path = minus_path
                path = _strip_path_prefix(plus_path)
        if path and path not in files:
            files.append(path)
    return files


def _diff_marker(region: List[str]) -> str:
    """Build the line that replaces a diff region."""
    marker = f'[diff: {len(region)} lines'
    files = _diff_files(region)
    if files:
        shown = files[:MARKER_MAX_FILES]
        marker += f'; {len(files)} file{"s" if len(files) != 1 else ""}: ' + ', '.join(shown)
        if len(files) > len(shown):
            marker += f', and {len(files) - len(shown)} more'
    return marker + ']'


def _diff_region_end(lines: List[str], start: int) -> int:
    """Return the index just past the diff region that begins at start.

    The region goes on for as long as the lines look like diff lines.
    Empty lines are allowed inside it, because many mail clients strip the
    single space of an empty context line. Empty lines at the very end of
    the region are given back to the text that follows. A bare "-- " line
    is the signature marker, not a removed line, so it ends the region.
    """
    idx = start
    in_binary = False
    while idx < len(lines):
        line = lines[idx]
        if in_binary:
            # Base85 data runs until an empty line that does not lead to
            # another literal/delta block
            if line == '':
                nextline = lines[idx + 1] if idx + 1 < len(lines) else ''
                if not _BINARY_BLOCK_RE.match(nextline):
                    in_binary = False
            idx += 1
            continue
        if line == '-- ':
            break
        if line.startswith('GIT binary patch'):
            in_binary = True
            idx += 1
            continue
        if line == '' or line.startswith(_DIFF_BODY_PREFIXES) or _starts_diff(lines, idx):
            idx += 1
            continue
        break

    while idx > start and lines[idx - 1] == '':
        idx -= 1
    return idx


def strip_diffs(body: str) -> str:
    """Replace every diff in a message body with a one-line marker.

    Everything that is not part of a diff is kept: the commit message,
    trailers, the "---" separator, the diffstat, and any text written after
    an inline diff. Quoted lines start with ">", so a reply that quotes a
    patch is never changed here.

    The marker looks like ``[diff: 42 lines; 2 files: mm/a.c, mm/b.c]`` so
    that a summarizer can still tell that a patch was posted and what it
    touches.
    """
    lines = body.splitlines()
    out: List[str] = []
    idx = 0
    while idx < len(lines):
        if _starts_diff(lines, idx):
            end = _diff_region_end(lines, idx)
            out.append(_diff_marker(lines[idx:end]))
            idx = end
            continue
        out.append(lines[idx])
        idx += 1

    result = '\n'.join(out)
    if body.endswith('\n'):
        result += '\n'
    return result


# Trailers that report on someone else's patch. Signed-off-by and friends
# are left out: they belong to the patch, not to the review.
_TRAILER_RE = re.compile(r'^(reviewed|acked|tested|nacked|naked)-by:\s*(.+)$', re.IGNORECASE)
_TRAILER_NAMES = {
    'reviewed': 'Reviewed-by',
    'acked': 'Acked-by',
    'tested': 'Tested-by',
    'nacked': 'Nacked-by',
    'naked': 'Nacked-by',
}


def strip_review_trailers(body: str) -> str:
    """Remove the review trailers (Reviewed-by, Acked-by and so on) from a body.

    Signed-off-by and other trailers that belong to the patch are kept.
    Quoted trailers are kept too, because they start with ">".
    """
    lines = [line for line in body.splitlines(keepends=True) if not _TRAILER_RE.match(line)]
    return ''.join(lines)


def shrink_thread(msgs: List[EmailMessage]) -> List[EmailMessage]:
    """Make a thread as small as possible before it goes to a summarizer.

    First liblore's minimize_thread() drops extra headers and deep quoting.
    It leaves messages that contain a diff as they are, so we then replace
    the diffs with markers and drop the signatures (including the git
    version footer that format-patch adds).

    Review trailers are also dropped from patches and cover letters. A
    patch often carries them from earlier versions of the series, and the
    model would report those old reviews as if they were new. The digest
    finds the new trailers in the replies itself.

    The input messages are not changed; new copies are returned.
    """
    shrunk: List[EmailMessage] = []
    for mmsg in minimize_thread(msgs, reduce_quote_context=True):
        body = strip_diffs(msg_get_payload(mmsg, strip_signature=True))
        if is_patch_posting(clean_header(mmsg.get('Subject'))):
            body = strip_review_trailers(body)
        if not body.strip():
            continue
        mmsg.set_payload(body, charset='utf-8')
        shrunk.append(mmsg)
    return shrunk


# =====================================================================
# Threads and facts
# =====================================================================

_MSGID_RE = re.compile(r'<([^>]+)>')
# Only real reply/forward prefixes: a generic "two or three letters and a
# colon" pattern would also eat kernel subsystem prefixes like "mm:" or "net:"
_REPLY_PREFIX_RE = re.compile(r'^((re|fwd?|aw|antw|sv|vs|wg|tr|odp|ref)(\[\d+])?:\s*)+', re.IGNORECASE)
_BRACKET_PREFIX_RE = re.compile(r'^(\s*\[[^]]*]\s*)+')
# "[GIT PULL]", "[PULL v2]", "[GIT,PULL]" and so on
_PULL_RE = re.compile(r'\[[^]]*\bpull\b[^]]*]', re.IGNORECASE)
# "bug" or "bugs" as a word: "[BUG]" and "BUG_ON" count, but "debugfs" and
# "bugfix" do not
_BUG_RE = re.compile(r'(?<![a-z])bugs?(?![a-z])', re.IGNORECASE)
_VERSION_RE = re.compile(r'^(?:patch)?v(\d+)$')
_COUNTER_RE = re.compile(r'^(\d+)/(\d+)$')


@dataclass(frozen=True)
class Trailer:
    """A review trailer found in a reply, such as Reviewed-by."""

    name: str
    value: str
    email: str
    # The From address of the message that carried this trailer
    sender: str

    @property
    def from_sender(self) -> bool:
        """True if the person named in the trailer also sent it.

        Anyone can write "Acked-by: Someone Famous" in a reply, so a trailer
        sent by somebody else should be shown with a warning.
        """
        return self.email == self.sender


@dataclass(frozen=True)
class SeriesInfo:
    """Patch details parsed from a subject like "[PATCH v3 2/7] ..."."""

    version: int = 1
    counter: int = 0
    expected: int = 1
    rfc: bool = False
    resend: bool = False


@dataclass
class ThreadUpdate:
    """One new message in a thread."""

    msgid: str
    subject: str
    author_name: str
    author_email: str
    date: Optional[datetime]
    is_patch: bool
    # Patch details from the subject, also for replies ("Re: [PATCH 2/7] ...")
    series: Optional[SeriesInfo] = None
    trailers: List[Trailer] = field(default_factory=list)


class Section(Enum):
    """The parts of a digest, in the order they are shown."""

    NEW_PATCHES = 'New patches and pull requests'
    PATCH_UPDATES = 'Updates to earlier patches'
    BUG_REPORTS = 'Bug reports'
    DISCUSSIONS = 'Discussions'

    @property
    def title(self) -> str:
        return str(self.value)


_SECTION_ORDER = {section: index for index, section in enumerate(Section)}


@dataclass
class DigestThread:
    """A thread with activity in the digest period."""

    root_msgid: str
    subject: str
    # True if the first message of the thread is in this period
    is_new: bool
    updates: List[ThreadUpdate] = field(default_factory=list)
    # Arrival position of the newest update, used for a stable sort order
    last_seen: int = 0

    @property
    def series(self) -> Optional[SeriesInfo]:
        """Patch details of the thread, or None if it is not a patch thread."""
        return parse_series(self.subject)

    @property
    def patch_count(self) -> int:
        """Number of patches posted, not counting replies or cover letters."""
        count = 0
        for update in self.updates:
            if not update.is_patch:
                continue
            if update.series and update.series.counter == 0 and update.series.expected > 1:
                continue
            count += 1
        return count

    @property
    def section(self) -> Section:
        """Which part of the digest the thread goes in.

        A thread goes with the new patches when patches were posted in
        this period, even as replies (a v2 sent in reply to v1, or a fix
        posted in a bug report thread), or when it is a new series or pull
        request. A patch or pull request thread with only replies is an
        update. Of the other threads, the ones with "bug" in the subject
        are bug reports, and the rest are discussions.
        """
        is_work = self.series is not None or is_pull_request(self.subject)
        if self.patch_count or (self.is_new and is_work):
            return Section.NEW_PATCHES
        if is_work:
            return Section.PATCH_UPDATES
        if is_bug_report(self.subject):
            return Section.BUG_REPORTS
        return Section.DISCUSSIONS

    @property
    def participants(self) -> List[Tuple[str, str]]:
        """(name, email) of each author, in order of their first message."""
        seen: Dict[str, Tuple[str, str]] = {}
        for update in self.updates:
            if update.author_email not in seen:
                seen[update.author_email] = (update.author_name, update.author_email)
        return list(seen.values())

    @property
    def trailers(self) -> List[Trailer]:
        """All review trailers given in this period, without duplicates."""
        found: List[Trailer] = []
        for update in self.updates:
            for trailer in update.trailers:
                if trailer not in found:
                    found.append(trailer)
        return found


def strip_reply_prefixes(subject: str) -> str:
    """Remove leading "Re:", "Fwd:", "Aw:" and similar from a subject."""
    return _REPLY_PREFIX_RE.sub('', subject).strip()


def parse_series(subject: str) -> Optional[SeriesInfo]:
    """Parse patch details from the bracketed prefixes of a subject.

    Returns None when the subject has no PATCH or RFC prefix.
    """
    bmatch = _BRACKET_PREFIX_RE.match(strip_reply_prefixes(subject))
    if not bmatch:
        return None
    tokens = re.sub(r'[\[\]]', ' ', bmatch.group(0)).lower().split()
    if not any(token.startswith('patch') or token == 'rfc' for token in tokens):
        return None

    version = 1
    counter = 0
    expected = 1
    for token in tokens:
        vmatch = _VERSION_RE.match(token)
        if vmatch and int(vmatch.group(1)) > 0:
            version = int(vmatch.group(1))
            continue
        cmatch = _COUNTER_RE.match(token)
        if cmatch:
            counter = int(cmatch.group(1))
            expected = int(cmatch.group(2))
    return SeriesInfo(
        version=version,
        counter=counter,
        expected=expected,
        rfc='rfc' in tokens,
        resend='resend' in tokens,
    )


def is_pull_request(subject: str) -> bool:
    """True for a pull request like "[GIT PULL] ...", or a reply to one."""
    return _PULL_RE.match(strip_reply_prefixes(subject)) is not None


def is_bug_report(subject: str) -> bool:
    """True when the subject mentions a bug, like "[BUG] mm: oops in frob()".

    Only the word counts: this does not check that the thread has no
    patches. DigestThread.section does that.
    """
    return _BUG_RE.search(subject) is not None


def is_patch_posting(subject: str) -> bool:
    """True for a patch or cover letter, False for a reply to one."""
    if _REPLY_PREFIX_RE.match(subject):
        return False
    return parse_series(subject) is not None


def find_trailers(body: str, sender: str) -> List[Trailer]:
    """Find review trailers in the unquoted lines of a message body."""
    trailers: List[Trailer] = []
    for line in body.splitlines():
        tmatch = _TRAILER_RE.match(line)
        if not tmatch:
            continue
        value = tmatch.group(2).strip()
        addr = email.utils.parseaddr(value)[1].lower()
        if '@' not in addr:
            # "Acked-by: me" or prose that happens to look like a trailer
            continue
        trailer = Trailer(_TRAILER_NAMES[tmatch.group(1).lower()], value, addr, sender)
        if trailer not in trailers:
            trailers.append(trailer)
    return trailers


def _get_refs(msg: EmailMessage, msgid: str) -> List[str]:
    """Return the message's parents, References first, without duplicates."""
    refs: List[str] = []
    for hdr in ('References', 'In-Reply-To'):
        for hval in msg.get_all(hdr, []):
            for ref in _MSGID_RE.findall(str(hval)):
                # Some tools list the message itself, which means nothing
                if ref != msgid and ref not in refs:
                    refs.append(ref)
    return refs


def _get_date(msg: EmailMessage) -> Optional[datetime]:
    """Parse the Date header. A date without a timezone is taken as UTC."""
    raw = msg.get('Date')
    if not raw:
        return None
    try:
        date = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date


def _make_update(msg: EmailMessage, msgid: str) -> ThreadUpdate:
    """Collect the facts about one message."""
    subject = clean_header(msg.get('Subject'))
    name, addr = msg_get_author(msg)
    addr = addr.lower()
    series = parse_series(subject)
    is_patch = series is not None and not _REPLY_PREFIX_RE.match(subject)
    trailers: List[Trailer] = []
    if not is_patch:
        # The trailers in a patch were collected on earlier versions, so
        # only replies count as new reviews.
        trailers = find_trailers(msg_get_payload(msg), addr)
    return ThreadUpdate(
        msgid=msgid,
        subject=subject,
        author_name=clean_header(name) or addr,
        author_email=addr,
        date=_get_date(msg),
        is_patch=is_patch,
        series=series,
        trailers=trailers,
    )


class _Groups:
    """Union-find over Message-IDs that remembers the first id of each set."""

    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, first: str, second: str) -> None:
        root1 = self.find(first)
        root2 = self.find(second)
        if root1 != root2:
            self.parent[root2] = root1


def group_threads(
    msgs: Sequence[EmailMessage], root_subjects: Optional[Mapping[str, str]] = None
) -> List[DigestThread]:
    """Group the messages of a digest period into threads.

    Messages are expected in arrival order (the order of the feed's git
    commits), and each thread lists its updates in that order. Messages
    that point at each other through References or In-Reply-To end up in
    the same thread, even if the message that links them is not in the
    period.

    A thread is new if its first message (one with no parents) is in the
    period. Otherwise it is a continuing thread, and its root is the first
    Message-ID in the longest References chain of its messages: a reply
    that only has In-Reply-To names its parent, not the first message.

    A thread is named after its first message. A continuing thread does
    not have that message, so it is named after its oldest message in the
    period, unless root_subjects has the subject of its root (see
    roots_to_look_up).

    Threads are sorted by section (see DigestThread.section), then by
    number of new messages, most first, and then by the arrival of their
    newest message, newest first. The digest shows them in this order.
    """
    groups = _Groups()
    entries: List[Tuple[str, EmailMessage, List[str]]] = []
    seen: set[str] = set()
    for msg in msgs:
        msgid = get_clean_msgid(msg)
        if not msgid:
            # public-inbox always assigns a Message-ID, so this is unusual
            logger.debug('Skipping digest message without a Message-ID: %s', msg.get('Subject'))
            continue
        if msgid in seen:
            continue
        seen.add(msgid)
        refs = _get_refs(msg, msgid)
        groups.find(msgid)
        for ref in refs:
            groups.union(msgid, ref)
        entries.append((msgid, msg, refs))

    # Pick the root of each group before building the threads, so the
    # result does not depend on which message of a thread arrived first:
    # the message without parents if it is in the period, otherwise the
    # oldest ancestor named by the longest References chain in the group.
    roots: Dict[str, Tuple[str, bool]] = {}
    longest: Dict[str, int] = {}
    for msgid, _msg, refs in entries:
        key = groups.find(msgid)
        if key in roots and roots[key][1]:
            continue
        if not refs:
            roots[key] = (msgid, True)
        elif len(refs) > longest.get(key, 0):
            longest[key] = len(refs)
            roots[key] = (refs[0], False)

    threads: Dict[str, DigestThread] = {}
    for position, (msgid, msg, _refs) in enumerate(entries):
        key = groups.find(msgid)
        update = _make_update(msg, msgid)
        root, is_new = roots[key]
        thread = threads.get(key)
        if thread is None:
            thread = DigestThread(root_msgid=root, subject=strip_reply_prefixes(update.subject), is_new=is_new)
            threads[key] = thread
        elif msgid == root:
            # The first message arrived after a reply to it
            thread.subject = strip_reply_prefixes(update.subject)
        thread.updates.append(update)
        thread.last_seen = position

    if root_subjects:
        for thread in threads.values():
            if not thread.is_new and thread.root_msgid in root_subjects:
                thread.subject = root_subjects[thread.root_msgid]

    return sorted(threads.values(), key=lambda thr: (_SECTION_ORDER[thr.section], -len(thr.updates), -thr.last_seen))


def roots_to_look_up(threads: Sequence[DigestThread]) -> List[str]:
    """The roots of continuing threads that are named after one patch.

    Reviewers reply to the patches of a series, and the cover letter is
    often older than the digest period. Then the thread is named after one
    patch, such as "[PATCH v3 2/7] ...". The root, usually the cover
    letter, has a better name, so it is worth looking up in the archive.
    """
    roots: List[str] = []
    for thread in threads:
        series = thread.series
        if not thread.is_new and series is not None and series.counter > 0 and series.expected > 1:
            roots.append(thread.root_msgid)
    return roots


# =====================================================================
# Rendering
# =====================================================================

# How many updates to list per thread before saying "and N more"
UPDATES_MAX = 10
# How many patches to list per thread before saying "and N more"
PATCHES_MAX = 30

# Characters public-inbox leaves alone in a Message-ID URL (MID_ESC in
# PublicInbox::MID). quote() always keeps letters, digits and "_.-~".
_MID_SAFE = "!$&'()*+,;=:@"

# Inline styles only: many mail clients drop <style> blocks. No colors
# for text or backgrounds except a mid-gray, which reads in light and dark.
_MUTED = 'color:#888'
_STYLE_BODY = 'font-family:sans-serif;font-size:14px;line-height:1.45;max-width:760px;margin:0 auto;padding:16px'
_STYLE_THREAD = 'border-top:1px solid #8886;padding:12px 0'
_STYLE_SMALL = 'margin:4px 0;font-size:13px'
# Sets machine-generated text apart from the facts around it
_STYLE_SUMMARY = 'border-left:3px solid #8888;margin:8px 0;padding:2px 0 2px 12px'
_STYLE_LABEL = f'margin:8px 0 0;{_MUTED};font-size:12px'
# Trailers are what maintainers look for first, so they stand out like code
_STYLE_TRAILERS = (
    'margin:6px 0;padding:4px 8px;background:#8882;border-radius:4px;'
    'font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px'
)
_STYLE_SECTION = 'font-size:18px;margin:24px 0 4px'


DEFAULT_FROM = 'korgalore <korgalore@localhost>'


@dataclass
class DigestInfo:
    """Everything about a digest that does not come from the messages."""

    feed_name: str
    delivery_name: str
    # Web archive base for links, such as https://lore.kernel.org/lkml
    link_base: str
    period_start: datetime
    period_end: datetime
    from_addr: str = DEFAULT_FROM
    # The summarizer model; None makes a plain digest without summaries
    model: Optional[str] = None
    # When the feed history has a gap, the time of the oldest message we
    # still have. Everything between period_start and this is missing.
    history_start: Optional[datetime] = None
    # The time zone that message times are shown in. None is the local
    # zone, worked out for each time on its own, so a digest whose period
    # crosses a daylight saving change shows every message at the hour its
    # sender saw. A fixed offset, such as period_end.tzinfo, would be an
    # hour off on the other side of the change.
    tz: Optional[tzinfo] = None


@dataclass(frozen=True)
class DigestPart:
    """Where one part of a split digest sits in the whole digest."""

    number: int
    total: int
    # 1-based numbers of the first and last thread in this part
    first: int
    last: int
    # Threads in the whole digest
    thread_count: int


class NoSummary(Enum):
    """Why a thread in a summarized digest has no summary."""

    # Nobody replied, or nothing was left after shrinking: the facts
    # already say everything, so no summary is shown
    NOT_NEEDED = 'not-needed'
    # Past the max_summaries limit of the digest
    BUDGET = 'budget'
    # The summarizer failed or could not be reached
    UNAVAILABLE = 'unavailable'


# A thread's root Message-ID mapped to its summary, or to why it has none
Summaries = Mapping[str, Union[str, NoSummary]]

# What the reader sees instead of a summary. These are our words, not the
# model's, so they are not labelled as machine-generated.
_NO_SUMMARY_NOTES = {
    NoSummary.BUDGET: 'Summary skipped: this digest reached its max_summaries limit.',
    NoSummary.UNAVAILABLE: 'Summary unavailable.',
}


# Gmail cuts off HTML bodies over about 102 KB, so parts stay under this
DIGEST_PART_MAX = 90_000


def mid_url(link_base: str, msgid: str) -> str:
    """Return the web archive URL of a message, escaped like public-inbox does."""
    return f'{link_base.rstrip("/")}/{quote(msgid, safe=_MID_SAFE)}/'


def _plural(count: int, word: str, plural: Optional[str] = None) -> str:
    """Return "1 message" or "3 messages"."""
    return f'{count} {word if count == 1 else (plural or word + "s")}'


def _fmt_time(date: Optional[datetime], tz: Optional[tzinfo]) -> str:
    """Short weekday and time in the digest's timezone, such as "Thu 09:12"."""
    if date is None:
        return '?'
    return date.astimezone(tz).strftime('%a %H:%M')


def _fmt_trailer(trailer: Trailer) -> str:
    """A trailer, with a warning when someone else sent it."""
    text = f'{trailer.name}: {trailer.value}'
    if not trailer.from_sender:
        text += f' (sent by {trailer.sender})'
    return text


def _thread_facts(thread: DigestThread) -> List[str]:
    """The short facts shown under a thread's subject."""
    facts = [
        'new' if thread.is_new else 'continuing',
        _plural(len(thread.updates), 'new message'),
        _plural(len(thread.participants), 'person', 'people'),
    ]
    series = thread.series
    if series is not None:
        if series.version > 1:
            facts.append(f'v{series.version}')
        if series.rfc:
            facts.append('RFC')
        if series.resend:
            facts.append('RESEND')
    if thread.patch_count:
        facts.append(_plural(thread.patch_count, 'patch', 'patches') + ' posted')
    return facts


def _thread_version(thread: DigestThread) -> int:
    """The series version in the thread's subject, 1 if it has none."""
    series = thread.series
    return series.version if series else 1


def _patch_label(series: SeriesInfo, thread_version: int) -> str:
    """A short name for a patch: "01/18", "v7 01/18", "v7" or ""."""
    label = ''
    if series.expected > 1:
        label = f'{series.counter:0{len(str(series.expected))}d}/{series.expected}'
    if series.version != thread_version:
        label = f'v{series.version} {label}'.strip()
    return label


def _update_note(update: ThreadUpdate, thread: DigestThread) -> str:
    """What an update adds: what it replies to, and its trailers.

    A reply to a numbered patch says "on 01/18" instead of repeating the
    patch's subject, which is already in the thread's list of patches.
    """
    parts: List[str] = []
    subject = strip_reply_prefixes(update.subject)
    if subject != thread.subject:
        series = update.series
        if series is None:
            parts.append(subject)
        elif series.expected > 1:
            parts.append(f'on {_patch_label(series, _thread_version(thread))}')
        else:
            parts.append(_BRACKET_PREFIX_RE.sub('', subject).strip())
    parts.extend(trailer.name for trailer in update.trailers)
    return ' | '.join(parts)


@dataclass
class _Patch:
    """A patch in a thread's list of patches posted."""

    # "01/18", or "v7 01/18" when the version differs from the thread's
    label: str
    # The subject without its bracketed prefixes
    title: str
    update: ThreadUpdate


@dataclass
class _ThreadLists:
    """A thread's updates, split for display."""

    # The first patch or cover letter posted, or None
    poster: Optional[ThreadUpdate]
    # Patches in series order. The posting that started the thread is not
    # in the list when its subject is the thread's title.
    patches: List[_Patch]
    # Everything else, in arrival order
    replies: List[ThreadUpdate]


def _thread_lists(thread: DigestThread) -> _ThreadLists:
    """Split a thread's updates into the patches posted and the replies.

    A series arrives in a burst, often out of order, and every patch has
    the same author and time. So the patches are listed once, in series
    order, under one "Posted by" line.
    """
    thread_version = _thread_version(thread)
    poster: Optional[ThreadUpdate] = None
    found: List[Tuple[Tuple[int, int, int], _Patch]] = []
    replies: List[ThreadUpdate] = []
    for index, update in enumerate(thread.updates):
        series = update.series if update.is_patch else None
        if series is None:
            replies.append(update)
            continue
        if poster is None:
            poster = update
        if update.msgid == thread.root_msgid and (series.counter == 0 or series.expected == 1):
            # A cover letter or a single patch: its subject is the title
            continue
        title = _BRACKET_PREFIX_RE.sub('', update.subject).strip()
        patch = _Patch(_patch_label(series, thread_version), title, update)
        found.append(((series.version, series.counter, index), patch))
    found.sort(key=lambda item: item[0])
    return _ThreadLists(poster, [patch for _, patch in found], replies)


def _period_label(info: DigestInfo) -> str:
    """The digest period, such as "2026-09-30 07:00 to 2026-10-01 07:00"."""
    return f'{info.period_start:%Y-%m-%d %H:%M} to {info.period_end:%Y-%m-%d %H:%M}'


def _totals(threads: Sequence[DigestThread]) -> str:
    """The totals line at the top of a digest."""
    messages = sum(len(thread.updates) for thread in threads)
    new = sum(1 for thread in threads if thread.is_new)
    return (
        f'{_plural(len(threads), "thread")}, {_plural(messages, "message")} '
        f'({new} new, {len(threads) - new} continuing)'
    )


def _gap_notice(info: DigestInfo) -> Optional[str]:
    """Say which messages are missing, when the feed history has a gap."""
    if info.history_start is None:
        return None
    return (
        f'Some messages are missing. Korgalore only has messages from '
        f'{info.history_start:%Y-%m-%d %H:%M} on, so anything that arrived between '
        f'{info.period_start:%Y-%m-%d %H:%M} and then is not in this digest. '
        f'You can find it in the archive: {info.link_base.rstrip("/")}/'
    )


def _part_label(part: DigestPart) -> str:
    """Which threads a part has, such as "Part 2 of 5: threads 41-87 of 210"."""
    label = f'Part {part.number} of {part.total}: threads {part.first}-{part.last} of {part.thread_count}'
    if part.number == 1:
        label += '. The other parts are replies to this one.'
    return label


# A list marker that starts a summary point: "-", "*", "•" or "1." / "1)"
_POINT_RE = re.compile(r'^(?:[-*\u2022]|\d{1,2}[.)])\s+')


def summary_points(summary: str) -> List[str]:
    """Split a summary into the points of a list.

    Models are asked for one "- " point per line, but they do not always
    listen. A line with another list marker also starts a point, and a
    line without a marker continues the point above it, as when a model
    wraps a long point. A summary without any markers, such as one saved
    before summaries were lists, gets one point per line.
    """
    points: List[str] = []
    continues = False
    for line in summary.splitlines():
        line = line.strip()
        if not line:
            continues = False
            continue
        pmatch = _POINT_RE.match(line)
        if pmatch:
            points.append(line[pmatch.end() :])
            continues = True
        elif continues:
            points[-1] += ' ' + line
        else:
            points.append(line)
    return points


def _summary_for(
    info: DigestInfo, summaries: Optional[Summaries], thread: DigestThread
) -> Tuple[Optional[str], Optional[str]]:
    """The thread's (summary, note), with at most one of them set.

    A plain digest has neither. In a summarized digest, a thread without
    a summary gets a note saying why, unless it does not need one.
    """
    if info.model is None:
        return None, None
    found = summaries.get(thread.root_msgid) if summaries else None
    if isinstance(found, str) and found.strip():
        return found.strip(), None
    if found is NoSummary.NOT_NEEDED:
        return None, None
    reason = found if isinstance(found, NoSummary) else NoSummary.UNAVAILABLE
    return None, _NO_SUMMARY_NOTES[reason]


def _text_header(info: DigestInfo, threads: Sequence[DigestThread], part: Optional[DigestPart]) -> List[str]:
    lines = [f'{info.feed_name} digest', _period_label(info), _totals(threads)]
    if part is not None:
        lines.append(_part_label(part))
    lines.append('')
    notice = _gap_notice(info)
    if notice and (part is None or part.number == 1):
        lines.extend(textwrap.wrap(notice, width=72, break_on_hyphens=False))
        lines.append('')
    if not threads:
        lines.append('No activity in this period.')
    return lines


def _text_thread(info: DigestInfo, thread: DigestThread, summaries: Optional[Summaries]) -> List[str]:
    tz = info.tz
    lines = ['-' * 72, thread.subject, '  ' + ' | '.join(_thread_facts(thread))]
    # A "+" marks a new trailer, as in b4
    lines.extend('  + ' + _fmt_trailer(trailer) for trailer in thread.trailers)
    summary, note = _summary_for(info, summaries, thread)
    if summary is not None:
        # Blank lines keep the summary apart from the facts and lists
        lines.extend(['', '  Summary (machine-generated):'])
        for point in summary_points(summary):
            lines.append(
                textwrap.fill(
                    point, width=72, initial_indent='    - ', subsequent_indent='      ', break_on_hyphens=False
                )
            )
        lines.append('')
    if note is not None:
        lines.append(f'  {note}')
    lists = _thread_lists(thread)
    if lists.poster is not None:
        poster = lists.poster
        posted = f'  Posted by {poster.author_name}, {_fmt_time(poster.date, tz)}'
        lines.append(posted + (':' if lists.patches else ''))
        shown = lists.patches[:PATCHES_MAX]
        width = max((len(patch.label) for patch in shown), default=0)
        for patch in shown:
            line = f'    {patch.label:{width}}  {patch.title}' if width else f'    {patch.title}'
            if patch.update.author_email != poster.author_email:
                line += f'  ({patch.update.author_name})'
            lines.append(line)
        if len(lists.patches) > PATCHES_MAX:
            lines.append(f'    ... and {len(lists.patches) - PATCHES_MAX} more')
    if lists.replies:
        lines.append('  Follow-ups:')
        for update in lists.replies[:UPDATES_MAX]:
            line = f'    {_fmt_time(update.date, tz)}  {update.author_name}'
            note = _update_note(update, thread)
            if note:
                line += f'  {note}'
            lines.append(line)
        if len(lists.replies) > UPDATES_MAX:
            lines.append(f'    ... and {len(lists.replies) - UPDATES_MAX} more')
    lines.append(f'  Read:  {mid_url(info.link_base, thread.root_msgid)}')
    lines.append('')
    return lines


def _section_starts(
    threads: Sequence[DigestThread], part_threads: Sequence[DigestThread], part: Optional[DigestPart]
) -> Dict[int, str]:
    """Where a section heading goes in a part: thread index to heading.

    A heading counts the section's threads in the whole digest. A part
    that starts in the middle of a section says that it continues.
    """
    counts: Dict[Section, int] = {}
    for thread in threads:
        counts[thread.section] = counts.get(thread.section, 0) + 1
    previous: Optional[Section] = None
    if part is not None and part.first > 1:
        previous = threads[part.first - 2].section
    starts: Dict[int, str] = {}
    for index, thread in enumerate(part_threads):
        section = thread.section
        if index == 0 or section != previous:
            heading = f'{section.title} ({counts[section]})'
            if section == previous:
                heading += ', continued'
            starts[index] = heading
        previous = section
    return starts


def _render_text_part(
    info: DigestInfo,
    threads: Sequence[DigestThread],
    part_threads: Sequence[DigestThread],
    part: Optional[DigestPart],
    summaries: Optional[Summaries],
) -> str:
    lines = _text_header(info, threads, part)
    starts = _section_starts(threads, part_threads, part)
    for index, thread in enumerate(part_threads):
        thread_lines = _text_thread(info, thread, summaries)
        if index in starts:
            # The heading's own rule replaces the thread's rule
            lines.extend(['=' * 72, starts[index].upper(), '=' * 72])
            thread_lines = thread_lines[1:]
        lines.extend(thread_lines)
    return '\n'.join(lines) + '\n'


def render_text(info: DigestInfo, threads: Sequence[DigestThread], summaries: Optional[Summaries] = None) -> str:
    """Render the text/plain part of a digest."""
    return _render_text_part(info, threads, threads, None, summaries)


def _link(url: str, text: str) -> str:
    """An HTML link. Both parts are escaped."""
    return f'<a href="{html.escape(url)}">{html.escape(text)}</a>'


def _html_header(info: DigestInfo, threads: Sequence[DigestThread], part: Optional[DigestPart]) -> List[str]:
    esc = html.escape
    status = esc(_period_label(info)) + '<br>' + esc(_totals(threads))
    if part is not None:
        status += '<br>' + esc(_part_label(part))
    out = [
        '<!DOCTYPE html>',
        '<html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">',
        f'<title>{esc(info.feed_name)} digest</title></head>',
        f'<body style="{_STYLE_BODY}">',
        f'<h1 style="font-size:20px;margin:0 0 4px">{esc(info.feed_name)} digest</h1>',
        f'<p style="margin:0 0 12px;{_MUTED}">{status}</p>',
    ]
    notice = _gap_notice(info)
    if notice and (part is None or part.number == 1):
        out.append(f'<p style="margin:0 0 12px">&#9888; {esc(notice)}</p>')
    if not threads:
        out.append('<p>No activity in this period.</p>')
    return out


def _html_thread(info: DigestInfo, thread: DigestThread, summaries: Optional[Summaries]) -> List[str]:
    esc = html.escape
    tz = info.tz
    out = [f'<div style="{_STYLE_THREAD}">']
    out.append(
        '<h3 style="font-size:16px;margin:0 0 4px">'
        + _link(mid_url(info.link_base, thread.root_msgid), thread.subject)
        + '</h3>'
    )
    out.append(f'<p style="{_STYLE_SMALL};{_MUTED}">{esc(" | ".join(_thread_facts(thread)))}</p>')
    if thread.trailers:
        out.append(f'<div style="{_STYLE_TRAILERS}">')
        for trailer in thread.trailers:
            warn = '' if trailer.from_sender else '&#9888; '
            out.append(f'<div>+ {warn}{esc(_fmt_trailer(trailer))}</div>')
        out.append('</div>')
    summary, note = _summary_for(info, summaries, thread)
    if summary is not None:
        out.append(f'<div style="{_STYLE_SUMMARY}">')
        out.append(f'<p style="margin:0;{_MUTED};font-size:12px">Summary (machine-generated):</p>')
        out.append('<ul style="margin:4px 0;padding-left:20px">')
        out.extend(f'<li>{esc(point)}</li>' for point in summary_points(summary))
        out.append('</ul>')
        out.append('</div>')
    if note is not None:
        out.append(f'<p style="margin:8px 0;{_MUTED};font-size:12px">{esc(note)}</p>')
    lists = _thread_lists(thread)
    if lists.poster is not None:
        poster = lists.poster
        posted = f'Posted by {esc(poster.author_name)}, '
        posted += _link(mid_url(info.link_base, poster.msgid), _fmt_time(poster.date, tz))
        out.append(f'<p style="{_STYLE_SMALL}">{posted}{":" if lists.patches else ""}</p>')
    if lists.patches:
        out.append(f'<ul style="{_STYLE_SMALL};padding-left:20px">')
        for patch in lists.patches[:PATCHES_MAX]:
            url = mid_url(info.link_base, patch.update.msgid)
            item = f'{_link(url, patch.label)} {esc(patch.title)}' if patch.label else _link(url, patch.title)
            if lists.poster is not None and patch.update.author_email != lists.poster.author_email:
                item += f' <span style="{_MUTED}">({esc(patch.update.author_name)})</span>'
            out.append(f'<li>{item}</li>')
        if len(lists.patches) > PATCHES_MAX:
            out.append(f'<li style="{_MUTED}">and {len(lists.patches) - PATCHES_MAX} more</li>')
        out.append('</ul>')
    if lists.replies:
        out.append(f'<p style="{_STYLE_LABEL}">Follow-ups:</p>')
        out.append(f'<ul style="{_STYLE_SMALL};padding-left:20px">')
        for update in lists.replies[:UPDATES_MAX]:
            item = _link(mid_url(info.link_base, update.msgid), _fmt_time(update.date, tz))
            item += f' {esc(update.author_name)}'
            note = _update_note(update, thread)
            if note:
                item += f' <span style="{_MUTED}">{esc(note)}</span>'
            out.append(f'<li>{item}</li>')
        if len(lists.replies) > UPDATES_MAX:
            out.append(f'<li style="{_MUTED}">and {len(lists.replies) - UPDATES_MAX} more</li>')
        out.append('</ul>')
    out.append('</div>')
    return out


_HTML_FOOTER = '</body></html>'


def _html_section(heading: str) -> str:
    return f'<h2 style="{_STYLE_SECTION}">{html.escape(heading)}</h2>'


def _render_html_part(
    info: DigestInfo,
    threads: Sequence[DigestThread],
    part_threads: Sequence[DigestThread],
    part: Optional[DigestPart],
    summaries: Optional[Summaries],
) -> str:
    out = _html_header(info, threads, part)
    starts = _section_starts(threads, part_threads, part)
    for index, thread in enumerate(part_threads):
        if index in starts:
            out.append(_html_section(starts[index]))
        out.extend(_html_thread(info, thread, summaries))
    out.append(_HTML_FOOTER)
    return '\n'.join(out) + '\n'


def render_html(info: DigestInfo, threads: Sequence[DigestThread], summaries: Optional[Summaries] = None) -> str:
    """Render the text/html part of a digest.

    Every value that comes from a message or a summarizer is escaped, and
    every link is built here from link_base and a Message-ID.
    """
    return _render_html_part(info, threads, threads, None, summaries)


def split_threads(
    info: DigestInfo,
    threads: Sequence[DigestThread],
    summaries: Optional[Summaries] = None,
    max_size: int = DIGEST_PART_MAX,
) -> List[List[DigestThread]]:
    """Split threads into parts whose HTML stays under max_size bytes.

    A thread is never split, so a thread that is bigger than max_size on
    its own gets a part of its own. The order of the threads is kept.
    """
    # Measure the header of a part with the longest label it can have
    sample = DigestPart(number=1, total=len(threads), first=len(threads), last=len(threads), thread_count=len(threads))
    overhead = len('\n'.join([*_html_header(info, threads, sample), _HTML_FOOTER]).encode()) + 2
    # Room for every section heading, at the longest it can be
    overhead += sum(
        len(_html_section(f'{section.title} ({len(threads)}), continued').encode()) + 1 for section in Section
    )
    parts: List[List[DigestThread]] = [[]]
    size = overhead
    for thread in threads:
        thread_size = len('\n'.join(_html_thread(info, thread, summaries)).encode()) + 1
        if parts[-1] and size + thread_size > max_size:
            parts.append([])
            size = overhead
        parts[-1].append(thread)
        size += thread_size
    return parts


def _digest_subject(info: DigestInfo, threads: Sequence[DigestThread], part: Optional[DigestPart]) -> str:
    messages = sum(len(thread.updates) for thread in threads)
    prefix = f'[DIGEST {part.number}/{part.total}]' if part else '[digest]'
    return (
        f'{prefix} {info.feed_name}: {info.period_end:%Y-%m-%d} '
        f'({_plural(len(threads), "thread")}, {_plural(messages, "message")})'
    )


def _build_digest(
    info: DigestInfo,
    threads: Sequence[DigestThread],
    part_threads: Sequence[DigestThread],
    part: Optional[DigestPart],
    summaries: Optional[Summaries],
    msgid: str,
    now: datetime,
    first_msgid: Optional[str] = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg['From'] = info.from_addr
    msg['Subject'] = _digest_subject(info, threads, part)
    msg['Date'] = email.utils.format_datetime(now)
    msg['Message-ID'] = msgid
    if first_msgid:
        # Thread the parts under part 1, like a patch series
        msg['In-Reply-To'] = first_msgid
        msg['References'] = first_msgid
    msg['X-Korgalore-Digest'] = info.delivery_name
    if part is not None:
        msg['X-Korgalore-Digest-Part'] = f'{part.number}/{part.total}'
    msg['X-Korgalore-Digest-Model'] = info.model or 'none'
    msg.set_content(_render_text_part(info, threads, part_threads, part, summaries))
    msg.add_alternative(_render_html_part(info, threads, part_threads, part, summaries), subtype='html')
    return msg


def _new_msgid(info: DigestInfo) -> str:
    domain = email.utils.parseaddr(info.from_addr)[1].rpartition('@')[2] or 'localhost'
    return email.utils.make_msgid('digest', domain=domain)


def render_digest(
    info: DigestInfo,
    threads: Sequence[DigestThread],
    summaries: Optional[Summaries] = None,
    msgid: Optional[str] = None,
    now: Optional[datetime] = None,
) -> EmailMessage:
    """Build the digest email: text/plain and text/html in multipart/alternative.

    summaries maps a thread's root Message-ID to its summary, or to a
    NoSummary reason. It is only used when info.model is set; a missing
    entry shows "Summary unavailable.". msgid and now are for tests; by
    default a new Message-ID and the current time are used.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    full_msgid = f'<{msgid}>' if msgid else _new_msgid(info)
    return _build_digest(info, threads, threads, None, summaries, full_msgid, now)


def render_digest_parts(
    info: DigestInfo,
    threads: Sequence[DigestThread],
    summaries: Optional[Summaries] = None,
    now: Optional[datetime] = None,
    max_size: int = DIGEST_PART_MAX,
) -> List[EmailMessage]:
    """Build the digest as one email, or as several when it is too big.

    The parts are numbered like a patch series ("[DIGEST 2/5]") and parts
    2 and up are replies to part 1. Every part shows the totals of the
    whole digest. A digest that fits in one email is the same as the one
    render_digest() makes.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    chunks = split_threads(info, threads, summaries, max_size)
    if len(chunks) == 1:
        return [render_digest(info, threads, summaries, now=now)]
    parts: List[EmailMessage] = []
    first = 1
    first_msgid: Optional[str] = None
    for number, chunk in enumerate(chunks, start=1):
        part = DigestPart(number, len(chunks), first, first + len(chunk) - 1, len(threads))
        msgid = _new_msgid(info)
        parts.append(_build_digest(info, threads, chunk, part, summaries, msgid, now, first_msgid))
        first_msgid = first_msgid or msgid
        first += len(chunk)
    return parts


@contextmanager
def flocked(lock_path: Path, wait: bool = False) -> Generator[bool, None, None]:
    """Hold an exclusive lock on lock_path for the length of the block.

    Yields False right away, without waiting, when someone else has the
    lock; with wait, it waits for them instead and always yields True.
    This uses flock and not lockf: a lockf lock belongs to the whole
    process, so a second lock in the same process would not see the first
    one, and the GUI runs pulls in threads.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as lockfh:
        try:
            flock(lockfh, LOCK_EX if wait else LOCK_EX | LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            flock(lockfh, LOCK_UN)


class DigestJob:
    """One digest on its way out, kept on disk between stages.

    A digest goes through these stages:

    1. collect: copy the messages of the period out of the feed, together
       with the delivery pointer that is saved after the last part. This
       is the only stage that reads the feed's git repositories, so it is
       the only one that needs the feed lock.
    2. summarize (summarized digests only): store a summary, or the
       reason for none, for each thread. This is the slow stage.
    3. render: group the messages into threads and write the parts.
    4. deliver: send the parts one by one. Each part is removed after the
       target accepts it, and the state is saved after the last one.

    The job file is replaced atomically at the end of each stage, so a job
    always resumes from the last finished stage. A job directory without
    a job file was never finished and is thrown away. A failed part is
    resent exactly as it was rendered, so nothing is sent twice and
    nothing changes in between.
    """

    JOB_FILE = 'job.json'
    COLLECTED = 'collected'
    SUMMARIZED = 'summarized'
    RENDERED = 'rendered'

    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages_dir = path / 'messages'
        self.parts_dir = path / 'parts'

    def exists(self) -> bool:
        return (self.path / self.JOB_FILE).exists()

    def locked(self, wait: bool = False) -> AbstractContextManager[bool]:
        """Hold the job lock while working on the job; see flocked().

        The lock file sits next to the job directory, because the
        directory is removed when the job is done.
        """
        return flocked(self.path.with_name(f'{self.path.name}.lock'), wait=wait)

    def _save(self, state: Dict[str, Any]) -> None:
        tmp = self.path / f'{self.JOB_FILE}.tmp'
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, self.path / self.JOB_FILE)

    def create(self, messages: Sequence[bytes], state: Dict[str, Any]) -> None:
        """Start a new job with these raw messages, replacing any old one."""
        self.clear()
        self.messages_dir.mkdir(parents=True)
        for number, raw in enumerate(messages, start=1):
            (self.messages_dir / f'{number:06d}.eml').write_bytes(raw)
        self._save({**state, 'stage': self.COLLECTED, 'messages': len(messages)})

    def load(self) -> Dict[str, Any]:
        job_file = self.path / self.JOB_FILE
        try:
            state: Dict[str, Any] = json.loads(job_file.read_text())
        except (OSError, ValueError) as e:
            raise StateError(f'Cannot read digest job {job_file}: {e}') from e
        if state.get('stage') not in (self.COLLECTED, self.SUMMARIZED, self.RENDERED):
            raise StateError(f'Unknown stage {state.get("stage")!r} in digest job {job_file}')
        return state

    def messages(self) -> List[bytes]:
        """The collected messages, in the order they were collected."""
        return [path.read_bytes() for path in sorted(self.messages_dir.glob('*.eml'))]

    def write_summaries(self, model: str, summaries: Summaries) -> None:
        """Store the summaries and move the job to the render stage."""
        state = self.load()
        texts = {root: found for root, found in summaries.items() if isinstance(found, str)}
        reasons = {root: found.value for root, found in summaries.items() if isinstance(found, NoSummary)}
        self._save({**state, 'stage': self.SUMMARIZED, 'model': model, 'summaries': texts, 'no_summary': reasons})

    def summaries(self, state: Mapping[str, Any]) -> Dict[str, Union[str, NoSummary]]:
        """The summaries stored by write_summaries(), from a loaded job state."""
        found: Dict[str, Union[str, NoSummary]] = dict()
        try:
            for root, text in state.get('summaries', {}).items():
                found[root] = str(text)
            for root, reason in state.get('no_summary', {}).items():
                found[root] = NoSummary(reason)
        except (AttributeError, ValueError) as e:
            raise StateError(f'Bad summaries in digest job {self.path}: {e}') from e
        return found

    def write_parts(self, parts: Sequence[EmailMessage]) -> None:
        """Store the rendered parts and move the job to the deliver stage."""
        state = self.load()
        if self.parts_dir.exists():
            # Left over from a render that did not finish
            shutil.rmtree(self.parts_dir)
        self.parts_dir.mkdir()
        for number, part in enumerate(parts, start=1):
            (self.parts_dir / f'{number:04d}.eml').write_bytes(part.as_bytes())
        self._save({**state, 'stage': self.RENDERED, 'parts': len(parts)})
        # The parts have everything now
        shutil.rmtree(self.messages_dir, ignore_errors=True)

    def pending(self) -> List[Path]:
        """The parts that were not delivered yet, in order."""
        return sorted(self.parts_dir.glob('*.eml'))

    def clear(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)


# Scheduling

_PERIODS = {'daily': timedelta(days=1), 'weekly': timedelta(weeks=1)}
_WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
_WEEKDAY_NAMES = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
_SEND_AT_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
# Delivery keys that only make sense for digests
DIGEST_KEYS = (
    'schedule',
    'send_at',
    'send_day',
    'send_empty',
    'digest_from',
    'summarizer',
    'max_summaries',
    'summary_instructions',
)


@dataclass(frozen=True)
class DigestSchedule:
    """When a digest delivery sends, and what goes into its email."""

    schedule: str = 'daily'
    send_at: time = time(7, 0)
    # Weekday for weekly digests, 0 is Monday
    send_day: int = 0
    send_empty: bool = False
    from_addr: str = DEFAULT_FROM
    # The [summarizers] entry to use; None makes a plain digest
    summarizer: Optional[str] = None
    # The most new summaries one digest asks for; None means no limit
    max_summaries: Optional[int] = None
    # Added to the summarizer's system prompt, for what this maintainer
    # wants to know about each thread
    summary_instructions: Optional[str] = None

    @classmethod
    def from_config(cls, delivery_name: str, details: Mapping[str, Any]) -> 'DigestSchedule':
        """Read and check the digest keys of a delivery.

        Raises ConfigurationError when a value is wrong.
        """

        def bad(key: str, why: str) -> ConfigurationError:
            return ConfigurationError(f"Delivery '{delivery_name}': {key} {why}")

        schedule = details.get('schedule', 'daily')
        if schedule not in _PERIODS:
            raise bad('schedule', f'must be one of: {", ".join(_PERIODS)} (got {schedule!r})')

        send_at_str = details.get('send_at', '07:00')
        smatch = _SEND_AT_RE.match(send_at_str) if isinstance(send_at_str, str) else None
        if not smatch:
            raise bad('send_at', f"must be a time like '07:00' (got {send_at_str!r})")
        send_at = time(int(smatch.group(1)), int(smatch.group(2)))

        send_day_str = details.get('send_day', 'mon')
        if 'send_day' in details and schedule != 'weekly':
            raise bad('send_day', "only works with schedule = 'weekly'")
        # 'fri' and 'friday' both work, 'f' and 'frisbee' don't
        day_prefix = send_day_str.lower()[:3] if isinstance(send_day_str, str) else ''
        if day_prefix not in _WEEKDAYS or not _WEEKDAY_NAMES[_WEEKDAYS.index(day_prefix)].startswith(
            send_day_str.lower()
        ):
            raise bad('send_day', f"must be a weekday like 'mon' (got {send_day_str!r})")
        send_day = _WEEKDAYS.index(day_prefix)

        send_empty = details.get('send_empty', False)
        if not isinstance(send_empty, bool):
            raise bad('send_empty', f'must be true or false (got {send_empty!r})')

        from_addr = details.get('digest_from', DEFAULT_FROM)
        if not isinstance(from_addr, str) or '@' not in email.utils.parseaddr(from_addr)[1]:
            raise bad('digest_from', f"must be an address like 'korgalore <me@example.org>' (got {from_addr!r})")

        summarizer = details.get('summarizer')
        if summarizer is not None and (not isinstance(summarizer, str) or not summarizer):
            raise bad('summarizer', f'must be the name of a [summarizers] entry (got {summarizer!r})')

        max_summaries = details.get('max_summaries')
        if max_summaries is not None:
            if summarizer is None:
                raise bad('max_summaries', 'only works with a summarizer')
            # bool is an int in Python, but "max_summaries = true" is a mistake
            if isinstance(max_summaries, bool) or not isinstance(max_summaries, int) or max_summaries < 1:
                raise bad('max_summaries', f'must be a whole number of at least 1 (got {max_summaries!r})')

        summary_instructions = details.get('summary_instructions')
        if summary_instructions is not None:
            if summarizer is None:
                raise bad('summary_instructions', 'only works with a summarizer')
            if not isinstance(summary_instructions, str) or not summary_instructions.strip():
                raise bad('summary_instructions', f'must be some text (got {summary_instructions!r})')
            summary_instructions = summary_instructions.strip()

        return cls(
            schedule,
            send_at,
            send_day,
            send_empty,
            from_addr,
            summarizer=summarizer,
            max_summaries=max_summaries,
            summary_instructions=summary_instructions,
        )

    @property
    def needs_worker(self) -> bool:
        """True when the digest is too slow to make inside kgl pull.

        Summaries can take hours with a local model, so these digests are
        finished by the digest worker, outside the feed lock.
        """
        return self.summarizer is not None

    @property
    def period(self) -> timedelta:
        """The normal length of one digest period."""
        return _PERIODS[self.schedule]

    def last_slot(self, now: datetime) -> datetime:
        """The latest scheduled send time at or before now.

        Send times are wall-clock times in the local time zone, so a 07:00
        digest stays at 07:00 when daylight saving time starts or ends.
        """
        local_now = now.astimezone()
        day = local_now.date()
        if self.schedule == 'weekly':
            day -= timedelta(days=(day.weekday() - self.send_day) % 7)
        # A naive datetime's astimezone() uses the local time zone rules
        # for that date, which gives the right UTC offset across DST
        slot = datetime.combine(day, self.send_at).astimezone()
        if slot > local_now:
            day -= self.period
            slot = datetime.combine(day, self.send_at).astimezone()
        return slot

    def is_due(self, last_sent: Optional[datetime], now: datetime) -> bool:
        """True when a digest should go out now.

        A delivery that never sent a digest is due right away, so a new
        setup shows its first digest on the next run.
        """
        return last_sent is None or last_sent < self.last_slot(now)

    def period_start(self, last_sent: Optional[datetime], now: datetime) -> datetime:
        """Where the next digest starts: the last one, or one period back."""
        return last_sent if last_sent is not None else now - self.period
