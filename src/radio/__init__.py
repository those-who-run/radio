"""radio, the voice engineer of Those Who Run v2 (README.md).

So far it starts, connects to NATS, reports its health and stops cleanly: taking
callouts, speaking them and Discord come in later issues (paddock#56, #57, #58).
"""

from importlib.metadata import version

# The installed package's version, from pyproject.toml: what `radio --version`
# prints and GET /healthz reports.
__version__ = version("radio")
