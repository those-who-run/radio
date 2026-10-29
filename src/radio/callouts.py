"""radio takes the callouts ecu publishes on the CALLOUTS stream and speaks them, one
at a time, most urgent first (paddock#56): v1's `CalloutQueue` and `speak_loop`,
with the stream in front.

Taking. Each level, `twr.v1.<src>.callout.p0`, `.p1` and `.p2`, has a durable pull
consumer, `radio-<src>-p0` and so on, so a restarted radio carries on where it
stopped. radio fetches every level, p0 first, until a whole round brings nothing
new, then phrases what it took in the stream's order, across the three levels,
and queues it (`radio.queue`). ecu publishes one callout at a time in the order it
decided, so the stream's sequence is ecu's order and a replay's is the same every
time. It is the order the callouts were raised in (`run_id`, then `at`) but for a
callout ecu held in the pit lane, which keeps the `at` it was raised on and is
published at the exit: radio phrases it where ecu published it. Phrasing comes
before anything decides to speak or drop a callout, so the levels, expiry and the
pit lane never change the order of each rule's seeded picks (paddock#90). A round runs when
a callout is published (a plain subscription to the same subjects says so) and
every REFETCH seconds, which picks up a redelivery.

Speaking. One callout at a time (paddock#56 R5): the next is taken only once the
last was said or dropped. A callout past its `expires_at` is dropped at the stage
it was in, `queued` (which covers the wait while another plays), `connecting` or
`synthesis`, logged and counted (`dropped`). One that fails to play is logged and
the next is still spoken (R7).

Acking (R6). A callout is acked when radio starts to say it, or when it is dropped,
so a radio that dies mid-sentence never says it again. The ack before speaking
waits for the server to take it (a double ack), so a connection that drops right
then doesn't bring the callout back to a restarted radio; if that ack fails, radio
logs it and speaks anyway, and remembers the callout, so this radio never says it
twice. One taken and not yet said
is redelivered after ACK_WAIT; radio knows it by its stream sequence and keeps the
one it holds, so a redelivery is never said twice, and after a restart it is said
once, or dropped if it expired meanwhile.

radio applies what ecu publishes, the level, priority, `hold_until`, `expires_at`
and the phrase key, and adds no rules of its own: a callout without `expires_at`
never goes stale (callout.proto).

Who says it (paddock#60). Each session's callouts are said by one engineer
(radio.engineer), chosen by the roster when the session's first callout is
phrased, with a phrasebook of the engineer's seeded by the session's id: a replay
of a session, with the ids ecu gave it, says the same words. The engineer changes
how a callout is said, never which callouts are said, nor when.

When (radio.timing). Live, on the wall clock, each callout takes as long as the
sink takes to play it. A replay's callouts are said on the replay's own clock,
instantly or by a simulated speaker, and each one's way through radio goes to the
timeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any

from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NATSTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.client import JetStreamContext
from twr.events.v1 import callout_pb2
from twr_contracts import callout, ce

from radio.callout import from_message
from radio.engineer import Engineer, Roster
from radio.phrases import PhraseError, Phrases, missing
from radio.queue import (
    CONNECTING,
    QUEUED,
    SYNTHESIS,
    CalloutQueue,
    Clock,
    Delivery,
    Pending,
    utc_now,
)
from radio.sink import Sink
from radio.timing import (
    DROPPED,
    EXPIRED,
    INSTANT,
    LIVE,
    SIMULATED,
    SPOKEN,
    ReplayClock,
    Timeline,
    later,
)

# The stream ecu publishes callouts to (contracts' subjects.yaml; paddock#73).
STREAM = "CALLOUTS"
# How long the server waits for an ack before it delivers a message again: after
# a restart, the callouts the last radio took and never said come back after this.
ACK_WAIT = 5.0
# The most messages one fetch takes.
FETCH_BATCH = 64
# How long a fetch waits on a level with nothing in it.
FETCH_WAIT = 0.02
# A round runs at least this often, for redeliveries.
REFETCH = 1.0
# How long radio waits before it tries the consumers again, when the stream
# isn't there yet or NATS is down.
RETRY_WAIT = 2.0
# How long radio waits for the server to take the ack before it speaks.
ACK_SYNC_WAIT = 1.0
# How many said or dropped callouts radio remembers, so a late redelivery of one
# is acked and never said again.
REMEMBER = 4096
# Other stages a callout is dropped at, beside the queue's.
NOT_CONNECTED = "not connected"
PHRASING = "phrasing"
EMPTY = "nothing to say"
PLAYING = "playing"
# The stages a stale callout is dropped at: it expired, where the others were
# dropped.
STALE = (QUEUED, CONNECTING, SYNTHESIS)
# How many sessions' phrasebooks radio keeps: a session's callouts after its
# phrasebook was let go start a fresh one, from the same seed.
SESSIONS = 16


def durable(src: str, level: callout.Level) -> str:
    """A level's consumer: radio-<src>-p0 and so on."""
    return f"radio-{src}-{level}"


class Callouts[C]:
    """Takes src's callouts from the stream and speaks them through sink."""

    def __init__(
        self,
        nc: Client,
        src: str,
        roster: Roster,
        sink: Sink[C],
        logger: logging.Logger,
        clock: Clock = utc_now,
        timing: str = LIVE,
        timeline: Timeline | None = None,
        replay: ReplayClock | None = None,
    ) -> None:
        self.nc = nc
        self.src = src
        self.roster = roster
        self.sink = sink
        self.logger = logger
        self.clock = clock
        self.timing = timing
        self.timeline = timeline or Timeline(None)
        # A replay's clock, for instant and simulated timing.
        self.replay = replay or ReplayClock()
        self.queue = CalloutQueue(self._expire)
        # Callouts dropped, by stage, phrased and said.
        self.dropped: Counter[str] = Counter()
        self.phrased = 0
        self.said = 0
        # Each session's engineer and phrasebook, by session id, oldest first.
        self._sessions: OrderedDict[str, tuple[Engineer, Phrases]] = OrderedDict()
        # The callouts taken and not yet settled, by stream sequence.
        self._held: dict[int, Pending] = {}
        # The stream sequences settled (said or dropped), oldest first.
        self._settled: dict[int, None] = {}
        self._subs: list[tuple[callout.Level, JetStreamContext.PullSubscription]] = []
        self._published = asyncio.Event()

    async def run(self) -> None:
        """Take and speak until cancelled."""
        await self._subscribe()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._take())
            tg.create_task(self._speak() if self.timing == LIVE else self._simulate())

    # Taking.

    async def _subscribe(self) -> None:
        """The level's consumers, created or brought up to date, retried until the
        stream is there."""
        js = self.nc.jetstream()
        while True:
            try:
                subs = []
                for level in callout.LEVELS:
                    name = durable(self.src, level)
                    await js.add_consumer(
                        STREAM,
                        ConsumerConfig(
                            durable_name=name,
                            description=f"radio speaks {self.src}'s {level} callouts",
                            filter_subject=callout.level_subject(self.src, level),
                            deliver_policy=DeliverPolicy.ALL,
                            ack_policy=AckPolicy.EXPLICIT,
                            ack_wait=ACK_WAIT,
                        ),
                    )
                    subs.append((level, await js.pull_subscribe_bind(durable=name, stream=STREAM)))
                break
            except Exception as err:  # Among them the stream not there yet.
                self.logger.warning(
                    "callouts: consumers", extra={"stream": STREAM, "err": _err(err)}
                )
                await asyncio.sleep(RETRY_WAIT)
        self._subs = subs
        await self.nc.subscribe(f"twr.v1.{self.src}.callout.*", cb=self._on_publish)
        self.logger.info(
            "callouts: taking", extra={"src": self.src, "stream": STREAM, "timing": self.timing}
        )

    async def _on_publish(self, msg: Msg) -> None:
        self._published.set()

    async def _take(self) -> None:
        # Taken and not yet phrased: kept across a fetch that fails part way, so
        # nothing taken is lost to the queue.
        taken: list[Pending] = []
        while True:
            self._published.clear()
            try:
                await self._round(taken)
            except Exception as err:  # NATS is down: nats-py reconnects.
                self.logger.warning("callouts: fetch", extra={"err": _err(err)})
                await asyncio.sleep(RETRY_WAIT)
                continue
            if not taken:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._published.wait(), REFETCH)
                continue
            for p in sorted(taken, key=lambda p: p.seq):
                await self._phrase(p)
            taken.clear()

    async def _round(self, taken: list[Pending]) -> None:
        """Adds what every level holds to taken, p0 first, fetched until a whole
        round brings nothing: what was published after that came after all
        of it."""
        while True:
            got = False
            for level, sub in self._subs:
                try:
                    msgs = await sub.fetch(FETCH_BATCH, timeout=FETCH_WAIT)
                except NATSTimeoutError:
                    continue
                # Taking, the replay's clock waits for this batch.
                self.replay.busy()
                for msg in msgs:
                    got = True
                    if (p := await self._receive(level, msg)) is not None:
                        taken.append(p)
            if not got:
                return

    async def _receive(self, level: callout.Level, msg: Msg) -> Pending | None:
        """A new callout, or None for a redelivery or a message radio can't read."""
        seq = msg.metadata.sequence.stream
        if seq in self._settled:
            await _ack(msg, self.logger)
            return None
        if (held := self._held.get(seq)) is not None:
            held.delivery = msg
            return None
        m = callout_pb2.Callout()
        try:
            ce.decode(msg.headers or {}, msg.data, m)
        except ValueError as err:
            self.logger.warning(
                "callout unreadable", extra={"subject": msg.subject, "err": _err(err)}
            )
            self._remember(seq)
            await _ack(msg, self.logger)
            return None
        p = self._held[seq] = Pending(m, level, seq, msg)
        return p

    def _session(self, session_id: str) -> tuple[Engineer, Phrases]:
        """The session's engineer and phrasebook, chosen at its first callout."""
        if (got := self._sessions.get(session_id)) is not None:
            self._sessions.move_to_end(session_id)
            return got
        engineer = self.roster.pick(session_id)
        # Seeded by the session, so a replay of it says the same words; a callout
        # outside a session (telemetry between them) is unseeded, as v1 was.
        got = self._sessions[session_id] = (engineer, engineer.phrases(session_id or None))
        while len(self._sessions) > SESSIONS:
            self._sessions.popitem(last=False)
        self.logger.info(
            "callouts: engineer", extra={"session_id": session_id, "engineer": engineer.name}
        )
        return got

    async def _phrase(self, p: Pending) -> None:
        """Phrase it, then queue it; one with nothing to say is settled."""
        view = from_message(p.callout)
        if gaps := missing(view):
            self.logger.warning(
                "callout slot missing", extra={**_about(p), "kind": view.kind, "missing": gaps}
            )
        self.phrased += 1
        p.engineer, phrases = self._session(p.callout.session_id)
        p.queued = self.clock() if self.timing == LIVE else p.created_at
        try:
            try:
                p.text = phrases.say(view)
            except PhraseError as err:
                self.logger.warning("callout not phrased", extra={**_about(p), "err": str(err)})
                await self._drop(p, PHRASING, p.queued)
                return
            if not p.text:
                self.logger.info("nothing to say", extra=_about(p))
                self.timeline.write(p, DROPPED, EMPTY)
                await self._settle(p)
                return
            self.queue.push(p)
        finally:
            # Queued or not, it says the replay has reached its time.
            self.replay.saw(p.created_at)

    # Speaking.

    async def _speak(self) -> None:
        while True:
            p = await self.queue.get(self.clock)
            await self._say(p)

    async def _simulate(self) -> None:
        """Speak on the replay's clock: decide at time t only once the replay is
        past it, so every callout created by then is queued, whatever the speed of
        the replay. at is when the speaker is next free."""
        at: datetime | None = None
        while True:
            ready = self.queue.next_at()
            if ready is None:
                await self.queue.wait()
                continue
            t = ready if at is None or ready > at else at
            if not self.replay.past(t):
                await self.replay.wait(t)
                continue
            found, stale = self.queue.pop(t, replay=True, expire=self.timing != INSTANT)
            for p in stale:
                await self._drop(p, QUEUED, t)
            at = t
            if found is not None:
                at = await self._say(found, t) or t

    async def _say(self, p: Pending, at: datetime | None = None) -> datetime | None:
        """Say p. at is the replay's time it starts at, and then the time the
        speaker is free again is returned; None live."""
        now = self.clock if at is None else _fixed(at)
        finished = at
        try:
            if not await self._connect(p, now):
                return finished
            clip = await self.sink.prepare(p)
            if clip is None:
                self.logger.info("nothing to say", extra=_about(p))
                self.timeline.write(p, DROPPED, EMPTY)
                return finished
            try:
                if self._stale(p, now()):
                    await self._drop(p, SYNTHESIS, now())
                    return finished
                started = now()
                await self._settle(p, sync=True)
                await self.sink.play(clip, p)
                if at is not None:
                    # The simulated speaker holds the channel for the clip; instant
                    # takes no time.
                    held = self.sink.seconds(clip) if self.timing == SIMULATED else 0.0
                    finished = later(at, held)
                self.said += 1
                self.timeline.write(p, SPOKEN, None, started, finished or self.clock())
            finally:
                self.sink.release(clip)
        except Exception:
            self.logger.exception("callout failed to play", extra=_about(p))
            self.timeline.write(p, DROPPED, PLAYING)
        finally:
            await self._settle(p)
        return finished

    async def _connect(self, p: Pending, now: Clock) -> bool:
        """Wait for the sink until the callout goes stale; False if it was dropped."""
        try:
            left = None if self.timing == INSTANT else p.left(now())
            ready = await asyncio.wait_for(self.sink.connect(), left)
        except TimeoutError:
            ready = False
        if self._stale(p, now()):
            await self._drop(p, CONNECTING, now())
            return False
        if not ready:
            await self._drop(p, NOT_CONNECTED, now())
            return False
        return True

    def _stale(self, p: Pending, now: datetime) -> bool:
        """Past its expires_at: never, in instant timing."""
        return self.timing != INSTANT and p.stale(now)

    async def _expire(self, p: Pending, stage: str) -> None:
        """The queue's drop, live."""
        await self._drop(p, stage, self.clock())

    async def _drop(self, p: Pending, stage: str, at: datetime | None = None) -> None:
        self.dropped[stage] += 1
        self.logger.warning("callout dropped", extra={**_about(p), "stage": stage})
        self.timeline.write(p, EXPIRED if stage in STALE else DROPPED, stage, dropped=at)
        await self._settle(p)

    async def _settle(self, p: Pending, sync: bool = False) -> None:
        """Ack it, once: said or dropped. sync waits for the server to take the ack
        (a double ack), as radio does before it speaks."""
        if p.seq in self._settled:
            return
        self._held.pop(p.seq, None)
        self._remember(p.seq)
        await _ack(p.delivery, self.logger, sync)

    def _remember(self, seq: int) -> None:
        self._settled[seq] = None
        while len(self._settled) > REMEMBER:
            del self._settled[next(iter(self._settled))]


def _fixed(t: datetime) -> Clock:
    return lambda: t


def _about(p: Pending) -> dict[str, Any]:
    c = p.callout
    return {"id": c.id, "rule": c.rule, "priority": c.priority, "text": p.text}


async def _ack(delivery: Delivery, logger: logging.Logger, sync: bool = False) -> None:
    try:
        if sync:
            await delivery.ack_sync(timeout=ACK_SYNC_WAIT)
        else:
            await delivery.ack()
    except Exception as err:  # Redelivered later, and acked then.
        logger.warning("callout ack", extra={"err": _err(err)})


def _err(err: BaseException) -> str:
    return str(err) or type(err).__name__
