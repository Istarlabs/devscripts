# Layout: cards, stacks, forms, grids, tokens (FluentUI 5)

Checked against `~/dev/tools/fluentui-blazor-dev/src/Core/Components` (`Card`, `Stack`, `DataGrid`).

## Cards

`FluentCard` in v5 is a plain container: padding, border, radius, `box-shadow: var(--shadow4)`, `width: 100%`, `height: fit-content`. Its stylesheet sets **no `contain`**:
the v4 card set `contain: content`, which clipped a select's list, a date picker or a tooltip at the card's edge. Whether a popup inside a v5 card always paints in
full has not been confirmed in the browser; if one is ever cut off, look at the card first.

- Parameters: `Appearance` (`CardAppearance`), `Shadow` (`CardShadow`), `Width`, `Height`, `OnClick` (a selectable card, `role="group"` by default).
- `MinimalStyle` does not exist. A card in a loop, or a card in a card, needs no workaround.

## A vertical `FluentStack` does not stretch its children

`HorizontalAlignment` defaults to `Left`, which becomes `align-items: start`, so in a vertical stack every child is as wide as its own content. v5's stack is
`Width="100%"` by default, so the stack itself is wide; its children are not.

Either give the child a width (`Class="w-100"` where a utility stylesheet is loaded) or set `HorizontalAlignment="HorizontalAlignment.Stretch"` - and check the
whole stack when you do, because stretch also widens every button in it.

**It bites hardest where the child is a CSS grid, and it is easy to miss when the child is a component of our own.** A component's root `div` is a stack child like
any other, so a `display: grid; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr))` inside a vertical stack shrinks to its content: the cards sit in two
narrow columns on the left of a full-width page. Nothing warns, the build is clean, and it is visible only in the browser. Whenever a vertical `FluentStack` holds a
grid, a `LocalizedDataGrid`, a tile list or a component that lays out its own children, set `HorizontalAlignment.Stretch` on the stack.

A plain `<div>` with `display: flex; flex-direction: column` stretches by default and needs none of this; prefer it for a page whose only job is to hold a grid.
`PageScaffold` is such a column.

v5 spells gaps `HorizontalGap` / `VerticalGap`; app-wide defaults go through the `configure` of `AddIstarnovaFluentUI` (`DefaultValues.For<FluentStack>()`).

## Form layout - the label/input wrapping trap

Labelled inputs render the label and the control as two siblings. Never put them straight into a wrapping horizontal stack: label and control wrap apart. Put
each field in one cell of a CSS grid (`grid-template-columns: repeat(auto-fit, minmax(240px, 1fr))`), full-width rows (selects, checkboxes, text areas, message
bars) spanning the row. The project keeps that grid as a CSS class in its `wwwroot/app.css`; do not reintroduce per-field `min-width` / `width` styles.

## Design tokens: v5 names, and an unknown one resolves to nothing

v5 uses the Fluent 2 token names (`--colorNeutralForeground1`, `--colorBrandBackground`, `--spacingHorizontalM`, `--borderRadiusMedium`, `--strokeWidthThin`,
`--shadow4`, `--fontSizeBase300`). **The v4 names (`--neutral-foreground-rest`, `--accent-fill-rest`, `--control-corner-radius`, ...) are not defined, and a `var()` of
an undefined custom property is silently empty**: the colour, border or shadow just does not appear, and nothing warns.

- v5 tokens already carry their unit: `--strokeWidthThin` is `1px`, so `calc(var(--strokeWidthThin) * 1px)` is `1px * 1px` and breaks. Write `var(--strokeWidthThin)`.
- A palette colour is `--colorPalette<Name>Foreground1/2` and `...Background1/2`; check a name against the theme CSS in the package before relying on it.

## Data grids

**`LocalizedDataGrid`, never a raw `FluentDataGrid`.** The wrapper brings the translated sort, resize and column labels (through the registered `IFluentLocalizer`),
its own paginator and localized empty/loading content; the raw grid's menus, empty message and paginator are English. Delete a `<FluentPaginator>` under it.

```razor
<LocalizedDataGrid Items="@Items" Pagination="@Pagination" TGridItem="Country" ResizableColumns="true">
    <PropertyColumn Property="@(c => c.Name)" Title="@Resources.Name" Sortable="true"/>
</LocalizedDataGrid>
```

- `ItemsPerPage` defaults to 10 and the grid pages; a grid without a pager needs an `ItemsPerPage` that covers the list (or `ShowPaginator="false"`).
- A grid that a repository pages and searches is `LocalizedSearchableDataGrid`. Render it behind a condition that says what it needs (a selected account): the
  provider runs once on mount.
- `GridTemplateColumns` (a CSS grid template) and other `FluentDataGrid` parameters the wrapper does not declare are splatted through; a string parameter is fine,
  a typed one (`bool`, `int`) the wrapper declares itself or it fails the cast at render time (see `SKILL.md`, wrappers).
- Do not render two grids that swap in and out of the same place while the page loads.
- A record that opens its own page is better as a tile than as a row with small icon buttons (a small icon button is a poor target for anyone with low vision).
  Where a grid stays (many rows, sorting, export), its action column holds `IconOnly` buttons with a `Title`.
