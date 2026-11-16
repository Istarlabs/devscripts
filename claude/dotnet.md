# Shared guidelines: .NET

The C# style itself (naming, braces, file headers and regions, one class per file, nullable reference
types, XML documentation) is in the user's global instructions and is not repeated here. Match the
surrounding files of the project first.

## Standards

- `python3 Scripts/dotnet.py verify` is the gate: build, tests, and the warnings on the lines this
  branch changed. It runs before every commit; `python3 Scripts/dotnet.py packages --vulnerable` after
  touching a package reference.
- **Never rebuild that from `dotnet … | grep …`.** The SDK output on this machine is German, so a
  hand-written `grep` for `error` or `warning` silently misses things, and a package advisory has no
  file position at all.
- **Logging goes through `[LoggerMessage]`**, in a partial `{ClassName}.Log.cs`; `no-direct-logger.sh`
  refuses a direct `ILogger` call. The `logging` skill has the pattern.
- Data access is asynchronous and goes through a repository, never a `DbContext` in a component or
  page; filter on `IQueryable` before materializing, and project to a DTO rather than load an entity.
- Text the user reads comes from resources, English as default and German beside it.

## Scripts

| Task | Command |
| --- | --- |
| Build, tests, warnings on changed lines | `python3 Scripts/dotnet.py verify` |
| Warnings by code, package advisories, SDK, project list | `python3 Scripts/dotnet.py --help` |
| A single-file C# script | `python3 Scripts/dotnet.py script --list` |
| Where a symbol is used, also in NuGet packages | `python3 Scripts/find.py Symbol [--nuget]` |
| What the running application logged | `python3 Scripts/logs.py [--grep X] [--level Warn]` |
