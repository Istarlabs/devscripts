#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for the routine Godot chores (C# / .NET edition) in this repository.

Every one of these used to be a hand-built chain: the long path of the unpacked editor, ANSI colours
and loading bars in front of the one error that matters, an SDK that is half there, and an adb port
that changes on every pairing.

    python3 Scripts/godot.py doctor                   what an Android export needs, and what is missing
    python3 Scripts/godot.py import                   headless import (.godot/, .uid files)
    python3 Scripts/godot.py build                    dotnet build of the solution (C# compile check)
    python3 Scripts/godot.py export                   the preset's APK/package, debug build
    python3 Scripts/godot.py export --release         release build (needs a release keystore)
    python3 Scripts/godot.py export --preset Linux --output build/game.x86_64
    python3 Scripts/godot.py templates                download, verify and install the export templates
    python3 Scripts/godot.py keystore                 create the debug keystore the editor settings point to
    python3 Scripts/godot.py keystore --release       create the release keystore of [godot.signing]
    python3 Scripts/godot.py pair HOST:PORT CODE      adb wireless pairing (Developer options > Wireless debugging)
    python3 Scripts/godot.py devices                  adb devices
    python3 Scripts/godot.py install                  adb install -r the exported APK, then start the app
    python3 Scripts/godot.py install --no-run
    python3 Scripts/godot.py desktop                  Linux release into ~/.local: lib/<name>/, bin/<name>, .desktop entry, icon
    python3 Scripts/godot.py desktop --remove         take it out again
    python3 Scripts/godot.py rpm                      build/<name>-<version>-<release>.x86_64.rpm (needs rpm-build; install with dnf)

The version is major.minor.height from Nerdbank.GitVersioning (PackageReference plus
version.json); without it the csproj <Version>. `export` of an Android preset writes version/name from it and
version/code = the commit count into export_presets.cfg first, so each APK is an update of the one before. An rpm built
from a dirty tree gets a Release of 1.dirty<timestamp>, which ranks above 1.
    python3 Scripts/godot.py run                      start the project in the editor's runtime (needs a display)
    python3 Scripts/godot.py logs                     adb logcat of the app, errors and Godot output

Config, all optional, in Scripts/project.toml:

    [godot]
    # binary = "~/dev/tools/godot/Godot_v4.6.2-stable_mono_linux_x86_64/Godot_v4.6.2-stable_mono_linux.x86_64"
    # preset = "Android"                 the export preset `export` and `install` use
    # output = "build/ksudoku.apk"       default: build/<lower-case project name>.apk
    # sdk = "~/Android/Sdk"
    # java = "/usr/lib/jvm/java-25-openjdk"      must hold bin/javac
    # device = "192.168.1.44:41234"    adb over WLAN, connected on demand; {PREFIX}_GODOT_DEVICE overrides
    # package = "de.example.game"        default: package/unique_name of the preset

    [godot.signing]                      `export --release`; handed to Godot as GODOT_ANDROID_KEYSTORE_RELEASE_*
    # keystore = "~/.config/kgames-signing/kgames.keystore"
    # alias = "kgames"
    # password_file = "~/.config/kgames-signing/password"     store and key share it (PKCS12)

    [godot.desktop]                      `desktop` and `rpm`; both need a preset named "Linux" and an .svg config/icon
    # name = "kmemory"                   file, binary, package name; default: lower-case project name
    # summary = "..."                    .desktop Comment and rpm Summary; default: the project name
    # categories = "Game;"               .desktop Categories
    # install_root = "~/.local"          `desktop` only; the rpm always installs under /usr

{PREFIX}_GODOT overrides the binary. Without either, `godot` on PATH, then ~/dev/tools/godot/**.

Exit code is non-zero when the step fails, so it can gate a commit.
"""
import configparser
import datetime
import glob
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import script_args
from project import project

REPO = str(project.root)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# What the editor prints that is progress, not information.
NOISE = re.compile(r"^\s*$|^\[\s*\d+%|^\[ DONE|^\[ *\d+ *%|loading_editor|first_scan|^Godot Engine v")


def refuse(message):
    sys.stderr.write(f"refused: {message}\n")
    sys.exit(2)


def expand(value):
    return os.path.expanduser(str(value))


def settings_path():
    return Path(expand("~/.config/godot")).glob("editor_settings-*.tres")


def editor_setting(key):
    """A value from the newest editor_settings-*.tres, or None."""
    for path in sorted(settings_path(), reverse=True):
        match = re.search(rf'^{re.escape(key)} = "(.*)"$', path.read_text(encoding="utf-8"), re.M)

        if match:
            return match.group(1)

    return None


def godot_binary():
    configured = os.environ.get(f"{project.env_prefix}_GODOT") or project.get("godot.binary")

    if configured:
        path = expand(configured)

        if not os.path.isfile(path):
            refuse(f"godot.binary '{path}' does not exist")

        return path

    found = shutil.which("godot") or shutil.which("godot-mono")

    if found:
        return found

    candidates = sorted(glob.glob(expand("~/dev/tools/godot/**/Godot*mono*.x86_64"), recursive=True), reverse=True)

    if candidates:
        return candidates[0]

    refuse("no Godot binary: set [godot] binary in project.toml or put `godot` on PATH")


def godot_version():
    """The full version string the export templates directory is named after, e.g. 4.6.2.stable.mono."""
    out = subprocess.run([godot_binary(), "--headless", "--version"], capture_output=True, text=True).stdout.strip()
    parts = out.split(".")

    if len(parts) < 4:
        refuse(f"cannot read the Godot version from '{out}'")

    return ".".join(parts[:parts.index("official")] if "official" in parts else parts[:5])


def sdk_path():
    return expand(project.get("godot.sdk") or editor_setting("export/android/android_sdk_path") or "~/Android/Sdk")


def java_path():
    return expand(project.get("godot.java") or editor_setting("export/android/java_sdk_path") or "")


def adb_path():
    return shutil.which("adb") or os.path.join(sdk_path(), "platform-tools", "adb")


def preset_name():
    return project.get("godot.preset", "Android")


def output_path(arguments=None):
    explicit = getattr(arguments, "output", None)
    return explicit or project.get("godot.output", f"build/{project.name.lower()}.apk")


def preset_value(preset, key):
    """An option of one export preset from export_presets.cfg."""
    path = Path(REPO) / "export_presets.cfg"

    if not path.is_file():
        refuse("no export_presets.cfg - create the preset in the editor or copy it in (it is git-ignored on purpose)")

    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")

    for section in parser.sections():
        if section.count(".") == 1 and parser.get(section, "name", fallback="").strip('"') == preset:
            return parser.get(f"{section}.options", key, fallback="").strip('"')

    refuse(f"no preset named '{preset}' in export_presets.cfg")


def package_name():
    return project.get("godot.package") or preset_value(preset_name(), "package/unique_name")


def run_godot(arguments, timeout=900, environment=None):
    """Run the editor with a filter on its output: progress bars and colours out, messages in."""
    process = subprocess.run([godot_binary(), "--headless", "--path", REPO, *arguments],
                             capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, **(environment or {})})
    lines = [ANSI.sub("", line) for line in (process.stdout + process.stderr).splitlines()]
    lines = [line for line in lines if not NOISE.search(line) and "shutdown_adb_on_exit" not in line
             and "editor_settings.cpp" not in line]

    for line in lines:
        print(line)

    return process.returncode, lines


def command_doctor(_):
    ok = True

    def report(label, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"{'ok     ' if good else 'MISSING'}  {label}{(' - ' + detail) if detail else ''}")

    binary = godot_binary()
    report("Godot binary", True, binary)
    version = godot_version()
    templates = Path(expand(f"~/.local/share/godot/export_templates/{version}"))
    report(f"export templates {version}", (templates / "android_debug.apk").is_file(), str(templates))

    sdk = sdk_path()
    report("Android SDK platform-tools", os.path.isfile(os.path.join(sdk, "platform-tools", "adb")), sdk)
    report("Android SDK build-tools", bool(glob.glob(os.path.join(sdk, "build-tools", "*", "apksigner"))))
    report("Android SDK platform", bool(glob.glob(os.path.join(sdk, "platforms", "android-*"))))

    java = java_path()
    report("JDK with javac", os.path.isfile(os.path.join(java, "bin", "javac")), java or "java_sdk_path unset")

    keystore = editor_setting("export/android/debug_keystore") or ""
    report("debug keystore", os.path.isfile(keystore), keystore or "unset in the editor settings")
    report("export_presets.cfg", (Path(REPO) / "export_presets.cfg").is_file())
    report("project.godot", (Path(REPO) / "project.godot").is_file())

    sys.exit(0 if ok else 1)


def command_import(_):
    code, _lines = run_godot(["--import"])
    sys.exit(code)


def command_build(_):
    solutions = sorted(Path(REPO).glob("*.sln")) + sorted(Path(REPO).glob("*.slnx"))

    if not solutions:
        refuse("no solution in the repository root")

    sys.exit(subprocess.run(["dotnet", "build", str(solutions[0]), "-v", "q", "-nologo"]).returncode)


def sync_android_version(preset):
    """Write version/name and version/code of an Android preset from git, so every APK is newer than the last
    one and the phone accepts it as an update. Does nothing for another platform."""
    path = Path(REPO) / "export_presets.cfg"
    text = path.read_text(encoding="utf-8")

    for block in re.finditer(r"^\[preset\.\d+\]\n.*?(?=^\[preset\.\d+\]$|\Z)", text, re.S | re.M):
        if f'name="{preset}"' not in block.group(0) or 'platform="Android"' not in block.group(0):
            continue

        name, code = app_version(), commit_count()
        updated, names = re.subn(r'^version/name=.*$', f'version/name="{name}"', block.group(0), flags=re.M)
        updated, codes = re.subn(r'^version/code=.*$', f"version/code={code}", updated, flags=re.M)

        if not (names and codes):
            refuse(f"preset '{preset}' has no version/name and version/code to update")

        path.write_text(text[:block.start()] + updated + text[block.end():], encoding="utf-8")
        print(f"android version {name} (code {code})")
        return


def command_export(arguments):
    preset = arguments.preset or preset_name()
    sync_android_version(preset)
    output = output_path(arguments)
    os.makedirs(os.path.dirname(os.path.join(REPO, output)) or REPO, exist_ok=True)

    environment = release_signing() if arguments.release else None
    code, lines = run_godot(["--export-release" if arguments.release else "--export-debug", preset, output],
                            environment=environment)
    exported = os.path.isfile(os.path.join(REPO, output))

    if code != 0 or not exported:
        print(f"export failed (exit {code}, {output} {'exists' if exported else 'not written'})")
        sys.exit(1)

    print(f"exported {output} ({os.path.getsize(os.path.join(REPO, output)) // 1024} KiB)")


def command_templates(_):
    version = godot_version()
    short, _, flavour = version.partition(".stable")
    mono = "_mono" if flavour.endswith("mono") else ""
    base = f"https://github.com/godotengine/godot-builds/releases/download/{short}-stable"
    name = f"Godot_v{short}-stable{mono}_export_templates.tpz"
    target = Path(expand(f"~/.local/share/godot/export_templates/{version}"))

    if (target / "version.txt").is_file():
        print(f"already installed: {target}")
        return

    with urllib.request.urlopen(f"{base}/SHA512-SUMS.txt") as response:
        sums = response.read().decode()

    expected = next((line.split()[0] for line in sums.splitlines() if line.endswith(name)), None)

    if not expected:
        refuse(f"{name} is not in the release's SHA512-SUMS.txt")

    with tempfile.TemporaryDirectory() as folder:
        archive = os.path.join(folder, name)
        print(f"downloading {name} ...")
        urllib.request.urlretrieve(f"{base}/{name}", archive)
        digest = hashlib.sha512()

        with open(archive, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)

        if digest.hexdigest() != expected:
            refuse("checksum mismatch, nothing installed")

        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(folder)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.path.join(folder, "templates"), target)

    print(f"installed into {target}")


def signing_config():
    """Keystore, alias and password file of [godot.signing], or a refusal naming the missing key."""
    keystore = project.get("godot.signing.keystore")
    alias = project.get("godot.signing.alias")
    password_file = project.get("godot.signing.password_file")

    if not (keystore and alias and password_file):
        refuse("a release build needs [godot.signing] keystore, alias and password_file in Scripts/project.toml")

    return expand(keystore), alias, expand(password_file)


def release_signing():
    """The variables Godot reads the release keystore from, so no password lands in export_presets.cfg."""
    keystore, alias, password_file = signing_config()

    if not (os.path.isfile(keystore) and os.path.isfile(password_file)):
        refuse(f"{keystore} or {password_file} does not exist - `keystore --release` creates both")

    return {
        "GODOT_ANDROID_KEYSTORE_RELEASE_PATH": keystore,
        "GODOT_ANDROID_KEYSTORE_RELEASE_USER": alias,
        "GODOT_ANDROID_KEYSTORE_RELEASE_PASSWORD": Path(password_file).read_text(encoding="utf-8").strip(),
    }


def create_release_keystore():
    """A new release keystore with a generated password. It signs every later update: lost, the app can only be
    installed anew, which deletes its data on the phone - so it refuses to overwrite one."""
    keystore, alias, password_file = signing_config()

    for path in (keystore, password_file):
        if os.path.exists(path):
            refuse(f"{path} already exists")

    os.makedirs(os.path.dirname(keystore), mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(password_file), mode=0o700, exist_ok=True)

    descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")

    holder = project.get("spdx.holder") or project.name
    keytool = os.path.join(java_path(), "bin", "keytool") if java_path() else "keytool"
    # :file keeps the password off the command line, where any process listing would show it.
    result = subprocess.run([keytool, "-genkeypair", "-storetype", "PKCS12", "-keystore", keystore, "-alias", alias,
                             "-storepass:file", password_file, "-keypass:file", password_file,
                             "-keyalg", "RSA", "-keysize", "4096", "-validity", "10000",
                             "-dname", f"CN={holder}, O={project.name}"])

    if result.returncode != 0:
        os.remove(password_file)
        sys.exit(result.returncode)

    os.chmod(keystore, 0o600)
    print(f"created {keystore} (alias {alias}), password in {password_file}\n"
          "Back both up: without them no later release installs over this one.")
    sys.exit(0)


def command_keystore(arguments):
    if arguments.release:
        create_release_keystore()

    path = expand(editor_setting("export/android/debug_keystore") or "~/.local/share/godot/keystores/debug.keystore")

    if os.path.exists(path):
        refuse(f"{path} already exists")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    keytool = os.path.join(java_path(), "bin", "keytool") if java_path() else "keytool"
    result = subprocess.run([keytool, "-genkeypair", "-keystore", path, "-alias", "androiddebugkey",
                             "-storepass", "android", "-keypass", "android", "-keyalg", "RSA", "-keysize", "2048",
                             "-validity", "9999", "-dname", "CN=Android Debug,O=Android,C=US"])
    sys.exit(result.returncode)


def adb(*arguments, check=False):
    return subprocess.run([adb_path(), *arguments], capture_output=True, text=True, check=check)


def connect_device():
    """Connect the configured WLAN device on demand; a USB or already-paired device needs nothing."""
    device = os.environ.get(f"{project.env_prefix}_GODOT_DEVICE") or project.get("godot.device")

    if device:
        adb("connect", device)

    listing = adb("devices").stdout.splitlines()[1:]
    online = [line.split()[0] for line in listing if line.strip().endswith("device")]

    if not online:
        refuse("no adb device online - `godot.py pair HOST:PORT CODE`, then set [godot] device")

    return online[0]


def command_pair(arguments):
    result = adb("pair", arguments.endpoint, arguments.code)
    print((result.stdout + result.stderr).strip())
    sys.exit(result.returncode)


def command_devices(_):
    print(adb("devices", "-l").stdout.strip())


def command_install(arguments):
    apk = os.path.join(REPO, output_path())

    if not os.path.isfile(apk):
        refuse(f"{apk} does not exist - `godot.py export` first")

    connect_device()
    result = adb("install", "-r", apk)
    print((result.stdout + result.stderr).strip())

    if result.returncode != 0 or "Success" not in result.stdout:
        sys.exit(1)

    if not arguments.no_run:
        adb("shell", "monkey", "-p", package_name(), "-c", "android.intent.category.LAUNCHER", "1")
        print(f"started {package_name()}")


def app_name():
    return project.get("godot.desktop.name") or project.name.lower()


def git_output(*arguments):
    result = subprocess.run(["git", "-C", REPO, *arguments], capture_output=True, text=True)

    if result.returncode != 0:
        refuse(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")

    return result.stdout.strip()


def commit_count():
    """Commits on HEAD. The Android versionCode: it only ever goes up, even when version.json is raised."""
    return int(git_output("rev-list", "--count", "HEAD"))


def is_dirty():
    return bool(git_output("status", "--porcelain"))


def nbgv_version():
    """The simple version Nerdbank.GitVersioning computes (major.minor.height), or None without the package."""
    csprojs = sorted(Path(REPO).glob("*.csproj"))

    if not csprojs:
        return None

    result = subprocess.run(["dotnet", "msbuild", str(csprojs[0]), "-nologo", "-restore", "-t:GetBuildVersion",
                             "-getProperty:BuildVersionSimple"], capture_output=True, text=True,
                            env={**os.environ, "DOTNET_CLI_UI_LANGUAGE": "en"})

    # One -getProperty prints the bare value (several would print JSON); anything else is an error text.
    value = result.stdout.strip()

    return value if result.returncode == 0 and re.fullmatch(r"\d+(\.\d+)+", value) else None


def app_version():
    """The version of the game: Nerdbank.GitVersioning when the csproj has it, else its <Version>, else 1.0.0."""
    computed = nbgv_version()

    if computed:
        return computed

    for csproj in sorted(Path(REPO).glob("*.csproj")):
        match = re.search(r"<Version>\s*([^<\s]+)\s*</Version>", csproj.read_text(encoding="utf-8"))

        if match:
            return match.group(1)

    return "1.0.0"


def stage_linux():
    """Export the Linux preset as a release into build/linux/<name>/ and return that folder."""
    folder = Path(REPO) / "build" / "linux" / app_name()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    output = os.path.relpath(folder / f"{app_name()}.x86_64", REPO)
    code, _lines = run_godot(["--export-release", "Linux", output])

    if code != 0 or not (folder / f"{app_name()}.x86_64").is_file():
        refuse(f"the Linux export failed (exit {code}) - is there a preset named 'Linux'?")

    (folder / f"{app_name()}.sh").unlink(missing_ok=True)

    return folder


def install_tree(staged, destination, prefix):
    """Lay the exported game out under destination/prefix: lib/<name>/ with everything Godot wrote, a bin/<name>
    symlink to the binary, the .desktop entry and the icon. Exec= names the real prefix, not the destination."""
    name = app_name()
    icon = re.search(r'^config/icon="res://(.+)"$', (Path(REPO) / "project.godot").read_text(encoding="utf-8"), re.M)

    if not icon or not icon.group(1).endswith(".svg"):
        refuse("config/icon in project.godot must be an .svg")

    root = Path(str(destination) + str(prefix))
    library = root / "lib" / name
    shutil.rmtree(library, ignore_errors=True)
    shutil.copytree(staged, library, symlinks=True)

    (root / "bin").mkdir(parents=True, exist_ok=True)
    link = root / "bin" / name
    link.unlink(missing_ok=True)
    link.symlink_to(f"../lib/{name}/{name}.x86_64")

    icons = root / "share" / "icons" / "hicolor" / "scalable" / "apps"
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(REPO) / icon.group(1), icons / f"{name}.svg")

    applications = root / "share" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    categories = project.get("godot.desktop.categories", "Game;")
    summary = project.get("godot.desktop.summary", project.name)
    (applications / f"{name}.desktop").write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={project.name}\nComment={summary}\nExec={prefix}/bin/{name}\nIcon={name}\n"
        f"Categories={categories}\nTerminal=false\n", encoding="utf-8")


def command_desktop(arguments):
    """Install for the current user: no root, nothing to register."""
    prefix = Path(expand(project.get("godot.desktop.install_root", "~/.local")))
    name = app_name()

    if arguments.remove:
        shutil.rmtree(prefix / "lib" / name, ignore_errors=True)

        for path in (prefix / "bin" / name, prefix / "share" / "applications" / f"{name}.desktop",
                     prefix / "share" / "icons" / "hicolor" / "scalable" / "apps" / f"{name}.svg"):
            path.unlink(missing_ok=True)

        print(f"removed {name} from {prefix}")
        return

    install_tree(stage_linux(), "", prefix)
    print(f"installed {name} {app_version()} into {prefix} (bin/{name}, share/applications/{name}.desktop)")


def command_rpm(_):
    """A binary rpm of the exported game under /usr. Nothing is compiled by rpmbuild: %install copies the
    staged tree, which is why debuginfo, stripping and the automatic dependency scan are off - they would
    treat the bundled .NET runtime as if the package had built it."""
    if not shutil.which("rpmbuild"):
        refuse("rpmbuild is missing: sudo dnf install rpm-build")

    # A pre-release suffix (1.0-beta) sorts lower in rpm with a tilde, as it should. A build from a dirty tree has the
    # same version as the commit before it, so it gets a longer Release, which rpm ranks higher: dnf still upgrades.
    name, version = app_name(), app_version().replace("-", "~")
    release = "1" if not is_dirty() else f"1.dirty{datetime.datetime.now():%Y%m%d%H%M}"
    top = Path(REPO) / "build" / "rpm"
    shutil.rmtree(top, ignore_errors=True)
    tree = top / "tree"
    install_tree(stage_linux(), tree, "/usr")

    holder = project.get("spdx.holder", "unknown")
    summary = project.get("godot.desktop.summary", project.name)
    spec = top / f"{name}.spec"
    spec.write_text(f"""%global debug_package %{{nil}}
%global __strip /bin/true
%global _build_id_links none
%global _missing_build_ids_terminate_build 0
AutoReqProv: no

Name:       {name}
Version:    {version}
Release:    {release}
Summary:    {summary}
License:    {project.get("spdx.licence", "MIT")}
BuildArch:  x86_64
Requires:   fontconfig libX11 libXcursor libXi libXinerama libXrandr libxkbcommon libwayland-client mesa-libGL alsa-lib

%description
{summary}. Exported with Godot; the .NET runtime is bundled in /usr/lib/{name}.

%install
mkdir -p %{{buildroot}}
cp -a {tree}/. %{{buildroot}}/

%files
/usr/lib/{name}
/usr/bin/{name}
/usr/share/applications/{name}.desktop
/usr/share/icons/hicolor/scalable/apps/{name}.svg

%changelog
* {datetime.date.today().strftime("%a %b %d %Y")} {holder} - {version}-{release}
- Built from the exported Linux preset.
""", encoding="utf-8")

    result = subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", str(spec)], capture_output=True, text=True)

    if result.returncode != 0:
        print(result.stdout + result.stderr)
        sys.exit(1)

    built = next(iter(sorted((top / "RPMS").rglob("*.rpm"))), None)

    if not built:
        refuse("rpmbuild wrote no rpm")

    target = Path(REPO) / "build" / built.name
    shutil.copy(built, target)
    print(f"built {os.path.relpath(target, REPO)} ({target.stat().st_size // 1024} KiB) - sudo dnf install ./{os.path.relpath(target, REPO)}")


def command_run(_):
    sys.exit(subprocess.run([godot_binary(), "--path", REPO]).returncode)


def command_logs(arguments):
    connect_device()
    process = adb("shell", "pidof", package_name())
    pid = process.stdout.strip().split()[0] if process.stdout.strip() else None

    if not pid:
        refuse(f"{package_name()} is not running on the device")

    sys.exit(subprocess.run([adb_path(), "logcat", "-d", f"--pid={pid}", "-t", str(arguments.lines)]).returncode)


def main():
    parser = script_args.parser(__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor")
    commands.add_parser("import")
    commands.add_parser("build")
    export = commands.add_parser("export")
    export.add_argument("--release", action="store_true")
    export.add_argument("--preset")
    export.add_argument("--output")
    commands.add_parser("templates")
    keystore = commands.add_parser("keystore")
    keystore.add_argument("--release", action="store_true",
                          help="create the release keystore [godot.signing] names, with a generated password")
    pair = commands.add_parser("pair")
    pair.add_argument("endpoint")
    pair.add_argument("code")
    commands.add_parser("devices")
    install = commands.add_parser("install")
    install.add_argument("--no-run", action="store_true")
    desktop = commands.add_parser("desktop")
    desktop.add_argument("--remove", action="store_true")
    commands.add_parser("rpm")
    commands.add_parser("run")
    logs = commands.add_parser("logs")
    logs.add_argument("-n", "--lines", type=int, default=200)

    arguments = parser.parse_args()
    globals()[f"command_{arguments.command}"](arguments)


if __name__ == "__main__":
    main()
