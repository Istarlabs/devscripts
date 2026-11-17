# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""What `dotnet.py android` and `logs.py --android` share: where adb is, which device, and which app.

Not a script of its own, like project.py. All of it comes from the `[android]` block of
Scripts/project.toml, and the machine-bound parts can be overridden by an environment variable:

    [android]
    project = "Some.App"                  the MAUI project (folder or .csproj), required
    # application_id = "org.some.app"     default: ApplicationId in that .csproj
    # adb = "~/Android/Sdk/platform-tools/adb"      default: PATH, then the SDK in dotnet.build_properties
    # device = "192.168.1.50:37123"     adb over WLAN; {PREFIX}_ANDROID_DEVICE overrides

    [android.release]                     `android release`: the APK built in a container, reproducibly
    containerfile = "deploy/android/Containerfile"
    script = "deploy/android/build.py"    runs inside it: --out DIR, optional --keystore/--alias/--*-password-file
    # image = "org.some.app-build"        default: {application_id}-build
    # nuget_volume = "org.some.app-nuget" default: {application_id}-nuget

    [android.signing]                     applied to --release only; the passwords stay in files
    keystore = "~/.config/some-signing/some.keystore"
    alias = "some"
    store_password_file = "~/.config/some-signing/password"
    key_password_file = "~/.config/some-signing/key-password"

A signing block is optional: a .csproj that signs by itself (conditions on a keystore that exists) keeps
doing so. {PREFIX}_ANDROID_ADB and {PREFIX}_ANDROID_KEYSTORE override the two paths.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from project import project


def fail(message):
    sys.stderr.write(f"refused: {message}\n")
    sys.exit(2)


def expand(value):
    return os.path.expanduser(str(value))


def app_project():
    """The MAUI project's .csproj."""
    entry = project.require_path("android.project")

    if entry.is_file():
        return entry

    found = sorted(entry.glob("*.csproj"))

    if not found:
        fail(f"android.project {entry} holds no .csproj")

    return found[0]


def application_id():
    configured = project.get("android.application_id")

    if configured:
        return configured

    match = re.search(r"<ApplicationId>([^<]+)</ApplicationId>", app_project().read_text(encoding="utf-8-sig"))

    if not match:
        fail(f"no ApplicationId in {app_project()} - set android.application_id")

    return match.group(1).strip()


def adb_path():
    configured = project.env("ANDROID_ADB", project.get("android.adb"))

    if configured:
        return expand(configured)

    found = shutil.which("adb")

    if found:
        return found

    # The SDK the build already uses (dotnet.build_properties) has adb under platform-tools.
    sdk = (project.get("dotnet.build_properties") or {}).get("AndroidSdkDirectory")
    candidate = Path(expand(sdk)) / "platform-tools" / "adb" if sdk else None

    if candidate and candidate.is_file():
        return str(candidate)

    fail("adb not found: put it on PATH or set android.adb "
         f"(or {project.env_name('ANDROID_ADB')})")


def device():
    return project.env("ANDROID_DEVICE", project.get("android.device"))


def adb(*arguments, check=True):
    """Run adb against the configured device - connecting it first, when it is a WLAN address."""
    target = device()
    command = [adb_path(), *(["-s", target] if target else []), *arguments]
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")

    if check and completed.returncode != 0:
        fail(f"{' '.join(command)}\n{(completed.stderr or completed.stdout).strip()}")

    return completed


def connect():
    """`adb connect` for a configured WLAN device; a USB one (or none configured) needs nothing."""
    target = device()

    if target and ":" in target:
        result = subprocess.run([adb_path(), "connect", target], capture_output=True, text=True, errors="replace")
        output = (result.stdout + result.stderr).strip()

        if "connected to" not in output:
            fail(f"adb connect {target}: {output}\n"
                 "         Wireless debugging on, and the port current? It changes each time it is switched on.")


def require_device():
    """The attached device's serial, or a refusal. Connects a WLAN device first."""
    connect()
    lines = adb("devices").stdout.splitlines()[1:]
    online = [line.split()[0] for line in lines if line.strip().endswith("device")]

    if not online:
        fail("no Android device attached - " + (f"{device()} did not answer" if device() else
             "USB debugging on, or android.device set for WLAN"))

    if len(online) > 1 and not device():
        fail(f"{len(online)} devices attached ({', '.join(online)}): set android.device "
             f"or {project.env_name('ANDROID_DEVICE')}")

    return online[0]


def signing_properties():
    """MSBuild properties for a signed Release build, from [android.signing]; none when it is absent."""
    keystore = project.env("ANDROID_KEYSTORE", project.get("android.signing.keystore"))

    if not keystore:
        return []

    keystore = expand(keystore)

    if not os.path.isfile(keystore):
        fail(f"keystore {keystore} does not exist")

    properties = {
        "AndroidKeyStore": "true",
        "AndroidSigningKeyStore": keystore,
        "AndroidSigningKeyAlias": project.require("android.signing.alias"),
        "AndroidSigningStorePass": "file:" + expand(project.require("android.signing.store_password_file")),
        "AndroidSigningKeyPass": "file:" + expand(project.require("android.signing.key_password_file")),
    }

    return [f"-p:{key}={value}" for key, value in properties.items()]


def release_settings():
    """[android.release]: the Containerfile of the toolchain and the script that builds inside it."""
    return {
        "containerfile": project.require("android.release.containerfile"),
        "script": project.require("android.release.script"),
        "image": project.get("android.release.image") or f"{application_id()}-build",
        "nuget_volume": project.get("android.release.nuget_volume") or f"{application_id()}-nuget",
    }


def signing_files():
    """The signing files of [android.signing] by the name the container sees them under, plus the alias."""
    files = {
        "keystore": expand(project.env("ANDROID_KEYSTORE", project.require("android.signing.keystore"))),
        "store-password": expand(project.require("android.signing.store_password_file")),
        "key-password": expand(project.require("android.signing.key_password_file")),
    }

    for path in files.values():
        if not os.path.isfile(path):
            fail(f"signing file {path} does not exist")

    return {"files": files, "alias": project.require("android.signing.alias")}


def newest_apk(configuration, signed=True):
    """The most recently built .apk of this configuration, or None."""
    output = app_project().parent / "bin" / configuration
    found = [path for path in output.rglob("*.apk") if not signed or path.name.endswith("-Signed.apk")]
    found = found or list(output.rglob("*.apk"))

    return max(found, key=lambda path: path.stat().st_mtime) if found else None
