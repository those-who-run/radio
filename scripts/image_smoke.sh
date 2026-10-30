#!/usr/bin/env bash
# The image's smoke test, the `smoke` command image.yml in those-who-run/.github runs
# on the image it built, as $IMAGE: on every PR from image.yml here (`image / build`)
# and on a release tag from release.yml, before that same build is published. It runs
# the image as it would be deployed, as its non-root user with a read-only root
# filesystem (paddock#55, AC2 and AC3). Without a NATS server it must answer
# GET /healthz with 503 and "connected": false while it retries; with one
# (nats-server -js) it must log "ready" and answer 200, each within 10 s.
set -euo pipefail
: "${IMAGE:?IMAGE names the image to test}"

docker run --rm --read-only "$IMAGE" --version
user=$(docker image inspect --format '{{.Config.User}}' "$IMAGE")
if [ "$user" != 65532:65532 ]; then
  echo "the image runs as '$user', not 65532:65532" >&2
  exit 1
fi
docker network create radio-smoke >/dev/null
nats=$(docker run --detach --network radio-smoke --name nats \
  docker.io/library/nats:2.15.2-alpine@sha256:823e7f1d6d8ffe3b30dbd73d4a06bbb939acf94929f7fdf224404da7a05bafb5 -js)
alone=$(docker run --detach --read-only --publish 127.0.0.1:8080:8080 \
  --env TWR_NATS_URL=nats://127.0.0.1:4222 "$IMAGE")
with=$(docker run --detach --read-only --network radio-smoke --publish 127.0.0.1:8081:8080 \
  --env TWR_NATS_URL=nats://nats:4222 "$IMAGE")
trap 'for c in "$alone" "$with"; do docker logs "$c"; done; docker rm --force "$alone" "$with" "$nats" >/dev/null; docker network rm radio-smoke >/dev/null' EXIT
# $1 the port, $2 a pattern the answer and its status match, $3 what the log
# of container $4 must hold.
wait_for() {
  for _ in $(seq 20); do
    body=$(curl --silent --show-error --write-out ' %{http_code}' "http://127.0.0.1:$1/healthz") || body=
    case $body in
      $2) if docker logs "$4" 2>&1 | grep -q "$3"; then echo "$body"; return 0; fi ;;
    esac
    sleep 0.5
  done
  echo "GET :$1/healthz did not match '$2', or the log has no '$3', within 10 s: $body" >&2
  return 1
}
wait_for 8080 '*"connected": false, "ok": false,*503' '"msg": "nats error"' "$alone"
wait_for 8081 '*"connected": true, "ok": true,*200' '"msg": "ready"' "$with"
