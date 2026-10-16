"""`printstash.sh` runs the default stack on Apple `container` and keeps its promises.

`deploy/apple-container/printstash.sh` starts the single-container stack from
`docker-compose.yml` on Apple's `container`: `up` is idempotent and never destroys
anything without `--recreate`, `down` keeps the data volume, a bad argument is
refused before any command runs, and `build` stops at the first image that fails
instead of substituting another. The script is run for real, under bash, against a
fake `container`, `curl`, `nc` and `timeout` on PATH. The fake replays `container
inspect` output recorded from a real 1.5.0 run (`tests/fixtures/apple_container/app/`,
fake paths, addresses and MACs). The last group reads the compose, bake and
Dockerfile sources so the script cannot drift from the stack it stands in for.

The real runtime end to end (a healthy `up`, a restart that keeps data, a build into
an 8 CPU / 16 GiB builder) is verified on a Mac with it running, not here.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.paths import FIXTURES_DIR, REPO_ROOT

SCRIPT = REPO_ROOT / "deploy" / "apple-container" / "printstash.sh"
CAPTURES = FIXTURES_DIR / "apple_container" / "app"
BASH = "/bin/bash" if sys.platform == "darwin" else (shutil.which("bash") or "bash")

FAKE_CONTAINER = '''#!{python}
"""A stand-in for `container` that keeps its state in $FAKE_DIR."""
import json, os, re, sys
from pathlib import Path

d = Path(os.environ["FAKE_DIR"])
argv = sys.argv[1:]
with (d / "calls.log").open("a") as log:
    log.write(json.dumps(argv) + "\\n")

def read(name, default=""):
    path = d / name
    return path.read_text().strip() if path.exists() else default

def inspect_text():
    state = read("state", "absent")
    template = (d / ("inspect_stopped.txt" if state == "stopped" else "inspect_running.txt")).read_text()
    labels = json.loads(read("labels", "{{}}"))
    for key, value in labels.items():
        template = re.sub(r'("printstash\\.%s" : ")[^"]*(")' % key, lambda m: m.group(1) + value + m.group(2), template)
    return template

if argv[:2] == ["system", "status"]:
    sys.exit(int(read("system_status_rc", "0")))
if argv[:2] == ["system", "start"]:
    code = int(read("system_start_rc", "0"))
    if code == 0:
        (d / "system_status_rc").write_text("0")
    sys.exit(code)
if argv[0] == "inspect":
    if read("state", "absent") == "absent":
        sys.stderr.write("Error: container not found: printstash\\n")
        sys.exit(1)
    sys.stdout.write(inspect_text())
    sys.exit(0)
if argv[0] == "run":
    code = int(read("run_rc", "0"))
    if code:
        sys.exit(code)
    labels = {{}}
    for index, token in enumerate(argv):
        if token == "--label":
            key, _, value = argv[index + 1].partition("=")
            labels[key.removeprefix("printstash.")] = value
    (d / "labels").write_text(json.dumps(labels))
    (d / "state").write_text("running")
    if read("exit_after_run"):
        (d / "state").write_text("stopped")
    print("printstash")
    sys.exit(0)
if argv[0] == "start":
    (d / "state").write_text("running")
    sys.exit(0)
if argv[0] == "stop":
    (d / "state").write_text("stopped")
    sys.exit(0)
if argv[0] == "delete":
    (d / "state").write_text("absent")
    sys.exit(0)
if argv[0] == "logs":
    sys.stdout.write((d / "logs.txt").read_text())
    sys.exit(0)
if argv[:2] == ["image", "pull"]:
    sys.exit(0)
if argv[0] == "build":
    tag = argv[argv.index("-t") + 1]
    sys.exit(1 if read("build_fails") == tag else 0)
sys.stderr.write("unscripted container command: %r\\n" % argv)
sys.exit(99)
'''

FAKE_CURL = """#!{python}
import json, os, sys
from pathlib import Path

d = Path(os.environ["FAKE_DIR"])
url = sys.argv[-1]
with (d / "curl.log").open("a") as log:
    log.write(url + "\\n")
count = len((d / "curl.log").read_text().splitlines())
if count <= int((d / "curl_fail_first").read_text() if (d / "curl_fail_first").exists() else 0):
    sys.exit(7)
if url.endswith("/") and (d / "root_fails").exists():
    sys.exit(22)
sys.exit(0)
"""

FAKE_NC = """#!{python}
import os, sys
from pathlib import Path

d = Path(os.environ["FAKE_DIR"])
host, port = sys.argv[-2], sys.argv[-1]
with (d / "nc.log").open("a") as log:
    log.write(f"{{host}} {{port}}\\n")
busy = (d / "busy").read_text().split() if (d / "busy").exists() else []
sys.exit(0 if f"{{host}}:{{port}}" in busy else 1)
"""

FAKE_TIMEOUT = """#!/bin/sh
# usage: timeout SECONDS COMMAND... ; hangs (exit 124) when FAKE_DIR/hang names COMMAND
if [ -f "$FAKE_DIR/hang" ] && [ "$2" = "$(cat "$FAKE_DIR/hang")" ]; then
  exit 124
fi
shift
exec "$@"
"""


class Stack:
    """One run of the script against a fake machine in a temporary directory."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "fake"
        self.bin = root / "bin"
        self.dir.mkdir()
        self.bin.mkdir()
        for name in ("inspect_running.txt", "inspect_stopped.txt", "logs.txt"):
            shutil.copy(CAPTURES / name, self.dir / name)
        python = sys.executable
        for name, source in {
            "container": FAKE_CONTAINER,
            "curl": FAKE_CURL,
            "nc": FAKE_NC,
            "timeout": FAKE_TIMEOUT,
        }.items():
            path = self.bin / name
            path.write_text(
                source.replace("{python}", python).replace("{{", "{").replace("}}", "}")
            )
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(root),
            "FAKE_DIR": str(self.dir),
            "PRINTSTASH_TIMEOUT_BIN": str(self.bin / "timeout"),
            "PRINTSTASH_POLL_SECONDS": "0",
            "PRINTSTASH_HEALTH_TIMEOUT": "6",
        }

    def put(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text)

    def state(
        self,
        state: str,
        *,
        image: str = "ghcr.io/xiao-villamor/printstash:latest",
        port: str = "3000",
        memory: str = "4g",
    ) -> None:
        self.put("state", state)
        self.put("labels", json.dumps({"image": image, "port": port, "memory": memory}))

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        path = self.dir / "calls.log"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def verbs(self) -> list[str]:
        return [
            " ".join(call[:2]) if call[0] in ("system", "image") else call[0]
            for call in self.calls()
        ]

    def curls(self) -> list[str]:
        path = self.dir / "curl.log"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    return Stack(tmp_path)


RUN_ARGV = [
    "run",
    "--detach",
    "--progress",
    "none",
    "--name",
    "printstash",
    "--memory",
    "4g",
    "-p",
    "127.0.0.1:3000:3000",
    "-e",
    "VAULT_SETUP_MODE=trusted_network",
    "-v",
    "printstash:/data",
    "--label",
    "printstash.image=ghcr.io/xiao-villamor/printstash:latest",
    "--label",
    "printstash.port=3000",
    "--label",
    "printstash.memory=4g",
    "ghcr.io/xiao-villamor/printstash:latest",
]


class TestUp:
    def test_creates_the_container_with_the_stack_settings(self, stack: Stack) -> None:
        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert RUN_ARGV in stack.calls()

    def test_prints_the_address_once_it_answers(self, stack: Stack) -> None:
        result = stack.run("up")

        assert "http://127.0.0.1:3000" in result.stdout

    def test_checks_a_free_port_on_both_loopback_addresses(self, stack: Stack) -> None:
        stack.run("up")

        assert (stack.dir / "nc.log").read_text().splitlines() == [
            "127.0.0.1 3000",
            "::1 3000",
        ]

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

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[run.index("-p") + 1] == "127.0.0.1:4100:3000"

    def test_polls_the_health_route_on_the_published_port(self, stack: Stack) -> None:
        stack.run("up", "--port", "4100")

        assert stack.curls() == [
            "http://127.0.0.1:4100/api/v1/health",
            "http://127.0.0.1:4100/",
        ]

    def test_uses_the_image_it_is_given(self, stack: Stack) -> None:
        stack.run("up", "--image", "printstash:local")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[-1] == "printstash:local"

    def test_builds_the_default_image_from_the_environment(self, stack: Stack) -> None:
        stack.run(
            "up",
            PRINTSTASH_IMAGE="registry.example/fork/printstash",
            PRINTSTASH_VERSION="0.14.0",
        )

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[-1] == "registry.example/fork/printstash:0.14.0"

    def test_takes_the_default_port_from_the_environment(self, stack: Stack) -> None:
        stack.run("up", PRINTSTASH_HTTP_PORT="4200")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[run.index("-p") + 1] == "127.0.0.1:4200:3000"

    def test_takes_the_memory_size_from_the_environment(self, stack: Stack) -> None:
        stack.run("up", PRINTSTASH_MEMORY="6g")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[run.index("--memory") + 1] == "6g"

    def test_does_not_set_the_restart_flag_nothing_could_act_on(
        self, stack: Stack
    ) -> None:
        stack.run("up")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert not any(token.startswith("VAULT_RESTART_ENABLED") for token in run)

    def test_leaves_a_running_container_with_the_same_spec_alone(
        self, stack: Stack
    ) -> None:
        stack.state("running")

        result = stack.run("up")

        assert result.returncode == 0
        assert "already running" in result.stdout
        assert stack.verbs() == ["system status", "inspect"]

    @pytest.mark.parametrize(
        "args",
        [("--port", "3001"), ("--image", "printstash:local")],
        ids=["port", "image"],
    )
    def test_refuses_to_replace_a_running_container_without_recreate(
        self, stack: Stack, args: tuple[str, ...]
    ) -> None:
        stack.state("running")

        result = stack.run("up", *args)

        assert result.returncode == 1
        assert "--recreate" in result.stderr
        assert {"stop", "delete", "run"}.isdisjoint(stack.verbs())

    def test_replaces_a_running_container_with_recreate(self, stack: Stack) -> None:
        stack.state("running")

        result = stack.run("up", "--image", "printstash:local", "--recreate")

        assert result.returncode == 0, result.stderr
        assert [v for v in stack.verbs() if v in ("stop", "delete", "run")] == [
            "stop",
            "delete",
            "run",
        ]

    def test_gives_a_replaced_container_the_full_grace_period_to_stop(
        self, stack: Stack
    ) -> None:
        stack.state("running")

        stack.run("up", "--port", "3001", "--recreate")

        assert ["stop", "--time", "60", "printstash"] in stack.calls()

    def test_starts_a_stopped_container_with_the_same_spec(self, stack: Stack) -> None:
        stack.state("stopped")

        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert "start" in stack.verbs()
        assert "run" not in stack.verbs()

    def test_saves_the_log_before_replacing_a_stopped_container_with_another_spec(
        self, stack: Stack
    ) -> None:
        stack.state("stopped")

        stack.run("up", "--port", "3001")

        verbs = stack.verbs()
        assert verbs.index("logs") < verbs.index("delete") < verbs.index("run")

    def test_pulls_first_when_asked(self, stack: Stack) -> None:
        stack.run("up", "--pull")

        verbs = stack.verbs()
        assert verbs.index("image pull") < verbs.index("run")

    def test_keeps_waiting_while_the_health_route_fails(self, stack: Stack) -> None:
        stack.put("curl_fail_first", "3")

        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert len(stack.curls()) == 5

    def test_keeps_waiting_while_the_root_page_fails(self, stack: Stack) -> None:
        stack.put("root_fails", "1")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="3")

        assert result.returncode == 1
        assert "did not answer within 3s" in result.stderr

    def test_prints_the_log_tail_when_it_never_answers(self, stack: Stack) -> None:
        stack.put("curl_fail_first", "1000")

        result = stack.run("up", PRINTSTASH_HEALTH_TIMEOUT="2")

        assert result.returncode == 1
        assert "Uvicorn running" in result.stderr

    def test_stops_waiting_when_the_container_exits(self, stack: Stack) -> None:
        stack.put("exit_after_run", "1")
        stack.put("curl_fail_first", "1000")

        result = stack.run("up")

        assert result.returncode == 1
        assert "exited before it answered" in result.stderr
        assert stack.curls() == []

    def test_starts_the_apiserver_when_it_is_stopped(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")

        result = stack.run("up")

        assert result.returncode == 0, result.stderr
        assert ["system", "start", "--enable-kernel-install"] in stack.calls()

    def test_does_not_start_a_running_apiserver(self, stack: Stack) -> None:
        stack.run("up")

        assert "system start" not in stack.verbs()

    def test_names_the_pfd_wedge_when_a_system_command_hangs(
        self, stack: Stack
    ) -> None:
        stack.put("hang", "container")

        result = stack.run("up")

        assert result.returncode == 1
        assert "apple/container#2275" in result.stderr

    def test_reports_a_failed_apiserver_start(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")
        stack.put("system_start_rc", "5")

        result = stack.run("up")

        assert result.returncode == 1
        assert "system start failed (exit 5)" in result.stderr


class TestDown:
    def test_does_nothing_when_there_is_no_container(self, stack: Stack) -> None:
        result = stack.run("down")

        assert result.returncode == 0
        assert {"stop", "delete"}.isdisjoint(stack.verbs())

    def test_removes_a_stopped_container_without_stopping_it(
        self, stack: Stack
    ) -> None:
        stack.state("stopped")

        result = stack.run("down")

        assert result.returncode == 0
        assert "stop" not in stack.verbs()
        assert "delete" in stack.verbs()

    def test_stops_a_running_container_before_removing_it(self, stack: Stack) -> None:
        stack.state("running")

        stack.run("down")

        assert [v for v in stack.verbs() if v in ("stop", "delete")] == [
            "stop",
            "delete",
        ]

    def test_keeps_the_data_volume(self, stack: Stack) -> None:
        stack.state("running")

        result = stack.run("down")

        assert "volume" not in stack.verbs()
        assert "volume is kept" in result.stdout

    def test_is_safe_to_run_twice(self, stack: Stack) -> None:
        stack.state("running")
        stack.run("down")

        second = stack.run("down")

        assert second.returncode == 0
        assert "is not running" in second.stdout

    def test_makes_no_container_call_when_the_apiserver_is_stopped(
        self, stack: Stack
    ) -> None:
        stack.put("system_status_rc", "1")

        result = stack.run("down")

        assert result.returncode == 0
        assert "inspect" not in stack.verbs()


class TestLogsAndStatus:
    def test_prints_the_container_log(self, stack: Stack) -> None:
        stack.state("running")

        result = stack.run("logs")

        assert "Application startup complete" in result.stdout

    def test_follows_the_log_when_asked(self, stack: Stack) -> None:
        stack.state("running")

        stack.run("logs", "-f")

        assert ["logs", "--follow", "printstash"] in stack.calls()

    def test_reports_the_spec_of_a_running_container(self, stack: Stack) -> None:
        stack.state("running", image="printstash:local", port="3999", memory="6g")

        result = stack.run("status")

        assert result.stdout.splitlines() == [
            "printstash: running",
            "image:  printstash:local",
            "port:   3999",
            "memory: 6g",
        ]

    def test_reports_a_stopped_container(self, stack: Stack) -> None:
        stack.state("stopped")

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


class TestArguments:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param((), id="no-command"),
            pytest.param(("frobnicate",), id="unknown-command"),
            pytest.param(("up", "--port", "abc"), id="port-not-a-number"),
            pytest.param(("up", "--port", "0"), id="port-zero"),
            pytest.param(("up", "--port", "65536"), id="port-too-large"),
            pytest.param(("up", "--port"), id="port-without-value"),
            pytest.param(("up", "--image"), id="image-without-value"),
            pytest.param(("up", "--bogus"), id="unknown-up-option"),
            pytest.param(("down", "now"), id="down-with-argument"),
            pytest.param(("logs", "-x"), id="unknown-logs-option"),
            pytest.param(("status", "now"), id="status-with-argument"),
            pytest.param(("build", "--variant", "huge"), id="unknown-variant"),
            pytest.param(("build", "--variant"), id="variant-without-value"),
        ],
    )
    def test_refuses_a_bad_invocation_before_any_command_runs(
        self, stack: Stack, args: tuple[str, ...]
    ) -> None:
        result = stack.run(*args)

        assert result.returncode == 2, result.stderr
        assert "usage:" in result.stderr
        assert stack.calls() == []

    def test_accepts_the_highest_port(self, stack: Stack) -> None:
        assert stack.run("up", "--port", "65535").returncode == 0


class TestBuild:
    def build_calls(self, stack: Stack) -> list[list[str]]:
        return [call for call in stack.calls() if call[0] == "build"]

    def test_builds_the_three_images_in_dependency_order(self, stack: Stack) -> None:
        result = stack.run("build")

        assert result.returncode == 0, result.stderr
        assert [call[call.index("-t") + 1] for call in self.build_calls(stack)] == [
            "api-image",
            "frontend-image",
            "printstash:local",
        ]

    def test_builds_each_image_from_its_own_directory(self, stack: Stack) -> None:
        stack.run("build")

        assert [
            Path(call[-1]).relative_to(REPO_ROOT).as_posix()
            for call in self.build_calls(stack)
        ] == [
            "backend",
            "frontend",
            "backend/unified",
        ]

    def test_gives_every_build_the_same_builder_size(self, stack: Stack) -> None:
        stack.run("build")

        sizes = {
            (call[call.index("--cpus") + 1], call[call.index("--memory") + 1])
            for call in self.build_calls(stack)
        }
        assert sizes == {("8", "16g")}

    def test_takes_the_builder_size_from_the_environment(self, stack: Stack) -> None:
        stack.run("build", PRINTSTASH_BUILD_CPUS="4", PRINTSTASH_BUILD_MEMORY="8g")

        sizes = {
            (call[call.index("--cpus") + 1], call[call.index("--memory") + 1])
            for call in self.build_calls(stack)
        }
        assert sizes == {("4", "8g")}

    @pytest.mark.parametrize("variant", ["full", "lite"], ids=str)
    def test_passes_the_variant_to_the_api_build_only(
        self, stack: Stack, variant: str
    ) -> None:
        stack.run("build", "--variant", variant)

        passed = [
            call[call.index("--build-arg") + 1] if "--build-arg" in call else None
            for call in self.build_calls(stack)
        ]
        assert passed == [f"PRINTSTASH_VARIANT={variant}", None, None]

    def test_builds_the_full_variant_by_default(self, stack: Stack) -> None:
        stack.run("build")

        assert "PRINTSTASH_VARIANT=full" in self.build_calls(stack)[0]

    def test_does_not_start_the_builder_separately(self, stack: Stack) -> None:
        stack.run("build")

        assert "builder" not in stack.verbs()

    def test_starts_the_apiserver_first_when_it_is_stopped(self, stack: Stack) -> None:
        stack.put("system_status_rc", "1")

        stack.run("build")

        assert stack.verbs().index("system start") < stack.verbs().index("build")

    @pytest.mark.parametrize(
        ("failing", "later"),
        [
            pytest.param("api-image", ["frontend-image", "printstash:local"], id="api"),
            pytest.param("frontend-image", ["printstash:local"], id="frontend"),
        ],
    )
    def test_stops_at_the_first_failed_image(
        self, stack: Stack, failing: str, later: list[str]
    ) -> None:
        stack.put("build_fails", failing)

        result = stack.run("build")

        built = [call[call.index("-t") + 1] for call in self.build_calls(stack)]
        assert result.returncode == 1
        assert failing in built
        assert set(later).isdisjoint(built)

    def test_names_the_image_that_failed(self, stack: Stack) -> None:
        stack.put("build_fails", "frontend-image")

        result = stack.run("build")

        assert "building frontend-image failed" in result.stderr

    def test_suggests_the_published_image_instead(self, stack: Stack) -> None:
        stack.put("build_fails", "frontend-image")

        result = stack.run("build")

        assert "up --image ghcr.io/xiao-villamor/printstash:latest" in result.stderr

    def test_never_runs_an_image_it_did_not_build(self, stack: Stack) -> None:
        stack.put("build_fails", "printstash:local")

        stack.run("build")

        assert {"run", "image pull"}.isdisjoint(stack.verbs())


def _compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _bake_targets() -> dict[str, dict[str, str]]:
    text = (REPO_ROOT / "docker-bake.hcl").read_text()
    targets: dict[str, dict[str, str]] = {}
    for name, body in re.findall(r'target "([\w-]+)" \{(.*?)\n\}', text, re.S):
        context = re.search(r'context\s*=\s*"([^"]+)"', body)
        variant = re.search(r'PRINTSTASH_VARIANT\s*=\s*"([^"]+)"', body)
        targets[name] = {
            "context": context.group(1) if context else "",
            "variant": variant.group(1) if variant else "",
        }
    return targets


class TestParityWithTheComposeStack:
    def test_runs_the_one_service_the_default_stack_defines(self) -> None:
        assert set(_compose()["services"]) == {"printstash"}

    def test_defaults_to_the_image_compose_defaults_to(self, stack: Stack) -> None:
        image = _compose()["services"]["printstash"]["image"]
        repository = re.search(r"PRINTSTASH_IMAGE:-([^}]+)\}", image)
        version = re.search(r"PRINTSTASH_VERSION:-([^}]+)\}", image)
        assert repository and version

        stack.run("up")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[-1] == f"{repository.group(1)}:{version.group(1)}"

    def test_publishes_the_port_compose_publishes(self, stack: Stack) -> None:
        container_port = _compose()["services"]["printstash"]["ports"][0].split(":")[-1]

        stack.run("up")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[run.index("-p") + 1].endswith(f":{container_port}")

    def test_sets_the_setup_mode_compose_sets(self, stack: Stack) -> None:
        environment = _compose()["services"]["printstash"]["environment"]

        stack.run("up")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert f"VAULT_SETUP_MODE={environment['VAULT_SETUP_MODE']}" in run

    def test_differs_by_leaving_out_the_restart_flag_only(self) -> None:
        environment = _compose()["services"]["printstash"]["environment"]

        assert set(environment) == {"VAULT_SETUP_MODE", "VAULT_RESTART_ENABLED"}

    def test_mounts_the_volume_compose_mounts_at_the_same_path(
        self, stack: Stack
    ) -> None:
        mount = _compose()["services"]["printstash"]["volumes"][0]

        stack.run("up")

        run = next(call for call in stack.calls() if call[0] == "run")
        assert run[run.index("-v") + 1] == mount

    def test_stops_with_the_grace_period_compose_allows(self, stack: Stack) -> None:
        grace = _compose()["services"]["printstash"]["stop_grace_period"]
        stack.state("running")

        stack.run("down")

        assert ["stop", "--time", grace.rstrip("s"), "printstash"] in stack.calls()


class TestParityWithTheImageBuild:
    def test_builds_the_api_from_the_bake_context(self, stack: Stack) -> None:
        stack.run("build")

        api = next(c for c in stack.calls() if c[0] == "build" and "api-image" in c)
        assert Path(api[-1]) == REPO_ROOT / _bake_targets()["api"]["context"]

    def test_builds_the_frontend_from_the_bake_context(self, stack: Stack) -> None:
        stack.run("build")

        frontend = next(
            c for c in stack.calls() if c[0] == "build" and "frontend-image" in c
        )
        assert Path(frontend[-1]) == REPO_ROOT / _bake_targets()["frontend"]["context"]

    def test_builds_the_unified_image_from_the_bake_context(self, stack: Stack) -> None:
        stack.run("build")

        unified = next(
            c for c in stack.calls() if c[0] == "build" and "printstash:local" in c
        )
        assert Path(unified[-1]) == REPO_ROOT / _bake_targets()["unified"]["context"]

    @pytest.mark.parametrize(
        ("variant", "target"), [("full", "api"), ("lite", "api-lite")], ids=str
    )
    def test_passes_the_variant_bake_passes(
        self, stack: Stack, variant: str, target: str
    ) -> None:
        stack.run("build", "--variant", variant)

        api = next(c for c in stack.calls() if c[0] == "build" and "api-image" in c)
        assert f"PRINTSTASH_VARIANT={_bake_targets()[target]['variant']}" in api

    def test_tags_the_image_bake_tags(self, stack: Stack) -> None:
        text = (REPO_ROOT / "docker-bake.hcl").read_text()
        tag = re.search(r'tags\s*=\s*\["([^"]+)"\]', text)
        assert tag is not None

        stack.run("build")

        assert any(c[0] == "build" and tag.group(1) in c for c in stack.calls())

    def test_names_the_images_the_unified_dockerfile_consumes(self) -> None:
        dockerfile = (REPO_ROOT / "backend" / "unified" / "Dockerfile").read_text()
        contexts = re.search(
            r"contexts = \{(.*?)\}", (REPO_ROOT / "docker-bake.hcl").read_text(), re.S
        )
        assert contexts is not None

        names = set(
            re.findall(r"^\s*([\w-]+)\s*=\s*\"target:", contexts.group(1), re.M)
        )

        assert names == {"api-image", "frontend-image"}
        assert re.search(r"^FROM api-image", dockerfile, re.M)
        assert "COPY --from=frontend-image" in dockerfile

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
