"""A stand-in for Apple's `container` CLI, and for `curl`, `nc` and `timeout`.

`tests/repo/test_apple_container_stack.py` runs `deploy/apple-container/printstash.sh`
for real against these. Each one is reached through a small shell wrapper on PATH
named after the tool, which runs `python -I apple_container_cli.py <tool> <args>`.

They are strict where the real tools are: every `container` subcommand accepts only
the options container 1.5.0 lists for it (an unknown option exits 64, as the real
CLI does), containers are looked up by the name given, and `--progress` takes only
the values the real subcommand takes. `curl` honours a configured proxy unless told
`--noproxy`, waits forever without `-m`, and treats an HTTP error as success without
`-f`. `nc` without `-z` would wait on stdin. `container inspect` and `container image
inspect` print the pretty JSON 1.5.0 prints, rendered from output recorded on a Mac
(`tests/fixtures/apple_container/app/`).

State and knobs live in the directory named by `$FAKE_DIR`, one small file each, so a
test sets up a scenario by writing files and reads back `calls.log`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_DIR"])

# Option arity per subcommand, from `container <subcommand> --help` on 1.5.0, limited
# to the subcommands printstash.sh uses. True takes a value, False is a flag.
OPTIONS: dict[tuple[str, ...], dict[str, bool]] = {
    ("system", "status"): {"--format": True},
    ("system", "start"): {"--enable-kernel-install": False},
    ("inspect",): {},
    ("run",): {
        "--detach": False,
        "--progress": True,
        "--name": True,
        "--memory": True,
        "-p": True,
        "-e": True,
        "--env-file": True,
        "-v": True,
        "--label": True,
    },
    ("start",): {},
    ("stop",): {"--time": True},
    ("delete",): {"--force": False},
    ("logs",): {"-n": True, "--follow": False},
    ("image", "pull"): {"--progress": True},
    ("image", "inspect"): {},
    ("volume", "inspect"): {},
    ("volume", "create"): {"-s": True},
    ("build",): {
        "--cpus": True,
        "--memory": True,
        "-t": True,
        "--build-arg": True,
        "--progress": True,
    },
}
PROGRESS = {
    ("run",): {"auto", "none", "ansi", "plain", "color"},
    ("image", "pull"): {"auto", "none", "ansi", "plain", "color"},
    ("build",): {"auto", "plain", "tty"},
}


def knob(name: str, default: str = "") -> str:
    path = STATE / name
    return path.read_text().strip() if path.exists() else default


def load(name: str, default: object) -> object:
    path = STATE / name
    return json.loads(path.read_text()) if path.exists() else default


def save(name: str, value: object) -> None:
    (STATE / name).write_text(json.dumps(value))


def log(tool: str, argv: list[str]) -> None:
    with (STATE / "calls.log").open("a") as handle:
        handle.write(json.dumps([tool, *argv]) + "\n")


def fail(code: int, message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def parse(
    command: tuple[str, ...], argv: list[str]
) -> tuple[dict[str, list[str]], list[str]]:
    allowed = OPTIONS[command]
    options: dict[str, list[str]] = {}
    positional: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-") and token != "-":
            if token not in allowed:
                fail(64, f"Error: Unknown option '{token}'")
            if allowed[token]:
                if index + 1 >= len(argv):
                    fail(64, f"Error: Missing value for '{token}'")
                options.setdefault(token, []).append(argv[index + 1])
                index += 2
                continue
            options.setdefault(token, []).append("")
        else:
            positional.append(token)
        index += 1
    for value in options.get("--progress", []):
        if value not in PROGRESS[command]:
            fail(64, f"Error: The value '{value}' is invalid for '--progress'")
    return options, positional


def render_inspect(name: str, entry: dict) -> str:
    stopped = entry["state"] == "stopped"
    template = (
        STATE / ("inspect_stopped.txt" if stopped else "inspect_running.txt")
    ).read_text()
    labels = ",\n".join(
        f'        "{key}" : "{value}"' for key, value in sorted(entry["labels"].items())
    )
    template = re.sub(
        r'"labels" : \{.*?\n      \}',
        '"labels" : {\n' + labels + "\n      }",
        template,
        count=1,
        flags=re.S,
    )
    template = re.sub(r'"state" : "[a-z]+"', f'"state" : "{entry["state"]}"', template)
    return template.replace('"id" : "printstash"', f'"id" : "{name}"')


def container(argv: list[str]) -> None:
    command = (
        tuple(argv[:2])
        if argv[:1] in (["system"], ["image"], ["volume"])
        else tuple(argv[:1])
    )
    if command not in OPTIONS:
        fail(64, f"Error: unscripted subcommand {argv!r}")
    options, positional = parse(command, argv[len(command) :])
    containers = load("containers.json", {})
    images = load("images.json", {})
    volumes = load("volumes.json", [])

    if command == ("system", "status"):
        sys.exit(int(knob("system_status_rc", "0")))
    if command == ("system", "start"):
        code = int(knob("system_start_rc", "0"))
        if code == 0:
            (STATE / "system_status_rc").write_text("0")
        sys.exit(code)
    if command == ("inspect",):
        if knob("inspect_rc"):
            fail(
                int(knob("inspect_rc")), 'Error: internalError: "XPC connection error"'
            )
        name = positional[0]
        if name not in containers:
            fail(1, f"Error: container not found: {name}")
        sys.stdout.write(render_inspect(name, containers[name]))
        return
    if command == ("run",):
        name = options["--name"][0]
        if name in containers:
            fail(1, f"Error: container with id {name} already exists")
        if knob("run_rc"):
            fail(
                int(knob("run_rc")),
                knob("run_stderr", "Error: failed to bootstrap container"),
            )
        labels = dict(value.split("=", 1) for value in options.get("--label", []))
        containers[name] = {
            "state": knob("state_after_run", "running"),
            "labels": labels,
            "image": positional[0],
            "env": options.get("-e", []),
            "env_file": options.get("--env-file", [""])[0],
            "volume": options.get("-v", [""])[0],
        }
        save("containers.json", containers)
        print(name)
        return
    if command in (("start",), ("stop",), ("delete",)):
        name = positional[0]
        if name not in containers:
            fail(
                1,
                f'Error: internalError: "failed to {command[0]} container" (cause: "notFound: "container with ID {name} not found"")',
            )
        if knob(f"{command[0]}_rc"):
            fail(
                int(knob(f"{command[0]}_rc")),
                f"Error: failed to {command[0]} container {name}",
            )
        if command == ("start",):
            containers[name]["state"] = knob("state_after_start", "running")
        elif command == ("stop",):
            containers[name]["state"] = "stopped"
        else:
            del containers[name]
        save("containers.json", containers)
        return
    if command == ("logs",):
        name = positional[0]
        if name not in containers:
            fail(1, f"Error: failed to get logs for container {name}")
        lines = (STATE / "logs.txt").read_text().splitlines(keepends=True)
        count = options.get("-n", [None])[0]
        sys.stdout.write("".join(lines[-int(count) :] if count else lines))
        return
    if command == ("image", "pull"):
        if knob("pull_rc"):
            fail(int(knob("pull_rc")), "Error: failed to pull image")
        images[positional[0]] = knob("pull_digest", "sha256:" + "1" * 64)
        save("images.json", images)
        return
    if command == ("image", "inspect"):
        ref = positional[0]
        if ref not in images:
            fail(1, f"Error: image not found: {ref}")
        print('[\n  {\n    "configuration" : {\n      "descriptor" : {')
        print(f'        "digest" : "{images[ref]}",')
        print(
            '        "mediaType" : "application/vnd.oci.image.index.v1+json"\n      },'
        )
        print(f'      "name" : "{ref}"\n    }}\n  }}\n]')
        return
    if command == ("volume", "inspect"):
        if positional[0] not in volumes:
            fail(1, f"Error: volume not found: {positional[0]}")
        print(json.dumps([{"configuration": {"name": positional[0]}}]))
        return
    if command == ("volume", "create"):
        volumes.append(positional[0])
        save("volumes.json", volumes)
        save("volume_size.json", options.get("-s", [""])[0])
        return
    if command == ("build",):
        tag = options["-t"][0]
        if knob("build_fails") == tag:
            fail(1, f"Error: build of {tag} failed")
        images[tag] = "sha256:" + str(len(images)) * 64
        save("images.json", images)


def curl(argv: list[str]) -> None:
    url = argv[-1]
    count = (
        len((STATE / "curl.log").read_text().splitlines())
        if (STATE / "curl.log").exists()
        else 0
    )
    # `main` has already logged this call, so the first call sees count == 1.
    if count <= int(knob("curl_fail_first", "0")):
        sys.exit(7)
    if os.environ.get("http_proxy") and "--noproxy" not in argv:
        sys.exit(5)  # the configured proxy cannot be resolved
    # `health_mode` applies to the health route only, `curl_mode` to every URL.
    health = url.endswith("/api/v1/health") and knob("health_mode")
    mode = knob("health_mode") if health else knob("curl_mode")
    if mode == "silent":
        if "-m" not in argv:
            # The forwarder accepted and the app never answers: real curl waits forever.
            time.sleep(20)
        sys.exit(28)
    if mode == "reset":
        sys.exit(56)
    if mode == "http-error" or (url.endswith("/") and knob("root_fails")):
        sys.exit(22 if "-f" in argv or "-fsS" in argv else 0)


def nc(argv: list[str]) -> None:
    if "-z" not in argv:
        fail(98, "nc without -z would connect and wait on stdin")
    host, port = argv[-2], argv[-1]
    busy = knob("busy").split()
    sys.exit(0 if f"{host}:{port}" in busy else 1)


def timeout(argv: list[str]) -> None:
    if not argv or not argv[0].isdigit():
        fail(125, "timeout: invalid duration")
    command = argv[1:]
    hang = knob("hang")
    if hang and " ".join(command).startswith(hang):
        sys.exit(124)
    os.execvp(command[0], command)


def main() -> None:
    tool, argv = sys.argv[1], sys.argv[2:]
    if tool != "timeout":
        log(tool, argv)
    if tool == "curl":
        with (STATE / "curl.log").open("a") as handle:
            handle.write(argv[-1] + "\n")
    {"container": container, "curl": curl, "nc": nc, "timeout": timeout}[tool](argv)


if __name__ == "__main__":
    main()
