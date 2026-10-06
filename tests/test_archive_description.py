"""Tests for write_archive_description.

lei's `-o v2:' writer never creates the `description' file public-inbox reads,
so archives show up in web listings as "($INBOX_DIR/description missing)".
These tests cover the file we write in its place: public-inbox treats it as a
single whitespace-collapsed line, and a description is cosmetic enough that
failing to write one must never raise.
"""

from pathlib import Path

import pytest

from korgalore.tracking import write_archive_description


def read_description(archive: Path) -> str:
    return (archive / 'description').read_text(encoding='utf-8')


class TestWriteArchiveDescription:
    """Writing public-inbox descriptions into lei-created v2 archives."""

    @pytest.mark.parametrize(
        ('given', 'expected'),
        [
            pytest.param('DOCUMENTATION patches', 'DOCUMENTATION patches\n', id='single-trailing-newline'),
            # public-inbox flattens the file anyway, so write it already flat
            pytest.param(
                '  [PATCH v2 1/3]\tadd\n\n  driver  ', '[PATCH v2 1/3] add driver\n', id='collapses-whitespace'
            ),
            # Subjects carry maintainer names, which are routinely non-ASCII
            pytest.param('Café patches from Ævar', 'Café patches from Ævar\n', id='non-ascii'),
            # An empty file reads back as missing, so nothing is written
            pytest.param('   \n\t ', None, id='blank-writes-nothing'),
        ],
    )
    def test_description_file(self, tmp_path: Path, given: str, expected: str | None) -> None:
        write_archive_description(tmp_path, given)

        if expected is None:
            assert not (tmp_path / 'description').exists()
        else:
            assert read_description(tmp_path) == expected

    def test_overwrites_previous_description(self, tmp_path: Path) -> None:
        write_archive_description(tmp_path, 'old name')
        write_archive_description(tmp_path, 'new name')

        assert read_description(tmp_path) == 'new name\n'

    def test_unwritable_archive_does_not_raise(self, tmp_path: Path) -> None:
        """A cosmetic file must never take tracking down with it."""
        missing = tmp_path / 'does-not-exist'

        write_archive_description(missing, 'DOCUMENTATION patches')

        assert not missing.exists()
