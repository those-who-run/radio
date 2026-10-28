"""The tests' fixtures, and the rule that a skipped test fails the run, as in v1 and
as CI's `python / check` has it."""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator, Iterator

import pytest
from helpers import FakeNATS

from radio import logs

_skipped: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # An expected failure (xfail) reports as skipped too, and counts the same.
    if report.skipped:
        _skipped.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _skipped and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for nodeid in _skipped:
        terminalreporter.write_line(f"skipped, which fails the run: {nodeid}", red=True)


@pytest.fixture
async def fake_nats() -> AsyncIterator[FakeNATS]:
    server = FakeNATS()
    await server.start()
    yield server
    await server.close()


@pytest.fixture
def log() -> Iterator[io.StringIO]:
    """The `radio` logger's JSON lines, at debug."""
    out = io.StringIO()
    logs.setup(logging.DEBUG, "json", out)
    yield out
    logging.getLogger("radio").handlers.clear()
