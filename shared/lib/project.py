#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""What every script here needs to know about the repository it was called from.

The scripts in this checkout are the same in every project; the four or five values that differ are
in `Scripts/project.toml` of each one. This module finds that file and answers questions about it.

    from project import project

    project.root                        the repository the script is working on
    project.require("dotnet.solution")  refuses, naming the key and the file, when it is missing
    project.get("deploy.unit", "app")   optional, with a default
    project.path("dotnet.ef_project")   the same value joined onto the repository root
    project.env("DB_HOST", "localhost") the {env_prefix}_DB_HOST environment variable

A missing key is a refusal rather than a traceback: the person running this is holding a project that
has not been configured for the command, and the useful answer names the key to add.
"""
import os
import sys
import tomllib
from pathlib import Path

CONFIG_NAME = "project.toml"


def _find_root():
    """The repository being worked on.

    The shim in a project's Scripts/ folder names it outright. Without one - running a script out of
    this checkout directly - the working directory is searched upwards, which is what makes
    `python3 ~/dev/tools/devscripts/shared/build/dotnet.py verify` work from anywhere inside a project.
    """
    declared = os.environ.get("DEVSCRIPTS_PROJECT")

    if declared:
        return Path(declared).resolve()

    for candidate in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        if (candidate / "Scripts" / CONFIG_NAME).is_file():
            return candidate

    sys.stderr.write(
        f"refused: no Scripts/{CONFIG_NAME} in {Path.cwd()} or any directory above it.\n"
        f"         {Path(__file__).resolve().parents[2]}/install.py <project> writes one.\n")
    sys.exit(2)


class Project:
    def __init__(self):
        self.root = _find_root()
        self.config_file = self.root / "Scripts" / CONFIG_NAME

        if not self.config_file.is_file():
            sys.stderr.write(f"refused: {self.config_file} does not exist.\n")
            sys.exit(2)

        with open(self.config_file, "rb") as handle:
            self._values = tomllib.load(handle)

        self.name = self._values.get("name", self.root.name)
        self.env_prefix = self._values.get("env_prefix", self.name.upper())

    def get(self, key, default=None):
        """The value at a dotted key, or the default when any part of the path is missing."""
        current = self._values

        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default

            current = current[part]

        return current

    def require(self, key):
        missing = object()
        value = self.get(key, missing)

        if value is missing:
            sys.stderr.write(
                f"refused: {self.config_file} has no {key!r}.\n"
                f"         This command needs it; add it and run again.\n")
            sys.exit(2)

        return value

    def path(self, key, default=None):
        """A configured repository-relative path, as an absolute one. None when unset and no default."""
        value = self.get(key, default)

        return None if value is None else self.root / value

    def require_path(self, key):
        return self.root / self.require(key)

    def env(self, name, default=None):
        """A per-project environment variable: `env("DB_HOST")` reads APP_DB_HOST when the prefix is APP."""
        return os.environ.get(f"{self.env_prefix}_{name}", default)

    def psql_environment(self):
        """
        The environment every psql call runs in.

        A short connect timeout, because a wrong host otherwise hangs until the TCP stack gives up, which
        inside a tool call looks like the tool is broken rather than the address being wrong. And TLS when
        the project asks for it: `[database] sslmode` and `sslrootcert` (or <PREFIX>_DB_SSLMODE and
        <PREFIX>_DB_SSLROOTCERT), which libpq reads as PGSSLMODE and PGSSLROOTCERT. Set per project and not in the
        shell, so a server without TLS elsewhere on the machine keeps working.
        """
        environment = {**os.environ, "PGCONNECT_TIMEOUT": os.environ.get("PGCONNECT_TIMEOUT", "8")}
        mode = self.env("DB_SSLMODE", self.get("database.sslmode"))
        root_certificate = self.env("DB_SSLROOTCERT", self.get("database.sslrootcert"))

        if mode:
            environment["PGSSLMODE"] = str(mode)

        if root_certificate:
            environment["PGSSLROOTCERT"] = str(root_certificate)

        return environment

    def env_name(self, name):
        """The spelling of that variable, for a message that tells someone what to set."""
        return f"{self.env_prefix}_{name}"


project = Project()
