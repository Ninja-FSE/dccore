# DCX for the settings window

`dccore.mrc` draws its settings window (`/dccore settings`) with **DCX** - Dialog Control eXtensions for mIRC - when it can load it, and with mIRC's own controls when it cannot. With DCX the pages are a tree on the left instead of tabs and a column of buttons; the pages themselves are the same window.

Neither file here is part of DCCore. Both are shipped unmodified, with their licences, so the window works without a separate download.

## The files

| | `dcx.dll` | `WebView2Loader.dll` |
|---|---|---|
| What | DCX itself | Microsoft's WebView2 loader, which DCX loads when it starts |
| From | DCX release `3.1-git978` (https://github.com/twig/dcxdll/releases/tag/3.1-git978), `dcx310dev66git978Optimal.zip` | the same zip; byte for byte Microsoft's `runtimes/win-x86/native/WebView2Loader.dll` in the `Microsoft.Web.WebView2` 1.0.2151.40 NuGet package |
| Project | https://github.com/twig/dcxdll | https://aka.ms/webview |
| Licence | BSD-3-Clause, [LICENSE-DCX.txt](LICENSE-DCX.txt) | BSD-3-Clause-style, [LICENSE-WebView2.txt](LICENSE-WebView2.txt) (the package's own `LICENSE.txt`) |
| Build | 32-bit (x86) | 32-bit (x86), version 1.0.2151.40 |
| SHA-256 | `321e9ac2168734edd6ff6e48f4bd9540e9297f5e1360773edda9d554e620db12` | `5659cbae9f3d412662515671a6c85aefe08eee17118c3de1330a2fed74dc415f` |

The script checks both hashes before it loads DCX, and `tests/test_the_bundled_dcx_dll_is_the_pinned_release.py` checks the files, this table and the script against each other - so neither can be swapped without all three changing together.

**Why the loader is here.** `dcx.dll` itself needs nothing but Windows' own libraries, but every time it starts it tries to load `WebView2Loader.dll` - from mIRC's folder, then from beside itself - for its web control (`web2ctrl`), and without it writes `D_ERROR LoadDLL (Warning Unable to Load WebView2Loader.dll, web2ctrl will not work.)` in the status window. The window uses no web control today; shipping the loader removes that line, and leaves DCX's web control available to a later version of the script. It only loads Microsoft Edge's WebView2 runtime when a web control is actually created.

## When the built-in window is used instead

`/dccore dcx` says which window is in use and why. The built-in one opens when:

- the option *Use the built-in settings window, not DCX* is on (the settings window's *This mIRC window* page);
- the mIRC version is below 7.45 - DCX 3.1 needs 7.45 or later;
- mIRC is the 64-bit build - `dcx.dll` is 32-bit and cannot be loaded into it;
- `dcx.dll` is missing, is not the file above (the hash differs), or will not load (an antivirus blocking it, say);
- `WebView2Loader.dll` is beside it but is not the file above. (Missing is fine: DCX then only writes the warning.)

Both windows are built from the same layout (`scripts/mirc/settings_window_layout.py`) and edit the same settings.

## Updating DCX

Replace the file, then change its column above **and** `dccore.dcx.sha256` (or `dccore.dcx.wv2sha256`) in `dccore.mrc`. The test fails until all three agree. Take the loader from the DCX release it came with, and check it against the matching `Microsoft.Web.WebView2` package.
