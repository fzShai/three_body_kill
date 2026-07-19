"""Turn phase helpers."""

from __future__ import annotations

TURN_PHASES = ("draw", "play", "discard")


def hand_limit(max_hp: int) -> int:
    return max(0, int(max_hp) - 2)
