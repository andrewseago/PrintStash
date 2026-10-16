# Run PrintStash on Apple `container`

`printstash.sh` starts the default single-container stack from `docker-compose.yml`
(web UI and API in one container, SQLite and local storage on one volume) on Apple's
[`container`](https://github.com/apple/container) runtime, and builds the image on
it. The advanced and manual-testing compose stacks (PostgreSQL, S3, split workers)
stay on Docker. Setting up `container` itself (version, kernel, the macOS 27 pf
hang, Local Network permission) is in [README.md](README.md).

## Use

```bash
deploy/apple-container/printstash.sh up              # create or start, wait until it answers
deploy/apple-container/printstash.sh status
deploy/apple-container/printstash.sh logs -f
deploy/apple-container/printstash.sh down            # remove the container, keep the data
```

`up` publishes `127.0.0.1:3000` and waits for `/api/v1/health` and `/` to answer. It is
idempotent:

| State | `up` does |
| --- | --- |
| no container | checks the port on `127.0.0.1` and `::1`, then runs it |
| running, same image/port/memory | prints the address, changes nothing |
| running, different | stops with exit 1; `--recreate` replaces it (data kept) |
| stopped, same | starts it |
| stopped, different | prints its last log lines, removes it, runs the new one |

Options: `--image REF`, `--port N`, `--pull`, `--recreate`. Defaults follow
`docker-compose.yml`: `ghcr.io/xiao-villamor/printstash:latest`, overridden by
`PRINTSTASH_IMAGE` and `PRINTSTASH_VERSION`; port `3000` (`PRINTSTASH_HTTP_PORT`);
memory `4g` (`PRINTSTASH_MEMORY`). Memory is explicit because each container is a VM
that defaults to 1 GiB, and the app sizes its native-worker budget from it.

Data lives on the named volume `printstash` (`/data`), which `down` and a system
restart keep. Use a named volume rather than a host folder: the entrypoint changes
ownership under `/data`, which Apple's virtiofs bind mounts do not allow.

Differences from `docker compose up`, all deliberate:

- `VAULT_RESTART_ENABLED` is not set. Nothing restarts this container, so a Settings
  restart would stop it for good.
- There is no restart policy: after a reboot, run `up` again.
- The apiserver is started for you (`container system start`) behind a timeout. A hang
  there is apple/container#2275, and the message says so.

## Build

```bash
deploy/apple-container/printstash.sh build [--variant full|lite]
deploy/apple-container/printstash.sh up --image printstash:local --recreate
```

`build` runs three `container build` calls: `api-image` from `backend/` (with the
variant), `frontend-image` from `frontend/`, then `printstash:local` from
`backend/unified/`, which consumes the first two by name. That replaces the named
build contexts of `docker-bake.hcl`, which `container build` does not have.

Every `container build` resizes the builder to its own `--cpus` and `--memory`, so
all three get the same: 8 CPUs and 16 GiB by default (`PRINTSTASH_BUILD_CPUS`,
`PRINTSTASH_BUILD_MEMORY`). Left at the defaults, each build would shrink the builder
back to 2 CPUs and 2 GiB. A full build took about 3.5 minutes on an M5 Max.

If a build fails, the script names the image, stops, and suggests running a published
image instead. It never substitutes one.

Notes:

- The builder reaches the network through the same path as `container run`. A
  full-tunnel VPN can break that egress (apple/container#1307); the build above was
  run with the VPN disconnected. Behind a TLS-intercepting proxy the builder needs the
  proxy's CA, as a Docker build would.
- `frontend/.dockerignore` excludes `node_modules` at any depth, so a checkout's
  installed packages are not uploaded to the builder.
- Only macOS tools are used, and bash 3.2 is enough.

## Not covered

The advanced stack (`docker-compose.advanced.yml`), split workers, the manual testing
stack and the release scripts need Docker. Running the test suite's services on
`container` is a separate path: see [README.md](README.md).
