#!/usr/bin/env python
"""Run backend tests in a Linux arm64 container on Apple container (macOS host).

Mesh workers cap their address space with `RLIMIT_AS`, which macOS rejects, and local
storage is classified from `/proc/self/mountinfo`, which macOS lacks. This runs the
tests that depend on either in a Linux container, with the services (PostgreSQL,
SeaweedFS, OpenSSH) started on the host by `tests/apple_container.py` and reached by
container address. See `deploy/apple-container/README.md`.

    cd backend
    .venv/bin/python scripts/linux_lane.py --services postgres,s3,sftp -- \\
        -m "(postgres or s3 or remote_storage) and not coverage_gate and not scale \\
and not bgcode" tests/e2e/test_split_topology.py -q

Everything after `--` goes to pytest. Only what pytest selects runs: this is not the
whole suite, `bgcode` needs the converter binary the image lacks, and Playwright is
not covered.

Containers may have no route to PyPI (a VPN blocks it here), so the locked
dependencies are installed on the host as Linux wheels into
`~/Library/Caches/printstash-linux-lane/deps-<uv.lock hash>` and mounted read-only.
The source is copied into the container because tests write beside it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType
from typing import Final

BACKEND: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from tests import apple_container as ac  # noqa: E402
from tests import containers  # noqa: E402

IMAGE: Final = "python:3.14.8-slim-trixie"
CACHE: Final = Path.home() / "Library" / "Caches" / "printstash-linux-lane"
SERVICES: Final = ("postgres", "pgvector", "s3", "sftp")
DEFAULT_SERVICES: Final = "postgres,s3,sftp"
PLUGIN: Final = "tests.linux_lane_services"
PLUGIN_ENV: Final = "PRINTSTASH_TEST_SERVICES_FILE"
LANE_PREFIX: Final = "printstash-linux-lane-"
WHEEL_PLATFORM: Final = "aarch64-manylinux_2_31"
PYTHON_VERSION: Final = "3.14"
SSH_PORT: Final = 2222
DELETE_TIMEOUT_S: Final = 30
# Written to the container's site-packages: tests start subprocesses with a scrubbed
# environment, so PYTHONPATH would not reach them.
PTH_LINES: Final = ("/deps", "/work", "/work/packages/printstash-core/src")
SOURCE_EXCLUDES: Final = (
    "./.venv",
    "./.git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "./tests/_data",
)
# One shell line, so the whole run is a single `container run`.
IN_CONTAINER: Final = (
    "mkdir -p /work "
    "&& printf '%s\\n' " + " ".join(f"'{line}'" for line in PTH_LINES) + " "
    "> \"$(python -c 'import site; print(site.getsitepackages()[0])')/lane.pth\" "
    "&& tar -C /src "
    + " ".join(f"--exclude={p}" for p in SOURCE_EXCLUDES)
    + " -cf - . "
    "| tar -C /work -xf - "
    f'&& exec python -m pytest -p no:randomly -p no:cacheprovider -p {PLUGIN} "$@"'
)


def parse_services(value: str) -> tuple[str, ...]:
    """The services to start, from a comma-separated list; unknown names are refused."""
    wanted = tuple(dict.fromkeys(part for part in value.split(",") if part))
    unknown = [name for name in wanted if name not in SERVICES]
    if unknown or not wanted:
        raise argparse.ArgumentTypeError(
            f"--services takes some of {', '.join(SERVICES)}; got {value!r}"
        )
    return wanted


def deps_directory(lock: bytes, cache: Path = CACHE) -> Path:
    """Where the Linux wheels for this `uv.lock` live."""
    return cache / f"deps-{hashlib.sha256(lock).hexdigest()[:12]}"


def install_deps(backend: Path, target: Path, run=subprocess.run) -> None:
    """Resolve the locked dependencies as Linux wheels on the host."""
    target.parent.mkdir(parents=True, exist_ok=True)
    requirements = target.parent / f"{target.name}.txt"
    run(
        [
            "uv",
            "export",
            "--frozen",
            "--extra",
            "dev",
            "--extra",
            "full",
            "--no-emit-project",
            "--no-emit-workspace",
            "--no-hashes",
            "-o",
            str(requirements),
        ],  # fmt: skip
        cwd=backend,
        check=True,
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--python-platform",
            WHEEL_PLATFORM,
            "--python-version",
            PYTHON_VERSION,
            "--only-binary",
            ":all:",
            "--target",
            str(target),
            "-r",
            str(requirements),
        ],  # fmt: skip
        cwd=backend,
        check=True,
    )


def lane_command(
    *,
    name: str,
    backend: Path,
    deps: Path,
    lane: Path,
    cpus: str,
    memory: str,
    pytest_args: Sequence[str],
) -> list[str]:
    return [
        "container", "run", "--rm", "--name", name, "-c", cpus, "-m", memory,
        "--mount", f"type=bind,source={backend},target=/src,readonly",
        "--mount", f"type=bind,source={deps},target=/deps,readonly",
        "--mount", f"type=bind,source={lane},target=/lane,readonly",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", f"{PLUGIN_ENV}=/lane/services.json",
        "-w", "/work", IMAGE, "sh", "-c", IN_CONTAINER, "pytest", *pytest_args,
    ]  # fmt: skip


def postgres_url(address: str) -> str:
    return (
        f"postgresql://{containers.POSTGRES_USER}:{containers.POSTGRES_PASSWORD}"
        f"@{address}:5432/{containers.POSTGRES_DB}"
    )


def services_document(
    runtime: ac.AppleRuntime,
    wanted: Sequence[str],
    keyscan: Callable[[str], str],
) -> dict[str, object]:
    """Start *wanted* on the host and describe them by container address."""
    document: dict[str, object] = {}
    if "postgres" in wanted:
        started = runtime.start(runtime.specs["postgres"])
        document["postgres"] = postgres_url(runtime.container_address(started.name))
    if "pgvector" in wanted:
        started = runtime.start(runtime.specs["pgvector"])
        document["pgvector"] = postgres_url(runtime.container_address(started.name))
    if "s3" in wanted:
        started = runtime.start(runtime.specs["seaweedfs"])
        address = runtime.container_address(started.name)
        document["s3"] = document["s3_private"] = (
            f"http://{address}:{containers.SEAWEEDFS_S3_PORT}"
        )
    if "sftp" in wanted:
        started = runtime.start(runtime.specs["openssh"])
        address = runtime.container_address(started.name)
        document["openssh"] = {
            "host": address,
            "port": SSH_PORT,
            "known_hosts": keyscan(address),
        }
    return document


def host_keyscan(address: str) -> str:
    scan = subprocess.run(
        ["ssh-keyscan", "-T", "5", "-p", str(SSH_PORT), address],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if not scan.stdout.strip():
        raise RuntimeError(f"ssh-keyscan of {address}:{SSH_PORT} returned no keys")
    return scan.stdout


def remove_container(name: str, run=subprocess.run) -> None:
    """Delete the test container even if the launcher was interrupted."""
    run(
        ["container", "delete", "--force", name],
        capture_output=True,
        timeout=DELETE_TIMEOUT_S,
        check=False,
    )


def _terminate(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run backend tests in a Linux container."
    )
    parser.add_argument("--services", type=parse_services, default=DEFAULT_SERVICES)
    parser.add_argument("--rebuild-deps", action="store_true")
    parser.add_argument("--cpus", default="8")
    parser.add_argument("--memory", default="8G")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    pytest_args = [a for a in options.pytest_args if a != "--"]
    if not pytest_args:
        parser.error("give pytest arguments after --, e.g. -- tests/e2e/test_x.py -q")

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    runtime = ac.AppleRuntime(containers)
    name = f"{LANE_PREFIX}{os.getpid()}"
    try:
        runtime.check_available()
        document = services_document(runtime, options.services, host_keyscan)
        deps = deps_directory((BACKEND / "uv.lock").read_bytes())
        if options.rebuild_deps or not deps.is_dir():
            install_deps(BACKEND, deps)
        with tempfile.TemporaryDirectory(prefix=LANE_PREFIX) as lane:
            Path(lane, "services.json").write_text(json.dumps(document))
            command = lane_command(
                name=name,
                backend=BACKEND,
                deps=deps,
                lane=Path(lane),
                cpus=options.cpus,
                memory=options.memory,
                pytest_args=pytest_args,
            )
            return subprocess.run(command, check=False).returncode
    finally:
        remove_container(name)
        containers.shutdown_containers()
        runtime.sweep_own()


if __name__ == "__main__":
    raise SystemExit(main())
