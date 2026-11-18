#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for the routine git and GitHub chores in this repository.

Every one of these used to be a hand-built command line: the repository path retyped or `cd`-ed to,
`GH_PAGER=cat` remembered or forgotten, and each unique
line asking for permission again. Worse, parsing git's output here means parsing *German* — this
machine's git and SDK are localized, so `grep`-ing for "nothing to commit" silently misses.

Everything below reads --porcelain or --json and never the human-readable text.

    python3 Scripts/repo.py init [--description T]    a directory that is no repository yet: git init + a private GitHub repo + labels
    python3 Scripts/repo.py labels                    the shared labels and the project's [[repo.labels]], on an existing repo
    python3 Scripts/repo.py status                    branch, upstream, staged/unstaged, ahead/behind
    python3 Scripts/repo.py diff [PATH ...]           what changed, not just which files
    python3 Scripts/repo.py diff --against master --stat
    python3 Scripts/repo.py branch fix/210-slug       master + pull, then a fresh branch off it
    python3 Scripts/repo.py branch b --from a         stacked: branch off another branch instead
    python3 Scripts/repo.py branch fix/x --carry      take the uncommitted work along
    python3 Scripts/repo.py switch master             move to a branch that exists; master is pulled
    python3 Scripts/repo.py test-branch test/import fix/a feat/b
                                                      one branch carrying several, for one test round
    python3 Scripts/repo.py test-branch test/import --delete   it never belonged on the remote
    python3 Scripts/repo.py land                      waived change: master fast-forwards to this branch, pushed, branch deleted
    python3 Scripts/repo.py commit --file msg.md      commit
    python3 Scripts/repo.py commit --signoff --file msg.md   with Signed-off-by, as contributors do
    python3 Scripts/repo.py commit -m "fix(x): ..." --all
    python3 Scripts/repo.py uncommit                  undo the last commit, changes stay staged
    python3 Scripts/repo.py log [PATH ...] [-n 20]    recent commits, one line each
    python3 Scripts/repo.py blame FILE --lines 24,48  who wrote it, and what that commit was about
    python3 Scripts/repo.py push                      push the current branch, setting upstream
    python3 Scripts/repo.py move OLD NEW [OLD NEW]    git mv in pairs, all of them or none
    python3 Scripts/repo.py leaks                     gitleaks over the history and the working files, before going public

    python3 Scripts/repo.py issues                    open issues, one line each
    python3 Scripts/repo.py issues --state closed --label bug
    python3 Scripts/repo.py issues --search 'accessibility OR WCAG'   in titles and bodies, not only labels
    python3 Scripts/repo.py digest                    what was finished in the last 7 days, by area
    python3 Scripts/repo.py digest --days 30
    python3 Scripts/repo.py issue 210                 title, labels and body
    python3 Scripts/repo.py issue 441 442 443         several at once, for a group that belongs together
    python3 Scripts/repo.py issue-create --title T --label bug,database --file body.md
    python3 Scripts/repo.py assign                    an assignee for anything open that lacks one
    python3 Scripts/repo.py comment 210 --file c.md   comment on an issue
    python3 Scripts/repo.py close 94 --file c.md      comment, then close
    python3 Scripts/repo.py label "high-priority" --color d73a4a --description "..."

    python3 Scripts/repo.py milestone                 every milestone, with its counts and due date
    python3 Scripts/repo.py milestone "Due 2026-09-04"    one milestone and the issues in it
    python3 Scripts/repo.py milestone-create --title "Due 2026-09-07" --due 2026-09-07
    python3 Scripts/repo.py milestone-edit 2 --due 2026-09-08     by number or by title
    python3 Scripts/repo.py milestone-close 2         close it; --reopen on milestone-edit undoes it
    python3 Scripts/repo.py issues --milestone 2      what is in it, or --milestone none for the rest
    python3 Scripts/repo.py issue-edit 639 --milestone "Due 2026-09-05"

    python3 Scripts/repo.py pr                        open PRs
    python3 Scripts/repo.py pr 314                    one PR, with mergeability
    python3 Scripts/repo.py pr-create --title T --file body.md
    python3 Scripts/repo.py pr-merge 314              waits out UNKNOWN, refuses unless MERGEABLE
    python3 Scripts/repo.py pr-close 314 --file c.md  close without merging, branch deleted
    python3 Scripts/repo.py issue-edit 210 --file b.md    replace an issue body
    python3 Scripts/repo.py issue-edit 210 --title "..."  retitle it, body left as it is
    python3 Scripts/repo.py issue-edit 210 --add-label parked
    python3 Scripts/repo.py pr-edit 314 --file b.md       replace a PR body
    python3 Scripts/repo.py pr-edit 314 --title "..."     retitle it, body left as it is
    python3 Scripts/repo.py pr-edit 314 --add-label parked
    python3 Scripts/repo.py resolve [--apply]         the recurring append-only merge conflicts

Exit code is non-zero when a command fails or a guard refuses, so it can gate a chain.
Guards refuse rather than half-apply: `branch` will not run with a dirty tree, `commit` and `push`
refuse on master unless --allow-master, `pr-merge` refuses anything not reported mergeable - after
giving GitHub a few seconds to work it out, because it answers UNKNOWN right after a push or a
retarget and that is a pending job, not a problem.
"""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import NoReturn

from project import project

SCRIPTS = str(project.root / "Scripts")
REPO = str(project.root)

MAIN_BRANCH = project.get("repo.main_branch", "master")

# A repository without a pipeline says so (`ci = false`), so `ci` answers that at once instead of
# polling for a run that is never going to appear.
HAS_CI = project.get("repo.ci", True)
MERGE_STRATEGY = project.get("repo.merge_strategy", "merge")

# Every issue and every pull request gets one. An unassigned item reads as nobody's, and on a
# repository with one maintainer that is never what it means - it means the assignment was forgotten.
# "@me" is whoever gh is authenticated as, so this stays right in a fork and in a clone.
ASSIGNEE = "@me"

# Both tools page by default and `gh` has no --no-pager flag at all; a pager in a captured
# subprocess is at best noise and at worst a hang.
ENV = {**os.environ, "GH_PAGER": "cat", "GIT_PAGER": "cat"}


def run(command, check=True, capture=True):
    """Run a command in the repository root. Exits with its output when it fails and check is set."""
    result = subprocess.run(command, cwd=REPO, env=ENV, text=True,
                            capture_output=capture)

    if check and result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    return result


def out(command):
    return run(command).stdout.strip()


def run_with_input(command, text):
    """Run a command feeding it text on stdin, which is how message bodies avoid shell quoting."""
    result = subprocess.run(command, cwd=REPO, env=ENV, text=True, input=text, capture_output=True)

    if result.returncode != 0:
        sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
        sys.exit(result.returncode)

    return result.stdout.strip()


def refuse(message) -> NoReturn:
    sys.stderr.write(f"refused: {message}\n")
    sys.exit(1)


def working_files():
    """Every file of the repository, tracked or new, with ignored ones left out. spdx.py and cpp.py
    filter this by type instead of each asking git the same question."""
    listed = run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]).stdout

    return sorted(path for path in set(listed.split("\0"))
                  if path and os.path.isfile(os.path.join(REPO, path)))


def current_branch():
    return out(["git", "branch", "--show-current"])


def is_dirty():
    return bool(out(["git", "status", "--porcelain"]))


def message_text(arguments):
    """The commit or comment body, from --file or -m. Exactly one of them is required."""
    if arguments.file:
        return open(os.path.join(REPO, arguments.file)
                    if not os.path.isabs(arguments.file) else arguments.file,
                    encoding="utf-8").read().rstrip()

    if arguments.message:
        return arguments.message.rstrip()

    refuse("need a message: -m \"...\" or --file path")


def guard_not_main(arguments, action):
    if current_branch() == MAIN_BRANCH and not arguments.allow_master:
        refuse(f"{action} on {MAIN_BRANCH}. Branch first, or pass --allow-master for a waived change.")


# --- git -------------------------------------------------------------------------------------

def command_init(arguments):
    """Make this directory a repository with a private home on GitHub, and nothing more.

    Creating the repository and `git init` are one step on purpose: `gh repo create --source .` adds
    the `origin` remote in the protocol gh is configured for, which a hand-written `git remote add`
    gets wrong as often as right. It does not commit or push - the first commit goes through
    `commit --all --allow-master` like any other, so what is in it is looked at once more.

    Refuses without a .gitignore, because the build directory is what gets committed by accident and
    is the one thing that is hard to take back out of a pushed history. If GitHub refuses (the name
    exists, no network), the `.git` this created is removed again, so the same call can be repeated.
    """
    git_dir = os.path.join(REPO, ".git")

    if os.path.exists(git_dir):
        refuse("this directory is already a repository")

    if not os.path.isfile(os.path.join(REPO, ".gitignore")):
        refuse("no .gitignore - copy one in first, so the build directory is not committed by accident")

    name = arguments.name or project.get("name", os.path.basename(REPO))

    run(["git", "init", "-b", MAIN_BRANCH])

    command = ["gh", "repo", "create", name, "--public" if arguments.public else "--private",
               "--source", ".", "--remote", "origin"]

    if arguments.description:
        command += ["--description", arguments.description]

    result = run(command, check=False)

    if result.returncode != 0:
        shutil.rmtree(git_dir)
        refuse((result.stderr or result.stdout or "").strip())

    print(f"initialised on {MAIN_BRANCH}, "
          f"{'public' if arguments.public else 'private'} repository {name} created: {out(['git', 'remote', 'get-url', 'origin'])}")

    # The repository exists by now, so a label that fails is reported, not undone: `labels` repeats it.
    if failed := create_labels():
        print(f"labels not created: {', '.join(failed)} - `repo.py labels` tries again", file=sys.stderr)


def command_status(_arguments):
    branch = current_branch()
    print(f"branch   {branch or '(detached)'}")

    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                   check=False).stdout.strip()

    if upstream:
        counts = out(["git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"]).split()
        print(f"upstream {upstream}  (behind {counts[0]}, ahead {counts[1]})")
    else:
        print("upstream (none)")

    changes = out(["git", "status", "--porcelain"])
    print(f"changes  {len(changes.splitlines()) if changes else 0} file(s)")

    if changes:
        print()
        print(changes)


def command_diff(arguments):
    """What changed, against the working tree, the index or another ref.

    Separate from `status`, which answers "which files" and stops there. The pager is what makes a
    hand-built `git diff` unreliable here - it swallows the output unless GIT_PAGER is set, and that
    is remembered about as often as the repository path was.
    """
    command = ["git", "diff"]

    if arguments.staged:
        command.append("--staged")

    if arguments.against:
        command.append(arguments.against)

    if arguments.stat:
        command.append("--stat")

    if arguments.paths:
        command += ["--"] + arguments.paths

    print(out(command) or "no differences")


def command_log(arguments):
    """The recent commits, one line each, optionally only those touching the given paths."""
    command = ["git", "log", f"-{arguments.count}", "--format=%h  %ad  %<(14,trunc)%an  %s", "--date=short"]

    if arguments.range:
        command.append(arguments.range)

    if arguments.paths:
        command += ["--"] + arguments.paths

    print(out(command) or "no commits")


def command_blame(arguments):
    """Who wrote these lines, in which commit, and what that commit said it was doing.

    The question this answers is not "who typed this" but "what was the change that introduced this,
    and did a human decide it or did it come along with something else". `git blame` alone gives a
    hash and a name; the commit subject is the part that carries the reasoning, so it is looked up
    and printed beside each line. Ranges keep the output to the passage in question - blaming a whole
    file and reading it is how this stops being used.
    """
    command = ["git", "blame", "--line-porcelain"]

    if arguments.lines:
        command += ["-L", arguments.lines]

    command += ["--", arguments.path]

    result = run(command, check=False)

    if result.returncode != 0:
        refuse((result.stderr or result.stdout or "").strip())

    subjects = {}
    commit = author = when = None

    for line in result.stdout.splitlines():
        if len(line) >= 40 and " " in line and all(c in "0123456789abcdef" for c in line[:40]):
            commit = line[:40]
        elif line.startswith("author "):
            author = line[len("author "):]
        elif line.startswith("author-time "):
            when = datetime.fromtimestamp(int(line[len("author-time "):])).strftime("%Y-%m-%d")
        elif line.startswith("\t"):
            if commit not in subjects:
                subjects[commit] = out(["git", "log", "-1", "--format=%s", commit])

            short = commit[:8] if commit else "?" * 8
            print(f"{short}  {when}  {author or '':<16.16}  {line[1:]}")
            print(f"{'':8}  {'':10}  {'':16}  \033[2m{subjects[commit]}\033[0m")


def pull_if_tracked(branch):
    """Bring the checked-out branch up to date, unless it has no remote to be behind."""
    # A stacked base, and a test branch, may exist only locally, and `pull` on a branch without an
    # upstream fails.
    if run(["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], check=False).returncode == 0:
        run(["git", "pull", "--ff-only"])


def command_branch(arguments):
    if arguments.delete:
        return delete_branch(arguments.name, arguments.force)

    # Uncommitted work is usually something that wandered in from elsewhere, which is why this
    # refuses by default. It is also, sometimes, exactly the change the branch is being made for:
    # the fix was written before anyone thought about where it should live. --carry says so out loud
    # rather than leaving the guard to be worked around with a stash.
    if is_dirty() and not arguments.carry:
        refuse("the working tree has changes; they would follow you onto the new branch "
               "(--carry if that is the point)")

    base = arguments.base or MAIN_BRANCH

    # The base is brought up to date *before* the tree moves onto it, not after.
    fast_forward_ref(base)
    run(["git", "checkout", base])
    pull_if_tracked(base)

    run(["git", "checkout", "-b", arguments.name])

    carried = " with the working tree" if arguments.carry and is_dirty() else ""
    print(f"on {arguments.name}, branched off {base}{carried}")


def fast_forward_ref(branch):
    """Bring a local branch up to its remote without checking it out.

    `git fetch origin master:master` moves the ref while the working tree stays where it is, which
    is what lets a branch be made from an up-to-date base in one move. Checking the base out first
    and pulling afterwards - the order this used to use - lands the tree in the *stale* base on the
    way past, and uncommitted work that touches a file the base has since changed makes that step
    refuse. A branch already merged on GitHub is the everyday case: locally master was behind, so
    the .gitignore it was about to be carried onto was the old one.

    Fails harmlessly when the branch is the one checked out, has no upstream, or has diverged; the
    checkout that follows then simply uses what is there.
    """
    run(["git", "fetch", "origin", f"{branch}:{branch}"], check=False)


def command_switch(arguments):
    """Move to a branch that already exists - which `branch` cannot do, it only makes new ones.

    Same guard as `branch`: uncommitted work follows you across and is how a change lands on the
    wrong branch, so this refuses unless --carry says that is the point.

    Landing on master also pulls it, because the merge that put something there happened on GitHub:
    without this, everything read afterwards - a build, a test count, a diff - describes the tree as
    it was before the merge, and `git status` agrees, comparing against a remote-tracking ref that is
    just as stale. Only master: a feature branch that has diverged locally would fail --ff-only and
    turn an ordinary switch into a refusal.
    """
    if is_dirty() and not arguments.carry:
        refuse("the working tree has changes; they would follow you onto that branch "
               "(--carry if that is the point)")

    known = out(["git", "branch", "--format=%(refname:short)"]).splitlines()

    if arguments.name not in known:
        remote = run(["git", "rev-parse", "--verify", f"origin/{arguments.name}"], check=False)

        if remote.returncode != 0:
            refuse(f"no branch '{arguments.name}' here or on origin")

        run(["git", "checkout", "-b", arguments.name, f"origin/{arguments.name}"])
    else:
        if arguments.name == MAIN_BRANCH:
            fast_forward_ref(arguments.name)

        run(["git", "checkout", arguments.name])

        if arguments.name == MAIN_BRANCH:
            pull_if_tracked(arguments.name)

    carried = " with the working tree" if arguments.carry and is_dirty() else ""
    print(f"on {arguments.name}{carried}")


def command_land(_arguments):
    """Put the current branch's commits on the main branch without a pull request, then drop the branch.

    The waived change: small enough that an issue and a PR are more overhead than the change, and the
    user said "straight to master". Done by hand it was four steps in the right order (uncommit, switch
    with the tree, commit with --allow-master, push) and a stale branch left over. This is the same
    result from a branch that already holds the commit: fast-forward only, so nothing is rewritten and
    a branch that master has moved past is refused rather than merged behind anyone's back.
    """
    name = current_branch()

    if not name or name == MAIN_BRANCH:
        refuse(f"on {name or 'a detached HEAD'}; land a branch with commits on it")

    if is_dirty():
        refuse("the working tree has changes; commit them first (they would not be part of what lands)")

    fast_forward_ref(MAIN_BRANCH)

    if int(out(["git", "rev-list", "--count", f"{MAIN_BRANCH}..{name}"])) == 0:
        refuse(f"{name} has no commit that {MAIN_BRANCH} lacks; nothing to land")

    if run(["git", "merge-base", "--is-ancestor", MAIN_BRANCH, name], check=False).returncode != 0:
        refuse(f"{MAIN_BRANCH} has moved on since {name} branched; `sync` it first")

    run(["git", "checkout", MAIN_BRANCH])
    pull_if_tracked(MAIN_BRANCH)
    run(["git", "merge", "--ff-only", name])
    run(["git", "push", "origin", MAIN_BRANCH])
    print(f"{name} landed on {MAIN_BRANCH} and was pushed")

    delete_branch(name)


def command_sync(arguments):
    """Bring the current branch up to date with the main branch, by merging it in.

    Two branches that each appended to the same generated file - a delta at the end of a schema file, a
    refreshed inventory - conflict as soon as the first of them is merged, and that is the normal
    consequence of working in parallel, not a mistake. A merge rather than a rebase, so nothing already
    pushed is rewritten. When it conflicts, the merge stays open for the files to be resolved and then
    committed with `commit`; `--abort` puts the branch back as it was.
    """
    in_progress = run(["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], check=False).returncode == 0

    if arguments.abort:
        if not in_progress:
            refuse("no merge is open")

        run(["git", "merge", "--abort"])
        print("merge aborted, the branch is as it was")
        return

    branch = current_branch()

    if branch == MAIN_BRANCH:
        refuse(f"already on {MAIN_BRANCH}; sync is for a branch that has to catch up with it")

    if in_progress:
        refuse("a merge is already open: resolve it and commit, or sync --abort")

    # Files nobody has added stay where they are; only a change to something tracked would be mixed in.
    if out(["git", "status", "--porcelain", "--untracked-files=no"]):
        refuse("the working tree has changes; commit them first, a merge would mix them in")

    run(["git", "fetch", "origin"], check=False)

    merged = run(["git", "merge", "--no-edit", f"origin/{MAIN_BRANCH}"], check=False)

    if merged.returncode == 0:
        print(f"{branch} is up to date with {MAIN_BRANCH}")
        return

    conflicted = out(["git", "diff", "--name-only", "--diff-filter=U"]).splitlines()

    sys.stderr.write(f"conflicts in {len(conflicted)} file(s), the merge is open:\n")

    for name in conflicted:
        sys.stderr.write(f"  {name}\n")

    sys.stderr.write("Resolve them, then `commit --path <files>`; `sync --abort` gives up.\n")
    sys.exit(1)


def command_test_branch(arguments):
    """Build one branch that carries several others, so a round of testing is one checkout.

    Several open pull requests means several things to click through, and clicking through them one
    branch at a time tests each against a different starting point - which is not what any of them
    will be merged into. This collects them onto master instead, once, and says what went in.

    Rebuilt rather than updated: the branches move while they are being reviewed, and a test branch
    that has been merged into repeatedly stops resembling what master would get. It is thrown away
    and made again, which is also why it refuses to overwrite one that has commits of its own -
    a fix typed onto the test branch during a test round is exactly what must not be silently lost.

    It stays local. A test branch carries no work of its own - everything in it is on the branches it
    merged - so pushing one puts a branch on GitHub that nobody reviews, nothing merges and everybody
    has to wonder about. `--delete` removes one that got there anyway.
    """
    if arguments.delete:
        return delete_test_branch(arguments.name)

    if not arguments.branches:
        refuse("name the branches to collect")

    if is_dirty() and not arguments.carry:
        refuse("the working tree has changes; commit or stash them before rebuilding a test branch "
               "(--carry if they are files that can stay where they are)")

    run(["git", "fetch", "origin"], check=False)

    missing = [branch for branch in arguments.branches
               if run(["git", "rev-parse", "--verify", "--quiet", branch],
                      check=False).returncode != 0]

    if missing:
        refuse(f"no such branch: {', '.join(missing)}")

    exists = run(["git", "rev-parse", "--verify", "--quiet", arguments.name], check=False).returncode == 0

    if exists:
        if not arguments.recreate:
            refuse(f"{arguments.name} already exists; --recreate throws it away and builds it again")

        own = own_commits(arguments.name, arguments.branches)

        if own and not arguments.force:
            refuse(f"{arguments.name} carries {len(own)} commit(s) that are on no other branch - "
                   f"{own[0]}. Move them somewhere they survive, or --force to lose them.")

        if arguments.name == current_branch():
            run(["git", "checkout", MAIN_BRANCH])

        run(["git", "branch", "-D", arguments.name])

    run(["git", "checkout", MAIN_BRANCH])

    if run(["git", "rev-parse", "--abbrev-ref", f"{MAIN_BRANCH}@{{upstream}}"], check=False).returncode == 0:
        run(["git", "pull", "--ff-only"])

    run(["git", "checkout", "-b", arguments.name])

    for branch in arguments.branches:
        # --no-ff throughout: the point of the branch is which pieces are in it, and a fast-forward
        # would leave the first one indistinguishable from the base.
        merged = run(["git", "merge", "--no-ff", "--no-edit", branch], check=False)

        if merged.returncode != 0:
            run(["git", "merge", "--abort"], check=False)
            refuse(f"'{branch}' conflicts with what is already on {arguments.name}. "
                   f"Merge it by hand to see the conflict, or leave it out of the round.")

        print(f"  + {branch}")

    print(f"\non {arguments.name}, carrying {len(arguments.branches)} branch(es) on top of {MAIN_BRANCH}")


def delete_test_branch(name):
    """Removes a test branch here and, if it ever got there, on the remote."""
    if not name.startswith("test/"):
        refuse(f"'{name}' is not a test branch; this deletes test/* only")

    if name == current_branch():
        run(["git", "checkout", MAIN_BRANCH])

    if run(["git", "rev-parse", "--verify", "--quiet", name], check=False).returncode == 0:
        run(["git", "branch", "-D", name])
        print(f"deleted {name}")

    run(["git", "fetch", "origin", "--prune"], check=False)

    if run(["git", "rev-parse", "--verify", "--quiet", f"origin/{name}"], check=False).returncode == 0:
        run(["git", "push", "origin", "--delete", name])
        print(f"deleted origin/{name}")


def own_commits(name, branches):
    """Commit subjects on `name` that no other branch and no base carries.

    A test branch is disposable, but only as long as everything in it exists somewhere else. What
    this looks for is the fix somebody typed while testing and has not moved yet - the one thing a
    rebuild would destroy without a trace.
    """
    reachable = [f"^{branch}" for branch in [MAIN_BRANCH, f"origin/{MAIN_BRANCH}", *branches]
                 if run(["git", "rev-parse", "--verify", "--quiet", branch], check=False).returncode == 0]

    return out(["git", "log", "--no-merges", "--format=%h %s", name, *reachable]).splitlines()


def delete_branch(name, force=False):
    """Remove a branch here and on the remote, once its commits are on master.

    The guard is the whole point: a branch is deleted when its work has landed, and "landed" is
    something git can answer rather than something to remember. `--force` is for the one branch the
    guard is wrong about: a test branch is built to be thrown away, so its merge commits reaching no
    other branch is what it is for and not a warning.

    "Landed" is asked as `git cherry`: of the commits this branch has and master does not, is there
    one whose change is not on master anyway. That is the question, and neither of the obvious
    substitutes answers it. `merge-base --is-ancestor` says no for a branch that collected several
    pull requests for a round of testing - its merge commits went nowhere, though every line in it
    is on master. Comparing the trees says no as soon as master has moved on, which it always has.
    """
    if name == MAIN_BRANCH:
        refuse(f"{MAIN_BRANCH} is not a branch to delete")

    if name == current_branch():
        refuse(f"on {name} right now; switch away first")

    # Merging a pull request deletes its branch, so asking for one again is the normal way to find
    # out it is already gone - and that deserves a sentence rather than git's "unknown commit".
    here = run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False).returncode == 0

    run(["git", "fetch", "origin"], check=False)

    # A branch that only the remote still has - the other machine's, or one the pull request left behind -
    # is judged by its remote copy and removed there.
    there = run(["git", "ls-remote", "--exit-code", "--heads", "origin", name], check=False).returncode == 0

    if not here and not there:
        print(f"{name} does not exist, here or on origin; nothing to delete")
        return

    ref = name if here else f"origin/{name}"
    main = f"origin/{MAIN_BRANCH}"
    unmerged = [line for line in out(["git", "cherry", main, ref]).splitlines() if line.startswith("+")]

    if unmerged and not force:
        refuse(f"{name} has {len(unmerged)} commit(s) that reached no branch; merge or drop them "
               "deliberately (--force if throwing them away is the point)")

    merged = run(["git", "merge-base", "--is-ancestor", ref, main], check=False).returncode == 0

    # -D rather than -d: a branch whose tree matches master but whose merge commits never landed is
    # not "merged" in git's sense, and the check above has already established there is nothing to
    # lose.
    if here:
        run(["git", "branch", "-D" if not merged else "-d", name])

    if there:
        run(["git", "push", "origin", "--delete", name])
        print(f"deleted {name}, here and on origin" if here else f"deleted origin/{name}")
    else:
        print(f"deleted {name}")


def command_branches(arguments):
    """List every branch here and on origin and whether its work has landed; `--prune` removes those that have.

    The listing is the answer to "which branches can go", which used to be a hand-built loop over
    `git branch -a`. Landed is the same question `branch --delete` asks, so a branch listed as landed is
    one that command accepts.
    """
    run(["git", "fetch", "--prune", "origin"], check=False)

    local = set(out(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"]).split())
    remote = {line.removeprefix("origin/") for line in
              out(["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"]).split()
              if line not in ("origin", "origin/HEAD")}
    main = f"origin/{MAIN_BRANCH}"
    current = current_branch()
    removable = []

    for name in sorted((local | remote) - {MAIN_BRANCH}):
        ref = name if name in local else f"origin/{name}"
        unmerged = [line for line in out(["git", "cherry", main, ref]).splitlines() if line.startswith("+")]
        where = "here+origin" if name in local and name in remote else "here" if name in local else "origin"
        state = f"{len(unmerged)} commit(s) not on {MAIN_BRANCH}" if unmerged else "landed"

        print(f"{name:55} {where:12} {state}{'  (checked out)' if name == current else ''}")

        if not unmerged and name != current:
            removable.append(name)

    if arguments.prune:
        for name in removable:
            delete_branch(name)
    elif removable:
        print(f"\n{len(removable)} landed; `branches --prune` removes them here and on origin")


def stage_paths(paths):
    """Stage the given paths, forcing only the ones named as a file.

    A few tracked data files sit under a directory .gitignore also lists, so naming one of those
    explicitly has to override the ignore - that is what `--path some/ignored/file.json` is for, and
    it is a deliberate act about one file.

    A directory is the opposite, and the repository root most of all: `--path .` with the same
    override force-added bin/, obj/ and .idea/ - a quarter of a gigabyte of build output, pushed
    before anyone could read the diff. It happened. So the root is refused outright and any other
    directory is staged the ordinary way, with .gitignore keeping its say.

    Resolved against REPO rather than the process's directory, because that is where git runs.
    """
    for path in paths:
        if os.path.realpath(os.path.join(REPO, path)) == os.path.realpath(REPO):
            refuse("--path . is not an explicit path, it is the whole tree. Name the files this "
                   "commit is about, or pass --all if you really mean everything.")

    files = [path for path in paths if os.path.isfile(os.path.join(REPO, path))]
    rest = [path for path in paths if not os.path.isfile(os.path.join(REPO, path))]

    if files:
        run(["git", "add", "-f", *files])

    if rest:
        run(["git", "add", *rest])


def merge_in_progress():
    """Whether a merge is waiting to be concluded.

    A merge that resolves to exactly what the branch already had stages nothing, and refusing it as
    "nothing staged" leaves the merge open with no way through this script - which is how a stacked
    branch carrying the same fix as the branch below it ends up needing a bare git commit.
    """
    return os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD"))


def command_commit(arguments):
    guard_not_main(arguments, "commit")

    text = message_text(arguments)

    if arguments.all:
        run(["git", "add", "-A"])
    elif arguments.path:
        stage_paths(arguments.path)

    if not out(["git", "diff", "--cached", "--name-only"]) and not merge_in_progress():
        refuse("nothing staged. Pass --all, or --path, or stage it yourself.")

    run_with_input(["git", "commit", "-F", "-", *(["--signoff"] if arguments.signoff else [])], text)

    print(out(["git", "log", "--oneline", "-1"]))


def command_uncommit(arguments):
    """Take the last commit apart again, keeping every change staged.

    The usual reason is a `commit --all` that swept up a file belonging to a different change, so the
    message on record describes work the commit does not only contain. Nothing is lost: the changes
    stay staged and are re-committed, normally as two commits.

    Refused once the commit is pushed. Rewriting what someone may have pulled is a different and much
    louder operation than this one, and it should not hide behind the same verb.
    """
    guard_not_main(arguments, "uncommit")

    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                   check=False).stdout.strip()

    if upstream:
        ahead = out(["git", "rev-list", "--count", f"{upstream}..HEAD"])

        if ahead == "0":
            refuse(f"the last commit is already on {upstream}; undoing it would rewrite pushed history")

    subject = out(["git", "log", "--oneline", "-1"])
    run(["git", "reset", "--soft", "HEAD~1"])

    print(f"undone, changes kept staged: {subject}")


def command_push(arguments):
    guard_not_main(arguments, "push")

    branch = current_branch()

    # A test branch carries nothing of its own, so a copy on GitHub is a branch nobody reviews and
    # nothing merges. `test-branch` lost its --push option for that reason and the rule was written
    # down - and then broken from here, because this is the other way to reach the same remote.
    if branch.startswith("test/"):
        refuse(f"{branch} is a test branch and stays local; "
               f"'test-branch {branch} --delete' removes one that got pushed")

    guard_schema_matches_model()

    run(["git", "push", "-u", "origin", branch], capture=False)


def guard_schema_matches_model():
    """Refuse a push whose db-init schema disagrees with the schema the model builds.

    The local `verify` compares schema.sql with the development database, and that database is
    whatever was applied to it by hand - an index named after the entity instead of the table, a
    default left off, passed it and failed in CI afterwards, on master, twice in one afternoon. CI
    compares against the schema EF itself builds from the model; this runs the same comparison
    before the branch leaves the machine, and only when the branch touches something that can change
    the schema (`db_init.watched_paths`), because it takes a minute or two.

    Needs `db_init.model_script`, the `dotnet.py script` that writes the model's CREATE script. A
    project without it, or a machine that cannot reach the database, is not asked: blocking a push
    over a comparison that cannot run would only teach people to push around it.
    """
    model_script = project.get("db_init.model_script")
    watched = tuple(project.get("db_init.watched_paths", []))

    if not model_script or not watched:
        return

    changed = out(["git", "diff", "--name-only", f"origin/{MAIN_BRANCH}...HEAD"]).splitlines()

    if not any(path.startswith(watched) for path in changed):
        return

    if run([sys.executable, os.path.join(SCRIPTS, "db.py"), "connection"], check=False).returncode != 0:
        print("schema check skipped: the database cannot be reached")

        return

    print("Checking schema.sql against the model before pushing ...")

    reference = os.path.join(tempfile.mkdtemp(), "model.sql")
    created = run([sys.executable, os.path.join(SCRIPTS, "dotnet.py"), "script", model_script, "--", reference],
                  check=False)

    if created.returncode != 0:
        refuse("the model's CREATE script could not be written:\n" + (created.stderr or created.stdout)[-1500:])

    compared = run([sys.executable, os.path.join(SCRIPTS, "verify_db_init.py"), "--reference", reference],
                   check=False)

    if compared.returncode != 0:
        refuse("schema.sql does not match the model; CI would fail on this.\n"
               + (compared.stdout or compared.stderr)[-2500:])


def command_move(arguments):
    """Rename tracked files, all of them or none.

    A chain of `git mv` lines does the same thing right up to the moment one of them fails, and then
    the tree is half moved - some files renamed, the rest not, and nothing saying which is which.
    Everything below is checked before the first rename, and anything already done is put back if a
    rename fails anyway.
    """
    if len(arguments.paths) % 2 != 0:
        refuse("needs pairs: move SRC DST [SRC DST ...]")

    pairs = list(zip(arguments.paths[0::2], arguments.paths[1::2]))
    tracked = set(out(["git", "ls-files"]).splitlines())
    destinations = set()

    for source, destination in pairs:
        if source not in tracked:
            refuse(f"{source} is not tracked by git")

        if os.path.exists(os.path.join(REPO, destination)):
            refuse(f"{destination} already exists")

        if destination in destinations:
            refuse(f"two sources want to become {destination}")

        destinations.add(destination)

    done = []

    for source, destination in pairs:
        os.makedirs(os.path.dirname(os.path.join(REPO, destination)), exist_ok=True)

        result = run(["git", "mv", source, destination], check=False)

        if result.returncode != 0:
            for undo_source, undo_destination in reversed(done):
                run(["git", "mv", undo_destination, undo_source], check=False)

            sys.stderr.write((result.stderr or result.stdout or "").strip() + "\n")
            refuse(f"{source} -> {destination} failed; the earlier {len(done)} move(s) were undone")

        done.append((source, destination))
        print(f"{source} -> {destination}")


def command_restore(arguments):
    """Throw away the uncommitted changes of tracked files, back to the last commit.

    Only tracked files: a new file has no committed version to go back to, and deleting it is a
    different decision this refuses to take on the side. What is thrown away cannot be recovered,
    so every file is named first.

    With --source REV the files go back to how that revision had them instead, in the working tree
    only - for a generated file whose appended part has to be produced again from its inputs. The
    result is an ordinary uncommitted change.
    """
    changed = set(out(["git", "diff", "--name-only", arguments.source or "HEAD", "--", *arguments.paths]).splitlines())
    tracked = set(out(["git", "ls-files"]).splitlines())
    targets = []

    for path in arguments.paths:
        if not any(name == path or name.startswith(path.rstrip("/") + "/") for name in tracked):
            refuse(f"{path} is not tracked by git - there is no committed version to restore")

        targets.extend(name for name in changed if name == path or name.startswith(path.rstrip("/") + "/"))

    if not targets:
        print("nothing to restore - no uncommitted changes there")
        return

    if arguments.source:
        run(["git", "restore", f"--source={arguments.source}", "--worktree", "--", *arguments.paths], capture=False)
    else:
        run(["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", *arguments.paths], capture=False)

    for name in sorted(targets):
        print(f"restored  {name}" + (f"  (as of {arguments.source})" if arguments.source else ""))


def command_untrack(arguments):
    """Stop tracking paths that are already committed, without deleting them.

    For something that was swept into a commit and does not belong in the repository - a browser
    capture, a downloaded dump, a scratch file. The files stay on disk; only git forgets them, and
    they are named so the next `commit --all` does not take them straight back.
    """
    tracked = set(out(["git", "ls-files"]).splitlines())
    targets = []

    for path in arguments.paths:
        matching = [name for name in tracked if name == path or name.startswith(path.rstrip("/") + "/")]

        if not matching:
            refuse(f"{path} is not tracked by git")

        targets.extend(matching)

    run(["git", "rm", "--cached", "-r", "--quiet", *arguments.paths], capture=False)

    for name in sorted(targets):
        print(f"untracked  {name}")

    print(f"\n{len(targets)} file(s) still on disk. Add them to .gitignore, "
          "or the next 'commit --all' tracks them again.")


def command_ignored(arguments):
    """Say whether paths are ignored, and by which rule.

    The question worth asking before a `commit --all`, because the answer is not guessable: this
    repository ignores /Data but tracks /data, and a path that looks covered may not be.
    """
    for path in arguments.paths:
        result = run(["git", "check-ignore", "-v", "--no-index", path], check=False)
        line = (result.stdout or "").strip()

        if not line:
            listed = run(["git", "ls-files", "--error-unmatch", path], check=False)

            print(f"{path}: tracked" if listed.returncode == 0
                  else f"{path}: not ignored and not tracked - a 'commit --all' would take it")
            continue

        source, pattern = line.split("\t")[0].rsplit(":", 1)[0], line.split(":")[2].split("\t")[0]
        print(f"{path}: ignored by {source} rule '{pattern}'")


def gitleaks_findings(mode, target, scratch):
    """gitleaks' findings over one target, read from its JSON report rather than its log lines."""
    report = os.path.join(scratch, f"{mode}.json")
    run(["gitleaks", mode, target, "--redact", "--no-banner", "--log-level", "warn", "--exit-code", "0",
         "--report-format", "json", "--report-path", report])

    with open(report, encoding="utf-8") as handle:
        return json.load(handle) or []


def command_leaks(_arguments):
    """Scan for secrets with gitleaks before a repository goes public, or whenever in doubt.

    Two scans, because a secret can sit in either place: the whole history, where a value deleted
    since is still readable, and the files a commit would take - tracked and new, ignored ones left
    out. `gitleaks dir .` alone reads build/, target/ and obj/ as well, megabytes of output that
    nobody publishes, so the working files are copied aside and scanned there. Secrets are redacted.
    """
    if shutil.which("gitleaks") is None:
        refuse("gitleaks is missing: 'sudo dnf install gitleaks', or a release from "
               "https://github.com/gitleaks/gitleaks")

    commits = int(out(["git", "rev-list", "--count", "--all"]) or 0)
    files = working_files()
    lines = []

    with tempfile.TemporaryDirectory(prefix="leaks-") as scratch:
        if commits:
            for finding in gitleaks_findings("git", REPO, scratch):
                lines.append(f"{finding['Commit'][:10]}  {finding['File']}:{finding['StartLine']}  "
                             f"{finding['RuleID']}")

        tree = os.path.join(scratch, "tree")

        for name in files:
            os.makedirs(os.path.dirname(os.path.join(tree, name)), exist_ok=True)
            shutil.copy2(os.path.join(REPO, name), os.path.join(tree, name), follow_symlinks=False)

        if files:
            for finding in gitleaks_findings("dir", tree, scratch):
                name = os.path.relpath(finding["File"], tree)
                lines.append(f"{'working':10}  {name}:{finding['StartLine']}  {finding['RuleID']}")

    if lines:
        print("\n".join(lines))
        print(f"\n{len(lines)} finding(s). A real secret is rotated first, then removed - from the history "
              "too, if it was ever committed. A false positive goes into .gitleaksignore.")
        sys.exit(1)

    print(f"no leaks: {commits} commit(s) of history, {len(files)} working file(s)")


# --- issues ----------------------------------------------------------------------------------

def command_issues(arguments):
    command = ["gh", "issue", "list", "--state", arguments.state, "--limit", str(arguments.limit),
               "--json", "number,title,labels,updatedAt"]

    if arguments.label:
        command += ["--label", arguments.label]

    # `gh` takes one --search, so the milestone filter and a text search share it. Terms are ANDed by
    # GitHub; `--search 'a OR b'` is how a word list is asked for.
    search = []

    if arguments.milestone == "none":
        search.append("no:milestone")
    elif arguments.milestone:
        command += ["--milestone", milestone_title(arguments.milestone)]

    if arguments.search:
        search.append(arguments.search)

    if search:
        command += ["--search", " ".join(search)]

    for issue in json.loads(out(command)):
        labels = ",".join(label["name"] for label in issue["labels"])
        print(f"{issue['number']}\t{issue['updatedAt'][:10]}\t{issue['title']}\t[{labels}]")


def command_digest(arguments):
    """What was actually finished in a window of days, grouped by area.

    Assembled from what GitHub records rather than from memory: an issue is in here because it was
    closed, not because anybody remembers closing it. The grouping is by label, which is the only
    thing in the data that says what an issue was *about* - a heading a script can defend.

    `since` is the day the window opens; GitHub filters `updated`, so a closure has to be checked
    again on the way out or an issue merely touched in the window is counted as done in it.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=arguments.days)).strftime("%Y-%m-%d")

    issues = json.loads(out(["gh", "issue", "list", "--state", "closed", "--limit", "500",
                             "--search", f"closed:>={since}",
                             "--json", "number,title,labels,closedAt"]))
    issues.sort(key=lambda issue: issue["closedAt"])

    merged = json.loads(out(["gh", "pr", "list", "--state", "merged", "--limit", "500",
                             "--search", f"merged:>={since}", "--json", "number"]))
    open_issues = json.loads(out(["gh", "issue", "list", "--state", "open", "--limit", "500",
                                  "--json", "number"]))

    print(f"{len(issues)} issue(s) closed and {len(merged)} pull request(s) merged "
          f"since {since}; {len(open_issues)} issue(s) still open")

    if not issues:
        return

    # An issue usually carries several labels, and the interesting one is never "enhancement" or
    # "bug" - those say how it arrived, not what it touched. Prefer any other label as the heading,
    # and count an issue once, under the first of them.
    generic = {"enhancement", "bug", "documentation", "question", "chore"}
    by_area = collections.defaultdict(list)

    for issue in issues:
        names = [label["name"] for label in issue["labels"]]
        specific = [name for name in names if name not in generic]
        by_area[(specific or names or ["unlabelled"])[0]].append(issue)

    for area in sorted(by_area, key=lambda name: (-len(by_area[name]), name)):
        print()
        print(f"{area} ({len(by_area[area])})")

        for issue in by_area[area]:
            print(f"  {issue['closedAt'][:10]}  #{issue['number']:<5} {issue['title']}")


def command_issue(arguments):
    """One issue - or a related group of them - with the discussion that followed each.

    The comments are part of the issue, not an extra: a decision taken after the body was written
    lives only there, and reading the body alone means acting on the plan as it stood before the
    thread corrected it.

    Several numbers are accepted because issues arrive in groups: an epic and its phases, a bug and
    the two it turned out to share a cause with. Reading them one call at a time invites reading
    half of them.
    """
    for index, number in enumerate(arguments.number):
        if index:
            print()
            print("=" * 100)
            print()

        print_issue(number, arguments.no_comments)


def print_issue(number, no_comments):
    issue = json.loads(out(["gh", "issue", "view", str(number),
                            "--json", "number,title,state,labels,body,comments"]))
    labels = ", ".join(label["name"] for label in issue["labels"])

    print(f"#{issue['number']}  {issue['title']}  ({issue['state']})")
    print(f"labels: {labels or '-'}")
    print()
    print(issue["body"])

    if no_comments:
        return

    for comment in issue.get("comments") or []:
        author = (comment.get("author") or {}).get("login", "?")

        print()
        print(f"--- comment by {author} on {comment.get('createdAt', '')[:10]} " + "-" * 40)
        print()
        print(comment.get("body", ""))


def command_issue_create(arguments):
    command = ["gh", "issue", "create", "--title", arguments.title, "--body-file", "-",
               "--assignee", ASSIGNEE]

    if arguments.label:
        command += ["--label", arguments.label]

    if arguments.milestone:
        command += ["--milestone", milestone_title(arguments.milestone)]

    print(run_with_input(command, message_text(arguments)))


def command_assign(arguments):
    """Give every open issue and pull request an assignee, for the ones that slipped through.

    Anything opened through this script carries one from the start. Things opened in the browser, or
    before that was the rule, do not - and an unassigned item on a one-maintainer repository does not
    say "unclaimed", it says the field was forgotten.
    """
    issues = json.loads(out(["gh", "issue", "list", "--state", "open", "--limit", "500",
                             "--json", "number,title,assignees"]))
    pulls = json.loads(out(["gh", "pr", "list", "--state", "open", "--limit", "500",
                            "--json", "number,title,assignees"]))

    work = [("issue", item) for item in issues if not item["assignees"]]
    work += [("pr", item) for item in pulls if not item["assignees"]]

    if not work:
        print("every open issue and pull request already has an assignee")
        return

    for kind, item in work:
        if arguments.dry_run:
            print(f"would assign {kind} #{item['number']}  {item['title']}")
            continue

        run(["gh", kind, "edit", str(item["number"]), "--add-assignee", ASSIGNEE])
        print(f"assigned {kind} #{item['number']}  {item['title']}")

    print(f"\n{len(work)} item(s){' would be' if arguments.dry_run else ''} assigned to {ASSIGNEE}")


def command_issue_edit(arguments):
    """Replace an issue's body, its title, or both.

    An issue's title goes stale the same way a PR's does - a bug that turns out to be louder or
    narrower than it was filed as is no longer described by the line it was opened with - so both are
    editable here and either one on its own is enough.

    Labels move for the same reason, and parking one is the common case: the decision is made, the
    work waits behind something else, and the label is how that is said out loud.
    """
    if not (arguments.title or arguments.milestone or arguments.add_label or arguments.remove_label
            or arguments.file or arguments.message):
        refuse("nothing to change. Pass --title, --milestone, --add-label, --remove-label, "
               "or a body via --file / -m.")

    command = ["gh", "issue", "edit", str(arguments.number)]

    if arguments.title:
        command += ["--title", arguments.title]

    if arguments.milestone == "none":
        command += ["--remove-milestone"]
    elif arguments.milestone:
        command += ["--milestone", milestone_title(arguments.milestone)]

    if arguments.add_label:
        command += ["--add-label", arguments.add_label]

    if arguments.remove_label:
        command += ["--remove-label", arguments.remove_label]

    if not (arguments.file or arguments.message):
        print(run(command).stdout.strip())
        return

    print(run_with_input(command + ["--body-file", "-"], message_text(arguments)))


def command_pr_edit(arguments):
    """Replace a pull request's title, its body, its labels, or all of them.

    The title moves as often as the body does - a PR that grew a second fix while it was open is
    describing something wider than it was opened for - so both are editable here and either one on
    its own is enough.

    Labels for the same reason they are editable on an issue, and parking is again the common case:
    an issue could be marked `parked` while the pull request in front of it could not, so the list of
    open pull requests said nothing and every one of them had to be opened to find out.
    """
    if not (arguments.title or arguments.add_label or arguments.remove_label
            or arguments.file or arguments.message):
        refuse("nothing to change. Pass --title, --add-label, --remove-label, "
               "or a body via --file / -m.")

    command = ["gh", "pr", "edit", str(arguments.number)]

    if arguments.title:
        command += ["--title", arguments.title]

    if arguments.add_label:
        command += ["--add-label", arguments.add_label]

    if arguments.remove_label:
        command += ["--remove-label", arguments.remove_label]

    if not (arguments.file or arguments.message):
        print(run(command).stdout.strip())
        return

    body = message_text(arguments)
    print(run_with_input(command + ["--body-file", "-"], body))


def command_comment(arguments):
    print(run_with_input(["gh", "issue", "comment", str(arguments.number), "--body-file", "-"],
                         message_text(arguments)))


def command_close(arguments):
    if arguments.file or arguments.message:
        command_comment(arguments)

    run(["gh", "issue", "close", str(arguments.number), "--reason", arguments.reason], capture=False)


def command_label(arguments):
    if not arguments.name:
        labels = json.loads(out(["gh", "label", "list", "--limit", "200",
                                 "--json", "name,description"]))

        for label in sorted(labels, key=lambda entry: entry["name"]):
            print(f"  {label['name']:<20} {label.get('description', '')}")

        return

    command = ["gh", "label", "create", arguments.name, "--force"]

    if arguments.color:
        command += ["--color", arguments.color]

    if arguments.description:
        command += ["--description", arguments.description]

    run(command, capture=False)


# What every repository gets besides GitHub's own ten; a project adds its areas as [[repo.labels]] in project.toml.
SHARED_LABELS = [
    {"name": "dependencies", "color": "0366d6", "description": "Package and SDK updates"},
    {"name": "epic", "color": "5319e7", "description": "Parent issue that tracks a group of issues"},
    {"name": "parked", "color": "cfd3d7", "description": "Deliberately postponed"},
    {"name": "ci", "color": "fbca04", "description": "Build, test and publish pipelines"},
]


def create_labels():
    """Create (or update, `--force`) the shared labels and the project's own. Returns the names that failed."""
    failed = []

    for label in SHARED_LABELS + list(project.get("repo.labels", [])):
        command = ["gh", "label", "create", label["name"], "--force"]

        if label.get("color"):
            command += ["--color", label["color"]]

        if label.get("description"):
            command += ["--description", label["description"]]

        if run(command, check=False).returncode != 0:
            failed.append(label["name"])

    return failed


def command_labels(_arguments):
    failed = create_labels()

    if failed:
        refuse(f"could not create: {', '.join(failed)}")

    print(f"{len(SHARED_LABELS) + len(project.get('repo.labels', []))} labels in place")


# --- milestones ------------------------------------------------------------------------------

MILESTONES = "repos/{owner}/{repo}/milestones"
DUE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def milestone_list(state="all"):
    return json.loads(out(["gh", "api", f"{MILESTONES}?state={state}&per_page=100"]))


def milestone_number(name):
    """A milestone is addressed by title as well as by number, because those are two different tools.

    `gh issue --milestone` takes the title and that is what a person remembers; the REST API, which is
    the only way to create or change one, takes the number. Accepting both means neither has to be
    looked up by hand first.
    """
    if str(name).isdigit():
        return int(name)

    for milestone in milestone_list():
        if milestone["title"] == name:
            return milestone["number"]

    refuse(f"no milestone titled {name!r}")


def milestone_title(name):
    """The other direction of milestone_number: `gh issue --milestone` only understands the title, so a
    number the help text promises to accept has to be turned into one first."""
    if not str(name).isdigit():
        return name

    for milestone in milestone_list():
        if milestone["number"] == int(name):
            return milestone["title"]

    refuse(f"no milestone numbered {name}")


def milestone_payload(arguments):
    payload = {}

    if getattr(arguments, "title", None):
        payload["title"] = arguments.title

    if arguments.description:
        payload["description"] = arguments.description

    if arguments.due:
        if not DUE_DATE.fullmatch(arguments.due):
            refuse(f"--due takes YYYY-MM-DD, not {arguments.due!r}")

        # Noon, not midnight: GitHub resolves due_on against the account's own time zone and stores
        # the date it lands on there, so a midnight-UTC timestamp comes back as the day before for
        # any account west of Greenwich. Noon survives every offset.
        payload["due_on"] = f"{arguments.due}T12:00:00Z"

    return payload


def print_milestone(milestone):
    due = (milestone.get("due_on") or "")[:10] or "-"

    print(f"{milestone['number']}\t{milestone['state']}\t{due}\t"
          f"{milestone['open_issues']} open / {milestone['closed_issues']} closed\t"
          f"{milestone['title']}")


def command_milestones(arguments):
    if not arguments.name:
        for milestone in milestone_list(arguments.state):
            print_milestone(milestone)

        return

    number = milestone_number(arguments.name)
    milestone = json.loads(out(["gh", "api", f"{MILESTONES}/{number}"]))

    print_milestone(milestone)

    if milestone.get("description"):
        print(milestone["description"])

    print()

    issues = json.loads(out(["gh", "issue", "list", "--milestone", milestone["title"],
                             "--state", "all", "--limit", "200",
                             "--json", "number,title,state,labels"]))

    for issue in issues:
        labels = ",".join(label["name"] for label in issue["labels"])
        print(f"  #{issue['number']:<5} {issue['state'].lower():<6} {issue['title']}\t[{labels}]")


def command_milestone_create(arguments):
    milestone = json.loads(run_with_input(
        ["gh", "api", "--method", "POST", MILESTONES, "--input", "-"],
        json.dumps(milestone_payload(arguments))))

    print_milestone(milestone)


def command_milestone_edit(arguments):
    payload = milestone_payload(arguments)

    if arguments.reopen:
        payload["state"] = "open"

    if not payload:
        refuse("nothing to change. Pass --title, --due, --description or --reopen.")

    number = milestone_number(arguments.name)
    milestone = json.loads(run_with_input(
        ["gh", "api", "--method", "PATCH", f"{MILESTONES}/{number}", "--input", "-"],
        json.dumps(payload)))

    print_milestone(milestone)


def command_milestone_close(arguments):
    number = milestone_number(arguments.name)
    milestone = json.loads(run_with_input(
        ["gh", "api", "--method", "PATCH", f"{MILESTONES}/{number}", "--input", "-"],
        json.dumps({"state": "closed"})))

    if milestone["open_issues"]:
        print(f"note: {milestone['open_issues']} issue(s) in it are still open")

    print_milestone(milestone)


# --- pull requests ---------------------------------------------------------------------------

def command_pr_close(arguments):
    """Close a pull request without merging it, optionally saying why.

    The counterpart to `close` for issues, and wanted for the same reason: work that was opened and
    then turned out not to be needed. The branch goes with it unless --keep-branch says otherwise -
    an abandoned branch left behind is the thing nobody remembers to clean up later.
    """
    if arguments.file or arguments.message:
        run_with_input(["gh", "pr", "comment", str(arguments.number), "--body-file", "-"],
                       message_text(arguments))

    command = ["gh", "pr", "close", str(arguments.number)]

    if not arguments.keep_branch:
        command.append("--delete-branch")

    run(command, capture=False)


def command_pr(arguments):
    if arguments.number is None:
        for pull in json.loads(out(["gh", "pr", "list", "--state", "open",
                                    "--json", "number,title,headRefName"])):
            print(f"{pull['number']}\t{pull['headRefName']}\t{pull['title']}")
        return

    pull = json.loads(out(["gh", "pr", "view", str(arguments.number),
                           "--json", ("number,title,state,isDraft,mergeable,mergeStateStatus,"
                                      "baseRefName,headRefName,url,body")]))

    print(f"#{pull['number']}  {pull['title']}")
    print(f"{pull['state']}{' (draft)' if pull['isDraft'] else ''}  "
          f"{pull['headRefName']} -> {pull['baseRefName']}  "
          f"{pull['mergeable']}/{pull['mergeStateStatus']}")
    print(pull["url"])
    print()
    print(pull["body"])


def command_pr_create(arguments):
    branch = current_branch()

    if branch == MAIN_BRANCH:
        refuse(f"cannot open a pull request from {MAIN_BRANCH}")

    body = message_text(arguments)
    command = ["gh", "pr", "create", "--title", arguments.title, "--body-file", "-",
               "--assignee", ASSIGNEE]

    if arguments.draft:
        command.append("--draft")

    if arguments.base:
        command += ["--base", arguments.base]

    print(run_with_input(command, body))


def command_pr_retarget(arguments):
    """Point a pull request at another base, reopening it if a deleted base closed it."""
    pull = json.loads(out(["gh", "pr", "view", str(arguments.number), "--json", "state"]))

    if pull["state"] == "CLOSED":
        run(["gh", "pr", "reopen", str(arguments.number)], capture=False)

    run(["gh", "pr", "edit", str(arguments.number), "--base", arguments.base], capture=False)
    print(f"#{arguments.number} now targets {arguments.base}")


def dependent_pulls(branch):
    """Open pull requests that use `branch` as their base — deleting it would close them."""
    pulls = json.loads(out(["gh", "pr", "list", "--state", "open", "--base", branch,
                            "--json", "number,title"]))

    return pulls


def view_pull(number):
    return json.loads(out(["gh", "pr", "view", str(number),
                           "--json", "state,isDraft,mergeable,mergeStateStatus,title,headRefName"]))


def await_mergeability(number, attempts=8, pause=5):
    """
    GitHub computes mergeability asynchronously: right after a push, a retarget or the merge of a base
    branch it answers UNKNOWN for a few seconds. Refusing on that reports a problem where there is only
    a pending background job, so wait for a real answer before deciding.
    """
    pull = view_pull(number)

    for attempt in range(attempts):
        if pull["mergeable"] != "UNKNOWN":
            return pull

        if attempt == 0:
            print(f"#{number}: GitHub is still computing mergeability, waiting ...")

        time.sleep(pause)
        pull = view_pull(number)

    return pull


def workflow_runs(branch, limit):
    # Not a bare json.loads: gh answers with something that is not JSON when the API hiccups, and a
    # JSONDecodeError traceback out of a status command reads as a bug in this script rather than as
    # a bad minute on GitHub's side.
    answer = out(["gh", "run", "list", "--branch", branch, "--limit", str(limit),
                  "--json", "databaseId,status,conclusion,name,"
                            "createdAt,startedAt,updatedAt,headSha"])

    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        refuse(f"gh did not answer with JSON for {branch}:\n{answer[:400]}")


def run_duration(entry):
    """How long a run took, or "" while it is still going.

    Measured from `startedAt`, not `createdAt`: a re-run keeps the original creation time, so the
    difference to `updatedAt` spans everything since the first attempt - it reported 8m08s for a run
    that took 1m12s.
    """
    if entry["status"] != "completed":
        return ""

    begin = datetime.fromisoformat((entry.get("startedAt") or entry["createdAt"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(entry["updatedAt"].replace("Z", "+00:00"))
    seconds = max(int((end - begin).total_seconds()), 0)

    return f"{seconds // 60}m{seconds % 60:02d}s"


def step_durations(run_id):
    """Seconds per step of a run's jobs, so a slow pipeline can be answered with a number.

    Asked of the REST API rather than parsed out of `gh run view`: that prints a tick per step and
    no timings at all, which is the one thing worth knowing when a run takes six minutes.
    """
    jobs = json.loads(out(["gh", "api",
                           f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs",
                           "--jq", ".jobs"]))
    measured = []

    for job in jobs:
        for step in job.get("steps", []):
            started, completed = step.get("started_at"), step.get("completed_at")

            if not started or not completed:
                continue

            begin = datetime.fromisoformat(started.replace("Z", "+00:00"))
            end = datetime.fromisoformat(completed.replace("Z", "+00:00"))
            measured.append((int((end - begin).total_seconds()), job["name"], step["name"],
                             step.get("conclusion", "")))

    return measured


def command_ci(arguments):
    """The runs of a branch, and on request what each step of one cost."""
    if not HAS_CI:
        print(f"{project.name} has no CI (repo.ci = false in Scripts/project.toml): nothing to wait for, "
              f"`verify` is the check")
        return 0

    if arguments.rerun:
        if not arguments.run:
            refuse("--rerun needs --run ID: which run to start the failed jobs of again")

        # A build that failed on a file another process held is not a verdict on the code, and the run is
        # the cheapest way to find out which of the two it was.
        run(["gh", "run", "rerun", str(arguments.run), "--failed"], check=True, capture=True)
        print(f"started the failed jobs of run {arguments.run} again")

        return 0

    branch = arguments.branch or current_branch()
    runs = workflow_runs(branch, arguments.limit)

    # A branch pushed a moment ago has no run yet, and --watch is exactly for waiting on the first one.
    if not runs and not arguments.watch:
        print(f"no workflow runs for {branch}")
        return 0

    if arguments.watch:
        # The run for the commit in hand, not simply the newest one on record. A push takes a moment
        # to show up, and watching whatever was there already reports the previous run as this one's
        # result - which is how a red build reads as green.
        head = out(["git", "rev-parse", "HEAD"])
        target = next((entry for entry in runs if entry["headSha"] == head), None)
        waited = 0

        while target is None and waited < arguments.appear_timeout:
            time.sleep(arguments.interval)
            waited += arguments.interval
            runs = workflow_runs(branch, arguments.limit)
            target = next((entry for entry in runs if entry["headSha"] == head), None)

        if target is None:
            print(f"no run for {head[:8]} after {waited}s - "
                  f"pushed? the newest run below is for an older commit")
        else:
            # Every workflow of the commit, not just the first: CI and a side workflow start together,
            # and waiting for the side one alone reported "done" while CI was still going.
            for entry in (entry for entry in runs if entry["headSha"] == head and entry["status"] != "completed"):
                print(f"watching run {entry['databaseId']} ({entry['name']}) for {head[:8]} ...")
                run(["gh", "run", "watch", str(entry["databaseId"]), "--interval",
                     str(arguments.interval)], check=False, capture=True)

        runs = workflow_runs(branch, arguments.limit)

        # Report the commit's failed run if it has one - a green neighbour must not stand in for it.
        of_head = [entry for entry in runs if entry["headSha"] == head]
        target = next((entry for entry in of_head if entry["conclusion"] not in (None, "success")),
                      of_head[0] if of_head else None)

    for entry in runs:
        started = (entry.get("startedAt") or entry["createdAt"])[:19].replace("T", " ")
        state = entry["conclusion"] or entry["status"]
        print(f"{entry['databaseId']}  {state:<12} {run_duration(entry):<7} {started}  {entry['name']}")

    if not runs:
        return 1

    # A branch runs several workflows, and the newest is not necessarily the one that failed: a green
    # side workflow started a second later hid a red CI run on 2026-09-25.
    wanted = arguments.run or (str(target["databaseId"]) if arguments.watch and target else None)
    newest = next((entry for entry in runs if str(entry["databaseId"]) == wanted), None) \
        if wanted else runs[0]

    if newest is None:
        refuse(f"run {arguments.run} is not among the last {arguments.limit} runs of {branch}")

    if arguments.timings:
        print()

        for seconds, job, step, conclusion in sorted(step_durations(newest["databaseId"]),
                                                     reverse=True):
            mark = "" if conclusion in ("success", "") else f"  <- {conclusion}"
            print(f"  {seconds:4d}s  {job}: {step}{mark}")

    if newest["conclusion"] not in (None, "success"):
        print()
        print(f"failing steps of {newest['databaseId']}:")
        run(["gh", "run", "view", str(newest["databaseId"])], check=False, capture=False)

        if arguments.log:
            # The failed steps' own output, last lines only: the error sits at the end, and a full
            # build log is thousands of lines of restore and compile noise above it.
            log = run(["gh", "run", "view", str(newest["databaseId"]), "--log-failed"],
                      check=False, capture=True).stdout.splitlines()
            print()
            print("\n".join(log[-arguments.log_lines:]))

        return 1

    return 0


def command_pr_merge(arguments):
    pull = await_mergeability(arguments.number)

    if pull["state"] != "OPEN":
        refuse(f"pull request is {pull['state']}")

    if pull["isDraft"]:
        refuse("pull request is a draft")

    if pull["mergeable"] != "MERGEABLE":
        refuse(f"GitHub reports mergeable={pull['mergeable']}, "
               f"mergeStateStatus={pull['mergeStateStatus']}")

    # Deleting a branch that another open pull request is based on does not retarget that pull
    # request — GitHub closes it. Learned the hard way on #315/#317.
    stacked = dependent_pulls(pull["headRefName"])

    if stacked and not arguments.keep_branch:
        listed = ", ".join(f"#{p['number']}" for p in stacked)
        refuse(f"{listed} {'is' if len(stacked) == 1 else 'are'} based on "
               f"'{pull['headRefName']}'. Deleting it would close "
               f"{'that pull request' if len(stacked) == 1 else 'those pull requests'} instead of "
               f"retargeting {'it' if len(stacked) == 1 else 'them'}. Retarget first "
               f"(repo.py pr-retarget {stacked[0]['number']} --base master), or pass --keep-branch.")

    strategy = f"--{arguments.strategy}"
    command = ["gh", "pr", "merge", str(arguments.number), strategy]

    if not arguments.keep_branch:
        command.append("--delete-branch")

    run(command, capture=False)
    print(f"#{arguments.number} merged")


# --- wiring ----------------------------------------------------------------------------------

# --- merge conflicts -------------------------------------------------------------------------

# Everything here is generated and append-only, and every branch adds to the end of it. The correct
# merge is always the same: keep both sides, master's first. Anything else is left to a person.
APPEND_ONLY_SUFFIXES = ("deploy/db-init/schema.sql", ".Designer.cs")
TRANSLATION_PREFIX = "Scripts/translations/"

CONFLICT = re.compile(r"^<<<<<<< [^\n]*\n(?P<ours>.*?)^=======\n(?P<theirs>.*?)^>>>>>>> [^\n]*\n",
                      re.MULTILINE | re.DOTALL)


def conflicted_files():
    return [line[3:].strip() for line in out(["git", "status", "--porcelain"]).splitlines()
            if line.startswith(("UU ", "AA "))]


def resolve_append_only(path):
    """Keep both sides in order. Works because the file is generated and only ever grows."""
    absolute = os.path.join(REPO, path)

    with open(absolute, encoding="utf-8-sig") as handle:
        text = handle.read()

    resolved, count = CONFLICT.subn(
        lambda match: match["theirs"].rstrip("\n") + "\n" + match["ours"], text)

    # A leftover marker means the file held a shape this does not understand. Matched on whole lines:
    # schema.sql is full of `-- ====...` rules that must not be mistaken for a separator.
    if re.search(r"^(<{7}|={7}|>{7})(\s|$)", resolved, re.MULTILINE):
        return None

    with open(absolute, "w", encoding="utf-8-sig") as handle:
        handle.write(resolved)

    return count


def resolve_translation_json(path):
    """Merge by key: master's order, then what this branch added or changed, minus what either side dropped.

    A key this branch left as it found it keeps master's version. Taking every key of this branch instead put
    the branch's older text over a translation master had added to an existing key in the meantime, and the
    keys then read as missing again.
    """
    def stage(number):
        return json.loads(out(["git", "show", f":{number}:{path}"]),
                          object_pairs_hook=collections.OrderedDict)

    base, ours, theirs = stage(1), stage(2), stage(3)
    merged = collections.OrderedDict(theirs)

    for key, value in ours.items():
        if key not in base or base[key] != value:
            merged[key] = value

    for key in base:
        if key not in ours or key not in theirs:
            merged.pop(key, None)

    with open(os.path.join(REPO, path), "w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    return len(merged) - len(theirs)


# One typed accessor of a generated resource Designer file, whole.
DESIGNER_ACCESSOR = re.compile(
    r"^        public static string (?P<name>\w+) \{\n            get \{\n.*?\n            \}\n        \}\n",
    re.MULTILINE | re.DOTALL)


def resolve_designer(path):
    """Master's file, plus the accessors only this branch has.

    Resolving the conflict hunks as they stand splits an accessor in two whenever the two sides end in the same
    closing braces - git then reports the shared lines as common - and the file stops compiling. The accessors are
    the unit here, not the hunks.
    """
    def stage(number):
        return run(["git", "show", f":{number}:{path}"]).stdout

    ours, theirs = stage(2), stage(3)
    known = {match["name"] for match in DESIGNER_ACCESSOR.finditer(theirs)}
    added = [match.group(0) for match in DESIGNER_ACCESSOR.finditer(ours) if match["name"] not in known]
    merged = theirs

    if added:
        closing = theirs.rfind("    }\n}")

        if closing < 0:
            return None

        merged = theirs[:closing].rstrip("\n") + "\n\n" + "\n".join(added) + "\n" + theirs[closing:]

    with open(os.path.join(REPO, path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(merged)

    return len(added)


def is_resx(path):
    """A .resx under a Resources project: generated from the translation JSON, so either side will do."""
    return path.endswith(".resx") and "Resources/" in path


def command_resolve(arguments):
    files = conflicted_files()

    if not files:
        print("no conflicted files")

        return

    unknown = [path for path in files
               if not path.endswith(APPEND_ONLY_SUFFIXES) and not path.startswith(TRANSLATION_PREFIX)
               and not is_resx(path)]

    if unknown:
        refuse("conflicts this cannot judge - resolve them yourself:\n  " + "\n  ".join(unknown))

    for path in files:
        if not arguments.apply:
            kind = ("translations" if path.startswith(TRANSLATION_PREFIX)
                    else "regenerated" if is_resx(path) else "append-only")
            print(f"would resolve ({kind})  {path}")

            continue

        if is_resx(path):
            run(["git", "checkout", "--ours", "--", path])
            run(["git", "add", "--", path])
            print(f"resolved  {path}  (regenerated by gen_resx.py)")
        elif path.endswith(".Designer.cs"):
            added = resolve_designer(path)

            if added is None:
                refuse(f"{path}: no closing braces found - resolve it yourself")

            print(f"resolved  {path}  (+{added} accessor(s) from this branch)")
        elif path.startswith(TRANSLATION_PREFIX):
            added = resolve_translation_json(path)
            print(f"resolved  {path}  (+{added} key(s) from this branch)")
        else:
            count = resolve_append_only(path)

            if count is None:
                refuse(f"{path}: markers left after merging - resolve it yourself")

            print(f"resolved  {path}  ({count} block(s))")

    if arguments.apply:
        # Deliberately not staged: the resx files still have to be regenerated, and a person should
        # read the diff before this becomes a commit.
        print("\nNot staged. Check `git diff`, run `python3 Scripts/gen_resx.py` if translations were "
              "among them, then `python3 Scripts/dotnet.py verify` before committing.")
    else:
        print("\nNothing changed. Pass --apply to write.")


def add_message_arguments(parser):
    parser.add_argument("-m", "--message", help="The text itself")
    parser.add_argument("--file", help="Path to a file holding the text (relative to the repo root)")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Branch, upstream, ahead/behind and the changed files")

    diff = subparsers.add_parser("diff", help="What changed, not just which files")
    diff.add_argument("paths", nargs="*", help="Limit to these files or directories")
    diff.add_argument("--staged", action="store_true", help="What is staged, rather than what is not")
    diff.add_argument("--against", help="Compare against this ref instead of the working tree, "
                                        "e.g. master")
    diff.add_argument("--stat", action="store_true", help="Only the per-file summary")

    branch = subparsers.add_parser("branch", help=f"Fresh branch off an up-to-date {MAIN_BRANCH}")
    branch.add_argument("name")
    branch.add_argument("--from", dest="base",
                        help="Branch off this instead of master, for a pull request stacked on another")
    branch.add_argument("--carry", action="store_true",
                        help="Take the uncommitted changes along instead of refusing")
    branch.add_argument("--delete", action="store_true",
                        help=f"Delete it here and on origin, once its commits are on {MAIN_BRANCH}")
    branch.add_argument("--force", action="store_true",
                        help="Delete it even though it carries commits found on no other branch, "
                             "for a test branch that was only ever meant to be thrown away")

    branches = subparsers.add_parser(
        "branches", help=f"Every branch, here and on origin, and whether its work is on {MAIN_BRANCH}")
    branches.add_argument("--prune", action="store_true",
                          help="Delete the landed ones, here and on origin")

    switch = subparsers.add_parser(
        "switch", help=f"Move to a branch that already exists, pulling {MAIN_BRANCH}")
    switch.add_argument("name")
    switch.add_argument("--carry", action="store_true",
                        help="Take the uncommitted changes along instead of refusing")

    test_branch = subparsers.add_parser(
        "test-branch", help="One branch carrying several others, for a round of testing")
    test_branch.add_argument("name")
    test_branch.add_argument("branches", nargs="*",
                             help=f"The branches to collect, merged onto {MAIN_BRANCH} in this order")
    test_branch.add_argument("--recreate", action="store_true",
                             help="Throw an existing one away and build it again")
    test_branch.add_argument("--force", action="store_true",
                             help="Recreate even though it carries commits found on no other branch")
    test_branch.add_argument("--delete", action="store_true",
                             help="Remove it here and on the remote, for one that was pushed")
    test_branch.add_argument("--carry", action="store_true",
                             help="Build it although the tree has uncommitted changes, files nobody "
                                  "has added included; they stay where they are")

    subparsers.add_parser(
        "land", help=f"Fast-forward {MAIN_BRANCH} to the current branch, push it and delete the branch "
                     "(the waived change, no pull request)")

    sync = subparsers.add_parser(
        "sync", help=f"Merge {MAIN_BRANCH} into the current branch, for one that has fallen behind")
    sync.add_argument("--abort", action="store_true", help="Give up an open merge")

    assign = subparsers.add_parser(
        "assign", help="Give every unassigned open issue and pull request an assignee")
    assign.add_argument("--dry-run", action="store_true", help="Name them instead of changing them")

    commit = subparsers.add_parser(
        "commit", help="Commit with the message as it is written")
    add_message_arguments(commit)
    commit.add_argument("--signoff", action="store_true",
                        help="Add Signed-off-by, confirming the Developer Certificate of Origin")
    commit.add_argument("--path", nargs="+", help="Stage these paths first")
    commit.add_argument("--all", action="store_true", help="Stage every change first")
    commit.add_argument("--allow-master", action="store_true",
                        help=f"Permit committing straight to {MAIN_BRANCH}")

    log = subparsers.add_parser("log", help="Recent commits, one line each")
    log.add_argument("paths", nargs="*", help="Only commits touching these paths")
    log.add_argument("-n", "--count", type=int, default=20, help="How many (default 20)")
    log.add_argument("--range", help="A revision range as git takes it: master..HEAD")

    blame = subparsers.add_parser(
        "blame", help="Who wrote these lines, in which commit, and what that commit was about")
    blame.add_argument("path")
    blame.add_argument("--lines", help="Restrict to a range, as git takes it: 24,48")

    uncommit = subparsers.add_parser(
        "uncommit", help="Undo the last commit, keeping its changes staged (refused once pushed)")
    uncommit.add_argument("--allow-master", action="store_true")

    init = subparsers.add_parser(
        "init", help="git init plus a new GitHub repository (private unless --public); needs a .gitignore")
    init.add_argument("--name", help="Repository name; default: name in project.toml")
    init.add_argument("--description", help="The one-line description shown on GitHub")
    init.add_argument("--public", action="store_true", help="Public instead of private")

    subparsers.add_parser(
        "labels", help="Create the shared labels and the project's [[repo.labels]] (init does it; this repeats it)")

    push = subparsers.add_parser("push", help="Push the current branch, setting upstream")
    push.add_argument("--allow-master", action="store_true")

    move = subparsers.add_parser("move", help="Rename tracked files, all of them or none")
    move.add_argument("paths", nargs="+", metavar="SRC DST",
                      help="Source and destination in pairs, repository-relative")

    restore = subparsers.add_parser(
        "restore", help="Discard the uncommitted changes of tracked files (cannot be undone)")
    restore.add_argument("paths", nargs="+", help="Files or directories, repository-relative")
    restore.add_argument("--source", metavar="REV",
                         help="Take the files from this revision instead of HEAD, into the working tree only")

    subparsers.add_parser(
        "leaks", help="gitleaks over the history and the working files; exit 1 on a finding")

    untrack = subparsers.add_parser(
        "untrack", help="Stop tracking committed paths, without deleting them from disk")
    untrack.add_argument("paths", nargs="+", help="Files or directories, repository-relative")

    ignored = subparsers.add_parser(
        "ignored", help="Whether paths are ignored, and by which rule")
    ignored.add_argument("paths", nargs="+", help="Files or directories, repository-relative")

    issues = subparsers.add_parser("issues", help="List issues, one line each")
    issues.add_argument("--state", default="open", choices=["open", "closed", "all"])
    issues.add_argument("--label")
    issues.add_argument("--milestone", help="Only this milestone, by title or number; "
                                            "'none' for the issues in no milestone at all")
    issues.add_argument("--search", help="Full-text search over titles and bodies, GitHub's own "
                                        "syntax: 'accessibility OR WCAG', '\"screen reader\"'")
    issues.add_argument("--limit", type=int, default=200)

    digest = subparsers.add_parser("digest", help="What was finished in the last N days, by area")
    digest.add_argument("--days", type=int, default=7,
                        help="How far back the window reaches (default 7)")

    issue = subparsers.add_parser("issue", help="Show one or more issues, body and comments")
    issue.add_argument("number", type=int, nargs="+",
                       help="One issue number, or several to read a related group in one go")
    issue.add_argument("--no-comments", action="store_true",
                       help="Body only, for an issue with a long thread")

    issue_create = subparsers.add_parser("issue-create", help="Open an issue")
    issue_create.add_argument("--title", required=True)
    issue_create.add_argument("--label", help="Comma-separated labels; they must already exist")
    issue_create.add_argument("--milestone", help="Put it in this milestone, by title or number")
    add_message_arguments(issue_create)

    comment = subparsers.add_parser("comment", help="Comment on an issue")
    comment.add_argument("number", type=int)
    add_message_arguments(comment)

    close = subparsers.add_parser("close", help="Optionally comment, then close an issue")
    close.add_argument("number", type=int)
    close.add_argument("--reason", default="completed", choices=["completed", "not planned"])
    add_message_arguments(close)

    label = subparsers.add_parser("label", help="List the labels, or create and update one")
    label.add_argument("name", nargs="?", help="Omit to list what the repository already has")
    label.add_argument("--color")
    label.add_argument("--description")

    milestone = subparsers.add_parser("milestone", help="List milestones, or show one with its issues")
    milestone.add_argument("name", nargs="?", help="Title or number; left out, all of them are listed")
    milestone.add_argument("--state", default="all", choices=["open", "closed", "all"])

    milestone_create = subparsers.add_parser("milestone-create", help="Open a milestone")
    milestone_create.add_argument("--title", required=True)
    milestone_create.add_argument("--due", help="Due date as YYYY-MM-DD")
    milestone_create.add_argument("--description")

    milestone_edit = subparsers.add_parser("milestone-edit",
                                           help="Change a milestone's title, due date or description")
    milestone_edit.add_argument("name", help="Title or number")
    milestone_edit.add_argument("--title")
    milestone_edit.add_argument("--due", help="Due date as YYYY-MM-DD")
    milestone_edit.add_argument("--description")
    milestone_edit.add_argument("--reopen", action="store_true", help="Open a closed one again")

    milestone_close = subparsers.add_parser("milestone-close", help="Close a milestone")
    milestone_close.add_argument("name", help="Title or number")

    pull_request = subparsers.add_parser("pr", help="List open PRs, or show one")
    pull_request.add_argument("number", type=int, nargs="?")

    pr_create = subparsers.add_parser("pr-create", help="Open a PR, appending the footer")
    pr_create.add_argument("--title", required=True)
    pr_create.add_argument("--base", help=f"Base branch (default {MAIN_BRANCH} on GitHub's side)")
    pr_create.add_argument("--draft", action="store_true")
    add_message_arguments(pr_create)

    pr_retarget = subparsers.add_parser("pr-retarget",
                                        help="Point a PR at another base, reopening it if needed")
    pr_retarget.add_argument("number", type=int)
    pr_retarget.add_argument("--base", required=True)

    issue_edit = subparsers.add_parser("issue-edit",
                                       help="Replace an issue title, its body, its labels, or all of them")
    issue_edit.add_argument("number", type=int)
    issue_edit.add_argument("--title", help="New title; the body may then be left out")
    issue_edit.add_argument("--milestone", help="Move it into this milestone, by title or number; "
                                                "'none' takes it out of the one it is in")
    issue_edit.add_argument("--add-label", help="Comma-separated labels to add; they must already exist")
    issue_edit.add_argument("--remove-label", help="Comma-separated labels to take off")
    add_message_arguments(issue_edit)

    pr_edit = subparsers.add_parser("pr-edit",
                                    help="Replace a PR title, its body, its labels, or all of them")
    pr_edit.add_argument("number", type=int)
    pr_edit.add_argument("--title", help="New title; the body may then be left out")
    pr_edit.add_argument("--add-label", help="Comma-separated labels to add; they must already exist")
    pr_edit.add_argument("--remove-label", help="Comma-separated labels to take off")
    add_message_arguments(pr_edit)

    resolve = subparsers.add_parser(
        "resolve", help="Resolve the recurring append-only merge conflicts (schema.sql, translations, Designer)")
    resolve.add_argument("--apply", action="store_true", help="Write the resolution instead of listing it")

    pr_close = subparsers.add_parser("pr-close", help="Close a PR without merging it")
    pr_close.add_argument("number", type=int)
    pr_close.add_argument("--keep-branch", action="store_true")
    add_message_arguments(pr_close)

    pr_merge = subparsers.add_parser("pr-merge", help="Merge a PR once GitHub reports it mergeable")
    pr_merge.add_argument("number", type=int)
    pr_merge.add_argument("--strategy", default=MERGE_STRATEGY, choices=["merge", "squash", "rebase"],
                          help=f"Default: repo.merge_strategy of project.toml ({MERGE_STRATEGY})")
    pr_merge.add_argument("--keep-branch", action="store_true")

    ci = subparsers.add_parser("ci", help="Workflow runs of a branch, with timings and failures")
    ci.add_argument("--branch", help="Defaults to the current branch")
    ci.add_argument("--limit", type=int, default=5)
    ci.add_argument("--watch", action="store_true",
                    help="Wait for the run of the current commit to finish, then report it")
    ci.add_argument("--interval", type=int, default=20, help="Seconds between polls while watching")
    ci.add_argument("--appear-timeout", type=int, default=120,
                    help="How long to wait for a run of the current commit to show up at all")
    ci.add_argument("--timings", action="store_true",
                    help="Seconds per step of the newest run, slowest first")
    ci.add_argument("--run", help="Report this run id instead of the newest one")
    ci.add_argument("--rerun", action="store_true",
                    help="Start the failed jobs of --run again, and report nothing else")
    ci.add_argument("--log", action="store_true",
                    help="For a failed run, also print the end of the failed steps' output")
    ci.add_argument("--log-lines", type=int, default=80, help="How many lines of that output")

    arguments = parser.parse_args()

    handlers = {
        "ci": command_ci,
        "init": command_init,
    "labels": command_labels,
        "status": command_status, "diff": command_diff,
        "branch": command_branch, "branches": command_branches, "switch": command_switch, "land": command_land,
        "test-branch": command_test_branch, "sync": command_sync,
        "commit": command_commit,
        "log": command_log, "blame": command_blame, "uncommit": command_uncommit,
        "push": command_push, "move": command_move,
        "untrack": command_untrack, "restore": command_restore, "ignored": command_ignored,
        "leaks": command_leaks,
        "issues": command_issues, "issue": command_issue, "digest": command_digest,
        "assign": command_assign,
        "issue-create": command_issue_create, "issue-edit": command_issue_edit,
        "comment": command_comment, "close": command_close, "label": command_label,
        "milestone": command_milestones, "milestone-create": command_milestone_create,
        "milestone-edit": command_milestone_edit, "milestone-close": command_milestone_close,
        "pr": command_pr, "pr-create": command_pr_create, "pr-edit": command_pr_edit,
        "pr-retarget": command_pr_retarget, "pr-merge": command_pr_merge,
        "pr-close": command_pr_close,
        "resolve": command_resolve,
    }

    handlers[arguments.command](arguments)


if __name__ == "__main__":
    main()
