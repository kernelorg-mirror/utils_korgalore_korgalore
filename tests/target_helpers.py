"""Helpers shared by the delivery target tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def valid_token_file(
    tmp_path: Path,
    access_token: str = 'valid',
    refresh_token: str = 'refresh',
    expires_in: float = 3600,
) -> Path:
    """Write an OAuth2 token file that expires `expires_in` seconds from now.

    A negative `expires_in` gives an already expired token.
    """
    token_file = tmp_path / 'token.json'
    token_file.write_text(
        json.dumps(
            {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'expires_at': datetime.now(UTC).timestamp() + expires_in,
            }
        )
    )
    return token_file
