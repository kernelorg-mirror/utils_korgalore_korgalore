"""Tests for digest thread shrinking."""

from email.message import EmailMessage
from typing import Optional

import pytest
from liblore.utils import msg_get_payload

from korgalore.digest import MARKER_MAX_FILES, shrink_thread, strip_diffs, strip_review_trailers

FORMAT_PATCH = """\
The widget frobnicator walks the whole list on every call. Keep a
pointer to the last entry instead.

Reviewed-by: A. Reviewer <a@example.org>
Signed-off-by: P. Author <p@example.org>
---
 mm/widget.c | 5 +++--
 1 file changed, 3 insertions(+), 2 deletions(-)

diff --git a/mm/widget.c b/mm/widget.c
index 1234567..89abcde 100644
--- a/mm/widget.c
+++ b/mm/widget.c
@@ -10,7 +10,8 @@ static int frob(struct widget *w)
 {
-	struct widget *p = w->head;
-	while (p->next)
+	struct widget *p = w->tail;
+
+	if (!p)
 		return -ENOENT;
 	return 0;
 }
--\x20
2.47.0
"""

COVER_LETTER = """\
This series reworks the widget frobnicator.

Changes in v3:
- drop patch 4, it was merged already

P. Author (2):
  mm: keep a widget tail pointer
  mm: use the tail pointer in frob()

 mm/widget.c | 12 ++++++++----
 1 file changed, 8 insertions(+), 4 deletions(-)

--\x20
2.47.0
"""

REVIEW_REPLY = """\
On Mon, P. Author wrote:
> diff --git a/mm/widget.c b/mm/widget.c
> --- a/mm/widget.c
> +++ b/mm/widget.c
> @@ -10,7 +10,8 @@
> -	struct widget *p = w->head;
> +	struct widget *p = w->tail;

This needs a lock, w->tail can change under us.
"""


def make_msg(
    body: str, subject: str = '[PATCH] mm: widget', charset: Optional[str] = None, raw_body: Optional[bytes] = None
) -> EmailMessage:
    """Build a simple text/plain message."""
    msg = EmailMessage()
    msg['From'] = 'P. Author <p@example.org>'
    msg['To'] = 'linux-mm@kvack.org'
    msg['Subject'] = subject
    msg['Message-ID'] = '<20261001.1@example.org>'
    msg['X-Mailer'] = 'git-send-email 2.47.0'
    if raw_body is not None:
        msg.set_payload(raw_body)
        msg['Content-Type'] = f'text/plain; charset="{charset}"'
        msg['Content-Transfer-Encoding'] = '8bit'
    else:
        msg.set_content(body)
    return msg


class TestStripDiffs:
    """Tests for strip_diffs()."""

    def test_format_patch(self) -> None:
        """Commit message, trailers and diffstat stay; the diff becomes a marker.

        The marker counts the diff lines and names the file, and the "-- "
        signature marker after the diff is not counted as a removed line.
        """
        result = strip_diffs(FORMAT_PATCH)
        assert 'Keep a\npointer to the last entry instead.' in result
        assert 'Reviewed-by: A. Reviewer <a@example.org>' in result
        assert ' mm/widget.c | 5 +++--' in result
        assert ' 1 file changed, 3 insertions(+), 2 deletions(-)' in result
        assert 'w->tail' not in result
        assert '@@' not in result
        assert result.endswith('[diff: 14 lines; 1 file: mm/widget.c]\n-- \n2.47.0\n')

    @pytest.mark.parametrize(
        'body',
        [
            pytest.param(COVER_LETTER, id='cover-letter-with-diffstat'),
            pytest.param(REVIEW_REPLY, id='quoted-diff'),
            pytest.param('I think this is fine.\n\n- one\n- two\n', id='plain-text'),
            # The "---" separator line must not start a diff by itself
            pytest.param('Fix it.\n---\n x.c | 2 +-\n', id='separator-alone'),
            # A "--- " line that is not followed by "+++ " is normal text
            pytest.param('--- cut here ---\nsome log output\n', id='minus-without-plus'),
        ],
    )
    def test_text_without_diff_unchanged(self, body: str) -> None:
        """Bodies without a diff pass through, newlines and all."""
        assert strip_diffs(body) == body

    def test_inline_patch_keeps_text_after(self) -> None:
        """Text written after an inline patch is kept."""
        body = (
            'Something like this?\n'
            '\n'
            'diff --git a/mm/widget.c b/mm/widget.c\n'
            '--- a/mm/widget.c\n'
            '+++ b/mm/widget.c\n'
            '@@ -1,2 +1,2 @@\n'
            '-old\n'
            '+new\n'
            '\n'
            'Then we can drop the lock below.\n'
        )
        result = strip_diffs(body)
        assert result == (
            'Something like this?\n\n[diff: 6 lines; 1 file: mm/widget.c]\n\nThen we can drop the lock below.\n'
        )

    def test_empty_context_line_inside_hunk(self) -> None:
        """An empty line inside a hunk does not end the diff."""
        body = 'diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n'
        assert strip_diffs(body) == '[diff: 8 lines; 1 file: x.c]\n'

    def test_patch_without_separator(self) -> None:
        """A patch with no "---" line and no diffstat is still found."""
        body = (
            'Fix the thing.\n'
            '\n'
            'Signed-off-by: P. Author <p@example.org>\n'
            'diff --git a/x.c b/x.c\n'
            '--- a/x.c\n'
            '+++ b/x.c\n'
            '@@ -1 +1 @@\n'
            '-a\n'
            '+b\n'
        )
        result = strip_diffs(body)
        assert result == ('Fix the thing.\n\nSigned-off-by: P. Author <p@example.org>\n[diff: 6 lines; 1 file: x.c]\n')

    def test_plain_unified_diff(self) -> None:
        """A diff -u style patch without a "diff" header line is found."""
        body = 'Try this:\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-a\n+b\n'
        assert strip_diffs(body) == 'Try this:\n[diff: 5 lines; 1 file: x.c]\n'

    def test_quilt_index_diff(self) -> None:
        """A quilt-style patch starting with "Index:" is found."""
        body = (
            'Index: linux/x.c\n'
            '===================================================================\n'
            '--- linux.orig/x.c\n'
            '+++ linux/x.c\n'
            '@@ -1 +1 @@\n'
            '-a\n'
            '+b\n'
        )
        assert strip_diffs(body) == '[diff: 7 lines; 1 file: linux/x.c]\n'

    def test_deleted_and_renamed_files(self) -> None:
        """Deleted files use their old name and renames use their new name."""
        body = (
            'diff --git a/old.c b/old.c\n'
            'deleted file mode 100644\n'
            'index 1234567..0000000\n'
            '--- a/old.c\n'
            '+++ /dev/null\n'
            '@@ -1 +0,0 @@\n'
            '-gone\n'
            'diff --git a/before.c b/after.c\n'
            'similarity index 100%\n'
            'rename from before.c\n'
            'rename to after.c\n'
        )
        assert strip_diffs(body) == '[diff: 11 lines; 2 files: old.c, after.c]\n'

    def test_deleted_file_without_git_header(self) -> None:
        """Without a "diff --git" line, a deleted file still gets its old name."""
        body = '--- a/old.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n'
        assert strip_diffs(body) == '[diff: 4 lines; 1 file: old.c]\n'

    def test_binary_patch(self) -> None:
        """Base85 data of a binary patch is part of the diff."""
        body = (
            'diff --git a/logo.png b/logo.png\n'
            'new file mode 100644\n'
            'index 0000000..1234567\n'
            'GIT binary patch\n'
            'literal 12\n'
            'TcmZ?wbhEHbWMp7q_{zWl\n'
            '\n'
            'literal 0\n'
            'HcmV?d00001\n'
            '\n'
            'Looks good to me.\n'
        )
        assert strip_diffs(body) == ('[diff: 9 lines; 1 file: logo.png]\n\nLooks good to me.\n')

    def test_two_diffs_with_text_between(self) -> None:
        """Each diff gets its own marker and the text between them stays."""
        body = 'First:\ndiff --git a/a.c b/a.c\n-x\n+y\nSecond:\ndiff --git a/b.c b/b.c\n-x\n+y\n'
        assert strip_diffs(body) == ('First:\n[diff: 3 lines; 1 file: a.c]\nSecond:\n[diff: 3 lines; 1 file: b.c]\n')

    def test_marker_limits_file_list(self) -> None:
        """Long file lists are cut off with "and N more"."""
        extra = 3
        count = MARKER_MAX_FILES + extra
        body = ''.join(f'diff --git a/f{i}.c b/f{i}.c\n+x\n' for i in range(count))
        result = strip_diffs(body)
        assert f'{count} files: ' in result
        assert f'f{MARKER_MAX_FILES - 1}.c, and {extra} more]' in result
        assert f'f{MARKER_MAX_FILES}.c' not in result

    def test_no_trailing_newline(self) -> None:
        """A body without a final newline does not gain one."""
        assert strip_diffs('diff --git a/x b/x\n+y') == '[diff: 2 lines; 1 file: x]'


class TestStripReviewTrailers:
    """Tests for strip_review_trailers()."""

    def test_every_kind_dropped(self) -> None:
        """All the review trailers go, in any case."""
        body = (
            'Fix the widget.\n'
            '\n'
            'Reviewed-by: A <a@example.org>\n'
            'ACKED-BY: B <b@example.org>\n'
            'Tested-by: C <c@example.org>\n'
            'Nacked-by: D <d@example.org>\n'
            'Signed-off-by: P. Author <p@example.org>\n'
        )
        assert strip_review_trailers(body) == 'Fix the widget.\n\nSigned-off-by: P. Author <p@example.org>\n'

    def test_quoted_and_inline_kept(self) -> None:
        """Quoted trailers and words in a sentence are not trailers."""
        body = '> Reviewed-by: A <a@example.org>\nI Reviewed-by: hand, see above.\n'
        assert strip_review_trailers(body) == body

    def test_last_line_without_newline(self) -> None:
        """A trailer on the last line goes, and the text before it stays."""
        assert strip_review_trailers('Text.\nAcked-by: B <b@example.org>') == 'Text.\n'


class TestShrinkThread:
    """Tests for shrink_thread()."""

    def test_patch_is_stripped_and_footer_dropped(self) -> None:
        """Diffs are replaced and the git version footer is removed."""
        result = shrink_thread([make_msg(FORMAT_PATCH)])
        assert len(result) == 1
        body = msg_get_payload(result[0], strip_signature=False)
        assert '[diff: 14 lines; 1 file: mm/widget.c]' in body
        assert '2.47.0' not in body

    def test_patch_loses_review_trailers(self) -> None:
        """Trailers carried from older versions do not reach the model."""
        result = shrink_thread([make_msg(FORMAT_PATCH, subject='[PATCH v2 1/2] mm: widget')])
        body = msg_get_payload(result[0], strip_signature=False)
        assert 'Reviewed-by' not in body
        assert 'Signed-off-by: P. Author <p@example.org>' in body
        assert 'Keep a\npointer to the last entry instead.\n\nSigned-off-by' in body

    def test_cover_letter_loses_review_trailers(self) -> None:
        """A cover letter that lists the reviews so far is cleaned too."""
        body = 'This series reworks the widgets.\n\nTested-by: T. Ester <t@example.org>\n'
        result = shrink_thread([make_msg(body, subject='[PATCH v2 0/2] mm: widgets')])
        assert msg_get_payload(result[0]) == 'This series reworks the widgets.\n\n'

    def test_reply_keeps_review_trailers(self) -> None:
        """A reviewer's own trailer stays: it is part of what they said."""
        body = 'Looks good.\n\nReviewed-by: A. Reviewer <a@example.org>\n'
        result = shrink_thread([make_msg(body, subject='Re: [PATCH v2 1/2] mm: widget')])
        assert 'Reviewed-by: A. Reviewer <a@example.org>' in msg_get_payload(result[0])

    def test_discussion_keeps_trailer_lines(self) -> None:
        """Only patches and cover letters lose them."""
        body = 'Is this how to write it?\nAcked-by: Some One <s@example.org>\n'
        result = shrink_thread([make_msg(body, subject='How do trailers work?')])
        assert 'Acked-by: Some One' in msg_get_payload(result[0])

    def test_extra_headers_dropped(self) -> None:
        """minimize_thread() still removes headers we do not need."""
        result = shrink_thread([make_msg(FORMAT_PATCH)])
        assert result[0]['Subject'] == '[PATCH] mm: widget'
        assert result[0]['X-Mailer'] is None

    def test_deep_quotes_dropped(self) -> None:
        """Quotes of quotes are removed by minimize_thread()."""
        body = '> > very old\n> old\n\nNew reply.\n'
        result = shrink_thread([make_msg(body, subject='Re: [PATCH] mm: widget')])
        reply = msg_get_payload(result[0])
        assert 'very old' not in reply
        assert '> old' in reply
        assert 'New reply.' in reply

    def test_input_not_changed(self) -> None:
        """The caller's messages are not modified."""
        msg = make_msg(FORMAT_PATCH)
        shrink_thread([msg])
        assert 'w->tail' in msg_get_payload(msg)
        assert msg['X-Mailer'] == 'git-send-email 2.47.0'

    def test_quote_only_message_dropped(self) -> None:
        """A message with nothing but a bottom quote disappears."""
        result = shrink_thread([make_msg('> just a quote\n', subject='Re: x')])
        assert result == []

    def test_non_utf8_body(self) -> None:
        """A latin-1 message is decoded and comes out as UTF-8 text."""
        raw = 'Gr\xfc\xdfe, the patch works.\n'.encode('latin-1')
        result = shrink_thread([make_msg('', charset='iso-8859-1', raw_body=raw)])
        assert msg_get_payload(result[0]) == 'Grüße, the patch works.\n'
