"""A real nats-server -js for the tests (paddock#56: never a mock of NATS).

The binary is the one TWR_NATS_SERVER names, else `nats-server` on the PATH, else
the release below: its archive fetched from nats-io's GitHub release once into the
user's cache, checked against its sha256 from the release's SHA256SUMS on every
run, and unpacked for the run. The same version as the image smoke test's
(image.yml).
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

VERSION = "2.15.2"
SUMS = {
    "linux-amd64": "67cb8e10bc6f0c4b47f75a728529a20953dbae2a4f9a4a52ae8517d65ae303ff",
    "linux-arm64": "bc2c788dcbf2849af749564e06a0d48c19bced851f2d68a2e21c54d078949b28",
    "darwin-amd64": "9c45b23c19498692e81cd39c75e8382f0e6ff59c0d5b9470e32fe6640afa4446",
    "darwin-arm64": "cbd2219d01502c38733485ac684269da585ee479fa3b1377264beecdefdf9e9d",
}
URL = "https://github.com/nats-io/nats-server/releases/download/v{v}/nats-server-v{v}-{p}.tar.gz"


def _platform() -> str:
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    return f"{platform.system().lower()}-{arch.get(platform.machine().lower(), '')}"


def binary(dest: Path) -> Path:
    """The nats-server to run. Without one named or on the PATH, the pinned release's
    archive, cached in the user's cache and checked against its sha256 every time,
    fetched again when it is missing or fails, and its binary unpacked into dest."""
    if named := os.environ.get("TWR_NATS_SERVER"):
        return Path(named)
    if found := shutil.which("nats-server"):
        return Path(found)
    plat = _platform()
    if plat not in SUMS:
        raise RuntimeError(f"no nats-server {VERSION} for {plat}: set TWR_NATS_SERVER")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "twr-radio-tests"
    archive = cache / f"nats-server-v{VERSION}-{plat}.tar.gz"
    data = archive.read_bytes() if archive.exists() else b""
    if hashlib.sha256(data).hexdigest() != SUMS[plat]:
        with urllib.request.urlopen(URL.format(v=VERSION, p=plat), timeout=60) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != SUMS[plat]:
            raise RuntimeError(f"nats-server {VERSION} for {plat} fails its sha256")
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / f"{archive.name}.{os.getpid()}"
        tmp.write_bytes(data)
        tmp.replace(archive)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.extractfile(f"nats-server-v{VERSION}-{plat}/nats-server")
        assert member is not None
        path = dest / "nats-server"
        path.write_bytes(member.read())
    path.chmod(0o755)
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class NATSServer:
    """nats-server -js on a free port, its store and binary in work_dir."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.store_dir = work_dir / "jetstream"
        self.port = free_port()
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [str(binary(self.work_dir)), "-js", "-a", "127.0.0.1", "-p", str(self.port)]
            + ["-sd", str(self.store_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("nats-server did not start within 10 s")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None
