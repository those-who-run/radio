"""`radio`: run the service until SIGINT or SIGTERM. `radio --version` prints the
version and exits.

Exit codes: 0 after a clean shutdown, 1 when the service fails, 2 for a bad flag or
setting.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from typing import IO

from radio import __version__, logs
from radio.service import Radio
from radio.settings import SettingsError, from_env


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] = os.environ,
    stdout: IO[str] = sys.stdout,
    stop: asyncio.Event | None = None,
) -> int:
    """main without the process around it, so the tests can call it. stop, when
    given, stops it as a signal would."""
    parser = argparse.ArgumentParser(
        prog="radio", description="The voice engineer of Those Who Run v2."
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:  # -h exits 0; a bad flag 2.
        return int(exit_.code or 0)
    if args.version:
        print(__version__, file=stdout)
        return 0
    try:
        settings = from_env(environ)
    except SettingsError as err:
        print(f"radio: {err}", file=sys.stderr)
        return 2
    logger = logs.setup(settings.log_level, settings.log_format, stdout)
    try:
        asyncio.run(_serve(Radio(settings, __version__, logger), stop))
    except Exception:
        logger.exception("stopped")
        return 1
    return 0


async def _serve(radio: Radio, stop: asyncio.Event | None) -> None:
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await radio.run(stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    sys.exit(main())
