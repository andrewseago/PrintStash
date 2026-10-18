"""The Apple `container` adapter starts the suite's services and fails with the fix.

`tests/apple_container.py` replaces the functions in `tests/containers.py` that
start PostgreSQL, SeaweedFS, Nextcloud and OpenSSH. It runs PostgreSQL, SeaweedFS and
OpenSSH on `container` and leaves Nextcloud out, so the resource-marked tests run on a
Mac that has Apple's `container` runtime and no Docker daemon; tests that need
Nextcloud are named as Docker-only.

Its promises, each held by a group below: a supported runtime is checked once; images
are pulled once, from the right registry, with transient failures retried; services
start on explicit loopback ports and count as ready only by their own signal and one
protocol probe; a service that never becomes ready is removed and not started again;
only a connection accepted and then dropped counts as missing Local Network permission;
every command is bounded, and a hang, an unavailable runtime or a missing permission
stops the run once with the fix, under xdist too; containers are always removed, by
the session or by the next run's reaper, which never touches another live session's;
Docker-only tests are named up front; and the adapter fails loudly when upstream
drifts away from what it copied.

A fake `container` CLI replays output recorded from a real 1.5.0 install
(`tests/fixtures/apple_container/`, fake paths, addresses and MACs), the clock and the
network are injected, and the collection hooks run in throwaway projects. A pristine
copy of `tests/containers.py` is loaded for each test, so nothing here touches the
module the rest of the suite uses.

Checked on a Mac with the runtime running, not here: the real 1.5.0 CLI end to end,
and a resource lane run through the adapter.
"""

from __future__ import annotations

import ast
import copy
import errno
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import threading
import urllib.error
import warnings
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.modules.storage.storage_identity import _is_loopback
from tests import apple_container as ac
from tests import conftest as suite_conftest
from tests.paths import FIXTURES_DIR, REPO_ROOT, TESTS_DIR

CAPTURES = FIXTURES_DIR / "apple_container"
OWNER = "4242-1700000000.25"
SSL_REQUEST = b"\x00\x00\x00\x08\x04\xd2\x16\x2f"
SSH_BANNER = b"SSH-2.0-OpenSSH_10.3\r\n"


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


def with_versions(client: str, server: str) -> subprocess.CompletedProcess[str]:
    entries = [
        {"appName": "container", "version": client},
        {"appName": "container-apiserver", "version": server},
    ]
    return reply(0, json.dumps(entries))


def with_arch(arch: str) -> subprocess.CompletedProcess[str]:
    status = json.loads(capture("system_status_running").stdout)
    status["host"]["architecture"] = arch
    return reply(0, json.dumps(status))


def entry(name: str, owner: str | None) -> dict[str, Any]:
    """A listing entry shaped like a recorded one, with this id and owner label."""
    shape = copy.deepcopy(json.loads(capture("list_all").stdout)[1])
    shape["configuration"]["id"] = name
    shape["configuration"]["labels"] = {} if owner is None else {ac.OWNER_LABEL: owner}
    return shape


class FakeDaemon:
    """Answers the `container` CLI from recorded replies and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.present: set[str] = set()
        self.run_results: list[subprocess.CompletedProcess[str]] = []
        self.pull_results: list[subprocess.CompletedProcess[str]] = []
        self.exec_results: list[subprocess.CompletedProcess[str]] = []
        self.logs_results: list[subprocess.CompletedProcess[str]] = []
        self.delete_results: list[subprocess.CompletedProcess[str]] = []
        self.keyscans: list[subprocess.CompletedProcess[str] | BaseException] = []
        self.status = capture("system_status_running")
        self.version = capture("system_version_running")
        self.inspect = capture("inspect_seaweedfs")
        self.listing: list[dict[str, Any]] = []
        self.timeouts: set[str] = set()
        self.deleted: list[str] = []

    def __call__(self, argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
        argv = tuple(argv)
        self.calls.append((argv, timeout))
        if argv[0] == "ssh-keyscan":
            result = self.keyscans.pop(0) if self.keyscans else capture("keyscan")
            if isinstance(result, BaseException):
                raise result
            return result
        verb = " ".join(argv[1:3]) if argv[1] in ("system", "image") else argv[1]
        if verb in self.timeouts:
            raise ac.CliTimeout(argv, timeout)
        if verb == "system status":
            return self.status
        if verb == "system version":
            return self.version
        if verb == "image inspect":
            present = argv[3] in self.present
            return capture(
                "image_inspect_present" if present else "image_inspect_missing"
            )
        if verb == "image pull":
            result = (
                self.pull_results.pop(0)
                if self.pull_results
                else capture("image_pull_ok")
            )
            if result.returncode == 0:
                self.present.add(argv[-1])
            return result
        if verb == "run":
            return self.run_results.pop(0) if self.run_results else capture("run_ok")
        if verb == "exec":
            return self.exec_results.pop(0) if self.exec_results else reply()
        if verb == "logs":
            return (
                self.logs_results.pop(0)
                if self.logs_results
                else capture("logs_seaweedfs_ready")
            )
        if verb == "inspect":
            return self.inspect
        if verb == "delete":
            self.deleted.append(argv[-1])
            return self.delete_results.pop(0) if self.delete_results else reply()
        if verb == "list":
            return reply(0, json.dumps(self.listing))
        raise AssertionError(f"unscripted command: {argv}")

    def verbs(self) -> list[str]:
        return [
            " ".join(argv[1:3]) if argv[1] in ("system", "image") else argv[1]
            for argv, _ in self.calls
            if argv[0] == "container"
        ]

    def runs(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if argv[:2] == ("container", "run")]

    def timeout_of(self, verb: str) -> set[int]:
        return {
            timeout
            for argv, timeout in self.calls
            if argv[0] == "container"
            and (" ".join(argv[1:3]) if argv[1] in ("system", "image") else argv[1])
            == verb
        }


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
        self.sent: list[tuple[int, bytes | None]] = []
        self.http_default: Any = 403
        self.tcp_default: Any = b"N"

    def http_status(self, url: str, timeout: float) -> int:
        self.urls.append(url)
        result = self.http.pop(0) if self.http else self.http_default
        if isinstance(result, BaseException):
            raise result
        return result

    def tcp_reply(self, port: int, payload: bytes | None, timeout: float) -> bytes:
        self.sent.append((port, payload))
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


def name_of(key: str) -> str:
    return f"printstash-test-{key}-{os.getpid()}-abcd1234"


class TestRunCli:
    def test_turns_a_hang_into_cli_timeout(self) -> None:
        with pytest.raises(ac.CliTimeout, match="did not answer within 1s"):
            ac.run_cli([sys.executable, "-c", "import time; time.sleep(5)"], 1)

    def test_names_a_missing_binary(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "no-such-container")

        with pytest.raises(ac.RuntimeUnavailable, match="no-such-container"):
            ac.run_cli([missing, "system", "status"], 5)

    def test_gives_the_command_no_stdin_to_wait_on(self) -> None:
        result = ac.run_cli(
            [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], 5
        )

        assert result.stdout.strip() == "''"


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

    @pytest.mark.parametrize(
        "token", ["not-a-pid", "123", "123-4.5", "123-4.567-x"], ids=str
    )
    def test_refuses_a_token_it_did_not_write(self, token: str) -> None:
        with pytest.raises(ValueError, match="not an owner token"):
            ac._owner_alive(token)


class TestNotice:
    def test_never_raises_when_warnings_are_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")

            ac._notice("could not remove x")

        assert "could not remove x" in capsys.readouterr().err


class TestOpenLock:
    def test_creates_a_directory_only_this_user_can_use(self, tmp_path: Path) -> None:
        os.close(ac._open_lock(tmp_path / "locks", "reaper.lock"))

        assert stat.S_IMODE((tmp_path / "locks").stat().st_mode) == 0o700

    def test_refuses_a_lock_file_that_is_a_symlink(self, tmp_path: Path) -> None:
        (tmp_path / "locks").mkdir(mode=0o700)
        victim = tmp_path / "victim"
        victim.write_text("keep me")
        (tmp_path / "locks" / "reaper.lock").symlink_to(victim)

        with pytest.raises(OSError):
            ac._open_lock(tmp_path / "locks", "reaper.lock")

        assert victim.read_text() == "keep me"

    def test_refuses_a_directory_that_is_a_symlink(self, tmp_path: Path) -> None:
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "locks").symlink_to(tmp_path / "elsewhere")

        with pytest.raises(PermissionError):
            ac._open_lock(tmp_path / "locks", "reaper.lock")


class TestRegistryReference:
    def test_pulls_a_digest_pinned_lscr_image_from_ghcr(self) -> None:
        reference = "lscr.io/linuxserver/openssh-server@sha256:" + "a" * 64

        assert (
            ac.registry_reference(reference)
            == "ghcr.io/linuxserver/openssh-server@sha256:" + "a" * 64
        )

    def test_leaves_other_registries_alone(self) -> None:
        assert ac.registry_reference("postgres:16-alpine") == "postgres:16-alpine"

    def test_refuses_an_lscr_image_that_is_not_pinned(self) -> None:
        with pytest.raises(ValueError, match="not pinned by digest"):
            ac.registry_reference("lscr.io/linuxserver/openssh-server:latest")


class TestCheckAvailable:
    def test_accepts_a_running_supported_runtime(
        self, runtime: ac.AppleRuntime
    ) -> None:
        runtime.check_available()

        assert runtime.arch == "arm64"

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

    @pytest.mark.parametrize(
        ("client", "server"),
        [
            pytest.param("1.4.1", "1.5.0", id="old-client"),
            pytest.param("1.6.0", "1.5.0", id="new-client"),
            pytest.param("1.5.0", "1.4.1", id="old-apiserver"),
            pytest.param("1.5.0", "1.6.0", id="new-apiserver"),
            pytest.param("2.0.0", "2.0.0", id="next-major"),
        ],
    )
    def test_rejects_a_version_outside_the_supported_range(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, client: str, server: str
    ) -> None:
        daemon.version = with_versions(client, server)

        with pytest.raises(ac.RuntimeUnavailable, match="outside the supported range"):
            runtime.check_available()

    @pytest.mark.parametrize("version", ["1.5.0", "1.5.9"], ids=str)
    def test_accepts_every_patch_of_the_supported_minor(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, version: str
    ) -> None:
        daemon.version = with_versions(version, version)

        runtime.check_available()

    def test_rejects_a_reply_without_the_apiserver(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.version = capture("system_version_stopped")

        with pytest.raises(ac.RuntimeUnavailable, match="apiserver must answer"):
            runtime.check_available()

    def test_reports_a_failed_version_command(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.version = reply(1, "", "Error: XPC connection error")

        with pytest.raises(ac.RuntimeUnavailable, match="system version failed"):
            runtime.check_available()

    def test_reports_unreadable_output(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.status = reply(0, "not json")

        with pytest.raises(ac.RuntimeUnavailable, match="cannot read"):
            runtime.check_available()

    def test_reports_a_hung_command_as_a_timeout(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"system status"}

        with pytest.raises(ac.CliTimeout):
            runtime.check_available()

    def test_answers_false_instead_of_raising_for_the_boolean_form(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.status = capture("system_status_stopped")

        assert runtime.available() is False

    def test_takes_the_architecture_from_the_host(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.status = with_arch("x86_64")

        runtime.start(runtime.specs["postgres"])

        argv = daemon.runs()[0]
        assert argv[argv.index("--platform") + 1] == "linux/amd64"


class TestEnsureImage:
    def test_pulls_nothing_when_the_image_is_present(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.present.add("postgres:16-alpine")

        runtime.ensure_image("postgres:16-alpine", "arm64")

        assert daemon.verbs() == ["image inspect"]

    def test_pulls_nothing_when_another_process_pulled_it_meanwhile(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        def cli(argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
            result = daemon(argv, timeout)
            if tuple(argv[1:3]) == ("image", "inspect"):
                # The other process finishes its pull while this one waits for the lock.
                daemon.present.add(argv[3])
            return result

        runtime.seams = ac.Seams(**{**runtime.seams.__dict__, "cli": cli})

        runtime.ensure_image("postgres:16-alpine", "arm64")

        assert daemon.verbs() == ["image inspect", "image inspect"]

    def test_waits_for_another_process_pulling_the_same_image(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        digest = hashlib.sha256(b"postgres:16-alpine").hexdigest()[:16]
        held = ac._open_lock(runtime.seams.lock_dir, f"pull-{digest}.lock")
        fcntl.flock(held, fcntl.LOCK_EX)
        waiter = threading.Thread(
            target=runtime.ensure_image, args=("postgres:16-alpine", "arm64")
        )
        waiter.start()
        waiter.join(0.5)
        pulls_while_held = daemon.verbs().count("image pull")
        os.close(held)
        waiter.join(10)

        assert (pulls_while_held, daemon.verbs().count("image pull")) == (0, 1)

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

        assert (
            runtime.ensure_image(reference, "arm64")
            == "ghcr.io/linuxserver/openssh-server@sha256:" + "b" * 64
        )

    def test_retries_a_transient_registry_failure_with_backoff(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, clock: Clock
    ) -> None:
        daemon.pull_results = [reply(1, "", "toomanyrequests: slow down")] * 2

        runtime.ensure_image("postgres:16-alpine", "arm64")

        assert clock.slept == [1, 2]

    def test_does_not_retry_a_permanent_pull_failure(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.pull_results = [capture("image_pull_lscr_refused")]

        with pytest.raises(ac.StartFailure, match="registrable domain"):
            runtime.ensure_image("postgres:16-alpine", "arm64")

        assert daemon.verbs().count("image pull") == 1

    def test_names_the_manual_pull_when_the_pull_times_out(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"image pull"}

        with pytest.raises(ac.StartFailure, match="Pull it by hand"):
            runtime.ensure_image("postgres:16-alpine", "arm64")

    def test_does_not_retry_a_pull_that_timed_out(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"image pull"}

        with pytest.raises(ac.StartFailure):
            runtime.ensure_image("postgres:16-alpine", "arm64")

        assert daemon.verbs().count("image pull") == 1


RUN_POSTGRES = [
    "container", "run", "--detach", "--progress", "none", "--platform", "linux/arm64",
    "--name", name_of("postgres"), "--label", f"printstash.test.owner={OWNER}",
    "-p", "127.0.0.1:20001:5432",
    "-e", "POSTGRES_USER=printstash", "-e", "POSTGRES_PASSWORD=printstash",
    "-e", "POSTGRES_DB=printstash", "postgres:16-alpine",
]  # fmt: skip


class TestStart:
    def test_runs_postgres_on_an_explicit_loopback_port(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert list(daemon.runs()[0]) == RUN_POSTGRES

    def test_runs_seaweedfs_with_its_mini_command(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        runtime.start(runtime.specs["seaweedfs"])

        assert daemon.runs()[0][-5:] == (
            pristine.SEAWEEDFS_IMAGE, "mini", "-dir=/data", "-master.volumeSizeLimitMB=8", "-master.telemetry=false",
        )  # fmt: skip

    def test_runs_openssh_from_ghcr(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        network.tcp_default = SSH_BANNER

        runtime.start(runtime.specs["openssh"])

        assert daemon.runs()[0][-1].startswith(
            "ghcr.io/linuxserver/openssh-server@sha256:"
        )

    def test_registers_the_handle_so_shutdown_force_deletes_it(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        pristine.shutdown_containers()

        assert (
            (("container", "delete", "--force", name_of("postgres"))),
            ac.SHORT_TIMEOUT_S,
        ) in daemon.calls

    def test_checks_availability_before_any_other_command(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert daemon.verbs()[:2] == ["system status", "system version"]

    def test_bounds_each_command_by_its_timeout(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        network.tcp_default = SSH_BANNER
        runtime.start(runtime.specs["openssh"])
        runtime.sweep_own()

        assert {
            verb: daemon.timeout_of(verb)
            for verb in ("run", "exec", "list", "image inspect", "image pull")
        } == {
            "run": {ac.RUN_TIMEOUT_S},
            "exec": {ac.SHORT_TIMEOUT_S},
            "list": {ac.SHORT_TIMEOUT_S},
            "image inspect": {ac.SHORT_TIMEOUT_S},
            "image pull": {ac.PULL_TIMEOUT_S},
        }

    def test_bounds_log_reads(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["seaweedfs"])

        assert daemon.timeout_of("logs") == {ac.SHORT_TIMEOUT_S}

    def test_registers_the_handle_before_running(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        daemon.timeouts = {"run"}

        with pytest.raises(ac.CliTimeout):
            runtime.start(runtime.specs["postgres"])

        pristine.shutdown_containers()
        assert daemon.deleted == [name_of("postgres")]

    def test_removes_the_container_after_a_failed_run(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.run_results = [capture("run_name_exists")]

        with pytest.raises(ac.StartFailure, match="already exists"):
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == [name_of("postgres")]

    def test_retries_with_a_new_port_when_the_port_is_taken(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.run_results = [capture("run_port_busy")]

        runtime.start(runtime.specs["postgres"])

        assert [argv[argv.index("-p") + 1] for argv in daemon.runs()] == [
            "127.0.0.1:20001:5432",
            "127.0.0.1:20002:5432",
        ]

    def test_backs_off_between_transient_failures(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, clock: Clock
    ) -> None:
        daemon.run_results = [reply(1, "", "toomanyrequests: slow down")] * 2

        runtime.start(runtime.specs["postgres"])

        assert clock.slept[:2] == [1, 2]

    def test_does_not_retry_a_permanent_failure(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.run_results = [capture("run_platform_mismatch")]

        with pytest.raises(
            ac.StartFailure, match="does not support required platforms"
        ):
            runtime.start(runtime.specs["postgres"])

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
        assert execs[0] == (
            "container", "exec", "-e", "PGPASSWORD=printstash", name_of("postgres"), "psql",
            "--username", "printstash", "--dbname", "printstash", "--host", "127.0.0.1", "-c", "select version();",
        )  # fmt: skip
        assert clock.slept == [1]

    def test_waits_for_the_seaweedfs_log_line(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, clock: Clock
    ) -> None:
        daemon.logs_results = [capture("logs_seaweedfs_not_ready")]

        runtime.start(runtime.specs["seaweedfs"])

        assert clock.slept == [1]

    def test_waits_for_openssh_nc_before_probing_the_banner(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, network: Network
    ) -> None:
        daemon.exec_results = [capture("nc_not_ready"), capture("nc_ready")]
        network.tcp_default = SSH_BANNER

        runtime.start(runtime.specs["openssh"])

        assert [argv[-4:] for argv, _ in daemon.calls if argv[1] == "exec"] == [
            ("nc", "-z", "127.0.0.1", "2222")
        ] * 2

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

        with pytest.raises(ac.StartFailure, match="not ready after 180s"):
            runtime.start(runtime.specs["postgres"])

        assert clock.slept == [1] * pristine.STARTUP_TIMEOUT_S

    def test_reports_only_the_last_twenty_log_lines(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        daemon.exec_results = [capture("psql_not_ready")] * (
            pristine.STARTUP_TIMEOUT_S + 5
        )
        daemon.logs_results = [reply(0, "".join(f"line {n}\n" for n in range(30)))]

        with pytest.raises(ac.StartFailure) as raised:
            runtime.start(runtime.specs["postgres"])

        tail = str(raised.value).split("Last log lines:\n")[1].splitlines()
        assert tail == [f"line {n}" for n in range(10, 30)]

    def test_removes_a_container_that_never_becomes_ready(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        daemon.exec_results = [capture("psql_not_ready")] * (
            pristine.STARTUP_TIMEOUT_S + 5
        )

        with pytest.raises(ac.StartFailure):
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == [name_of("postgres")]
        assert pristine._started == []

    def test_does_not_start_a_service_again_after_it_failed(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon, pristine: ModuleType
    ) -> None:
        daemon.exec_results = [capture("psql_not_ready")] * (
            pristine.STARTUP_TIMEOUT_S + 5
        )
        with pytest.raises(ac.StartFailure):
            runtime.start(runtime.specs["postgres"])

        with pytest.raises(
            ac.StartFailure, match="failed to start earlier in this session"
        ):
            runtime.start(runtime.specs["postgres"])

        assert len(daemon.runs()) == 1


class TestProbe:
    def test_sends_an_ssl_request_to_the_published_postgres_port(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert network.sent == [(20001, SSL_REQUEST)]

    def test_sends_nothing_to_openssh_before_its_banner(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp_default = SSH_BANNER

        runtime.start(runtime.specs["openssh"])

        assert network.sent == [(20001, None)]

    def test_accepts_any_http_answer_from_seaweedfs(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
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

    def test_names_local_network_when_http_is_dropped(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        import http.client

        network.http = [http.client.RemoteDisconnected("closed")] * 3

        with pytest.raises(ac.LocalNetworkDenied):
            runtime.start(runtime.specs["seaweedfs"])

    def test_pauses_before_probing_again(
        self, runtime: ac.AppleRuntime, network: Network, clock: Clock
    ) -> None:
        network.tcp = [ConnectionResetError(54, "reset"), b"N"]

        runtime.start(runtime.specs["postgres"])

        assert clock.slept == [1]

    def test_reports_refused_connections_as_a_start_failure(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [ConnectionRefusedError(61, "refused")] * 3

        with pytest.raises(ac.StartFailure, match="did not answer"):
            runtime.start(runtime.specs["postgres"])

    def test_does_not_blame_local_network_for_a_mix_of_failures(
        self, runtime: ac.AppleRuntime, network: Network
    ) -> None:
        network.tcp = [
            ConnectionResetError(54, "reset"),
            ConnectionRefusedError(61, "x"),
            ConnectionRefusedError(61, "x"),
        ]

        with pytest.raises(ac.StartFailure):
            runtime.start(runtime.specs["postgres"])

    @pytest.mark.parametrize(
        ("service", "answer"),
        [
            pytest.param("openssh", b"HTTP/1.1 400", id="openssh"),
            pytest.param("postgres", b"E", id="postgres"),
        ],
    )
    def test_reports_the_wrong_protocol_as_a_start_failure(
        self, runtime: ac.AppleRuntime, network: Network, service: str, answer: bytes
    ) -> None:
        network.tcp_default = answer

        with pytest.raises(ac.StartFailure, match="not in the protocol"):
            runtime.start(runtime.specs[service])

    def test_removes_the_container_when_the_probe_fails(
        self, runtime: ac.AppleRuntime, network: Network, daemon: FakeDaemon
    ) -> None:
        network.tcp = [ConnectionRefusedError(61, "refused")] * 3

        with pytest.raises(ac.StartFailure):
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == [name_of("postgres")]


class TestContainerAddress:
    def test_reads_the_address_without_its_prefix_length(
        self, runtime: ac.AppleRuntime
    ) -> None:
        assert runtime.container_address("anything") == "192.168.64.10"

    def test_reports_a_failed_inspect(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.inspect = capture("inspect_missing")

        with pytest.raises(ac.StartFailure, match="cannot inspect"):
            runtime.container_address("anything")

    def test_reports_a_container_without_a_network(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.inspect = capture("inspect_stopped")

        with pytest.raises(ac.StartFailure, match="no network address"):
            runtime.container_address("anything")

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


class TestDelete:
    def test_force_deletes_within_the_short_timeout(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.delete("x")

        assert daemon.calls == [
            (("container", "delete", "--force", "x"), ac.SHORT_TIMEOUT_S)
        ]

    def test_says_nothing_about_a_container_already_gone(
        self,
        runtime: ac.AppleRuntime,
        daemon: FakeDaemon,
        recwarn: pytest.WarningsRecorder,
    ) -> None:
        daemon.delete_results = [capture("delete_missing")]

        runtime.delete("x")

        assert [str(w.message) for w in recwarn] == []

    def test_warns_about_another_failure(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.delete_results = [reply(1, "", "Error: XPC connection error")]

        with pytest.warns(UserWarning, match="could not remove x"):
            runtime.delete("x")

    def test_warns_instead_of_raising_when_delete_hangs(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"delete"}

        with pytest.warns(UserWarning, match="could not remove x"):
            runtime.delete("x")


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

    def test_leaves_containers_it_did_not_start(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.listing = [
            entry("someone-elses-web", "1-1.00"),
            entry("printstash-test-x", None),
        ]

        runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == []

    def test_keeps_a_container_whose_owner_label_is_unreadable(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.seams = ac.Seams(
            **{**runtime.seams.__dict__, "owner_alive": ac._owner_alive}
        )
        daemon.listing = [entry("printstash-test-postgres-1-bad00000", "not-a-pid")]

        with pytest.warns(UserWarning, match="unreadable owner label"):
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == []

    def test_still_reaps_the_rest_when_warnings_are_errors(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.seams = ac.Seams(
            **{**runtime.seams.__dict__, "owner_alive": ac._owner_alive}
        )
        daemon.listing = [
            entry("printstash-test-postgres-1-bad00000", "not-a-pid"),
            entry("printstash-test-postgres-2-dead0000", "1-1.00"),
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            runtime.start(runtime.specs["postgres"])

        assert daemon.deleted == ["printstash-test-postgres-2-dead0000"]

    def test_reaps_once_per_process(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])
        runtime.start(runtime.specs["pgvector"])

        assert daemon.verbs().count("list") == 1

    def test_reaps_before_the_first_run(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        runtime.start(runtime.specs["postgres"])

        assert daemon.verbs().index("list") < daemon.verbs().index("run")

    def test_skips_when_another_process_holds_the_lock(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        held = ac._open_lock(runtime.seams.lock_dir, "reaper.lock")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            runtime.start(runtime.specs["postgres"])
        finally:
            os.close(held)

        assert "list" not in daemon.verbs()

    def test_warns_instead_of_raising_when_the_listing_fails(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"list"}

        with pytest.warns(UserWarning, match="reaper skipped"):
            runtime.start(runtime.specs["postgres"])


class TestSweepOwn:
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

    def test_runs_when_the_session_finishes(
        self, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        plugin = ac.AppleContainerPlugin(config(), runtime, TESTS_DIR)  # type: ignore[arg-type]
        runtime.start(runtime.specs["postgres"])
        daemon.listing = [entry("mine", OWNER)]

        plugin.pytest_sessionfinish(SimpleNamespace(), 0)  # type: ignore[arg-type]

        assert daemon.deleted == ["mine"]


class TestActivate:
    def test_changes_nothing_off_darwin(
        self, pristine: ModuleType, daemon: FakeDaemon
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}

        plugin = ac.activate(
            config(), pristine, "linux", seams=ac.Seams(cli=daemon), tests_dir=TESTS_DIR
        )  # type: ignore[arg-type]

        assert plugin is None
        assert {name: getattr(pristine, name) for name in ac.PATCHED} == before

    def test_replaces_every_start_function_on_darwin(
        self, pristine: ModuleType, runtime: ac.AppleRuntime
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}

        ac.activate(
            config(), pristine, "darwin", seams=runtime.seams, tests_dir=TESTS_DIR
        )  # type: ignore[arg-type]

        assert [n for n in ac.PATCHED if getattr(pristine, n) is before[n]] == []

    def test_runs_no_command_while_activating(
        self, pristine: ModuleType, runtime: ac.AppleRuntime, daemon: FakeDaemon
    ) -> None:
        ac.activate(
            config(), pristine, "darwin", seams=runtime.seams, tests_dir=TESTS_DIR
        )  # type: ignore[arg-type]

        assert daemon.calls == []

    def test_restores_the_originals_when_pytest_cleans_up(
        self, pristine: ModuleType, runtime: ac.AppleRuntime
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}
        pytest_config = config()
        ac.activate(
            pytest_config, pristine, "darwin", seams=runtime.seams, tests_dir=TESTS_DIR
        )  # type: ignore[arg-type]

        [cleanup() for cleanup in pytest_config.cleanups]

        assert {name: getattr(pristine, name) for name in ac.PATCHED} == before

    @pytest.mark.parametrize("name", [*ac.PATCHED, *ac.RELIED_ON], ids=str)
    def test_patches_nothing_when_containers_drifts(
        self, pristine: ModuleType, runtime: ac.AppleRuntime, name: str
    ) -> None:
        delattr(pristine, name)
        before = {n: getattr(pristine, n) for n in ac.PATCHED if hasattr(pristine, n)}

        plugin = ac.activate(
            config(), pristine, "darwin", seams=runtime.seams, tests_dir=TESTS_DIR
        )  # type: ignore[arg-type]

        assert isinstance(plugin, ac.AdapterDriftPlugin)
        assert name in plugin.message
        assert {n: getattr(pristine, n) for n in before} == before

    def test_patches_nothing_when_a_listed_file_is_gone(
        self, pristine: ModuleType, runtime: ac.AppleRuntime, tmp_path: Path
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}

        with pytest.raises(pytest.UsageError, match="lists files that are gone"):
            ac.activate(
                config(), pristine, "darwin", seams=runtime.seams, tests_dir=tmp_path
            )  # type: ignore[arg-type]

        assert {name: getattr(pristine, name) for name in ac.PATCHED} == before


class TestInstall:
    @pytest.mark.parametrize("name", [*ac.PATCHED, *ac.RELIED_ON], ids=str)
    def test_names_what_is_missing_when_containers_drifts(
        self, pristine: ModuleType, runtime: ac.AppleRuntime, name: str
    ) -> None:
        delattr(pristine, name)

        with pytest.raises(ac.AdapterDriftError, match=name):
            ac.install(pristine, runtime)


class TestInstalledStarters:
    @pytest.fixture
    def installed(
        self,
        pristine: ModuleType,
        runtime: ac.AppleRuntime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ModuleType:
        # The lanes that exclude container-backed tests set this, and `_resolve`
        # refuses to start anything under it; these tests start fake services.
        monkeypatch.delenv("PRINTSTASH_TEST_NO_EXTERNAL", raising=False)
        ac.install(pristine, runtime)
        return pristine

    def test_postgres_url_names_the_loopback_address(
        self, installed: ModuleType
    ) -> None:
        assert (
            installed.postgres_url()
            == "postgresql://printstash:printstash@127.0.0.1:20001/printstash"
        )

    def test_postgres_url_starts_the_container_once(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        installed.postgres_url()
        installed.postgres_url()

        assert len(daemon.runs()) == 1

    def test_pgvector_url_starts_the_pgvector_image_once(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        installed.pgvector_url()
        installed.pgvector_url()

        assert [argv[-1] for argv in daemon.runs()] == [ac.PGVECTOR_IMAGE]

    def test_s3_endpoint_names_the_loopback_address(
        self, installed: ModuleType
    ) -> None:
        assert installed.s3_endpoint() == "http://127.0.0.1:20001"

    def test_s3_private_endpoint_names_the_container_address(
        self, installed: ModuleType
    ) -> None:
        assert installed.s3_private_endpoint() == "http://192.168.64.10:8333"

    def test_the_private_endpoint_is_not_a_loopback_address(
        self, installed: ModuleType
    ) -> None:
        host = installed.s3_private_endpoint().split("//")[1].split(":")[0]

        assert not _is_loopback(host)

    def test_the_public_endpoint_is_a_loopback_address(
        self, installed: ModuleType
    ) -> None:
        host = installed.s3_endpoint().split("//")[1].split(":")[0]

        assert _is_loopback(host)

    def test_checks_the_container_address_is_reachable(
        self, installed: ModuleType, network: Network
    ) -> None:
        installed.s3_endpoint()

        assert "http://192.168.64.10:8333/" in network.urls

    def test_stops_naming_the_terminal_when_the_address_is_unreachable(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.http = [
            403,
            urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host")),
        ]

        with pytest.raises(pytest.exit.Exception, match="terminal app"):
            installed.s3_endpoint()

    def test_openssh_endpoint_returns_the_loopback_endpoint(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.tcp_default = SSH_BANNER

        host, port, _known_hosts = installed.openssh_endpoint()

        assert (host, port) == ("127.0.0.1", 20001)

    def test_openssh_endpoint_returns_the_scanned_host_keys(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.tcp_default = SSH_BANNER

        _host, _port, known_hosts = installed.openssh_endpoint()

        assert known_hosts.startswith("# 127.0.0.1:12222 SSH-2.0-OpenSSH")

    def test_scans_with_its_own_timeout_below_the_command_bound(
        self, installed: ModuleType, network: Network, daemon: FakeDaemon
    ) -> None:
        network.tcp_default = SSH_BANNER

        installed.openssh_endpoint()

        assert (
            ("ssh-keyscan", "-T", "2", "-p", "20001", "127.0.0.1"),
            5,
        ) in daemon.calls

    @pytest.mark.parametrize(
        "first",
        [
            pytest.param(reply(0, ""), id="empty-reply"),
            pytest.param(ac.CliTimeout(["ssh-keyscan"], 5), id="timeout"),
            pytest.param(
                ac.RuntimeUnavailable("ssh-keyscan is not installed"), id="missing"
            ),
        ],
    )
    def test_retries_a_failed_host_key_scan(
        self,
        installed: ModuleType,
        network: Network,
        daemon: FakeDaemon,
        clock: Clock,
        first: Any,
    ) -> None:
        network.tcp_default = SSH_BANNER
        daemon.keyscans = [first, capture("keyscan")]

        installed.openssh_endpoint()

        assert clock.slept.count(0.25) == 1

    def test_gives_up_on_host_keys_after_the_set_attempts(
        self, installed: ModuleType, network: Network, daemon: FakeDaemon
    ) -> None:
        network.tcp_default = SSH_BANNER
        daemon.keyscans = [reply(0, "")] * ac.KEYSCAN_ATTEMPTS

        with pytest.raises(
            RuntimeError, match=f"failed after {ac.KEYSCAN_ATTEMPTS} attempts"
        ):
            installed.openssh_endpoint()

        assert (
            len([argv for argv, _ in daemon.calls if argv[0] == "ssh-keyscan"])
            == ac.KEYSCAN_ATTEMPTS
        )

    def test_stops_naming_the_start_command_when_apple_is_stopped(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        daemon.status = capture("system_status_stopped")

        with pytest.raises(
            pytest.exit.Exception,
            match="cannot start PostgreSQL.*container system start",
        ):
            installed.require_docker("PostgreSQL")

    def test_stops_naming_the_wedge_when_a_command_hangs(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        daemon.timeouts = {"system status"}

        with pytest.raises(pytest.exit.Exception, match="stealth mode.*then reboot"):
            installed.require_docker("PostgreSQL")

    def test_stops_naming_local_network_from_a_service_start(
        self, installed: ModuleType, network: Network
    ) -> None:
        network.tcp = [ConnectionResetError(54, "reset")] * 3

        with pytest.raises(pytest.exit.Exception, match="Local Network"):
            installed.postgres_url()

    def test_reports_docker_as_available_when_the_runtime_is_up(
        self, installed: ModuleType
    ) -> None:
        assert installed.docker_available() is True

    def test_nextcloud_fails_naming_how_to_classify_the_test(
        self, installed: ModuleType, daemon: FakeDaemon
    ) -> None:
        with pytest.raises(pytest.fail.Exception, match="NEXTCLOUD_IDS"):
            installed.nextcloud_endpoint()

        assert daemon.runs() == []


class TestCaptures:
    @pytest.mark.parametrize(
        "path",
        sorted(p for p in CAPTURES.rglob("*") if p.is_file()),
        ids=lambda p: p.relative_to(CAPTURES).as_posix(),
    )
    def test_hold_only_placeholder_identities(self, path: Path) -> None:
        text = path.read_text()

        homes = set(re.findall(r"/Users/([^/\s\"]+)", text))
        macs = set(re.findall(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", text))
        addresses = {
            ipaddress.ip_address(a)
            for a in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        }
        allowed = [
            ipaddress.ip_network(n)
            for n in ("127.0.0.0/8", "192.168.64.0/24", "0.0.0.0/32")
        ]
        assert homes <= {"example"}
        assert macs <= {"02:00:00:00:00:10"}
        assert [a for a in addresses if not any(a in net for net in allowed)] == []


def project(root: Path, *, tests: dict[str, str], omit: tuple[str, ...] = ()) -> Path:
    """A throwaway project whose conftest installs the adapter against a recorder.

    Every file the plugin lists exists, empty unless *tests* gives it content, except
    those in *omit*. The recorder answers every command with a failure, so a test that
    asks for a service gets the stopped-apiserver stop.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts =\nmarkers =\n    bgcode: needs docker\n    e2e: tier\n    postgres: resource\n"
    )
    (root / "conftest.py").write_text(
        "import json, subprocess\n"
        "from pathlib import Path\n"
        "from tests import containers\n"
        "from tests.apple_container import Seams, activate, pytest_addoption\n"
        "CALLS = Path(__file__).parent / 'calls.json'\n"
        "def record(argv, timeout):\n"
        "    with CALLS.open('a') as handle:\n"
        "        handle.write(json.dumps(list(argv)) + '\\n')\n"
        "    return subprocess.CompletedProcess(argv, 1, '', 'recorder')\n"
        "def pytest_configure(config):\n"
        "    seams = Seams(cli=record, lock_dir=Path(__file__).parent / 'locks')\n"
        "    activate(config, containers, 'darwin', seams=seams, tests_dir=Path(__file__).parent)\n"
    )
    for name, source in tests.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    for name in (*ac.DOCKER_ONLY_FILES, *ac.NEXTCLOUD_FILES):
        path = root / name
        if name not in omit and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
    return root


PLAIN = "def test_plain():\n    pass\n"
DOCKER_ONLY = {
    "e2e/test_search_image_proxy.py": "def test_proxy():\n    pass\n",
    "e2e/test_search_query_logs.py": "def test_logs():\n    pass\n",
    "test_marked.py": "import pytest\n\n@pytest.mark.bgcode\ndef test_marked():\n    pass\n",
}
DOCKER_ONLY_IDS = (
    "test_search_image_proxy.py::test_proxy",
    "test_search_query_logs.py::test_logs",
    "test_marked.py::test_marked",
)
NEEDS_A_SERVICE = (
    "import pytest\nfrom tests import containers\n\n"
    "@pytest.mark.parametrize('n', [0, 1])\n"
    "def test_needs_pg(n):\n    containers.postgres_url()\n"
)


def pytest_in(
    root: Path, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    environment.pop("PRINTSTASH_TEST_NO_EXTERNAL", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *args],
        cwd=cwd or root,
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


class TestPluginStop:
    def test_reports_a_stop_during_a_test(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_service.py": NEEDS_A_SERVICE})

        result = pytest_in(root)

        assert (
            "cannot start PostgreSQL: the container apiserver is not running"
            in result.stdout + result.stderr
        )

    def test_reports_a_stop_during_a_test_through_xdist(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_service.py": NEEDS_A_SERVICE})

        result = pytest_in(root, "-p", "xdist.plugin", "-n", "2")

        output = result.stdout + result.stderr
        assert "the container apiserver is not running" in output, output
        assert "INTERNALERROR" not in output


class TestDockerOnlyTests:
    def test_a_run_that_selects_one_stops_naming_every_one(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root)

        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert [name for name in DOCKER_ONLY_IDS if name not in output] == [], output
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

        assert [name for name in DOCKER_ONLY_IDS if name not in result.stdout] == [], (
            result.stdout
        )

    def test_a_run_that_deselects_them_first_does_not_stop(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, "-m", "not bgcode", "-k", "plain")

        assert result.returncode == 0, result.stdout + result.stderr

    def test_collect_only_lists_them_without_stopping(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={"test_plain.py": PLAIN, **DOCKER_ONLY})

        result = pytest_in(root, "--collect-only")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "test_marked.py::test_marked" in result.stdout

    def test_a_collection_error_is_reported_instead_of_the_stop(
        self, tmp_path: Path
    ) -> None:
        root = project(
            tmp_path,
            tests={"test_broken.py": "raise RuntimeError('broken')\n", **DOCKER_ONLY},
        )

        result = pytest_in(root)

        assert "1 error during collection" in result.stdout, result.stdout
        assert "need a Docker Engine API" not in result.stdout + result.stderr

    def test_classifies_a_test_reached_through_a_symlinked_path(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path / "real", tests={"test_plain.py": PLAIN, **DOCKER_ONLY})
        link = tmp_path / "link"
        link.symlink_to(root)

        result = pytest_in(
            root, str(link / "e2e" / "test_search_image_proxy.py"), ac.DESELECT_OPTION
        )

        assert "1 deselected" in result.stdout, result.stdout + result.stderr

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
        assert [name for name in DOCKER_ONLY_IDS if name not in result.stdout] == [], (
            result.stdout
        )

    def test_a_listed_file_that_is_gone_is_a_usage_error(self, tmp_path: Path) -> None:
        files = {k: v for k, v in DOCKER_ONLY.items() if "image_proxy" not in k}
        root = project(
            tmp_path,
            tests={"test_plain.py": PLAIN, **files},
            omit=("e2e/test_search_image_proxy.py",),
        )

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


class TestAdapterDriftPlugin:
    @pytest.fixture
    def drifted(self, tmp_path: Path) -> Path:
        root = project(tmp_path, tests={"test_plain.py": PLAIN})
        (root / "conftest.py").write_text(
            (root / "conftest.py")
            .read_text()
            .replace(
                "def pytest_configure(config):\n",
                "def pytest_configure(config):\n    del containers.SEAWEEDFS_READY_LOG\n",
            )
        )
        return root

    def test_lets_a_run_without_resource_tests_go_on(self, drifted: Path) -> None:
        result = pytest_in(drifted)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_stops_a_run_that_selects_a_resource_test(self, drifted: Path) -> None:
        (drifted / "test_db.py").write_text(
            "import pytest\n\n@pytest.mark.postgres\ndef test_db():\n    pass\n"
        )

        result = pytest_in(drifted)

        assert result.returncode == 1
        assert (
            "no longer defines ['SEAWEEDFS_READY_LOG']" in result.stdout + result.stderr
        )


NEXTCLOUD_TESTS = {
    "contract/modules/storage/storage_opendal/test_real_provider.py": (
        "import pytest\n\n"
        "def test_nextcloud_preserves_the_original():\n    pass\n\n"
        "@pytest.mark.parametrize('provider', ['nextcloud', 'sftp'])\n"
        "def test_materializable(provider):\n    pass\n"
    ),
    "contract/modules/storage/storage_opendal/test_webdav_cleanup.py": (
        "import pytest\n\n"
        "@pytest.fixture(params=['nextcloud', 'wsgidav'])\n"
        "def endpoint(request):\n    return request.param\n\n"
        "def test_cleanup(endpoint):\n    pass\n"
    ),
    "contract/modules/storage/test_storage_providers.py": (
        "import pytest\n\n"
        "@pytest.mark.parametrize('provider', ['synology_webdav', 'koofr', 'minio', 'hetzner_storage_box'])\n"
        "def test_preserves_bytes(provider):\n    pass\n"
    ),
}
NEXTCLOUD_NAMED = (
    "test_real_provider.py::test_nextcloud_preserves_the_original",
    "test_real_provider.py::test_materializable[nextcloud]",
    "test_webdav_cleanup.py::test_cleanup[nextcloud]",
    "test_storage_providers.py::test_preserves_bytes[synology_webdav]",
    "test_storage_providers.py::test_preserves_bytes[koofr]",
)
NEXTCLOUD_UNNAMED = (
    "test_real_provider.py::test_materializable[sftp]",
    "test_webdav_cleanup.py::test_cleanup[wsgidav]",
    "test_storage_providers.py::test_preserves_bytes[minio]",
    "test_storage_providers.py::test_preserves_bytes[hetzner_storage_box]",
)


class TestNextcloudTests:
    def test_a_run_that_selects_one_stops_naming_it(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests=NEXTCLOUD_TESTS)

        result = pytest_in(root)

        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        assert [name for name in NEXTCLOUD_NAMED if name not in output] == [], output

    def test_the_stop_leaves_out_the_tests_that_do_not_need_it(
        self, tmp_path: Path
    ) -> None:
        root = project(tmp_path, tests=NEXTCLOUD_TESTS)

        result = pytest_in(root)

        output = result.stdout + result.stderr
        assert [name for name in NEXTCLOUD_UNNAMED if name in output] == [], output

    def test_the_stop_says_why(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests=NEXTCLOUD_TESTS)

        result = pytest_in(root)

        assert "(needs Nextcloud)" in result.stdout + result.stderr

    def test_the_opt_in_flag_leaves_them_out(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests=NEXTCLOUD_TESTS)

        result = pytest_in(root, ac.DESELECT_OPTION)

        assert "4 passed, 5 deselected" in result.stdout, result.stdout

    def test_the_summary_names_each_one_left_out(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests=NEXTCLOUD_TESTS)

        result = pytest_in(root, ac.DESELECT_OPTION)

        assert [n for n in NEXTCLOUD_NAMED if n not in result.stdout] == [], (
            result.stdout
        )

    def test_a_nextcloud_name_in_another_file_runs_normally(
        self, tmp_path: Path
    ) -> None:
        root = project(
            tmp_path,
            tests={"test_config.py": "def test_nextcloud_config_parses():\n    pass\n"},
        )

        result = pytest_in(root)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_listed_file_that_is_gone_is_a_usage_error(self, tmp_path: Path) -> None:
        root = project(tmp_path, tests={}, omit=(ac.NEXTCLOUD_FILES[0],))

        result = pytest_in(root)

        assert result.returncode == 4, result.stdout + result.stderr
        assert ac.NEXTCLOUD_FILES[0] in result.stderr


REACHES_NEXTCLOUD = (
    "nextcloud_endpoint",
    "real_preset_configuration",
    "cleanup_endpoint",
)
# Files that mention what reaches Nextcloud and do not themselves need it.
NOT_NEXTCLOUD = {
    "contract/modules/conftest.py": "defines the cleanup_endpoint fixture",
    "contract/modules/similarity/test_processing.py": "uses the seaweedfs preset only",
    "containers.py": "the upstream starter",
    "fixtures/storage_presets.py": "defines real_preset_configuration",
    "repo/test_apple_container.py": "this file",
}


def _fixtures_calling(name: str) -> set[str]:
    found = set()
    for path in TESTS_DIR.rglob("conftest.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(call, ast.Call)
                and getattr(call.func, "id", getattr(call.func, "attr", "")) == name
                for call in ast.walk(node)
            ):
                found.add(node.name)
    return found


class TestNextcloudInventory:
    def test_the_ids_are_nextcloud_plus_every_webdav_preset(self) -> None:
        from app.modules.storage.storage_providers import PRESETS

        webdav = {
            name for name, preset in PRESETS.items() if preset["transport"] == "webdav"
        }

        assert set(ac.NEXTCLOUD_IDS) == {"nextcloud"} | webdav, (
            "a WebDAV preset was added or removed: update NEXTCLOUD_IDS"
        )

    def test_every_file_that_reaches_nextcloud_is_listed_or_explained(self) -> None:
        users = {
            path.relative_to(TESTS_DIR).as_posix()
            for path in TESTS_DIR.rglob("*.py")
            if any(name in path.read_text() for name in REACHES_NEXTCLOUD)
        }

        assert users - set(ac.NEXTCLOUD_FILES) - set(NOT_NEXTCLOUD) == set(), (
            "a test file now reaches Nextcloud: list it in NEXTCLOUD_FILES or explain it in NOT_NEXTCLOUD"
        )

    def test_every_listed_file_really_reaches_nextcloud(self) -> None:
        stale = [
            n
            for n in ac.NEXTCLOUD_FILES
            if not any(w in (TESTS_DIR / n).read_text() for w in REACHES_NEXTCLOUD)
        ]

        assert stale == []

    def test_only_the_known_fixture_reaches_nextcloud_directly(self) -> None:
        assert _fixtures_calling("nextcloud_endpoint") == {"cleanup_endpoint"}, (
            "a new fixture reaches Nextcloud: add its name to REACHES_NEXTCLOUD and its users to NEXTCLOUD_FILES"
        )


def _assigned_constants(module: ModuleType) -> dict[str, object]:
    return {name: value for name, value in vars(module).items() if name.isupper()}


def _start_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _value(node: ast.expr, constants: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    assert isinstance(node, ast.Name), ast.dump(node)
    return constants[node.id]


def _builder_calls(function: ast.FunctionDef) -> set[str]:
    """The testcontainers builder methods, wait strategies and keyword arguments a starter uses."""
    calls = {
        call.func.attr
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and (call.func.attr.startswith("with_") or call.func.attr == "waiting_for")
    }
    strategies = {
        call.func.id
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id.endswith("Strategy")
    }
    kwargs = {
        f"{call.func.id}({keyword.arg})"
        for call in ast.walk(function)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id.endswith("Container")
        for keyword in call.keywords
    }
    return calls | strategies | kwargs


class TestSpecsMatchContainers:
    @pytest.fixture
    def source(self) -> ast.Module:
        return ast.parse((TESTS_DIR / "containers.py").read_text())

    @pytest.mark.parametrize(
        ("service", "function"),
        [
            pytest.param("seaweedfs", "_start_seaweedfs", id="seaweedfs"),
            pytest.param("openssh", "_start_openssh", id="openssh"),
        ],
    )
    def test_environment_matches_what_the_upstream_function_sets(
        self,
        runtime: ac.AppleRuntime,
        source: ast.Module,
        pristine: ModuleType,
        service: str,
        function: str,
    ) -> None:
        constants = _assigned_constants(pristine)

        upstream = {
            (_value(call.args[0], constants), _value(call.args[1], constants))
            for call in ast.walk(_start_function(source, function))
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "with_env"
        }

        assert set(runtime.specs[service].env) == upstream

    def test_postgres_environment_names_the_upstream_credentials(
        self, runtime: ac.AppleRuntime, pristine: ModuleType
    ) -> None:
        assert dict(runtime.specs["postgres"].env) == {
            "POSTGRES_USER": pristine.POSTGRES_USER,
            "POSTGRES_PASSWORD": pristine.POSTGRES_PASSWORD,
            "POSTGRES_DB": pristine.POSTGRES_DB,
        }

    def test_pgvector_image_is_the_literal_upstream_starts(
        self, source: ast.Module
    ) -> None:
        literals = {
            node.value
            for node in ast.walk(_start_function(source, "pgvector_url"))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("pgvector/")
        }

        assert literals == {ac.PGVECTOR_IMAGE}

    def test_pgvector_url_is_the_body_the_adapter_replaced(
        self, source: ast.Module
    ) -> None:
        body = ast.dump(
            _start_function(source, "pgvector_url"), include_attributes=False
        )

        assert hashlib.sha256(body.encode()).hexdigest()[:16] == "f3943fe24e9efec3", (
            "upstream changed pgvector_url; review the adapter's replacement, then update this hash"
        )

    @pytest.mark.parametrize(
        ("service", "function", "port"),
        [
            pytest.param(
                "seaweedfs", "_start_seaweedfs", "SEAWEEDFS_S3_PORT", id="seaweedfs"
            ),
            pytest.param("openssh", "_start_openssh", 2222, id="openssh"),
        ],
    )
    def test_container_port_is_the_one_upstream_exposes(
        self,
        runtime: ac.AppleRuntime,
        source: ast.Module,
        pristine: ModuleType,
        service: str,
        function: str,
        port: object,
    ) -> None:
        constants = _assigned_constants(pristine)
        expected = constants[port] if isinstance(port, str) else port

        exposed = {
            _value(arg, constants)
            for call in ast.walk(_start_function(source, function))
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "with_exposed_ports"
            for arg in call.args
        }

        assert exposed == {expected}
        assert runtime.specs[service].container_port == expected

    def test_every_starter_configures_only_what_the_adapter_copied(
        self, source: ast.Module
    ) -> None:
        assert {
            name: _builder_calls(_start_function(source, name))
            for name in (
                "_start_postgres",
                "_start_seaweedfs",
                "_start_openssh",
                "pgvector_url",
            )
        } == {
            "_start_postgres": {
                "PostgresContainer(username)",
                "PostgresContainer(password)",
                "PostgresContainer(dbname)",
            },
            "_start_seaweedfs": {
                "with_env",
                "with_exposed_ports",
                "with_command",
                "waiting_for",
                "LogMessageWaitStrategy",
                "with_startup_timeout",
            },
            "_start_openssh": {
                "with_env",
                "with_exposed_ports",
                "waiting_for",
                "PortWaitStrategy",
                "with_startup_timeout",
            },
            "pgvector_url": {
                "PostgresContainer(username)",
                "PostgresContainer(password)",
                "PostgresContainer(dbname)",
            },
        }, (
            "upstream configures a container differently now: mirror it in build_specs, then update this list"
        )

    def test_the_starters_are_the_ones_the_adapter_replaces(
        self, source: ast.Module
    ) -> None:
        starters = {
            node.name
            for node in source.body
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id.endswith("Container")
                for call in ast.walk(node)
            )
        }

        assert starters == {
            "_start_postgres",
            "_start_seaweedfs",
            "_start_nextcloud",
            "_start_openssh",
            "pgvector_url",
        }, "upstream added or removed a service: handle it in tests/apple_container.py"

    def test_the_resource_markers_are_the_ones_the_suite_checks(self) -> None:
        assert set(ac.RESOURCE_MARKERS) == set(suite_conftest._RESOURCES)

    def test_every_name_the_adapter_reads_is_checked_for(self) -> None:
        tree = ast.parse((TESTS_DIR / "apple_container.py").read_text())

        read = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "containers"
        }

        assert read - set(ac.PATCHED) - set(ac.RELIED_ON) == set()

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
        }


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


def _require_docker_callers() -> set[str]:
    return {
        str(path.relative_to(TESTS_DIR))
        for path in TESTS_DIR.rglob("test_*.py")
        if path.name != Path(__file__).name
        and any(
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") in ("require_docker", "docker_available")
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }


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
            "a test now shells out to docker: list it in DOCKER_ONLY_FILES or explain it in DOCKER_CLI_NOT_NEEDING_A_DAEMON"
        )

    def test_every_test_gated_on_docker_is_listed(self) -> None:
        assert _require_docker_callers() - set(ac.DOCKER_ONLY_FILES) == set(), (
            "a test now checks for Docker itself: list it in DOCKER_ONLY_FILES"
        )

    def test_the_bgcode_marker_exists(self) -> None:
        markers = (REPO_ROOT / "backend" / "pyproject.toml").read_text()

        assert [m for m in ac.DOCKER_ONLY_MARKERS if f'"{m}:' not in markers] == []


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
        } <= importers


class TestEscapeHatch:
    def test_disabling_the_plugin_removes_its_option(self) -> None:
        environment = {
            k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")
        }

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:tests.apple_container", "--collect-only", "-q",
             ac.DESELECT_OPTION, str(Path(__file__))],
            cwd=REPO_ROOT / "backend",
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )  # fmt: skip

        assert result.returncode == 4, result.stdout + result.stderr
        assert f"unrecognized arguments: {ac.DESELECT_OPTION}" in result.stderr


class TestBackendConftest:
    def test_does_nothing_but_load_the_plugin(self) -> None:
        tree = ast.parse((REPO_ROOT / "backend" / "conftest.py").read_text())

        assert [ast.unparse(node) for node in tree.body] == [
            "pytest_plugins = ('tests.apple_container',)"
        ]
