"""Shared command guard helpers for OpenClaw AstrBot plugins."""

from __future__ import annotations


IMAGE_COMMANDS = ("gptimg", "gptedit", "banana", "bananaedit")


def is_reserved_image_command(text: str) -> bool:
    value = "" if text is None else str(text).strip().lower()
    if not value:
        return False
    if value.startswith("/"):
        return True
    return any(value == command or value.startswith(f"{command} ") for command in IMAGE_COMMANDS)
