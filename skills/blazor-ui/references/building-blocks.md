# The building blocks of Istarnova, and how they are used

Source: `~/dev/projects/dotnet/Istarnova`. Applications reference it as source (`ProjectReference`), so a change there is
in the application at once; read the code when a detail matters. Namespaces: `Istarnova.Blazor.FluentUI.*`, `Istarnova.Blazor.*`,
`Istarnova.EfCore.*`, `Istarnova.AspNetCore.*`.

## Registering

```csharp
builder.AddServiceDefaults();                                   // Istarnova.AspNetCore.ServiceDefaults
builder.Services.AddIstarnovaFluentUI();                        // localizer, class names, toasts, GridPageMemory
builder.Services.AddScoped<INavigationService, NavigationService>();   // the application's own menu
builder.Services.AddTrustedForwardedHeaders(builder.Configuration);    // behind a reverse proxy
// ...
app.UseForwardedHeaders();                                      // first: before anything that looks at scheme or address
app.UseSecurityHeaders(SecurityHeaders.WebApplicationPolicy);
app.UseSupportedCultures("en", "en", "de", "ja");               // default, then the supported ones
app.MapDefaultEndpoints();                                      // /health and /alive
```

`AddIstarnovaFluentUI(configure, localizer)`: `configure` is applied last (default stack gaps and the like), `localizer` replaces the library's.

## Grids

```razor
<LocalizedDataGrid TGridItem="Country" Items="@_countries" ItemsPerPage="20" ResizableColumns="true">
    <PropertyColumn Property="@(country => country.Name)" Title="@Resources.Name" Sortable="true"/>
</LocalizedDataGrid>
```

- Own parameters: `Items`, `ItemsProvider`, `Pagination`, `ItemsPerPage` (default 10), `ResizableColumns`, `MultiLine` (default true), `Loading`,
  `EmptyContent`, `LoadingContent`, `AutoItemsPerPage`, `GenerateHeader`, `ShowPaginator`. Everything else is splatted to the grid.
- It carries its own `LocalizedPaginator`: **delete a `<FluentPaginator>` under it.**
- `PaginationState.SetCurrentPageIndexAsync(n)` makes the grid fetch again, also when the index does not change: that is how a filter refreshes a grid.
- The grid remembers the page it was left on per address and item type (`GridPageMemory`, scoped, registered by `AddIstarnovaFluentUI`), so coming back
  from a detail page lands where the list was left.
- `LocalizedSearchableDataGrid<T>`: `Repository` is an `IQueryableRepository<T>`; a search box appears when it is also an
  `ISearchQueryableRepository<T>`. `Narrow` adds the caller's filter and includes, `DefaultOrder` orders when the grid has no sort of its own,
  `EmptyMessage` / `NoMatchMessage` / `ErrorMessage` are the application's texts. It implements `IRefreshableList` (`RefreshAsync`).

## The frame of a page

```razor
<PageScaffold Title="@Resources.Countries" TitleIcon="@(new RegularIcons.Size24.Globe())"
              Breadcrumbs="@_breadcrumbs">
    <Actions><FluentButton ...>@Resources.New</FluentButton></Actions>
    <TitleSuffix><HelpHint .../></TitleSuffix>     @* whatever belongs next to the title *@
    ...
</PageScaffold>
```

A breadcrumb trail (`PageBreadcrumb(text, url)`, the last one without a url) replaces the back link. `CrudPage` is the same with a default "create" button
(`OnCreate`, `CreateText`).

## The menu

```csharp
public sealed class NavigationService : INavigationService
{
    public IEnumerable<NavigationMenuObject> BuildMainMenu() =>
    [
        new() { Title = Resources.Home, Url = "/", Icon = new RegularIcons.Size20.Home(), Match = NavLinkMatch.All },
        new() { Title = Resources.Admin, Url = "/admin", Policy = PolicyNames.Administrators, SubItems = [ ... ] },
    ];
}
```

`<NavMenu/>` in the navigation area of the layout. `Policy` and `Roles` hide an entry the user may not use. A group with a `Url` stays open on every page under
it. `ReevaluateOn="@someObservableState"` makes the menu ask its policies again the moment that state changes. `Items` overrides the service.

## Errors

`<PageErrorBoundary>@Body</PageErrorBoundary>` around the content: an exception shows a panel with a short reference (eight hex characters, logged) and
"try again" / "go to the start page"; the panel clears when the person navigates. `Actions` adds buttons next to those and receives the reference.

`UserFacingError.Describe(exception, fallbackText, applicationCallback)`: a concurrency conflict becomes the library's localized text; the application's own
exceptions go through the callback (return `null` for the ones it does not know).

## State of a circuit

```csharp
builder.Services.AddCircuitUserState<IAppState, AppState>();   // AppState : ObservableState, IAppState; IAppState : ICircuitUserState
```

The handler calls `InitializeAsync(principal)` when the connection comes up and whenever the authentication changes. An application that has more to do at
those moments derives from `AuthenticationCircuitHandler<TState>` and overrides `OnAuthenticationAsync` (call the base first), then registers its own handler
instead of using `AddCircuitUserState` for the handler part. Components follow a state with `Subscribe(this, () => InvokeAsync(StateHasChanged))` and
`Unsubscribe(this)` on dispose.

## Dialogs and toasts

`IDialogService.ShowDialogAsync<TDialog>(content, options)` (`DialogServiceExtensions`) hands the model over as the dialog's `Content` parameter and turns
the height `"auto"` into `fit-content`. `INotificationService.ShowSuccess/Info/Warning/Error(message)` show a toast without waiting for it to close.
`FluentDialogResult<T>` carries how a CRUD dialog ended (`OkCreate`, `OkUpdate`, `CancelDelete`, ...) and the record. See `dialogs.md`.

## Texts and cultures

Library resources are `Istarnova.Resources` (`internal`, readable by the Istarnova projects): `ComponentResources` (grid, scaffold and boundary texts),
`FluentLibraryResources` (the 106 keys FluentUI asks its localizer for), `BlazorResources`. English, German and Japanese, as **neutral** cultures.
To add a language, pass the application's `ResourceManager` to `IstarnovaFluentLocalizer` (and to `AddIstarnovaFluentUI`).

## Logging

`ComponentLogger` (`Istarnova.Blazor.Logging`): `LogComponentLoadFailed`, `LogComponentSaveFailed`, `LogComponentActionFailed`, `LogComponentLoaded`,
`LogComponentFailure`, ... EventIds 6010 to 6049 are the libraries'; an application's own start at 6100. See the **logging** skill.

## What is not here

Pickers, document views, record tiles, dialogs of one domain, the import page: each application has its own, because they speak its domain. The extras
of the project (below) name them.
