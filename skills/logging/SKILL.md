---
name: logging
description: >
  Logging rules for a .NET project: source-generated log methods only (LoggerMessage), the shared loggers of Istarnova
  (RepositoryLogger, ComponentLogger) versus {ClassName}.Log.cs partials, EventId ranges, log levels, and what must never appear in a
  message (personal data, keys, addresses with secrets - ids only). Use before writing or changing any logging call, a *.Log.cs file,
  or an exception message.
---

# Logging

Always a generated `[LoggerMessage]` method: compile-time code, no allocation when the level is off. Never string interpolation into
`_logger.Log...($"...")`.

## Which logger

The deciding question is how many classes say it. A message repeated across classes is not that class's message.

- **Recurring across classes -> a shared logger.** `RepositoryLogger` (`Istarnova.EfCore.Logging`, `_logger.LogRepositoryCreated(nameof(Order), order.OrderId);`)
  for repositories, `ComponentLogger` (`Istarnova.Blazor.Logging`) for pages, tabs and dialogs. Add to those (in Istarnova) rather than writing a private copy.
- **Specific to one class -> `{ClassName}.Log.cs`**, a `partial` class with `private static partial` methods. Blazor: `Index.razor.Log.cs`, with a `DependentUpon` entry in the
  `.csproj` (see **blazor-ui**).
- An instance partial method (`private partial void LogX(...)`) needs a field of type `ILogger` in the class; with an `[Inject]` property use the static form and pass
  the logger: `private static partial void LogX(ILogger logger, ...)`.

**Nothing calls `ILogger` directly.** The hook `no-direct-logger.sh` refuses an edit that adds `_logger.LogError("...")` or any other level; `dotnet.py verify` counts them
across the solution against `dotnet.logging_baseline` (it catches files that arrive without going through the edit tool).

## A class-specific partial

```csharp
public partial class MyService
{
    [LoggerMessage(EventId = 3001, Level = LogLevel.Information, Message = "Processing the schedule of ScheduleId = {scheduleId}")]
    private static partial void LogProcessingSchedule(ILogger logger, int scheduleId);

    [LoggerMessage(EventId = 3003, Level = LogLevel.Error, Message = "Failed to generate the report for ReportId = {reportId}")]
    private static partial void LogReportFailed(ILogger logger, Exception exception, int reportId);
}
```

Called as `LogReportFailed(_logger, ex, reportId);`.

## Conventions

- **EventId ranges** (unique per area): 2000 to 2079 `RepositoryLogger` (2080 to 2999 an application's own repository messages), 3000 to 3999 business services, 4000 to 4999 import services,
  5000 to 5999 report generation, 6000 to 6099 the Istarnova UI loggers (`ComponentLogger` 6010 to 6022, the error boundary 6030, the circuit handler 6040 and 6041), **6100 and up an
  application's own UI messages**. A project that deviates says so in its appendix.
- **Names**: `Log{Operation}{Entity}`, e.g. `LogCreatedOrder`.
- **Levels**: `Debug` diagnostics (counts, query parameters), `Information` successful operations, `Warning` not found / validation failures / recoverable, `Error` exceptions and
  failed operations, `Critical` system failure or data corruption.
- Structured data as parameters, never concatenated. An error log always takes the `Exception` parameter.

## What must never appear in a log message

A log can be written to a table or a file that is **handed to someone else** - exported, attached to a bug report. It is only safe to hand over if it is safe by construction;
scrubbing free text afterwards is guesswork.

Never put in a message: names, dates of birth, identifiers tied to a person, document or file names (people name files after what is in them), search terms, **secrets of any kind - a
key, a token, a link that carries one (an `authkey` in a query string), a password**, or the full address of an outgoing request when its query or path can be the secret.

**Ids are what belong there instead.** `OrderId = 7` means nothing outside the instance and is what makes an entry worth following up. The same applies to exceptions the code
throws itself: their messages end up in logs.
