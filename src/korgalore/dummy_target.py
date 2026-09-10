"""Service for discarding messages without delivering them anywhere."""

import logging
from typing import Any, List, Optional

logger = logging.getLogger('korgalore')


class DummyTarget:
    """No-op delivery target: accepts and discards every message.

    Useful when the lei v2 archive is the only storage that matters and a
    korgalore target is only needed to satisfy the CLI's requirement that
    at least one target be configured.
    """

    DEFAULT_LABELS: List[str] = []

    def __init__(self, identifier: str) -> None:
        """Initialize dummy target.

        Args:
            identifier: Target identifier for logging
        """
        self.identifier = identifier

    def connect(self) -> None:
        """Connect to dummy target (no-op)."""
        logger.debug('Dummy target ready: %s', self.identifier)

    def import_message(
        self,
        raw_message: bytes,
        labels: List[str],
        feed_name: Optional[str] = None,
        delivery_name: Optional[str] = None,
        subfolder: Optional[str] = None,
    ) -> Any:
        """Discard the message.

        Args:
            raw_message: Ignored.
            labels: Ignored.
            feed_name: Ignored.
            delivery_name: Ignored.
            subfolder: Ignored (no folder concept).

        Returns:
            None
        """
        logger.debug('Discarding message for dummy target: %s', self.identifier)
        return None
