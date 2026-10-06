"""Tests for CLI delivery mapping and subfolder template handling."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest

from korgalore import ConfigurationError
from korgalore.cli import map_deliveries, refresh_subfolder_templates
from korgalore.imap_target import ImapTarget
from korgalore.maildir_target import MaildirTarget
from tests.digest_helpers import make_ctx


def run_map(tmp_path: Path, target: str = 'maildir', **delivery: Any) -> click.Context:
    """Run map_deliveries for one delivery against a maildir or imap target.

    Extra keyword arguments (subfolder, labels) become delivery settings.
    Returns the context so callers can inspect ctx.obj.
    """
    if target == 'maildir':
        maildir_path = tmp_path / 'mail'
        target_cfg: dict[str, Any] = {'type': 'maildir', 'path': str(maildir_path)}
        target_obj: Any = MaildirTarget('local', str(maildir_path))
        name = 'local'
    else:
        pw_file = tmp_path / 'password.txt'
        pw_file.write_text('secret')
        target_cfg = {
            'type': 'imap',
            'server': 'imap.example.com',
            'username': 'user@example.com',
            'password_file': str(pw_file),
        }
        target_obj = ImapTarget('imap-server', 'imap.example.com', 'user@example.com', password_file=str(pw_file))
        name = 'imap-server'

    ctx = make_ctx(
        {
            'config': {'targets': {name: target_cfg}, 'feeds': {}},
            'targets': {name: target_obj},
            'feeds': {},
            'deliveries': {},
        }
    )
    deliveries = {'test-delivery': {'feed': 'https://lore.kernel.org/test', 'target': name, **delivery}}
    with patch('korgalore.cli.get_feed_for_delivery') as mock_feed:
        mock_feed.return_value = MagicMock(feed_key='test')
        map_deliveries(ctx, deliveries)
    return ctx


class TestSubfolderTemplateMaildir:
    """Tests for strftime template expansion in Maildir subfolders."""

    @pytest.mark.parametrize('template', ['%Y/%m', 'Archive/%Y/%m/%d'], ids=['year-month', 'nested-archive'])
    def test_strftime_template_expanded_and_stored(self, tmp_path: Path, template: str) -> None:
        """Templates are expanded now, and the original is kept for GUI refresh."""
        ctx = run_map(tmp_path, subfolder=template)

        _, _, _, subfolder = ctx.obj['deliveries']['test-delivery']
        # Expanded the same way production does
        assert subfolder == datetime.now().astimezone().strftime(template)
        assert ctx.obj['subfolder_templates']['test-delivery'] == template

    def test_refresh_subfolder_templates(self, tmp_path: Path) -> None:
        """refresh_subfolder_templates re-expands stored templates."""
        ctx = run_map(tmp_path, subfolder='%Y-%m-%d_%H')

        _, _, _, initial_subfolder = ctx.obj['deliveries']['test-delivery']
        assert re.match(r'^\d{4}-\d{2}-\d{2}_\d{2}$', initial_subfolder)

        # Refresh should re-expand (will be same if run immediately)
        refresh_subfolder_templates(ctx)

        _, _, _, refreshed_subfolder = ctx.obj['deliveries']['test-delivery']
        # Re-expanding the stored template must not have degraded it to the
        # raw '%Y-%m-%d_%H' string.
        assert re.match(r'^\d{4}-\d{2}-\d{2}_\d{2}$', refreshed_subfolder)
        assert refreshed_subfolder == initial_subfolder


class TestSubfolderMapping:
    """Subfolder values that are accepted, and what they map to."""

    @pytest.mark.parametrize('target', ['maildir', 'imap'], ids=['maildir', 'imap'])
    def test_subfolder_without_template_unchanged(self, tmp_path: Path, target: str) -> None:
        """Subfolder without % is not treated as template, on any target."""
        ctx = run_map(tmp_path, target, subfolder='Lists/LKML')

        _, _, _, subfolder = ctx.obj['deliveries']['test-delivery']
        assert subfolder == 'Lists/LKML'
        assert 'test-delivery' not in ctx.obj['subfolder_templates']

    def test_empty_subfolder_treated_as_none(self, tmp_path: Path) -> None:
        ctx = run_map(tmp_path, subfolder='')

        _, _, _, subfolder = ctx.obj['deliveries']['test-delivery']
        assert subfolder is None

    def test_labels_without_percent_allowed(self, tmp_path: Path) -> None:
        ctx = run_map(tmp_path, labels=['INBOX', 'Lists/LKML'])

        _, _, labels, _ = ctx.obj['deliveries']['test-delivery']
        assert labels == ['INBOX', 'Lists/LKML']


class TestMapDeliveriesRejections:
    """Delivery settings that map_deliveries refuses."""

    @pytest.mark.parametrize(
        ('target', 'delivery', 'messages'),
        [
            (
                'imap',
                {'subfolder': '%Y/%m'},
                ['strftime templates in subfolder are only supported for Maildir', 'ImapTarget'],
            ),
            (
                'maildir',
                {'labels': ['INBOX', 'Archive/%Y']},
                ['strftime templates in labels are not supported', 'Archive/%Y'],
            ),
            ('maildir', {'subfolder': ['Lists', 'LKML']}, ['must be a string, not a list']),
        ],
        ids=['imap-subfolder-template', 'labels-percent', 'subfolder-list'],
    )
    def test_rejected(self, tmp_path: Path, target: str, delivery: dict[str, Any], messages: list[str]) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            run_map(tmp_path, target, **delivery)

        for message in messages:
            assert message in str(exc_info.value)
