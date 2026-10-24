# Radio

The voice engineer for Those Who Run v2: it turns callouts from NATS
into speech in Discord, with the pit-lane policy.
It is written in Python, as Piper and Discord voice need, and speaks as
a second bot beside v1's.

Issues are off in this repository. Every issue, the roadmap and the decisions
(ADRs) live in [those-who-run/paddock](https://github.com/those-who-run/paddock). A pull request here closes
its paddock issue with `Closes those-who-run/paddock#N.`

## Checks

`make check` runs what CI runs; [`AGENTS.md`](AGENTS.md) says what that is and
what a new clone runs first.
