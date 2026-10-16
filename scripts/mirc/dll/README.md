# The theme's DLLs

`dccore.mrc` uses these two for its dark theme and its icons (`/dccore theme`, `/dccore icons`, the section "Theme and icons" in the script). Both are DCCore's own, built from the sources in `src/`. Without them the script works and looks as it did before the theme: a missing `dccore_ui.dll` leaves the dialogs light, a missing `dccore_icons.dll` leaves the menus and the control panel's page list without icons.

## The files

| | `dccore_ui.dll` | `dccore_icons.dll` |
|---|---|---|
| What | Dark title bar, background and controls for the script's dialogs; the icons beside the script's own menu items | 52 icons (16, 20, 24, 32 and 48 px), resources only - no code is run |
| Source | `src/dccore_ui.c` | `src/make_icons.py` (draws them; `icons-preview.png` shows all of them) |
| Build | 32-bit (x86), mingw-w64 | 32-bit (x86), mingw-w64 `windres` |
| SHA-256 | `3927391c2c3bfd1ed7a64cdbd806c45b758dce53665d6c7bcde54ae276d14e1c` | `9b3d5002aa24a44309038501686ae74292c550f8158c99fcf93fdcabee69e4ee` |

The script checks `dccore_ui.dll` against its hash (`dccore.theme.uisha256`) before it loads it, as it checks `dcx.dll`: a file that is not this build is not run. `dccore_icons.dll` is only read for its pictures, by DCX and by Windows.

## `dccore_ui.dll`

Calls, through mIRC's `$dll`:

- `ApplyTheme <hwnd> <mode> [x]` - mode 1 dark, 0 back to the system's look. `x` means the dialog is marked by DCX: DCX paints its background and its own controls (they carry the window property `dcx_cthis`), so only mIRC's controls and the title bar are touched. Every subclass is taken off again before the DLL is unloaded.

  Most of mIRC's controls take the dark colours from the dialog (`WM_CTLCOLOR*`) and Windows' dark themes (`DarkMode_Explorer`, `DarkMode_CFD`, `DarkMode_ItemsView`). Some would not, and are painted by the DLL: check boxes and radio buttons (a dark glyph of its own - Windows' is a white box), push buttons (Windows draws a disabled one light grey), group boxes, list headers (Windows' dark header has black text, which custom draw cannot change), and disabled text (Windows draws it embossed, white with a grey shadow, whatever the colours). Glyphs and buttons are drawn four times as large and averaged down, for smooth edges. Clicks, keys and state stay the controls' own.

  Text without ClearType: ClearType tints each letter's edges red and blue, and light text on a dark background comes out thin and blurred. In the dark theme every control of the dialog, mIRC's and DCX's, gets a twin of its font with `ANTIALIASED_QUALITY` - grey smoothing where the font asks for it, crisp pixels where it does not (Tahoma at mIRC's usual sizes). mIRC's controls share one twin per font; a DCX control gets one of its own, since DCX deletes a control's font when it changes or ends it. The light theme, an unload, or the control's end gives each control its own font back. DCX's texts draw with the font DCX was given (`xdid -f`), not the window's, so in the dark theme the script gives each DCX text and table its font with DCX's flag `a` (anti-aliased) as it makes it (`dccore.sw.fontflags`, `dccore.sw.dcxaa`).
- `MenuIcons <icon file>|off` - icons beside this script's menu items and nobody else's. A hook on mIRC's thread (`WH_CALLWNDPROCRET`) sees each `WM_INITMENUPOPUP` after mIRC has filled the menu; an item whose text begins with a number and `$chr(11)` (the script's `$dccore.mi`) loses that mark and gets that icon as its menu bitmap (`hbmpItem`, 32-bit with alpha). Every other item and every other menu is left as mIRC made it: no owner-draw, no colours. An item another program draws itself is skipped.
- `Version` - `dccore_ui 1.6`.

The dark dialogs are not applied while mIRC itself is in dark mode (mIRC 7.79 and later, `$darkmode`): mIRC then draws its dialogs dark already. The menus follow mIRC: dark in its own dark mode, light otherwise.

Build:

```
i686-w64-mingw32-gcc -O2 -Wall -shared -s -o dccore_ui.dll dccore_ui.c \
    -Wl,--kill-at -Wl,--no-insert-timestamp -static-libgcc \
    -lcomctl32 -luxtheme -lgdi32 -luser32
```

`--no-insert-timestamp` makes the build the same file every time, so the hash above can be checked against a build of your own - with the same compiler, and the output named `dccore_ui.dll` (the name is inside the file). The shipped file was built on Ubuntu 22.04 with `gcc-mingw-w64-i686` 10.3.0-14ubuntu1+24.3, `binutils-mingw-w64-i686` 2.38-3ubuntu1+9build1 and `mingw-w64-i686-dev` 8.0.0-1; another version builds a working DLL with another hash.

### What changed in 1.6

- `ThemeMenus` is gone (the script never called it).
- `dwmapi.dll` is no longer imported: `DwmSetWindowAttribute` is looked up when the DLL first needs it, and without it the title bar stays light.
- The first `ApplyTheme` or `MenuIcons` pins the DLL in memory, so a `/dll -u` from inside a dialog event cannot unmap it under its own code. `UnloadDll` still takes the hook and every subclass off. The price: once the DLL has been used, mIRC holds the file until it exits, so replacing `dccore_ui.dll` needs a restart of mIRC.
- `ApplyTheme` answers `ERR ...` when it cannot make a dialog dark (too many dark dialogs at once, or the subclass failed), and refuses a window of another thread.
- A DCX control whose font DCX replaces (`xdid -f`) is let go of (DCX deletes both its fonts itself); going light takes every control's subclass off whatever its style is now; the two background brushes are deleted when the last dark dialog goes light, and at unload.
- `MenuIcons` reads the icon file's path as UTF-8 first, so a script folder with letters outside the ANSI code page works.
- The anti-aliased drawing of glyphs and buttons is capped at 1024 x 256 pixels a control.

## `dccore_icons.dll`

The icons' order is their number, and the script's `dccore.ico.names` must list them in the same order: `app console settings script status slots queue dlqueue uptime version help lists fetch ask purge library rebuild audio ban unban clear ignore moveup movedown update rehash stop connect disconnect pair unpair trust chat downloads panel font dashboard cancel again user speed search pack folder hash palette megaphone mail bug key home shield`. A new icon goes at the end, so the others keep their numbers.

Build:

```
python3 make_icons.py build          # the .ico files, dccore_icons.rc, icons-preview.png
cd build
i686-w64-mingw32-windres dccore_icons.rc -O coff -o res.o
i686-w64-mingw32-gcc -shared -s -o dccore_icons.dll ../dll.c res.o -Wl,--no-insert-timestamp -static-libgcc
```

`src/dll.c` is an empty `DllMain` returning `TRUE`; the icons are all in the resources.

`make_icons.py` needs Pillow and a bold font for its five letter icons: DejaVu Sans Bold (the shipped icons' font) where it finds it, otherwise Arial Bold or Segoe UI Bold; `--font <path>` names another.

## Where they show

- **Menus**: with `/dccore icons on` the script starts `MenuIcons` and gives each of its items its number (`$dccore.mi`). Only those items get a picture; the menus are otherwise mIRC's own. DCX is not involved.
- **The control panel's page list**: just after the window is made, in a timer of its own, the DCX listview gets the whole file (`xdid -w +n -1`) and its rows are filled again, each page with its icon by name (`dccore.theme.pagemap`). A page the layout gains later has no icon until it is named there.
- **Without DCX** - not loadable, or the built-in window chosen in the mIRC settings - the page list has no icons; the menu icons and the dark dialogs only need `dccore_ui.dll`.
