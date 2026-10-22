"""Use services a macOS host already started, from a test process in a Linux container.

Mesh workers cap their address space with `RLIMIT_AS`, which macOS rejects, and local
storage is classified from `/proc/self/mountinfo`, which macOS lacks. The tests that
depend on either run in a Linux container instead (`scripts/linux_lane.py`). A
container cannot start sibling containers, so the macOS host starts the services with
`tests/apple_container.py` and hands their container addresses over in a JSON file.

Load with `-p tests.linux_lane_services`. When `PRINTSTASH_TEST_SERVICES_FILE` names
that file, the start functions in `tests.containers` return its endpoints instead of
asking for a Docker daemon, and the tests that need Nextcloud are deselected the way
the Apple container adapter leaves them out. Without the variable this plugin does
nothing, so it is safe to leave loaded.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Final, NoReturn
from urllib.parse import urlsplit

import pytest

from tests import apple_container

SERVICES_FILE_ENV: Final = "PRINTSTASH_TEST_SERVICES_FILE"
CONNECT_TIMEOUT_S: Final = 5.0
SERVICE_KEYS: Final = frozenset({"postgres", "pgvector", "s3", "s3_private", "openssh"})
OPENSSH_KEYS: Final = frozenset({"host", "port", "known_hosts"})
TESTS_DIR: Final = Path(__file__).resolve().parent


def load_services(path: Path) -> dict[str, object]:
    """The services file, refusing keys this plugin does not know."""
    services = json.loads(path.read_text())
    if not isinstance(services, dict):
        raise ValueError(f"{path} must hold a JSON object")
    unknown = sorted(set(services) - SERVICE_KEYS)
    if unknown:
        raise ValueError(f"{path} names unknown services: {unknown}")
    ssh = services.get("openssh")
    if ssh is not None and (not isinstance(ssh, dict) or set(ssh) != OPENSSH_KEYS):
        raise ValueError(f"{path}: 'openssh' must hold exactly {sorted(OPENSSH_KEYS)}")
    return services


def endpoints(services: dict[str, object]) -> Iterator[tuple[str, int]]:
    """Every host and port the file points at, for the reachability check."""
    for key in ("postgres", "pgvector", "s3"):
        url = services.get(key)
        if url is not None:
            assert isinstance(url, str)
            parts = urlsplit(url)
            assert parts.hostname is not None and parts.port is not None
            yield parts.hostname, parts.port
    ssh = services.get("openssh")
    if ssh is not None:
        assert isinstance(ssh, dict)
        yield str(ssh["host"]), int(ssh["port"])


def unreachable(
    services: dict[str, object], connect=socket.create_connection
) -> list[str]:
    """Endpoints that refuse a connection from this process."""
    failed = []
    for host, port in endpoints(services):
        try:
            connect((host, port), CONNECT_TIMEOUT_S).close()
        except OSError as exc:
            failed.append(f"{host}:{port} ({exc})")
    return failed


def install(containers: ModuleType, services: dict[str, object]) -> None:
    """Point the start functions in *containers* at *services*."""

    def need(key: str) -> object:
        if key not in services:
            raise LookupError(
                f"the Linux lane was not given a {key!r} service "
                f"(have: {sorted(services)}); pass it to --services"
            )
        return services[key]

    def start_s3() -> str:
        containers._resolved["s3_private"] = need("s3_private")
        return str(need("s3"))

    def start_openssh() -> tuple[str, int, str]:
        ssh = need("openssh")
        assert isinstance(ssh, dict)
        return str(ssh["host"]), int(ssh["port"]), str(ssh["known_hosts"])

    def start_nextcloud() -> NoReturn:
        # Reached only by a test `NEXTCLOUD_FILES` and `NEXTCLOUD_IDS` did not name.
        pytest.fail(apple_container.NEXTCLOUD_UNSUPPORTED, pytrace=False)

    replacements = {
        "docker_available": lambda: True,
        "require_docker": lambda resource: None,
        "_start_postgres": lambda: str(need("postgres")),
        "_start_seaweedfs": start_s3,
        "_start_openssh": start_openssh,
        "_start_nextcloud": start_nextcloud,
        "pgvector_url": lambda: containers._resolve(
            "pgvector", "PostgreSQL with pgvector", lambda: str(need("pgvector"))
        ),
    }
    assert set(replacements) == set(apple_container.PATCHED)
    for name, replacement in replacements.items():
        setattr(containers, name, replacement)


class LinuxLanePlugin:
    """Leaves out what needs Nextcloud, which the lane never starts."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        paths = {TESTS_DIR / name for name in apple_container.NEXTCLOUD_FILES}
        kept, left_out = [], []
        for item in items:
            if Path(
                str(item.path)
            ).resolve() in paths and apple_container._names_nextcloud(item):
                left_out.append(item)
            else:
                kept.append(item)
        if left_out:
            config.hook.pytest_deselected(items=left_out)
            items[:] = kept


def pytest_configure(config: pytest.Config) -> None:
    path = os.environ.get(SERVICES_FILE_ENV)
    if not path:
        return
    from tests import containers

    missing = [
        name
        for name in (*apple_container.PATCHED, "_resolved")
        if not hasattr(containers, name)
    ]
    if missing:
        pytest.exit(
            f"tests/containers.py no longer defines {missing}; update "
            "tests/linux_lane_services.py to match.",
            returncode=1,
        )
    services = load_services(Path(path))
    failed = unreachable(services)
    if failed:
        pytest.exit(
            "the Linux lane cannot reach services the host started: "
            f"{', '.join(failed)}. Check that they are still running "
            "(`container list`) and that this Mac's firewall or VPN is not "
            "blocking the container network.",
            returncode=1,
        )
    originals = {name: getattr(containers, name) for name in apple_container.PATCHED}
    install(containers, services)
    plugin = LinuxLanePlugin(config)
    config.pluginmanager.register(plugin, "linux-lane-hooks")

    def restore() -> None:
        config.pluginmanager.unregister(plugin)
        for name, value in originals.items():
            setattr(containers, name, value)

    config.add_cleanup(restore)
