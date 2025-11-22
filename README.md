# pylauncher

![screenshot](demo.png)

This is a simple launcher I wrote during my transition from xfce-panel's whisker-menu to waybar.

## Features

- **Starring applications** will add them to the main startup and create the file `~/.config/launcher-favorites.json`
- **Command execution**: It will execute a command (e.g., `pkill waybar`) if a matching application during the search doesn't match

## Shell Configuration

By default, the launcher uses Fish shell aliases. To use your default shell instead:

Remove:
```python
['fish', '-c', query],
```

And uncomment:
```python
query,
shell=True,
```

## Window Styling (Niri)

I style it in niri with:
```
window-rule {
    match app-id="pylauncher.py"
    open-floating true
    opacity 1.0
    default-window-height { proportion 0.37; }
    default-column-width { proportion 0.11; }
    default-floating-position x=0 y=0 relative-to="bottom-left"
    geometry-corner-radius 0 8 0 0
}
```
