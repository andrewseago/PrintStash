# Run PrintStash on Apple `container`

`printstash.sh` starts the default single-container stack from `docker-compose.yml`
(web UI and API in one container, SQLite and local storage on one volume) on Apple's
[`container`](https://github.com/apple/container) runtime, and builds the image on
it. The advanced and manual-testing compose stacks (PostgreSQL, S3, split workers)
stay on Docker. Setting up `container` itself (version, kernel, the macOS 27 pf
hang, Local Network permission) is in [README.md](README.md); the script does not run
the test adapter's version or Local Network checks, though it names Local Network
when the published port drops connections.

## Use

```bash
deploy/apple-container/printstash.sh up              # create or start, wait until it answers
deploy/apple-container/printstash.sh status
deploy/apple-container/printstash.sh logs -f
deploy/apple-container/printstash.sh restart         # stop and start, nothing else changes
deploy/apple-container/printstash.sh down            # remove the container, keep the data
```

`up` publishes `127.0.0.1:3000` and waits for `/api/v1/health` and `/` to answer,
for at most `PRINTSTASH_HEALTH_TIMEOUT` seconds (180). It changes nothing that already
matches, and replaces a container only when told to:

| State | `up` does |
| --- | --- |
| no container | checks the port on `127.0.0.1` and `::1`, then runs it |
| running, matching | checks that it answers, changes nothing |
| stopped, matching | starts it and waits until it answers |
| running or stopped, different | stops with exit 1 naming what differs; `--recreate` replaces it (data kept) |
| created by something else | refuses to touch it |

"Matching" means the same image reference *and* digest, port, memory, volume and
settings file. A rebuilt `printstash:local`, or a newer image pulled under the same
tag, is a change, so it is never left running by mistake.

Before replacing a container, `up` pulls or checks the new image and the new port, so
a typo or a failed pull leaves the running instance alone.

Options:

- `--image REF`: defaults follow `docker-compose.yml`:
  `ghcr.io/xiao-villamor/printstash:latest`, overridden by `PRINTSTASH_IMAGE` and
  `PRINTSTASH_VERSION`. Like compose, the script reads those two and
  `PRINTSTASH_HTTP_PORT` from the repository's `.env` when the environment does not
  set them; nothing else in `.env` is read.
- `--port N`: default `3000` (`PRINTSTASH_HTTP_PORT`).
- `--env-file FILE`: `VAULT_*` and `NGINX_*` settings, one `KEY=value` per line, as in
  [docs/deployment.md](../../docs/deployment.md). Changing the file's contents is a
  change of spec, so apply it with `up --env-file FILE --recreate`.
- `--pull`: fetch the image again first. Then `--recreate` if it changed.
- `--recreate`: replace the container even if it matches.
- Memory: `4g` (`PRINTSTASH_MEMORY`). It is explicit because each container is a VM
  that defaults to 1 GiB, and the app sizes its native-worker budget from it.

To update to a newly published image: `up --pull --recreate`. To run a new local
build: `build`, then `up --image printstash:local --recreate`.

Do not switch an existing volume to an older image than the one that last ran on it:
the newer one has already upgraded the database, and the older one stops at startup
(`Can't locate revision`). Downgrades are not supported, here or on Docker.

## Data

Everything lives on the named volume `printstash` (`/data`). `down`, `restart` and a
system restart keep it. Use a named volume rather than a host folder: the entrypoint
changes ownership under `/data`, which Apple's virtiofs bind mounts do not allow.

**These permanently delete the library, its database and its local backups:**
`container volume delete printstash`, `container volume prune` while no container uses
it (after `down`), and `uninstall-container.sh -d`. Uninstall `container` with `-k` to
keep volumes.

A new volume is created on first `up`, and the script says so. It is a sparse 512 GiB
disk image by default and cannot grow later. On a Mac with less free space than that,
PrintStash's free-space checks see the volume's size, not the disk's; and a library that
may outgrow it needs a bigger one. Either way, set the size before the first `up`:
`PRINTSTASH_VOLUME_SIZE=200G printstash.sh up` (or `container volume create -s 200G
printstash`). Another volume name: `PRINTSTASH_VOLUME`. Changing it is a change of spec.

To bring a library over from Docker, with both runtimes installed and the Docker stack
stopped:

```bash
docker run --rm -v printstash:/data -v "$PWD":/out alpine tar -C /data -cf /out/printstash.tar .
PRINTSTASH_VOLUME_SIZE=512G deploy/apple-container/printstash.sh up    # creates the volume
deploy/apple-container/printstash.sh down
container run --rm -v printstash:/data -v "$PWD":/in alpine tar -C /data -xf /in/printstash.tar
deploy/apple-container/printstash.sh up
```

## Stopping

`down` and `restart` give the app 60 seconds to stop, as compose does. Nothing else
does:

- A logout, reboot or shutdown ends the container's VM like a power cut, and
  `container system stop` (which a `container` upgrade also needs) gives it 5 seconds.
  Run `printstash.sh down` first.
- There is no restart policy: after a reboot, run `up` again.

## Differences from `docker compose up`

- Only `127.0.0.1` is published, so other devices on the network cannot reach it. That
  is also what lets `trusted_network` first-run setup apply only to this Mac.
- `VAULT_RESTART_ENABLED` is not set. Its dialog promises the app returns by itself,
  and nothing here would bring it back until `up` ran. Settings that say "restart
  required" are applied with `printstash.sh restart`.
- Settings come from `--env-file`, not from compose's environment block.
- The apiserver is started for you (`container system start`). Every `container` call
  is bounded, and a hang is named as apple/container#2275.

## Build

```bash
deploy/apple-container/printstash.sh build [--variant full|lite]
deploy/apple-container/printstash.sh up --image printstash:local --recreate
```

`build` runs three `container build` calls: `api-image` from `backend/` (with the
variant), `frontend-image` from `frontend/`, then `printstash:local` from
`backend/unified/`, which consumes the first two by name. That replaces the named
build contexts of `docker-bake.hcl`, which `container build` does not have. Those
three names are global, so only one build runs at a time; a second one stops with a
message naming the lock.

Every `container build` resizes the builder to its own `--cpus` and `--memory`, so
all three get the same: 8 CPUs and 16 GiB by default (`PRINTSTASH_BUILD_CPUS`,
`PRINTSTASH_BUILD_MEMORY`). Left at the defaults, each build would shrink the builder
back to 2 CPUs and 2 GiB. A full build took about 3.5 minutes on an M5 Max.

If a build fails, the script names the image, stops, and suggests running the
configured published image instead. It never substitutes one.

Notes:

- The builder reaches the network through the same path as `container run`. A
  full-tunnel VPN can break that egress (apple/container#1307); the build above was
  run with the VPN disconnected. Behind a TLS-intercepting proxy the builder needs the
  proxy's CA, as a Docker build would.
- `frontend/.dockerignore` excludes `node_modules` at any depth, so a checkout's
  installed packages are not uploaded to the builder.
- Only macOS tools are used, and bash 3.2 is enough. The script works through a
  symlink on PATH.

## Not covered

The advanced stack (`docker-compose.advanced.yml`), split workers, the manual testing
stack and the release scripts need Docker. Running the test suite's services on
`container` is a separate path: see [README.md](README.md).
