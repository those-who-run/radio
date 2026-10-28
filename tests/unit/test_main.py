import asyncio
import io
import json
import socket

import pytest

from radio import __version__
from radio.__main__ import main


def test_version() -> None:
    out = io.StringIO()
    assert main(["--version"], {}, out) == 0
    assert out.getvalue() == f"{__version__}\n"


def test_a_bad_flag_or_setting(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--nope"], {}, io.StringIO()) == 2
    assert main([], {"TWR_LOG_FORMAT": "xml"}, io.StringIO()) == 2
    assert "TWR_LOG_FORMAT" in capsys.readouterr().err
    assert main(["-h"], {}, io.StringIO()) == 0


def test_runs_until_stopped() -> None:
    stop = asyncio.Event()
    stop.set()
    out = io.StringIO()
    env = {"TWR_HEALTH_ADDR": "127.0.0.1:0", "TWR_LOG_FORMAT": "json"}
    assert main([], env, out, stop) == 0
    assert [json.loads(line)["msg"] for line in out.getvalue().splitlines()] == [
        "serving",
        "shutting down",
        "stopped",
    ]


def test_fails_when_it_cannot_listen() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        out = io.StringIO()
        env = {"TWR_HEALTH_ADDR": f"127.0.0.1:{port}", "TWR_LOG_FORMAT": "json"}
        assert main([], env, out, asyncio.Event()) == 1
    assert json.loads(out.getvalue().splitlines()[-1])["msg"] == "stopped"
