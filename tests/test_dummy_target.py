"""Tests for DummyTarget message delivery."""

from korgalore.dummy_target import DummyTarget


class TestDummyTarget:
    """Tests for DummyTarget, the no-op delivery target."""

    def test_init(self) -> None:
        """Identifier is stored, no other setup required."""
        target = DummyTarget('test')
        assert target.identifier == 'test'

    def test_connect_is_noop(self) -> None:
        """connect() does nothing and raises nothing."""
        target = DummyTarget('test')
        target.connect()

    def test_import_message_discards_and_returns_none(self) -> None:
        """import_message() accepts any message and returns None."""
        target = DummyTarget('test')
        result = target.import_message(
            raw_message=b'From: test@example.com\n\nBody',
            labels=['some-label'],
            feed_name='feed',
            delivery_name='delivery',
            subfolder='202609',
        )
        assert result is None

    def test_default_labels_is_empty(self) -> None:
        """DEFAULT_LABELS matches the other targets' convention."""
        assert DummyTarget.DEFAULT_LABELS == []
