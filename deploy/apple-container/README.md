# Test services on Apple `container`

This fork can run the suite's real services (PostgreSQL, SeaweedFS and OpenSSH) on
Apple's [`container`](https://github.com/apple/container) runtime instead of Docker.
It is a local development path for an Apple-silicon Mac. CI stays on Docker: hosted
runners cannot run `container`. Nextcloud is not run on it at all; the tests that need
it are Docker-only.

`backend/conftest.py` loads `backend/tests/apple_container.py`, which replaces the
functions in `tests/containers.py` that start the services. On any platform other
than macOS it does nothing. The tests, their markers and the pinned images are the
same ones Docker runs.

To run the app itself on `container`, see [APP.md](APP.md).

## Setup (once)

1. **Install `container` 1.5.x** (`>=1.5.0,<1.6`; the adapter checks both the client
   and the apiserver and stops the run otherwise). On a Mac without it, install
   `container-1.5.0-installer-signed.pkg` from
   [the 1.5.0 release](https://github.com/apple/container/releases/tag/1.5.0). To
   upgrade an older install:

   ```bash
   container system stop                       # if an older one is running
   sudo /usr/local/bin/update-container.sh -v 1.5.0
   ```

2. **Start the apiserver with a bound.** On macOS 27 a hung `com.apple.pfd` makes
   every `container` command wait forever (apple/container#2275). Application
   Firewall stealth mode avoids the trigger:

   ```bash
   launchctl print system/com.apple.pfd | grep -E 'state|runs|last exit'
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setstealthmode on
   /usr/bin/timeout 600 container system start --enable-kernel-install
   ```

   If `runs` climbs with `last exit code = 3`, that is the wedge. Turn stealth mode on,
   then reboot. A reboot without stealth mode brings the hang back with the next
   container.

3. **Install the recommended kernel.** An in-place upgrade keeps the 0.10 kernel:

   ```bash
   container system kernel set --recommended   # about 700 MB from GitHub
   container run --rm alpine uname -r          # 6.18.35
   ```

4. **Local Network.** Published ports need `container-runtime-linux` allowed under
   System Settings > Privacy & Security > Local Network; container addresses need
   the terminal app allowed. The adapter checks both and says which is missing.

## What runs where

| Marker or area | On Apple container |
| --- | --- |
| `postgres`, `s3`, `remote_storage` | yes, through the adapter, except the tests that need Nextcloud |
| tests that need Nextcloud: the `nextcloud` parametrizations and the WebDAV presets (`synology_webdav`, `qnap_webdav`, `hetzner_storage_box_webdav`, `koofr`) in `test_real_provider.py`, `test_webdav_cleanup.py`, `test_storage_providers.py`, `test_remote_backup.py` and `test_remote_library_scan.py` | no: Nextcloud is not run on Apple container |
| `bgcode` | no: builds the converter with `docker buildx` |
| `tests/e2e/test_search_image_proxy.py`, `tests/e2e/test_search_query_logs.py` | no: run nginx with `docker run --network host` |
| the image lane (`./scripts/test.sh image`), the Playwright real-backend, delivery and startup suites, release scripts | no: Docker |
| `docker-compose.advanced.yml`, `deploy/manual-testing/` | no: Docker compose |

Tests that need Docker stop the run at collection and name each one. To run
everything else, leave them out explicitly:

```bash
./scripts/test.sh full-resources --apple-container-deselect-docker-only
```

They are named in the terminal summary, not hidden. The `bgcode` tests build the
official converter with `docker buildx`; there is no Apple path for that.

## Known limits

Measured on an M5 Max, macOS 27.2, `container` 1.5.0:

- **Nextcloud is not run.** Its pin is an amd64-only manifest, so it would run under
  Rosetta, where the first requests of each web-server worker outlast PHP's 30 s limit
  and come back as a `200` error page. Rather than run it unreliably, the tests that
  need it are Docker-only. They are found by file and name: a test in one of the
  files listed in `NEXTCLOUD_FILES` whose name or parameter id contains an entry of
  `NEXTCLOUD_IDS`. A test that reaches Nextcloud without matching fails with a message
  saying how to classify it.
- **Many tests fail on this Mac with or without the adapter**, because of macOS, not
  the runtime: the native mesh and inference workers exit non-zero (`worker_failed`),
  the local filesystem probe reports no verified delete (so storage runs in the
  "guarded" tier), and sqlite-vec is not loadable. About 650 tests in the `pr` lane
  fail the same way on an unmodified `main`.
- Image pulls, volume creation and the first run of an image unpack it, so a cold
  run is slower than Docker's.

## Behaviour that differs from Docker

- Endpoints are `127.0.0.1:<port>`, never `localhost`: one address is published per
  port.
- The port forwarder accepts a connection before the service listens, so readiness
  is the service's own signal (a `psql` query, a log line, `nc` inside the container),
  followed by one protocol probe through the published port.
- `lscr.io/linuxserver/...@sha256:<digest>` is pulled from `ghcr.io` under the same
  digest. 1.5.0 refuses the `lscr.io` credential exchange.
- Every container is labelled `printstash.test.owner=<pid>-<start time>`, and a
  session removes its own containers when it ends. After a hard kill they keep
  running until the next run that starts a service: its reaper removes
  `printstash-test-*` containers whose owner is gone. There is no Ryuk; remove them
  sooner by hand (see below).
- Every `container` command has a timeout. A hang stops the run naming #2275, except
  an image pull that outlasts its 30 minutes: that fails naming the command to pull
  the image by hand.

## Running the suite

```bash
cd backend
uv run pytest -m "(postgres or s3 or remote_storage) and not coverage_gate and not scale" \
  tests --apple-container-deselect-docker-only
```

Without the flag the run stops at collection and names the tests that need Docker.

`-n auto` is 18 workers on an M5 Max, and each can start its own service VMs of
4 vCPUs and 1 GiB. Cap them for the resource lanes:

```bash
PYTEST_XDIST_AUTO_NUM_WORKERS=4 ./scripts/test.sh e2e tests/e2e/test_opendal_gc.py \
  tests/e2e/test_remote_backup.py --apple-container-deselect-docker-only
```

To bypass the adapter and use Docker, even on a Mac: `-p no:tests.apple_container`.

## Processes outside pytest

These import `tests.containers` but never run under the plugin, so they still need
Docker:

- `tests/e2e/runtime_image.py` (the image lane)
- `tests/fakes/browser_delivery.py` (the Playwright delivery backend)
- `tests/fakes/s3_delivery.py` (its S3 side)

## If something goes wrong

- `container system status` exits 1: start it (step 2).
- A message naming `apple/container#2275`: check `launchctl print system/com.apple.pfd`,
  turn stealth mode on, then reboot (step 2).
- A message naming Local Network: grant it (step 4), then re-run.
- Leftovers: `container list --all` shows `printstash-test-*`; they are removed by
  the next run, or by hand with `container delete --force <name>`.
