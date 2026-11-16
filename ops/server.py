#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Looking after a server over ssh: logs, systemd, podman, new containers. One tool, several machines.

The machines are in `~/.config/devscripts/servers.toml`, one table each: the ssh alias and what differs
between them. Not per-project: a server is a machine, so this is called by its path, with the machine first.
A three-line wrapper per machine (`main("web")`) saves typing the name.

    [web]
    ssh = "web"                      the alias in ~/.ssh/config, never the address
    data_directory = "services"      where `new` keeps a service's data, relative to the home directory
    after_new = "a proxy host and a DNS record, if it needs a name"     optional reminder after `new`
    timezone = "Europe/Berlin"       optional, TZ for the containers `new` creates

    python3 ~/dev/tools/devscripts/ops/server.py web logs nginx                 last 60 entries of the last 6 hours
    python3 ~/dev/tools/devscripts/ops/server.py web logs nginx -n 200 --since "-30min"
    python3 ~/dev/tools/devscripts/ops/server.py web logs app --grep "proxy|forward" --exclude health
    python3 ~/dev/tools/devscripts/ops/server.py web ps                         containers, and failed units
    python3 ~/dev/tools/devscripts/ops/server.py web ps shop -a --ports         one stack's containers, also the exited, with ports
    python3 ~/dev/tools/devscripts/ops/server.py web logs shop_app_1 --container --grep "error|warn"
                                                                                a container that is no unit (a compose stack)

    server.py web status UNIT | cat UNIT | timers     what systemd says; `cat` shows the generated unit
    server.py web restart UNIT [UNIT ...]             also start, stop; `--wait` until it is active
    server.py web reload                              daemon-reload, which re-runs the quadlet generator
    server.py web stats | exec CONTAINER CMD ...      podman stats --no-stream; a command in a container
    server.py web put FILE dir/ | get dir/f .         a file to / from the server, relative to its home directory
    server.py web update [--apply]                    podman auto-update, a dry run unless --apply
    server.py web install FILE.container [--restart]  copy a unit to the server, daemon-reload, optionally restart
    server.py web new NAME --image IMG [--port 1234:8080] [--env K=V] [--volume config:/config] [--network N] [--dry-run]
                                                      a new rootless service

Everything is rootless: sudo would ask for a password over ssh, so a rootful unit
can be read with `logs --system` where the journal allows it, and is otherwise changed by hand.

`logs` takes the *unit* name, which is not always the container name. Always the ssh alias, never the address:
the alias carries the user and the key.

Filtering works on *entries*, not lines. A Python traceback arrives as one journal line per traceback
line, only the first carrying the timestamp and level, so a line-wise `--grep` prints either the message
without its trace or the trace without its message. Podman's own `container died/create/start` lines carry
the image's whole label list; they are cut to `--width` characters instead of dropped, since a restart is
exactly what one looks for.
"""

import argparse
import re
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

SERVERS = Path.home() / ".config" / "devscripts" / "servers.toml"
SERVER = {"name": "", "ssh": "", "data_directory": "services", "after_new": "", "timezone": ""}  # set by configure()
UNIT_DIRECTORY = "~/.config/containers/systemd"
PREFIX = re.compile(r"^\S+ +\S+ \S+ \S+ [^\s:]+: ")
CONTINUATION = re.compile(r"^(\s|Traceback|\S*(Error|Exception)\b)")
TIMESTAMPED = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def configure(name):
    """Load one machine from servers.toml into SERVER; refuse an unknown name, naming the known ones."""
    if not SERVERS.is_file():
        sys.exit(f"no {SERVERS}: one [name] table per machine with at least `ssh` (see --help)")
    servers = tomllib.loads(SERVERS.read_text(encoding="utf-8"))
    if name not in servers:
        sys.exit(f"no server '{name}' in {SERVERS}; known: {', '.join(servers)}")
    if "ssh" not in servers[name]:
        sys.exit(f"[{name}] in {SERVERS} has no `ssh` key")
    SERVER.update(servers[name], name=name)


def ssh(command, check=True):
    """Run a command on the server; exit with its error unless `check` is off, then return stdout either way."""
    result = subprocess.run(["ssh", SERVER["ssh"], command], capture_output=True, text=True)
    if check and result.returncode != 0:
        sys.exit(result.stderr.strip() or f"ssh {SERVER['ssh']} failed with {result.returncode}")
    return result.stdout + ("" if check else result.stderr)


def entries(lines):
    """Group journal lines into entries: a line starts a new one unless its message continues a traceback.

    ponytail: heuristic on the message text; a continuation line that starts with a word other than
    Traceback/*Error/*Exception becomes its own entry. Good for Python services, not a general parser.
    """
    grouped = []
    for line in lines:
        message = PREFIX.sub("", line, count=1)
        if grouped and CONTINUATION.match(message) and not TIMESTAMPED.match(message):
            grouped[-1].append(line)
        else:
            grouped.append([line])
    return grouped


def select(grouped, grep, exclude):
    """Keep entries whose text matches `grep` and not `exclude`, case-insensitively."""
    chosen = []
    for entry in grouped:
        text = "\n".join(entry)
        if grep and not re.search(grep, text, re.IGNORECASE):
            continue
        if exclude and re.search(exclude, text, re.IGNORECASE):
            continue
        chosen.append(entry)
    return chosen


def logs(arguments):
    if arguments.container:
        # podman takes a duration (`6h`, `30m`), not journalctl's `-6h` / `-30min`.
        since = re.sub(r"min$", "m", arguments.since.lstrip("-"))
        command = f"podman logs --since {shlex.quote(since)} {shlex.quote(arguments.unit)} 2>&1"
    else:
        scope = "" if arguments.system else "--user "
        command = f"journalctl {scope}-u {shlex.quote(arguments.unit)} --no-pager --since {shlex.quote(arguments.since)}"
    chosen = select(entries(ssh(command).splitlines()), arguments.grep, arguments.exclude)
    for entry in chosen[-arguments.lines:]:
        for line in entry:
            print(line if len(line) <= arguments.width else line[: arguments.width] + " ...")
    if not chosen:
        sys.exit(1)


def put(arguments):
    """Copy a file to the server; the destination is relative to the home directory, and a missing directory is made."""
    remote = arguments.destination
    if remote.endswith("/"):
        remote += Path(arguments.file).name
    ssh(f"mkdir -p {shlex.quote(str(Path(remote).parent))}")
    subprocess.run(["scp", "-q", arguments.file, f"{SERVER['ssh']}:{remote}"], check=True)
    print(f"{arguments.file} -> {SERVER['name']}:{remote}")


def get(arguments):
    """Copy a file from the server (relative to the home directory) to a local path."""
    subprocess.run(["scp", "-q", f"{SERVER['ssh']}:{arguments.source}", arguments.destination], check=True)
    print(f"{SERVER['name']}:{arguments.source} -> {arguments.destination}")


def ps(arguments):
    flags = ("-a " if arguments.all else "") + (f"--filter name={shlex.quote(arguments.filter)} " if arguments.filter else "")
    columns = "{{.Names}}\\t{{.Status}}" + ("\\t{{.Ports}}" if arguments.ports else "")
    print(ssh(f"podman ps {flags}--format '{columns}'"), end="")
    if arguments.filter:
        return
    print("--- failed units")
    print(ssh("systemctl --user --failed --no-legend; systemctl --failed --no-legend"), end="")


def systemctl(*arguments):
    return "systemctl --user " + " ".join(shlex.quote(argument) for argument in arguments)


def status(arguments):
    print(ssh(systemctl("status", "--no-pager", arguments.unit), check=False), end="")


def cat(arguments):
    print(ssh(systemctl("cat", arguments.unit)), end="")


def timers(_):
    print(ssh(systemctl("list-timers", "--no-pager"), check=False), end="")


def reload(_):
    ssh(systemctl("daemon-reload"))
    print("daemon-reload done")


def act(arguments):
    """start, stop or restart units; with --wait, fail unless every one of them ends up active."""
    for unit in arguments.units:
        ssh(systemctl(arguments.command, unit))
        print(f"{arguments.command} {unit}")
    if arguments.command == "stop" or not arguments.wait:
        return
    for unit in arguments.units:
        for _ in range(arguments.wait):
            state = ssh(systemctl("is-active", unit), check=False).strip()
            if state != "activating":
                break
            time.sleep(1)
        print(f"{unit}: {state}")
        if state != "active":
            sys.exit(1)


def stats(_):
    print(ssh("podman stats --no-stream --format 'table {{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}'"), end="")


def execute(arguments):
    print(ssh("podman exec " + shlex.join([arguments.container, *arguments.command]), check=False), end="")


def update(arguments):
    print(ssh("podman auto-update" + ("" if arguments.apply else " --dry-run"), check=False), end="")


def volume_line(data_directory, name, volume):
    """`DIR:TARGET[:OPTIONS]` as a Volume= line; podman takes the options comma-separated, Z among them."""
    source, target = volume.split(":", 1)
    target, _, options = target.partition(":")
    options = ",".join(filter(None, [options, "Z"]))
    return f"Volume=%h/{data_directory}/{name}/{source}:{target}:{options}"


def render_unit(name, image, description, ports, environment, volumes, networks, autoupdate, data_directory):
    """The quadlet text for a new rootless service: data under data_directory, auto-update, restart on failure."""
    container = [f"Image={image}", f"ContainerName={name}"]
    if autoupdate:
        container.append("AutoUpdate=registry")
    container += [f"PublishPort={port}" for port in ports]
    timezone = [f"TZ={SERVER['timezone']}"] if SERVER["timezone"] else []
    container += [f"Environment={pair}" for pair in [*timezone, *environment]]
    container += [f"Network={network}" for network in networks]
    container += [volume_line(data_directory, name, volume) for volume in volumes]
    return "\n".join(
        [
            "[Unit]",
            f"Description={description or name}",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Container]",
            *container,
            "",
            "[Service]",
            "Restart=always",
            "TimeoutStartSec=900",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def new(arguments):
    text = render_unit(arguments.name, arguments.image, arguments.description, arguments.port,
                       arguments.env, arguments.volume, arguments.network, not arguments.no_autoupdate,
                       SERVER["data_directory"])
    if arguments.dry_run:
        print(text, end="")
        return
    target = f"{UNIT_DIRECTORY}/{arguments.name}.container"
    if ssh(f"test -e {target} && echo exists", check=False).strip() == "exists":
        sys.exit(f"{target} exists on {SERVER['name']}; edit it there, or use `install` to replace it deliberately")
    for volume in arguments.volume:
        ssh(f"mkdir -p ~/{SERVER['data_directory']}/{shlex.quote(arguments.name)}/{shlex.quote(volume.split(':', 1)[0])}")
    subprocess.run(["ssh", SERVER["ssh"], f"cat > {target}"], input=text, text=True, check=True)
    reload(None)
    arguments.units, arguments.command, arguments.wait = [arguments.name], "start", 120
    act(arguments)
    if SERVER["after_new"]:
        print("next: " + SERVER["after_new"])


def install(arguments):
    """Copy a .container file into the unit directory, daemon-reload, optionally restart that unit."""
    subprocess.run(["scp", "-q", arguments.file, f"{SERVER['ssh']}:{UNIT_DIRECTORY}/"], check=True)
    reload(None)
    if arguments.restart:
        arguments.units = [arguments.file.rsplit("/", 1)[-1].removesuffix(".container")]
        arguments.command, arguments.wait = "restart", 120
        act(arguments)


def main(server=None):
    """Run for `server`, or for the first command-line word when called as `server.py NAME ...`."""
    if server is None:
        if len(sys.argv) < 2:
            sys.exit("usage: server.py NAME COMMAND ...   (names: the tables of ~/.config/devscripts/servers.toml)")
        server = sys.argv.pop(1)
    configure(server)
    parser = argparse.ArgumentParser(prog=f"{server}.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("logs")
    sub.add_argument("unit")
    sub.add_argument("-n", "--lines", type=int, default=60, help="how many entries to show (default 60)")
    sub.add_argument("--since", default="-6h", help="journalctl --since (default -6h)")
    sub.add_argument("--grep", help="entries whose text matches this regex")
    sub.add_argument("--exclude", help="drop entries matching this regex")
    sub.add_argument("--width", type=int, default=220, help="cut longer lines (default 220)")
    sub.add_argument("--system", action="store_true", help="a rootful unit, not a user unit")
    sub.add_argument("--container", action="store_true",
                     help="the argument is a container name and its output is `podman logs`: for a container without a unit")
    sub.set_defaults(run=logs)
    sub = commands.add_parser("put")
    sub.add_argument("file")
    sub.add_argument("destination", help="relative to the home directory; a trailing / keeps the file's name")
    sub.set_defaults(run=put)
    sub = commands.add_parser("get")
    sub.add_argument("source", help="relative to the home directory")
    sub.add_argument("destination")
    sub.set_defaults(run=get)
    sub = commands.add_parser("ps")
    sub.add_argument("filter", nargs="?", help="only containers whose name contains this; no failed-units list then")
    sub.add_argument("-a", "--all", action="store_true", help="also the ones that exited (a compose migrator)")
    sub.add_argument("--ports", action="store_true", help="a column with the published ports")
    sub.set_defaults(run=ps)
    sub = commands.add_parser("status")
    sub.add_argument("unit")
    sub.set_defaults(run=status)
    sub = commands.add_parser("cat")
    sub.add_argument("unit")
    sub.set_defaults(run=cat)
    commands.add_parser("timers").set_defaults(run=timers)
    commands.add_parser("reload").set_defaults(run=reload)
    for name in ("start", "stop", "restart"):
        sub = commands.add_parser(name)
        sub.add_argument("units", nargs="+")
        sub.add_argument("--wait", type=int, nargs="?", const=120, default=0, metavar="SECONDS",
                         help="then wait until the unit is active (default 120 seconds), fail if it is not")
        sub.set_defaults(run=act, command=name)
    commands.add_parser("stats").set_defaults(run=stats)
    sub = commands.add_parser("exec")
    sub.add_argument("container")
    sub.add_argument("command", nargs=argparse.REMAINDER)
    sub.set_defaults(run=execute)
    sub = commands.add_parser("update")
    sub.add_argument("--apply", action="store_true", help="really update (a dry run otherwise)")
    sub.set_defaults(run=update)
    sub = commands.add_parser("install")
    sub.add_argument("file")
    sub.add_argument("--restart", action="store_true")
    sub.set_defaults(run=install)
    sub = commands.add_parser("new")
    sub.add_argument("name")
    sub.add_argument("--image", required=True)
    sub.add_argument("--description")
    sub.add_argument("--port", action="append", default=[], metavar="HOST:CONTAINER")
    sub.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    sub.add_argument("--volume", action="append", default=[], metavar="DIR:TARGET",
                     help="<data directory of this server>/NAME/DIR, mounted at TARGET")
    sub.add_argument("--network", action="append", default=[], metavar="NAME", help="join a Podman network (repeatable)")
    sub.add_argument("--no-autoupdate", action="store_true")
    sub.add_argument("--dry-run", action="store_true", help="print the unit, change nothing")
    sub.set_defaults(run=new)
    arguments = parser.parse_args()
    arguments.run(arguments)


if __name__ == "__main__":
    main()
