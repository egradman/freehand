"""Pie menu overlay: a GTK4 layer-shell surface that draws what freehand tells it.

Runs under the system Python (needs PyGObject, pycairo, gtk4-layer-shell) with
libgtk4-layer-shell preloaded; freehand starts it and writes JSON lines to stdin:

  {"cmd": "show", "title": str, "items": [{"label", "content", "markup"}]}
  {"cmd": "items", "items": [...]}            replace items (e.g. source loaded)
  {"cmd": "hover", "index": int|null, "thumb": [x, y]|null}
  {"cmd": "choose", "index": int}             flash, then hide
  {"cmd": "hide"}

It never takes keyboard focus or pointer input, so the app underneath keeps both.
Imports only the stdlib, gi and cairo: it must not import freehand.
"""

import json
import math
import os
import sys
import tomllib
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
import cairo  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as Layer  # noqa: E402

THEME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omarchy/current/theme/colors.toml"
DEFAULT_COLORS = {
    "background": "#1a1b26", "lighter_background": "#24283b", "selection": "#292e42",
    "foreground": "#a9b1d6", "bright_foreground": "#c0caf5", "dark_foreground": "#565f89",
    "accent": "#7aa2f7", "muted": "#414868",
}
FONT = os.environ.get("FREEHAND_PIE_FONT", "sans")


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def load_colors():
    c = dict(DEFAULT_COLORS)
    try:
        c.update({k: v for k, v in tomllib.loads(THEME.read_text()).items() if isinstance(v, str) and v.startswith("#")})
    except (OSError, ValueError):
        pass
    return {k: hex_rgb(v) for k, v in c.items()}


def compass_point(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def slice_path(cr, cx, cy, outer, inner, start, end, gap):
    """Inset both radial sides by gap/2 pixels, yielding parallel gap edges."""
    outer_inset = math.asin(gap / (2 * outer))
    inner_inset = math.asin(gap / (2 * inner))
    cr.new_path()
    cr.arc(cx, cy, outer, start + outer_inset, end - outer_inset)
    cr.arc_negative(cx, cy, inner, end - inner_inset, start + inner_inset)
    cr.close_path()


class PieWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app)
        Layer.init_for_window(self)
        Layer.set_namespace(self, "freehand-pie")
        Layer.set_layer(self, Layer.Layer.OVERLAY)
        Layer.set_keyboard_mode(self, Layer.KeyboardMode.NONE)
        Layer.set_exclusive_zone(self, -1)
        for edge in (Layer.Edge.TOP, Layer.Edge.BOTTOM, Layer.Edge.LEFT, Layer.Edge.RIGHT):
            Layer.set_anchor(self, edge, True)

        css = Gtk.CssProvider()
        css.load_from_data(b"window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self.draw)
        self.set_child(self.area)
        self.connect("realize", self.on_realize)

        self.title, self.items = "", []
        self.loading, self.empty_text = False, "Empty"
        self.highlight, self.thumb, self.flash = None, None, False
        self.dwell_index, self.dwell_progress = None, 0
        self.colors = load_colors()

    def on_realize(self, *_):
        surface = self.get_surface()
        if surface:
            surface.set_input_region(cairo.Region())  # click-through

    # -- commands ---------------------------------------------------------------

    def handle(self, msg):
        cmd = msg.get("cmd")
        if cmd == "show":
            self.colors = load_colors()
            self.title = msg.get("title", "")
            self.items = msg.get("items", [])
            self.loading = msg.get("loading", False)
            self.empty_text = msg.get("empty_text", "Empty")
            self.highlight, self.thumb, self.flash = None, None, False
            self.dwell_index, self.dwell_progress = None, 0
            self.set_visible(True)
        elif cmd == "items":
            self.items = msg.get("items", [])
            self.loading = False
            if self.highlight is not None and self.highlight >= len(self.items):
                self.highlight = None
        elif cmd == "hover":
            self.highlight = msg.get("index")
            t = msg.get("thumb")
            self.thumb = tuple(t) if t else None
        elif cmd == "dwell":
            self.dwell_index = msg.get("index")
            self.dwell_progress = max(0, min(1, msg.get("progress", 0)))
        elif cmd == "choose":
            self.highlight, self.flash = msg.get("index"), True
            GLib.timeout_add(140, self._hide)
        elif cmd == "hide":
            self._hide()
        self.area.queue_draw()

    def _hide(self):
        self.flash = False
        self.set_visible(False)
        return False

    # -- drawing ----------------------------------------------------------------

    def rgba(self, cr, name, a=1.0):
        r, g, b = self.colors[name]
        cr.set_source_rgba(r, g, b, a)

    def text(self, cr, s, x, y, width, height=None, size=13, bold=False, markup=False,
             align=Pango.Alignment.LEFT, color="foreground"):
        layout = PangoCairo.create_layout(cr)
        fd = Pango.FontDescription.from_string(f"{FONT} {'Bold ' if bold else ''}{size}")
        layout.set_font_description(fd)
        layout.set_width(int(width * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_alignment(align)
        if height is not None:
            layout.set_height(int(height * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
        if markup:
            try:
                layout.set_markup(s, -1)
            except GLib.Error:
                layout.set_text(s, -1)
        else:
            layout.set_text(s, -1)
        _, h = layout.get_pixel_size()
        self.rgba(cr, color)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return h

    def draw(self, area, cr, w, h):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        self.rgba(cr, "background", 0.6)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        n = len(self.items)
        if n and all(i is None for i in self.items):
            n = 0
        R = min(h * 0.44, w * 0.44)
        cx, cy = w / 2, h / 2
        inner = R * 0.28
        scale = R / 300  # text sizes were tuned for a 300 px pie

        if n == 0:
            self._disc(cr, cx, cy, R)
        else:
            self._slices(cr, cx, cy, R, inner, n, scale)
        tip = None
        if self.thumb:
            # A full-length swipe reaches just past the centre disc, short of the labels.
            reach = inner + (R - inner) * 0.18
            tip = cx + self.thumb[0] * reach, cy + self.thumb[1] * reach
            cr.new_path()
            cr.move_to(cx, cy)
            cr.line_to(*tip)
            self.rgba(cr, "bright_foreground", 0.6)
            cr.set_line_width(3 * scale)
            cr.stroke()
        self._centre(cr, cx, cy, inner, scale, empty=n == 0)
        if tip:
            cr.new_path()
            cr.arc(*tip, 9 * scale, 0, 2 * math.pi)
            self.rgba(cr, "bright_foreground", 0.9)
            cr.fill()

    def _disc(self, cr, cx, cy, R):
        cr.new_path()
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        self.rgba(cr, "lighter_background", 0.95)
        cr.fill()

    def _measure(self, cr, s, width, size, bold=False, markup=False):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(f"{FONT} {'Bold ' if bold else ''}{size}"))
        layout.set_width(int(width * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        if markup:
            try:
                layout.set_markup(s, -1)
            except GLib.Error:
                layout.set_text(s, -1)
        else:
            layout.set_text(s, -1)
        return layout.get_pixel_size()[1]

    def _slices(self, cr, cx, cy, R, inner, n, scale):
        width = 360 / n
        gap = 6 * scale if n > 1 else 0
        ring_inner = inner + 4 * scale
        text_inset = math.degrees(math.asin(gap / (2 * ring_inner)))
        label_size = round((12 if n <= 8 else 11) * scale)
        body_size = round(9 * scale)
        for i, item in enumerate(self.items):
            a0 = math.radians(i * width - width / 2 - 90)
            a1 = math.radians(i * width + width / 2 - 90)
            hot = i == self.highlight and item is not None
            slice_path(cr, cx, cy, R, ring_inner, a0, a1, gap)
            if item is None:  # empty slot
                self.rgba(cr, "background", 0.75)
                cr.fill_preserve()
                self.rgba(cr, "muted", 0.5)
                cr.set_line_width(1)
                cr.stroke()
                continue
            if hot:
                self.rgba(cr, "accent", 1.0 if self.flash else 0.85)
            else:
                tint = item.get("tint")
                if tint:
                    base = self.colors["lighter_background"]
                    color = hex_rgb(tint)
                    cr.set_source_rgba(*(a * .65 + b * .35 for a, b in zip(base, color)), 1.0)
                else:
                    self.rgba(cr, "lighter_background", 1.0)
            cr.fill_preserve()
            self.rgba(cr, "muted", 0.9)
            cr.set_line_width(1)
            cr.stroke()

            if hot and i == self.dwell_index and self.dwell_progress > 0:
                # Fill the highlighted wedge outward as the dwell completes.
                fill_r = ring_inner + (R - ring_inner) * self.dwell_progress
                slice_path(cr, cx, cy, fill_r, ring_inner, a0, a1, gap)
                self.rgba(cr, "bright_foreground", 0.45)
                cr.fill()

            # Text box: the biggest upright rectangle that fits inside the wedge.
            lx, ly, bw, bh = self._fit_box(cx, cy, R, inner, i * width, width / 2 - text_inset, scale)
            label, content = item.get("label", ""), item.get("content", "")
            if item.get("submenu"):
                content = (content + "\nHold for options").strip()
            markup = item.get("markup", False)
            lh = min(self._measure(cr, label, bw, label_size, bold=True), bh * (0.6 if content else 1))
            ch = min(self._measure(cr, content, bw, body_size, markup=markup), bh - lh - 6) if content else 0
            y = ly - (lh + (ch + 6 if ch > 0 else 0)) / 2
            fg = "background" if hot else "bright_foreground"
            y += self.text(cr, label, lx - bw / 2, y, bw, height=lh, size=label_size, bold=True,
                           align=Pango.Alignment.CENTER, color=fg) + 6
            if ch > 0:
                self.text(cr, content, lx - bw / 2, y, bw, height=ch, size=body_size, markup=markup,
                          align=Pango.Alignment.CENTER, color="background" if hot else "foreground")

    @staticmethod
    def _fit_box(cx, cy, R, inner, deg, half, scale):
        """-> (centre x, centre y, width, height) of a text box inside the wedge at `deg`."""
        pad = 10 * scale
        r_lo, r_hi = inner + pad, R - pad
        half = math.radians(min(half, 89))

        def inside(px, py):
            dx, dy = px - cx, py - cy
            r = math.hypot(dx, dy)
            if not r_lo <= r <= r_hi:
                return False
            off = (math.atan2(dx, -dy) - math.radians(deg) + math.pi) % (2 * math.pi) - math.pi
            return abs(off) * r <= half * r - pad  # stay `pad` px clear of the side edges

        best = None
        for frac in (0.45, 0.5, 0.55, 0.6, 0.65):
            mid = inner + (R - inner) * frac
            x, y = compass_point(cx, cy, mid, deg)
            for aspect in (1.6, 1.25, 1.0, 0.8):  # width / height
                h = (R - inner) * 0.9
                while h > 8:
                    w = h * aspect
                    pts = [(x + sx * w / 2, y + sy * h / 2)
                           for sx in (-1, 0, 1) for sy in (-1, 0, 1) if sx or sy]
                    if all(inside(*pt) for pt in pts):
                        break
                    h *= 0.95
                if best is None or w * h > best[2] * best[3]:
                    best = (x, y, w, h)
        return best

    def _centre(self, cr, cx, cy, inner, scale, empty=False):
        cr.new_path()
        cr.arc(cx, cy, inner, 0, 2 * math.pi)
        self.rgba(cr, "background", 0.97)
        cr.fill_preserve()
        self.rgba(cr, "accent", 0.7)
        cr.set_line_width(2)
        cr.stroke()
        label = ("Loading…" if self.loading else self.empty_text) if empty else self.title
        if (not empty and self.highlight is not None and self.highlight < len(self.items)
                and self.items[self.highlight]):
            label = self.items[self.highlight].get("label", "")
        size = round(12 * scale)
        tw = inner * 1.6
        th = min(self._measure(cr, label, tw, size, bold=True), inner * 1.2)
        self.text(cr, label, cx - tw / 2, cy - th / 2, tw, height=th, size=size, bold=True,
                  align=Pango.Alignment.CENTER, color="bright_foreground")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=None)
        self.win = None
        self.buf = b""

    def do_activate(self):
        self.hold()  # stay alive while hidden
        self.win = PieWindow(self)
        GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.PRIORITY_HIGH,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP, self.on_stdin)

    def on_stdin(self, channel, cond):
        chunk = os.read(sys.stdin.fileno(), 1 << 20)
        if not chunk:
            self.quit()
            return False
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            try:
                self.win.handle(json.loads(line))
            except Exception as e:  # never die on one bad message
                print(f"pie_ui: {e}", file=sys.stderr, flush=True)
        return True


if __name__ == "__main__":
    App().run([])
