"""Tests for thread yanking and duplicate suppression.

A thread fetched from lore can contain the same message several times
over, once per archived mailing list, because a patch series is routinely
copied to more than one list. Yanking such a thread used to deliver every
copy. perform_yank() now hands the mbox to liblore's
split_and_dedupe_as_bytes(), which keeps one copy per Message-ID and
prefers the copy from the source least likely to have mangled it.
"""

from unittest.mock import MagicMock, patch

import click

from korgalore.cli import perform_yank
from tests.digest_helpers import make_ctx


def _mbox_message(msgid: str, listid: str, body: str) -> bytes:
    """Build one mboxrd entry with the headers dedupe cares about."""
    return (
        b'From mboxrd@z Thu Jan  1 00:00:00 1970\n'
        b'From: Dev <dev@example.com>\n'
        b'Subject: [PATCH] crypto: fix the thing\n'
        + f'Message-ID: <{msgid}>\n'.encode()
        + f'List-Id: <{listid}>\n'.encode()
        + b'\n'
        + body.encode()
        + b'\n'
    )


def _make_context() -> click.Context:
    """Create a minimal Click context for perform_yank."""
    return make_ctx({'config': {'targets': {}}, 'targets': {}, 'hide_bar': True})


def _imported(target: MagicMock) -> list[bytes]:
    """Return the raw messages handed to import_message, in order."""
    return [call.args[0] for call in target.import_message.call_args_list]


class TestYankThreadDeduplication:
    """A cross-posted thread is delivered once per Message-ID."""

    @patch('korgalore.cli.close_requests_session')
    @patch('korgalore.cli.get_lore_node')
    @patch('korgalore.cli.get_target')
    def test_crossposted_message_delivered_once(
        self, mock_get_target: MagicMock, mock_get_node: MagicMock, mock_close: MagicMock
    ) -> None:
        """The same Message-ID arriving from two lists yields one delivery, in mbox order."""
        mbox = b''.join(
            _mbox_message(f'msg{n}@example.com', 'linux-crypto.vger.kernel.org', f'body {n}') for n in range(1, 4)
        )
        # The cross-posted copy of the first message, from another list.
        mbox += _mbox_message('msg1@example.com', 'crypto.lists.example.com', 'body 1')

        target = MagicMock()
        mock_get_target.return_value = target
        mock_get_node.return_value.get_mbox_by_msgid.return_value = mbox

        uploaded, failed = perform_yank(_make_context(), 'local', '<msg1@example.com>', thread=True, labels_list=[])

        assert (uploaded, failed) == (3, 0)
        delivered = _imported(target)
        assert len(delivered) == 3
        # Deduplication must not reorder a thread
        assert [b'body 1' in delivered[0], b'body 2' in delivered[1], b'body 3' in delivered[2]] == [True] * 3

    @patch('korgalore.cli.close_requests_session')
    @patch('korgalore.cli.get_lore_node')
    @patch('korgalore.cli.get_target')
    def test_preferred_list_copy_wins_over_earlier_copy(
        self, mock_get_target: MagicMock, mock_get_node: MagicMock, mock_close: MagicMock
    ) -> None:
        """A later kernel.org copy replaces an earlier copy from elsewhere.

        This is why plain first-wins dedupe is not good enough: the copy
        that happens to come first in the mbox may be the mangled one.
        """
        mbox = _mbox_message('dup@example.com', 'crypto.lists.example.com', 'MANGLED by the list')
        mbox += _mbox_message('dup@example.com', 'linux-crypto.vger.kernel.org', 'pristine copy')

        target = MagicMock()
        mock_get_target.return_value = target
        mock_get_node.return_value.get_mbox_by_msgid.return_value = mbox

        uploaded, failed = perform_yank(_make_context(), 'local', '<dup@example.com>', thread=True, labels_list=[])

        assert (uploaded, failed) == (1, 0)
        delivered = _imported(target)
        assert len(delivered) == 1
        assert b'pristine copy' in delivered[0]
        assert b'MANGLED' not in delivered[0]
