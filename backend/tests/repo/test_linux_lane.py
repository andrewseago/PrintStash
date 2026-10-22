"""The Linux lane runs the tests macOS cannot, against services the host started.

Mesh workers cap `RLIMIT_AS` and local storage is classified from
`/proc/self/mountinfo`; macOS rejects the first and lacks the second, so the tests
that depend on either run in a Linux container (`scripts/linux_lane.py`). The
container reaches PostgreSQL, SeaweedFS and OpenSSH by the address the host adapter
(`tests/apple_container.py`) started them on, through a JSON file that
`tests/linux_lane_services.py` turns into the start functions of `tests.containers`.

Its promises, each held by a group below: the plugin does nothing without the file;
it refuses a malformed file and services it cannot reach, naming them; it answers
every start function the Apple container adapter replaces, and nothing else; it
leaves out the tests that need Nextcloud; the launcher starts only the services asked
for, builds one exact container command, installs the locked dependencies as Linux
wheels, and removes the test container and the services however it ends.

Checked on a Mac with the runtime running, not here: a real container run (see the
PR notes) and the services' behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tests import apple_container as ac
from tests import linux_lane_services as plugin
from tests.paths import REPO_ROOT, TESTS_DIR

SCRIPT = REPO_ROOT / "backend" / "scripts" / "linux_lane.py"
SERVICES = {
    "postgres": "postgresql://u:p@192.168.64.7:5432/printstash",
    "pgvector": "postgresql://u:p@192.168.64.8:5432/printstash",
    "s3": "http://192.168.64.9:8333",
    "s3_private": "http://192.168.64.9:8333",
    "openssh": {"host": "192.168.64.6", "port": 2222, "known_hosts": "# keys\n"},
}


@pytest.fixture
def pristine() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pristine_containers_lane", TESTS_DIR / "containers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lane() -> ModuleType:
    spec = importlib.util.spec_from_file_location("linux_lane_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def services_file(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "services.json"
    path.write_text(json.dumps(document))
    return path


class TestLoadServices:
    def test_reads_every_known_service(self, tmp_path: Path) -> None:
        loaded = plugin.load_services(services_file(tmp_path, SERVICES))

        assert loaded == SERVICES

    def test_refuses_a_service_it_does_not_know(self, tmp_path: Path) -> None:
        path = services_file(tmp_path, {"redis": "redis://x"})

        with pytest.raises(ValueError, match=r"unknown services: \['redis'\]"):
            plugin.load_services(path)

    def test_refuses_a_file_that_is_not_an_object(self, tmp_path: Path) -> None:
        path = services_file(tmp_path, ["postgres"])

        with pytest.raises(ValueError, match="must hold a JSON object"):
            plugin.load_services(path)

    def test_refuses_an_openssh_entry_with_other_keys(self, tmp_path: Path) -> None:
        path = services_file(tmp_path, {"openssh": {"host": "h", "port": 22}})

        with pytest.raises(ValueError, match="'openssh' must hold exactly"):
            plugin.load_services(path)


class TestEndpoints:
    def test_names_the_host_with_the_port_of_each_url(self) -> None:
        found = list(
            plugin.endpoints({"postgres": SERVICES["postgres"], "s3": SERVICES["s3"]})
        )

        assert found == [("192.168.64.7", 5432), ("192.168.64.9", 8333)]

    def test_names_the_host_with_the_port_of_openssh(self) -> None:
        found = list(plugin.endpoints({"openssh": SERVICES["openssh"]}))

        assert found == [("192.168.64.6", 2222)]

    def test_names_nothing_for_a_file_without_services(self) -> None:
        assert list(plugin.endpoints({})) == []


class TestUnreachable:
    def test_reports_an_endpoint_that_refuses_the_connection(self) -> None:
        def refuse(address: tuple[str, int], timeout: float) -> Any:
            raise ConnectionRefusedError(61, "refused")

        failed = plugin.unreachable({"s3": SERVICES["s3"]}, connect=refuse)

        assert [item.split(" ")[0] for item in failed] == ["192.168.64.9:8333"]

    def test_reports_nothing_when_every_endpoint_answers(self) -> None:
        opened: list[tuple[str, int]] = []

        def accept(address: tuple[str, int], timeout: float) -> Any:
            opened.append(address)
            return SimpleNamespace(close=lambda: None)

        failed = plugin.unreachable(
            {"postgres": SERVICES["postgres"], "openssh": SERVICES["openssh"]},
            connect=accept,
        )

        assert (failed, opened) == (
            [],
            [("192.168.64.7", 5432), ("192.168.64.6", 2222)],
        )

    def test_gives_each_connection_the_timeout(self) -> None:
        seen: list[float] = []

        def accept(address: tuple[str, int], timeout: float) -> Any:
            seen.append(timeout)
            return SimpleNamespace(close=lambda: None)

        plugin.unreachable({"s3": SERVICES["s3"]}, connect=accept)

        assert seen == [plugin.CONNECT_TIMEOUT_S]


class TestInstall:
    @pytest.fixture
    def installed(self, pristine: ModuleType) -> ModuleType:
        plugin.install(pristine, dict(SERVICES))
        return pristine

    def test_replaces_every_function_the_apple_adapter_replaces(
        self, pristine: ModuleType
    ) -> None:
        before = {name: getattr(pristine, name) for name in ac.PATCHED}

        plugin.install(pristine, dict(SERVICES))

        assert [n for n in ac.PATCHED if getattr(pristine, n) is before[n]] == []

    def test_leaves_no_name_the_adapter_relies_on_undefined(
        self, pristine: ModuleType
    ) -> None:
        assert ac.missing_names(pristine) == []

    def test_starts_postgres_at_the_address_in_the_file(
        self, installed: ModuleType
    ) -> None:
        assert installed._start_postgres() == SERVICES["postgres"]

    def test_starts_s3_at_the_address_in_the_file(self, installed: ModuleType) -> None:
        assert installed._start_seaweedfs() == SERVICES["s3"]

    def test_records_the_private_s3_endpoint_when_s3_starts(
        self, installed: ModuleType
    ) -> None:
        installed._start_seaweedfs()

        assert installed._resolved["s3_private"] == SERVICES["s3_private"]

    def test_starts_openssh_at_the_address_in_the_file(
        self, installed: ModuleType
    ) -> None:
        assert installed._start_openssh() == ("192.168.64.6", 2222, "# keys\n")

    def test_resolves_pgvector_through_the_upstream_cache(
        self, installed: ModuleType
    ) -> None:
        installed.pgvector_url()
        installed.pgvector_url()

        assert installed._resolved["pgvector"] == SERVICES["pgvector"]

    def test_reports_docker_as_available(self, installed: ModuleType) -> None:
        assert installed.docker_available() is True

    def test_asks_nothing_of_a_daemon_for_a_resource(
        self, installed: ModuleType
    ) -> None:
        assert installed.require_docker("PostgreSQL") is None

    def test_fails_a_test_that_reaches_nextcloud_naming_how_to_classify_it(
        self, installed: ModuleType
    ) -> None:
        with pytest.raises(pytest.fail.Exception, match="NEXTCLOUD_IDS"):
            installed._start_nextcloud()

    def test_names_the_missing_service_with_the_option_that_adds_it(
        self, pristine: ModuleType
    ) -> None:
        plugin.install(pristine, {"postgres": SERVICES["postgres"]})

        with pytest.raises(LookupError, match=r"'s3_private'.*--services"):
            pristine._start_seaweedfs()


def pytest_in(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    environment.pop("PRINTSTASH_TEST_NO_EXTERNAL", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:tests.apple_container",
         "-p", "tests.linux_lane_services", "-q", *args],
        cwd=REPO_ROOT / "backend",
        env={**environment, **env},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )  # fmt: skip


@pytest.fixture
def listening() -> Any:
    """Local sockets that accept connections, so a services file can point at them."""
    sockets: list[socket.socket] = []

    def listen() -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        sockets.append(server)
        return server.getsockname()[1]

    yield listen
    for server in sockets:
        server.close()


COLLECTED = "tests/e2e/test_remote_backup.py"


class TestPluginInPytest:
    def test_does_nothing_without_the_services_file(self) -> None:
        result = pytest_in("--collect-only", COLLECTED, env={})

        assert "[koofr]" in result.stdout, result.stdout + result.stderr

    def test_stops_naming_a_service_it_cannot_reach(self, tmp_path: Path) -> None:
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        path = services_file(tmp_path, {"s3": f"http://127.0.0.1:{port}"})

        result = pytest_in(
            "--collect-only", COLLECTED, env={plugin.SERVICES_FILE_ENV: str(path)}
        )

        assert result.returncode == 1
        assert f"127.0.0.1:{port}" in result.stdout + result.stderr

    def test_stops_naming_a_file_with_an_unknown_service(self, tmp_path: Path) -> None:
        path = services_file(tmp_path, {"redis": "redis://x"})

        result = pytest_in(
            "--collect-only", COLLECTED, env={plugin.SERVICES_FILE_ENV: str(path)}
        )

        assert "unknown services: ['redis']" in result.stdout + result.stderr

    def test_leaves_out_the_tests_that_need_nextcloud(
        self, tmp_path: Path, listening: Any
    ) -> None:
        path = services_file(tmp_path, {"s3": f"http://127.0.0.1:{listening()}"})

        result = pytest_in(
            "--collect-only", "-m", "s3 or remote_storage", COLLECTED,
            env={plugin.SERVICES_FILE_ENV: str(path)},
        )  # fmt: skip

        assert [
            n
            for n in ("nextcloud", "koofr", "synology_webdav")
            if f"[{n}]" in result.stdout
        ] == []

    def test_keeps_the_tests_that_do_not_need_nextcloud(
        self, tmp_path: Path, listening: Any
    ) -> None:
        path = services_file(tmp_path, {"s3": f"http://127.0.0.1:{listening()}"})

        result = pytest_in(
            "--collect-only", "-m", "s3 or remote_storage", COLLECTED,
            env={plugin.SERVICES_FILE_ENV: str(path)},
        )  # fmt: skip

        assert "[s3]" in result.stdout, result.stdout + result.stderr

    def test_reports_how_many_it_left_out(self, tmp_path: Path, listening: Any) -> None:
        path = services_file(tmp_path, {"s3": f"http://127.0.0.1:{listening()}"})

        result = pytest_in(
            "--collect-only", "-m", "s3 or remote_storage", COLLECTED,
            env={plugin.SERVICES_FILE_ENV: str(path)},
        )  # fmt: skip

        assert "deselected" in result.stdout


class TestConfigure:
    @pytest.fixture
    def configured(
        self,
        pristine: ModuleType,
        tmp_path: Path,
        listening: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> SimpleNamespace:
        import tests

        path = services_file(tmp_path, {"s3": f"http://127.0.0.1:{listening()}"})
        monkeypatch.setenv(plugin.SERVICES_FILE_ENV, str(path))
        monkeypatch.setattr(tests, "containers", pristine)
        registered: list[object] = []
        cleanups: list[Any] = []
        config = SimpleNamespace(
            pluginmanager=SimpleNamespace(
                register=lambda hooks, name: registered.append(hooks),
                unregister=lambda hooks: registered.remove(hooks),
            ),
            add_cleanup=cleanups.append,
        )
        before = {name: getattr(pristine, name) for name in ac.PATCHED}
        plugin.pytest_configure(config)  # type: ignore[arg-type]
        return SimpleNamespace(
            pristine=pristine, before=before, registered=registered, cleanups=cleanups
        )

    def test_replaces_the_start_functions_when_the_file_is_set(
        self, configured: SimpleNamespace
    ) -> None:
        replaced = [
            n
            for n in ac.PATCHED
            if getattr(configured.pristine, n) is configured.before[n]
        ]

        assert replaced == []

    def test_registers_the_hooks_that_leave_out_nextcloud(
        self, configured: SimpleNamespace
    ) -> None:
        assert [type(h) for h in configured.registered] == [plugin.LinuxLanePlugin]

    def test_restores_the_start_functions_when_pytest_cleans_up(
        self, configured: SimpleNamespace
    ) -> None:
        [cleanup() for cleanup in configured.cleanups]

        restored = {n: getattr(configured.pristine, n) for n in ac.PATCHED}
        assert restored == configured.before

    def test_unregisters_the_hooks_when_pytest_cleans_up(
        self, configured: SimpleNamespace
    ) -> None:
        [cleanup() for cleanup in configured.cleanups]

        assert configured.registered == []


class TestParseServices:
    def test_reads_a_comma_separated_list(self, lane: ModuleType) -> None:
        assert lane.parse_services("postgres,s3") == ("postgres", "s3")

    def test_drops_a_repeated_name(self, lane: ModuleType) -> None:
        assert lane.parse_services("s3,postgres,s3") == ("s3", "postgres")

    @pytest.mark.parametrize("value", ["redis", "postgres,redis", ""], ids=str)
    def test_refuses_an_unknown_or_empty_list(
        self, lane: ModuleType, value: str
    ) -> None:
        with pytest.raises(Exception, match="--services takes some of"):
            lane.parse_services(value)


class TestDepsDirectory:
    def test_is_the_same_for_the_same_lock(
        self, lane: ModuleType, tmp_path: Path
    ) -> None:
        assert lane.deps_directory(b"lock", tmp_path) == lane.deps_directory(
            b"lock", tmp_path
        )

    def test_differs_for_another_lock(self, lane: ModuleType, tmp_path: Path) -> None:
        assert lane.deps_directory(b"one", tmp_path) != lane.deps_directory(
            b"two", tmp_path
        )

    def test_lives_under_the_cache_directory(
        self, lane: ModuleType, tmp_path: Path
    ) -> None:
        assert lane.deps_directory(b"lock", tmp_path).parent == tmp_path


class TestInstallDeps:
    def test_exports_the_lock_then_installs_it_as_linux_wheels(
        self, lane: ModuleType, tmp_path: Path
    ) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []
        target = tmp_path / "cache" / "deps-abc"

        lane.install_deps(
            tmp_path, target, run=lambda argv, **kw: calls.append((argv, kw))
        )

        requirements = str(tmp_path / "cache" / "deps-abc.txt")
        assert [argv for argv, _ in calls] == [
            [
                "uv", "export", "--frozen", "--extra", "dev", "--extra", "full",
                "--no-emit-project", "--no-emit-workspace", "--no-hashes",
                "-o", requirements,
            ],
            [
                "uv", "pip", "install", "--python-platform", "aarch64-manylinux_2_31",
                "--python-version", "3.14", "--only-binary", ":all:",
                "--target", str(target), "-r", requirements,
            ],
        ]  # fmt: skip

    def test_requires_each_command_to_succeed(
        self, lane: ModuleType, tmp_path: Path
    ) -> None:
        seen: list[dict[str, Any]] = []

        lane.install_deps(
            tmp_path, tmp_path / "d", run=lambda argv, **kw: seen.append(kw)
        )

        assert [kw["check"] for kw in seen] == [True, True]


class TestLaneCommand:
    @pytest.fixture
    def command(self, lane: ModuleType, tmp_path: Path) -> list[str]:
        return lane.lane_command(
            name="printstash-linux-lane-7",
            backend=tmp_path / "backend",
            deps=tmp_path / "deps",
            lane=tmp_path / "lane",
            cpus="8",
            memory="8G",
            pytest_args=["-m", "postgres", "tests/e2e/test_x.py", "-q"],
        )

    def test_runs_a_named_container_that_removes_itself(
        self, command: list[str]
    ) -> None:
        assert command[:5] == [
            "container",
            "run",
            "--rm",
            "--name",
            "printstash-linux-lane-7",
        ]

    def test_sizes_the_container_from_cpus_then_memory(
        self, command: list[str]
    ) -> None:
        assert (command[command.index("-c") + 1], command[command.index("-m") + 1]) == (
            "8",
            "8G",
        )

    def test_mounts_the_source_dependencies_then_services_read_only(
        self, command: list[str], tmp_path: Path
    ) -> None:
        mounts = [command[i + 1] for i, v in enumerate(command) if v == "--mount"]

        assert mounts == [
            f"type=bind,source={tmp_path / 'backend'},target=/src,readonly",
            f"type=bind,source={tmp_path / 'deps'},target=/deps,readonly",
            f"type=bind,source={tmp_path / 'lane'},target=/lane,readonly",
        ]

    def test_points_the_plugin_at_the_mounted_services_file(
        self, command: list[str]
    ) -> None:
        assert "PRINTSTASH_TEST_SERVICES_FILE=/lane/services.json" in command

    def test_uses_the_pinned_python_image(
        self, lane: ModuleType, command: list[str]
    ) -> None:
        assert lane.IMAGE in command

    def test_hands_every_pytest_argument_through_unchanged(
        self, command: list[str]
    ) -> None:
        assert command[-5:] == ["pytest", "-m", "postgres", "tests/e2e/test_x.py", "-q"]

    def test_loads_the_services_plugin_in_the_container(self, lane: ModuleType) -> None:
        assert "-p tests.linux_lane_services" in lane.IN_CONTAINER

    def test_copies_the_source_without_the_host_environment(
        self, lane: ModuleType
    ) -> None:
        assert (
            "--exclude=./.venv" in lane.IN_CONTAINER
            and "--exclude=./.git" in lane.IN_CONTAINER
        )

    def test_puts_the_dependencies_on_every_python_path(self, lane: ModuleType) -> None:
        assert all(line in lane.IN_CONTAINER for line in lane.PTH_LINES)


class FakeRuntime:
    def __init__(self) -> None:
        self.specs = {
            k: SimpleNamespace(key=k)
            for k in ("postgres", "pgvector", "seaweedfs", "openssh")
        }
        self.started: list[str] = []
        self.swept = False

    def start(self, spec: Any) -> Any:
        self.started.append(spec.key)
        return ac.StartedService(f"printstash-test-{spec.key}-1", 20001)

    def container_address(self, name: str) -> str:
        return {"postgres": "192.168.64.7", "pgvector": "192.168.64.8",
                "seaweedfs": "192.168.64.9", "openssh": "192.168.64.6"}[name.split("-")[2]]  # fmt: skip

    def check_available(self) -> None:
        return None

    def sweep_own(self) -> None:
        self.swept = True


class TestServicesDocument:
    def test_starts_only_the_services_asked_for(self, lane: ModuleType) -> None:
        runtime = FakeRuntime()

        lane.services_document(runtime, ("postgres",), lambda address: "")

        assert runtime.started == ["postgres"]

    def test_describes_postgres_by_container_address(self, lane: ModuleType) -> None:
        document = lane.services_document(FakeRuntime(), ("postgres",), lambda a: "")

        assert document == {"postgres": SERVICES["postgres"].replace("u:p", f"{lane.containers.POSTGRES_USER}:{lane.containers.POSTGRES_PASSWORD}")}  # fmt: skip

    def test_describes_pgvector_by_container_address(self, lane: ModuleType) -> None:
        document = lane.services_document(FakeRuntime(), ("pgvector",), lambda a: "")

        assert "@192.168.64.8:5432/" in str(document["pgvector"])

    def test_describes_both_s3_endpoints_by_container_address(
        self, lane: ModuleType
    ) -> None:
        document = lane.services_document(FakeRuntime(), ("s3",), lambda a: "")

        assert document == {
            "s3": "http://192.168.64.9:8333",
            "s3_private": "http://192.168.64.9:8333",
        }

    def test_describes_openssh_with_the_host_keys_scanned_at_its_address(
        self, lane: ModuleType
    ) -> None:
        scanned: list[str] = []

        def keyscan(address: str) -> str:
            scanned.append(address)
            return "# keys\n"

        document = lane.services_document(FakeRuntime(), ("sftp",), keyscan)

        assert (document, scanned) == (
            {
                "openssh": {
                    "host": "192.168.64.6",
                    "port": 2222,
                    "known_hosts": "# keys\n",
                }
            },
            ["192.168.64.6"],
        )

    def test_writes_a_document_the_plugin_accepts(
        self, lane: ModuleType, tmp_path: Path
    ) -> None:
        document = lane.services_document(
            FakeRuntime(), ("postgres", "pgvector", "s3", "sftp"), lambda a: "# keys\n"
        )

        assert plugin.load_services(services_file(tmp_path, document)) == document


class TestHostKeyscan:
    def test_scans_the_ssh_port_with_its_own_timeout(
        self, lane: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []

        def run(argv: list[str], **kwargs: Any) -> Any:
            seen.append(argv)
            return SimpleNamespace(stdout="# keys\n")

        monkeypatch.setattr(lane.subprocess, "run", run)

        lane.host_keyscan("192.168.64.6")

        assert seen == [["ssh-keyscan", "-T", "5", "-p", "2222", "192.168.64.6"]]

    def test_refuses_an_empty_answer(
        self, lane: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            lane.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=" \n")
        )

        with pytest.raises(RuntimeError, match="returned no keys"):
            lane.host_keyscan("192.168.64.6")


class TestRemoveContainer:
    def test_force_deletes_the_named_container_within_a_bound(
        self, lane: ModuleType
    ) -> None:
        seen: list[tuple[list[str], dict[str, Any]]] = []

        lane.remove_container(
            "printstash-linux-lane-7", run=lambda argv, **kw: seen.append((argv, kw))
        )

        assert seen[0][0] == [
            "container",
            "delete",
            "--force",
            "printstash-linux-lane-7",
        ]
        assert seen[0][1]["timeout"] == lane.DELETE_TIMEOUT_S

    def test_does_not_raise_when_there_is_nothing_to_delete(
        self, lane: ModuleType
    ) -> None:
        seen: list[dict[str, Any]] = []

        lane.remove_container("x", run=lambda argv, **kw: seen.append(kw))

        assert seen[0]["check"] is False


class TestTerminate:
    def test_turns_sigterm_into_a_clean_exit(self, lane: ModuleType) -> None:
        with pytest.raises(SystemExit) as raised:
            lane._terminate(signal.SIGTERM, None)

        assert raised.value.code == 128 + signal.SIGTERM


class TestMain:
    @pytest.fixture
    def wired(
        self, lane: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> SimpleNamespace:
        runtime = FakeRuntime()
        removed: list[str] = []
        shut: list[bool] = []
        deps = tmp_path / "deps"
        deps.mkdir()
        monkeypatch.setattr(lane.ac, "AppleRuntime", lambda containers: runtime)
        monkeypatch.setattr(lane, "deps_directory", lambda lock: deps)
        monkeypatch.setattr(lane, "remove_container", lambda name: removed.append(name))
        monkeypatch.setattr(
            lane.containers, "shutdown_containers", lambda: shut.append(True)
        )
        monkeypatch.setattr(lane, "host_keyscan", lambda address: "# keys\n")
        monkeypatch.setattr(lane.signal, "signal", lambda *a: None)
        return SimpleNamespace(runtime=runtime, removed=removed, shut=shut, lane=lane)

    def test_refuses_to_run_without_pytest_arguments(
        self, wired: SimpleNamespace
    ) -> None:
        with pytest.raises(SystemExit) as raised:
            wired.lane.main(["--services", "postgres"])

        assert raised.value.code == 2

    def test_returns_the_exit_status_of_the_test_container(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wired.lane.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=5),
        )

        assert wired.lane.main(["--services", "postgres", "--", "tests/x.py"]) == 5

    def test_removes_the_test_container_with_the_services_after_a_run(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wired.lane.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=0),
        )

        wired.lane.main(["--services", "postgres", "--", "tests/x.py"])

        assert (len(wired.removed), wired.shut, wired.runtime.swept) == (
            1,
            [True],
            True,
        )

    def test_removes_the_test_container_with_the_services_when_the_run_is_interrupted(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(argv: list[str], **kw: Any) -> Any:
            raise KeyboardInterrupt

        monkeypatch.setattr(wired.lane.subprocess, "run", interrupted)

        with pytest.raises(KeyboardInterrupt):
            wired.lane.main(["--services", "postgres", "--", "tests/x.py"])

        assert (len(wired.removed), wired.shut, wired.runtime.swept) == (
            1,
            [True],
            True,
        )

    def test_removes_the_services_when_one_fails_to_start(
        self, wired: SimpleNamespace
    ) -> None:
        def broken(spec: Any) -> Any:
            raise ac.StartFailure("no")

        wired.runtime.start = broken

        with pytest.raises(ac.StartFailure):
            wired.lane.main(["--services", "postgres", "--", "tests/x.py"])

        assert (wired.shut, wired.runtime.swept) == ([True], True)

    def test_installs_dependencies_only_when_they_are_missing(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        installed: list[Path] = []
        missing = tmp_path / "missing"
        monkeypatch.setattr(wired.lane, "deps_directory", lambda lock: missing)
        monkeypatch.setattr(
            wired.lane, "install_deps", lambda backend, target: installed.append(target)
        )
        monkeypatch.setattr(
            wired.lane.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=0),
        )

        wired.lane.main(["--services", "postgres", "--", "tests/x.py"])

        assert installed == [missing]

    def test_keeps_dependencies_that_are_already_installed(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed: list[Path] = []
        monkeypatch.setattr(
            wired.lane, "install_deps", lambda backend, target: installed.append(target)
        )
        monkeypatch.setattr(
            wired.lane.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=0),
        )

        wired.lane.main(["--services", "postgres", "--", "tests/x.py"])

        assert installed == []

    def test_reinstalls_dependencies_when_asked(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed: list[Path] = []
        monkeypatch.setattr(
            wired.lane, "install_deps", lambda backend, target: installed.append(target)
        )
        monkeypatch.setattr(
            wired.lane.subprocess,
            "run",
            lambda argv, **kw: SimpleNamespace(returncode=0),
        )

        wired.lane.main(
            ["--services", "postgres", "--rebuild-deps", "--", "tests/x.py"]
        )

        assert len(installed) == 1

    def test_writes_the_services_file_the_container_reads(
        self, wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, object]] = []

        def run(argv: list[str], **kw: Any) -> Any:
            source = (
                next(a for a in argv if a.endswith(",target=/lane,readonly"))
                .split("source=")[1]
                .split(",")[0]
            )
            seen.append(json.loads(Path(source, "services.json").read_text()))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(wired.lane.subprocess, "run", run)

        wired.lane.main(["--services", "s3", "--", "tests/x.py"])

        assert seen == [
            {"s3": "http://192.168.64.9:8333", "s3_private": "http://192.168.64.9:8333"}
        ]
