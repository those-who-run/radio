"""The parts of contracts' `Callout` (`proto/twr/events/v1/callout.proto`) that radio
phrases: the rule, the kind, the slots and the text.

radio takes the generated `twr.events.v1.Callout` from NATS (paddock#56) and
phrases this view of it (`from_message`): the names and meanings are the proto's,
but an `optional` int that is not set is None here, where the generated type says
0, so `radio.phrases` can tell "not said" from zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from twr.events.v1 import callout_pb2

# CalloutSlots' optional ints, None here when not set.
_OPTIONAL = (
    "lap",
    "lap_time_ms",
    "delta_ms",
    "laps",
    "laps_left",
    "position",
    "previous_position",
    "total_cars",
    "best_lap_ms",
)


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


def from_message(m: callout_pb2.Callout) -> Callout:
    """The phrasing view of a callout as it came off the bus: an optional slot that
    is not set (HasField) is None, and a corner a later contracts adds is left out."""
    s = m.slots
    slots = CalloutSlots(
        **{name: getattr(s, name) if s.HasField(name) else None for name in _OPTIONAL},
        track=s.track,
        corners=tuple(Corner(c) for c in s.corners if c in Corner),
        timed=s.timed,
    )
    return Callout(rule=m.rule, kind=m.kind, slots=slots, text=m.text)
