import io
import logging

from helpers import lines
from twr.events.v1 import callout_pb2
from twr_contracts.callout import Level

from radio.queue import Pending
from radio.sink import LogSink


class Delivery:
    async def ack(self) -> None:
        return None


async def test_the_log_sink_logs_what_it_would_say(log: io.StringIO) -> None:
    sink = LogSink(logging.getLogger("radio"))
    assert await sink.connect()
    assert await sink.prepare("") is None
    clip = await sink.prepare("Final lap.")
    assert clip == "Final lap."
    m = callout_pb2.Callout(id="c1", rule="final_lap", kind="template", priority=10)
    await sink.play(clip, Pending(m, Level.P1, 1, Delivery()))
    sink.release(clip)
    [line] = lines(log)
    assert (line["msg"], line["id"], line["rule"], line["text"]) == (
        "speaking",
        "c1",
        "final_lap",
        "Final lap.",
    )
