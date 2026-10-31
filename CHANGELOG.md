# Changelog

## [0.1.5](https://github.com/those-who-run/radio/compare/v0.1.4...v0.1.5) (2026-10-31)


### Features

* **phrases:** the race summary names the track ecu sends ([#31](https://github.com/those-who-run/radio/issues/31)) ([300dfef](https://github.com/those-who-run/radio/commit/300dfef4aca4393092db17b7102be3d17fedb656))

## [0.1.4](https://github.com/those-who-run/radio/compare/v0.1.3...v0.1.4) (2026-10-30)


### Features

* **radio:** read ecu's pit state at start and after a reconnect ([#28](https://github.com/those-who-run/radio/issues/28)) ([73d5d17](https://github.com/those-who-run/radio/commit/73d5d17f35b8f8ec58f72aedccdcd3c0f3b62818))


### Bug Fixes

* **radio:** count listeners from the channel when speaking, and warn only once a callout plays ([#29](https://github.com/those-who-run/radio/issues/29)) ([78e4d63](https://github.com/those-who-run/radio/commit/78e4d63155af4c882b2d845cfb6b66dc798c78ce))

## [0.1.3](https://github.com/those-who-run/radio/compare/v0.1.2...v0.1.3) (2026-10-30)


### Features

* **radio:** its status in the live bucket, and when each callout finished ([#25](https://github.com/those-who-run/radio/issues/25)) ([171f1d6](https://github.com/those-who-run/radio/commit/171f1d6b7594e40a8cf064f7d7c40e96b11b9ae3))
* **radio:** know who is listening, and warn when talking to an empty channel ([#26](https://github.com/those-who-run/radio/issues/26)) ([777b5fa](https://github.com/those-who-run/radio/commit/777b5fa7eb68c1a8885757679baedfc1df7741b1))

## [0.1.2](https://github.com/those-who-run/radio/compare/v0.1.1...v0.1.2) (2026-10-30)


### Features

* **radio:** take back what was queued at a pit entry, by its pit_lane ([#23](https://github.com/those-who-run/radio/issues/23)) ([e54e7c9](https://github.com/those-who-run/radio/commit/e54e7c9636673adffcea35739e2fd9bfd21ab6e6))

## [0.1.1](https://github.com/those-who-run/radio/compare/v0.1.0...v0.1.1) (2026-10-29)


### Features

* **radio:** speak in Discord through the bot whose token it's given ([#19](https://github.com/those-who-run/radio/issues/19)) ([5fc84f2](https://github.com/those-who-run/radio/commit/5fc84f26ef7dbc297e6acc4649c2efb195bd2fc6))

## 0.1.0 (2026-10-29)


### Features

* **radio:** console and WAV sinks, instant and simulated timing on the replay's clock, and the engineer ([#18](https://github.com/those-who-run/radio/issues/18)) ([aed915e](https://github.com/those-who-run/radio/commit/aed915ed7680bec0e0eb6a6e8159f4f139df7045))
* **radio:** phrase callouts with v1's script, phrasebook and number words ([#16](https://github.com/those-who-run/radio/issues/16)) ([a3efa7f](https://github.com/those-who-run/radio/commit/a3efa7f99d69874cd714b63c30750f18f3b26b9e))
* **radio:** Piper synthesis, sentence by sentence, with a clip cache and the voice settings ([#14](https://github.com/those-who-run/radio/issues/14)) ([7776d11](https://github.com/those-who-run/radio/commit/7776d11988c5d518fe94bff4c37a03baef6ee373))
* **radio:** take callouts from the CALLOUTS stream, p0 first, and drop them past expires_at ([#17](https://github.com/those-who-run/radio/issues/17)) ([d91ed4d](https://github.com/those-who-run/radio/commit/d91ed4df812f0894fe114e9341c332caffb96268))
* **radio:** the Python package, the /healthz entry point and the twr-radio image ([#11](https://github.com/those-who-run/radio/issues/11)) ([91c9c03](https://github.com/those-who-run/radio/commit/91c9c03226e75f822a7e0603b6c5ce5ae418d2da))


### Dependencies

* update all dependencies ([8cf83e4](https://github.com/those-who-run/radio/commit/8cf83e48a4b22dff35a160fe02995b748d90b6ec))
* update dependency python to 3.15 ([#5](https://github.com/those-who-run/radio/issues/5)) ([8cf83e4](https://github.com/those-who-run/radio/commit/8cf83e48a4b22dff35a160fe02995b748d90b6ec))
