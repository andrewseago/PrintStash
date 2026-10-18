"""`printstash.sh` runs the default stack on Apple `container` and keeps its promises.

`deploy/apple-container/printstash.sh` starts the single-container stack from
`docker-compose.yml` on Apple's `container`. Its promises: `up` changes nothing that
already matches and replaces a container only with `--recreate`, pulling and checking
the new image first so a failure leaves the old one running; a rebuilt or re-pulled
image counts as a change; `down` keeps the data volume; nothing it did not create is
ever stopped or deleted; every `container` call is bounded, and a hang is named as
apple/container#2275; health is polled on the published port, bounded in wall time;
a bad argument is refused before any command runs; `build` stops at the first image
that fails and never substitutes another.

The script runs for real, under bash, from a copy laid out like the repository, with
`tests/fakes/apple_container_cli.py` standing in for `container`, `curl`, `nc` and
`timeout` on PATH. Those fakes accept only the options the real tools take and render
`container inspect` from output recorded on a Mac, so an invocation the real CLI
would refuse fails here too. The parity groups read `docker-compose.yml`,
`docker-bake.hcl` and `backend/unified/Dockerfile` from the real repository, so the
script cannot drift from the stack it stands in for.

The real runtime end to end (a healthy `up`, a restart that keeps data, a build into
an 8 CPU / 16 GiB builder) is verified on a Mac with it running, not here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from tests.paths import FIXTURES_DIR, REPO_ROOT, TESTS_DIR

SCRIPT = REPO_ROOT / "deploy" / "apple-container" / "printstash.sh"
CAPTURES = FIXTURES_DIR / "apple_container" / "app"
FAKE = TESTS_DIR / "fakes" / "apple_container_cli.py"
BASH = "/bin/bash" if sys.platform == "darwin" else (shutil.which("bash") or "bash")

DEFAULT_IMAGE = "ghcr.io/xiao-villamor/printstash:latest"
DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64
SPEC = {
    "managed": "1",
    "image": DEFAULT_IMAGE,
    "digest": DIGEST,
    "port": "3000",
    "memory": "4g",
    "volume": "printstash",
    "env": "none",
}


class Stack:
    """One run of the script, from a copy of the repository layout, on a fake machine."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.dir = root / "fake"
        self.bin = root / "bin"
        script = self.repo / "deploy" / "apple-container" / "printstash.sh"
        script.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, script)
        self.script = script
        for directory in ("backend/unified", "frontend"):
            (self.repo / directory).mkdir(parents=True)
        (self.repo / "backend" / "unified" / "Dockerfile").write_text(
            "FROM api-image\n"
        )
        self.dir.mkdir()
        self.bin.mkdir()
        for name in ("inspect_running.txt", "inspect_stopped.txt", "logs.txt"):
            shutil.copy(CAPTURES / name, self.dir / name)
        for tool in ("container", "curl", "nc", "timeout"):
            wrapper = self.bin / tool
            wrapper.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE}" {tool} "$@"\n'
            )
            wrapper.chmod(0o755)
        self.put_json("images.json", {DEFAULT_IMAGE: DIGEST})
        self.put_json("volumes.json", ["printstash"])
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(root),
            "TMPDIR": str(root),
            "FAKE_DIR": str(self.dir),
            "PRINTSTASH_TIMEOUT_BIN": str(self.bin / "timeout"),
            "PRINTSTASH_POLL_SECONDS": "0",
            "PRINTSTASH_HEALTH_TIMEOUT": "3",
            "PRINTSTASH_BUILD_LOCK": str(root / "build.lock"),
        }

    def put(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text)

    def put_json(self, name: str, value: object) -> None:
        (self.dir / name).write_text(json.dumps(value))

    def get_json(self, name: str) -> object:
        return json.loads((self.dir / name).read_text())

    def container(self, state: str, name: str = "printstash", **labels: str) -> None:
        self.put_json(
            "containers.json",
            {
                name: {
                    "state": state,
                    "labels": {
                        f"printstash.{key}": value
                        for key, value in {**SPEC, **labels}.items()
                    },
                }
            },
        )

    def run(
        self, *args: str, script: Path | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(script or self.script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self, tool: str = "container") -> list[list[str]]:
        path = self.dir / "calls.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [entry[1:] for entry in map(json.loads, lines) if entry[0] == tool]

    def verbs(self) -> list[str]:
        return [
            " ".join(call[:2]) if call[0] in ("system", "image", "volume") else call[0]
            for call in self.calls()
        ]

    def call(self, verb: str) -> list[str]:
        """The first call whose leading words are *verb* (`"run"`, `"volume create"`)."""
        words = verb.split()
        return next(call for call in self.calls() if call[: len(words)] == words)

    def builds(self) -> list[list[str]]:
        return [call for call in self.calls() if call[0] == "build"]

    def tag_of(self, build: list[str]) -> str:
        return build[build.index("-t") + 1]

    def option(self, verb: str, flag: str) -> str:
        call = self.call(verb)
        return call[call.index(flag) + 1]

    def labels(self, verb: str = "run") -> dict[str, str]:
        call = self.call(verb)
        return dict(
            call[index + 1].split("=", 1)
            for index, token in enumerate(call)
            if token == "--label"
        )

    def curls(self) -> list[str]:
        path = self.dir / "curl.log"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


RUN_ARGV = [
    "run", "--detach", "--progress", "none",
    "--name", "printstash", "--memory", "4g",
    "-p", "127.0.0.1:3000:3000",
    "-e", "VAULT_SETUP_MODE=trusted_network",
    "-v", "printstash:/data",
    "--label", "printstash.managed=1",
    "--label", f"printstash.image={DEFAULT_IMAGE}",
    "--label", f"printstash.digest={DIGEST}",
    "--label", "printstash.port=3000",
    "--label", "printstash.memory=4g",
    "--label", "printstash.volume=printstash",
    "--label", "printstash.env=none",
    DEFAULT_IMAGE,
]  # fmt: skip


class TestResolveScript:
    def test_finds_the_repository_through_a_symlink_on_path(self, stack: Stack) -> None:
        link = stack.root / "elsewhere" / "printstash"
        link.parent.mkdir()
        link.symlink_to(stack.script)

        stack.run("build", script=link)

        assert Path(stack.builds()[0][-1]) == stack.repo / "backend"

    def test_ignores_an_exported_cdpath(self, stack: Stack) -> None:
        stack.run("build", CDPATH=f".:{stack.root}")

        assert Path(stack.builds()[0][-1]) == stack.repo / "backend"


class TestReadDotenv:
    def test_takes_the_image_from_the_repository_env_file(self, stack: Stack) -> None:
        (stack.repo / ".env").write_text(
            'PRINTSTASH_IMAGE="ghcr.io/fork/printstash"\nPRINTSTASH_VERSION=0.14.0\n'
        )
        stack.put_json("images.json", {"ghcr.io/fork/printstash:0.14.0": DIGEST})

        stack.run("up")

        assert stack.call("run")[-1] == "ghcr.io/fork/printstash:0.14.0"

    def test_takes_the_port_from_the_repository_env_file(self, stack: Stack) -> None:
        (stack.repo / ".env").write_text("PRINTSTASH_HTTP_PORT=8080\n")

        stack.run("up")

        assert stack.option("run", "-p") == "127.0.0.1:8080:3000"

    def test_lets_the_environment_win_over_the_env_file(self, stack: Stack) -> None:
        (stack.repo / ".env").write_text("PRINTSTASH_HTTP_PORT=8080\n")

        stack.run("up", PRINTSTASH_HTTP_PORT="9090")

        assert stack.option("run", "-p") == "127.0.0.1:9090:3000"

    def test_ignores_other_keys_in_the_env_file(self, stack: Stack) -> None:
        (stack.repo / ".env").write_text("PRINTSTASH_MEMORY=64g\nVAULT_JWT_SECRET=x\n")

        stack.run("up")

        assert stack.option("run", "--memory") == "4g"


class TestCtr:
    @pytest.mark.parametrize(
        ("command", "hang"),
        [
            pytest.param(("up",), "container system status", id="up-status"),
            pytest.param(("up",), "container run", id="up-run"),
            pytest.param(("up",), "container image inspect", id="up-image"),
            pytest.param(("status",), "container system status", id="status"),
            pytest.param(("down",), "container system status", id="down-status"),
            pytest.param(("logs",), "container system status", id="logs"),
        ],
    )
    def test_names_the_pfd_wedge_when_a_command_hangs(
        self, stack: Stack, command: tuple[str, ...], hang: str
    ) -> None:
        stack.put("hang", hang)

        result = stack.run(*command)

        assert result.returncode == 1, result.stderr
        assert "apple/container#2275" in result.stderr

    def test_names_the_wedge_when_stopping_hangs(self, stack: Stack) -> None:
        stack.container("running")
        stack.put("hang", "container stop")

        result = stack.run("down")

        assert "apple/container#2275" in result.stderr

    def test_names_the_wedge_when_only_the_first_start_hangs(
        self, stack: Stack
    ) -> None:
        stack.put("system_status_rc", "1")
        stack.put("hang", "container system start")

        result = stack.run("up")

        assert "apple/container#2275" in result.stderr

    def test_says_the_cli_is_missing(self, stack: Stack) -> None:
        (stack.bin / "container").unlink()

        result = stack.run("status")

        assert result.returncode == 1
        assert "not installed or not on PATH" in result.stderr

    def test_says_the_timeout_tool_is_missing(self, stack: Stack) -> None:
        result = stack.run("status", PRINTSTASH_TIMEOUT_BIN=str(stack.root / "missing"))

        assert result.returncode == 1
        assert "PRINTSTASH_TIMEOUT_BIN" in result.stderr


class TestCmdUp:
    def test_creates_the_container_with_the_stack_settings(self, stack: Stack) -> None:
        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert stack.call("run") == RUN_ARGV

    def test_prints_the_address_once_it_answers(self, stack: Stack) -> None:
        result = stack.run("up")

        assert result.stdout.strip() == "PrintStash is running at http://127.0.0.1:3000"

    def test_checks_a_free_port_on_both_loopback_addresses(self, stack: Stack) -> None:
        stack.run("up")

        assert stack.calls("nc") == [["-z", "127.0.0.1", "3000"], ["-z", "::1", "3000"]]

    @pytest.mark.parametrize(
        "busy", ["127.0.0.1:3000", "::1:3000"], ids=["ipv4", "ipv6"]
    )
    def test_refuses_a_port_that_is_already_taken(
        self, stack: Stack, busy: str
    ) -> None:
        stack.put("busy", busy)

        result = stack.run("up")

        assert result.returncode == 1
        assert "port 3000 is already in use" in result.stderr
        assert "run" not in stack.verbs()

    def test_uses_the_port_it_is_given(self, stack: Stack) -> None:
        stack.run("up", "--port", "4100")

        assert stack.option("run", "-p") == "127.0.0.1:4100:3000"

    def test_uses_the_image_it_is_given(self, stack: Stack) -> None:
        stack.run("up", "--image", "printstash:local")

        assert stack.call("run")[-1] == "printstash:local"

    def test_builds_the_default_image_from_the_environment(self, stack: Stack) -> None:
        stack.run(
            "up",
            PRINTSTASH_IMAGE="registry.example/fork/printstash",
            PRINTSTASH_VERSION="0.14.0",
        )

        assert stack.call("run")[-1] == "registry.example/fork/printstash:0.14.0"

    def test_takes_the_default_port_from_the_environment(self, stack: Stack) -> None:
        stack.run("up", PRINTSTASH_HTTP_PORT="4200")

        assert stack.option("run", "-p") == "127.0.0.1:4200:3000"

    def test_takes_the_memory_size_from_the_environment(self, stack: Stack) -> None:
        stack.run("up", PRINTSTASH_MEMORY="6g")

        assert stack.option("run", "--memory") == "6g"

    def test_uses_the_container_name_from_the_environment(self, stack: Stack) -> None:
        stack.run("up", PRINTSTASH_CONTAINER_NAME="ps-test")

        assert stack.option("run", "--name") == "ps-test"

    def test_mounts_the_volume_from_the_environment(self, stack: Stack) -> None:
        stack.put_json("volumes.json", ["ps-data"])

        stack.run("up", PRINTSTASH_VOLUME="ps-data")

        assert stack.option("run", "-v") == "ps-data:/data"

    def test_passes_an_env_file_to_the_container(self, stack: Stack) -> None:
        settings = stack.root / "settings.env"
        settings.write_text("VAULT_MAX_UPLOAD_MB=1024\n")

        stack.run("up", "--env-file", str(settings))

        assert stack.option("run", "--env-file") == str(settings)

    def test_records_the_env_file_contents_in_the_spec(self, stack: Stack) -> None:
        settings = stack.root / "settings.env"
        settings.write_text("VAULT_MAX_UPLOAD_MB=1024\n")

        stack.run("up", "--env-file", str(settings))

        assert re.fullmatch(r"[0-9a-f]{16}", stack.labels()["printstash.env"])

    def test_does_not_set_the_restart_flag_nothing_could_act_on(
        self, stack: Stack
    ) -> None:
        stack.run("up")

        assert [
            t for t in stack.call("run") if t.startswith("VAULT_RESTART_ENABLED")
        ] == []

    def test_creates_a_missing_volume_saying_it_is_empty(self, stack: Stack) -> None:
        stack.put_json("volumes.json", [])

        result = stack.run("up")

        assert "creating a new, empty data volume printstash" in result.stderr
        assert stack.get_json("volumes.json") == ["printstash"]

    def test_creates_the_volume_at_the_size_asked_for(self, stack: Stack) -> None:
        stack.put_json("volumes.json", [])

        stack.run("up", PRINTSTASH_VOLUME_SIZE="2T")

        assert stack.option("volume create", "-s") == "2T"

    def test_pulls_an_image_it_does_not_have_before_running(self, stack: Stack) -> None:
        stack.put_json("images.json", {})

        stack.run("up")

        assert stack.verbs().index("image pull") < stack.verbs().index("run")

    def test_records_the_digest_of_the_image_it_runs(self, stack: Stack) -> None:
        stack.run("up")

        assert stack.labels()["printstash.digest"] == DIGEST

    def test_polls_the_health_route_on_the_published_port(self, stack: Stack) -> None:
        stack.run("up", "--port", "4100")

        assert stack.curls() == [
            "http://127.0.0.1:4100/api/v1/health",
            "http://127.0.0.1:4100/",
        ]

    def test_polls_health_bypassing_a_configured_proxy(self, stack: Stack) -> None:
        result = stack.run("up", http_proxy="http://proxy.invalid:3128")

        assert result.returncode == 0, result.stderr

    def test_leaves_a_matching_running_container_alone(self, stack: Stack) -> None:
        stack.container("running")

        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert {"stop", "delete", "run", "start"}.isdisjoint(stack.verbs())

    def test_checks_a_matching_running_container_answers(self, stack: Stack) -> None:
        stack.container("running")
        stack.put("curl_mode", "silent")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert result.returncode == 1
        assert "did not answer" in result.stderr

    def test_starts_a_matching_stopped_container(self, stack: Stack) -> None:
        stack.container("stopped")

        result = stack.run("up")

        assert "start" in stack.verbs()
        assert "run" not in stack.verbs()
        assert "http://127.0.0.1:3000" in result.stdout

    def test_waits_for_a_restarted_container_to_answer(self, stack: Stack) -> None:
        stack.container("stopped")
        stack.put("curl_fail_first", "2")

        stack.run("up")

        assert len(stack.curls()) == 4

    def test_reports_a_restarted_container_that_exits(self, stack: Stack) -> None:
        stack.container("stopped")
        stack.put("state_after_start", "stopped")

        result = stack.run("up")

        assert "exited before it answered" in result.stderr

    @pytest.mark.parametrize(
        ("labels", "args", "named"),
        [
            pytest.param({"port": "3001"}, (), "port", id="port"),
            pytest.param({"image": "printstash:local"}, (), "image", id="image"),
            pytest.param({"memory": "8g"}, (), "memory", id="memory"),
            pytest.param({"volume": "old"}, (), "volume", id="volume"),
            pytest.param({"env": "0123456789abcdef"}, (), "env", id="env"),
        ],
    )
    def test_refuses_to_replace_a_different_container_without_recreate(
        self, stack: Stack, labels: dict[str, str], args: tuple[str, ...], named: str
    ) -> None:
        stack.container("running", **labels)

        result = stack.run("up", *args)

        assert result.returncode == 1
        assert f"different {named}" in result.stderr
        assert {"stop", "delete", "run"}.isdisjoint(stack.verbs())

    def test_refuses_a_rebuilt_image_without_recreate(self, stack: Stack) -> None:
        stack.container("running", digest=NEW_DIGEST)

        result = stack.run("up")

        assert result.returncode == 1
        assert "has changed (rebuilt or pulled)" in result.stderr

    def test_replaces_a_container_running_a_rebuilt_image(self, stack: Stack) -> None:
        stack.container("running", digest=NEW_DIGEST)

        stack.run("up", "--recreate")

        assert [v for v in stack.verbs() if v in ("stop", "delete", "run")] == [
            "stop",
            "delete",
            "run",
        ]

    def test_replaces_a_matching_container_with_recreate(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("up", "--recreate")

        assert [v for v in stack.verbs() if v in ("stop", "delete", "run")] == [
            "stop",
            "delete",
            "run",
        ]

    def test_pulls_again_when_asked(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("up", "--pull")

        assert "image pull" in stack.verbs()

    def test_reports_a_newly_pulled_image_as_a_change(self, stack: Stack) -> None:
        stack.container("running")
        stack.put("pull_digest", NEW_DIGEST)

        result = stack.run("up", "--pull")

        assert "has changed (rebuilt or pulled)" in result.stderr

    def test_runs_a_newly_pulled_image_with_recreate(self, stack: Stack) -> None:
        stack.container("running")
        stack.put("pull_digest", NEW_DIGEST)

        stack.run("up", "--pull", "--recreate")

        assert stack.labels()["printstash.digest"] == NEW_DIGEST

    def test_keeps_the_old_container_when_the_pull_fails(self, stack: Stack) -> None:
        stack.container("running")
        stack.put("pull_rc", "1")

        result = stack.run("up", "--pull", "--recreate")

        assert result.returncode == 1
        assert {"stop", "delete", "run"}.isdisjoint(stack.verbs())

    def test_keeps_the_old_container_when_the_new_port_is_taken(
        self, stack: Stack
    ) -> None:
        stack.container("running")
        stack.put("busy", "127.0.0.1:4000")

        result = stack.run("up", "--port", "4000", "--recreate")

        assert result.returncode == 1
        assert {"stop", "delete"}.isdisjoint(stack.verbs())

    def test_does_not_check_the_port_the_old_container_holds(
        self, stack: Stack
    ) -> None:
        stack.container("running")
        stack.put("busy", "127.0.0.1:3000")

        result = stack.run("up", "--recreate")

        assert result.returncode == 0, result.stderr

    def test_gives_a_replaced_container_the_full_grace_period(
        self, stack: Stack
    ) -> None:
        stack.container("running")

        stack.run("up", "--recreate")

        assert stack.call("stop") == ["stop", "--time", "60", "printstash"]

    def test_saves_the_log_before_replacing_a_stopped_container(
        self, stack: Stack
    ) -> None:
        stack.container("stopped", port="3001")

        stack.run("up", "--recreate")

        assert stack.verbs().index("logs") < stack.verbs().index("delete")

    def test_refuses_to_replace_a_stopped_different_container_without_recreate(
        self, stack: Stack
    ) -> None:
        stack.container("stopped", port="3001")

        result = stack.run("up")

        assert result.returncode == 1
        assert "delete" not in stack.verbs()

    @pytest.mark.parametrize("command", ["up", "down"], ids=str)
    def test_never_touches_a_container_it_did_not_create(
        self, stack: Stack, command: str
    ) -> None:
        stack.put_json(
            "containers.json", {"printstash": {"state": "stopped", "labels": {}}}
        )

        result = stack.run(command)

        assert result.returncode == 1
        assert "did not create" in result.stderr
        assert {"stop", "delete"}.isdisjoint(stack.verbs())

    def test_waits_for_a_settling_container(self, stack: Stack) -> None:
        stack.container("stopping")

        result = stack.run("up")

        assert result.returncode == 1
        assert "printstash is stopping" in result.stderr

    def test_keeps_waiting_while_the_health_route_fails(self, stack: Stack) -> None:
        stack.put("curl_fail_first", "3")

        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert len(stack.curls()) == 5

    def test_does_not_count_an_http_error_as_ready(self, stack: Stack) -> None:
        stack.put("curl_mode", "http-error")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert result.returncode == 1

    def test_does_not_count_an_http_error_on_the_health_route_as_ready(
        self, stack: Stack
    ) -> None:
        stack.put("health_mode", "http-error")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert result.returncode == 1

    def test_bounds_each_health_request(self, stack: Stack) -> None:
        stack.put("health_mode", "silent")
        started = time.monotonic()

        stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert time.monotonic() - started < 10

    def test_keeps_waiting_while_the_root_page_fails(self, stack: Stack) -> None:
        stack.put("root_fails", "1")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert result.returncode == 1
        assert "did not answer within 1s" in result.stderr

    def test_bounds_the_wait_in_wall_time(self, stack: Stack) -> None:
        stack.put("curl_mode", "silent")
        started = time.monotonic()

        stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="2", PRINTSTASH_POLL_SECONDS="1")

        assert time.monotonic() - started < 10

    def test_prints_the_log_tail_when_it_never_answers(self, stack: Stack) -> None:
        stack.put("curl_mode", "silent")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert "Uvicorn running" in result.stderr

    def test_names_local_network_when_connections_are_dropped(
        self, stack: Stack
    ) -> None:
        stack.put("curl_mode", "reset")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="1")

        assert "Local Network" in result.stderr

    def test_stops_waiting_when_the_container_exits(self, stack: Stack) -> None:
        stack.put("state_after_run", "stopped")

        result = stack.run("up")

        assert result.returncode == 1
        assert "exited before it answered" in result.stderr
        assert stack.curls() == []

    def test_prints_the_log_tail_when_the_container_exits(self, stack: Stack) -> None:
        stack.put("state_after_run", "stopped")

        result = stack.run("up")

        assert "Uvicorn running" in result.stderr

    def test_reports_a_failed_run(self, stack: Stack) -> None:
        stack.put("run_rc", "1")

        result = stack.run("up")

        assert result.returncode == 1

    def test_reports_a_failed_start(self, stack: Stack) -> None:
        stack.container("stopped")
        stack.put("start_rc", "1")

        result = stack.run("up")

        assert result.returncode == 1
        assert "container start printstash failed" in result.stderr

    def test_reports_an_inspect_error_instead_of_absent(self, stack: Stack) -> None:
        stack.put("inspect_rc", "2")

        result = stack.run("up")

        assert result.returncode == 1
        assert "container inspect printstash failed" in result.stderr
        assert "run" not in stack.verbs()

    def test_starts_the_apiserver_when_it_is_stopped(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")

        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert ["system", "start", "--enable-kernel-install"] in stack.calls()

    def test_does_not_start_a_running_apiserver(self, stack: Stack) -> None:
        stack.run("up")

        assert "system start" not in stack.verbs()

    def test_reports_a_failed_apiserver_start(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")
        stack.put("system_start_rc", "5")

        result = stack.run("up")

        assert result.returncode == 1
        assert "system start failed (exit 5)" in result.stderr

    def test_reports_an_unexpected_status_failure(self, stack: Stack) -> None:
        stack.put("system_status_rc", "3")

        result = stack.run("up")

        assert result.returncode == 1
        assert "system status failed (exit 3)" in result.stderr

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--port", "abc"), id="port-not-a-number"),
            pytest.param(("--port", "0"), id="port-zero"),
            pytest.param(("--port", "65536"), id="port-too-large"),
            pytest.param(("--port",), id="port-without-value"),
            pytest.param(
                ("--port", "0", "--port", "3000"), id="first-of-two-ports-invalid"
            ),
            pytest.param(("--image",), id="image-without-value"),
            pytest.param(("--image", ""), id="empty-image"),
            pytest.param(("--image", "--pull"), id="image-looks-like-an-option"),
            pytest.param(("--env-file", "/nonexistent"), id="unreadable-env-file"),
            pytest.param(("--bogus",), id="unknown-option"),
        ],
    )
    def test_refuses_a_bad_invocation_before_any_command_runs(
        self, stack: Stack, args: tuple[str, ...]
    ) -> None:
        result = stack.run("up", *args)

        assert result.returncode == 2, result.stderr
        assert "usage:" in result.stderr
        assert stack.calls() == []

    def test_refuses_a_bad_port_from_the_environment(self, stack: Stack) -> None:
        result = stack.run("up", PRINTSTASH_HTTP_PORT="abc")

        assert result.returncode == 2
        assert stack.calls() == []

    def test_accepts_the_highest_port(self, stack: Stack) -> None:
        assert stack.run("up", "--port", "65535").returncode == 0


class TestCmdRestart:
    def test_stops_then_starts_a_running_container(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("restart")

        assert [
            v for v in stack.verbs() if v in ("stop", "start", "delete", "run")
        ] == ["stop", "start"]

    def test_waits_for_it_to_answer_on_its_own_port(self, stack: Stack) -> None:
        stack.container("running", port="4100")

        result = stack.run("restart")

        assert "http://127.0.0.1:4100" in result.stdout

    def test_starts_a_stopped_container(self, stack: Stack) -> None:
        stack.container("stopped")

        stack.run("restart")

        assert "stop" not in stack.verbs()

    def test_refuses_when_there_is_no_container(self, stack: Stack) -> None:
        result = stack.run("restart")

        assert result.returncode == 1
        assert "run up first" in result.stderr

    def test_refuses_a_container_it_did_not_create(self, stack: Stack) -> None:
        stack.put_json(
            "containers.json", {"printstash": {"state": "running", "labels": {}}}
        )

        result = stack.run("restart")

        assert result.returncode == 1
        assert "stop" not in stack.verbs()

    def test_refuses_arguments(self, stack: Stack) -> None:
        assert stack.run("restart", "now").returncode == 2


class TestCmdDown:
    def test_does_nothing_when_there_is_no_container(self, stack: Stack) -> None:
        result = stack.run("down")

        assert result.returncode == 0
        assert {"stop", "delete"}.isdisjoint(stack.verbs())

    def test_removes_a_stopped_container_without_stopping_it(
        self, stack: Stack
    ) -> None:
        stack.container("stopped")

        stack.run("down")

        assert [v for v in stack.verbs() if v in ("stop", "delete")] == ["delete"]

    def test_stops_a_running_container_before_removing_it(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("down")

        assert [v for v in stack.verbs() if v in ("stop", "delete")] == [
            "stop",
            "delete",
        ]

    def test_keeps_the_data_volume(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("down")

        assert stack.get_json("volumes.json") == ["printstash"]

    def test_names_the_volume_the_container_mounted(self, stack: Stack) -> None:
        stack.container("running", volume="ps-data")

        result = stack.run("down")

        assert "the ps-data volume is kept" in result.stdout

    def test_is_safe_to_run_twice(self, stack: Stack) -> None:
        stack.container("running")
        stack.run("down")

        second = stack.run("down")

        assert second.returncode == 0
        assert "is not running" in second.stdout

    def test_reports_a_failed_delete(self, stack: Stack) -> None:
        stack.container("stopped")
        stack.put("delete_rc", "1")

        result = stack.run("down")

        assert result.returncode == 1
        assert "removed" not in result.stdout

    def test_makes_no_container_call_when_the_apiserver_is_stopped(
        self, stack: Stack
    ) -> None:
        stack.put("system_status_rc", "1")

        result = stack.run("down")

        assert result.returncode == 0
        assert "inspect" not in stack.verbs()

    def test_refuses_arguments(self, stack: Stack) -> None:
        result = stack.run("down", "now")

        assert result.returncode == 2
        assert stack.calls() == []


class TestCmdLogs:
    def test_prints_the_container_log(self, stack: Stack) -> None:
        stack.container("running")

        result = stack.run("logs")

        assert "Application startup complete" in result.stdout

    def test_follows_the_log_when_asked(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("logs", "-f")

        assert stack.call("logs") == ["logs", "--follow", "printstash"]

    def test_refuses_when_the_apiserver_is_stopped(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")

        result = stack.run("logs")

        assert result.returncode == 1
        assert "apiserver is not running" in result.stderr

    @pytest.mark.parametrize(
        "args", [("-x",), ("-f", "more")], ids=["unknown-option", "extra-argument"]
    )
    def test_refuses_a_bad_invocation(
        self, stack: Stack, args: tuple[str, ...]
    ) -> None:
        result = stack.run("logs", *args)

        assert result.returncode == 2
        assert stack.calls() == []


class TestCmdStatus:
    def test_reports_the_spec_of_a_running_container(self, stack: Stack) -> None:
        stack.container(
            "running",
            image="printstash:local",
            port="3999",
            memory="6g",
            volume="ps-data",
        )

        result = stack.run("status")

        assert result.stdout.splitlines() == [
            "printstash: running",
            "image:  printstash:local",
            "port:   3999",
            "memory: 6g",
            "volume: ps-data",
        ]

    def test_reports_a_stopped_container(self, stack: Stack) -> None:
        stack.container("stopped")

        assert stack.run("status").stdout.splitlines()[0] == "printstash: stopped"

    def test_reports_an_absent_container(self, stack: Stack) -> None:
        assert stack.run("status").stdout.splitlines() == ["printstash: absent"]

    def test_reports_a_stopped_apiserver_without_starting_it(
        self, stack: Stack
    ) -> None:
        stack.put("system_status_rc", "1")

        result = stack.run("status")

        assert result.stdout.strip() == "apiserver: stopped"
        assert "system start" not in stack.verbs()

    def test_refuses_arguments(self, stack: Stack) -> None:
        assert stack.run("status", "now").returncode == 2


class TestUsage:
    @pytest.mark.parametrize(
        "args", [(), ("frobnicate",)], ids=["no-command", "unknown-command"]
    )
    def test_prints_usage_before_any_command_runs(
        self, stack: Stack, args: tuple[str, ...]
    ) -> None:
        result = stack.run(*args)

        assert result.returncode == 2
        assert "usage:" in result.stderr
        assert stack.calls() == []


class TestCmdBuild:
    def test_builds_the_three_images_in_dependency_order(self, stack: Stack) -> None:
        result = stack.run("build")

        assert result.returncode == 0, result.stderr
        assert [stack.tag_of(b) for b in stack.builds()] == [
            "api-image",
            "frontend-image",
            "printstash:local",
        ]

    def test_builds_each_image_from_its_own_directory(self, stack: Stack) -> None:
        stack.run("build")

        assert [
            Path(b[-1]).relative_to(stack.repo).as_posix() for b in stack.builds()
        ] == [
            "backend",
            "frontend",
            "backend/unified",
        ]

    def test_gives_every_build_the_same_builder_size(self, stack: Stack) -> None:
        stack.run("build")

        assert {
            (b[b.index("--cpus") + 1], b[b.index("--memory") + 1])
            for b in stack.builds()
        } == {("8", "16g")}

    def test_takes_the_builder_size_from_the_environment(self, stack: Stack) -> None:
        stack.run("build", PRINTSTASH_BUILD_CPUS="4", PRINTSTASH_BUILD_MEMORY="8g")

        assert {
            (b[b.index("--cpus") + 1], b[b.index("--memory") + 1])
            for b in stack.builds()
        } == {("4", "8g")}

    @pytest.mark.parametrize("variant", ["full", "lite"], ids=str)
    def test_passes_the_variant_to_the_api_build_only(
        self, stack: Stack, variant: str
    ) -> None:
        stack.run("build", "--variant", variant)

        assert [
            b[b.index("--build-arg") + 1] if "--build-arg" in b else None
            for b in stack.builds()
        ] == [
            f"PRINTSTASH_VARIANT={variant}",
            None,
            None,
        ]

    def test_builds_the_full_variant_by_default(self, stack: Stack) -> None:
        stack.run("build")

        assert "PRINTSTASH_VARIANT=full" in stack.builds()[0]

    def test_does_not_start_the_builder_separately(self, stack: Stack) -> None:
        stack.run("build")

        assert "builder" not in stack.verbs()

    def test_starts_the_apiserver_first_when_it_is_stopped(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")

        stack.run("build")

        assert stack.verbs().index("system start") < stack.verbs().index("build")

    @pytest.mark.parametrize(
        ("failing", "built"),
        [
            pytest.param("api-image", ["api-image"], id="api"),
            pytest.param(
                "frontend-image", ["api-image", "frontend-image"], id="frontend"
            ),
        ],
    )
    def test_stops_at_the_first_failed_image(
        self, stack: Stack, failing: str, built: list[str]
    ) -> None:
        stack.put("build_fails", failing)

        result = stack.run("build")

        assert result.returncode == 1
        assert [stack.tag_of(b) for b in stack.builds()] == built

    def test_names_the_image_that_failed(self, stack: Stack) -> None:
        stack.put("build_fails", "frontend-image")

        result = stack.run("build")

        assert "building frontend-image failed" in result.stderr

    def test_suggests_the_configured_published_image_instead(
        self, stack: Stack
    ) -> None:
        stack.put("build_fails", "frontend-image")

        result = stack.run("build", PRINTSTASH_IMAGE="ghcr.io/fork/printstash")

        assert "up --image ghcr.io/fork/printstash:latest" in result.stderr

    def test_never_runs_or_pulls_after_a_failed_build(self, stack: Stack) -> None:
        stack.put("build_fails", "printstash:local")

        stack.run("build")

        assert {"run", "image pull"}.isdisjoint(stack.verbs())

    def test_refuses_while_another_build_holds_the_lock(self, stack: Stack) -> None:
        (stack.root / "build.lock").mkdir()

        result = stack.run("build")

        assert result.returncode == 1
        assert stack.builds() == []

    def test_releases_the_lock_when_it_finishes(self, stack: Stack) -> None:
        stack.run("build")

        assert not (stack.root / "build.lock").exists()

    def test_says_where_it_looked_for_the_sources(self, stack: Stack) -> None:
        (stack.repo / "backend" / "unified" / "Dockerfile").unlink()

        result = stack.run("build")

        assert result.returncode == 1
        assert str(stack.repo) in result.stderr

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--variant", "huge"), id="unknown-variant"),
            pytest.param(("--variant",), id="variant-without-value"),
            pytest.param(("--bogus",), id="unknown-option"),
        ],
    )
    def test_refuses_a_bad_invocation_before_any_command_runs(
        self, stack: Stack, args: tuple[str, ...]
    ) -> None:
        result = stack.run("build", *args)

        assert result.returncode == 2
        assert stack.calls() == []


def _compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _service() -> dict:
    return _compose()["services"]["printstash"]


def _bake_targets() -> dict[str, dict[str, object]]:
    text = (REPO_ROOT / "docker-bake.hcl").read_text()
    targets: dict[str, dict[str, object]] = {}
    for name, body in re.findall(r'target "([\w-]+)" \{(.*?)\n\}', text, re.S):
        targets[name] = {
            "keys": set(re.findall(r"^\s*([\w-]+)\s*=", body, re.M)),
            "context": (re.search(r'context\s*=\s*"([^"]+)"', body) or [None, ""])[1],
            "args": dict(
                re.findall(
                    r'(\w+)\s*=\s*"([^"]+)"',
                    (re.search(r"args\s*=\s*\{(.*?)\}", body) or [None, ""])[1],
                )
            ),
        }
    return targets


class TestParityWithTheComposeStack:
    def test_runs_the_one_service_the_default_stack_defines(self) -> None:
        assert set(_compose()["services"]) == {"printstash"}

    def test_knows_every_setting_the_service_has(self) -> None:
        assert set(_service()) == {
            "image",
            "restart",
            "stop_grace_period",
            "ports",
            "environment",
            "volumes",
        }

    def test_publishes_exactly_one_port_like_compose(self) -> None:
        assert len(_service()["ports"]) == 1

    def test_mounts_exactly_one_volume_like_compose(self) -> None:
        assert len(_service()["volumes"]) == 1

    def test_defaults_to_the_image_compose_defaults_to(self, stack: Stack) -> None:
        repository = re.search(r"PRINTSTASH_IMAGE:-([^}]+)\}", _service()["image"])
        version = re.search(r"PRINTSTASH_VERSION:-([^}]+)\}", _service()["image"])
        assert repository and version

        stack.run("up")

        assert stack.call("run")[-1] == f"{repository.group(1)}:{version.group(1)}"

    def test_publishes_the_port_compose_publishes(self, stack: Stack) -> None:
        container_port = _service()["ports"][0].split(":")[-1]

        stack.run("up")

        assert stack.option("run", "-p").endswith(f":{container_port}")

    def test_sets_the_setup_mode_compose_sets(self, stack: Stack) -> None:
        stack.run("up")

        assert (
            f"VAULT_SETUP_MODE={_service()['environment']['VAULT_SETUP_MODE']}"
            in stack.call("run")
        )

    def test_differs_by_leaving_out_the_restart_flag_only(self) -> None:
        assert set(_service()["environment"]) == {
            "VAULT_SETUP_MODE",
            "VAULT_RESTART_ENABLED",
        }

    def test_mounts_the_volume_compose_mounts_at_the_same_path(
        self, stack: Stack
    ) -> None:
        stack.run("up")

        assert stack.option("run", "-v") == _service()["volumes"][0]

    def test_stops_with_the_grace_period_compose_allows(self, stack: Stack) -> None:
        stack.container("running")

        stack.run("down")

        assert stack.call("stop") == [
            "stop",
            "--time",
            _service()["stop_grace_period"].rstrip("s"),
            "printstash",
        ]


class TestParityWithTheImageBuild:
    def test_knows_every_setting_of_the_bake_targets(self) -> None:
        assert {name: target["keys"] for name, target in _bake_targets().items()} == {
            "api": {"context", "args"},
            "api-lite": {"context", "args"},
            "frontend": {"context"},
            "unified": {"context", "contexts", "api-image", "frontend-image", "tags"},
        }

    @pytest.mark.parametrize(
        ("target", "tag"),
        [
            ("api", "api-image"),
            ("frontend", "frontend-image"),
            ("unified", "printstash:local"),
        ],
        ids=str,
    )
    def test_builds_each_image_from_its_bake_context(
        self, stack: Stack, target: str, tag: str
    ) -> None:
        stack.run("build")

        build = next(b for b in stack.builds() if stack.tag_of(b) == tag)
        assert (
            Path(build[-1]).relative_to(stack.repo).as_posix()
            == _bake_targets()[target]["context"]
        )

    @pytest.mark.parametrize(
        ("variant", "target"), [("full", "api"), ("lite", "api-lite")], ids=str
    )
    def test_passes_exactly_the_args_bake_passes(
        self, stack: Stack, variant: str, target: str
    ) -> None:
        stack.run("build", "--variant", variant)

        api = stack.builds()[0]
        passed = dict(
            api[i + 1].split("=", 1)
            for i, token in enumerate(api)
            if token == "--build-arg"
        )
        assert passed == _bake_targets()[target]["args"]

    def test_tags_the_image_bake_tags(self, stack: Stack) -> None:
        tag = re.search(
            r'tags\s*=\s*\["([^"]+)"\]', (REPO_ROOT / "docker-bake.hcl").read_text()
        )
        assert tag is not None

        stack.run("build")

        assert stack.tag_of(stack.builds()[-1]) == tag.group(1)

    def test_names_the_images_the_unified_dockerfile_consumes(self) -> None:
        dockerfile = (REPO_ROOT / "backend" / "unified" / "Dockerfile").read_text()

        consumed = set(re.findall(r"^FROM (\S+)", dockerfile, re.M)) | set(
            re.findall(r"--from=(\S+)", dockerfile)
        )

        assert consumed == {"api-image", "frontend-image"}

    def test_polls_the_routes_the_image_healthcheck_polls(self, stack: Stack) -> None:
        dockerfile = (REPO_ROOT / "backend" / "unified" / "Dockerfile").read_text()
        healthcheck = re.search(r"HEALTHCHECK.*?CMD (.*)", dockerfile, re.S)
        assert healthcheck is not None
        routes = re.findall(
            r"http://127\.0\.0\.1:3000(/\S*)?", healthcheck.group(1).split("\n#")[0]
        )

        stack.run("up")

        assert [url.split(":3000")[1] for url in stack.curls()] == [
            route or "/" for route in routes
        ]
