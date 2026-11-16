#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lint and test the PHP part of a project.

    python3 Scripts/php.py lint      php -l on every .php file under [php] sources
    python3 Scripts/php.py test      vendor/bin/phpunit, else the system phpunit on tests/; skipped when neither
    python3 Scripts/php.py verify    lint, then test

PHP is the local `php` when there is one, otherwise a throw-away podman container of [php] image
(default docker.io/library/php:8.5-cli) with the project mounted - so a machine needs no PHP installed, and
the image can match the server's version. `vendor/` is the project's: composer is not run from here.

[php]
directory = "."                 # holds composer.json; default "."
sources = ["lib", "appinfo"]    # folders to lint; default ["lib", "appinfo", "templates", "tests"]
image = "docker.io/library/php:8.5-cli"
"""
import argparse
import shlex
import shutil
import subprocess
import sys

from project import project

DIRECTORY = project.path("php.directory", ".")
SOURCES = project.get("php.sources", ["lib", "appinfo", "templates", "tests"])
IMAGE = project.get("php.image", "docker.io/library/php:8.5-cli")


def run(command):
    """A shell command inside the PHP directory, in the container when this machine has no php."""
    if shutil.which("php"):
        full = ["sh", "-c", command]
    else:
        full = ["podman", "run", "--rm", "-v", f"{DIRECTORY}:/app:Z", "-w", "/app", IMAGE, "sh", "-c", command]

    print("+ " + command, flush=True)

    return subprocess.run(full, cwd=DIRECTORY).returncode


def lint():
    folders = " ".join(shlex.quote(folder) for folder in SOURCES if (DIRECTORY / folder).is_dir())

    if not folders:
        print("- lint: none of the [php] sources exist, skipped")

        return 0

    # stdout is only "No syntax errors" per file; errors go to stderr and stay visible.
    return run(f"find {folders} -name '*.php' -print0 | xargs -0 -n1 php -d display_errors=stderr -d log_errors=0 -l >/dev/null")


def test():
    if (DIRECTORY / "vendor" / "bin" / "phpunit").is_file():
        return run("vendor/bin/phpunit")

    # A system phpunit (dnf install phpunit) only counts when there is something to run and php is local.
    if shutil.which("phpunit") and shutil.which("php") and (DIRECTORY / "tests").is_dir():
        return run("phpunit tests")

    print("- test: no vendor/bin/phpunit (or system phpunit and a tests/ folder), skipped")

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["lint", "test", "verify"])
    command = parser.parse_args().command
    steps = {"lint": [lint], "test": [test], "verify": [lint, test]}[command]

    if any(step() != 0 for step in steps):
        sys.exit(1)

    print(f"{command} ok")


if __name__ == "__main__":
    main()
