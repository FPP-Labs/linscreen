# Changelog

## 1.0.0

First public release.

### Capture
- Tray app for area screenshots on Wayland and X11.
- Picks a working capture backend for the desktop:
  - `grim` + `slurp` on Hyprland, Sway, Niri and other wlroots compositors;
  - `spectacle` on KDE and `gnome-screenshot` on GNOME;
  - `xdg-desktop-portal` as the Wayland fallback;
  - `maim` / `scrot` on X11;
  - a built-in selection overlay when none of these is available.
- Every shot is copied to the clipboard and saved to `~/Pictures/LinScreen/`.
- Single instance: running `linscreen` again opens the settings; `--screenshot`, `--quit` and `--replace` control the running app.

### Hotkeys
- LinScreen writes the screenshot hotkey into compositor configs and reloads the compositor:
  - Hyprland (Omarchy `bindings.lua`, `bindings.conf`, `hyprland.conf`);
  - Sway and i3;
  - Niri.
- Hand-written `linscreen --screenshot` binds are found and taken over, so the shortcut is restored after a reinstall.
- Config edits are made safely:
  - a dated backup is made before the first edit of the day;
  - symlinked configs are edited in place and file permissions are kept;
  - a file with broken LinScreen markers is left untouched;
  - Hyprland edits are rolled back when the reload reports a new error;
  - on Niri, a key that is already taken is reported instead of being overwritten;
  - single keys without modifiers are limited to Print, F1–F24 and media keys.

### Interface
- Settings window that follows the system light or dark palette and reflows on narrow windows.
- Selection overlay with an accent frame, handles, size label and crosshair.
- Autostart switch, copyable command snippets and a desktop-environment badge.

### Packaging
- Arch Linux package (`PKGBUILD`).
- `.deb` for Debian 12+ / Ubuntu 24.04+ and `.rpm` for Fedora, built with nfpm and smoke-tested in clean containers.
