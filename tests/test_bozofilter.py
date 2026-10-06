"""Tests for the bozofilter module."""

from pathlib import Path
from typing import cast

import pytest

from korgalore.bozofilter import (
    add_to_bozofilter,
    extract_email_address,
    is_bozofied,
    load_bozofilter,
)


class TestLoadBozofilter:
    """Tests for load_bozofilter function."""

    @pytest.mark.parametrize('content', [None, ''], ids=['file-missing', 'file-empty'])
    def test_empty(self, tmp_path: Path, content: str | None) -> None:
        """Returns empty set when the bozofilter file is missing or empty."""
        if content is not None:
            (tmp_path / 'bozofilter.txt').write_text(content)
        assert load_bozofilter(tmp_path) == set()

    def test_lowercases_addresses(self, tmp_path: Path) -> None:
        """Normalizes addresses to lowercase."""
        content = 'SPAM@EXAMPLE.COM\nTroll@Example.Org\n'
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {'spam@example.com', 'troll@example.org'}

    def test_mixed_content(self, tmp_path: Path) -> None:
        """Handles mixed comments, blank lines, and addresses."""
        content = """# Bozofilter
# Last updated: 2026-01-15

spam@example.com # sends junk patches
troll@example.org

# Bots
bot1@example.net # automated spam
bot2@example.net
"""
        (tmp_path / 'bozofilter.txt').write_text(content)
        result = load_bozofilter(tmp_path)
        assert result == {
            'spam@example.com',
            'troll@example.org',
            'bot1@example.net',
            'bot2@example.net',
        }


class TestAddToBozofilter:
    """Tests for add_to_bozofilter function."""

    def test_creates_file_with_single_address(self, tmp_path: Path) -> None:
        """Creates the bozofilter file if missing and adds the address to it."""
        added = add_to_bozofilter(tmp_path, ['spam@example.com'])
        assert added == 1
        assert (tmp_path / 'bozofilter.txt').exists()
        result = load_bozofilter(tmp_path)
        assert 'spam@example.com' in result

    def test_adds_multiple_addresses(self, tmp_path: Path) -> None:
        """Adds multiple addresses."""
        added = add_to_bozofilter(tmp_path, ['a@example.com', 'b@example.com'])
        assert added == 2
        result = load_bozofilter(tmp_path)
        assert result == {'a@example.com', 'b@example.com'}

    def test_skips_existing_addresses(self, tmp_path: Path) -> None:
        """Doesn't add addresses that already exist."""
        (tmp_path / 'bozofilter.txt').write_text('existing@example.com\n')
        added = add_to_bozofilter(tmp_path, ['existing@example.com', 'new@example.com'])
        assert added == 1
        result = load_bozofilter(tmp_path)
        assert result == {'existing@example.com', 'new@example.com'}

    def test_includes_reason_in_comment(self, tmp_path: Path) -> None:
        """Includes reason in the trailing comment."""
        add_to_bozofilter(tmp_path, ['spam@example.com'], reason='sends junk')
        content = (tmp_path / 'bozofilter.txt').read_text()
        assert 'sends junk' in content

    def test_includes_date_in_comment(self, tmp_path: Path) -> None:
        """Includes date in the trailing comment."""
        add_to_bozofilter(tmp_path, ['spam@example.com'])
        content = (tmp_path / 'bozofilter.txt').read_text()
        assert 'added on' in content

    def test_lowercases_when_adding(self, tmp_path: Path) -> None:
        """Normalizes addresses to lowercase when adding."""
        add_to_bozofilter(tmp_path, ['SPAM@EXAMPLE.COM'])
        result = load_bozofilter(tmp_path)
        assert 'spam@example.com' in result

    def test_skips_empty_addresses(self, tmp_path: Path) -> None:
        """Skips empty or whitespace-only addresses."""
        added = add_to_bozofilter(tmp_path, ['', '  ', 'valid@example.com'])
        assert added == 1
        result = load_bozofilter(tmp_path)
        assert result == {'valid@example.com'}


class TestExtractEmailAddress:
    """Tests for extract_email_address function."""

    @pytest.mark.parametrize(
        ('header', 'expected'),
        [
            pytest.param('John Doe <john@example.com>', 'john@example.com', id='angle-brackets'),
            pytest.param('john@example.com', 'john@example.com', id='bare-address'),
            pytest.param('JOHN@EXAMPLE.COM', 'john@example.com', id='lowercased'),
            pytest.param('"Doe, John" <john@example.com>', 'john@example.com', id='complex-name'),
            pytest.param('<john@example.com>', 'john@example.com', id='no-name'),
            pytest.param('', None, id='empty'),
            # Passing None is not part of the signature, but callers feed this
            # straight from header lookups, so the guard has to hold.
            pytest.param(cast('str', None), None, id='none'),
        ],
    )
    def test_extract(self, header: str, expected: str | None) -> None:
        assert extract_email_address(header) == expected


class TestIsBozofied:
    """Tests for is_bozofied function."""

    @pytest.mark.parametrize(
        ('header', 'bozo', 'expected'),
        [
            pytest.param('spam@example.com', set(), False, id='empty-filter'),
            pytest.param('spam@example.com', {'spam@example.com'}, True, id='exact-address'),
            pytest.param('Spammer <spam@example.com>', {'spam@example.com'}, True, id='display-name'),
            pytest.param('SPAM@EXAMPLE.COM', {'spam@example.com'}, True, id='upper-case'),
            pytest.param('Spammer <SPAM@Example.Com>', {'spam@example.com'}, True, id='upper-case-display-name'),
            pytest.param('good@example.com', {'spam@example.com'}, False, id='no-match'),
            pytest.param('Good User <good@example.com>', {'spam@example.com'}, False, id='no-match-display-name'),
            pytest.param('', {'spam@example.com'}, False, id='empty-header'),
        ],
    )
    def test_is_bozofied(self, header: str, bozo: set[str], expected: bool) -> None:
        assert is_bozofied(header, bozo) is expected
