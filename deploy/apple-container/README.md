# Test services on Apple `container`

This fork can run the suite's real services (PostgreSQL, SeaweedFS, Nextcloud,
OpenSSH) on Apple's [`container`](https://github.com/apple/container) runtime
instead of Docker. It is a local development path for an Apple-silicon Mac. CI
stays on Docker: hosted runners cannot run `container`.

`backend/conftest.py` loads `backend/tests/apple_container.py`, which replaces the
functions in `tests/containers.py` that start the services. On any platform other
than macOS it does nothing. The tests, their markers and the pinned images are the
same ones Docker runs.

## Setup (once)

1. **Install `container` 1.5.x** (`>=1.5.0,<1.6`; the adapter checks both the client
   and the apiserver and stops the run otherwise):

   ```bash
   container system stop                       # if an older one is running
   sudo /usr/local/bin/update-container.sh -v 1.5.0
   ```

2. **Start the apiserver with a bound.** On macOS 27 a hung `com.apple.pfd` makes
   every `container` command wait forever (apple/container#2275, survives reboots
   until pf is healthy again). Application Firewall stealth mode avoids the
   trigger:

   ```bash
   launchctl print system/com.apple.pfd | grep -E 'state|runs|last exit'
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setstealthmode on
   /usr/bin/timeout 600 container system start --enable-kernel-install
   ```

   If `runs` climbs with `last exit code = 3`, that is the wedge; a reboot clears it.

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
| `postgres`, `s3`, `remote_storage` | yes, through the adapter |
| `bgcode` | no: builds the converter with `docker buildx` |
| `tests/e2e/test_search_image_proxy.py`, `tests/e2e/test_search_query_logs.py` | no: run nginx with `docker run --network host` |
| the image lane (`./scripts/test.sh image`), the Playwright real-backend, delivery and startup suites, release scripts | no: Docker |
| `docker-compose.advanced.yml`, `deploy/manual-testing/` | no: Docker compose |

Tests that need Docker stop the run at collection and name each one. To run
everything else, leave them out explicitly:

```bash
./scripts/test.sh full-resources --apple-container-deselect-docker-only
```

They are named in the terminal summary, not hidden. For `bgcode` alone, a locally
built converter can be used instead:
`VAULT_BGCODE_EXECUTABLE=/path/to/bgcode` (the tests build the official converter
through Docker, so the marker is still left out).

## Known limits

Measured on an M5 Max, macOS 27.2, `container` 1.5.0:

- **Nextcloud is unreliable.** Its pin is an amd64-only manifest, so it runs under
  Rosetta. The first requests of each web-server worker take tens of seconds, and a
  request that crosses PHP's 30 s limit comes back as a `200` error page. The
  adapter waits for an authenticated WebDAV listing, which covers the install but not
  every cold worker. Tests that use the Nextcloud container (the `nextcloud`
  parametrizations and the WebDAV presets in `test_storage_providers.py`) fail or
  stall intermittently; the WsgiDAV-backed and OpenSSH-backed contracts pass.
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
  is the service's own signal (a `psql` query, a log line, `/status.php`, `nc` inside
  the container), followed by one protocol probe through the published port.
- `lscr.io/linuxserver/...@sha256:<digest>` is pulled from `ghcr.io` under the same
  digest. 1.5.0 refuses the `lscr.io` credential exchange.
- Nextcloud's pin is an amd64-only manifest and runs under Rosetta with 2 GiB.
- Every container is labelled `printstash.test.owner=<pid>-<start time>`. A reaper
  removes `printstash-test-*` containers whose owner is gone, the way Ryuk does for
  Docker. After a hard kill, the next run cleans up.
- Every `container` command has a timeout; a hang stops the run naming #2275.

## Running the suite

```bash
cd backend
uv run pytest -m "(postgres or s3 or remote_storage) and not coverage_gate and not scale" tests
```

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
- A message naming `apple/container#2275`: check `launchctl print system/com.apple.pfd`.
- A message naming Local Network: grant it (step 4), then re-run.
- Leftovers: `container list --all` shows `printstash-test-*`; they are removed by
  the next run, or by hand with `container delete --force <name>`.
