#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""RPMs of this project, and publishing them to an RPM repository reached over ssh.

    python3 Scripts/rpm.py build [NAME ...]              build, stage, package into ~/.cache/devscripts/rpm
    python3 Scripts/rpm.py publish [NAME ...]            the same, then copy them to the repository, where they are signed
    python3 Scripts/rpm.py publish [NAME ...] --dry-run  build and say what would be sent, send nothing
    python3 Scripts/rpm.py publish [NAME ...] --force    send a package even when it did not change
    python3 Scripts/rpm.py spec [NAME]                   print the spec that would be used

Without NAME every package of the project. One way for every language: the build commands put the files
into a staging tree, laid out as on the target system ({stage}/usr/bin/..., cmake --install with
DESTDIR, dotnet publish -o {stage}/usr/lib/<name>); `files` and `links` add to it. rpm.py then writes a
minimal spec around that tree and runs rpmbuild, which still finds the libraries a binary needs itself.

    [rpm]                                                    one package: its keys right here, with `name`
    name = "gputaskman"
    summary = "An htop for what is inside the GPU"
    license = "MIT"
    url = "https://github.com/..."                          optional
    version_file = "Cargo.toml"                              Cargo.toml, CMakeLists.txt or a .csproj/.props
    build = ["cargo build --release -p gputaskman"]          shell commands in the repository; {stage}, {version}
    files = { "target/release/gputaskman" = "/usr/bin/gputaskman" }   repository path (file or folder) -> target
    links = { "/usr/bin/app" = "/usr/lib/app/App" }          optional symbolic links
    requires = ["dotnet-runtime-10.0"]                       optional, beyond what rpmbuild finds
    recommends = ["qt6-qtbase-postgresql"]                   optional, installed by dnf unless declined
    arch = "noarch"                                          optional, for packages without native binaries
    post = "udevadm control --reload-rules || :"             optional scriptlets: %post, %preun, %postun
    preun = "if [ $1 -eq 0 ]; then ...; fi"                  ($1 is how many copies remain; 0 = removal)
    repository = "host:path/to/repo"                         for publish; DEVSCRIPTS_RPM_REPOSITORY otherwise

    [rpm.packages.NAME]                                      several packages: one table each; the keys of
                                                             [rpm] itself (license, url, ...) are their defaults

The release is a timestamp, so every build is newer than the last one and `dnf upgrade` takes it. Signing
happens on the server: publish copies the packages to <path>/incoming/ and runs <path>/publish.sh there,
which signs them and updates the metadata, so the private key never leaves it.

`publish` sends only what changed. After staging, a package gets a fingerprint - every file's path, mode and
content, every link's target, the spec without its release - and one that matches what was last sent from
this machine (~/.cache/devscripts/rpm/published.json) is neither packaged nor sent. Otherwise a project with
several packages would offer an update of each one every time, and dnf would reinstall a package that is
byte for byte the same. A build that is not reproducible (a timestamp compiled into a binary) counts as a
change: the fingerprint is about the bytes.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from project import project

OUTPUT = Path.home() / ".cache" / "devscripts" / "rpm"
PUBLISHED = OUTPUT / "published.json"
SCRIPTLETS = ("post", "preun", "postun")


def read_version(text, filename):
    """The version a project file declares, or None.

    Cargo.toml: [package] or [workspace.package] version (a member with `version.workspace = true` has
    none of its own: name the workspace's Cargo.toml). CMakeLists.txt: project(... VERSION x.y.z).
    .csproj / .props: <Version>.
    """
    if filename.endswith(".toml"):
        data = tomllib.loads(text)
        version = data.get("package", {}).get("version") or data.get("workspace", {}).get("package", {}).get("version")
        return version if isinstance(version, str) else None

    if filename == "CMakeLists.txt":
        match = re.search(r"project\s*\([^)]*\bVERSION\s+([0-9][0-9A-Za-z.]*)", text, re.IGNORECASE)
        return match.group(1) if match else None

    match = re.search(r"<Version>\s*([^<\s]+)\s*</Version>", text)
    return match.group(1) if match else None


def package_configs(rpm):
    """The packages of an [rpm] table: itself when it has a `name`, else one per [rpm.packages.NAME], each
    on top of the shared keys of [rpm]."""
    if "packages" not in rpm:
        return [dict(rpm)] if "name" in rpm else []

    shared = {key: value for key, value in rpm.items() if key != "packages"}
    return [{**shared, **settings, "name": name} for name, settings in rpm["packages"].items()]


def release_stamp(moment):
    return moment.strftime("%Y%m%d%H%M")


def package_paths(stage, name):
    """(paths, directories) for %files: every file and link, and the directories the package owns.

    A directory is the package's own when it carries the package's name (/usr/lib/gputaskman-qt,
    /usr/share/gputaskman-qt); everything else (/usr/bin, /usr/share/applications) belongs to the system
    and is not claimed.
    """
    paths, directories = [], []

    for current, folders, files in os.walk(stage):
        relative = "/" + os.path.relpath(current, stage) if current != str(stage) else ""

        for folder in list(folders):
            full = os.path.join(current, folder)
            if folder == name:
                directories.append(f"{relative}/{folder}")
                folders.remove(folder)  # owned as a whole
            elif os.path.islink(full):
                paths.append(f"{relative}/{folder}")
                folders.remove(folder)

        paths += [f"{relative}/{file}" for file in files]

    return sorted(paths), sorted(directories)


def quote(path):
    return '"' + path.replace("%", "%%") + '"'


def spec_text(meta, stage, paths, directories):
    """A spec that packages a finished tree: no %prep, no %build, %install copies the stage."""
    lines = [
        "%global debug_package %{nil}",
        "%global _build_id_links none",
        # Fedora's default takes the build date from %changelog, which a generated spec does not have.
        "%global source_date_epoch_from_changelog 0",
        f"Name: {meta['name']}",
        f"Version: {meta['version']}",
        f"Release: {meta['release']}",
        f"Summary: {meta['summary']}",
        f"License: {meta['license']}",
    ]
    if meta.get("url"):
        lines.append(f"URL: {meta['url']}")
    if meta.get("arch"):
        lines.append(f"BuildArch: {meta['arch']}")
    lines += [f"Requires: {requirement}" for requirement in meta.get("requires", [])]
    lines += [f"Recommends: {recommendation}" for recommendation in meta.get("recommends", [])]
    lines += [
        "",
        "%description",
        meta.get("description") or meta["summary"],
        "",
        "%install",
        f"cp -a {shlex.quote(str(stage))}/. %{{buildroot}}/",
        "",
    ]
    for scriptlet in SCRIPTLETS:
        if meta.get(scriptlet):
            lines += [f"%{scriptlet}", meta[scriptlet].strip(), ""]
    lines += [
        "%files",
        *(quote(directory) for directory in directories),
        *(quote(path) for path in paths),
        "",
    ]
    return "\n".join(lines)


def fingerprint(stage, meta, paths, directories):
    """What a package is, as a hash: the staged tree (paths, modes, contents, link targets) and its spec.

    The release is left out, it is new on every build; so is the staging folder's own name, which the spec's
    %install copies from.
    """
    digest = hashlib.sha256()
    digest.update(spec_text({**meta, "release": ""}, Path("/stage"), paths, directories).encode("utf-8"))

    for current, folders, files in os.walk(stage):
        folders.sort()
        for name in sorted(folders + files):
            full = Path(current) / name
            relative = full.relative_to(stage).as_posix()
            if full.is_symlink():
                digest.update(f"\0link {relative} {os.readlink(full)}".encode("utf-8"))
            elif full.is_dir():
                digest.update(f"\0dir {relative} {full.stat().st_mode:o}".encode("utf-8"))
            else:
                digest.update(f"\0file {relative} {full.stat().st_mode:o} ".encode("utf-8"))
                digest.update(hashlib.sha256(full.read_bytes()).digest())

    return digest.hexdigest()


def load_published():
    """Per package: the fingerprint and the file of what was last sent from here."""
    try:
        return json.loads(PUBLISHED.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_published(records):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    PUBLISHED.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(command, **kwargs):
    completed = subprocess.run(command, **kwargs)
    if completed.returncode != 0:
        shown = command if isinstance(command, str) else shlex.join(command)
        sys.exit(f"refused: `{shown}` failed with exit code {completed.returncode}")
    return completed


def require(config, key):
    if key not in config:
        sys.exit(f"refused: {project.config_file} has no {key!r} for the package {config.get('name', '?')!r}")
    return config[key]


def metadata(config):
    version_file = project.root / require(config, "version_file")
    version = read_version(version_file.read_text(encoding="utf-8"), version_file.name)
    if not version:
        sys.exit(f"refused: no version in {version_file.relative_to(project.root)}")

    return {
        "name": require(config, "name"),
        "version": version.replace("-", "~"),  # 1.0.0-beta sorts before 1.0.0 only as 1.0.0~beta
        "release": release_stamp(datetime.datetime.now()),
        "summary": require(config, "summary"),
        "license": require(config, "license"),
        "url": config.get("url"),
        "arch": config.get("arch"),
        "requires": config.get("requires", []),
        "recommends": config.get("recommends", []),
        "description": config.get("description"),
        **{scriptlet: config.get(scriptlet) for scriptlet in SCRIPTLETS},
    }


def stage_files(stage, meta, config):
    for command in config.get("build", []):
        command = command.format(stage=shlex.quote(str(stage)), version=meta["version"])
        print(f"$ {command}", flush=True)
        run(command, shell=True, cwd=project.root)

    for source, target in config.get("files", {}).items():
        origin = project.root / source
        destination = stage / target.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, destination, symlinks=True, dirs_exist_ok=True)
        elif origin.is_file():
            shutil.copy2(origin, destination)
        else:
            sys.exit(f"refused: rpm.files names {source}, which the build did not produce")

    for link, target in config.get("links", {}).items():
        path = stage / link.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to(target)


def build(config, print_spec=False, published=None):
    """Build and package one package: (its name, the finished .rpm, its fingerprint).

    With `published` (load_published()), a package whose fingerprint is the one sent last is not packaged:
    the .rpm is None.
    """
    if shutil.which("rpmbuild") is None:
        sys.exit("refused: rpmbuild not found - sudo dnf install rpm-build")

    meta = metadata(config)
    name = meta["name"]
    with tempfile.TemporaryDirectory(prefix="rpm-") as scratch:
        scratch = Path(scratch)
        stage = scratch / "stage"
        stage.mkdir()
        stage_files(stage, meta, config)
        paths, directories = package_paths(stage, name)
        if not paths and not directories:
            sys.exit(f"refused: the staging tree of {name} is empty - check its build and files")

        spec = scratch / f"{name}.spec"
        spec.write_text(spec_text(meta, stage, paths, directories), encoding="utf-8")
        if print_spec:
            print(spec.read_text(encoding="utf-8"))
            return name, None, None

        mark = fingerprint(stage, meta, paths, directories)
        last = (published or {}).get(name, {})
        if last.get("fingerprint") == mark:
            print(f"unchanged {name}: the same as {last.get('rpm', 'what was sent last')}, not packaged", flush=True)
            return name, None, mark

        run(["rpmbuild", "--quiet", "-bb", "--define", f"_topdir {scratch / 'rpmbuild'}", str(spec)])
        built = next((scratch / "rpmbuild" / "RPMS").rglob("*.rpm"))
        OUTPUT.mkdir(parents=True, exist_ok=True)
        result = OUTPUT / built.name
        shutil.copy2(built, result)

    print(f"built {result}", flush=True)
    return name, result, mark


def publish(configs, dry_run, force=False):
    repository = configs[0].get("repository") or os.environ.get("DEVSCRIPTS_RPM_REPOSITORY")
    if not repository:
        sys.exit("refused: no repository to publish to - set [rpm] repository = \"host:path\" in project.toml "
                 "or DEVSCRIPTS_RPM_REPOSITORY")

    host, _, directory = repository.partition(":")

    records = load_published()
    results = [build(config, published=None if force else records) for config in configs]
    packages = [(name, path, mark) for name, path, mark in results if path is not None]
    if not packages:
        print("nothing changed since the last publish - nothing sent (--force sends anyway)")
        return

    names = " ".join(path.name for _, path, _ in packages)
    if dry_run:
        print(f"would copy {names} to {host}:{directory}/incoming/ and run {directory}/publish.sh there")
        return

    run(["scp", "-q", *(str(path) for _, path, _ in packages), f"{host}:{directory}/incoming/"])
    run(["ssh", host, f"{directory}/publish.sh"])

    # Only once the server took them: a failed send leaves them to be sent next time.
    for name, path, mark in packages:
        records[name] = {"fingerprint": mark, "rpm": path.name}
    save_published(records)


def selected(configs, names):
    if not configs:
        sys.exit(f"refused: {project.config_file} has no [rpm] block with a package")
    if not names:
        return configs

    known = {config["name"]: config for config in configs}
    unknown = [name for name in names if name not in known]
    if unknown:
        sys.exit(f"refused: no package {', '.join(unknown)} here; known: {', '.join(known)}")
    return [known[name] for name in names]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="build and package into ~/.cache/devscripts/rpm")
    build_parser.add_argument("names", nargs="*", help="only these packages")
    spec_parser = commands.add_parser("spec", help="print the spec, package nothing")
    spec_parser.add_argument("names", nargs="*", help="only these packages")
    publish_parser = commands.add_parser("publish", help="build, copy to the repository, sign there")
    publish_parser.add_argument("names", nargs="*", help="only these packages")
    publish_parser.add_argument("--dry-run", action="store_true", help="build, but send nothing")
    publish_parser.add_argument("--force", action="store_true",
                                help="send every package, also those that did not change since the last publish")
    arguments = parser.parse_args()

    configs = selected(package_configs(project.get("rpm", {})), arguments.names)
    if arguments.command == "publish":
        publish(configs, arguments.dry_run, arguments.force)
        return

    for config in configs:
        build(config, print_spec=arguments.command == "spec")


if __name__ == "__main__":
    main()
