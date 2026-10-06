"""Shared helpers for tests that talk to a LoreFeed through a mock node."""

import gzip
import json
from typing import Any, Dict
from unittest.mock import MagicMock


def gzipped_response(payload: bytes) -> MagicMock:
    """A mock HTTP response whose body is the given bytes, gzipped."""
    response = MagicMock()
    response.content = gzip.compress(payload)
    response.raise_for_status = MagicMock()
    return response


def manifest_response(manifest_data: Dict[str, Any]) -> MagicMock:
    """A mock HTTP response carrying a gzipped manifest.js.gz."""
    return gzipped_response(json.dumps(manifest_data).encode())
