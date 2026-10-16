"""Run the suite's real services on Apple's `container` instead of Docker.

`tests/containers.py` starts PostgreSQL, SeaweedFS, Nextcloud and OpenSSH through
testcontainers, which needs a Docker Engine API. On a Mac with Apple's `container`
runtime and no Docker daemon those tests stop with "no Docker daemon is reachable".
This plugin swaps the functions that start PostgreSQL, SeaweedFS and OpenSSH for ones
that drive the `container` CLI, so the same tests run against the same pinned images.
It does not change what any test asserts, and it changes nothing on any other
platform. Nextcloud is not run at all: tests that need it are Docker-only.

How it fits in
--------------
* `backend/conftest.py` loads this module as a plugin. `pytest_configure` calls
  `activate`, which does nothing unless the platform is darwin.
* On darwin `install` replaces the seven names in `PATCHED` on the `tests.containers`
  module. The rest of that module (`_resolve`, `_resolved`, `_started`,
  `shutdown_containers`, `fresh_postgres_database`, `s3_private_endpoint`) stays
  upstream's, so caching, teardown and the S3 failure-domain logic are unchanged.
  A rename upstream makes `install` raise naming what is missing.
* Nothing runs a `container` command at import, configure or collection. The first
  command is the availability check of the first selected resource, so a run that
  starts nothing makes no calls.

What differs from Docker
------------------------
* Ports are published explicitly (`-p 127.0.0.1:<port>:<container port>`), one
  address per port, so endpoints are `127.0.0.1`, never `localhost`.
* The port forwarder accepts a connection before the service listens, so a TCP check
  proves nothing. Readiness is the service's own signal, followed by one protocol
  probe through the published port.
* `lscr.io` images are pulled from `ghcr.io` under the same digest: 1.5.0 refuses the
  `lscr.io` credential exchange.
* There is no Ryuk. A reaper removes containers whose owning process is gone.

A stopped apiserver, an unsupported version, a hung command and a missing Local
Network permission each stop the run with one message naming the fix. Tests that
only work with Docker (`bgcode`, the two nginx end-to-end files, and every test that
needs Nextcloud) are named in a collection stop, or left out with
`--apple-container-deselect-docker-only`.
"""

from __future__ import annotations

import dataclasses
import enum
import errno
import fcntl
import hashlib
import http.client
import json
import os
import platform as host_platform
import re
import secrets
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NoReturn

import psutil
import pytest

CONTAINER: Final = "container"
MIN_VERSION: Final = (1, 5, 0)
MAX_VERSION_EXCLUSIVE: Final = (1, 6, 0)
PUBLISH_HOST: Final = "127.0.0.1"
NAME_PREFIX: Final = "printstash-test-"
OWNER_LABEL: Final = "printstash.test.owner"
SHORT_TIMEOUT_S: Final = 30
RUN_TIMEOUT_S: Final = 600
PULL_TIMEOUT_S: Final = 1800
PROBE_ATTEMPTS: Final = 3
KEYSCAN_ATTEMPTS: Final = 20

DESELECT_OPTION: Final = "--apple-container-deselect-docker-only"

# Not inline in `containers.py`'s start functions, so the adapter repeats it. The
# drift test compares it with the literal in that file.
PGVECTOR_IMAGE: Final = (
    "pgvector/pgvector:0.8.0-pg16"
    "@sha256:a132765ec351c65111b5b675928a3a0515a466a40f97277329db8b8209ad8bc9"
)
PGVECTOR_RESOURCE: Final = "PostgreSQL with pgvector"

# Tests that need a Docker Engine API whatever the runtime. Paths are relative to
# the tests directory; markers are matched on the item, never on its keywords.
DOCKER_ONLY_FILES: Final = (
    "e2e/test_search_image_proxy.py",
    "e2e/test_search_query_logs.py",
)
DOCKER_ONLY_MARKERS: Final = ("bgcode",)

# Nextcloud is not run on Apple container: its pin is an amd64-only manifest, and under
# Rosetta the first requests of each web-server worker outlast PHP's 30 s limit. A test
# needs it when it is in one of these files and names Nextcloud, or one of the WebDAV
# presets that `tests/fixtures/storage_presets.py` points at it, in its parameter id or
# its function name. Other tests in the same files (SFTP, S3, WsgiDAV) run normally.
NEXTCLOUD_FILES: Final = (
    "contract/modules/storage/storage_opendal/test_real_provider.py",
    "contract/modules/storage/storage_opendal/test_webdav_cleanup.py",
    "contract/modules/storage/test_storage_providers.py",
    "e2e/test_remote_backup.py",
    "e2e/test_remote_library_scan.py",
)
NEXTCLOUD_IDS: Final = (
    "nextcloud",
    "synology_webdav",
    "qnap_webdav",
    "hetzner_storage_box_webdav",
    "koofr",
)

PATCHED: Final = (
    "docker_available",
    "require_docker",
    "_start_postgres",
    "_start_seaweedfs",
    "_start_nextcloud",
    "_start_openssh",
    "pgvector_url",
)
# Everything else `install` reads from `tests.containers`.
RELIED_ON: Final = (
    "_resolve",
    "_resolved",
    "_started",
    "shutdown_containers",
    "fresh_postgres_database",
    "s3_private_endpoint",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "SEAWEEDFS_IMAGE",
    "SEAWEEDFS_S3_PORT",
    "SEAWEEDFS_COMMAND",
    "SEAWEEDFS_READY_LOG",
    "OPENSSH_IMAGE",
    "POSTGRES_IMAGE",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "STARTUP_TIMEOUT_S",
    "CONTAINER_START_ATTEMPTS",
    "_TRANSIENT_START_MARKERS",
)

NEXTCLOUD_UNSUPPORTED: Final = (
    "Nextcloud is not run on Apple container, so this test needs Docker. Add it to "
    "NEXTCLOUD_FILES or NEXTCLOUD_IDS in tests/apple_container.py so it is named up "
    "front; see deploy/apple-container/README.md."
)

_APPLE_TRANSIENT_MARKERS: Final = (
    "xpc connection error",
    "connection invalid",
    "timed out",
    "temporarily unavailable",
)


class AdapterDriftError(RuntimeError):
    """`tests/containers.py` no longer has a name this adapter relies on."""


class CliTimeout(Exception):
    """A `container` command did not answer in time. Never retried."""

    def __init__(self, argv: Sequence[str], timeout: int) -> None:
        super().__init__(f"{' '.join(argv[:3])} did not answer within {timeout}s")
        self.argv = tuple(argv)
        self.timeout = timeout


class RuntimeUnavailable(Exception):
    """The `container` runtime cannot run services; the message names the fix."""


class StartFailure(RuntimeError):
    """A service did not start or never became ready."""


class LocalNetworkDenied(Exception):
    """A published port or a container address is blocked by Local Network privacy."""


CliRunner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


def run_cli(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one command to completion, turning a hang into `CliTimeout`."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliTimeout(argv, timeout) from exc
    except FileNotFoundError as exc:
        raise RuntimeUnavailable(
            f"`{argv[0]}` is not installed or not on PATH"
        ) from exc


def _http_status(url: str, timeout: float) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _tcp_reply(port: int, payload: bytes | None, timeout: float) -> bytes:
    with socket.create_connection((PUBLISH_HOST, port), timeout=timeout) as peer:
        if payload is not None:
            peer.sendall(payload)
        return peer.recv(64)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((PUBLISH_HOST, 0))
        return probe.getsockname()[1]


def _own_token() -> str:
    return _owner_token(os.getpid())


def _owner_token(pid: int) -> str:
    return f"{pid}-{psutil.Process(pid).create_time():.2f}"


def _owner_alive(token: str) -> bool:
    """Whether the process that wrote *token* still exists as the same process."""
    pid_text, _, _start = token.partition("-")
    pid = int(pid_text)
    if not psutil.pid_exists(pid):
        return False
    try:
        return _owner_token(pid) == token
    except psutil.NoSuchProcess:
        return False


@dataclasses.dataclass(frozen=True)
class Seams:
    """Everything the runtime reaches outside itself through, so tests inject fakes."""

    cli: CliRunner = run_cli
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    free_port: Callable[[], int] = _free_port
    http_status: Callable[[str, float], int] = _http_status
    tcp_reply: Callable[[int, bytes | None, float], bytes] = _tcp_reply
    own_token: Callable[[], str] = _own_token
    owner_alive: Callable[[str], bool] = _owner_alive
    lock_dir: Path = dataclasses.field(
        default_factory=lambda: (
            Path(tempfile.gettempdir()) / "printstash-apple-container"
        )
    )
    arch: str = dataclasses.field(
        default_factory=lambda: (
            "arm64" if host_platform.machine() in ("arm64", "aarch64") else "amd64"
        )
    )


@dataclasses.dataclass(frozen=True)
class LogLine:
    text: str


@dataclasses.dataclass(frozen=True)
class ExecOk:
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()


Readiness = LogLine | ExecOk


class ProbeKind(str, enum.Enum):
    """The protocol the host speaks to the published port once the service is ready."""

    POSTGRES = "postgres"
    HTTP = "http"
    SSH = "ssh"


@dataclasses.dataclass(frozen=True)
class ServiceSpec:
    key: str
    image: str
    container_port: int
    readiness: Readiness
    probe: ProbeKind | None
    env: tuple[tuple[str, str], ...] = ()
    command: tuple[str, ...] = ()


def registry_reference(image: str) -> str:
    """The reference to pull *image* from.

    1.5.0 refuses to exchange credentials between `lscr.io` and the `ghcr.io` it
    redirects to. LinuxServer publishes the same content on both, so a digest-pinned
    `lscr.io` reference is pulled from `ghcr.io` unchanged. Without a digest the
    content is not pinned, so there is nothing to carry across.
    """
    prefix = "lscr.io/linuxserver/"
    if not image.startswith(prefix):
        return image
    if "@sha256:" not in image:
        raise ValueError(
            f"{image} is not pinned by digest, so it cannot be re-pointed at ghcr.io"
        )
    return "ghcr.io/linuxserver/" + image.removeprefix(prefix)


def build_specs(containers: ModuleType) -> dict[str, ServiceSpec]:
    """One spec per service, from the pins and constants in `tests/containers.py`."""
    postgres_env = (
        ("POSTGRES_USER", containers.POSTGRES_USER),
        ("POSTGRES_PASSWORD", containers.POSTGRES_PASSWORD),
        ("POSTGRES_DB", containers.POSTGRES_DB),
    )
    psql = ExecOk(
        argv=(
            "psql",
            "--username",
            containers.POSTGRES_USER,
            "--dbname",
            containers.POSTGRES_DB,
            "--host",
            "127.0.0.1",
            "-c",
            "select version();",
        ),
        env=(("PGPASSWORD", containers.POSTGRES_PASSWORD),),
    )
    return {
        "postgres": ServiceSpec(
            key="postgres",
            image=containers.POSTGRES_IMAGE,
            container_port=5432,
            readiness=psql,
            probe=ProbeKind.POSTGRES,
            env=postgres_env,
        ),
        "pgvector": ServiceSpec(
            key="pgvector",
            image=PGVECTOR_IMAGE,
            container_port=5432,
            readiness=psql,
            probe=ProbeKind.POSTGRES,
            env=postgres_env,
        ),
        "seaweedfs": ServiceSpec(
            key="seaweedfs",
            image=containers.SEAWEEDFS_IMAGE,
            container_port=containers.SEAWEEDFS_S3_PORT,
            readiness=LogLine(containers.SEAWEEDFS_READY_LOG),
            probe=ProbeKind.HTTP,
            env=(
                ("AWS_ACCESS_KEY_ID", containers.S3_ACCESS_KEY),
                ("AWS_SECRET_ACCESS_KEY", containers.S3_SECRET_KEY),
            ),
            command=tuple(shlex.split(containers.SEAWEEDFS_COMMAND)),
        ),
        "openssh": ServiceSpec(
            key="openssh",
            image=containers.OPENSSH_IMAGE,
            container_port=2222,
            readiness=ExecOk(argv=("nc", "-z", "127.0.0.1", "2222")),
            probe=ProbeKind.SSH,
            env=(
                ("USER_NAME", "contract"),
                ("USER_PASSWORD", "contract-only"),
                ("PASSWORD_ACCESS", "true"),
                ("SUDO_ACCESS", "false"),
            ),
        ),
    }


def _message_wedge(error: CliTimeout) -> str:
    return (
        f"a `container` command hung ({error}). On macOS 27 this is "
        "apple/container#2275: com.apple.pfd stopped answering and every command "
        "waits behind it. Check `launchctl print system/com.apple.pfd` (state, runs, "
        "last exit code); a climbing `runs` count with exit code 3 confirms it, and "
        "only a reboot clears it. Turning on Application Firewall stealth mode "
        "beforehand avoids it."
    )


def _message_local_network() -> str:
    return (
        "the published port accepts a connection and then resets it. "
        "container-runtime-linux has no Local Network permission: enable it under "
        "System Settings > Privacy & Security > Local Network, then re-run."
    )


def _message_container_address(host: str) -> str:
    return (
        f"the container address {host} is unreachable from this process. Enable "
        "Local Network for the terminal app (System Settings > Privacy & Security > "
        "Local Network), then re-run."
    )


def _parse_version(text: str) -> tuple[int, ...]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        raise RuntimeUnavailable(f"cannot read the container version from {text!r}")
    return tuple(int(part) for part in match.groups())


class ServiceHandle:
    """What `containers._started` holds: `shutdown_containers` calls `stop()`."""

    def __init__(self, runtime: AppleRuntime, name: str) -> None:
        self.runtime = runtime
        self.name = name

    def stop(self) -> None:
        self.runtime.delete(self.name)


@dataclasses.dataclass(frozen=True)
class StartedService:
    name: str
    port: int
    address: str


class AppleRuntime:
    """Starts, waits for and removes service containers through the CLI."""

    def __init__(self, containers: ModuleType, seams: Seams | None = None) -> None:
        self.containers = containers
        self.seams = seams or Seams()
        self.specs = build_specs(containers)
        self._available = False
        self._reaped = False
        self._started_any = False

    # --- availability -----------------------------------------------------

    def check_available(self) -> None:
        """Raise `RuntimeUnavailable` unless a supported runtime is answering."""
        if self._available:
            return
        status = self.seams.cli(
            [CONTAINER, "system", "status", "--format", "json"], SHORT_TIMEOUT_S
        )
        if status.returncode != 0:
            raise RuntimeUnavailable(
                "the container apiserver is not running. Start it with "
                "`/usr/bin/timeout 600 container system start --enable-kernel-install`."
            )
        version = self.seams.cli(
            [CONTAINER, "system", "version", "--format", "json"], SHORT_TIMEOUT_S
        )
        entries = {
            entry["appName"]: entry["version"] for entry in json.loads(version.stdout)
        }
        client = entries.get("container")
        server = entries.get("container-apiserver")
        if client is None or server is None:
            raise RuntimeUnavailable(
                f"container reported {sorted(entries)}; both the client and the "
                "apiserver must answer."
            )
        for role, text in (("client", client), ("apiserver", server)):
            parsed = _parse_version(text)
            if not MIN_VERSION <= parsed < MAX_VERSION_EXCLUSIVE:
                raise RuntimeUnavailable(
                    f"container {role} {text} is outside the supported range "
                    f">=1.5.0,<1.6 (client {client}, apiserver {server})."
                )
        self._available = True

    def available(self) -> bool:
        try:
            self.check_available()
        except RuntimeUnavailable, CliTimeout:
            return False
        return True

    # --- images -----------------------------------------------------------

    def ensure_image(self, image: str, arch: str) -> str:
        """Pull *image* once across processes and return the reference to run."""
        reference = registry_reference(image)
        inspect = self.seams.cli(
            [CONTAINER, "image", "inspect", reference], SHORT_TIMEOUT_S
        )
        if inspect.returncode == 0:
            return reference
        self.seams.lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(reference.encode()).hexdigest()[:16]
        with open(self.seams.lock_dir / f"pull-{digest}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (
                self.seams.cli(
                    [CONTAINER, "image", "inspect", reference], SHORT_TIMEOUT_S
                ).returncode
                == 0
            ):
                return reference
            argv = [
                CONTAINER,
                "image",
                "pull",
                "--platform",
                f"linux/{arch}",
                "--progress",
                "none",
                reference,
            ]
            try:
                pulled = self.seams.cli(argv, PULL_TIMEOUT_S)
            except CliTimeout as exc:
                raise StartFailure(
                    f"pulling {reference} took longer than {PULL_TIMEOUT_S}s. Pull it "
                    f"by hand and re-run: `{' '.join(argv)}`"
                ) from exc
        if pulled.returncode != 0:
            raise StartFailure(
                f"pulling {reference} failed: {pulled.stderr.strip() or pulled.stdout.strip()}"
            )
        return reference

    # --- start ------------------------------------------------------------

    def start(self, spec: ServiceSpec) -> StartedService:
        self.check_available()
        self._reap_once()
        arch = self.seams.arch
        reference = self.ensure_image(spec.image, arch)
        attempts = self.containers.CONTAINER_START_ATTEMPTS
        for attempt in range(attempts):
            name = f"{NAME_PREFIX}{spec.key}-{os.getpid()}-{secrets.token_hex(4)}"
            port = self.seams.free_port()
            handle = ServiceHandle(self, name)
            self.containers._started.append(handle)
            self._started_any = True
            failure = self._run(spec, reference, arch, name, port)
            if failure is None:
                self._wait_ready(spec, name, port)
                self._probe(spec, port)
                return StartedService(name, port, self.container_address(name))
            handle.stop()
            retryable = self._retryable(failure)
            if not retryable or attempt == attempts - 1:
                raise StartFailure(f"container run failed for {spec.key}: {failure}")
            self.seams.sleep(2**attempt)
        raise AssertionError("container start retry loop exhausted without an outcome")

    def _run(
        self, spec: ServiceSpec, reference: str, arch: str, name: str, port: int
    ) -> str | None:
        argv = [
            CONTAINER,
            "run",
            "--detach",
            "--progress",
            "none",
            "--platform",
            f"linux/{arch}",
            "--name",
            name,
            "--label",
            f"{OWNER_LABEL}={self.seams.own_token()}",
            "-p",
            f"{PUBLISH_HOST}:{port}:{spec.container_port}",
        ]
        for key, value in spec.env:
            argv += ["-e", f"{key}={value}"]
        argv += [reference, *spec.command]
        result = self.seams.cli(argv, RUN_TIMEOUT_S)
        if result.returncode == 0:
            return None
        return result.stderr.strip() or result.stdout.strip() or "no output"

    def _retryable(self, failure: str) -> bool:
        text = failure.lower()
        markers = (
            *self.containers._TRANSIENT_START_MARKERS,
            *_APPLE_TRANSIENT_MARKERS,
            "address already in use",
        )
        return any(marker in text for marker in markers)

    # --- readiness --------------------------------------------------------

    def _wait_ready(self, spec: ServiceSpec, name: str, port: int) -> None:
        deadline = self.seams.monotonic() + self.containers.STARTUP_TIMEOUT_S
        diagnostic = "no attempt made"
        while True:
            ready, diagnostic = self._ready_once(spec, name, port)
            if ready:
                return
            if self.seams.monotonic() >= deadline:
                raise StartFailure(
                    f"{spec.key} was not ready after "
                    f"{self.containers.STARTUP_TIMEOUT_S}s ({diagnostic}). "
                    f"Last log lines:\n{self._log_tail(name)}"
                )
            self.seams.sleep(1)

    def _ready_once(self, spec: ServiceSpec, name: str, port: int) -> tuple[bool, str]:
        readiness = spec.readiness
        if isinstance(readiness, ExecOk):
            argv = [CONTAINER, "exec"]
            for key, value in readiness.env:
                argv += ["-e", f"{key}={value}"]
            argv += [name, *readiness.argv]
            result = self.seams.cli(argv, SHORT_TIMEOUT_S)
            return result.returncode == 0, result.stderr.strip() or "exec failed"
        result = self.seams.cli([CONTAINER, "logs", name], SHORT_TIMEOUT_S)
        seen = readiness.text in result.stdout + result.stderr
        return seen, f"log line {readiness.text!r} not seen yet"

    def _log_tail(self, name: str) -> str:
        result = self.seams.cli([CONTAINER, "logs", name], SHORT_TIMEOUT_S)
        return "\n".join((result.stdout + result.stderr).splitlines()[-20:])

    def _probe(self, spec: ServiceSpec, port: int) -> None:
        """Speak the service's protocol once through the published port."""
        if spec.probe is None:
            return
        resets = 0
        last: BaseException | None = None
        for attempt in range(PROBE_ATTEMPTS):
            try:
                reply = self._probe_once(spec.probe, port)
            except ConnectionResetError as exc:
                resets += 1
                last = exc
            except (OSError, http.client.HTTPException) as exc:
                last = exc
            else:
                if reply:
                    return
                resets += 1
                last = None
            if attempt < PROBE_ATTEMPTS - 1:
                self.seams.sleep(1)
        if resets == PROBE_ATTEMPTS:
            raise LocalNetworkDenied(_message_local_network())
        raise StartFailure(
            f"{spec.key} was ready but its published port did not answer: {last!r}"
        )

    def _probe_once(self, kind: ProbeKind, port: int) -> bytes:
        if kind is ProbeKind.POSTGRES:
            # SSLRequest: any server that speaks the protocol answers N or S.
            reply = self.seams.tcp_reply(port, struct.pack("!II", 8, 80877103), 3)
            return reply if reply[:1] in (b"N", b"S") else b""
        if kind is ProbeKind.SSH:
            reply = self.seams.tcp_reply(port, None, 3)
            return reply if reply.startswith(b"SSH-") else b""
        status = self.seams.http_status(f"http://{PUBLISH_HOST}:{port}/", 3)
        return str(status).encode()

    # --- addresses --------------------------------------------------------

    def container_address(self, name: str) -> str:
        result = self.seams.cli([CONTAINER, "inspect", name], SHORT_TIMEOUT_S)
        if result.returncode != 0:
            raise StartFailure(f"cannot inspect {name}: {result.stderr.strip()}")
        networks = json.loads(result.stdout)[0]["status"]["networks"]
        return networks[0]["ipv4Address"].split("/")[0]

    def check_container_address(self, address: str, port: int) -> None:
        """One request to the container's own address; Local Network can block it."""
        try:
            self.seams.http_status(f"http://{address}:{port}/", 5)
        except (OSError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, OSError) and reason.errno in (
                errno.EHOSTUNREACH,
                errno.ENETUNREACH,
            ):
                raise LocalNetworkDenied(_message_container_address(address)) from exc
            raise StartFailure(f"{address}:{port} did not answer: {exc!r}") from exc

    # --- removal ----------------------------------------------------------

    def delete(self, name: str) -> None:
        """Remove *name*; a container that is already gone is not a problem."""
        try:
            result = self.seams.cli(
                [CONTAINER, "delete", "--force", name], SHORT_TIMEOUT_S
            )
        except CliTimeout as exc:
            warnings.warn(f"could not remove {name}: {exc}", stacklevel=2)
            return
        if result.returncode != 0 and "not found" not in result.stderr.lower():
            warnings.warn(
                f"could not remove {name}: {result.stderr.strip()}", stacklevel=2
            )

    def _list_all(self) -> list[dict[str, Any]]:
        result = self.seams.cli(
            [CONTAINER, "list", "--all", "--format", "json"], SHORT_TIMEOUT_S
        )
        if result.returncode != 0:
            raise StartFailure(f"container list failed: {result.stderr.strip()}")
        return json.loads(result.stdout)

    def _reap_once(self) -> None:
        """Remove service containers whose owning process is gone. Never raises."""
        if self._reaped:
            return
        self._reaped = True
        try:
            self.seams.lock_dir.mkdir(parents=True, exist_ok=True)
            with open(self.seams.lock_dir / "reaper.lock", "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                for entry in self._list_all():
                    name = entry["configuration"]["id"]
                    owner = entry["configuration"].get("labels", {}).get(OWNER_LABEL)
                    if not name.startswith(NAME_PREFIX) or owner is None:
                        continue
                    try:
                        alive = self.seams.owner_alive(owner)
                    except ValueError:
                        # A label this adapter did not write: not ours to remove.
                        warnings.warn(
                            f"{name} has an unreadable owner label {owner!r}",
                            stacklevel=2,
                        )
                        continue
                    if not alive:
                        self.delete(name)
        except Exception as exc:  # the reaper is best effort by contract
            warnings.warn(f"container reaper skipped: {exc!r}", stacklevel=2)

    def sweep_own(self) -> None:
        """Remove what this process started and `shutdown_containers` missed."""
        if not self._started_any:
            return
        token = self.seams.own_token()
        try:
            for entry in self._list_all():
                labels = entry["configuration"].get("labels", {})
                if labels.get(OWNER_LABEL) == token:
                    self.delete(entry["configuration"]["id"])
        except Exception as exc:  # teardown must not fail a finished run
            warnings.warn(f"container sweep skipped: {exc!r}", stacklevel=2)


def _exit(message: str) -> NoReturn:
    pytest.exit(message, returncode=1)


def install(containers: ModuleType, runtime: AppleRuntime) -> Callable[[], None]:
    """Swap the start functions in *containers* for ones that use *runtime*.

    Returns the function that puts the originals back. Raises `AdapterDriftError`
    before changing anything when a name the adapter relies on is gone.
    """
    missing = [name for name in (*PATCHED, *RELIED_ON) if not hasattr(containers, name)]
    if missing:
        raise AdapterDriftError(
            f"tests/containers.py no longer defines {missing}; update "
            "tests/apple_container.py to match."
        )
    originals = {name: getattr(containers, name) for name in PATCHED}
    specs = runtime.specs

    def guarded(action: Callable[[], Any], resource: str) -> Any:
        try:
            return action()
        except CliTimeout as exc:
            _exit(f"cannot start {resource}: {_message_wedge(exc)}")
        except RuntimeUnavailable as exc:
            _exit(f"cannot start {resource}: {exc}")
        except LocalNetworkDenied as exc:
            _exit(f"cannot start {resource}: {exc}")

    def docker_available() -> bool:
        return runtime.available()

    def require_docker(resource: str) -> None:
        guarded(runtime.check_available, resource)

    def postgres_url(spec: ServiceSpec, resource: str) -> str:
        service = guarded(lambda: runtime.start(spec), resource)
        return (
            f"postgresql://{containers.POSTGRES_USER}:{containers.POSTGRES_PASSWORD}"
            f"@{PUBLISH_HOST}:{service.port}/{containers.POSTGRES_DB}"
        )

    def start_postgres() -> str:
        return postgres_url(specs["postgres"], containers.POSTGRES_RESOURCE)

    def pgvector_url() -> str:
        return containers._resolve(
            "pgvector",
            PGVECTOR_RESOURCE,
            lambda: postgres_url(specs["pgvector"], PGVECTOR_RESOURCE),
        )

    def start_seaweedfs() -> str:
        service = guarded(
            lambda: runtime.start(specs["seaweedfs"]), containers.S3_RESOURCE
        )
        guarded(
            lambda: runtime.check_container_address(
                service.address, containers.SEAWEEDFS_S3_PORT
            ),
            containers.S3_RESOURCE,
        )
        # GC tests need the container's own address: a loopback backup is correctly
        # considered shared with a local Vault.
        containers._resolved["s3_private"] = (
            f"http://{service.address}:{containers.SEAWEEDFS_S3_PORT}"
        )
        return f"http://{PUBLISH_HOST}:{service.port}"

    def start_nextcloud() -> NoReturn:
        # Reached only by a test `NEXTCLOUD_FILES` and `NEXTCLOUD_IDS` did not name.
        pytest.fail(NEXTCLOUD_UNSUPPORTED, pytrace=False)

    def start_openssh() -> tuple[str, int, str]:
        service = guarded(
            lambda: runtime.start(specs["openssh"]), containers.OPENSSH_RESOURCE
        )
        last = "no response"
        for attempt in range(KEYSCAN_ATTEMPTS):
            result = runtime.seams.cli(
                ["ssh-keyscan", "-p", str(service.port), PUBLISH_HOST], 5
            )
            if result.returncode == 0 and result.stdout.strip():
                return PUBLISH_HOST, service.port, result.stdout
            last = f"exit={result.returncode}, {result.stderr.strip() or 'empty host-key response'}"
            if attempt < KEYSCAN_ATTEMPTS - 1:
                runtime.seams.sleep(0.25)
        raise RuntimeError(
            "ssh-keyscan is required for pinned SFTP contracts; "
            f"host-key readiness failed after {KEYSCAN_ATTEMPTS} attempts ({last})"
        )

    replacements: dict[str, Callable[..., Any]] = {
        "docker_available": docker_available,
        "require_docker": require_docker,
        "_start_postgres": start_postgres,
        "_start_seaweedfs": start_seaweedfs,
        "_start_nextcloud": start_nextcloud,
        "_start_openssh": start_openssh,
        "pgvector_url": pgvector_url,
    }
    for name, replacement in replacements.items():
        setattr(containers, name, replacement)

    def undo() -> None:
        for name, original in originals.items():
            setattr(containers, name, original)

    return undo


def _names_nextcloud(item: pytest.Item) -> bool:
    """Whether *item*'s parameter ids or function name name Nextcloud.

    Parameter ids join with '-', and none of `NEXTCLOUD_IDS` contains one.
    """
    callspec = getattr(item, "callspec", None)
    ids = callspec.id.split("-") if callspec is not None else []
    name = getattr(item, "originalname", None) or item.name
    return any(part in NEXTCLOUD_IDS for part in ids) or "nextcloud" in name.split("_")


class AppleContainerPlugin:
    """The collection and teardown hooks that go with the runtime."""

    def __init__(
        self,
        config: pytest.Config,
        runtime: AppleRuntime,
        tests_dir: Path,
        docker_only_files: Sequence[str] = DOCKER_ONLY_FILES,
        nextcloud_files: Sequence[str] = NEXTCLOUD_FILES,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.tests_dir = tests_dir
        self.docker_only_files = tuple(docker_only_files)
        self.nextcloud_files = tuple(nextcloud_files)
        self.deselected: set[str] = set()
        self.opt_in = bool(config.getoption(DESELECT_OPTION))
        missing = [
            name
            for name in (*self.docker_only_files, *self.nextcloud_files)
            if not (tests_dir / name).is_file()
        ]
        if missing:
            raise pytest.UsageError(
                f"tests/apple_container.py lists files that are gone: {missing}. "
                "Update DOCKER_ONLY_FILES or NEXTCLOUD_FILES."
            )

    def _reason(self, item: pytest.Item) -> str | None:
        for marker in DOCKER_ONLY_MARKERS:
            if item.get_closest_marker(marker) is not None:
                return f"marked {marker}"
        path = Path(str(item.path))
        for name in self.docker_only_files:
            if path == self.tests_dir / name:
                return f"in {name}"
        for name in self.nextcloud_files:
            if path == self.tests_dir / name and _names_nextcloud(item):
                return "needs Nextcloud"
        return None

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        if not self.opt_in:
            return
        kept: list[pytest.Item] = []
        removed: list[pytest.Item] = []
        for item in items:
            (removed if self._reason(item) else kept).append(item)
        if not removed:
            return
        config.hook.pytest_deselected(items=removed)
        items[:] = kept
        self.deselected.update(item.nodeid for item in removed)
        output = getattr(config, "workeroutput", None)
        if output is not None:
            output["apple_container_deselected"] = sorted(self.deselected)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        # After the suite's own resource check, so a stopped apiserver is reported
        # before the list of tests that would not have run on it anyway.
        if (
            self.opt_in
            or session.config.option.collectonly
            or session.testsfailed
            or session.shouldstop
        ):
            return
        found = sorted(
            f"{item.nodeid} ({reason})"
            for item in session.items
            if (reason := self._reason(item)) is not None
        )
        if not found:
            return
        message = (
            "these tests need a Docker Engine API and cannot run on Apple container:\n  "
            + "\n  ".join(found)
            + f"\nRe-run with {DESELECT_OPTION} to leave them out and see them "
            "named in the summary, or run them on a machine with Docker."
        )
        session.shouldstop = message
        _exit(message)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: object) -> None:
        output = getattr(node, "workeroutput", None) or {}
        self.deselected.update(output.get("apple_container_deselected", ()))

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if not self.deselected:
            return
        terminalreporter.section("Apple container: Docker-only tests left out")
        for nodeid in sorted(self.deselected):
            terminalreporter.line(nodeid)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: object) -> None:
        self.runtime.sweep_own()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        DESELECT_OPTION,
        action="store_true",
        default=False,
        help=(
            "Apple container: leave out the tests that need a Docker Engine API "
            "(bgcode, the nginx end-to-end files, anything that needs Nextcloud) "
            "instead of stopping the run."
        ),
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    from tests import containers
    from tests.paths import TESTS_DIR

    activate(config, containers, sys.platform, tests_dir=TESTS_DIR)


def activate(
    config: pytest.Config,
    containers: ModuleType,
    platform_name: str,
    *,
    runtime: AppleRuntime | None = None,
    tests_dir: Path,
) -> AppleContainerPlugin | None:
    """Install the adapter on darwin; do nothing anywhere else."""
    if platform_name != "darwin":
        return None
    runtime = runtime or AppleRuntime(containers)
    undo = install(containers, runtime)
    plugin = AppleContainerPlugin(config, runtime, tests_dir)
    config.pluginmanager.register(plugin, "apple-container-hooks")

    def cleanup() -> None:
        config.pluginmanager.unregister(plugin)
        undo()

    config.add_cleanup(cleanup)
    return plugin
