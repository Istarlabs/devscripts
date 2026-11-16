# What FluentUI 5 lets through the build

Migrating from v4 cost most of its time on things that **compile clean and are wrong in the browser**. A green build proves nothing about these.

## 1. A component v5 removed renders as plain markup

An unknown tag in a `.razor` file is not an error: Razor emits **RZ10012 as a warning** and writes the element out as HTML. In 5.0 these are gone:
`FluentNavMenu`, `FluentNavLink`, `FluentToolbar`, `FluentBreadcrumb`, `FluentSplitter`, `FluentDialogHeader`, `FluentDialogFooter`, `FluentDesignTheme`,
`FluentAnchor` (now `FluentLink`), `FluentSearch`, `FluentTextField` and `FluentNumberField` (checked against the installed assembly; the navigation is
`FluentNav`). The page builds and the control is simply not there.

- **Read the warnings, not only the errors:** `python3 Scripts/dotnet.py verify` lists warnings on the lines you changed, and
  `python3 Scripts/dotnet.py warnings --code RZ10012` lists every one.

## 2. A removed or misspelt parameter lands in `AdditionalAttributes`

A Razor attribute the component does not declare is handed to it as a loose HTML attribute. It builds clean; then it does nothing, or - when it differs from
a real parameter only in case - is matched at render time and fails the cast. `@bind-Open` on a removed `Open` still compiles. v5 removed `FluentMenu.Anchor`,
`Open` / `OpenChanged` and `Width`, among others.

- Look the parameter up before you use one that is new to the code base: the component's `.razor.cs` in the v5 source lists every `[Parameter]`, and
  `find.py "ParameterName" --text --in <source>/src/Core/Components/<Area>` answers "does it exist".
- Lower-case `class=` / `style=` on a FluentUI component are loose attributes; use `Class` / `Style`.
- A project that keeps a parameter audit (`Scripts/fluentui_v5.py audit`, `dotnet.py script fluentui_parameters`) says so in its appendix.

## 3. An unknown CSS variable resolves to nothing

See `layout.md`: v4 tokens silently drop colours, borders and shadows.

## 4. The same signature, a different meaning

- `IDialogService.ShowConfirmationAsync` swapped its argument order (see `dialogs.md`). Four strings either way.
- Enum members renamed under the same enum name: `Appearance.Accent` is `ButtonAppearance.Primary`, `Neutral` is `Default`, `Lightweight` is `Transparent`,
  `Stealth` is `Subtle`.
- `FluentDatePicker` / `FluentTimePicker` are generic now (`TValue="DateTime?"`), as are `FluentNumberInput` (`TValue`), `FluentSelect` and `FluentAutocomplete`
  (`TOption`, `TValue`). A fractional `Step` / `Min` / `Max` on a number input whose type is inferred only compiles when the literal has the bound type (`0.5m`).
- `FluentProgressBar.Value`, `Min` and `Max` are `int`.

## 5. Behaviour that is not in the parameter list

- **Menus:** a `FluentMenu` needs a `FluentMenuList` around the items and a trigger by id (`Trigger="@anchorId"`), otherwise the items render unslotted in the
  normal flow. Menu items reserve a 16 px start slot, so use **Size16** icons and put an icon in as ordinary content in front of the text.
- **`FluentButton`:** `Label` is drawn as text even when `IconOnly`; `Title` is the accessible name and the tooltip. There is no `Href`: a button that
  navigates calls `NavigationManager.NavigateTo`. `FluentAnchor` is `FluentLink`.
- **Text:** write text with `FluentText` - `As="TextTag.H4"` / `TextTag.Paragraph` (not `P`), sizes `TextSize.Size400` (not `400`), weights `TextWeight.Semibold`.
- **Inputs:** `FluentTextInput` / `FluentNumberInput` replace the v4 text and number fields; a search box is `TextInputType="TextInputType.Search"` with a
  `StartTemplate` holding the icon; a field that holds a secret (a pasted key) is `TextInputType.Password`. A waiting indicator is `FluentSpinner`.
- **Tabs:** `FluentTab` has `DeferredLoading`, which renders the content when the tab is first selected. In a prerendered page the panel shows a progress bar
  until the circuit is up; do not test tab content with a plain HTTP request.
- **The library's own texts are English only** unless an `IFluentLocalizer` supplies them: grid menus, the paginator and message-box buttons.
  `AddIstarnovaFluentUI()` registers one; a missing key reads English, not empty.
- **Providers:** `FluentProviders` sits once in `MainLayout.razor`; the v4 `FluentDesignTheme` is gone (theme is CSS variables and `color-scheme`).
- A dialog's content renders on its first `await`: see `dialogs.md`, the `EditForm` guard.
- A `FluentPaginator` renders nothing until its `PaginationState` knows the total (`SetTotalItemCountAsync`); a grid sets it after loading.

## Where to look when something here is in doubt

Read the source before trusting a guide. The MCP server's migration guides were wrong on `FluentNumberInput` (it exists), `TextSize.400` and `TextTag.P`.
The v5 source is local, see `SKILL.md` ("Where the source is").
