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
| `TWR_RADIO_VOICE_MODEL` | `voices/en_GB-alba-medium.onnx` (the baked-in voice in the image) | The Piper voice, an `.onnx` file with its `.onnx.json` beside it. |
| `TWR_RADIO_VOICE_FILE` | unset: v1's defaults (`length_scale` 0.9, `noise_scale` 0.5, `noise_w_scale` 0.5, `sentence_pause_secs` 0.35) | A YAML file whose `voice:` section tunes the voice ([The voice](#the-voice)). |
| `TWR_RADIO_CACHE_DIR` | `twr-radio` under `$XDG_CACHE_HOME`, or `~/.cache` | The sentence cache. radio refuses a directory another user owns. |
| `TWR_RADIO_CACHE_MAX_MB` | `100` | The sentence cache's cap, in MiB; the least recently used sentence goes first. |

A value radio can't read stops it at start with exit code 2.

## The voice

radio speaks with Piper and v1's voice, `en_GB-alba-medium`, sentence by
sentence with a pause between sentences, caching each sentence so a phrase
said before is not made again (paddock#57). It is not wired to callouts yet:
taking them (paddock#56) and the Discord sink (paddock#58) come next.

The model is never committed. `make voice` fetches it into `voices/` from the
pinned [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/v1.0.0/en/en_GB/alba/medium)
release and checks both files against [`src/radio/voices/SHA256SUMS`](src/radio/voices/SHA256SUMS),
v1's sums; the image bakes it in the same way, and radio checks a known voice
again when it loads it. A voice of your own is a path in `TWR_RADIO_VOICE_MODEL`;
radio has no sums for it, so it logs `voice model not checked`.

The voice file's `voice:` section takes v1's keys. In v2 the rules belong to
ecu, but radio reads nothing but the `voice:` section, so a v1 rules file can be
pointed at it as it is. A key left out takes v1's default, and a null leaves the
setting to the model's own config:

```yaml
voice:
  length_scale: 0.9           # under 1 speaks faster (the default)
  noise_scale: 0.5           # variation in the voice (the default)
  noise_w_scale: 0.5          # variation in the timing (the default)
  # speaker_id: 0             # in a voice with several speakers
  sentence_pause_secs: 0.35   # silence between sentences (the default)
```

The tests use a fake voice, so `make check` and CI need no model.
`make voice-test` runs the synthesiser against the real one, at
`TWR_RADIO_VOICE_MODEL` or the one `make voice` fetched; it is not part of
`make check`.

The voice's attribution (CC BY 4.0) is in [`NOTICE`](NOTICE).

## The image

`ghcr.io/those-who-run/twr-radio`, built from the [`Dockerfile`](Dockerfile)
on every PR by CI's `image / build`, which runs it as its non-root user
(65532) with a read-only root filesystem, against no NATS and against
`nats-server -js`. Locally, `make image`. It carries the voice, and its speech
cache is `/tmp/.cache/twr-radio`: with the read-only root, mount a tmpfs or a
volume on `/tmp`.

## Checks

`make check` runs what CI runs; [`AGENTS.md`](AGENTS.md) says what that is and
what a new clone runs first.
