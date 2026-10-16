/*
 * dccore_ui.dll - the dark theme of the DCCore mIRC script's dialogs and menus
 *
 * Calls (mIRC's $dll / /dll):
 *   ApplyTheme  <hwnd> <mode> [flags]   mode 1 dark, 0 back to the system's look.
 *                                       flags "x": the dialog is marked by DCX. DCX
 *                                       paints that dialog's background and its own
 *                                       controls, so here only mIRC's own controls
 *                                       and the title bar are touched.
 *   MenuIcons   <icon file>|off         icons in front of the script's own menu
 *                                       items, nobody else's: see below
 *   Version                             "dccore_ui <version>"
 *
 * Every call returns its answer as the $dll() value ("OK" or "ERR ...").
 *
 * A control created by DCX carries the window property "dcx_cthis": those are
 * left to DCX (their colours are set with xdid -C / -i), except that DCX
 * listviews and treeviews get the dark scroll bars, and DCX listviews light
 * header text. Check boxes, radio buttons, push buttons, group boxes and
 * disabled texts are painted here (see "controls painted here"); the rest
 * take the dark colours through WM_CTLCOLOR* and Windows' dark themes.
 *
 * Menu icons. Each popup sends WM_INITMENUPOPUP to the window that owns it
 * as it opens. A hook on mIRC's own thread (WH_CALLWNDPROCRET) sees that
 * message after the window - mIRC, and whatever else hooked it - has filled
 * the menu, whichever window owns it, and in each menu an item whose text begins
 * "<N>" and $chr(11) - the script's $dccore.mi - loses that and gets icon N
 * of the icon file (counted from 1) as its bitmap (hbmpItem). Every other
 * item, and every menu without such an item, is left exactly as mIRC made
 * it: no owner-draw, no other colours. An item another program draws itself
 * (MFT_OWNERDRAW) is skipped.
 *
 * Build: see ../README.md (32-bit, for a 32-bit mIRC).
 */
#define WIN32_LEAN_AND_MEAN
#define _WIN32_IE 0x0600
#include <windows.h>
#include <commctrl.h>
#include <uxtheme.h>
#include <vssym32.h>
#include <stdlib.h>
#include <string.h>

#define DCCORE_UI_VERSION "dccore_ui 1.6"

#ifndef LOAD_LIBRARY_AS_IMAGE_RESOURCE
#define LOAD_LIBRARY_AS_IMAGE_RESOURCE 0x00000020
#endif

#ifndef DWMWA_USE_IMMERSIVE_DARK_MODE
#define DWMWA_USE_IMMERSIVE_DARK_MODE 20
#endif

#define CLR_BG    RGB(32, 32, 32)
#define CLR_EDIT  RGB(43, 45, 48)
#define CLR_FG    RGB(238, 238, 238)
#define CLR_DIS   RGB(160, 160, 160)  /* a disabled control's text */
#define CLR_LINE  RGB(85, 85, 85)     /* a group box's frame */

#ifndef BST_HOT
#define BST_HOT 0x0200
#endif

#define F_DCX     1u      /* the dialog is marked by DCX */

typedef struct { DWORD mVersion; HWND mHwnd; BOOL mKeep; BOOL mUnicode; DWORD mBeta; DWORD mBytes; } LOADINFO;

static HBRUSH g_brBg, g_brEdit;
static const UINT_PTR SUBCLASS_ID = 0x44434355; /* 'DCCU' */
static const UINT_PTR CHILD_ID    = 0x44434343; /* 'DCCC': a control painted here */

/* the dialogs subclassed now, so an unload can undo it first */
#define MAX_DLGS 32
static HWND g_dlgs[MAX_DLGS];

/* 0 when a dialog to be made dark finds no room: it then stays light, as
   a subclass the unload could not find would outlive the DLL */
static int track(HWND h, int on) {
    int i, free_at = -1;
    for (i = 0; i < MAX_DLGS; i++) {
        if (g_dlgs[i] == h) { if (!on) g_dlgs[i] = NULL; return 1; }
        if (!g_dlgs[i] && free_at < 0) free_at = i;
    }
    if (!on) return 1;
    if (free_at < 0) return 0;
    g_dlgs[free_at] = h;
    return 1;
}

static int is_dcx(HWND h) { return h && GetPropW(h, L"dcx_cthis") != NULL; }

static int any_tracked(void) {
    int i;
    for (i = 0; i < MAX_DLGS; i++) if (g_dlgs[i]) return 1;
    return 0;
}

/* the two brushes: made on demand by the first dark dialog, deleted when
   the last one goes light and at unload */
static void free_brushes(void) {
    if (g_brBg)   { DeleteObject(g_brBg);   g_brBg = NULL; }
    if (g_brEdit) { DeleteObject(g_brEdit); g_brEdit = NULL; }
}

/* ---------- the module stays mapped ----------
 * A dialog event (an sclick, say) runs inside DlgProc or CtlProc; a /dll -u
 * from there would free the module under them, and the return would land in
 * memory that is gone. So the first ApplyTheme or MenuIcons pins the module:
 * never unmapped for the life of the process. UnloadDll still takes the hook
 * and every subclass off, as before.
 */
#ifndef GET_MODULE_HANDLE_EX_FLAG_PIN
#define GET_MODULE_HANDLE_EX_FLAG_PIN 0x00000001
#endif
#ifndef GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS
#define GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS 0x00000004
#endif

static void pin_module(void) {
    static int pinned;
    HMODULE me;
    if (pinned) return;
    pinned = 1;
    /* any address inside this module names it: g_dlgs is one */
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_PIN | GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS, (LPCWSTR)g_dlgs, &me);
}

/* ---------- the title bar: DWM's, where there is one ----------
 * dwmapi.dll is looked up, not imported: a Windows without it loads this
 * DLL all the same, and its title bars simply stay light. One LoadLibraryW,
 * kept for the life of the process.
 */
typedef HRESULT (WINAPI *fnDwmSetWindowAttribute)(HWND hwnd, DWORD dwAttribute, LPCVOID pvAttribute, DWORD cbAttribute);

static fnDwmSetWindowAttribute dwm_set_attribute(void) {
    static fnDwmSetWindowAttribute f;
    static int looked;
    if (!looked) {
        HMODULE dwm = LoadLibraryW(L"dwmapi.dll");
        f = dwm ? (fnDwmSetWindowAttribute)GetProcAddress(dwm, "DwmSetWindowAttribute") : NULL;
        looked = 1;
    }
    return f;
}

/* ---------- the dialog's subclass: dark backgrounds and light text ---------- */
static LRESULT CALLBACK DlgProc(HWND h, UINT m, WPARAM w, LPARAM l, UINT_PTR id, DWORD_PTR ref) {
    switch (m) {
    case WM_CTLCOLORDLG:
        if (ref & F_DCX) break;
        SetTextColor((HDC)w, CLR_FG); SetBkColor((HDC)w, CLR_BG);
        return (LRESULT)g_brBg;
    case WM_CTLCOLORSTATIC:
    case WM_CTLCOLORBTN:
    case WM_CTLCOLORSCROLLBAR:
        if (is_dcx((HWND)l)) break;
        SetTextColor((HDC)w, CLR_FG); SetBkColor((HDC)w, CLR_BG);
        return (LRESULT)g_brBg;
    case WM_CTLCOLOREDIT:
    case WM_CTLCOLORLISTBOX:
        if (is_dcx((HWND)l)) break;
        SetTextColor((HDC)w, CLR_FG); SetBkColor((HDC)w, CLR_EDIT);
        return (LRESULT)g_brEdit;
    case WM_ERASEBKGND:
        if (ref & F_DCX) break;
        { RECT rc; GetClientRect(h, &rc); FillRect((HDC)w, &rc, g_brBg); }
        return 1;
    case WM_NCDESTROY:
        RemoveWindowSubclass(h, DlgProc, id);
        track(h, 0);
        break;
    }
    return DefSubclassProc(h, m, w, l);
}

/* ---------- controls painted here ----------
 * Some of mIRC's controls would not take the dark colours the dialog's
 * WM_CTLCOLOR* gives them, or Windows' dark themes leave them light:
 *  - check boxes and radio buttons: the theme's glyph is a white box, and
 *    their text comes in the theme's colour (black);
 *  - push buttons: a disabled one is drawn light grey;
 *  - disabled text, of any of them or of a static: Windows draws it
 *    embossed (white with a grey shadow), whatever the colours;
 *  - group boxes: frame and title come from the theme;
 *  - a list's header: dark, with black text.
 * Those are painted here: glyphs and buttons drawn four times as large and
 * averaged down (smooth edges with GDI alone), the text light, a disabled
 * one plain grey with no colour fringes (grey ClearType on black blurs),
 * the focus rectangle and the keyboard cues as Windows shows them.
 * Everything else about them - clicks, keys, messages, their state - is
 * still the control's own.
 */
#define CLR_GLYPH_LINE   RGB(160, 160, 160)
#define CLR_GLYPH_HOT    RGB(205, 205, 205)
#define CLR_GLYPH_OFF    RGB(80, 80, 80)
#define CLR_GLYPH_MARKX  RGB(110, 110, 110)  /* a disabled box's check */
#define CLR_BTN          RGB(51, 51, 51)
#define CLR_BTN_HOT      RGB(69, 69, 69)
#define CLR_BTN_DOWN     RGB(38, 38, 38)
#define CLR_BTN_ON       RGB(74, 74, 74)     /* a push-like check box, checked */
#define CLR_BTN_OFF      RGB(40, 40, 40)
#define CLR_BTN_LINE     RGB(95, 95, 95)
#define CLR_BTN_LINEDEF  RGB(150, 150, 150)  /* the default button, or the focused one */
#define CLR_BTN_LINEOFF  RGB(62, 62, 62)

static HFONT ctl_font(HWND h) {
    HFONT f = (HFONT)SendMessageW(h, WM_GETFONT, 0, 0);
    if (!f && GetParent(h)) f = (HFONT)SendMessageW(GetParent(h), WM_GETFONT, 0, 0);
    return f ? f : (HFONT)GetStockObject(DEFAULT_GUI_FONT);
}
static int hide_prefix(HWND h) { return (SendMessageW(h, WM_QUERYUISTATE, 0, 0) & UISF_HIDEACCEL) != 0; }
static int hide_focus(HWND h)  { return (SendMessageW(h, WM_QUERYUISTATE, 0, 0) & UISF_HIDEFOCUS) != 0; }

/* ---- the text without ClearType ----
 * GDI's ClearType tints the edges of each letter red and blue for the
 * screen's sub-pixels; light text on a dark background comes out thin and
 * blurred that way. So in the dark theme every control of the dialog -
 * mIRC's and DCX's - gets a twin of its own font with ANTIALIASED_QUALITY:
 * grey smoothing where the font asks for it, crisp pixels where it does not
 * (Tahoma, mIRC's dialog font, at its usual sizes). The light theme, an
 * unload, or the control's end gives the control its own font back.
 *
 * mIRC's controls share one twin per font (kept until the DLL unloads).
 * A DCX control gets a twin of its own: DCX deletes the font a control has
 * when it changes or ends it, so its twin is DCX's to delete then - or ours,
 * when the control gets its own font back. When DCX changes it, DCX deletes
 * the font the control had before the twin as well: FontProc only lets go.
 */
#define MAX_FONTS 32
static struct { LOGFONTW lf; HFONT f; } g_fonts[MAX_FONTS];
static HFONT plain_font(HFONT f) {
    LOGFONTW lf;
    int i;
    memset(&lf, 0, sizeof lf);
    if (!f || !GetObjectW(f, sizeof lf, &lf)) return f;
    if (lf.lfQuality == ANTIALIASED_QUALITY || lf.lfQuality == NONANTIALIASED_QUALITY) return f;
    lf.lfQuality = ANTIALIASED_QUALITY;
    for (i = 0; i < MAX_FONTS; i++)
        if (g_fonts[i].f && !memcmp(&g_fonts[i].lf, &lf, sizeof lf)) return g_fonts[i].f;
    for (i = 0; i < MAX_FONTS; i++)
        if (!g_fonts[i].f) {
            g_fonts[i].f = CreateFontIndirectW(&lf);
            if (!g_fonts[i].f) return f;
            g_fonts[i].lf = lf;
            return g_fonts[i].f;
        }
    return f;
}
static void free_fonts(void) {
    int i;
    for (i = 0; i < MAX_FONTS; i++) if (g_fonts[i].f) { DeleteObject(g_fonts[i].f); g_fonts[i].f = NULL; }
}
/* the font text is drawn with here: the control's, without ClearType */
static HGDIOBJ text_font(HWND h, HDC hdc) {
    return SelectObject(hdc, plain_font(ctl_font(h)));
}

static const WCHAR P_FONT[] = L"dccore_ui.font", P_TWIN[] = L"dccore_ui.twin", P_OWN[] = L"dccore_ui.own";
static const UINT_PTR FONT_ID = 0x44434346; /* 'DCCF' */
static void untwin_font(HWND c, BOOL redraw);

static LRESULT CALLBACK FontProc(HWND h, UINT m, WPARAM w, LPARAM l, UINT_PTR id, DWORD_PTR ref) {
    if (m == WM_NCDESTROY) untwin_font(h, FALSE);   /* its own font back before it ends */
    else if (m == WM_SETFONT && GetPropW(h, P_OWN) && (HANDLE)w != GetPropW(h, P_TWIN)) {
        /* DCX replaces its control's font (xdid -f). Its setControlFont()
           deletes both fonts itself: the one the control has (the twin) and
           the one it keeps (the font from before the twin). Neither is ours
           to delete again, so the control is only let go of: untwin_font then
           finds no twin and touches nothing. A WM_SETFONT of the twin itself
           (twin_font's own), or on a control whose twin is a shared plain
           font (no P_OWN), changes nothing. */
        RemovePropW(h, P_FONT); RemovePropW(h, P_TWIN); RemovePropW(h, P_OWN);
        RemoveWindowSubclass(h, FontProc, FONT_ID);
    }
    return DefSubclassProc(h, m, w, l);
}

static void twin_font(HWND c, int own) {
    HFONT cur, tw;
    LOGFONTW lf;
    if (GetPropW(c, P_TWIN)) return;                         /* has one */
    cur = (HFONT)SendMessageW(c, WM_GETFONT, 0, 0);
    memset(&lf, 0, sizeof lf);
    if (!cur || !GetObjectW(cur, sizeof lf, &lf)) return;   /* no font of its own */
    if (lf.lfQuality == ANTIALIASED_QUALITY || lf.lfQuality == NONANTIALIASED_QUALITY) return;
    if (own) { lf.lfQuality = ANTIALIASED_QUALITY; tw = CreateFontIndirectW(&lf); }
    else tw = plain_font(cur);
    if (!tw || tw == cur) return;
    if (!SetWindowSubclass(c, FontProc, FONT_ID, 0)) { if (own) DeleteObject(tw); return; }
    SetPropW(c, P_FONT, (HANDLE)cur);
    SetPropW(c, P_TWIN, (HANDLE)tw);
    if (own) SetPropW(c, P_OWN, (HANDLE)1);
    SendMessageW(c, WM_SETFONT, (WPARAM)tw, TRUE);
}

static void untwin_font(HWND c, BOOL redraw) {
    HFONT orig = (HFONT)GetPropW(c, P_FONT), tw = (HFONT)GetPropW(c, P_TWIN);
    int own = GetPropW(c, P_OWN) != NULL;
    RemoveWindowSubclass(c, FontProc, FONT_ID);
    if (!tw) return;
    RemovePropW(c, P_FONT); RemovePropW(c, P_TWIN); RemovePropW(c, P_OWN);
    /* a font set since (DCX's xdid -f, mIRC's did -f) stays, and DCX has
       deleted the twin of its control then */
    if ((HFONT)SendMessageW(c, WM_GETFONT, 0, 0) != tw) return;
    SendMessageW(c, WM_SETFONT, (WPARAM)orig, redraw);
    if (own) DeleteObject(tw);
}

/* ---- shapes drawn large and averaged down ---- */
#define SS 4
enum { SH_BOX, SH_RADIO, SH_BUTTON };
typedef struct {
    int shape, mark;           /* mark: 0 none, 1 check, 2 mixed (a box) / the dot (a radio) */
    COLORREF line, fill, ink;  /* frame, inside, the mark */
} SHAPE;

static void shape_big(HDC dc, int W, int H, const SHAPE *s) {
    HPEN pen = CreatePen(PS_INSIDEFRAME, SS, s->line);
    HBRUSH br = CreateSolidBrush(s->fill);
    HGDIOBJ op = SelectObject(dc, pen), ob = SelectObject(dc, br);
    RECT r = {0, 0, W, H};
    FillRect(dc, &r, g_brBg);
    if (s->shape == SH_RADIO) Ellipse(dc, 0, 0, W, H);
    else if (s->shape == SH_BOX) RoundRect(dc, 0, 0, W, H, W / 4, H / 4);
    else RoundRect(dc, 0, 0, W, H, 8 * SS, 8 * SS);
    SelectObject(dc, op); SelectObject(dc, ob);
    DeleteObject(pen); DeleteObject(br);

    if (s->shape == SH_BOX && s->mark == 1) {
        LOGBRUSH lb;
        POINT p[3];
        HPEN ck;
        lb.lbStyle = BS_SOLID; lb.lbColor = s->ink; lb.lbHatch = 0;
        ck = ExtCreatePen(PS_GEOMETRIC | PS_SOLID | PS_ENDCAP_ROUND | PS_JOIN_ROUND, (W * 3) / 26 + 1, &lb, 0, NULL);
        p[0].x = W * 24 / 100; p[0].y = H * 52 / 100;
        p[1].x = W * 42 / 100; p[1].y = H * 70 / 100;
        p[2].x = W * 77 / 100; p[2].y = H * 31 / 100;
        op = SelectObject(dc, ck);
        Polyline(dc, p, 3);
        SelectObject(dc, op);
        DeleteObject(ck);
    } else if (s->mark) {
        HBRUSH ib = CreateSolidBrush(s->ink);
        HGDIOBJ np = SelectObject(dc, GetStockObject(NULL_PEN));
        ob = SelectObject(dc, ib);
        if (s->shape == SH_RADIO) Ellipse(dc, W * 3 / 10, H * 3 / 10, W - W * 3 / 10 + 1, H - H * 3 / 10 + 1);
        else Rectangle(dc, W * 27 / 100, H * 27 / 100, W - W * 27 / 100 + 1, H - H * 27 / 100 + 1);
        SelectObject(dc, np); SelectObject(dc, ob);
        DeleteObject(ib);
    }
}

/* the drawn size is capped: at SS times MAX_SHAPE_W x MAX_SHAPE_H the big
   DIB is 16 MB. A larger control gets its shape drawn at the cap's size, at
   its top left; none in the script's dialogs comes near it. */
#define MAX_SHAPE_W 1024
#define MAX_SHAPE_H 256
static void draw_shape(HDC hdc, const RECT *at, const SHAPE *s) {
    int w = at->right - at->left, h = at->bottom - at->top, W, H, x, y;
    BITMAPINFO bi;
    DWORD *big = NULL, *small = NULL;
    HBITMAP hb, hs;
    HDC mb, ms;
    HGDIOBJ ob, os;
    if (w <= 0 || h <= 0) return;
    if (w > MAX_SHAPE_W) w = MAX_SHAPE_W;
    if (h > MAX_SHAPE_H) h = MAX_SHAPE_H;
    W = w * SS; H = h * SS;
    memset(&bi, 0, sizeof bi);
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    bi.bmiHeader.biWidth = W; bi.bmiHeader.biHeight = -H;
    hb = CreateDIBSection(hdc, &bi, DIB_RGB_COLORS, (void **)&big, NULL, 0);
    bi.bmiHeader.biWidth = w; bi.bmiHeader.biHeight = -h;
    hs = CreateDIBSection(hdc, &bi, DIB_RGB_COLORS, (void **)&small, NULL, 0);
    if (!hb || !hs || !big || !small) { if (hb) DeleteObject(hb); if (hs) DeleteObject(hs); return; }
    mb = CreateCompatibleDC(hdc);
    ms = CreateCompatibleDC(hdc);
    ob = SelectObject(mb, hb);
    os = SelectObject(ms, hs);
    shape_big(mb, W, H, s);
    GdiFlush();
    for (y = 0; y < h; y++)
        for (x = 0; x < w; x++) {
            unsigned r = 0, g = 0, b = 0;
            int i, j;
            for (j = 0; j < SS; j++)
                for (i = 0; i < SS; i++) {
                    DWORD p = big[(y * SS + j) * W + x * SS + i];
                    r += (p >> 16) & 255; g += (p >> 8) & 255; b += p & 255;
                }
            small[y * w + x] = ((r / (SS * SS)) << 16) | ((g / (SS * SS)) << 8) | (b / (SS * SS));
        }
    BitBlt(hdc, at->left, at->top, w, h, ms, 0, 0, SRCCOPY);
    SelectObject(mb, ob); SelectObject(ms, os);
    DeleteDC(mb); DeleteDC(ms);
    DeleteObject(hb); DeleteObject(hs);
}

/* a check box's or radio button's glyph: its size the theme's (it follows
   the screen's DPI), 13 pixels at 96 DPI without one */
static SIZE glyph_size(HWND h, HDC hdc, int radio) {
    SIZE gs;
    HTHEME th = OpenThemeData(h, L"Button");
    gs.cx = gs.cy = MulDiv(13, GetDeviceCaps(hdc, LOGPIXELSY), 96);
    if (th) {
        SIZE t;
        if (SUCCEEDED(GetThemePartSize(th, hdc, radio ? BP_RADIOBUTTON : BP_CHECKBOX, 1, NULL, TS_DRAW, &t)) && t.cx > 4 && t.cy > 4) gs = t;
        CloseThemeData(th);
    }
    return gs;
}

static void paint_check(HWND h, HDC hdc) {
    RECT rc, g, tr;
    LONG st = GetWindowLongW(h, GWL_STYLE);
    DWORD type = st & BS_TYPEMASK;
    int radio = (type == BS_RADIOBUTTON || type == BS_AUTORADIOBUTTON);
    int on = IsWindowEnabled(h), left = (st & BS_LEFTTEXT) != 0;
    LRESULT check = SendMessageW(h, BM_GETCHECK, 0, 0);
    LRESULT bst = SendMessageW(h, BM_GETSTATE, 0, 0);
    SHAPE s;
    SIZE gs;
    WCHAR text[512];
    UINT fl;
    HGDIOBJ oldf;
    int gap;

    GetClientRect(h, &rc);
    FillRect(hdc, &rc, g_brBg);
    oldf = text_font(h, hdc);

    gs = glyph_size(h, hdc, radio);
    g.left = left ? rc.right - gs.cx : rc.left;
    g.top = rc.top + (rc.bottom - rc.top - gs.cy) / 2;
    g.right = g.left + gs.cx;
    g.bottom = g.top + gs.cy;
    s.shape = radio ? SH_RADIO : SH_BOX;
    s.mark = (check == BST_CHECKED) ? 1 : (check == BST_INDETERMINATE && !radio) ? 2 : 0;
    s.line = !on ? CLR_GLYPH_OFF : (bst & (BST_HOT | BST_PUSHED)) ? CLR_GLYPH_HOT : CLR_GLYPH_LINE;
    s.fill = !on ? CLR_BG : (bst & BST_PUSHED) ? CLR_BTN_DOWN : (bst & BST_HOT) ? CLR_BTN : CLR_EDIT;
    s.ink = on ? CLR_FG : CLR_GLYPH_MARKX;
    draw_shape(hdc, &g, &s);

    GetWindowTextW(h, text, 512);
    gap = gs.cx / 3 + 1;
    tr = rc;
    if (left) tr.right = g.left - gap; else tr.left = g.right + gap;
    fl = (left ? DT_RIGHT : DT_LEFT) | DT_NOCLIP | (hide_prefix(h) ? DT_HIDEPREFIX : 0);
    if (st & BS_MULTILINE) {
        RECT m = tr;
        DrawTextW(hdc, text, -1, &m, fl | DT_WORDBREAK | DT_CALCRECT);
        tr.top = rc.top + (rc.bottom - rc.top - (m.bottom - m.top)) / 2;
        fl |= DT_WORDBREAK;
    } else fl |= DT_SINGLELINE | DT_VCENTER;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, on ? CLR_FG : CLR_DIS);
    DrawTextW(hdc, text, -1, &tr, fl);

    if (GetFocus() == h && !hide_focus(h) && text[0]) {
        RECT f = tr;
        DrawTextW(hdc, text, -1, &f, (fl & ~DT_VCENTER) | DT_CALCRECT);
        if (left) { int wd = f.right - f.left; f.right = tr.right; f.left = f.right - wd; }
        if (!(st & BS_MULTILINE)) { int hgt = f.bottom - f.top; f.top = rc.top + (rc.bottom - rc.top - hgt) / 2; f.bottom = f.top + hgt; }
        InflateRect(&f, 1, 0);
        SetTextColor(hdc, CLR_FG);
        SetBkColor(hdc, CLR_BG);
        DrawFocusRect(hdc, &f);
    }
    SelectObject(hdc, oldf);
}

/* a push button, or a check box or radio button that looks like one */
static void paint_push(HWND h, HDC hdc) {
    RECT rc, tr;
    LONG st = GetWindowLongW(h, GWL_STYLE);
    DWORD type = st & BS_TYPEMASK;
    int on = IsWindowEnabled(h), focus = (GetFocus() == h);
    LRESULT bst = SendMessageW(h, BM_GETSTATE, 0, 0);
    int checked = (st & BS_PUSHLIKE) && SendMessageW(h, BM_GETCHECK, 0, 0) == BST_CHECKED;
    SHAPE s;
    WCHAR text[512];
    UINT fl;
    HGDIOBJ oldf;

    GetClientRect(h, &rc);
    s.shape = SH_BUTTON;
    s.mark = 0;
    s.ink = 0;
    s.fill = !on ? CLR_BTN_OFF : (bst & BST_PUSHED) ? CLR_BTN_DOWN : checked ? CLR_BTN_ON : (bst & BST_HOT) ? CLR_BTN_HOT : CLR_BTN;
    s.line = !on ? CLR_BTN_LINEOFF : (type == BS_DEFPUSHBUTTON || focus) ? CLR_BTN_LINEDEF : CLR_BTN_LINE;
    draw_shape(hdc, &rc, &s);

    oldf = text_font(h, hdc);
    GetWindowTextW(h, text, 512);
    tr = rc;
    InflateRect(&tr, -4, -2);
    switch (st & BS_CENTER) {
    case BS_LEFT:  fl = DT_LEFT; break;
    case BS_RIGHT: fl = DT_RIGHT; break;
    default:       fl = DT_CENTER; break;
    }
    fl |= (hide_prefix(h) ? DT_HIDEPREFIX : 0);
    if (st & BS_MULTILINE) {
        RECT m = tr;
        DrawTextW(hdc, text, -1, &m, fl | DT_WORDBREAK | DT_CALCRECT);
        tr.top = rc.top + (rc.bottom - rc.top - (m.bottom - m.top)) / 2;
        fl |= DT_WORDBREAK;
    } else fl |= DT_SINGLELINE | DT_VCENTER;
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, on ? CLR_FG : CLR_DIS);
    DrawTextW(hdc, text, -1, &tr, fl);
    if (focus && !hide_focus(h)) {
        RECT f = rc;
        InflateRect(&f, -3, -3);
        SetTextColor(hdc, CLR_FG);
        SetBkColor(hdc, s.fill);
        DrawFocusRect(hdc, &f);
    }
    SelectObject(hdc, oldf);
}

static void paint_group(HWND h, HDC hdc) {
    RECT rc, tr;
    WCHAR text[512];
    SIZE ts = {0, 0};
    int y, x0, x1, on = IsWindowEnabled(h);
    HGDIOBJ oldf, oldp;
    HPEN pen;
    int n;

    GetClientRect(h, &rc);
    oldf = text_font(h, hdc);
    n = GetWindowTextW(h, text, 512);
    GetTextExtentPoint32W(hdc, n ? text : L"X", n ? n : 1, &ts);
    y = rc.top + ts.cy / 2;
    x0 = rc.left + 6;
    x1 = n ? x0 + ts.cx + 4 : x0;

    pen = CreatePen(PS_SOLID, 1, CLR_LINE);
    oldp = SelectObject(hdc, pen);
    MoveToEx(hdc, x0, y, NULL);        LineTo(hdc, rc.left, y);
    LineTo(hdc, rc.left, rc.bottom - 1); LineTo(hdc, rc.right - 1, rc.bottom - 1);
    LineTo(hdc, rc.right - 1, y);      LineTo(hdc, x1, y);
    SelectObject(hdc, oldp);
    DeleteObject(pen);

    if (n) {
        tr.left = x0; tr.top = rc.top; tr.right = x1; tr.bottom = rc.top + ts.cy;
        FillRect(hdc, &tr, g_brBg);
        tr.left += 2;
        SetBkMode(hdc, TRANSPARENT);
        SetTextColor(hdc, on ? CLR_FG : CLR_DIS);
        DrawTextW(hdc, text, -1, &tr, DT_LEFT | DT_SINGLELINE | DT_NOCLIP | (hide_prefix(h) ? DT_HIDEPREFIX : 0));
    }
    SelectObject(hdc, oldf);
}

/* a disabled text: the same text, grey - where Windows would emboss it */
static void paint_static(HWND h, HDC hdc) {
    RECT rc;
    LONG st = GetWindowLongW(h, GWL_STYLE);
    DWORD type = st & SS_TYPEMASK;
    WCHAR text[2048];
    UINT fl = DT_EXPANDTABS;
    HGDIOBJ oldf;

    GetClientRect(h, &rc);
    FillRect(hdc, &rc, g_brBg);
    oldf = text_font(h, hdc);
    GetWindowTextW(h, text, 2048);
    if (type == SS_CENTER) fl |= DT_CENTER;
    else if (type == SS_RIGHT) fl |= DT_RIGHT;
    if (type == SS_LEFTNOWORDWRAP || type == SS_SIMPLE || (st & SS_CENTERIMAGE)) fl |= DT_SINGLELINE;
    else fl |= DT_WORDBREAK;
    if (st & SS_CENTERIMAGE) fl |= DT_VCENTER;
    if (st & SS_NOPREFIX) fl |= DT_NOPREFIX; else if (hide_prefix(h)) fl |= DT_HIDEPREFIX;
    switch (st & SS_ELLIPSISMASK) {
    case SS_ENDELLIPSIS:  fl |= DT_END_ELLIPSIS; break;
    case SS_PATHELLIPSIS: fl |= DT_PATH_ELLIPSIS; break;
    case SS_WORDELLIPSIS: fl |= DT_WORD_ELLIPSIS; break;
    }
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, CLR_DIS);
    DrawTextW(hdc, text, -1, &rc, fl);
    SelectObject(hdc, oldf);
}

/* A list's column header. Windows' dark header (DarkMode_ItemsView) has a
   dark background but black text, and custom draw cannot change that, so
   it is painted whole here: the background, each column's text as the
   column aligns it, the dividers, the sort arrow, the column the mouse is
   over. Its theme stays on, for the mouse tracking. */
#define CLR_HDR_HOT  RGB(48, 48, 48)
#define CLR_HDR_DOWN RGB(26, 26, 26)
static void paint_header(HWND h, HDC out) {
    RECT rc, ir, tr;
    int n, i, hot = -1, down = 0, on = IsWindowEnabled(h);
    HDC hdc;
    HBITMAP bm;
    HGDIOBJ ob, oldf, oldp;
    HPEN pen;
    POINT pt;

    GetClientRect(h, &rc);
    if (rc.right <= 0 || rc.bottom <= 0) return;
    hdc = CreateCompatibleDC(out);
    bm = CreateCompatibleBitmap(out, rc.right, rc.bottom);
    ob = SelectObject(hdc, bm);
    FillRect(hdc, &rc, g_brBg);

    if (on && (GetWindowLongW(h, GWL_STYLE) & HDS_BUTTONS) && GetCursorPos(&pt) && ScreenToClient(h, &pt) && PtInRect(&rc, pt)) {
        HDHITTESTINFO ht;
        memset(&ht, 0, sizeof ht);
        ht.pt = pt;
        hot = (int)SendMessageW(h, HDM_HITTEST, 0, (LPARAM)&ht);
        if (!(ht.flags & HHT_ONHEADER)) hot = -1;
        down = GetCapture() == h && (GetKeyState(VK_LBUTTON) & 0x8000);
    }

    oldf = text_font(h, hdc);
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, on ? CLR_FG : CLR_DIS);
    pen = CreatePen(PS_SOLID, 1, CLR_LINE);
    oldp = SelectObject(hdc, pen);
    n = (int)SendMessageW(h, HDM_GETITEMCOUNT, 0, 0);
    for (i = 0; i < n; i++) {
        HDITEMW it;
        WCHAR text[260];
        UINT fl = DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX;
        if (!SendMessageW(h, HDM_GETITEMRECT, i, (LPARAM)&ir) || ir.right <= ir.left) continue;
        if (i == hot) {
            HBRUSH b = CreateSolidBrush(down ? CLR_HDR_DOWN : CLR_HDR_HOT);
            FillRect(hdc, &ir, b);
            DeleteObject(b);
        }
        memset(&it, 0, sizeof it);
        text[0] = 0;
        it.mask = HDI_TEXT | HDI_FORMAT;
        it.pszText = text;
        it.cchTextMax = 260;
        SendMessageW(h, HDM_GETITEMW, i, (LPARAM)&it);
        text[259] = 0;
        if (it.fmt & (HDF_SORTUP | HDF_SORTDOWN)) {
            /* above the text, in the middle, as Windows puts it */
            POINT a[3];
            HBRUSH b = CreateSolidBrush(CLR_DIS);
            HGDIOBJ obr = SelectObject(hdc, b), onp = SelectObject(hdc, GetStockObject(NULL_PEN));
            int cx = (ir.left + ir.right) / 2, top = ir.top + 1;
            if (it.fmt & HDF_SORTUP) { a[0].x = cx - 4; a[0].y = top + 4; a[1].x = cx + 5; a[1].y = top + 4; a[2].x = cx; a[2].y = top - 1; }
            else { a[0].x = cx - 4; a[0].y = top; a[1].x = cx + 5; a[1].y = top; a[2].x = cx; a[2].y = top + 5; }
            Polygon(hdc, a, 3);
            SelectObject(hdc, obr); SelectObject(hdc, onp);
            DeleteObject(b);
        }
        switch (it.fmt & HDF_JUSTIFYMASK) {
        case HDF_RIGHT:  fl |= DT_RIGHT; break;
        case HDF_CENTER: fl |= DT_CENTER; break;
        default:         fl |= DT_LEFT; break;
        }
        tr = ir;
        tr.left += 6;
        tr.right -= 6;
        if (i == hot && down) OffsetRect(&tr, 1, 1);
        DrawTextW(hdc, text, -1, &tr, fl);
        MoveToEx(hdc, ir.right - 1, ir.top, NULL);
        LineTo(hdc, ir.right - 1, ir.bottom);
    }
    MoveToEx(hdc, rc.left, rc.bottom - 1, NULL);
    LineTo(hdc, rc.right, rc.bottom - 1);
    SelectObject(hdc, oldp);
    DeleteObject(pen);
    SelectObject(hdc, oldf);

    BitBlt(out, 0, 0, rc.right, rc.bottom, hdc, 0, 0, SRCCOPY);
    SelectObject(hdc, ob);
    DeleteObject(bm);
    DeleteDC(hdc);
}

#define K_CHECK  1
#define K_GROUP  2
#define K_STATIC 3
#define K_PUSH   4
#define K_HEADER 5   /* a list's column header */

static void paint_kind(HWND h, HDC hdc, DWORD_PTR kind) {
    if (kind == K_CHECK) paint_check(h, hdc);
    else if (kind == K_PUSH) paint_push(h, hdc);
    else if (kind == K_GROUP) paint_group(h, hdc);
    else if (kind == K_HEADER) paint_header(h, hdc);
    else paint_static(h, hdc);
}

static LRESULT CALLBACK CtlProc(HWND h, UINT m, WPARAM w, LPARAM l, UINT_PTR id, DWORD_PTR kind) {
    switch (m) {
    case WM_PAINT:
        if (kind == K_STATIC && IsWindowEnabled(h)) break;   /* enabled: WM_CTLCOLORSTATIC does it */
        {
            PAINTSTRUCT ps;
            HDC hdc = BeginPaint(h, &ps);
            paint_kind(h, hdc, kind);
            EndPaint(h, &ps);
        }
        return 0;
    case WM_PRINTCLIENT:
        if (kind == K_STATIC && IsWindowEnabled(h)) break;
        paint_kind(h, (HDC)w, kind);
        return 0;
    case WM_ERASEBKGND:
        if (kind == K_CHECK || kind == K_PUSH || kind == K_HEADER) return 1;
        break;
    case WM_ENABLE:
    case WM_SETTEXT:
    case WM_UPDATEUISTATE:
    case WM_SETFOCUS:
    case WM_KILLFOCUS:
    case WM_LBUTTONUP:
    case WM_CAPTURECHANGED:
    case BM_SETCHECK:
    case BM_SETSTATE:
    case BM_SETSTYLE: {
        /* the control may draw itself here, outside WM_PAINT: paint over it */
        LRESULT r = DefSubclassProc(h, m, w, l);
        InvalidateRect(h, NULL, kind == K_STATIC || kind == K_GROUP);
        return r;
    }
    case WM_NCDESTROY:
        RemoveWindowSubclass(h, CtlProc, id);
        break;
    }
    return DefSubclassProc(h, m, w, l);
}

static void paint_here(HWND c, int dark, DWORD_PTR kind) {
    if (dark) SetWindowSubclass(c, CtlProc, CHILD_ID, kind);
    else RemoveWindowSubclass(c, CtlProc, CHILD_ID);
}

/* ---------- each control ---------- */
static BOOL CALLBACK EnumChild(HWND c, LPARAM lp) {
    int dark = (int)lp;
    char cls[64];
    LONG st;
    GetClassNameA(c, cls, sizeof cls);

    if (is_dcx(c)) {
        /* DCX's own: the scroll bars of its lists, and the text's font (their
           headers are not DCX's, and are painted below like any other) */
        if (!lstrcmpiA(cls, "SysListView32") || !lstrcmpiA(cls, "SysTreeView32"))
            SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
        if (dark) twin_font(c, 1); else untwin_font(c, TRUE);
        return TRUE;
    }
    st = GetWindowLongA(c, GWL_STYLE);
    if (dark) twin_font(c, 0);
    else {
        /* the subclass off first, whichever branch below put it there and
           whether the control's class and style still match that branch:
           one left behind would outlive the theme, and the unload could not
           find it once the dialog is untracked */
        RemoveWindowSubclass(c, CtlProc, CHILD_ID);
        untwin_font(c, TRUE);
    }
    if (!lstrcmpiA(cls, "Button")) {
        DWORD t = st & BS_TYPEMASK;
        int pic = (st & (BS_ICON | BS_BITMAP)) != 0;
        if (t == BS_GROUPBOX) {
            SetWindowTheme(c, NULL, NULL);
            paint_here(c, dark, K_GROUP);
        } else if (t == BS_CHECKBOX || t == BS_AUTOCHECKBOX || t == BS_3STATE || t == BS_AUTO3STATE ||
                   t == BS_RADIOBUTTON || t == BS_AUTORADIOBUTTON) {
            /* the theme stays on: it is what tells the control the mouse is over it */
            SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
            if (pic) paint_here(c, 0, 0);
            else paint_here(c, dark, (st & BS_PUSHLIKE) ? K_PUSH : K_CHECK);
        } else if (t == BS_PUSHBUTTON || t == BS_DEFPUSHBUTTON) {
            SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
            if (pic) paint_here(c, 0, 0);
            else paint_here(c, dark, K_PUSH);
        } else if (t != BS_OWNERDRAW) {
            /* split buttons, command links: Windows' dark theme as it is */
            SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
        }
    } else if (!lstrcmpiA(cls, "Static")) {
        DWORD t = st & SS_TYPEMASK;
        if (t == SS_LEFT || t == SS_CENTER || t == SS_RIGHT || t == SS_LEFTNOWORDWRAP || t == SS_SIMPLE)
            paint_here(c, dark, K_STATIC);
    } else if (!lstrcmpiA(cls, "Edit")) {
        /* a multi-line one has scroll bars, which only Explorer's dark theme has */
        SetWindowTheme(c, dark ? ((st & ES_MULTILINE) ? L"DarkMode_Explorer" : L"DarkMode_CFD") : NULL, NULL);
    } else if (!lstrcmpiA(cls, "ComboBox")) {
        SetWindowTheme(c, dark ? L"DarkMode_CFD" : NULL, NULL);
    } else if (!lstrcmpiA(cls, "ListBox") || !lstrcmpiA(cls, "ScrollBar") ||
               !lstrcmpiA(cls, "SysTabControl32") || !lstrcmpiA(cls, "msctls_updown32")) {
        SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
    } else if (!lstrcmpiA(cls, "SysHeader32")) {
        SetWindowTheme(c, dark ? L"DarkMode_ItemsView" : NULL, NULL);
        paint_here(c, dark, K_HEADER);
    } else if (!lstrcmpiA(cls, "SysListView32")) {
        SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
        SendMessageA(c, LVM_SETBKCOLOR,     0, dark ? CLR_EDIT : GetSysColor(COLOR_WINDOW));
        SendMessageA(c, LVM_SETTEXTBKCOLOR, 0, dark ? CLR_EDIT : GetSysColor(COLOR_WINDOW));
        SendMessageA(c, LVM_SETTEXTCOLOR,   0, dark ? CLR_FG   : GetSysColor(COLOR_WINDOWTEXT));
    } else if (!lstrcmpiA(cls, "SysTreeView32")) {
        SetWindowTheme(c, dark ? L"DarkMode_Explorer" : NULL, NULL);
        SendMessageA(c, TVM_SETBKCOLOR,   0, dark ? (LPARAM)CLR_EDIT : (LPARAM)-1);
        SendMessageA(c, TVM_SETTEXTCOLOR, 0, dark ? (LPARAM)CLR_FG   : (LPARAM)-1);
    }
    InvalidateRect(c, NULL, TRUE);
    return TRUE;
}

/* every control's subclass off (an unload) */
static BOOL CALLBACK UnpaintChild(HWND c, LPARAM lp) {
    RemoveWindowSubclass(c, CtlProc, CHILD_ID);
    untwin_font(c, TRUE);
    return TRUE;
}

/* NULL when done; otherwise the answer for the script, and the dialog was
   left as it was */
static const char *apply(HWND dlg, int dark, DWORD flags) {
    BOOL v = dark ? TRUE : FALSE;
    fnDwmSetWindowAttribute dwm = dwm_set_attribute();
    if (dark) {
        if (!track(dlg, 1)) return "ERR no room for another dark dialog";
        if (!g_brBg)   g_brBg   = CreateSolidBrush(CLR_BG);
        if (!g_brEdit) g_brEdit = CreateSolidBrush(CLR_EDIT);
        if (!SetWindowSubclass(dlg, DlgProc, SUBCLASS_ID, (DWORD_PTR)flags)) {
            track(dlg, 0);
            if (!any_tracked()) free_brushes();
            return "ERR cannot subclass the dialog";
        }
    } else {
        RemoveWindowSubclass(dlg, DlgProc, SUBCLASS_ID);
        track(dlg, 0);
        if (!any_tracked()) free_brushes();   /* the last dark dialog */
    }

    if (dwm && FAILED(dwm(dlg, DWMWA_USE_IMMERSIVE_DARK_MODE, &v, sizeof v)))
        dwm(dlg, 19, &v, sizeof v); /* Windows 10 before 2004 */

    EnumChildWindows(dlg, EnumChild, (LPARAM)dark);
    SetWindowPos(dlg, NULL, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED);
    RedrawWindow(dlg, NULL, NULL, RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_FRAME);
    return NULL;
}

/* ---------- menu icons ---------- */
#define MAX_ICONS 128
static HINSTANCE g_hinst;
static HHOOK   g_hook;              /* on mIRC's thread, while the icons are on */
static HMODULE g_icons;             /* the icon file, loaded as data only */
static HBITMAP g_bmp[MAX_ICONS];    /* icon N-1 as a menu bitmap, made on first use */

static void free_bitmaps(void) {
    int i;
    for (i = 0; i < MAX_ICONS; i++) if (g_bmp[i]) { DeleteObject(g_bmp[i]); g_bmp[i] = NULL; }
}

/* An icon as a 32-bit top-down bitmap with premultiplied alpha - what a
   menu draws with its transparency (hbmpItem, Windows Vista and later). */
static HBITMAP icon_to_bitmap(HICON hi, int cx, int cy) {
    ICONINFO ii;
    BITMAPINFO bi;
    HBITMAP out;
    void *bits = NULL;
    HDC dc;
    if (!GetIconInfo(hi, &ii)) return NULL;
    memset(&bi, 0, sizeof bi);
    bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bi.bmiHeader.biWidth = cx;
    bi.bmiHeader.biHeight = -cy;
    bi.bmiHeader.biPlanes = 1;
    bi.bmiHeader.biBitCount = 32;
    bi.bmiHeader.biCompression = BI_RGB;
    dc = GetDC(NULL);
    out = CreateDIBSection(dc, &bi, DIB_RGB_COLORS, &bits, NULL, 0);
    if (out && bits) {
        BITMAP bm;
        int ok = 0;
        memset(bits, 0, (size_t)cx * cy * 4);
        if (ii.hbmColor && GetObject(ii.hbmColor, sizeof bm, &bm) && bm.bmWidth == cx && bm.bmHeight == cy && bm.bmBitsPixel == 32)
            ok = GetDIBits(dc, ii.hbmColor, 0, cy, bits, &bi, DIB_RGB_COLORS) == cy;
        if (ok) {
            BYTE *px = (BYTE *)bits;
            int i, n = cx * cy, alpha = 0;
            for (i = 0; i < n; i++) if (px[i * 4 + 3]) { alpha = 1; break; }
            for (i = 0; i < n; i++) {
                BYTE a = alpha ? px[i * 4 + 3] : 255;
                px[i * 4 + 0] = (BYTE)(px[i * 4 + 0] * a / 255);
                px[i * 4 + 1] = (BYTE)(px[i * 4 + 1] * a / 255);
                px[i * 4 + 2] = (BYTE)(px[i * 4 + 2] * a / 255);
                px[i * 4 + 3] = a;
            }
        } else {
            /* not a 32-bit icon: let Windows draw it */
            HDC mem = CreateCompatibleDC(dc);
            HGDIOBJ old = SelectObject(mem, out);
            DrawIconEx(mem, 0, 0, hi, cx, cy, 0, NULL, DI_NORMAL);
            SelectObject(mem, old);
            DeleteDC(mem);
        }
    }
    ReleaseDC(NULL, dc);
    if (ii.hbmColor) DeleteObject(ii.hbmColor);
    if (ii.hbmMask) DeleteObject(ii.hbmMask);
    return out;
}

/* n counted from 1, as the script counts them */
static HBITMAP menu_bitmap(int n) {
    int cx, cy;
    HICON hi;
    if (!g_icons || n < 1 || n > MAX_ICONS) return NULL;
    if (g_bmp[n - 1]) return g_bmp[n - 1];
    cx = GetSystemMetrics(SM_CXSMICON);
    cy = GetSystemMetrics(SM_CYSMICON);
    hi = (HICON)LoadImageW(g_icons, MAKEINTRESOURCEW(n), IMAGE_ICON, cx, cy, 0);
    if (!hi) return NULL;
    g_bmp[n - 1] = icon_to_bitmap(hi, cx, cy);
    DestroyIcon(hi);
    return g_bmp[n - 1];
}

/* "<N>" then $chr(11) at the start of an item's text (spaces allowed around
   them, as mIRC may leave): N, and where the text itself begins */
static int parse_marker(const WCHAR *t, int *num, const WCHAR **rest) {
    const WCHAR *p = t;
    int n = 0, digits = 0;
    while (*p == L' ') p++;
    while (*p >= L'0' && *p <= L'9' && digits < 4) { n = n * 10 + (*p - L'0'); p++; digits++; }
    if (!digits) return 0;
    while (*p == L' ') p++;
    if (*p != 0x0B) return 0;
    p++;
    while (*p == L' ') p++;
    *num = n;
    *rest = p;
    return 1;
}

static void mark_menu(HMENU hm) {
    int i, n = GetMenuItemCount(hm);
    WCHAR buf[1024];
    for (i = 0; i < n; i++) {
        MENUITEMINFOW mii;
        const WCHAR *rest;
        int num;
        buf[0] = 0;   /* not the previous item's text, should this one have none */
        memset(&mii, 0, sizeof mii);
        mii.cbSize = sizeof mii;
        mii.fMask = MIIM_FTYPE | MIIM_STRING;
        mii.dwTypeData = buf;
        mii.cch = (UINT)(sizeof buf / sizeof buf[0]) - 1;
        if (!GetMenuItemInfoW(hm, (UINT)i, TRUE, &mii)) continue;
        if (mii.fType & (MFT_OWNERDRAW | MFT_SEPARATOR | MFT_BITMAP)) continue;
        if (!mii.dwTypeData || !parse_marker(buf, &num, &rest)) continue;
        memset(&mii, 0, sizeof mii);
        mii.cbSize = sizeof mii;
        mii.fMask = MIIM_STRING;
        mii.dwTypeData = (LPWSTR)rest;
        if (menu_bitmap(num)) { mii.fMask |= MIIM_BITMAP; mii.hbmpItem = menu_bitmap(num); }
        SetMenuItemInfoW(hm, (UINT)i, TRUE, &mii);
    }
}

/* after the owner has handled WM_INITMENUPOPUP: the menu is filled, and only
   the marked items are touched (not the window menu, HIWORD(lParam)) */
static LRESULT CALLBACK RetProc(int code, WPARAM w, LPARAM l) {
    if (code == HC_ACTION) {
        const CWPRETSTRUCT *c = (const CWPRETSTRUCT *)l;
        if (c->message == WM_INITMENUPOPUP && !HIWORD(c->lParam) && g_icons) mark_menu((HMENU)c->wParam);
    }
    return CallNextHookEx(g_hook, code, w, l);
}

static void menus_off(void) {
    if (g_hook) { UnhookWindowsHookEx(g_hook); g_hook = NULL; }
    free_bitmaps();
    if (g_icons) { FreeLibrary(g_icons); g_icons = NULL; }
}

/* ---------- mIRC's entry points ---------- */
__declspec(dllexport) void __stdcall LoadDll(LOADINFO *li) { li->mKeep = TRUE; li->mUnicode = FALSE; }

/* mTimeout 1: unused for ten minutes - stay. Otherwise (/dll -u, mIRC exiting):
   every subclass is taken off first, so no window is left calling into a
   DLL that is gone. */
__declspec(dllexport) int __stdcall UnloadDll(int mTimeout) {
    int i;
    if (mTimeout == 1) return 0;
    for (i = 0; i < MAX_DLGS; i++)
        if (g_dlgs[i]) {
            if (IsWindow(g_dlgs[i])) {
                EnumChildWindows(g_dlgs[i], UnpaintChild, 0);
                RemoveWindowSubclass(g_dlgs[i], DlgProc, SUBCLASS_ID);
            }
            g_dlgs[i] = NULL;
        }
    menus_off();
    free_fonts();
    free_brushes();
    return 1;
}

#define MIRC_PROC(name) __declspec(dllexport) int __stdcall name(HWND mWnd, HWND aWnd, char *data, char *parms, BOOL show, BOOL nopause)

/* data = "<hwnd> <mode> [flags]" */
MIRC_PROC(ApplyTheme) {
    char *p;
    unsigned long hv = strtoul(data, &p, 10);
    long mode = strtol(p, &p, 10);
    DWORD flags = 0;
    HWND dlg = (HWND)(ULONG_PTR)hv;
    const char *err;
    pin_module();
    while (*p) { if (*p == 'x' || *p == 'X') flags |= F_DCX; p++; }
    if (!hv || !IsWindow(dlg)) { lstrcpyA(data, "ERR bad hwnd"); return 3; }
    /* a subclass runs on the window's thread: another thread's window is refused */
    if (GetWindowThreadProcessId(dlg, NULL) != GetCurrentThreadId()) { lstrcpyA(data, "ERR not this thread's window"); return 3; }
    err = apply(dlg, mode ? 1 : 0, flags);
    lstrcpyA(data, err ? err : "OK");
    return 3;
}

/* data = the icon file's path, or "off". Called on mIRC's main thread. */
MIRC_PROC(MenuIcons) {
    WCHAR path[MAX_PATH * 2];
    HMODULE m;
    pin_module();
    if (!lstrcmpiA(data, "off")) { menus_off(); lstrcpyA(data, "OK"); return 3; }
    /* mIRC hands a non-Unicode DLL UTF-8, so a script folder with letters
       outside the ANSI code page reads right that way; a path that is not
       valid UTF-8 is read in the ANSI code page instead */
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, data, -1, path, MAX_PATH * 2) &&
        !MultiByteToWideChar(CP_ACP, 0, data, -1, path, MAX_PATH * 2)) { lstrcpyA(data, "ERR bad path"); return 3; }
    m = LoadLibraryExW(path, NULL, LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE);
    if (!m) { lstrcpyA(data, "ERR cannot read the icon file"); return 3; }
    free_bitmaps();
    if (g_icons) FreeLibrary(g_icons);
    g_icons = m;
    if (!g_hook) g_hook = SetWindowsHookExW(WH_CALLWNDPROCRET, RetProc, g_hinst, GetCurrentThreadId());
    if (!g_hook) { FreeLibrary(g_icons); g_icons = NULL; lstrcpyA(data, "ERR no menu hook"); return 3; }
    lstrcpyA(data, "OK");
    return 3;
}

MIRC_PROC(Version) {
    lstrcpyA(data, DCCORE_UI_VERSION);
    return 3;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD why, LPVOID r) {
    if (why == DLL_PROCESS_ATTACH) g_hinst = h;
    return TRUE;
}
