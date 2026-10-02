"""Shared helpers for the digest tests."""

from typing import Any, Dict

import click


def make_ctx(obj: Dict[str, Any]) -> click.Context:
    """A click context with the given ctx.obj, like the cli functions get."""
    ctx = click.Context(click.Command('test'))
    ctx.obj = obj
    return ctx
