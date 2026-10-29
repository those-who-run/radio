import io
import json
import logging

from radio import logs


def test_json_lines() -> None:
    out = io.StringIO()
    logger = logs.setup(logging.INFO, "json", out)
    logger.debug("hidden")
    logger.info("ready", extra={"nats": "nats://nats:4222"})
    logger.warning("slow")
    [ready, slow] = [json.loads(line) for line in out.getvalue().splitlines()]
    assert list(ready) == ["time", "level", "msg", "nats"]
    assert ready["level"] == "INFO"
    assert ready["msg"] == "ready"
    assert ready["nats"] == "nats://nats:4222"
    assert str(ready["time"]).endswith("+00:00")
    assert slow["level"] == "WARN"


def test_text_is_logfmt() -> None:
    out = io.StringIO()
    logger = logs.setup(logging.DEBUG, "text", out)
    logger.debug("nats not connected, retrying", extra={"err": "", "in_s": 0.5, "a": 'x="y"'})
    line = out.getvalue().strip()
    assert line.startswith("time=")
    assert ' level=DEBUG msg="nats not connected, retrying" err="" in_s=0.5 a="x=\\"y\\""' in line


def test_an_exception_is_logged_with_its_trace() -> None:
    out = io.StringIO()
    logger = logs.setup(logging.INFO, "json", out)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("stopped")
    record = json.loads(out.getvalue())
    assert record["level"] == "ERROR"
    assert "RuntimeError: boom" in record["err"]


def test_setup_again_replaces_the_handler() -> None:
    first, second = io.StringIO(), io.StringIO()
    logs.setup(logging.INFO, "json", first)
    logs.setup(logging.INFO, "json", second).info("once")
    assert first.getvalue() == ""
    assert len(second.getvalue().splitlines()) == 1


def test_every_librarys_lines_are_written_and_redacted() -> None:
    # Through the root logger: asyncio's and aiohttp's lines too, from info up,
    # with the secret kept out.
    out = io.StringIO()
    logs.setup(logging.DEBUG, "json", out, ["s3cret-value"])
    try:
        logging.getLogger("asyncio").error("task failed with s3cret-value")
        logging.getLogger("aiohttp.client").info("connected")
        logging.getLogger("discord.gateway").debug("every gateway event")
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        assert [r["msg"] for r in records] == ["task failed with [redacted]", "connected"]
        root = logging.getLogger()
        logs.setup(logging.INFO, "json", io.StringIO())
        assert sum(h.get_name() == "radio" for h in root.handlers) == 1
    finally:
        logs.setup(logging.INFO, "json", io.StringIO())
