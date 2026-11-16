#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""The knowledge graph of a repository (graphify), for questions about how the parts hang together.

    python3 Scripts/graph.py status                 does a graph exist, how old, how big
    python3 Scripts/graph.py build                  (re)build it: code only, structural, no LLM, no tokens
    python3 Scripts/graph.py query "question"       breadth-first walk from the nodes the question names
    python3 Scripts/graph.py hubs [N]               the N best connected nodes: where the repository is held together
    python3 Scripts/graph.py uses Istarnova [N]     which types of another repository this project refers to, most first
    python3 Scripts/graph.py around Name            every node called Name, merged: edges by relation and by project
    python3 Scripts/graph.py path "A" "B"           shortest path between two nodes
    python3 Scripts/graph.py explain "Name"         the tool's own description of one node

    --in PATH                                       another repository (a name is looked up next to this one:
                                                    --in Istarnova), with its own graph, kept in that repository
                                                    if it git-ignores graphify-out/

The graph answers "who depends on this" and "what does this touch" across thousands of files without reading them. It does
not answer how something behaves: for that the code is still the source. graphify reads code with tree-sitter, so a
language feature newer than its grammar is read partly, and a file type it has no grammar for contributes nothing (the
build names both).

A partial class is one node per file plus one node for the references to it. `around` merges all nodes of that name,
which `explain` cannot.

A graph is kept in the graphify-out/ folder of its repository: always for the project the script runs in, which should
git-ignore it, and for another repository (--in) when that one already git-ignores graphify-out/. Otherwise it is kept
under ~/.local/state/graphify/<name>/, so that building it leaves that checkout clean: the scripts there refuse a dirty
tree. So there is one graph per repository, wherever it is asked from.

graphify lives in its own environment (uv tool: `uv tool install graphifyy`); this script finds it from the shebang of the
graphify command and runs itself there for the steps that need its library.
"""

import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT = Path(os.environ.get("DEVSCRIPTS_PROJECT") or Path.cwd()).resolve()
COMMAND = Path.home() / ".local" / "bin" / "graphify"
STATE = Path.home() / ".local" / "state" / "graphify"


class Target:
    """The repository a command is about, and where its graph is kept."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root if root == PROJECT or ignores_graph(root) else STATE / root.name
        self.out = self.state / "graphify-out"
        self.graph = self.out / "graph.json"


def ignores_graph(root: Path) -> bool:
    """Whether the repository at `root` already git-ignores graphify-out/, so that a graph can live in it without
    making the checkout dirty."""
    return subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "graphify-out/graph.json"],
                          check=False).returncode == 0


def graphify_python() -> str:
    """The interpreter graphify is installed in, read from the first line of its command."""
    if not COMMAND.exists():
        sys.exit(f"refused: graphify is not installed ({COMMAND}). Install it with: uv tool install graphifyy")

    return COMMAND.read_text(encoding="utf-8").splitlines()[0].removeprefix("#!").strip()


def run_graphify(target: Target, *arguments: str) -> int:
    target.state.mkdir(parents=True, exist_ok=True)
    return subprocess.run([str(COMMAND), *arguments], cwd=target.state, check=False,
                          env={**os.environ, "GRAPHIFY_OUT": str(target.out)}).returncode


def need_library(target: Target) -> None:
    """Runs the rest of the script inside graphify's environment when its library is not importable here.

    GRAPHIFY_OUT is set first, for this process and the one that replaces it: graphify writes its cache under the
    repository it reads unless it is told where its output is, and for another repository that would dirty its checkout.
    """
    os.environ["GRAPHIFY_OUT"] = str(target.out)

    try:
        import graphify  # noqa: F401
    except ImportError:
        os.execv(graphify_python(), [graphify_python(), str(Path(__file__).resolve()), *sys.argv[1:]])


def load_graph(target: Target) -> dict:
    if not target.graph.exists():
        suffix = "" if target.root == PROJECT else f" --in {target.root.name}"
        sys.exit(f"refused: no graph for {target.root.name} yet - run 'python3 Scripts/graph.py build{suffix}'")

    return json.loads(target.graph.read_text(encoding="utf-8"))


def status(target: Target) -> int:
    if not target.graph.exists():
        print(f"no graph for {target.root.name}")
        return 1

    graph = load_graph(target)
    age_hours = (time.time() - target.graph.stat().st_mtime) / 3600
    edges = graph.get("links") or graph.get("edges") or []

    print(f"{target.root.name}: {len(graph['nodes'])} nodes, {len(edges)} edges, built {age_hours:.1f} h ago")
    print("rebuild after larger changes (unchanged files come from the cache): python3 Scripts/graph.py build")
    return 0


def build(target: Target) -> int:
    need_library(target)

    from graphify.analyze import god_nodes, suggest_questions, surprising_connections
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.detect import detect
    from graphify.export import to_json
    from graphify.extract import collect_files, extract
    from graphify.paths import out_path
    from graphify.report import generate
    from graphify.watch import _read_build_excludes, _read_build_gitignore

    target.out.mkdir(parents=True, exist_ok=True)
    os.chdir(target.state)

    detection = detect(target.root, extra_excludes=_read_build_excludes(out_path()),
                       gitignore=_read_build_gitignore(out_path()))
    code_files = []

    for entry in detection.get("files", {}).get("code", []):
        path = Path(entry)
        code_files.extend(collect_files(path) if path.is_dir() else [path])

    print(f"{target.root.name}: {len(code_files)} code file(s)")

    # cache_root says where the cache lives and root what the node ids are relative to; they differ for another
    # repository, whose checkout must stay clean.
    extraction = extract(code_files, cache_root=target.state, root=target.root)
    extraction.setdefault("hyperedges", [])
    graph = build_from_json(extraction, root=str(target.root), directed=False)

    if graph.number_of_nodes() == 0:
        print("refused: the extraction produced no nodes")
        return 1

    communities = cluster(graph)
    cohesion = score_all(graph, communities)

    # A community is named after its best connected member; naming hundreds of them by hand is not worth it, and the
    # name of a hub is what a reader looks for.
    labels = {}

    for community, members in communities.items():
        hub = max(members, key=lambda node: graph.degree(node))
        labels[community] = graph.nodes[hub].get("label", str(hub))

    gods = god_nodes(graph)
    surprises = surprising_connections(graph, communities)
    questions = suggest_questions(graph, communities, labels)

    if not to_json(graph, communities, str(target.graph), community_labels=labels, force=True):
        print("refused: graph.json was not written")
        return 1

    report = generate(graph, communities, cohesion, labels, gods, surprises, detection,
                      {"input": 0, "output": 0}, str(target.root), suggested_questions=questions)
    (target.out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")

    print(f"{graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges, {len(communities)} communities")
    run_graphify(target, "export", "html")
    return 0


def around(target: Target, name: str, limit: int) -> int:
    graph = load_graph(target)
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph.get("links") or graph.get("edges") or []
    wanted = name.lower()
    own = {node_id for node_id, node in nodes.items()
           if node.get("label", "").lower() == wanted or node_id.lower() == wanted}

    if not own:
        print(f"no node called {name!r}")
        return 1

    files = sorted({nodes[node_id].get("source_file") for node_id in own if nodes[node_id].get("source_file")})
    relations = Counter()
    projects = Counter()
    neighbours = Counter()

    for edge in edges:
        source, destination = edge["source"], edge["target"]

        if (source in own) == (destination in own):
            continue

        other = destination if source in own else source
        source_file = nodes.get(other, {}).get("source_file") or ""

        relations[edge.get("relation", "?")] += 1
        projects[source_file.split("/", 1)[0] if source_file else "(none)"] += 1
        neighbours[nodes.get(other, {}).get("label", other)] += 1

    total = sum(relations.values())
    print(f"{name}: {len(own)} node(s) in {len(files)} file(s), {total} edge(s) to other nodes")

    for file in files[:12]:
        print(f"  file: {file}")

    print("by relation:", dict(relations.most_common()))
    print("by project: ", dict(projects.most_common(12)))
    print(f"most frequent neighbours (top {limit}):")

    for label, count in neighbours.most_common(limit):
        print(f"  {count:4d}  {label}")

    return 0


def hubs(target: Target, limit: int) -> int:
    """The best connected nodes, one line each: where a repository is held together."""
    graph = load_graph(target)
    nodes = {node["id"]: node for node in graph["nodes"]}
    degree = Counter()

    for edge in graph.get("links") or graph.get("edges") or []:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    for node_id, count in degree.most_common(limit):
        node = nodes.get(node_id, {})
        print(f"{count:5d}  {node.get('label', node_id)}  ({node.get('source_file') or '-'})")

    return 0


def uses(target: Target, other: Target, limit: int) -> int:
    """Which types of `other` this project's code refers to, most referred first.

    A type of another repository is a node in this graph that has no file here; it counts when `other` defines a type
    of that name. That is by name, so an equally named type of a library is counted too: read the list, not just the
    numbers. Extension methods do not show at all: a call names the method, never the class that holds it.
    """
    mine = load_graph(target)
    theirs = load_graph(other)
    defined = {node["label"] for node in theirs["nodes"]
               if node.get("source_file") and node.get("file_type") == "code" and "(" not in node["label"]
               and "." not in node["label"]}
    nodes = {node["id"]: node for node in mine["nodes"]}
    references = Counter()
    projects = {}

    for edge in mine.get("links") or mine.get("edges") or []:
        destination = nodes.get(edge["target"], {})

        if destination.get("label") not in defined or destination.get("source_file"):
            continue

        label = destination["label"]
        references[label] += 1
        source_file = nodes.get(edge["source"], {}).get("source_file") or ""
        projects.setdefault(label, Counter())[source_file.split("/", 1)[0] if source_file else "(none)"] += 1

    print(f"{target.root.name} refers to {len(references)} type(s) of {other.root.name}, "
          f"{sum(references.values())} references")

    for label, count in references.most_common(limit):
        main = ", ".join(f"{name} {n}" for name, n in projects[label].most_common(2))
        print(f"{count:5d}  {label}  ({main})")

    return 0


def resolve_target(arguments: list[str]) -> Target:
    """Takes --in PATH out of the arguments and returns the repository it names (the project by default)."""
    if "--in" not in arguments:
        return Target(PROJECT)

    index = arguments.index("--in")

    if index + 1 >= len(arguments):
        sys.exit("refused: --in needs a path or the name of a repository next to this one")

    value = arguments[index + 1]
    del arguments[index:index + 2]

    candidate = Path(value).expanduser()

    if not candidate.is_dir():
        candidate = PROJECT.parent / value

    if not candidate.is_dir():
        sys.exit(f"refused: no repository at {value!r} (also tried next to this one)")

    return Target(candidate.resolve())


def main() -> int:
    arguments = sys.argv[1:]
    target = resolve_target(arguments)
    command = arguments[0] if arguments else ""

    if command == "status":
        return status(target)

    if command == "build":
        return build(target)

    if command == "query" and len(arguments) >= 2:
        return run_graphify(target, "query", arguments[1], "--budget", arguments[2] if len(arguments) > 2 else "2500")

    if command == "around" and len(arguments) >= 2:
        return around(target, arguments[1], int(arguments[2]) if len(arguments) > 2 else 15)

    if command == "uses" and len(arguments) >= 2:
        sibling = PROJECT.parent / arguments[1]
        other = Target((sibling if sibling.is_dir() else Path(arguments[1]).expanduser()).resolve())
        return uses(target, other, int(arguments[2]) if len(arguments) > 2 else 25)

    if command == "hubs":
        return hubs(target, int(arguments[1]) if len(arguments) > 1 else 20)

    if command == "path" and len(arguments) == 3:
        return run_graphify(target, "path", arguments[1], arguments[2])

    if command == "explain" and len(arguments) == 2:
        return run_graphify(target, "explain", arguments[1])

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
