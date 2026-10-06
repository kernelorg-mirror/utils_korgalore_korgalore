"""Tests for digest threading and thread facts."""

from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from korgalore.digest import (
    DigestThread,
    SeriesInfo,
    Trailer,
    find_trailers,
    group_threads,
    is_bug_report,
    is_patch_posting,
    is_pull_request,
    parse_series,
    strip_reply_prefixes,
)
from tests.digest_helpers import mkmsg


def only(threads: list[DigestThread]) -> DigestThread:
    """Return the single thread in a list."""
    assert len(threads) == 1
    return threads[0]


class TestGroupThreads:
    """Tests for group_threads()."""

    def test_new_thread(self) -> None:
        """A root and its replies form one new thread, in arrival order."""
        thread = only(
            group_threads(
                [
                    mkmsg('root@x', '[PATCH] mm: widget'),
                    mkmsg('r1@x', 'Re: [PATCH] mm: widget', refs=['root@x'], irt='root@x'),
                    mkmsg('r2@x', 'Re: [PATCH] mm: widget', refs=['root@x', 'r1@x'], irt='r1@x'),
                ]
            )
        )
        assert thread.is_new
        assert thread.root_msgid == 'root@x'
        assert thread.subject == '[PATCH] mm: widget'
        assert [update.msgid for update in thread.updates] == ['root@x', 'r1@x', 'r2@x']

    def test_continuing_thread(self) -> None:
        """Replies to a root outside the period form a continuing thread."""
        thread = only(
            group_threads(
                [
                    mkmsg('r5@x', 'Re: [PATCH v3 2/7] mm: tail', refs=['cover@x', 'p2@x'], irt='p2@x'),
                    mkmsg('r6@x', 'Re: [PATCH v3 2/7] mm: tail', refs=['cover@x', 'p2@x', 'r5@x']),
                ]
            )
        )
        assert not thread.is_new
        assert thread.root_msgid == 'cover@x'
        assert thread.subject == '[PATCH v3 2/7] mm: tail'

    def test_root_from_longest_references(self) -> None:
        """A reply with only In-Reply-To must not name a mid-thread root.

        The root (and so the summary cache key and the subject lookup)
        must not depend on which reply arrived first.
        """
        short_first = [
            mkmsg('r6@x', 'Re: [PATCH v3 2/7] mm: tail', irt='r5@x'),
            mkmsg('r7@x', 'Re: [PATCH v3 2/7] mm: tail', refs=['cover@x', 'p2@x', 'r5@x'], irt='r5@x'),
        ]
        for order in (short_first, list(reversed(short_first))):
            thread = only(group_threads(order))
            assert not thread.is_new
            assert thread.root_msgid == 'cover@x'
            assert len(thread.updates) == 2

    def test_linked_through_missing_message(self) -> None:
        """Two replies to the same old message are one thread."""
        thread = only(
            group_threads(
                [
                    mkmsg('a@x', 'Re: old', irt='old@x'),
                    mkmsg('b@x', 'Re: old', irt='old@x'),
                ]
            )
        )
        assert len(thread.updates) == 2
        assert thread.root_msgid == 'old@x'

    def test_chain_without_references(self) -> None:
        """In-Reply-To alone is enough to follow a chain of replies."""
        thread = only(
            group_threads(
                [
                    mkmsg('a@x', 'Re: old', irt='old@x'),
                    mkmsg('b@x', 'Re: old', irt='a@x'),
                    mkmsg('c@x', 'Re: old', irt='b@x'),
                ]
            )
        )
        assert len(thread.updates) == 3

    def test_root_arrives_after_reply(self) -> None:
        """If the root shows up later than a reply, the thread is still new."""
        thread = only(
            group_threads(
                [
                    mkmsg('r1@x', 'Re: hello', irt='root@x'),
                    mkmsg('root@x', 'hello'),
                ]
            )
        )
        assert thread.is_new
        assert thread.root_msgid == 'root@x'
        assert thread.subject == 'hello'
        assert [update.msgid for update in thread.updates] == ['r1@x', 'root@x']

    def test_unrelated_threads_stay_apart(self) -> None:
        """Messages with no links between them are separate threads."""
        threads = group_threads([mkmsg('a@x', 'one'), mkmsg('b@x', 'two')])
        assert len(threads) == 2

    def test_sort_order(self) -> None:
        """Busiest thread first; ties go to the most recent arrival."""
        threads = group_threads(
            [
                mkmsg('quiet@x', 'quiet'),
                mkmsg('busy@x', 'busy'),
                mkmsg('busy1@x', 'Re: busy', irt='busy@x'),
                mkmsg('later@x', 'later'),
            ]
        )
        assert [thread.root_msgid for thread in threads] == ['busy@x', 'later@x', 'quiet@x']

    def test_self_reference_ignored(self) -> None:
        """A message that lists itself in References is still a root."""
        thread = only(group_threads([mkmsg('a@x', 'hello', refs=['a@x'])]))
        assert thread.is_new
        assert thread.root_msgid == 'a@x'

    @pytest.mark.parametrize(
        ('msgs', 'updates'),
        [
            # The same Message-ID twice in the period is one update
            pytest.param([mkmsg('a@x', 'hello'), mkmsg('a@x', 'hello')], [1], id='duplicate-counted-once'),
            # A message with no Message-ID cannot be linked, so it is left out
            pytest.param([mkmsg(None, 'hello')], [], id='no-msgid-skipped'),
            pytest.param([], [], id='empty'),
        ],
    )
    def test_edge_inputs(self, msgs: list[EmailMessage], updates: list[int]) -> None:
        assert [len(thread.updates) for thread in group_threads(msgs)] == updates


class TestThreadFacts:
    """Tests for the facts collected per thread and per update."""

    def test_participants_unique_in_order(self) -> None:
        """Each author is listed once, in order of their first message."""
        thread = only(
            group_threads(
                [
                    mkmsg('a@x', 'hello', sender='Ann <ann@example.org>'),
                    mkmsg('b@x', 'Re: hello', irt='a@x', sender='Bob <bob@example.org>'),
                    mkmsg('c@x', 'Re: hello', irt='b@x', sender='Ann <ANN@example.org>'),
                ]
            )
        )
        assert thread.participants == [('Ann', 'ann@example.org'), ('Bob', 'bob@example.org')]

    @pytest.mark.parametrize(
        ('sender', 'expected'),
        [
            pytest.param('bob@example.org', 'bob@example.org', id='no-display-name'),
            pytest.param('=?utf-8?q?J=C3=BCrgen?= <j@example.org>', 'Jürgen', id='rfc2047-decoded'),
        ],
    )
    def test_author_name(self, sender: str, expected: str) -> None:
        thread = only(group_threads([mkmsg('a@x', 'hello', sender=sender)]))
        assert thread.updates[0].author_name == expected

    def test_patch_count(self) -> None:
        """Patches are counted; replies and the cover letter are not."""
        thread = only(
            group_threads(
                [
                    mkmsg('c@x', '[PATCH 0/2] mm: series'),
                    mkmsg('p1@x', '[PATCH 1/2] mm: one', irt='c@x'),
                    mkmsg('p2@x', '[PATCH 2/2] mm: two', irt='c@x'),
                    mkmsg('r@x', 'Re: [PATCH 1/2] mm: one', irt='p1@x'),
                ]
            )
        )
        assert thread.patch_count == 2
        assert thread.series == SeriesInfo(counter=0, expected=2)

        # A lone patch has counter 0 too, but it is not a cover letter
        assert only(group_threads([mkmsg('p@x', '[PATCH] mm: one')])).patch_count == 1

    def test_reply_trailers_collected(self) -> None:
        """A Reviewed-by in a reply is a new trailer on the thread."""
        body = 'Looks good.\n\nReviewed-by: Bob <bob@example.org>\n'
        thread = only(
            group_threads(
                [
                    mkmsg('p@x', '[PATCH] mm: widget'),
                    mkmsg('r@x', 'Re: [PATCH] mm: widget', body=body, irt='p@x', sender='Bob <bob@example.org>'),
                ]
            )
        )
        assert thread.trailers == [
            Trailer('Reviewed-by', 'Bob <bob@example.org>', 'bob@example.org', 'bob@example.org')
        ]
        assert thread.trailers[0].from_sender

    def test_patch_trailers_ignored(self) -> None:
        """Trailers inside a patch were given earlier, so they are not new."""
        body = 'Fix it.\n\nReviewed-by: Bob <bob@example.org>\nSigned-off-by: P <p@example.org>\n'
        thread = only(group_threads([mkmsg('p@x', '[PATCH v2] mm: widget', body=body)]))
        assert thread.trailers == []

    def test_trailers_deduplicated(self) -> None:
        """The same trailer sent twice is listed once."""
        body = 'Acked-by: Bob <bob@example.org>\n'
        thread = only(
            group_threads(
                [
                    mkmsg('a@x', 'Re: x', body=body, irt='p@x', sender='bob@example.org'),
                    mkmsg('b@x', 'Re: x', body=body, irt='p@x', sender='bob@example.org'),
                ]
            )
        )
        assert len(thread.trailers) == 1

    def test_dates(self) -> None:
        """Dates are parsed with their timezone; bad or missing ones are None."""
        threads = group_threads(
            [
                mkmsg('a@x', 'one', date='Thu, 01 Oct 2026 09:12:00 +0200'),
                mkmsg('b@x', 'two', date='Thu, 01 Oct 2026 09:12:00'),
                mkmsg('c@x', 'three', date='not a date'),
                mkmsg('d@x', 'four', date=None),
            ]
        )
        dates = {thread.root_msgid: thread.updates[0].date for thread in threads}
        assert dates['a@x'] == datetime(2026, 10, 1, 9, 12, tzinfo=timezone(timedelta(hours=2)))
        assert dates['b@x'] == datetime(2026, 10, 1, 9, 12, tzinfo=UTC)
        assert dates['c@x'] is None
        assert dates['d@x'] is None


class TestFindTrailers:
    """Tests for find_trailers()."""

    def test_names_are_normalized(self) -> None:
        """Trailer names get their usual spelling, and NAKed-by means Nacked-by."""
        body = (
            'reviewed-by: A <a@example.org>\n'
            'ACKED-BY: B <b@example.org>\n'
            'Tested-by: C <c@example.org>\n'
            'NAKed-by: D <d@example.org>\n'
            'Nacked-by: E <e@example.org>\n'
        )
        names = [trailer.name for trailer in find_trailers(body, 'x@example.org')]
        assert names == ['Reviewed-by', 'Acked-by', 'Tested-by', 'Nacked-by', 'Nacked-by']

    @pytest.mark.parametrize(
        'body',
        [
            pytest.param('> Reviewed-by: A <a@example.org>\n', id='quoted'),
            pytest.param('  Reviewed-by: A <a@example.org>\n', id='indented'),
            pytest.param(
                'Signed-off-by: A <a@example.org>\nReported-by: B <b@example.org>\n',
                id='not-review-trailers',
            ),
            pytest.param('Acked-by: me\n', id='no-address'),
        ],
    )
    def test_ignored(self, body: str) -> None:
        """Quoted or indented lines, non-review trailers and ones without an address are not trailers."""
        assert find_trailers(body, 'x@example.org') == []

    def test_bare_address(self) -> None:
        """A trailer with only an address is accepted."""
        trailers = find_trailers('Acked-by: Bob@Example.org\n', 'bob@example.org')
        assert trailers[0].email == 'bob@example.org'
        assert trailers[0].from_sender

    def test_trailer_from_someone_else(self) -> None:
        """A trailer naming a different person than the sender is marked."""
        trailers = find_trailers('Acked-by: Famous Person <famous@example.org>\n', 'troll@example.org')
        assert not trailers[0].from_sender


class TestParseSeries:
    """Tests for parse_series() and friends."""

    @pytest.mark.parametrize(
        ('subject', 'expected'),
        [
            ('[PATCH] mm: x', SeriesInfo()),
            ('[PATCH v3 2/7] mm: x', SeriesInfo(version=3, counter=2, expected=7)),
            ('[PATCH 0/4] mm: x', SeriesInfo(counter=0, expected=4)),
            ('[RFC PATCH net-next v2 1/3] net: x', SeriesInfo(version=2, counter=1, expected=3, rfc=True)),
            ('[RFC] mm: idea', SeriesInfo(rfc=True)),
            ('[PATCHv4] mm: x', SeriesInfo(version=4)),
            ('[PATCH RESEND v2] mm: x', SeriesInfo(version=2, resend=True)),
            ('[PATCH 6.1.y] [PATCH v2 3/5] mm: x', SeriesInfo(version=2, counter=3, expected=5)),
            ('Re: [PATCH v2] mm: x', SeriesInfo(version=2)),
            ('[patch v0] odd', SeriesInfo()),
        ],
    )
    def test_patch_subjects(self, subject: str, expected: SeriesInfo) -> None:
        """Version, counter and flags come from the bracketed prefixes."""
        assert parse_series(subject) == expected

    @pytest.mark.parametrize(
        'subject',
        ['mm: no prefix', '[GIT PULL] mm updates', '[ANNOUNCE] v6.18', 'Re: question', ''],
    )
    def test_not_patches(self, subject: str) -> None:
        """Subjects without a PATCH or RFC prefix have no series info."""
        assert parse_series(subject) is None

    @pytest.mark.parametrize(
        ('subject', 'expected'),
        [
            ('[PATCH 0/3] mm: x', True),
            ('Re: [PATCH 0/3] mm: x', False),
            ('mm: x', False),
        ],
    )
    def test_is_patch_posting(self, subject: str, expected: bool) -> None:
        """Patches and cover letters are postings; replies are not."""
        assert is_patch_posting(subject) is expected

    @pytest.mark.parametrize(
        ('subject', 'expected'),
        [
            ('Re: Re: hello', 'hello'),
            ('Fwd: Re: [PATCH] x', '[PATCH] x'),
            ('AW: hello', 'hello'),
            ('RE[2]: hello', 'hello'),
            ('hello', 'hello'),
            ('mm: fix the widget', 'mm: fix the widget'),
            ('Re: net: fix the socket', 'net: fix the socket'),
            ('bpf: x', 'bpf: x'),
        ],
    )
    def test_strip_reply_prefixes(self, subject: str, expected: str) -> None:
        """Reply and forward prefixes are removed; patch prefixes stay."""
        assert strip_reply_prefixes(subject) == expected
        # A reply without its root gets the stripped subject as the thread subject
        thread = group_threads([mkmsg('a@x', subject, irt='old@x')])[0]
        assert thread.subject == expected


@pytest.mark.parametrize(
    ('subject', 'expected'),
    [
        ('[GIT PULL] mm fixes', True),
        ('[GIT PULL v2] mm fixes', True),
        ('[PULL] drm fixes', True),
        ('[git,pull] drm fixes', True),
        ('Re: [GIT PULL] mm fixes', True),
        ('[PATCH] git: fix pull with rebase', False),
        ('[PATCH] pull-up resistor driver', False),
        ('[PATCH v2 pullup] gpio: fix the bias', False),
        ('Please pull my tree', False),
    ],
)
def test_pull_request(subject: str, expected: bool) -> None:
    """Only a "pull" inside the first brackets makes a pull request."""
    assert is_pull_request(subject) is expected


@pytest.mark.parametrize(
    ('subject', 'expected'),
    [
        ('[BUG] mm: oops in frob()', True),
        ('BUG: unable to handle page fault in frob', True),
        ('kernel BUG_ON hit in mm/widget.c', True),
        ('Possible bug in the widget code', True),
        ('Re: two bugs in git rebase', True),
        ('[Bug 220001] New: frob() hangs', True),
        ('debugfs: add a widget file', False),
        ('bugfix release plans', False),
        ('mm: why is this slow?', False),
    ],
)
def test_bug_report(subject: str, expected: bool) -> None:
    """Only "bug" or "bugs" as a word makes a bug report."""
    assert is_bug_report(subject) is expected
