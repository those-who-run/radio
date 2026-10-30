"""The pit lane is quiet from entry to exit (paddock#59): the part of it that is
radio's, which is only what ecu can't do.

ecu applies each rule's `pit_lane` to every callout raised in the lane
(contracts' `PitLane`; ecu's internal/rules/engine.go): from the frame of the
entry to the frame of the exit, a `speak` callout is published at once, a `drop`
one never, and a `hold` one only when the lane ends, as raised then. Nothing
raised in the lane reaches radio but what ecu means it to say. What ecu can't see
is radio's queue: a callout published before the entry and not said by then,
waiting out its hold or behind another callout. v1 took those out of its queue at
the entry (`Engine._apply_pits`: `queue.remove(policy != "speak")`, then
`_quiet`), and so does radio:

- `drop` (position): dropped, at the stage `pit lane`. The place lost 0.7 s
  before the first entry at SUGO (2026-10-18), held 1 s, is never said.
- `hold`, or not set (PitLane's default): held until the lane ends, then as
  raised then: created_at is the end, expires_at the end plus its life, and
  hold_until no longer applies (v1's `_release`). The lane ends at the exit
  (PitExited's exited_at), at a PitTeleport that withdraws the entry, or at the
  flag (RaceFinished) for a session that finishes in the lane; an entry after
  the flag ends as it starts, since v1 released what it held on every frame
  once finished. A session that ends any other way with callouts held drops
  them (SessionEnded, SessionDiscarded), as ecu drops its own and v1 cleared
  `pit_held`.
- `speak` (fuel, race_summary): said as anywhere else.

When. The lane is applied when the speaker comes to a callout, at the time it
speaks by (the wall clock live, the replay's own on a replay), not when the entry
reaches radio: a callout is caught by an entry made after it was created and by
the time the speaker came to it (created_at < entered_at <= now). CALLOUTS and
EVENTS are not ordered, so a callout raised just before the entry can come after
the PitEntered, and a replay delivers both well ahead of its clock; going by the
times, a replay at any speed says what v1 said. Every time is ecu's frame time in
UTC, as the contract says: entered_at and exited_at, and the ce-time of a
PitTeleport or RaceFinished, which carry no UTC time of their own.

Who decides. radio decides nothing here: the policy is the callout's, as ecu
published it, and the lane is ecu's events.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

from twr.events.v1 import callout_pb2

from radio.queue import HELD, MOVED, PIT_DROP, Pending

# How many sessions' lanes radio keeps, as callouts.SESSIONS.
SESSIONS = 16

SPEAK = callout_pb2.PIT_LANE_SPEAK
DROP = callout_pb2.PIT_LANE_DROP


@dataclass
class Lane:
    """One pit entry, and its end once it has one."""

    entered: datetime
    left: datetime | None = None
    # The session ended in the lane without its flag: what it held is dropped.
    cleared: bool = False
    # The callouts held for its end, in the order they were held.
    held: list[Pending] = field(default_factory=list)


@dataclass
class _Session:
    # The lanes by entry number (PitEntered.entry).
    lanes: dict[int, Lane] = field(default_factory=dict)
    # The flag fell (RaceFinished).
    finished: bool = False


class PitLane:
    """The pit lanes of a source's sessions, from ecu's events, and the callouts held
    in them. The queue asks `check` of each callout as it comes to it."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    def _session(self, session_id: str) -> _Session:
        if (s := self._sessions.get(session_id)) is not None:
            self._sessions.move_to_end(session_id)
            return s
        s = self._sessions[session_id] = _Session()
        while len(self._sessions) > SESSIONS:
            self._sessions.popitem(last=False)
        return s

    # The events.

    def entered(self, session_id: str, entry: int, t: datetime) -> None:
        """PitEntered: the lane opens at t. One sent again (an ecu restart) is the
        same entry."""
        s = self._session(session_id)
        if entry in s.lanes:
            return
        # After the flag, v1 released what it held on the frame it held it.
        s.lanes[entry] = Lane(t, t if s.finished else None)
        self.logger.info(
            "pit lane: entered", extra={"session_id": session_id, "entry": entry, "at": t}
        )

    def left(self, session_id: str, entry: int, t: datetime) -> list[Pending]:
        """PitExited or a PitTeleport withdrawing the entry: the lane ends at t.
        Returns the callouts it held, as raised at t, to queue again."""
        lane = self._session(session_id).lanes.get(entry)
        if lane is None or lane.left is not None or lane.cleared:
            return []
        lane.left = t
        return self._release(lane)

    def finished(self, session_id: str, t: datetime) -> list[Pending]:
        """RaceFinished: a lane still open ends at the flag, t, with no exit."""
        s = self._session(session_id)
        s.finished = True
        out = []
        for lane in s.lanes.values():
            if lane.left is None and not lane.cleared:
                lane.left = t
                out += self._release(lane)
        return out

    def ended(self, session_id: str) -> list[Pending]:
        """SessionEnded or SessionDiscarded: a lane still open is cleared. Returns
        the callouts it held, to drop."""
        s = self._sessions.get(session_id)
        if s is None:
            return []
        out = []
        for lane in s.lanes.values():
            if lane.left is None and not lane.cleared:
                lane.cleared = True
                out += lane.held
                lane.held = []
        return out

    def _release(self, lane: Lane) -> list[Pending]:
        assert lane.left is not None
        out, lane.held = lane.held, []
        for p in out:
            moved(p, lane.left)
            self.logger.info("callout released at the pit exit", extra=_about(p))
        return out

    # The queue's gate.

    def check(self, p: Pending, now: datetime) -> str | None:
        """What the lane does to p, which the speaker has come to at now: None when
        nothing, PIT_DROP to drop it, HELD when the lane has taken it until its
        end, MOVED when it is now as raised at the end of a lane already over."""
        policy = p.callout.pit_lane
        if policy == SPEAK:
            return None
        s = self._sessions.get(p.callout.session_id)
        if s is None:
            return None
        verdict = None
        for lane in sorted(s.lanes.values(), key=lambda lane: lane.entered):
            if not p.created_at < lane.entered <= now:
                continue
            if policy == DROP or lane.cleared:
                self.logger.info("callout dropped in the pit lane", extra=_about(p))
                return PIT_DROP
            if lane.left is None:
                lane.held.append(p)
                self.logger.info("callout held for the pit exit", extra=_about(p))
                return HELD
            # Held and released before the speaker came to it: it is as raised at
            # the lane's end, and a later entry can catch it again.
            moved(p, lane.left)
            self.logger.info("callout released at the pit exit", extra=_about(p))
            verdict = MOVED
        return verdict


def moved(p: Pending, t: datetime) -> None:
    """p as raised at t: its life restarts there, and its hold no longer applies
    (v1's `_release`: created_at the release, hold 0)."""
    if p.expires_at is not None:
        p.expires_at = t + (p.expires_at - p.created_at)
    p.created_at = t
    p.hold_until = None


def _about(p: Pending) -> dict[str, object]:
    c = p.callout
    return {"id": c.id, "rule": c.rule, "session_id": c.session_id}
