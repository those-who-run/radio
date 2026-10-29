"""A skipped test fails the run, as in v1 and as CI's `python / check` has it: a
test skipped in its body or by a mark, an expected failure (xfail, which reports as
a skip), and a module skipped whole at collection (`pytest.skip(...,
allow_module_level=True)`, `pytest.importorskip`). conftest.py loads it.

The state lives in a plugin object per run, so a run inside a test (pytester)
keeps its own."""

from __future__ import annotations

import pytest


class NoSkip:
    def __init__(self) -> None:
        self.skipped: list[str] = []

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.skipped.append(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # A run whose every module was skipped collected no tests: it fails too.
        if self.skipped and exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        for nodeid in self.skipped:
            terminalreporter.write_line(f"skipped, which fails the run: {nodeid}", red=True)


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(NoSkip(), "radio-noskip")
