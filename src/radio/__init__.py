"""radio, the voice engineer of Those Who Run v2 (README.md).

It takes ecu's callouts (callouts.py), phrases them as its engineer (engineer.py,
phrases.py) and speaks them to the console, to WAV files (sink.py, speech.py) or
in a Discord voice channel (discord_sink.py, gateway.py), live or on a replay's
clock (timing.py).
"""

from importlib.metadata import version

# The installed package's version, from pyproject.toml: what `radio --version`
# prints and GET /healthz reports.
__version__ = version("radio")
