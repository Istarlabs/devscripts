---
name: blazor-ui
description: >
  Guide for building a Blazor Server UI on FluentUI Blazor 5 with the shared Istarnova building blocks
  (Istarnova.Blazor.FluentUI, Istarnova.Blazor, Istarnova.AspNetCore). Use when adding or editing a .razor page, dialog or
  component, a code-behind (.razor.cs), scoped CSS, a data grid, the navigation, a page frame, an error boundary, per-circuit
  state, or when a FluentUI parameter, enum or icon has to be looked up. Covers the building blocks that already exist (never
  rebuild them), the markup/code-behind separation rule, what the v5 build does not catch (removed components, unknown parameters,
  v4 design tokens) and where the FluentUI source is on this machine.
---

# Blazor UI (FluentUI 5 and Istarnova)

Blazor Server (interactive, long-lived circuits) on **FluentUI Blazor 5** (`Microsoft.FluentUI.AspNetCore.Components` plus `.Icons`).
Three applications once carried their own copy of the grids, the page frame, the navigation and the circuit state; they live in
**Istarnova** now (`~/dev/projects/dotnet/Istarnova`, referenced as source). **Look there before writing a component** - that is
the whole point of this skill, and the table below is the index.

| Read | When |
| --- | --- |
| [references/building-blocks.md](references/building-blocks.md) | Before building a grid, a page frame, a menu, an error panel, per-circuit state or a localizer - they exist. |
| [references/traps.md](references/traps.md) | Before and after any markup: what v5 lets through the build (removed components, unknown parameters, v4 tokens, changed meanings). |
| [references/dialogs.md](references/dialogs.md) | Writing or changing a dialog: `FormDialogBody`, `ViewDialogBody`, the `Form*Field` components, the options, confirmations, nested dialogs. |
| [references/layout.md](references/layout.md) | Cards, stacks, forms, design tokens, data grids. |

## The building blocks, in one line each

| You need | Use | Project |
| --- | --- | --- |
| a list of records | `LocalizedDataGrid<T>` - **never a raw `FluentDataGrid`**: its menus, empty message and paginator are English whatever the session speaks | `Istarnova.Blazor.FluentUI` |
| a list a repository pages and searches | `LocalizedSearchableDataGrid<T>` | same |
| the frame of a page (title, way back, actions) | `PageScaffold`, `PageBreadcrumb`; an administration list page: `CrudPage` | same |
| the menu | `INavigationService` (the application builds the entries) + `NavMenu` | same |
| a page that must not take the circuit down | `PageErrorBoundary` | same |
| FluentUI itself registered | `AddIstarnovaFluentUI()` (localizer for the texts FluentUI draws, class names, toasts, page memory of the grids) | same |
| state that follows the signed-in user | `AddCircuitUserState<TState, TImplementation>()`; derive from `AuthenticationCircuitHandler<TState>` for more steps | `Istarnova.Blazor` |
| state components follow | `ObservableState` / `IObservableState` | same |
| a user-facing text for an exception | `UserFacingError.Describe(exception, fallback, applicationCallback)` | same |
| a key of a keyboard event (`@onkeyup`, `@onkeydown`) | `args.IsEnter()`, `args.IsKey(KeyboardKey.Escape)`, `args.ToKeyboardKey()` - never `args.Key is "Enter"` | same |
| log messages every page shares | `ComponentLogger` (EventIds 6010 to 6022) | same |
| headers, proxy, cultures, health, telemetry | `Istarnova.AspNetCore` | `Istarnova.AspNetCore` |

A project without one of them yet is told by `dotnet.py verify` only about the raw grid; the rest is on you.

## Where the source is - read it before trusting a guide

The MCP server's migration guides were wrong on `FluentNumberInput` (it exists), `TextSize.400` (it is `Size400`) and `TextTag.P` (it is
`Paragraph`), and cost hours. The source answers a behaviour or parameter question in one read. **Search the source first; use the MCP
for discovery only.**

| What | Where |
| --- | --- |
| **FluentUI Blazor v5 source** (development checkout) | `~/dev/tools/fluentui-blazor-dev`, a clone of github.com/microsoft/fluentui-blazor, branch `dev` |
| - components, one folder per area | `src/Core/Components/{Area}/` - `Dialog`, `List` (select, autocomplete, listbox), `DataGrid`, `Button`, `Stack`, `Card`, `TextInput`, `Menu`, `Tabs`, `Nav`, `Pagination` |
| - enums | `src/Core/Enums/` |
| - runnable examples | `examples/Demo` |
| - icon sources | `src/Assets/FluentUI.Icons/Icons/{Regular,Filled,Light,Color}` |
| **The installed package** (what the build really uses) | `~/.nuget/packages/microsoft.fluentui.aspnetcore.components/<version>/lib/net10.0/` - the `.dll` and its `.xml` |
| **The Istarnova source** | `~/dev/projects/dotnet/Istarnova`, a clone of github.com/Istarlabs/Istarnova - `Istarnova.Blazor.FluentUI`, `Istarnova.Blazor`, `Istarnova.EfCore`, `Istarnova.AspNetCore` |

The checkout is the *development* branch, so when it and the installed package could differ (a parameter that is not in the build),
the package's `.xml` is the tie-break.

How to read it: `python3 Scripts/find.py "text" --text --in /absolute/path` (the hooks refuse a raw `grep`/`find`; `--in` takes any
absolute path, one term per call), and `Read` for a file. *What does this parameter do* -> the component's `.razor.cs` (the XML
summaries are the documentation); *what does it render* -> its `.razor` and `.razor.css`; *how is it meant to be used* -> the same
component under `examples/Demo`. An enum member that does not exist is a compile error, a parameter that does not exist is **not** -
see `references/traps.md`.

## Hard rule: separate markup and code-behind

- Markup **only** in `.razor`. C# code-behind **only** in `.razor.cs` (`public sealed partial class`).
- Never C# logic in `@code` blocks beyond trivial cases; never markup in `.cs`.
- Scoped styling in `.razor.css`. **No comments in `.razor` and `.css` files**: what needs saying goes in the `.razor.cs`.
- Further partials of a component keep the `.razor.` prefix: `Index.razor.Log.cs` for its generated log methods, `Index.razor.Regex.cs`
  for `[GeneratedRegex]` partials.
- **Those extra partials need a `DependentUpon` entry** in the owning `.csproj`, otherwise they sit next to the component instead of under it
  - only `{Component}.razor.cs` nests by convention:
  ```xml
  <ItemGroup>
      <Compile Update="Components\Pages\Home.razor.Log.cs">
          <DependentUpon>Home.razor</DependentUpon>
      </Compile>
  </ItemGroup>
  ```
- A string that carries values (`"{0} items, {1} total"`) is assembled in the **code-behind** as a named property, never in the markup: an expression
  chained across lines ends at the first line break and prints the rest as text, and a call with a string argument inside an attribute value closes
  the attribute at its first inner quote. Both compile.

## The rules, in one place

Each has cost somebody a debugging session.

- **Read the warnings, not only the errors.** A removed component is an RZ10012 *warning*; a removed or misspelt parameter is no diagnostic at all
  (`references/traps.md`). `dotnet.py verify` shows the warnings on the lines you changed.
- **A vertical `FluentStack` does not stretch its children.** Give the child `Class="w-100"` or set `HorizontalAlignment.Stretch` on the stack - this
  includes a grid, a data grid or one of our own components as the child: `auto-fit` columns collapse to the content's width and only the browser shows it.
- **A `FluentButton` gets its accessible name through `Title`**, never a loose `aria-label` (it lands on a custom element without a role). `Title` also
  shows the name on hover, so no `Tooltip` with the same text beside it.
- **A wrapper around a FluentUI component declares every parameter it accepts.** What it does not declare is invisible to the Razor compiler and arrives
  as a *string*, which is matched by name against the inner component's typed parameter at render time - where the cast throws and tears down the whole
  circuit, not just the page. Declare, forward by name, and let `[Parameter(CaptureUnmatchedValues = true)]` carry only what is left
  (`LocalizedDataGrid` is the pattern).
- **A form dialog is `FormDialogBody`, not a hand-written `EditForm`.** If one is written by hand anyway, wrap its `EditForm` in `@if (_editContext is { })`. v5 renders dialog content on its first `await`; an unbuilt context takes the circuit down.
- **Never a Razor comment between the attributes of a tag** - it becomes an attribute and kills the circuit.
- **A removed or renamed colour token is silently empty.** Use v5's Fluent 2 names (`--colorNeutralForeground1`).
- **Do not render two grids that swap in and out of the same place while the page loads** (`@if (loading) ... else grid`): a grid removed while its
  script loads throws `ObjectDisposedException`. Decide what to show first, then render once.
- **Never a `FluentSelect` over a list that can grow.** A picker that searches in the database is the answer; a raw `FluentAutocomplete` is a rewrite in
  v5 and `Multiple` defaults to `true`.

## Localization in the UI

- All user-facing text comes from resource files, never a literal (see the **localization** skill).
- **The texts FluentUI draws itself** (grid menus, the paginator, message-box buttons) come from `IFluentLocalizer`. `AddIstarnovaFluentUI()` registers
  the library's: English, German and Japanese. An application with more languages passes its own `ResourceManager` (FluentUI's keys, e.g.
  `Paginator_GoFirstPage`), which is asked first.
- **Neutral cultures.** Resources of the libraries are `de` and `ja`, not `ja-JP`: the application asks for `ja`, and a satellite named `ja-JP` is not found
  for it, so the session silently falls back to English.
- For enum dropdowns and labels use the project's enum-localization helpers (see the project's appendix below, if it has one).

## Checklist for a new page, tab or dialog

0. Is it in `references/building-blocks.md`? Is any component, parameter, enum or icon new to this code base -> look it up in the v5 source first.
1. `.razor` markup + `.razor.cs` code-behind (sealed partial) + optional `.razor.css`; no comments in the markup or the CSS.
2. Inject the repository interface and a logger; never a `DbContext` in a component.
3. Every label from resources, in every language the project ships.
4. Place under the right `Pages/{Feature}/` folder; register a launch point (button, menu entry, tile) in the parent.
5. A dialog: `FormDialogBody` (edits a model) or `ViewDialogBody` (shows something) with the `Form*Field` components; only one with a footer of its own is written by
   hand (`EditForm` behind the guard, buttons directly in `ActionTemplate`). A flow that wants a second dialog goes into the page instead.
6. Any list -> `LocalizedDataGrid` (no `FluentPaginator` under it).
7. A vertical `FluentStack` around a grid or a self-laying-out component -> `HorizontalAlignment.Stretch`.
8. Colours, borders and shadows from Fluent 2 tokens.
9. Build, then read the **warnings on your lines** - RZ10012 is a removed component.
