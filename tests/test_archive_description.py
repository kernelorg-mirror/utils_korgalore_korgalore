"""Tests for write_archive_description.

lei's `-o v2:' writer never creates the `description' file public-inbox reads,
so archives show up in web listings as "($INBOX_DIR/description missing)".
These tests cover the file we write in its place: public-inbox treats it as a
single whitespace-collapsed line, and a description is cosmetic enough that
failing to write one must never raise.
"""

from pathlib import Path

from korgalore.tracking import write_archive_description


def read_description(archive: Path) -> str:
    return (archive / 'description').read_text(encoding='utf-8')


class TestWriteArchiveDescription:
    """Writing public-inbox descriptions into lei-created v2 archives."""

    def test_writes_single_trailing_newline(self, tmp_path: Path) -> None:
        write_archive_description(tmp_path, 'DOCUMENTATION patches')

        assert read_description(tmp_path) == 'DOCUMENTATION patches\n'

    def test_collapses_whitespace_to_one_line(self, tmp_path: Path) -> None:
        """public-inbox flattens the file anyway, so write it already flat."""
        write_archive_description(tmp_path, '  [PATCH v2 1/3]\tadd\n\n  driver  ')

        assert read_description(tmp_path) == '[PATCH v2 1/3] add driver\n'

    def test_preserves_non_ascii(self, tmp_path: Path) -> None:
        """Subjects carry maintainer names, which are routinely non-ASCII."""
        write_archive_description(tmp_path, 'Café patches from Ævar')

        assert read_description(tmp_path) == 'Café patches from Ævar\n'

    def test_blank_description_writes_nothing(self, tmp_path: Path) -> None:
        """An empty file reads back as missing, so skip it and keep the dir clean."""
        write_archive_description(tmp_path, '   \n\t ')

        assert not (tmp_path / 'description').exists()

    def test_overwrites_previous_description(self, tmp_path: Path) -> None:
        write_archive_description(tmp_path, 'old name')
        write_archive_description(tmp_path, 'new name')

        assert read_description(tmp_path) == 'new name\n'

    def test_unwritable_archive_does_not_raise(self, tmp_path: Path) -> None:
        """A cosmetic file must never take tracking down with it."""
        missing = tmp_path / 'does-not-exist'

        write_archive_description(missing, 'DOCUMENTATION patches')

        assert not missing.exists()
