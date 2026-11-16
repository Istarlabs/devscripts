#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Build the container image, push it to the internal registry and restart it on the server.

Three commands that used to be typed by hand, with the part that is easy to skip built in: every
image gets a timestamped tag next to `latest`, and the build stamps what it was made from into the
image itself. Without that, `latest` is the only thing the registry knows and a running container
cannot say which source it came from.

    python3 Scripts/deploy.py deploy            build, push, restart, wait for healthy
    python3 Scripts/deploy.py deploy --no-restart
    python3 Scripts/deploy.py status            what the server actually runs right now
    python3 Scripts/deploy.py tags              the tags the registry holds, newest first
    python3 Scripts/deploy.py logs              the last 50 entries the container logged
    python3 Scripts/deploy.py logs --level Warn --since 1h
    python3 Scripts/deploy.py logs --follow      stream it

`logs` is here rather than in `logs.py` because the connection is the hard part, not the reading:
that script owns where NLog puts a file on *this* machine, this one already owns the server and the
container name. It filters whole entries the way `logs.py` does — a logged exception spans many
lines and only the first carries a level, so a line-by-line match prints a stack trace without the
message that explains it.

The unit on the server tracks `latest` (`Pull=newer`), so a restart is what picks a new image up.
The timestamped tags are the history: to go back to one, point `Image=` in
`~/.config/containers/systemd/{unit}.container` at it and restart.

Registry, image, host and unit come from the [deploy] block of Scripts/project.toml.

A second server is a named target: `--target production` reads [deploy.targets.production], whose keys
(registry, image, ssh_host, unit, container, label_prefix) replace the [deploy] ones. A registry that
only listens on the server's own loopback is reached with `tunnel = "5000:localhost:5000"`: the push
and `tags` open that ssh port forward for as long as they need it, and the server pulls from its own
`localhost:5000`. The local end may be any free port (`tunnel = "5001:localhost:5000"`) when something on
this machine already listens on 5000; the image keeps its `registry` name, only the push goes to the other port.

    python3 Scripts/deploy.py deploy --target production
    python3 Scripts/deploy.py status --target production

A target may track a tag of its own (`tag = "dev"`) instead of `latest`, so that a copy of the application next to
production can be fed branch builds without moving what production restarts from. And a target may be a podman-compose
stack rather than a unit (`compose = "lab/pmr-dev"`, `service = "app"`): `deploy` then pushes, copies the compose file,
the schema script and the routine dump to that directory, and recreates the one service. `reset` throws the stack and
its volumes away and starts it again empty - the first-run state:

    python3 Scripts/deploy.py reset --target dev

A registry only grows: `keep_tags = 5` (a target's or [deploy]'s) makes `deploy` end with `prune`, which deletes every
build tag but the newest five (and the floating one) and gives the disk space back with a garbage collection while the
registry's unit (`registry_unit`, default `registry`; its volume or host directory `registry_volume`, default `registry-data`) is stopped. `prune --keep N` does it by hand, `deploy --no-prune`
skips it. Rolling back past the kept tags is then a rebuild.

    python3 Scripts/deploy.py prune --target production

Exit code is non-zero when a step fails, so it can gate a chain.
"""
import argparse
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import NoReturn

from project import project

SCRIPTS = str(project.root / "Scripts")
REPO = str(project.root)

# deploy.context is relative to the repository root: ".." when the Containerfile needs a sibling
# repository in the build context, "." when it does not.
CONTEXT = os.path.normpath(os.path.join(REPO, project.get("deploy.context", ".")))

def _target():
    """The name after `--target`, read here because the settings below are fixed before argparse runs."""
    for index, argument in enumerate(sys.argv):
        if argument == "--target" and index + 1 < len(sys.argv):
            return sys.argv[index + 1]

        if argument.startswith("--target="):
            return argument.split("=", 1)[1]

    return None


TARGET = _target()

if TARGET and project.get(f"deploy.targets.{TARGET}") is None:
    sys.stderr.write(f"refused: {project.config_file} has no [deploy.targets.{TARGET}].\n")
    sys.exit(2)


# A target may build from a Containerfile of its own: a second service of the repository has another image.
CONTAINERFILE = str(project.path(
    f"deploy.targets.{TARGET}.containerfile" if TARGET and project.get(f"deploy.targets.{TARGET}.containerfile")
    else "deploy.containerfile",
    "Containerfile"))


def setting(key, default=None):
    """A deploy setting: the one of the chosen target when it has it, else the [deploy] one, else the default."""
    missing = object()
    value = project.get(f"deploy.targets.{TARGET}.{key}", missing) if TARGET else missing

    return project.get(f"deploy.{key}", default) if value is missing else value


def required(key):
    return setting(key) if setting(key) is not None else project.require(f"deploy.{key}")


REGISTRY = required("registry")
IMAGE = setting("image", project.name.lower())
SSH_HOST = required("ssh_host")

# The tag the server tracks. A second instance of the same image (a dev copy next to production) tracks another one,
# so that building for it never moves the tag production restarts from.
FLOATING_TAG = setting("tag", "latest")

# A target that runs as a podman-compose stack instead of a systemd unit: the directory on the server (relative to the
# ssh home) that holds `deploy/compose.yaml` and what it mounts, and the service in it that this target's image is.
COMPOSE = setting("compose")
SERVICE = setting("service")
COMPOSE_PROJECT = os.path.basename(COMPOSE.rstrip("/")) if COMPOSE else None

UNIT = setting("unit") if COMPOSE else required("unit")
CONTAINER = setting("container", UNIT)
LABEL_PREFIX = setting("label_prefix", UNIT or SERVICE)

# `local:host:port` of an ssh port forward to SSH_HOST, for a registry that only listens on the server's own loopback:
# the push and the tag list go through it, and the server pulls from its own localhost.
TUNNEL = setting("tunnel")

HEALTH_TIMEOUT_SECONDS = 120

# Two loggers write to this container's console and they do not agree on a format: NLog's target
# writes "[2026-08-23 18:02:51.6490] INFO - Class.Method: message", the framework's own console
# logger writes "info: Namespace.Class[42]" with the message indented on the next line. A line that
# matches neither is the continuation of whatever came before it — an indented message, a stack
# trace — and belongs to that entry.
NLOG_ENTRY = re.compile(r"^\[\d{4}-\d{2}-\d{2} [\d:.]+\]\s+(?P<level>[A-Z]+)\s+-")
FRAMEWORK_ENTRY = re.compile(r"^(?P<level>trce|dbug|info|warn|fail|crit):\s")

# Lowest to highest, so "--level Warn" can mean "this and above". The two spellings of each level
# are folded together here; NLog's Fatal and the framework's crit are the same severity.
LEVELS = ["trace", "debug", "information", "warning", "error", "critical"]
LEVEL_ALIASES = {
    "trce": "trace", "trace": "trace",
    "dbug": "debug", "debug": "debug",
    "info": "information", "information": "information",
    "warn": "warning", "warning": "warning",
    "fail": "error", "error": "error",
    "crit": "critical", "fatal": "critical", "critical": "critical",
}


def run(command, check=True, capture=True):
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=capture)

    if check and result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    return result


def out(command):
    return run(command).stdout.strip()


def refuse(message) -> NoReturn:
    sys.stderr.write(f"refused: {message}\n")
    sys.exit(1)


def build_tag():
    """`20260823_1858_59efb56c`, plus `_dirty` when the tree was not clean.

    Local time on purpose — the tag is read by whoever asks "what did I deploy this evening" — and
    the commit is in it because the timestamp alone dates a build without saying what is in it.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    revision = out(["git", "rev-parse", "--short=8", "HEAD"])
    dirty = "_dirty" if out(["git", "status", "--porcelain"]) else ""

    return f"{stamp}_{revision}{dirty}"


def reference(tag):
    return f"{REGISTRY}/{IMAGE}:{tag}"


def source_labels(tag):
    """What this image was built from, so a running container can be traced back to a commit.

    The working tree is allowed to be dirty — deploying to try something out is the normal case here
    — but then the commit alone would be a false answer, so the state is recorded next to it.
    """
    revision = out(["git", "rev-parse", "HEAD"])
    branch = out(["git", "branch", "--show-current"])
    dirty = "_dirty" in tag

    return [
        f"org.opencontainers.image.version={tag}",
        f"org.opencontainers.image.revision={revision}",
        f"org.opencontainers.image.created={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"{LABEL_PREFIX}.build.branch={branch}",
        f"{LABEL_PREFIX}.build.dirty={'true' if dirty else 'false'}",
    ]


def build(tag):
    command = ["podman", "build", "-f", CONTAINERFILE]

    for label in source_labels(tag):
        command += ["--label", label]

    command += ["-t", reference(tag), "-t", reference(FLOATING_TAG), CONTEXT]

    print(f"building {reference(tag)} ...")
    run(command, capture=False)


@contextlib.contextmanager
def tunnel():
    """The ssh port forward of TUNNEL for the length of the block; nothing when the target has none."""
    if not TUNNEL:
        yield
        return

    local_port = int(TUNNEL.split(":")[0])
    process = subprocess.Popen(["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-L", TUNNEL, SSH_HOST],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    try:
        deadline = time.monotonic() + 15

        while time.monotonic() < deadline:
            if process.poll() is not None:
                refuse(f"the tunnel to {SSH_HOST} ({TUNNEL}) did not come up: {process.stderr.read().strip()}")

            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            refuse(f"the tunnel to {SSH_HOST} ({TUNNEL}) did not open port {local_port}")

        yield
    finally:
        process.terminate()
        process.wait(timeout=10)


def push(tag):
    with tunnel():
        for name in (reference(tag), reference(FLOATING_TAG)):
            print(f"pushing {name} ...")

            # The image is named for the registry as the server sees it (localhost:5000); this machine reaches it at
            # the local end of the tunnel, whose port may differ because something here already listens on 5000.
            destination = [f"docker://127.0.0.1:{TUNNEL.split(':')[0]}/{name.split('/', 1)[1]}"] if TUNNEL else []
            result = run(["podman", "push"] + (["--tls-verify=false"] if TUNNEL else []) + [name] + destination,
                         capture=False, check=False)

            if result.returncode != 0:
                refuse(f"pushing {name} failed")


def ssh_json(remote_command):
    """Ask the server something and read it as JSON, which avoids quoting a --format through ssh."""
    result = run(["ssh", SSH_HOST] + remote_command, check=False)

    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def container_state():
    data = ssh_json(["podman", "inspect", CONTAINER, "--format", "json"])

    return data[0] if data else None


def wait_for_healthy():
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        state = container_state()
        health = ((state or {}).get("State") or {}).get("Health") or {}
        status = health.get("Status")

        if status == "healthy":
            return True

        if status in ("unhealthy", None) and state and not state["State"].get("Running"):
            return False

        time.sleep(3)

    return False


def compose(*arguments):
    """podman-compose on the server, in the stack's `deploy` directory and under the stack's own project name."""
    # A compose.override.yaml that lives only on the server (a published port for a workstation, say) has to be named:
    # podman-compose does not pick it up by itself, and it is never synced from here.
    files = "-f compose.yaml $([ -f compose.override.yaml ] && echo -f compose.override.yaml)"
    run(["ssh", SSH_HOST, f"cd {COMPOSE}/deploy && podman-compose -p {COMPOSE_PROJECT} {files} {' '.join(arguments)}"],
        capture=False)


def sync_compose():
    """Puts the compose file, the schema script and the routine dump where the compose file expects them.

    Everything else the stack needs (the .env with the passwords) belongs to the server and is never written here.
    """
    run(["ssh", SSH_HOST, f"mkdir -p {COMPOSE}/deploy {COMPOSE}/Scripts/dump"])
    run(["rsync", "-a", "deploy/compose.yaml", "deploy/db-init", f"{SSH_HOST}:{COMPOSE}/deploy/"], capture=False)
    run(["rsync", "-a", "--delete", "Scripts/dump/PostgreSQL", f"{SSH_HOST}:{COMPOSE}/Scripts/dump/"], capture=False)


def restart(expected_tag):
    # The unit carries Pull=newer, and it has been observed not to fetch a freshly pushed `latest` —
    # leaving the previous image running while every output claimed a deploy. Pulling here is
    # deterministic, and the check below is what proves it rather than assuming it.
    print(f"pulling on {SSH_HOST} ...")
    run(["ssh", SSH_HOST, "podman", "pull", "-q"] + (["--tls-verify=false"] if TUNNEL else []) + [reference(FLOATING_TAG)],
        capture=False)

    if COMPOSE:
        print(f"recreating {SERVICE} of {COMPOSE_PROJECT} ...")
        compose("up", "-d", "--no-deps", "--force-recreate", SERVICE)
    else:
        print(f"restarting {UNIT} ...")
        run(["ssh", SSH_HOST, "systemctl", "--user", "restart", UNIT], capture=False)

    if not wait_for_healthy():
        refuse(f"{CONTAINER} did not report healthy within {HEALTH_TIMEOUT_SECONDS}s — "
               f"`python3 Scripts/deploy.py logs --level Warn`")

    running = (((container_state() or {}).get("Config") or {}).get("Labels") or {}) \
        .get("org.opencontainers.image.version")

    if running != expected_tag:
        refuse(f"{CONTAINER} is healthy but running {running or 'an unlabelled image'}, "
               f"not {expected_tag} — the new image did not reach the server")

    print("healthy")


def command_deploy(arguments):
    if not os.path.isdir(os.path.join(CONTEXT, "Istarnova")):
        refuse(f"no Istarnova checkout next to this repository ({CONTEXT}) — the image cannot build")

    tag = build_tag()
    build(tag)

    if arguments.no_push:
        print(f"built {reference(tag)} (not pushed)")
        return

    push(tag)

    if COMPOSE:
        sync_compose()

    if not arguments.no_restart:
        restart(tag)

    print(f"deployed {reference(tag)}")

    if setting("keep_tags") is not None and not arguments.no_prune:
        prune_registry(int(setting("keep_tags")))


def command_reset(arguments):
    if not COMPOSE:
        refuse("reset is for a compose target; a unit has no volumes to throw away")

    print(f"{COMPOSE_PROJECT} on {SSH_HOST}: removing the stack and every volume it holds (database, key rings)")
    sync_compose()
    compose("down", "-v")

    if not arguments.keep_down:
        compose("up", "-d")


def command_recreate(_arguments):
    if not COMPOSE:
        refuse("recreate is for a compose target")

    # The containers of a stack live in one pod, and a published port belongs to the pod: a port added to the compose
    # file or to the override only takes effect when the pod is built again. The volumes stay.
    print(f"{COMPOSE_PROJECT} on {SSH_HOST}: building the stack again, keeping its volumes")
    sync_compose()
    compose("down")
    compose("up", "-d")


def command_status(_arguments):
    state = container_state()

    if state is None:
        refuse(f"no container named {CONTAINER} on {SSH_HOST}")

    labels = (state.get("Config") or {}).get("Labels") or {}
    health = (state.get("State") or {}).get("Health") or {}

    print(f"image     {state.get('ImageName')}")
    print(f"image id  {state.get('Image', '')[:19]}")
    # A base image brings its own version label (ubuntu writes "24.04"), so an image built before
    # this script existed answers plausibly and wrongly. The revision below is the honest tell.
    print(f"version   {labels.get('org.opencontainers.image.version', '(unknown)')}")
    print(f"revision  {labels.get('org.opencontainers.image.revision', '(unknown)')[:12]}"
          f"{'  +uncommitted changes' if labels.get(f'{LABEL_PREFIX}.build.dirty') == 'true' else ''}")
    print(f"branch    {labels.get(f'{LABEL_PREFIX}.build.branch', '(unknown)')}")
    print(f"built     {labels.get('org.opencontainers.image.created', '(unknown)')}")
    print(f"state     {state['State'].get('Status')}, health {health.get('Status', 'n/a')}")
    print(f"started   {state['State'].get('StartedAt', '')[:19]}")


def log_entries(text):
    """Group the container's console output into entries, each starting at a line that carries a level."""
    entries = []

    for line in text.splitlines():
        match = NLOG_ENTRY.match(line) or FRAMEWORK_ENTRY.match(line)

        if match:
            entries.append({"level": LEVEL_ALIASES.get(match.group("level").lower()), "lines": [line]})
        elif entries:
            entries[-1]["lines"].append(line)
        else:
            # Output from before the first recognizable entry — the runtime's own startup lines.
            entries.append({"level": None, "lines": [line]})

    return entries


def command_logs(arguments):
    remote = f"podman logs --tail {arguments.tail} "

    if arguments.since:
        remote += f"--since {arguments.since} "

    if arguments.follow:
        # Streaming and entry-grouping do not mix: the grouper cannot know an entry has ended until
        # the next one starts, so following prints the raw stream and ignores the filters.
        run(["ssh", "-t", SSH_HOST, remote + f"--follow {CONTAINER}"], check=False, capture=False)
        return

    result = run(["ssh", SSH_HOST, remote + f"{CONTAINER} 2>&1"], check=False)

    if result.returncode != 0:
        refuse(f"could not read the log of {CONTAINER} on {SSH_HOST}: "
               f"{(result.stderr or result.stdout).strip()}")

    entries = log_entries(result.stdout)

    if arguments.grep:
        needle = arguments.grep.lower()
        entries = [e for e in entries if needle in "\n".join(e["lines"]).lower()]

    if arguments.level:
        minimum = LEVELS.index(LEVEL_ALIASES[arguments.level.lower()])
        entries = [e for e in entries
                   if e["level"] and LEVELS.index(e["level"]) >= minimum]

    if not entries:
        refuse("nothing matched")

    for entry in entries if arguments.all else entries[-arguments.lines:]:
        print("\n".join(entry["lines"]))


def registry_url(path):
    """An address of the registry's API; the tunnel has to be open when the registry is only reachable through one."""
    if TUNNEL:
        return f"http://127.0.0.1:{TUNNEL.split(':')[0]}/v2/{IMAGE}/{path}"

    return f"https://{REGISTRY}/v2/{IMAGE}/{path}"


def registry_tags():
    with urllib.request.urlopen(registry_url("tags/list"), timeout=15) as response:
        return json.load(response).get("tags") or []


def ordered(tags):
    """The floating tag first, then the timestamps newest to oldest — they sort as strings by construction."""
    return [tag for tag in [FLOATING_TAG] + sorted((t for t in tags if t != FLOATING_TAG), reverse=True) if tag in tags]


def command_tags(_arguments):
    try:
        with tunnel():
            tags = registry_tags()
    except OSError as error:
        refuse(f"{REGISTRY} unreachable: {error}")

    for tag in ordered(tags):
        print(tag)


# Accepting every manifest type makes the registry answer with the digest it stores, whatever podman pushed.
MANIFEST_TYPES = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
BUILD_TAG = re.compile(r"^\d{8}_\d{4}_[0-9a-f]{8}(_dirty)?$")


def manifest_digest(tag):
    request = urllib.request.Request(registry_url(f"manifests/{tag}"), method="HEAD", headers={"Accept": MANIFEST_TYPES})

    with urllib.request.urlopen(request, timeout=15) as response:
        return response.headers["Docker-Content-Digest"]


def prune_registry(keep):
    """Keeps the floating tag and the newest `keep` build tags; deletes the rest and takes the space back.

    A registry deletes by digest, so a tag whose digest a kept tag shares is not touched. The blobs only leave the disk
    with the garbage collection, which runs with the registry stopped so that no push can race it.
    """
    registry = setting("registry_unit", "registry")

    try:
        with tunnel():
            tags = registry_tags()
            builds = sorted((t for t in tags if BUILD_TAG.match(t)), reverse=True)
            kept = [t for t in tags if t == FLOATING_TAG] + builds[:keep]
            kept_digests = {manifest_digest(tag) for tag in kept}
            doomed = {}

            for tag in builds[keep:]:
                digest = manifest_digest(tag)

                if digest not in kept_digests:
                    doomed[digest] = tag

            for digest, tag in doomed.items():
                request = urllib.request.Request(registry_url(f"manifests/{digest}"), method="DELETE")
                urllib.request.urlopen(request, timeout=30).close()
                print(f"deleted {tag}")
    except OSError as error:
        refuse(f"{REGISTRY} unreachable or refused the delete: {error}")

    if not doomed:
        print(f"nothing to prune: {len(builds)} build tag(s), keeping {keep}")
        return

    print(f"collecting garbage on {SSH_HOST} (registry stopped for the length of it) ...")
    run(["ssh", SSH_HOST, "systemctl", "--user", "stop", registry], capture=False)

    try:
        run(["ssh", SSH_HOST, "podman", "run", "--rm", "-v", f"{setting('registry_volume', 'registry-data')}:/var/lib/registry:Z",
             "-e", "REGISTRY_STORAGE_DELETE_ENABLED=true", "docker.io/library/registry:2",
             "garbage-collect", "/etc/docker/registry/config.yml"], capture=False)
    finally:
        run(["ssh", SSH_HOST, "systemctl", "--user", "start", registry], capture=False)


def command_prune(arguments):
    keep = arguments.keep if arguments.keep is not None else setting("keep_tags")

    if keep is None:
        refuse("how many tags to keep? --keep N, or keep_tags in the [deploy] block of project.toml")

    prune_registry(int(keep))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    # Read before this parser runs (see _target); listed here so --help shows it and argparse accepts it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--target", help="Deploy to a [deploy.targets.NAME] of project.toml instead of the [deploy] host")

    deploy = commands.add_parser("deploy", help="build, push and restart", parents=[common])
    deploy.add_argument("--no-push", action="store_true", help="build only, leave the registry alone")
    deploy.add_argument("--no-restart", action="store_true", help="push, but leave the server running what it runs")
    deploy.add_argument("--no-prune", action="store_true", help="leave the old tags in the registry (keep_tags)")
    deploy.set_defaults(function=command_deploy)

    reset = commands.add_parser("reset", help="compose target: drop the stack and its volumes, start it again empty",
                                parents=[common])
    reset.add_argument("--keep-down", action="store_true", help="drop it, but do not start it again")
    reset.set_defaults(function=command_reset)

    recreate = commands.add_parser("recreate", help="compose target: build the stack again, keep the volumes",
                                   parents=[common])
    recreate.set_defaults(function=command_recreate)

    status = commands.add_parser("status", help="what the server runs right now", parents=[common])
    status.set_defaults(function=command_status)

    tags = commands.add_parser("tags", help="tags in the registry, newest first", parents=[common])
    tags.set_defaults(function=command_tags)

    prune = commands.add_parser("prune", help="delete all but the newest build tags from the registry", parents=[common])
    prune.add_argument("--keep", type=int, help="How many build tags to keep (default: keep_tags of project.toml)")
    prune.set_defaults(function=command_prune)

    logs = commands.add_parser("logs", help="what the container logged", parents=[common])
    logs.add_argument("-n", "--lines", type=int, default=50, help="How many entries to show (default: 50)")
    logs.add_argument("--all", action="store_true", help="Show every matching entry")
    logs.add_argument("--grep", help="Only entries containing this text, case-insensitive")
    logs.add_argument("--level", choices=["Trace", "Debug", "Info", "Warn", "Error", "Critical"],
                      help="This level and above")
    logs.add_argument("--since", help="Only what was logged since then, as podman takes it: 10m, 1h, 2026-08-23")
    logs.add_argument("--follow", action="store_true", help="Stream it; filters do not apply")
    logs.add_argument("--tail", type=int, default=5000,
                      help="Raw lines to fetch before filtering (default: 5000)")
    logs.set_defaults(function=command_logs)

    arguments = parser.parse_args()
    arguments.function(arguments)


if __name__ == "__main__":
    main()
