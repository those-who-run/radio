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

radio serves `GET /healthz`, connects to NATS and logs `ready`, then takes
its source's callouts and speaks them ([The callouts](#the-callouts)) to the
console or to WAV files ([Sinks and timing](#sinks-and-timing)). Until NATS
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
| `TWR_RADIO_VOICE_MODEL` | `voices/en_GB-alba-medium.onnx`, relative to the working directory, where `make voice` puts it from the repository's root (`/app/voices/en_GB-alba-medium.onnx`, baked in, in the image) | The Piper voice, an `.onnx` file with its `.onnx.json` beside it. |
| `TWR_RADIO_SRC` | `live` | The source whose callouts radio speaks, `twr.v1.<src>.callout.>`: `live`, or `replay-<run>` for a replay. Letters, digits, `-` and `_`. |
| `TWR_RADIO_VOICE_FILE` | unset: v1's defaults (`length_scale` 0.9, `noise_scale` 0.5, `noise_w_scale` 0.5, `sentence_pause_secs` 0.35), and v1's script | A v1 rules file: its `voice:` section tunes the voice ([The voice](#the-voice)), and its `script:` and template keys the phrases ([The phrases](#the-phrases)). |
| `TWR_RADIO_CACHE_DIR` | `twr-radio` under `$XDG_CACHE_HOME`, or `~/.cache` | The sentence cache. radio refuses a directory another user owns. |
| `TWR_RADIO_CACHE_MAX_MB` | `100` | The sentence cache's cap, in MiB; the least recently used sentence goes first. |
| `TWR_RADIO_SINK` | `console` | Where radio speaks: `console`, a line a callout on standard output, or `wav`, a WAV file a callout and the line. `discord` comes with paddock#58. Neither needs a Discord setting. |
| `TWR_RADIO_WAV_DIR` | unset | The directory the `wav` sink writes to; it needs one. |
| `TWR_RADIO_TIMING` | `live` | How long a callout takes: `live` on the wall clock, or a replay's own clock, `instant` or `simulated` ([Sinks and timing](#sinks-and-timing)). |
| `TWR_RADIO_TIMELINE_FILE` | unset | Where each callout's timeline goes, a JSON line each, appended to, so a restart keeps what came before. |

A value radio can't read stops it at start with exit code 2.

## The voice

radio speaks with Piper and v1's voice, `en_GB-alba-medium`, sentence by
sentence with a pause between sentences, caching each sentence so a phrase
said before is not made again (paddock#57). The `wav` sink speaks with it, and
the Discord sink (paddock#58) will.

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

## The phrases

ecu's callouts say what to say, not how (contracts' `Callout`, paddock#71): the
rule, `kind` (the key of the phrase in v1's script) and typed slots. radio phrases
them as v1.0.0 did (paddock#90), with v1's code carried as it is:

- [`data/script.yaml`](src/radio/data/script.yaml), v1's script: four to six
  variants for each callout, one picked at random and never the same one twice
  running for a rule and key;
- each rule's built-in phrase, v1's `script: false` and the only phrase for
  `template_joined`, `template_joined_position` and `delta_first`;
- v1's number words ([`verbalise.py`](src/radio/verbalise.py)): "one oh four point
  six", "P ten of twenty", tenths rounded half up, and the delta as the
  difference of the spoken times.

`radio.phrases.Phrases.say(callout)` gives the sentence `Synthesizer.render`
speaks. A situational key (`template_gained_lead`, `delta_slower_large`) falls back
to its general one when the script lacks it, as v1 did; a callout with no `kind`,
or one radio doesn't know, is spoken from its `text` as it is. Seeded, the same
callouts give the same words, for a replay. A v1 rules file's templates and its
`script: false` can be laid over the script (`radio.phrasing.load_rules_file`), and
a template whose placeholders its key can't fill is refused when it loads (stricter
than v1, which refused only a template that wasn't text, and failed when it picked
the bad variant). They come from the file `TWR_RADIO_VOICE_FILE` names, and a bad
one stops radio at start; without it radio speaks the script.

The phrases have no abbreviations: speech splits sentences after any full stop
and a space, so "Approx. 3 s" would pause mid-sentence. A test keeps it so.

## The callouts

radio is the one consumer of the callouts ecu publishes (paddock#56), and applies
what ecu decides, the level, the priority, `hold_until`, `expires_at` and the
phrase key, with no rules of its own ([`callouts.py`](src/radio/callouts.py)):

- **Taking.** Each level has a durable pull consumer on the `CALLOUTS` stream,
  `radio-<src>-p0`, `-p1` and `-p2`, so a restarted radio carries on where it
  stopped. radio fetches every level, p0 first, until nothing new comes, then
  phrases what it took in the order ecu published it (the stream's), before
  anything decides to speak or drop it, so a seeded replay says the same words.
  A callout whose kind lacks a slot it says is logged (`callout slot missing`)
  and still spoken; one radio can't read or phrase is logged and dropped.
- **Order.** As v1's queue: p0 before p1 before p2, then the priority, then the
  age. A callout waits until its `hold_until`, and more urgent ones that are due go
  ahead of it.
- **Staleness.** A callout past its `expires_at` is dropped, not spoken, at the
  stage it was in: `queued` (which covers waiting while another plays),
  `connecting` or `synthesis`, each logged as `callout dropped` with its stage.
  Without `expires_at` it never goes stale.
- **One at a time.** The next is taken only once the last was spoken or dropped;
  one that fails to play is logged and the next is still spoken.
- **Acks.** A callout is acked when radio starts to say it or drops it. One taken
  and not yet said comes back after 5 s (the consumers' `ack_wait`) if radio
  stopped, so a restart says each callout once and drops the ones that expired.

The speaking side is a sink (`radio.sink.Sink`: connect, prepare, play, release).

## The engineer

An engineer is who says the callouts: a name, a voice (the Piper model and its
`voice:` settings), a script (the templates) and, later, a personality
([`engineer.py`](src/radio/engineer.py)). It changes how a callout is said, the
words and the voice, never which callouts are said or when: ecu decides those.

There is one engineer today, `v1`: v1's voice and v1's script, from
`TWR_RADIO_VOICE_MODEL` and `TWR_RADIO_VOICE_FILE` as before. When radio speaks
with a voice, every engineer's model is checked and loaded at start, off the
event loop. radio chooses the
engineer per session, not once for the process: `Roster.pick(session_id)` gives
the configured one, else the default. Each session's callouts are phrased by
its engineer's phrasebook, seeded by the session's id, so a replay of a session,
with the session id ecu gave it, says the same words (paddock#90). That needs
ecu's replay to give seeded session ids, which it doesn't yet (it mints them
with `ulid.Make`); until it does, each replay's words are a fresh draw. A callout
outside a session, such as telemetry lost between sessions, is phrased unseeded,
as v1 phrased everything.

A second engineer is added as data: an `Engineer` in the roster, with its voice
model, voice settings and templates. The roster then picks the driver's choice,
or, when there is none, one at random seeded by the session id, so that two
replays of one recording get the same engineer (drawn from the roster sorted by
name, so the order it was built in doesn't matter). A personality, later,
changes how something is said, never which callouts are said: that is ecu's.
Nothing that takes, orders or times callouts changes.

## Sinks and timing

A replay, the parity check and the ephemeral environment need no Discord
(paddock#60). radio speaks to:

- `console`: a line a callout, v1's `ConsolePlayer`'s `[t] (rule) text`, with the
  kind and the id; `t` is ecu's `created_at`, in UTC.
- `wav`: each callout said is synthesised off the event loop and written to
  `<dir>/<nnn>-<rule>.wav`, numbered in the order said (v1's `WavDirPlayer`),
  and its line printed. The numbers carry on after the highest already in the
  directory, so a restart never overwrites a file.

`TWR_RADIO_TIMING` says how long each callout takes ([`timing.py`](src/radio/timing.py)):

- `live`: the wall clock, each callout as long as the sink takes to play it.
- `instant`: v1's replay. Each callout is said at its `created_at`, or when its
  hold ends, in the queue's order, takes no time, and never goes stale. For
  checking what is said.
- `simulated`: a speaker, as live, on the replay's clock: one callout at a time,
  each holding the channel for as long as it takes to say, with the staleness
  and hold rules on. For checking timing. How long a callout takes comes from the
  voice when radio has it (the `wav` sink, or the `console` sink with the model
  present), and else from an estimate fitted to the voice, about 2.4 words a
  second at `length_scale` 0.9 (`radio.speech.estimate`).

On a replay's clock, time is the callouts' own: ecu publishes them in the order
it raised them, so once radio has queued one created at `t`, nothing created
before `t` is still to come, and radio decides up to `t`. When nothing comes, the
clock creeps on with the wall clock, a second behind, so the last callouts are
said too; it stands still while radio is taking callouts, so a long burst is
never overtaken. A replay whose callouts are never more than a second apart in
wall time, where they were further apart in its own time, gives the same result
at any speed. A gap of more than a second in publishing (a stalled replayer, or
a replay slower than recorded) lets the clock creep past that gap, and a
callout published after it can then be said late or in another order.

With `TWR_RADIO_TIMELINE_FILE`, each callout settled is a JSON line:
`callout_id`, `rule`, `kind`, `text`, `engineer`, `at` (ecu's frame), `created_at`,
`queued`, `started`, `finished`, `dropped` (times in RFC 3339, UTC), `outcome`
(`spoken`, `expired` or `dropped`) and `stage` (where it expired or was dropped:
`queued`, `connecting`, `synthesis`, `not connected`, `phrasing`, `playing` or
`nothing to say`).

The tests run against a real `nats-server -js`: the binary `TWR_NATS_SERVER`
names, else `nats-server` on the `PATH`, else the pinned release, fetched once into
the user's cache and checked against its sha256 each time the tests run.

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
