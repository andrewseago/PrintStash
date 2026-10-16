"""The Apple `container` adapter starts the suite's services and fails with the fix.

`tests/apple_container.py` replaces the functions in `tests/containers.py` that
start PostgreSQL, SeaweedFS, Nextcloud and OpenSSH, so the resource-marked tests run
on a Mac that has Apple's `container` runtime and no Docker daemon. These tests hold
the adapter to what it promises without needing that runtime: a fake `container` CLI
replays output recorded from a real 1.5.0 install (`tests/fixtures/apple_container/`,
fake paths, addresses and MACs), the clock and the network are injected, and the
collection hooks run in a throwaway project. A pristine copy of `tests/containers.py`
is loaded for each test, so nothing here touches the module the rest of the suite
uses.

Two things are not covered here and are checked on a Mac with the runtime running:
the real 1.5.0 CLI end to end, and a test suite run through the adapter.
"""

from __future__ import annotations

import ast
import copy
import errno
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.modules.storage.storage_identity import _is_loopback
from tests import apple_container as ac
from tests.paths import FIXTURES_DIR, REPO_ROOT, TESTS_DIR

CAPTURES = FIXTURES_DIR / "apple_container"
OWNER = "4242-1700000000.25"
BODY = {"container": "ps", "apiserver": "ps"}


def capture(name: str) -> subprocess.CompletedProcess[str]:
    """A recorded CLI reply, as the runner would return it."""
    recorded = json.loads((CAPTURES / f"{name}.json").read_text())
    return subprocess.CompletedProcess(
        recorded["argv"], recorded["returncode"], recorded["stdout"], recorded["stderr"]
    )


def reply(
    code: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def with_version(text: str) -> subprocess.CompletedProcess[str]:
    recorded = capture("system_version_running")
    return reply(0, recorded.stdout.replace("1.5.0", text))


def entry(name: str, owner: str | None) -> dict[str, Any]:
    """A listing entry shaped like a recorded one, with this id and owner label."""
    shape = json.loads(capture("list_all").stdout)[1]
    shape = copy.deepcopy(shape)
    shape["configuration"]["id"] = name
    shape["configuration"]["labels"] = {} if owner is None else {ac.OWNER_LABEL: owner}
    return shape


class FakeDaemon:
    """Answers the `container` CLI from recorded replies and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.present: set[str] = set()
        self.run_results: list[subprocess.CompletedProcess[str]] = []
        self.exec_results: list[subprocess.CompletedProcess[str]] = []
        self.logs_results: list[subprocess.CompletedProcess[str]] = []
        self.pull_result = capture("image_pull_ok")
        self.status = capture("system_status_running")
        self.version = capture("system_version_running")
        self.listing: list[dict[str, Any]] = []
        self.keyscan = capture("keyscan")
        self.timeouts: set[str] = set()
        self.deleted: list[str] = []
        self.keyscans: list[subprocess.CompletedProcess[str]] = []
        self.pull_hangs = False

    def __call__(self, argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
        argv = tuple(argv)
        self.calls.append((argv, timeout))
        if argv[0] == "ssh-keyscan":
            return self.keyscans.pop(0) if self.keyscans else self.keyscan
        verb = argv[1]
        if verb in self.timeouts:
            raise ac.CliTimeout(argv, timeout)
        if argv[1:3] == ("system", "status"):
            return self.status
        if argv[1:3] == ("system", "version"):
            return self.version
        if argv[1:3] == ("image", "inspect"):
            present = argv[3] in self.present
            return capture(
                "image_inspect_present" if present else "image_inspect_missing"
            )
        if argv[1:3] == ("image", "pull"):
            if self.pull_hangs:
                raise ac.CliTimeout(argv, timeout)
            self.present.add(argv[-1])
            return self.pull_result
        if verb == "run":
            return self.run_results.pop(0) if self.run_results else capture("run_ok")
        if verb == "exec":
            return self.exec_results.pop(0) if self.exec_results else reply()
        if verb == "logs":
            if self.logs_results:
                return self.logs_results.pop(0)
            return capture("logs_seaweedfs_ready")
        if verb == "inspect":
            return capture("inspect_seaweedfs")
        if verb == "delete":
            self.deleted.append(argv[-1])
            return reply()
        if verb == "list":
            return reply(0, json.dumps(self.listing))
        raise AssertionError(f"unscripted command: {argv}")

    def verbs(self) -> list[str]:
        return [
            " ".join(argv[1:3] if argv[1] in ("system", "image") else argv[1:2])
            for argv, _ in self.calls
        ]

    def runs(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if argv[1] == "run"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Network:
    """Scripted host-side answers, one per call, with a default for the rest."""

    def __init__(self) -> None:
        self.http: list[Any] = []
        self.tcp: list[Any] = []
        self.urls: list[str] = []
        self.requests: list[tuple[str, float, dict[str, Any]]] = []
        self.http_default: Any = 200
        self.tcp_default: Any = b"N"

    def http_status(self, url: str, timeout: float, **request: Any) -> int:
        self.urls.append(url)
        self.requests.append((url, timeout, request))
        result = self.http.pop(0) if self.http else self.http_default
        if isinstance(result, BaseException):
            raise result
        return result

    def tcp_reply(self, port: int, payload: bytes | None, timeout: float) -> bytes:
        result = self.tcp.pop(0) if self.tcp else self.tcp_default
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def pristine() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pristine_containers", TESTS_DIR / "containers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def daemon() -> FakeDaemon:
    return FakeDaemon()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def network() -> Network:
    return Network()


@pytest.fixture
def runtime(
    pristine: ModuleType,
    daemon: FakeDaemon,
    clock: Clock,
    network: Network,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ac.AppleRuntime:
    monkeypatch.setattr(ac.secrets, "token_hex", lambda size: "abcd1234")
    ports = iter(range(20001, 20100))
    return ac.AppleRuntime(
        pristine,
        ac.Seams(
            cli=daemon,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            free_port=lambda: next(ports),
            http_status=network.http_status,
            tcp_reply=network.tcp_reply,
            own_token=lambda: OWNER,
            owner_alive=lambda token: token == OWNER,
            lock_dir=tmp_path / "locks",
            arch="arm64",
        ),
    )


def config(*, deselect: bool = False) -> SimpleNamespace:
    registered: list[Any] = []
    cleanups: list[Callable[[], None]] = []
    return SimpleNamespace(
        getoption=lambda name: deselect,
        pluginmanager=SimpleNamespace(
            register=lambda plugin, name: registered.append(plugin),
            unregister=lambda plugin: registered.remove(plugin),
            registered=registered,
        ),
        add_cleanup=cleanups.append,
        cleanups=cleanups,
    )


class TestActivate:
    def test_changes_nothing_off_darwin(
        self, pristine: ModuleType, daemon: FakeDaemon
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}

        plugin = ac.activate(
            config(),
            pristine,
            "linux",
            runtime=ac.AppleRuntime(pristine, ac.Seams(cli=daemon)),
            tests_dir=TESTS_DIR,
        )

        assert plugin is None
        assert {name: getattr(pristine, name) for name in ac.PATCHED} == before
        assert daemon.calls == []

    def test_replaces_every_start_function_on_darwin(
        self, pristine: ModuleType, runtime: ac.AppleRuntime
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}

        ac.activate(config(), pristine, "darwin", runtime=runtime, tests_dir=TESTS_DIR)

        assert [n for n in ac.PATCHED if getattr(pristine, n) is before[n]] == []

    def test_runs_no_command_while_activating(
        self, pristine: ModuleType, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        ac.activate(config(), pristine, "darwin", runtime=runtime, tests_dir=TESTS_DIR)

        assert daemon.calls == []

    def test_restores_the_originals_when_pytest_cleans_up(
        self, pristine: ModuleType, runtime: ac.AppleRuntime
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}
        pytest_config = config()
        ac.activate(
            pytest_config, pristine, "darwin", runtime=runtime, tests_dir=TESTS_DIR
        )

        for cleanup in pytest_config.cleanups:
            cleanup()

        assert {name: getattr(pristine, name) for name in ac.PATCHED} == before
        assert pytest_config.pluginmanager.registered == []

    @pytest.mark.parametrize("name", [*ac.PATCHED, *ac.RELIED_ON], ids=str)
    def test_names_what_is_missing_when_containers_drifts(
        self, pristine: ModuleType, runtime: ac.AppleRuntime, name: str
    ) -> None:
        delattr(pristine, name)

        with pytest.raises(ac.AdapterDriftError, match=name):
            ac.install(pristine, runtime)

    def test_patches_nothing_when_a_name_is_missing(
        self, pristine: ModuleType, runtime: ac.AppleRuntime
    ) -> None:
        before = pristine.require_docker
        del pristine._resolve

        with pytest.raises(ac.AdapterDriftError):
            ac.install(pristine, runtime)

        assert pristine.require_docker is before


class TestCheckAvailable:
    def test_accepts_a_running_supported_runtime(
        self, runtime: ac.AppleRuntime
    ) -> None:
        runtime.check_available()

    def test_checks_once_per_process(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.check_available()
        runtime.check_available()

        assert daemon.verbs() == ["system status", "system version"]

    def test_names_the_start_command_when_the_apiserver_is_stopped(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.status = capture("system_status_stopped")

        with pytest.raises(ac.RuntimeUnavailable, match="container system start"):
            runtime.check_available()

    @pytest.mark.parametrize("version", ["1.4.1", "1.6.0", "2.0.0"], ids=str)
    def test_rejects_a_version_outside_the_supported_range(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, version: str
    ) -> None:
        daemon.version = with_version(version)

        with pytest.raises(ac.RuntimeUnavailable, match=version):
            runtime.check_available()

    @pytest.mark.parametrize("version", ["1.5.0", "1.5.9"], ids=str)
    def test_accepts_every_patch_of_the_supported_minor(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, version: str
    ) -> None:
        daemon.version = with_version(version)

        runtime.check_available()

    def test_rejects_a_reply_without_the_apiserver(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.version = capture("system_version_stopped")

        with pytest.raises(ac.RuntimeUnavailable, match="apiserver"):
            runtime.check_available()

    def test_reports_a_hung_command_as_a_timeout(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"system"}

        with pytest.raises(ac.CliTimeout):
            runtime.check_available()

    def test_reports_unavailable_without_raising_for_the_boolean_form(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.status = capture("system_status_stopped")

        assert runtime.available() is False

    def test_bounds_every_command_by_a_timeout(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.check_available()

        assert [timeout for _, timeout in daemon.calls] == [ac.SHORT_TIMEOUT_S] * 2


class TestRegistryReference:
    def test_pulls_a_digest_pinned_lscr_image_from_ghcr(self) -> None:
        reference = "lscr.io/linuxserver/openssh-server@sha256:" + "a" * 64

        assert ac.registry_reference(reference) == (
            "ghcr.io/linuxserver/openssh-server@sha256:" + "a" * 64
        )

    def test_leaves_other_registries_alone(self) -> None:
        assert ac.registry_reference("postgres:16-alpine") == "postgres:16-alpine"

    def test_refuses_an_lscr_image_that_is_not_pinned(self) -> None:
        with pytest.raises(ValueError, match="not pinned by digest"):
            ac.registry_reference("lscr.io/linuxserver/openssh-server:latest")


class TestEnsureImage:
    def test_pulls_nothing_when_the_image_is_present(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.present.add("postgres:16-alpine")

        runtime.ensure_image("postgres:16-alpine", "arm64")

        assert daemon.verbs() == ["image inspect"]

    def test_pulls_the_requested_platform_without_progress(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.ensure_image("postgres:16-alpine", "amd64")

        pull = next(call for call in daemon.calls if call[0][1:3] == ("image", "pull"))
        assert pull == (
            (
                "container",
                "image",
                "pull",
                "--platform",
                "linux/amd64",
                "--progress",
                "none",
                "postgres:16-alpine",
            ),
            ac.PULL_TIMEOUT_S,
        )

    def test_returns_the_reference_it_pulled(self, runtime: ac.AppleRuntime) -> None:
        reference = "lscr.io/linuxserver/openssh-server@sha256:" + "b" * 64

        assert runtime.ensure_image(reference, "arm64") == (
            "ghcr.io/linuxserver/openssh-server@sha256:" + "b" * 64
        )

    def test_names_the_manual_pull_when_the_pull_times_out(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.pull_hangs = True

        with pytest.raises(ac.StartFailure, match="Pull it by hand"):
            runtime.ensure_image("postgres:16-alpine", "arm64")

    def test_does_not_retry_a_pull_that_timed_out(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.pull_hangs = True

        with pytest.raises(ac.StartFailure):
            runtime.ensure_image("postgres:16-alpine", "arm64")

        assert daemon.verbs().count("image pull") == 1

    def test_reports_a_failed_pull_with_the_cli_message(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.pull_result = capture("image_pull_lscr_refused")

        with pytest.raises(ac.StartFailure, match="registrable domain"):
            runtime.ensure_image("postgres:16-alpine", "arm64")


def run_argv(
    name: str,
    port: int,
    *,
    platform: str,
    image: str,
    tail: list[str],
    memory: str | None = None,
) -> list[str]:
    return [
        "container",
        "run",
        "--detach",
        "--progress",
        "none",
        "--platform",
        platform,
        "--name",
        name,
        "--label",
        f"printstash.test.owner={OWNER}",
        "-p",
        f"127.0.0.1:{port}:{tail[0]}",
        *(["--memory", memory] if memory else []),
        *tail[1:],
        image,
    ]


class TestStart:
    def test_runs_postgres_on_an_explicit_loopback_port(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert list(daemon.runs()[0]) == run_argv(
            f"printstash-test-postgres-{os.getpid()}-abcd1234",
            20001,
            platform="linux/arm64",
            image="postgres:16-alpine",
            tail=[
                "5432",
                "-e",
                "POSTGRES_USER=printstash",
                "-e",
                "POSTGRES_PASSWORD=printstash",
                "-e",
                "POSTGRES_DB=printstash",
            ],
        )

    def test_runs_seaweedfs_with_its_mini_command(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        runtime.start(runtime.specs["seaweedfs"])

        assert daemon.runs()[0][-5:] == (
            pristine.SEAWEEDFS_IMAGE,
            "mini",
            "-dir=/data",
            "-master.volumeSizeLimitMB=8",
            "-master.telemetry=false",
        )

    def test_runs_nextcloud_on_amd64_with_two_gigabytes(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        network.http_default = 207

        runtime.start(runtime.specs["nextcloud"])

        argv = daemon.runs()[0]
        assert argv[argv.index("--platform") + 1] == "linux/amd64"
        assert argv[argv.index("--memory") + 1] == "2g"

    def test_runs_openssh_from_ghcr(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        network.tcp_default = b"SSH-2.0-OpenSSH_10.3\r\n"

        runtime.start(runtime.specs["openssh"])

        assert daemon.runs()[0][-1].startswith(
            "ghcr.io/linuxserver/openssh-server@sha256:"
        )

    def test_labels_the_container_with_its_owner(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        argv = daemon.runs()[0]
        assert argv[argv.index("--label") + 1] == f"printstash.test.owner={OWNER}"

    def test_registers_the_handle_so_shutdown_removes_it(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        pristine.shutdown_containers()

        assert daemon.deleted == [f"printstash-test-postgres-{os.getpid()}-abcd1234"]

    def test_checks_availability_before_any_other_command(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert daemon.verbs()[:2] == ["system status", "system version"]

    def test_removes_the_container_after_a_failed_run(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.run_results = [capture("run_name_exists")]

        with pytest.raises(ac.StartFailure, match="already exists"):
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == [f"printstash-test-postgres-{os.getpid()}-abcd1234"]

    def test_retries_with_a_new_port_when_the_port_is_taken(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.run_results = [capture("run_port_busy")]

        runtime.start(runtime.specs["postgres"])

        assert [argv[argv.index("-p") + 1] for argv in daemon.runs()] == [
            "127.0.0.1:20001:5432",
            "127.0.0.1:20002:5432",
        ]

    def test_retries_a_transient_registry_failure(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, clock: Clock
    ) -> None:
        daemon.run_results = [reply(1, "", "toomanyrequests: slow down")]

        runtime.start(runtime.specs["postgres"])

        assert len(daemon.runs()) == 2
        assert clock.slept[:1] == [1]

    def test_does_not_retry_a_permanent_failure(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.run_results = [capture("run_platform_mismatch")]

        with pytest.raises(
            ac.StartFailure, match="does not support required platforms"
        ):
            runtime.start(runtime.specs["nextcloud"])

        assert len(daemon.runs()) == 1

    def test_gives_up_after_the_configured_attempts(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        daemon.run_results = [
            capture("run_port_busy")
        ] * pristine.CONTAINER_START_ATTEMPTS

        with pytest.raises(ac.StartFailure, match="Address already in use"):
            runtime.start(runtime.specs["postgres"])

        assert len(daemon.runs()) == pristine.CONTAINER_START_ATTEMPTS


class TestReadiness:
    def test_polls_psql_until_it_answers(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, clock: Clock
    ) -> None:
        daemon.exec_results = [capture("psql_not_ready"), capture("psql_ready")]

        runtime.start(runtime.specs["postgres"])

        execs = [argv for argv, _ in daemon.calls if argv[1] == "exec"]
        assert len(execs) == 2
        assert execs[0][2:5] == (
            "-e",
            "PGPASSWORD=printstash",
            f"printstash-test-postgres-{os.getpid()}-abcd1234",
        )
        assert execs[0][-2:] == ("-c", "select version();")
        assert clock.slept == [1]

    def test_waits_for_the_seaweedfs_log_line(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, clock: Clock
    ) -> None:
        daemon.logs_results = [capture("logs_seaweedfs_not_ready")]

        runtime.start(runtime.specs["seaweedfs"])

        assert clock.slept == [1]

    def test_waits_for_nextcloud_to_answer_a_dav_listing_with_207(
        self, runtime: ac.AppleRuntime, network: Network, clock: Clock
    ) -> None:
        network.http = [ConnectionResetError(54, "Connection reset by peer"), 200, 207]

        runtime.start(runtime.specs["nextcloud"])

        assert [url for url, _, _ in network.requests[:3]] == [
            "http://127.0.0.1:20001/remote.php/dav/files/admin/"
        ] * 3
        assert clock.slept == [1, 1]

    def test_sends_the_dav_listing_as_an_authenticated_propfind(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.http = [207]

        runtime.start(runtime.specs["nextcloud"])

        assert network.requests[0][1:] == (
            60,
            {"method": "PROPFIND", "auth": ("admin", "contract-only")},
        )

    def test_treats_a_malformed_http_reply_as_not_ready(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        import http.client

        network.http = [http.client.BadStatusLine("garbage"), 207]

        runtime.start(runtime.specs["nextcloud"])

        assert len(network.requests) == 2

    def test_waits_for_openssh_nc_before_probing_the_banner(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        daemon.exec_results = [capture("nc_not_ready"), capture("nc_ready")]
        network.tcp_default = b"SSH-2.0-OpenSSH_10.3\r\n"

        runtime.start(runtime.specs["openssh"])

        assert [argv[-4:] for argv, _ in daemon.calls if argv[1] == "exec"] == [
            ("nc", "-z", "127.0.0.1", "2222")
        ] * 2

    def test_reports_the_last_log_lines_when_a_service_never_answers(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        daemon.exec_results = [capture("psql_not_ready")] * (
            pristine.STARTUP_TIMEOUT_S + 5
        )

        with pytest.raises(
            ac.StartFailure, match=r"(?s)not ready after 180s.*Last log lines"
        ):
            runtime.start(runtime.specs["postgres"])

    def test_polls_once_a_second_until_the_startup_timeout(
        self,
        runtime: ac.AppleRuntime,
        daemon: FakeDaemon,
        clock: Clock,
        pristine: ModuleType,
    ) -> None:
        daemon.exec_results = [capture("psql_not_ready")] * (
            pristine.STARTUP_TIMEOUT_S + 5
        )

        with pytest.raises(ac.StartFailure):
            runtime.start(runtime.specs["postgres"])

        assert clock.slept == [1] * pristine.STARTUP_TIMEOUT_S


class TestProbe:
    def test_speaks_the_postgres_protocol_through_the_published_port(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [b"S"]

        runtime.start(runtime.specs["postgres"])

        assert network.tcp == []

    def test_accepts_any_http_answer_from_seaweedfs(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.http_default = 403

        runtime.start(runtime.specs["seaweedfs"])

        assert network.urls[0] == "http://127.0.0.1:20001/"

    def test_names_local_network_when_the_port_accepts_then_resets(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [ConnectionResetError(54, "reset")] * 3

        with pytest.raises(ac.LocalNetworkDenied, match="container-runtime-linux"):
            runtime.start(runtime.specs["postgres"])

    def test_names_local_network_when_the_port_accepts_then_closes(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [b"", b"", b""]

        with pytest.raises(ac.LocalNetworkDenied):
            runtime.start(runtime.specs["postgres"])

    def test_recovers_when_a_reset_is_followed_by_an_answer(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [ConnectionResetError(54, "reset"), b"N"]

        runtime.start(runtime.specs["postgres"])

    def test_reports_a_refused_connection_as_a_start_failure(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [ConnectionRefusedError(61, "refused")] * 3

        with pytest.raises(ac.StartFailure, match="did not answer"):
            runtime.start(runtime.specs["postgres"])

    def test_rejects_a_server_that_does_not_speak_ssh(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp_default = b"HTTP/1.1 400"

        with pytest.raises(ac.LocalNetworkDenied):
            runtime.start(runtime.specs["openssh"])


class TestContainerAddress:
    def test_reads_the_address_without_its_prefix_length(
        self, runtime: ac.AppleRuntime
    ) -> None:
        assert runtime.container_address("anything") == "192.168.64.10"

    def test_names_the_terminal_when_the_address_is_unreachable(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.http = [
            urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host"))
        ]

        with pytest.raises(ac.LocalNetworkDenied, match="terminal app"):
            runtime.check_container_address("192.168.64.10", 8333)

    def test_reports_another_failure_as_a_start_failure(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.http = [ConnectionRefusedError(61, "refused")]

        with pytest.raises(ac.StartFailure, match="did not answer"):
            runtime.check_container_address("192.168.64.10", 8333)


class TestInstalledStarters:
    @pytest.fixture
    def installed(
        self,
        pristine: ModuleType,
        runtime: ac.AppleRuntime,
        network: Network,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ModuleType:
        # The lanes that exclude container-backed tests set this, and `_resolve`
        # refuses to start anything under it; these tests start fake services.
        monkeypatch.delenv("PRINTSTASH_TEST_NO_EXTERNAL", raising=False)
        network.http_default = 403
        network.tcp_default = b"N"
        ac.install(pristine, runtime)
        return pristine

    def test_postgres_url_names_the_loopback_address(
        self, installed: ModuleType
    ) -> None:
        assert installed._start_postgres() == (
            "postgresql://printstash:printstash@127.0.0.1:20001/printstash"
        )

    def test_pgvector_url_is_cached_by_the_upstream_resolver(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        first = installed.pgvector_url()
        second = installed.pgvector_url()

        assert first == second
        assert len(daemon.runs()) == 1

    def test_seaweedfs_endpoint_names_the_loopback_address(
        self, installed: ModuleType
    ) -> None:
        assert installed._start_seaweedfs() == "http://127.0.0.1:20001"

    def test_seaweedfs_publishes_the_container_address_for_the_gc_tests(
        self, installed: ModuleType
    ) -> None:
        installed._start_seaweedfs()

        assert installed.s3_private_endpoint.__name__ == "s3_private_endpoint"
        assert installed._resolved["s3_private"] == "http://192.168.64.10:8333"

    def test_the_private_endpoint_is_not_a_loopback_address(
        self, installed: ModuleType
    ) -> None:
        installed._start_seaweedfs()

        host = installed._resolved["s3_private"].split("//")[1].split(":")[0]
        assert not _is_loopback(host)

    def test_the_public_endpoint_is_a_loopback_address(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.http_default = 207

        host = installed._start_nextcloud().split("//")[1].split(":")[0]

        assert _is_loopback(host)

    def test_openssh_returns_the_loopback_endpoint(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.tcp_default = b"SSH-2.0-OpenSSH_10.3\r\n"

        host, port, _known_hosts = installed._start_openssh()

        assert (host, port) == ("127.0.0.1", 20001)

    def test_openssh_returns_the_scanned_host_keys(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.tcp_default = b"SSH-2.0-OpenSSH_10.3\r\n"

        _host, _port, known_hosts = installed._start_openssh()

        assert known_hosts.startswith("# 127.0.0.1:12222 SSH-2.0-OpenSSH")

    def test_openssh_retries_an_empty_host_key_reply(
        self, installed: ModuleType, daemon: FakeDaemon, clock: Clock, network: Network
    ) -> None:
        network.tcp_default = b"SSH-2.0-OpenSSH_10.3\r\n"
        daemon.keyscans = [reply(0, ""), capture("keyscan")]

        host, port, known_hosts = installed._start_openssh()

        assert (host, port) == ("127.0.0.1", 20001)
        assert known_hosts.startswith("# 127.0.0.1:12222 SSH-2.0-OpenSSH")
        assert clock.slept.count(0.25) == 1

    def test_stops_the_run_naming_the_start_command_when_apple_is_stopped(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        daemon.status = capture("system_status_stopped")

        with pytest.raises(
            pytest.exit.Exception,
            match="cannot start PostgreSQL.*container system start",
        ):
            installed.require_docker("PostgreSQL")

    def test_stops_the_run_naming_the_wedge_when_a_command_hangs(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"system"}

        with pytest.raises(pytest.exit.Exception, match="apple/container#2275"):
            installed.require_docker("PostgreSQL")

    def test_stops_the_run_naming_local_network_from_a_service_start(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.tcp = [ConnectionResetError(54, "reset")] * 3

        with pytest.raises(pytest.exit.Exception, match="Local Network"):
            installed._start_postgres()

    def test_reports_docker_as_available_when_the_runtime_is_up(
        self, installed: ModuleType
    ) -> None:
        assert installed.docker_available() is True


class TestReaper:
    def test_removes_a_container_whose_owner_is_gone(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.listing = [entry("printstash-test-postgres-1-aaaa0000", "1-1.00")]

        runtime.start(runtime.specs["postgres"])

        assert "printstash-test-postgres-1-aaaa0000" in daemon.deleted

    def test_keeps_a_container_whose_owner_is_alive(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.listing = [entry("printstash-test-postgres-1-aaaa0000", OWNER)]

        runtime.start(runtime.specs["postgres"])

        assert "printstash-test-postgres-1-aaaa0000" not in daemon.deleted

    def test_keeps_a_container_whose_owner_label_is_unreadable(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.seams = ac.Seams(
            **{**runtime.seams.__dict__, "owner_alive": ac._owner_alive}
        )
        daemon.listing = [
            entry("printstash-test-postgres-1-bad00000", "not-a-pid"),
            entry("printstash-test-postgres-2-dead0000", "1-1.00"),
        ]

        with pytest.warns(UserWarning, match="unreadable owner label"):
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == ["printstash-test-postgres-2-dead0000"]

    def test_leaves_containers_it_did_not_start(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.listing = [
            entry("someone-elses-web", "1-1.00"),
            entry("printstash-test-x", None),
        ]

        runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == []

    def test_reaps_once_per_process(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        network.http_default = 207

        runtime.start(runtime.specs["postgres"])
        runtime.start(runtime.specs["nextcloud"])

        assert daemon.verbs().count("list") == 1

    def test_reaps_before_the_first_run(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert daemon.verbs().index("list") < daemon.verbs().index("run")

    def test_skips_when_another_process_holds_the_lock(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        import fcntl

        runtime.seams.lock_dir.mkdir(parents=True)
        with open(runtime.seams.lock_dir / "reaper.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            runtime.start(runtime.specs["postgres"])

        assert "list" not in daemon.verbs()

    def test_warns_instead_of_raising_when_the_listing_fails(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"list"}

        with pytest.warns(UserWarning, match="reaper skipped"):
            runtime.start(runtime.specs["postgres"])

    def test_sweeps_only_what_this_process_labelled(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])
        daemon.listing = [entry("mine", OWNER), entry("theirs", "9-9.00")]

        runtime.sweep_own()

        assert daemon.deleted == ["mine"]

    def test_sweeps_nothing_when_nothing_was_started(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.sweep_own()

        assert daemon.calls == []


class TestOwnerIdentity:
    def test_recognises_this_process(self) -> None:
        assert ac._owner_alive(ac._own_token()) is True

    def test_rejects_a_recycled_pid_with_another_start_time(self) -> None:
        pid = ac._own_token().split("-")[0]

        assert ac._owner_alive(f"{pid}-1.00") is False

    def test_rejects_a_process_that_has_exited(self) -> None:
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()

        assert ac._owner_alive(f"{finished.pid}-1.00") is False


class TestCaptures:
    @pytest.mark.parametrize(
        "path", sorted(CAPTURES.glob("*.json")), ids=lambda p: p.stem
    )
    def test_hold_no_private_identity(self, path: Path) -> None:
        text = path.read_text()

        assert "/Users/andrewws" not in text
        assert "andrewseago" not in text


def project(root: Path, *, tests: dict[str, str]) -> Path:
    """A throwaway project whose conftest installs the adapter against a recorder."""
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts =\nmarkers =\n    bgcode: needs docker\n    e2e: tier\n"
    )
    (root / "conftest.py").write_text(
        "import json, os, subprocess\n"
        "from pathlib import Path\n"
        "from tests import containers\n"
        "from tests.apple_container import AppleRuntime, Seams, activate, pytest_addoption\n"
        "CALLS = Path(__file__).parent / 'calls.json'\n"
        "def record(argv, timeout):\n"
        "    with CALLS.open('a') as handle:\n"
        "        handle.write(json.dumps(list(argv)) + '\\n')\n"
        "    return subprocess.CompletedProcess(argv, 1, '', 'recorder')\n"
        "def pytest_configure(config):\n"
        "    runtime = AppleRuntime(containers, Seams(cli=record))\n"
        "    activate(config, containers, 'darwin', runtime=runtime, tests_dir=Path(__file__).parent)\n"
    )
    for name, source in tests.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


PLAIN = "def test_plain():\n    pass\n"
DOCKER_ONLY = {
    "e2e/test_search_image_proxy.py": "def test_proxy():\n    pass\n",
    "e2e/test_search_query_logs.py": "def test_logs():\n    pass\n",
    "test_marked.py": "import pytest\n\n@pytest.mark.bgcode\ndef test_marked():\n    pass\n",
}


def pytest_in(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *args],
        cwd=root,
        env={
            **environment,
            "PYTHONPATH": str(REPO_ROOT / "backend"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


class TestDockerOnlyTests:
    def test_a_run_that_selects_one_stops_naming_every_one(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root)

        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        for name in (
            "test_search_image_proxy.py::test_proxy",
            "test_search_query_logs.py::test_logs",
            "test_marked.py::test_marked",
        ):
            assert name in output, output
        assert "passed" not in result.stdout

    def test_the_stop_names_the_opt_in_flag(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root)

        assert ac.DESELECT_OPTION in result.stdout + result.stderr

    def test_the_opt_in_flag_leaves_them_out(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, ac.DESELECT_OPTION)

        assert "1 passed, 3 deselected" in result.stdout, result.stdout

    def test_the_opt_in_flag_runs_everything_else(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, ac.DESELECT_OPTION, "-rp")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASSED test_plain.py::test_plain" in result.stdout

    def test_the_summary_names_each_test_that_was_left_out(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, ac.DESELECT_OPTION)

        for name in (
            "test_search_image_proxy.py::test_proxy",
            "test_search_query_logs.py::test_logs",
            "test_marked.py::test_marked",
        ):
            assert name in result.stdout, result.stdout

    def test_a_run_that_deselects_them_first_does_not_stop(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, "-m", "not bgcode", "-k", "plain")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_xdist_shows_the_stop_instead_of_losing_it(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, "-p", "xdist.plugin", "-n", "2")

        output = result.stdout + result.stderr
        assert "test_marked.py::test_marked" in output, output
        assert "no active workers" not in output

    def test_xdist_collects_the_names_from_every_worker(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, "-p", "xdist.plugin", "-n", "2", ac.DESELECT_OPTION)

        assert result.returncode == 0, result.stdout + result.stderr
        for name in (
            "test_search_image_proxy.py::test_proxy",
            "test_search_query_logs.py::test_logs",
            "test_marked.py::test_marked",
        ):
            assert name in result.stdout, result.stdout

    def test_a_listed_file_that_is_gone_is_a_usage_error(self, tmp_path: Path) -> None:
        files = {k: v for k, v in DOCKER_ONLY.items() if "image_proxy" not in k}
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **files})

        result = pytest_in(root)

        assert result.returncode == 4, result.stdout + result.stderr
        assert "e2e/test_search_image_proxy.py" in result.stderr

    def test_a_run_that_selects_none_makes_no_container_command(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, ac.DESELECT_OPTION)

        assert result.returncode == 0, result.stdout + result.stderr
        assert not (root / "calls.json").exists()


def _assigned_strings(tree: ast.AST) -> dict[str, str]:
    return {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _start_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _env_pairs(
    function: ast.FunctionDef, constants: dict[str, str]
) -> set[tuple[str, str]]:
    def literal(node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return str(node.value)
        assert isinstance(node, ast.Name), ast.dump(node)
        return constants[node.id]

    return {
        (literal(call.args[0]), literal(call.args[1]))
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "with_env"
    }


class TestSpecsMatchContainers:
    @pytest.fixture
    def source(self) -> tuple[ast.Module, dict[str, str]]:
        tree = ast.parse((TESTS_DIR / "containers.py").read_text())
        return tree, _assigned_strings(tree)

    @pytest.mark.parametrize(
        ("service", "function"),
        [
            pytest.param("seaweedfs", "_start_seaweedfs", id="seaweedfs"),
            pytest.param("nextcloud", "_start_nextcloud", id="nextcloud"),
            pytest.param("openssh", "_start_openssh", id="openssh"),
        ],
    )
    def test_environment_matches_what_the_upstream_function_sets(
        self, runtime: ac.AppleRuntime, source: Any, service: str, function: str
    ) -> None:
        tree, constants = source

        upstream = _env_pairs(_start_function(tree, function), constants)

        assert set(runtime.specs[service].env) == upstream

    def test_postgres_environment_names_the_upstream_credentials(
        self, runtime: ac.AppleRuntime, pristine: ModuleType
    ) -> None:
        assert dict(runtime.specs["postgres"].env) == {
            "POSTGRES_USER": pristine.POSTGRES_USER,
            "POSTGRES_PASSWORD": pristine.POSTGRES_PASSWORD,
            "POSTGRES_DB": pristine.POSTGRES_DB,
        }

    def test_pgvector_image_is_the_literal_upstream_starts(self, source: Any) -> None:
        tree, _ = source
        literals = {
            node.value
            for node in ast.walk(_start_function_anywhere(tree, "pgvector_url"))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("pgvector/pgvector")
        }

        assert literals == {ac.PGVECTOR_IMAGE}

    @pytest.mark.parametrize(
        ("service", "function", "port"),
        [
            pytest.param("seaweedfs", "_start_seaweedfs", 8333, id="seaweedfs"),
            pytest.param("nextcloud", "_start_nextcloud", 80, id="nextcloud"),
            pytest.param("openssh", "_start_openssh", 2222, id="openssh"),
        ],
    )
    def test_container_port_is_the_one_upstream_exposes(
        self,
        runtime: ac.AppleRuntime,
        source: Any,
        service: str,
        function: str,
        port: int,
    ) -> None:
        tree, constants = source
        exposed = {
            constants.get(arg.id) if isinstance(arg, ast.Name) else arg.value
            for call in ast.walk(_start_function(tree, function))
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "with_exposed_ports"
            for arg in call.args
        }

        assert runtime.specs[service].container_port == port
        assert exposed <= {port, None}

    def test_every_testcontainers_user_is_handled_or_outside_pytest(self) -> None:
        users = {
            str(path.relative_to(TESTS_DIR))
            for path in TESTS_DIR.rglob("*.py")
            if "testcontainers" in path.read_text()
            and path.name not in {Path(__file__).name, "apple_container.py"}
        }

        assert users == {
            "containers.py",
            "e2e/runtime_image.py",
            "fixtures/dbos-2.31.1/generate.py",
        }, (
            "a new testcontainers user needs an Apple decision in tests/apple_container.py"
        )


def _start_function_anywhere(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _docker_cli_users() -> set[str]:
    users = set()
    for path in TESTS_DIR.rglob("*.py"):
        if path.name in {Path(__file__).name, "apple_container.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, (ast.List, ast.Tuple))
                and len(node.elts) >= 2
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "docker"
            ):
                users.add(str(path.relative_to(TESTS_DIR)))
    return users


# Files that name the docker CLI and are not tests that need a daemon.
DOCKER_CLI_NOT_NEEDING_A_DAEMON = {
    "bgcode_support.py": "builds the converter; its only callers are the bgcode-marked tests",
    "containers.py": "the upstream path the adapter replaces",
    "e2e/runtime_image.py": "the image lane, run as its own process outside pytest",
    "repo/test_bgcode_support.py": "mocks subprocess.run",
    "repo/test_compose_layout.py": "reads compose files; the compose CLI only renders them",
    "repo/test_default_compose.py": "renders compose files",
    "repo/test_unified_container.py": "renders compose files",
    "repo/test_workers_compose.py": "renders compose files",
}


class TestDockerOnlyInventory:
    def test_every_listed_file_really_calls_docker(self) -> None:
        assert set(ac.DOCKER_ONLY_FILES) <= _docker_cli_users()

    def test_every_docker_cli_user_is_classified(self) -> None:
        classified = set(ac.DOCKER_ONLY_FILES) | set(DOCKER_CLI_NOT_NEEDING_A_DAEMON)

        assert _docker_cli_users() - classified == set(), (
            "a test now shells out to docker: list it in DOCKER_ONLY_FILES or explain "
            "it in DOCKER_CLI_NOT_NEEDING_A_DAEMON"
        )

    def test_the_bgcode_marker_exists(self) -> None:
        markers = (REPO_ROOT / "backend" / "pyproject.toml").read_text()

        assert all(f'"{marker}:' in markers for marker in ac.DOCKER_ONLY_MARKERS)


class TestOutOfPytestConsumers:
    def test_every_process_outside_pytest_that_uses_containers_is_documented(
        self,
    ) -> None:
        readme = (REPO_ROOT / "deploy" / "apple-container" / "README.md").read_text()
        consumers = [
            "tests/e2e/runtime_image.py",
            "tests/fakes/browser_delivery.py",
            "tests/fakes/s3_delivery.py",
        ]

        assert [path for path in consumers if path not in readme] == []

    def test_the_documented_files_are_the_ones_that_import_containers(self) -> None:
        importers = {
            str(path.relative_to(TESTS_DIR.parent))
            for path in TESTS_DIR.rglob("*.py")
            if "tests.containers" in path.read_text()
        }

        assert {
            "tests/e2e/runtime_image.py",
            "tests/fakes/browser_delivery.py",
            "tests/fakes/s3_delivery.py",
        } <= importers | {"tests/e2e/runtime_image.py"}


class TestBackendConftest:
    def test_does_nothing_but_load_the_plugin(self) -> None:
        tree = ast.parse((REPO_ROOT / "backend" / "conftest.py").read_text())

        assert [ast.unparse(node) for node in tree.body] == [
            "pytest_plugins = ('tests.apple_container',)"
        ]
