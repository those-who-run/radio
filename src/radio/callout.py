"""The parts of contracts' `Callout` (v0.2.2, `proto/twr/events/v1/callout.proto`) that
radio phrases: the rule, the kind, the slots and the text.

A stand-in until contracts publishes its Python package (paddock#265): paddock#56,
which takes callouts from NATS, swaps it for the generated type. The names and
meanings are the proto's; an `optional` int that is not set is None here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Corner(IntEnum):
    """One of the car's four corners, as GT7 orders its tyres (contracts' `Corner`)."""

    UNSPECIFIED = 0
    FRONT_LEFT = 1
    FRONT_RIGHT = 2
    REAR_LEFT = 3
    REAR_RIGHT = 4


@dataclass(frozen=True)
class CalloutSlots:
    """The values that fill a callout's phrase, typed and unspoken (`CalloutSlots`).
    A slot that is not set is not said."""

    lap: int | None = None
    lap_time_ms: int | None = None
    # The lap less the best before it, exact; radio says the spoken difference.
    delta_ms: int | None = None
    laps: int | None = None
    laps_left: int | None = None
    position: int | None = None
    previous_position: int | None = None
    total_cars: int | None = None
    track: str = ""
    corners: tuple[Corner, ...] = ()
    best_lap_ms: int | None = None
    timed: bool = False


@dataclass(frozen=True)
class Callout:
    """What to say: v1's rule name, the script key ecu chose (kind) and its slots, or,
    from another publisher, the text to speak as it is."""

    rule: str = ""
    kind: str = ""
    slots: CalloutSlots = field(default_factory=CalloutSlots)
    text: str = ""
