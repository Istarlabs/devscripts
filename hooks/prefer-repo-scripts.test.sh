#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

#
# Checks prefer-repo-scripts.sh against the commands it must refuse and the ones it must not break.
#
# The DENY block is not invented: every line is a command actually run in this repository while the
# hook was in place and silent, which is how the two gaps it had were found. The ALLOW block is the
# other half of the job - a hook that refuses the working habits gets switched off, and then it
# guards nothing.
#
#   bash .claude/hooks/prefer-repo-scripts.test.sh

set -uo pipefail

hook="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prefer-repo-scripts.sh"
failures=0

check() {
    local want="$1" cmd="$2"
    local out got
    out="$(jq -nc --arg cmd "$cmd" '{tool_input:{command:$cmd}}' | bash "$hook")"
    [ -z "$out" ] && got="ALLOW" || got="DENY"

    if [ "$got" = "$want" ]; then
        printf '  ok   %-6s %s\n' "$got" "$cmd"
    else
        printf '  FAIL want=%s got=%s  %s\n' "$want" "$got" "$cmd"
        failures=$((failures + 1))
    fi
}

echo "must be refused:"
check DENY 'grep -rn "FluentSelect" Components/'
check DENY '\grep -rn "FluentSelect" Components/'
check DENY '\grep -c "new Unit" UnitConfiguration.cs'
check DENY '\grep -oP "VALUES \(" schema.sql | sort -u | tr "\n" " "'
check DENY 'for f in $(\grep -rl "FluentSelect" .); do echo "$f"; done'
check DENY '\grep -rl "HasData" Configurations | xargs \grep -ln "Unit"'
check DENY '\find /home/dev/dev -name "Unit.cs" -not -path "*/obj/*"'
check DENY '\sed -n "2377,2400p" deploy/db-init/schema.sql'
check DENY '\cat Scripts/repo.py'
check DENY 'awk "{print \$1}" units.csv'
check DENY 'rg -l "SomeSymbol"'
check DENY 'python3 -c "import os; print(os.getcwd())"'
check DENY "python -c 'print(1)'"
check DENY 'python3 - <<PY
import json
PY'
check DENY "python3 - <<'PY'
import json
PY"
check DENY 'python3 <<PY
print(1)
PY'
check DENY 'python3 -u -c "print(1)"'
check DENY '\ls Data/ | python3 -c "import sys; print(sys.stdin.read())"'
check DENY 'gh issue list'
check DENY 'git commit -m "wip"'
check DENY 'psql -c "select 1"'
check DENY 'dotnet build'
check DENY 'cargo clippy --all-targets'
check DENY 'cargo test --no-fail-fast'
check DENY 'cargo build --release'
check DENY 'cmake --build build -j8'
check DENY 'cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo'
check DENY 'ctest --test-dir build --output-on-failure'
check DENY 'cmake --install build'
# The NO_SCRIPT=1 opt-out was removed on 2026/08/21 - it has to be refused like any other prefix.
check DENY 'NO_SCRIPT=1 \grep -rn "x" .'
check DENY 'NO_SCRIPT=1 \find . -name "*.razor"'
# git grep is a raw sweep behind a subcommand, and it went unnoticed while every honest spelling was
# refused - the same shape as the escape hatch above.
check DENY 'GIT_PAGER=cat git grep -n "Toast" -- Components/'
check DENY 'git grep -ln "HasData"'
# Read-only history went through git directly while repo.py owned the rest; now it has 'log'.
check DENY 'GIT_PAGER=cat git log --oneline -4'
check DENY 'git --no-pager log -5'
check DENY 'GIT_PAGER=cat git -C /home/dev/dev/projects/dotnet/Istarnova log --oneline -3 -- Istarnova/Extensions/NumberExtensions.cs'
# fd and the rest of the dotnet CLI, all run in a project on 2026/09/25 while the hook was silent.
check DENY 'fd -tf -e razor . MyApp/Components | grep -i order'
check DENY 'fd IActivityEventRepository.cs'
check DENY "fd -tf AddRecordMessages MyApp.Database.EfCore/Migrations -E '*.Designer.cs'"
check DENY 'python3 Scripts/find.py X --text; fd -HI -e xml . ~/.nuget/packages/pdfpig/0.1.16/lib | head'
check DENY 'dotnet restore MyApp.Importer/MyApp.Importer.csproj 2>&1 | tail -3'
check DENY 'dotnet list package --vulnerable'
check DENY 'dotnet run Scripts/csharp/loga_overview_read.cs'
# A pipe that carries nothing, put in front so that grep counts as "behind a pipe" - oversteer-qt, 2026/11/10.
check DENY 'true | grep -n -i -E "mouse|cursor" config.cfg global_controls.sii | head -60'
check DENY '\ls Scripts; true | grep -n -E "ui_joy" game.log.txt'
check DENY ': | grep -rn "x" .'
check DENY "printf '' | grep -n x file.txt"
check DENY 'echo | grep -n x file.txt'

echo
echo "must keep working:"
check ALLOW 'python3 Scripts/repo.py status || true'
check ALLOW 'echo "a b" | grep -c a'
check ALLOW 'python3 Scripts/find.py UnitPicker --lines'
check ALLOW 'python3 Scripts/dotnet.py verify'
check ALLOW 'python3 Scripts/repo.py pr 414'
check ALLOW 'python3 Scripts/db.py query --format table -c "SELECT 1"'
check ALLOW 'python3 Scripts/db.py query -c "SELECT 1"'
check ALLOW 'python3 Scripts/translations.py add SharedResources --from new-keys.json'
check ALLOW 'python3 /home/dev/dev/projects/dotnet/Istarnova/Scripts/repo.py status'
check ALLOW 'python3 Scripts/csharp_check.py -c config.json'
check ALLOW 'python3 Scripts/repo.py issues | head -40'
check ALLOW 'python3 Scripts/db.py query -c "SELECT 1" | \grep -c Mass'
check ALLOW 'git status --short'
check ALLOW 'python3 Scripts/repo.py log Scripts/find.py -n 5'
check ALLOW 'python3 Scripts/find.py "some text" --text'
check ALLOW 'python3 Scripts/cpp.py verify'
check ALLOW 'python3 Scripts/rust.py verify'
# cargo run/add/fetch/tree stay available, and so do ffmpeg and ffprobe - measuring a real file by
# hand is the point in dolphin-video-converter, and no script replaces a one-off probe.
check ALLOW 'cargo run -- --analyze-only file.mkv'
check ALLOW 'cargo tree --edges normal'
check ALLOW 'ffprobe -v error -show_streams file.mkv'
check ALLOW 'python3 Scripts/cpp.py run -- --version'
# The binary a C++ project builds is not the build system, and running it is the point.
check ALLOW 'build/bin/kurodlg --help'
check ALLOW 'git diff --stat master...HEAD'
check ALLOW 'gh api repos/:owner/:repo'
check ALLOW '\ls -1 Scripts/'
check ALLOW 'wc -l < units.csv'
check ALLOW 'python3 Scripts/find.py "FillColor" --text --in /home/dev/.nuget/packages/pdfpig/0.1.16/lib/net8.0'
check ALLOW 'python3 Scripts/dotnet.py script loga_overview_read'
check ALLOW 'python3 Scripts/dotnet.py packages --versions PdfPig'
check ALLOW 'dotnet run --project MyApp.AppHost'
check ALLOW 'dotnet --list-sdks'
check ALLOW 'python3 Scripts/find.py fd --text'

echo
if [ "$failures" -eq 0 ]; then
    echo "all checks passed"
else
    echo "$failures check(s) failed"
    exit 1
fi
