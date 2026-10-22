"""Turning errors into short reasons for the user, the same way in the app and the CLI."""

from __future__ import annotations


def describe_error(error: BaseException) -> str:
    """A short reason: the OS message for file errors, else the error text."""
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__
