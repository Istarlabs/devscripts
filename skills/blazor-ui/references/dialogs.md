# Dialogs (FluentUI 5)

How a dialog is built and opened, what the move from v4 changed, and which v4 workarounds are gone. Checked against the v5 source in
`~/dev/tools/fluentui-blazor-dev/src/Core/Components/Dialog`.

## The pattern

A dialog is an ordinary component. It is handed its model as a parameter, and the dialog instance as a cascading value:

```csharp
public sealed partial class CountryDialog
{
    [Parameter] public Country Content { get; set; } = new();

    [CascadingParameter] public IDialogInstance Dialog { get; set; } = null!;

    private EditContext? _editContext;
    private bool _isSaving;

    protected override void OnInitialized() => _editContext = new(Content);

    private Task CancelAsync()
        => Dialog.CloseAsync(DialogResult.Cancel(FluentDialogResult<Country>.Cancel));

    private async Task SaveAsync()
    {
        // validate, copy locals into Content, then
        await Dialog.CloseAsync(IsEditMode
            ? FluentDialogResult<Country>.OkUpdate(Content)
            : FluentDialogResult<Country>.OkCreate(Content));
    }
}
```

- `Dialog.CloseAsync(value)` closes with `DialogResult.Ok(value)`, so the caller reads it as `result.Value`. `Dialog.CancelAsync()` and
  `Dialog.CloseAsync(DialogResult.Cancel(...))` are the cancelling ones.
- `FluentDialogResult<T>` (`Istarnova.Blazor.FluentUI.Models`): `OkCreate` / `OkUpdate` carry the entity. **A dialog on a CRUD page does not save by itself** - it
  returns the entity and the page persists it.
- Create or update is `Content.{Entity}Id > 0`. Inject the repository interface only where the dialog really needs data of its own (a lookup); never a `DbContext`.

The markup:

```razor
<FluentDialogBody FixedHeaderFooter="true">
    <TitleTemplate> ... </TitleTemplate>

    <ChildContent>
        @if (_editContext is { })
        {
            <EditForm EditContext="_editContext">
                <DataAnnotationsValidator/>
                ...
            </EditForm>
        }
    </ChildContent>

    <ActionTemplate>
        <FluentButton Appearance="ButtonAppearance.Primary" OnClick="@SaveAsync" Loading="@_isSaving" Disabled="@_isSaving">
            @(Dialog.Options.Footer.PrimaryAction.Label ?? Resources.Save)
        </FluentButton>
        <FluentButton Appearance="ButtonAppearance.Default" OnClick="@CancelAsync" Disabled="@_isSaving">
            @(Dialog.Options.Footer.SecondaryAction.Label ?? Resources.Cancel)
        </FluentButton>
    </ActionTemplate>
</FluentDialogBody>
```

- **The `@if (_editContext is { })` is not optional.** v5 renders a dialog's content on its first `await`; an `EditForm` whose context is not built yet throws, and
  the circuit goes with it.
- The buttons are **direct children of `ActionTemplate`** - wrapped in a container they land outside the action slot.
- `FixedHeaderFooter` defaults to `true`: the header and the footer stay, only the content scrolls.

**This scaffold is for a dialog the blocks below do not cover** (a footer with a result of its own). A dialog that edits one model is a `FormDialogBody`, one that
only shows something a `ViewDialogBody`.

## A form dialog: `FormDialogBody`

`FormDialogBody` (`Istarnova.Blazor.FluentUI/Dialogs`, with `ViewDialogBody` and `DialogTitle`) owns the `FluentDialogBody`, icon + title, the `_editContext` guard,
`EditForm`, `DataAnnotationsValidator`, the error line and the save / cancel footer:

```razor
<FormDialogBody Icon="@_dialogIcon" Model="Content" OnSave="@SaveAsync" ErrorMessage="@_errorMessage">
    <FormTextField Label="@Resources.Name" @bind-Value="Content.Name" Full="true"/>
</FormDialogBody>
```

- `OnSave` runs after the model validated; the dialog closes itself there (`Dialog.CloseAsync(FluentDialogResult<T>.OkCreate(...))`). The footer is locked until it
  returns, so a dialog keeps no `_isSaving` and no `_editContext`. Cancel closes with `Dialog.CancelAsync()`.
- `ChildContent` is the inside of the `form-grid`. `ShowValidationSummary` adds the `FluentValidationSummary`; `Title` overrides the title from the options.
  `CanSave="@(...)"` disables Save while the dialog cannot save yet - say it there, not by a click that does nothing.
- **No message is drawn beside a field.** The validation messages are the summary under the form (`ShowValidationSummary="true"`); a `FluentValidationMessage`
  next to an input is a cell of its own in the grid.
- The footer labels come from the options the dialog was opened with (`Footer.PrimaryAction.Label`), so "Share" or "Add to order" instead of "Save" is a matter for
  the caller.
- **A wrapper around a FluentUI input must not forward a `null` `ValueExpression`.** A FluentUI input sets a default expression itself (`FluentInputBase`), so a
  bare `Value=` / `ValueChanged=` on it is fine inside the dialog's `EditForm`. What throws ("requires a value for the 'ValueExpression' parameter") is a component
  that passes its own `ValueExpression` on explicitly while the caller gave none: the null replaces the default. The `Form*Field` components fall back to an
  expression over their own `Value` for that reason; a new wrapper has to do the same.
- `DialogResources` (the library's Save, Cancel, Close) is `internal`: a footer an application writes by hand uses the application's own "Close".

### The fields inside it (`Istarnova.Blazor.FluentUI/Fields`)

A labelled input is never written as a `<div class="form-field">` plus a FluentUI input; it is one of these, each in its own cell of the `form-grid`
(`form-grid.css` of the library, linked once in `App.razor`):

| Field | Wraps | Beyond the common parameters |
| --- | --- | --- |
| `FormTextField` | `FluentTextInput` | `MaxLength`, `Placeholder`, `TextInputType`, `AutoComplete`, `Immediate` |
| `FormTextAreaField` | `FluentTextArea` | `Height`, `MaxLength`, `Placeholder`, `Immediate` |
| `FormNumberField<TValue>` | `FluentNumberInput` | `Min`, `Max`, `Step`, `Placeholder`, `Immediate` |
| `FormDateField<TValue>` | `FluentDatePicker` | `MinDate`, `MaxDate`, `Placeholder` |
| `FormCheckboxField`, `FormSwitchField` | `FluentCheckbox`, `FluentSwitch` | - |
| `FormSelectField<TItem>` | `FluentSelect` | a short, fixed list |
| `FormField` | nothing: the bare cell | for a picker, a select over an enum, a switch with `CheckedMessage`, anything built by hand |

Common to all: `@bind-Value`, `Label`, `Required`, `Disabled`, `ReadOnly`, `Autofocus`, `Id`, `Full` (the whole row), `Center`, `Hint` (the line under the input).

- **A parameter a field does not declare reaches the FluentUI input as a string and throws on a typed one** - and takes the circuit with it. A missing parameter is
  added to the field (and to `FormFieldBase` if it is common), never passed through as a loose attribute; until then that input stays a FluentUI input inside a
  `FormField` (a `Pattern` on a text input).
- `Min` / `Max` / `Step` / `MinDate` / `MaxDate` are forwarded only when given (`FormFieldBase.CollectGiven`): `FluentNumberInput` fills them from the type, and an
  unset `Min` forwarded as `0` would forbid every negative number.
- A choice among records whose table can grow is a searching picker, never a `FluentSelect`.

## A dialog that only shows something: `ViewDialogBody`

Read-only dialogs (statistics, one record's details, a report) are `ViewDialogBody`: icon + title (`DialogTitle`), the content and a Close button. It saves
nothing and closes with `Dialog.CloseAsync()` - **with no value**.

```razor
<ViewDialogBody Icon="@(new RegularIcons.Size24.WeatherMoon())" Title="@Resources.SleepTherapy">
    <SleepNightDetails .../>
</ViewDialogBody>
```

- `TitleContent` replaces icon and title for a heading of several lines; `Actions` adds buttons in front of Close.
- A dialog whose Close hands a result back (a "changed" flag the page reloads on) writes its `FluentDialogBody` by hand, with `DialogTitle` for the title; the
  component does not try to cover it.

## Opening a dialog

`IDialogService.ShowDialogAsync<TDialog>(content, options)` (`DialogServiceExtensions` in `Istarnova.Blazor.FluentUI.Extensions`) puts the model into the dialog's
`Content` parameter and turns the height `"auto"` into `fit-content` (a modal is positioned between its top and bottom insets: `auto` stretches it over the viewport).

```csharp
var result = await DialogService.ShowDialogAsync<CountryDialog>(country, new()
{
    Header = { Title = Resources.EditCountry },
    Footer = { PrimaryAction = { Label = Resources.Save }, SecondaryAction = { Label = Resources.Cancel } },
    Width = "700px",
    Height = "auto",
});

if (result is { Cancelled: false, Value: FluentDialogResult<Country> { Data: { } saved } })
{
    // persist, reload
}
```

A project with a component base class that opens dialogs, persists what comes back and shows the toast says so in its appendix; use that instead of calling the
service in every page.

`DialogOptions` (v5): `Header.Title`, `Footer.PrimaryAction` / `SecondaryAction` (`Label`), `Width`, `Height`, `Size`, `Modal`, `Alignment` (a drawer, not a centred
dialog), `PreventDismissOnEscape`, `Class`, `Style`, `OnStateChange`. Escape closes with a cancelled result by default.

### Confirmation: mind the argument order

**`IDialogService.ShowConfirmationAsync` takes `(message, title, primary, secondary)`** - v4 had `(message, primary, secondary, title)`. Both are four strings, so a
wrong order compiles and silently relabels the buttons. Name the arguments, or go through a helper of the project that does.

## What is gone from v4

| v4 | v5 |
| --- | --- |
| `IDialogContentComponent<T>` | a plain component with `[Parameter] Content` and `[CascadingParameter] IDialogInstance Dialog` |
| `DialogParameters` | `DialogOptions` (`Header` / `Footer` objects instead of `Title`, `PrimaryAction`, `SecondaryAction` strings) |
| `FluentDialogHeader` / `FluentDialogFooter` | `TitleTemplate` / `ActionTemplate` on `FluentDialogBody`, or the options |
| `FluentDialog.Hide()` / `Show()` | `HideAsync()` / `ShowAsync()`. A dialog opened through the service is closed with a result, not hidden |
| `TrapFocus`, `PreventScroll`, `ShowTitle`, `DismissTitle`, `PreventDismissOnOverlayClick` | dropped; the texts the library draws itself come from the `IFluentLocalizer` |
| `DialogType.Panel` | `Alignment = DialogAlignment.Start / End` |
| `IToastService` | `INotificationService` (`ShowSuccess`, `ShowError`, ... - the extensions in `Istarnova.Blazor.FluentUI.Extensions` fire and forget) |

The v4 body workaround (`overflow-y: auto; min-height: 0` on `FluentDialogBody`) is not needed: the body scrolls by itself. A dialog sizes to its content unless
`Height` says otherwise.

## Nested dialogs

Nested dialogs were not re-verified under v5, so do not assume either way. The accessibility reason to avoid them stands (a stack of modals is a screen reader's
worst case): put an inner flow in the page, or on a routed page; open a dialog from a dialog only when there is no alternative, and then test it in the browser first.

## Carried over from v4 and not re-checked

- **Never a Razor comment between the attributes of a tag.** It becomes an attribute and kills the circuit. A `.razor` file carries no comments at all.
- **Nothing inside a dialog may be wider than the dialog.** A child that overflows was painted outside the dialog's background. Give fixed-width fields `flex:none`,
  the flexible one `flex:1; min-width:0`, and let the row wrap.
- **A `FluentSelect` in a dialog opened from another dialog never showed its list in v4.** A handful of fixed choices is a `FluentRadioGroup` with the options
  drawn inline; a long list is a searching picker.
- **A footer button says "Close"** from resources, never the bare word.
