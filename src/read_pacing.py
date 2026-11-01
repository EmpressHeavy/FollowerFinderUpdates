"""Read pacing state machine for browser/API list & enrichment reads.

No live X calls. Unit-testable. Hard-stops on repeated Loading/blank/429;
shrinks batches on warnings; resumes from checkpoints without retry storms.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class PaceEvent(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"  # slow load, partial blank, soft rate hint
    LOADING_STUCK = "loading_stuck"
    BLANK = "blank"
    RATE_LIMIT_429 = "429"
    CHALLENGE = "challenge"
    SESSION_FAIL = "session_fail"


class PaceState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COOLDOWN = "cooldown"
    HARD_STOP = "hard_stop"


@dataclass
class PaceConfig:
    initial_batch_size: int = 25
    min_batch_size: int = 5
    max_batch_size: int = 50
    cooldown_seconds: float = 3.0
    shrink_factor: float = 0.5
    warning_shrink_after: int = 2
    hard_stop_after_failures: int = 3  # consecutive Loading/blank/429/challenge
    max_retries_per_item: int = 1  # no retry-storm


@dataclass
class PaceMachine:
    config: PaceConfig = field(default_factory=PaceConfig)
    state: PaceState = PaceState.IDLE
    batch_size: int = 25
    consecutive_failures: int = 0
    consecutive_warnings: int = 0
    items_since_checkpoint: int = 0
    last_event: str | None = None
    stop_reason: str | None = None
    seen_keys: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.batch_size = self.config.initial_batch_size

    def start(self) -> None:
        if self.state == PaceState.HARD_STOP:
            return
        self.state = PaceState.RUNNING

    def should_skip(self, key: str) -> bool:
        """Duplicate/skip prevention for resume."""
        return key in self.seen_keys

    def mark_seen(self, key: str) -> None:
        self.seen_keys.add(key)

    def note_success(self, key: str | None = None) -> dict[str, Any]:
        if key:
            self.mark_seen(key)
        self.consecutive_failures = 0
        self.consecutive_warnings = 0
        self.items_since_checkpoint += 1
        self.last_event = PaceEvent.SUCCESS.value
        if self.state != PaceState.HARD_STOP:
            self.state = PaceState.RUNNING
        return self.status()

    def note_event(self, event: PaceEvent | str, *, key: str | None = None) -> dict[str, Any]:
        ev = PaceEvent(event) if not isinstance(event, PaceEvent) else event
        self.last_event = ev.value
        if ev == PaceEvent.SUCCESS:
            return self.note_success(key)

        if ev == PaceEvent.WARNING:
            self.consecutive_warnings += 1
            if self.consecutive_warnings >= self.config.warning_shrink_after:
                self._shrink()
                self.consecutive_warnings = 0
                self.state = PaceState.COOLDOWN
            return self.status()

        # Hard failure class
        self.consecutive_failures += 1
        self._shrink()
        if self.consecutive_failures >= self.config.hard_stop_after_failures:
            self.state = PaceState.HARD_STOP
            self.stop_reason = f"hard_stop after {self.consecutive_failures} consecutive {ev.value}"
        else:
            self.state = PaceState.COOLDOWN
            self.stop_reason = f"cooldown after {ev.value}"
        return self.status()

    def _shrink(self) -> None:
        nxt = max(self.config.min_batch_size, int(self.batch_size * self.config.shrink_factor))
        self.batch_size = nxt

    def can_continue(self) -> bool:
        return self.state != PaceState.HARD_STOP

    def cooldown_seconds(self) -> float:
        if self.state == PaceState.COOLDOWN:
            return self.config.cooldown_seconds
        if self.state == PaceState.HARD_STOP:
            return 0.0
        return 0.0

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "batch_size": self.batch_size,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_warnings": self.consecutive_warnings,
            "items_since_checkpoint": self.items_since_checkpoint,
            "last_event": self.last_event,
            "stop_reason": self.stop_reason,
            "seen_count": len(self.seen_keys),
            "can_continue": self.can_continue(),
            "cooldown_seconds": self.cooldown_seconds(),
        }

    def to_checkpoint_fragment(self) -> dict[str, Any]:
        return {
            "pacing": {
                "state": self.state.value,
                "batch_size": self.batch_size,
                "consecutive_failures": self.consecutive_failures,
                "seen_keys_sample": sorted(list(self.seen_keys))[:50],
                "seen_count": len(self.seen_keys),
                "stop_reason": self.stop_reason,
            }
        }
