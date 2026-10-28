# Radio

The voice engineer for Those Who Run v2: it turns callouts from NATS
into speech in Discord, with the pit-lane policy.
It is written in Python, as Piper and Discord voice need, and speaks as
a second bot beside v1's.

Issues are off in this repository. Every issue, the roadmap and the decisions
(ADRs) live in [those-who-run/paddock](https://github.com/those-who-run/paddock). A pull request here
names its paddock issue: `Closes those-who-run/paddock#N.` when it meets every
acceptance criterion, `Part of those-who-run/paddock#N` otherwise
([`AGENTS.md`](AGENTS.md)).

## Running

```sh
make venv   # .venv from uv.lock (needs uv)
make run    # radio, logging text to standard output
```

radio serves `GET /healthz`, connects to NATS and logs `ready`. Until NATS
answers it retries, logging each failed attempt, and `/healthz` answers 503;
once connected it answers 200. Either way the body is
`{"status": "ok" | "unavailable", "version": "…", "connected": true | false}`.
SIGINT or SIGTERM drains the connection and stops it, with exit code 0.
`radio --version` prints the version.

| Setting | Default | What it does |
| --- | --- | --- |
| `TWR_NATS_URL` | `nats://127.0.0.1:4222` | The NATS server, or several separated by commas. |
| `TWR_HEALTH_ADDR` | `:8080` | Where `GET /healthz` listens, `host:port`; an empty host is every interface. |
| `TWR_LOG_LEVEL` | `info` | The lowest level logged: `debug`, `info`, `warn` or `error`. |
| `TWR_LOG_FORMAT` | `text` (`json` in the image) | `text` writes logfmt, `json` one object a line. |

A value radio can't read stops it at start with exit code 2.

## The image

`ghcr.io/those-who-run/twr-radio`, built from the [`Dockerfile`](Dockerfile)
on every PR by CI's `image / build`, which runs it as its non-root user
(65532) with a read-only root filesystem, against no NATS and against
`nats-server -js`. Locally, `make image`.

## Checks

`make check` runs what CI runs; [`AGENTS.md`](AGENTS.md) says what that is and
what a new clone runs first.
