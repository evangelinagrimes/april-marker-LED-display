"""
LED Matrix GUI
===============
Interactive control panel for the ESP32 LED matrix: load an image, pick
two-tone or full-color rendering, adjust colors/brightness with an instant
on-screen preview, and push it to the matrix either live as you drag a
slider or on demand with a Send button.

Run:
    python gui.py

Built on the same image_loader.py / renderer.py / matrix_link.py that
send_image.py (the scriptable CLI) uses -- render_rgb() in renderer.py is
the single function that feeds both the preview shown here and the actual
wire payload, so the preview cannot drift from what the panel shows.

Requires the RGB888 firmware (v2) -- see led_matrix_esp/led_matrix_esp.ino
and matrix_link.py's module docstring for the wire protocol. Connecting to
older grayscale-only firmware will show a warning that the board didn't
answer a ping.

--------------------------------------------------------------------------
Threading
--------------------------------------------------------------------------
One long-lived daemon worker thread owns the MatrixLink exclusively (it is
not thread-safe). The GUI thread never touches serial directly, and the
worker thread never touches Tk widgets directly -- they communicate only
through three queues:

  cmd_q   (GUI -> worker)  "connect"/"disconnect"/"quit" commands
  frame_q (GUI -> worker)  maxsize=1, latest-wins -- see _push_frame()
  evt_q   (worker -> GUI)  status/error/ack events, drained by _drain_events()
                           via root.after(), which is safe to call from the
                           GUI thread's own event loop (unlike calling it
                           from the worker thread, which is not safe)

frame_q is capped at 1 and drained-then-filled by the producer (the GUI
thread) because a ttk.Scale fires on every motion event -- a few seconds
of dragging is 100+ callbacks, and at ~78ms per round trip an unbounded
queue would mean several seconds of visible lag after you release the
mouse, all of it already-stale frames. A 60ms UI-side debounce (just under
that round trip) on top of the drain means at most one frame is ever in
flight and one queued, and the *last* slider value always wins.
--------------------------------------------------------------------------
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageTk

import aruco_gen
import image_loader
import matrix_link
import renderer
import status_window

if sys.platform == "win32":
    # Tk 8.6 isn't DPI-aware on its own, which blurs the preview on a scaled
    # display -- and the preview being crisp is the whole point of this UI.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

PREVIEW_SCALE = 24        # on-screen pixels per matrix pixel
SEND_DEBOUNCE_MS = 60     # just under the ~78ms serial round trip
EVENT_POLL_MS = 50        # how often the GUI thread drains evt_q
CMD_POLL_TIMEOUT_S = 0.05  # how often the worker thread checks cmd_q
WORKER_JOIN_TIMEOUT_S = 2.5
STATUS_POLL_MS = 1000     # how often the status window's worker/queue health fields refresh
HEARTBEAT_INTERVAL_S = 5.0  # idle-time ping cadence -- see _worker's heartbeat branch

IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
DEFAULT_IMAGE_PATH = os.path.join(IMAGES_DIR, "attempt1.txt")
# Separate from IMAGES_DIR (source images, checked into git) -- this is
# where "Save Image..." writes rendered output. Gitignored since it's a
# per-user scratch/export location, not project source material.
SAVED_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_images")

STATUS_COLORS = {"info": "#1a1a1a", "warning": "#a06000", "error": "#b00020"}
DOT_CONNECTED = "#2e7d32"
DOT_DISCONNECTED = "#888888"

# which -> (state attr, button attr, color-picker dialog title, reload the
# image source on change instead of just re-rendering). Secondary is the
# only entry needing a reload -- see _on_pick_color/_reload_or_rerender.
# Border has no entry of its own -- it always matches "on" (see renderer.py's
# border_color=None fallback to color_on).
_COLOR_PICKERS = {
    "on": ("color_on", "btn_color_on", "Primary", False),
    "secondary": ("secondary_color", "btn_secondary", "Secondary", True),
}


def _to_hex(rgb: tuple) -> str:
    return "#%02x%02x%02x" % rgb


class MatrixApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("LED Matrix Control")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Image/render state (GUI thread only)
        self.img: "image_loader.LoadedImage | None" = None
        self.rgb = None                # last-rendered (H, W, 3) uint8 array
        self.color_on = renderer.DEFAULTS.color_on   # also the border color -- see renderer.py
        self.secondary_color = (0, 0, 0)    # blank-pixel color -- transparent PNG areas and the two-tone off color
        self._current_image_path = None     # last successfully loaded path, for reload on secondary color change --
                                             # None if the current image (e.g. a just-generated, not-yet-saved ArUco
                                             # marker) has no backing file; see _reload_or_rerender/_on_generate_aruco
        self._current_image_name = None     # display/save-suggestion name -- set even when _current_image_path isn't
        self._raster_cache_path = None      # path the fields below were decoded from, or None
        self._raster_cache_im = None        # decoded RGBA PIL.Image for _raster_cache_path (raster sources only)
        self._raster_cache_size = None      # its (width, height) before resizing
        self._photo = None             # must stay referenced or PhotoImage goes blank
        self._canvas_img_id = None
        self._send_job = None          # after() id for the send debounce
        self._ports_cache = []         # [(device, description), ...] from the last refresh
        self._connect_target = None    # port passed to the worker's last "connect" cmd, for the status window

        # Connection state (GUI thread's mirror of the worker's real state --
        # the GUI thread never touches MatrixLink directly)
        self.connected = False
        self._transport = None  # status_window.TRANSPORT_SERIAL/_WIFI once connected, else None -- see _update_send_visibility

        # Worker thread + queues (see module docstring)
        self.cmd_q = queue.Queue()
        self.frame_q = queue.Queue(maxsize=1)
        self.evt_q = queue.Queue()
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)

        # tk variables backing the controls
        self.var_mode = tk.StringVar(value=renderer.TWO_TONE)
        self.var_brightness = tk.IntVar(value=renderer.DEFAULTS.brightness)
        self.var_invert = tk.BooleanVar(value=renderer.DEFAULTS.invert)
        self.var_border = tk.BooleanVar(value=renderer.DEFAULTS.border)
        self.var_live = tk.BooleanVar(value=True)
        self.var_aruco_dict = tk.StringVar(value=aruco_gen.DEFAULT_DICTIONARY)
        self.var_aruco_id = tk.StringVar(value="0")
        self.var_aruco_border = tk.StringVar(value="1")

        self._build_ui()
        self._auto_size_window()
        # After sizing, not before -- sizing the window around the full
        # layout (Send section included) means the window is already tall
        # enough for Send to reappear later without clipping anything below
        # it, the same class of bug _auto_size_window's docstring warns
        # about for a fixed size.
        self._update_send_visibility()
        self._build_status_window()
        self._on_refresh_ports()
        self._worker_thread.start()
        self.root.after(EVENT_POLL_MS, self._drain_events)
        self.root.after(STATUS_POLL_MS, self._poll_status_window_health)

        self._load_image_path(DEFAULT_IMAGE_PATH)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=0)
        outer.rowconfigure(0, weight=1)

        self._build_preview(outer)

        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="n", padx=(12, 0))
        self._build_connection(right)
        self._build_image(right)
        self._build_render(right)
        self._build_send(right)

        self._build_statusbar(outer)

    def _auto_size_window(self):
        """Size the window to whatever the control column actually needs,
        clamped to the screen, instead of a fixed size that silently clips
        controls as more get added -- a fixed 920x600 is what made the
        brightness slider look like it had vanished (it was pushed below
        the window's bottom edge, along with everything under it)."""
        self.root.update_idletasks()
        req_w = self.root.winfo_reqwidth()
        req_h = self.root.winfo_reqheight()
        margin = 80  # rough allowance for the title bar + taskbar
        height = min(req_h, self.root.winfo_screenheight() - margin)
        self.root.geometry(f"{req_w}x{height}")
        self.root.minsize(req_w, height)

    def _build_preview(self, parent):
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=0, sticky="n")

        size = image_loader.MATRIX_WIDTH * PREVIEW_SCALE
        self.canvas = tk.Canvas(frame, width=size, height=size, bg="black", highlightthickness=1,
                                 highlightbackground="#444")
        self.canvas.pack()

        self.lbl_image_path = ttk.Label(frame, text="(no image loaded)")
        self.lbl_image_path.pack(anchor="w", pady=(8, 0))
        self.lbl_image_info = ttk.Label(frame, text="", foreground="#666")
        self.lbl_image_info.pack(anchor="w")
        ttk.Label(frame, text="Logical view -- serpentine wiring is applied on the ESP32.",
                  foreground="#666", wraplength=size).pack(anchor="w", pady=(4, 0))

    def _build_connection(self, parent):
        box = ttk.LabelFrame(parent, text="Connection", padding=8)
        box.pack(fill="x", pady=(0, 8))
        box.columnconfigure(0, weight=1)

        self.combo_port = ttk.Combobox(box, state="readonly")
        self.combo_port.grid(row=0, column=0, sticky="ew")
        ttk.Button(box, text="Refresh", command=self._on_refresh_ports).grid(row=0, column=1, padx=(6, 0))

        self.btn_connect = ttk.Button(box, text="Connect", command=self._on_toggle_connection)
        self.btn_connect.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self.lbl_firmware = ttk.Label(box, text="", foreground="#666")
        self.lbl_firmware.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_image(self, parent):
        box = ttk.LabelFrame(parent, text="Image", padding=8)
        box.pack(fill="x", pady=(0, 8))
        ttk.Button(box, text="Open Image...", command=self._on_open_image).pack(fill="x")

        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=(10, 8))

        ttk.Label(box, text="ArUco Dictionary").pack(anchor="w")
        self.combo_aruco_dict = ttk.Combobox(
            box, state="readonly", textvariable=self.var_aruco_dict,
            values=sorted(aruco_gen.DICTIONARIES))
        self.combo_aruco_dict.pack(fill="x", pady=(0, 6))
        self.combo_aruco_dict.bind("<<ComboboxSelected>>", lambda _e: self._update_aruco_range_label())

        id_row = ttk.Frame(box)
        id_row.pack(fill="x")
        ttk.Label(id_row, text="Marker ID").pack(side="left")
        ttk.Entry(id_row, textvariable=self.var_aruco_id, width=8).pack(side="left", padx=(6, 12))
        ttk.Label(id_row, text="Border bits").pack(side="left")
        ttk.Entry(id_row, textvariable=self.var_aruco_border, width=6).pack(side="left", padx=(6, 0))

        self.lbl_aruco_range = ttk.Label(box, text="", foreground="#666")
        self.lbl_aruco_range.pack(anchor="w", pady=(2, 0))
        self._update_aruco_range_label()

        ttk.Button(box, text="Generate ArUco Marker", command=self._on_generate_aruco).pack(fill="x", pady=(6, 0))

        ttk.Separator(box, orient="horizontal").pack(fill="x", pady=(10, 8))
        ttk.Button(box, text="Save Image...", command=self._on_save_image).pack(fill="x")

    def _build_render(self, parent):
        box = ttk.LabelFrame(parent, text="Rendering", padding=8)
        box.pack(fill="x", pady=(0, 8))

        mode_row = ttk.Frame(box)
        mode_row.pack(fill="x")
        ttk.Radiobutton(mode_row, text="Two-tone", variable=self.var_mode,
                         value=renderer.TWO_TONE, command=self._on_mode_changed).pack(side="left")
        self.radio_full_color = ttk.Radiobutton(
            mode_row, text="Full color", variable=self.var_mode,
            value=renderer.FULL_COLOR, command=self._on_mode_changed)
        self.radio_full_color.pack(side="left", padx=(10, 0))

        self.btn_color_on = self._make_color_button(box, "on", "Primary Color", self.color_on)
        self.btn_color_on.pack(fill="x", pady=(8, 0))

        self.btn_secondary = self._make_color_button(
            box, "secondary", "Secondary Color", self.secondary_color, fg="white")
        self.btn_secondary.pack(fill="x", pady=(6, 0))

        ttk.Label(box, text="Brightness").pack(anchor="w", pady=(8, 0))
        ttk.Scale(box, from_=0, to=100, variable=self.var_brightness,
                  command=self._on_slider_changed).pack(fill="x")

        self.chk_invert = ttk.Checkbutton(box, text="Invert colors", variable=self.var_invert,
                                           command=self._on_settings_changed)
        self.chk_invert.pack(anchor="w", pady=(8, 0))

        # No color swatch of its own -- the border always matches Primary
        # Color (renderer.py resolves border_color=None to color_on).
        self.chk_border = ttk.Checkbutton(box, text="Border", variable=self.var_border,
                                           command=self._on_border_toggle)
        self.chk_border.pack(anchor="w", pady=(4, 0))

        ttk.Button(box, text="Reset to defaults", command=self._on_reset).pack(fill="x", pady=(8, 0))

    def _build_send(self, parent):
        box = ttk.LabelFrame(parent, text="Send", padding=8)
        box.pack(fill="x")
        self.send_frame = box  # shown/hidden by _update_send_visibility

        send_row = ttk.Frame(box)
        send_row.pack(fill="x")
        # Always clickable -- _on_send reports "not connected"/"no image" via
        # the status bar rather than the button just going dead, and this dot
        # is the always-visible complement: green means a click will actually
        # do something, gray means it won't.
        self.status_dot = tk.Canvas(send_row, width=14, height=14, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self._dot_id = self.status_dot.create_oval(2, 2, 12, 12, fill=DOT_DISCONNECTED, outline="")
        self.btn_send = ttk.Button(send_row, text="Send to Matrix", command=self._on_send)
        self.btn_send.pack(side="left", fill="x", expand=True)

        ttk.Checkbutton(box, text="Live update", variable=self.var_live).pack(anchor="w", pady=(6, 0))

    def _build_statusbar(self, parent):
        self.lbl_status = ttk.Label(parent, text="Ready.", relief="sunken", anchor="w", padding=(6, 3))
        self.lbl_status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _build_status_window(self):
        self.status_window = status_window.StatusWindow(self.root)
        # Place it beside the main window rather than stacked on top of it,
        # so both are visible at once without the user having to drag one
        # aside first -- the whole point of a status monitor is watching it
        # while you use the main controls.
        self.root.update_idletasks()
        x = self.root.winfo_x() + self.root.winfo_width() + 12
        y = self.root.winfo_y()
        self.status_window.win.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Rendering / preview
    # ------------------------------------------------------------------

    def _current_settings(self) -> renderer.RenderSettings:
        return renderer.RenderSettings(
            mode=self.var_mode.get(),
            # threshold/smooth aren't user-adjustable (see _build_render) --
            # every source image here is already pure black/white, so the
            # default hard threshold always lands cleanly on either side.
            threshold=renderer.DEFAULTS.threshold,
            color_on=self.color_on,
            color_off=self.secondary_color,
            smooth=renderer.DEFAULTS.smooth,
            brightness=int(round(self.var_brightness.get())),
            invert=self.var_invert.get(),
            border=self.var_border.get(),
            # border_color omitted -- renderer.py falls back to color_on.
        )

    def _on_slider_changed(self, _value):
        # ttk.Scale's command fires with a float string; round it back to an
        # int so the numeric value (and the label, if one is added later)
        # doesn't read like 127.94827586.
        self.var_brightness.set(int(round(self.var_brightness.get())))
        self._on_settings_changed()

    def _on_mode_changed(self):
        self._update_control_states()
        self._on_settings_changed()

    def _update_control_states(self):
        full_color = self.var_mode.get() == renderer.FULL_COLOR
        # Primary Color feeds two-tone rendering AND the border (border
        # always matches it -- see renderer.py), so it must stay enabled in
        # full-color mode whenever the border is on, even though the rest
        # of the two-tone controls above are disabled there. Invert (which
        # now swaps color_on/color_off rather than negating the image, see
        # renderer.py) only has any visible effect under that same
        # condition -- full-color pixels themselves don't come from
        # color_on/color_off at all.
        primary_enabled = (not full_color) or self.var_border.get()
        self.btn_color_on.configure(state=("normal" if primary_enabled else "disabled"))
        self.chk_invert.configure(state=("normal" if primary_enabled else "disabled"))
        # Secondary Color always matters (transparency compositing works in
        # both modes), so it's never gated on `mode` here.

        has_color = self.img is not None and self.img.rgb is not None
        self.radio_full_color.configure(state=("normal" if has_color else "disabled"))

    def _make_color_button(self, parent, which: str, text: str, color: tuple,
                            fg: str = None) -> tk.Button:
        """Build a swatch button wired to _on_pick_color(which). Only
        constructs the button -- callers still .pack() it themselves, since
        layout (fill/expand vs. a small fixed swatch) varies by call site."""
        kwargs = {"text": text, "bg": _to_hex(color), "command": lambda: self._on_pick_color(which)}
        if fg:
            kwargs["fg"] = fg
        return tk.Button(parent, **kwargs)

    def _on_border_toggle(self):
        # Toggling border can change whether Primary Color needs to be
        # enabled in full-color mode -- see _update_control_states.
        self._update_control_states()
        self._on_settings_changed()

    def _on_settings_changed(self, *_):
        if self.img is None:
            return
        self._rerender()
        self._update_preview()
        if self.var_live.get() and self.connected:
            self._schedule_send()

    def _rerender(self):
        try:
            self.rgb = renderer.render_rgb(self.img, self._current_settings())
        except ValueError:
            # Shouldn't normally happen -- the full-color radio is disabled
            # without color data -- but fail safe rather than crash.
            self.rgb = None

    def _update_preview(self):
        if self.rgb is None:
            return
        preview = renderer.to_preview_image(self.rgb, scale=PREVIEW_SCALE)
        self._photo = ImageTk.PhotoImage(preview)
        if self._canvas_img_id is None:
            self._canvas_img_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        else:
            self.canvas.itemconfig(self._canvas_img_id, image=self._photo)

    def _on_reset(self):
        self.var_mode.set(renderer.DEFAULTS.mode)
        self.var_brightness.set(renderer.DEFAULTS.brightness)
        self.var_invert.set(renderer.DEFAULTS.invert)
        self.var_border.set(renderer.DEFAULTS.border)
        self.color_on = renderer.DEFAULTS.color_on
        self.secondary_color = (0, 0, 0)
        self.btn_color_on.configure(bg=_to_hex(self.color_on))
        self.btn_secondary.configure(bg=_to_hex(self.secondary_color))
        self._update_control_states()
        # A reload (not just a re-render) since the secondary color may
        # have just changed -- see _reload_or_rerender.
        self._reload_or_rerender("Reset to defaults.")

    # ------------------------------------------------------------------
    # Image loading
    # ------------------------------------------------------------------

    def _on_open_image(self):
        path = filedialog.askopenfilename(
            title="Open Image",
            filetypes=[
                ("Hex image", "*.h *.hpp *.c *.inc *.txt"),
                ("Image", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                ("All files", "*.*"),
            ],
            parent=self.root,
        )
        if not path:
            return
        self._load_image_path(path)

    def _update_aruco_range_label(self):
        size = aruco_gen.dictionary_size(self.var_aruco_dict.get())
        self.lbl_aruco_range.configure(text=f"Valid IDs: 0-{size - 1}")

    def _on_generate_aruco(self):
        """Read the inline dictionary/ID/border fields, generate the
        marker, and load it into the preview/render pipeline the same way
        _on_open_image would -- but purely in memory. Nothing is written to
        disk here; that only happens if/when Save Image is pressed."""
        try:
            marker_id = int(self.var_aruco_id.get())
            border_bits = int(self.var_aruco_border.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Marker ID and border bits must be whole numbers.",
                                  parent=self.root)
            return
        dictionary = self.var_aruco_dict.get()
        try:
            gray = aruco_gen.generate_marker(dictionary, marker_id, border_bits=border_bits)
        except aruco_gen.ArucoGenError as e:
            messagebox.showerror("Could not generate marker", str(e), parent=self.root)
            return
        img = image_loader.LoadedImage(gray=gray, rgb=None, path=None,
                                        source_size=(gray.shape[1], gray.shape[0]))
        self._current_image_path = None  # nothing on disk -- see _reload_or_rerender
        self._current_image_name = f"aruco_{dictionary}_id{marker_id}.png"
        self._set_current_image(
            img, f"{self._current_image_name} (not saved)",
            f"Generated {dictionary} id {marker_id} -- press Save Image to keep it.")

    def _on_save_image(self):
        """Save the current preview (self.rgb -- full brightness, matching
        what's on screen, see renderer.py's module docstring) as a PNG. The
        dialog defaults into SAVED_IMAGES_DIR, but a user can still browse
        elsewhere -- IMAGES_DIR is for source material this project ships
        with, SAVED_IMAGES_DIR (gitignored) is a per-user scratch/export
        spot for whatever this renders."""
        if self.rgb is None:
            self._set_status("No image to save.", "error")
            return
        os.makedirs(SAVED_IMAGES_DIR, exist_ok=True)
        stem = os.path.splitext(self._current_image_name or "render")[0]
        path = filedialog.asksaveasfilename(
            title="Save Image",
            initialdir=SAVED_IMAGES_DIR,
            initialfile=f"{stem}_render.png",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        Image.fromarray(self.rgb, "RGB").save(path)
        self._set_status(f"Saved {os.path.basename(path)}.", "info")

    def _decode_and_composite(self, path: str) -> "image_loader.LoadedImage":
        """Like image_loader.load_image(), but caches the decoded raster
        source per path so repeated secondary-color changes on the same
        image (see _reload_or_rerender) recomposite in memory instead of
        re-reading and re-decoding the file from disk every time."""
        ext = os.path.splitext(path)[1].lower()
        if ext not in image_loader.RASTER_EXTS:
            return image_loader.load_image(path, background=self.secondary_color)
        if self._raster_cache_path != path:
            self._raster_cache_im, self._raster_cache_size = image_loader.decode_raster(path)
            self._raster_cache_path = path
        return image_loader.composite_raster(
            self._raster_cache_im, self.secondary_color, path, self._raster_cache_size)

    def _set_current_image(self, img: "image_loader.LoadedImage", display_name: str, status_msg: str):
        """Apply a decoded/generated LoadedImage as what's currently shown
        and sent -- shared by _load_image_path (a real file) and
        _on_generate_aruco (purely in-memory, no file involved)."""
        self.img = img
        self.lbl_image_path.configure(text=display_name)
        self.lbl_image_info.configure(text=f"{img.source_size[0]}x{img.source_size[1]} source")
        if img.rgb is None and self.var_mode.get() == renderer.FULL_COLOR:
            self.var_mode.set(renderer.TWO_TONE)
        self._update_control_states()
        self._on_settings_changed()
        self._set_status(status_msg, "info")

    def _load_image_path(self, path: str, status_msg: str = None):
        try:
            img = self._decode_and_composite(path)
        except image_loader.ImageLoadError as e:
            # Keep whatever was previously loaded -- a bad file shouldn't
            # blank out a perfectly good preview.
            messagebox.showerror("Could not load image", str(e), parent=self.root)
            self._set_status(f"Could not load {os.path.basename(path)}: {e}", "error")
            return

        self._current_image_path = path
        self._current_image_name = os.path.basename(path)
        self._set_current_image(img, self._current_image_name,
                                 status_msg or f"Loaded {self._current_image_name}.")

    def _reload_or_rerender(self, status_msg: str):
        """Secondary-color changes need to recomposite from the source
        file, not just re-render, whenever the current image has one --
        raster compositing needs the original alpha channel, which is gone
        once .img is cached as flattened RGB (_decode_and_composite's cache
        means this only re-decodes from disk the first time per path).

        But the current image might not have a backing file at all (an
        ArUco marker fresh out of _on_generate_aruco, not yet saved) -- for
        those there's nothing to recomposite (no alpha, no file), so just
        re-render in place instead; the secondary color already feeds
        render_rgb() live either way."""
        if self._current_image_path is not None:
            self._load_image_path(self._current_image_path, status_msg=status_msg)
        else:
            self._on_settings_changed()
            self._set_status(status_msg, "info")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _on_refresh_ports(self):
        self._ports_cache = matrix_link.list_ports()
        if self._ports_cache:
            self.combo_port["values"] = [f"{d} — {desc}" for d, desc in self._ports_cache]
            self.combo_port.current(0)
            self.btn_connect.configure(state="normal")
        else:
            self.combo_port["values"] = ["(no serial ports found)"]
            self.combo_port.current(0)
            self.btn_connect.configure(state="disabled")

    def _selected_port(self):
        idx = self.combo_port.current()
        if not self._ports_cache or idx < 0 or idx >= len(self._ports_cache):
            return None
        return self._ports_cache[idx][0]

    def _on_toggle_connection(self):
        if self.connected:
            self.cmd_q.put(("disconnect", None))
            return
        port = self._selected_port()
        if not port:
            self._set_status("No serial port selected -- plug in the ESP32 and hit Refresh.", "error")
            return
        self.btn_connect.configure(state="disabled")
        self._set_status(f"Connecting to {port}...", "info")
        self._connect_target = port
        self.status_window.set_connecting(port, matrix_link.BAUD_RATE)
        self.cmd_q.put(("connect", port))

    def _set_connected(self, connected: bool, info: str = ""):
        self.connected = connected
        self.btn_connect.configure(state="normal", text=("Disconnect" if connected else "Connect"))
        self.lbl_firmware.configure(text=info if connected else "")
        self.status_dot.itemconfig(self._dot_id, fill=(DOT_CONNECTED if connected else DOT_DISCONNECTED))

    def _update_send_visibility(self):
        """The Send section (manual "Send to Matrix" + Live update) only
        shows for a WiFi connection -- serial pushes automatically anyway
        (connect-time sync plus Live update, which stays effectively on
        with no visible toggle while serial-connected), so a manual send
        control adds nothing there. Since matrix_link.py only implements
        serial today, this section stays hidden until a WiFi transport
        exists and _transport actually gets set to TRANSPORT_WIFI."""
        if self._transport == status_window.TRANSPORT_WIFI:
            self.send_frame.pack(fill="x")
        else:
            self.send_frame.pack_forget()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _schedule_send(self):
        if self._send_job is not None:
            self.root.after_cancel(self._send_job)
        self._send_job = self.root.after(SEND_DEBOUNCE_MS, self._push_frame)

    def _push_frame(self, payload: bytes = None):
        """Queue a frame for the worker to send. With no argument, sends
        the current render (the debounced/live-update/Send-button path,
        gated on having something connected to send to); pass `payload`
        explicitly to queue something else regardless of that gate (see
        _on_close's blank-on-exit frame)."""
        self._send_job = None
        if payload is None:
            if self.rgb is None or not self.connected:
                return
            # Brightness is applied here, to the payload only -- self.rgb
            # (and the preview drawn from it) stays at full brightness so
            # dimming the LEDs doesn't also dim your ability to see what
            # you're sending. See renderer.py's module docstring.
            out = renderer.apply_brightness(self.rgb, int(round(self.var_brightness.get())))
            payload = renderer.to_payload(out)
        # Latest-wins: drop whatever's queued before adding the new one, so
        # the worker never sends a stale frame from mid-drag (see module
        # docstring).
        try:
            self.frame_q.get_nowait()
        except queue.Empty:
            pass
        self.frame_q.put_nowait(payload)

    def _on_send(self):
        if self.rgb is None:
            self._set_status("No image loaded.", "error")
            return
        if not self.connected:
            self._set_status("Not connected.", "error")
            return
        self._push_frame()

    # ------------------------------------------------------------------
    # Worker thread -- the only thing that ever touches MatrixLink
    # ------------------------------------------------------------------

    def _worker(self):
        link = None
        last_activity = 0.0  # time.monotonic() of the last successful send/ping -- drives the idle heartbeat below
        while True:
            try:
                cmd, arg = self.cmd_q.get(timeout=CMD_POLL_TIMEOUT_S)
            except queue.Empty:
                cmd, arg = None, None

            if cmd == "quit":
                if link is not None:
                    # Best-effort: send whatever's queued (the GUI queues a
                    # blank frame right before this command -- see
                    # _on_close) so the panel goes dark on exit instead of
                    # showing the last image forever. MatrixLink.close()
                    # does the send-then-close itself, swallowing failures
                    # the same way ping()'s timeout already is elsewhere.
                    try:
                        payload = self.frame_q.get_nowait()
                    except queue.Empty:
                        payload = None
                    link.close(final_payload=payload)
                return

            elif cmd == "connect":
                if link is not None:
                    link.close()
                    link = None
                try:
                    link = matrix_link.MatrixLink(arg)
                    link.open()
                    fw_id = link.ping()
                    if fw_id is None:
                        time.sleep(matrix_link.BOOT_SETTLE_S)
                        fw_id = link.ping()
                    last_activity = time.monotonic()
                    if fw_id is None:
                        self.evt_q.put(("connected", ""))
                        self.evt_q.put(("warning",
                                         "Port open, but the firmware didn't answer -- "
                                         "is it flashed with the RGB firmware?"))
                    else:
                        self.evt_q.put(("connected", fw_id))
                except matrix_link.MatrixLinkError as e:
                    link = None
                    self.evt_q.put(("error", str(e)))

            elif cmd == "disconnect":
                if link is not None:
                    link.close()
                    link = None
                self.evt_q.put(("disconnected", ""))

            if link is not None and link.is_open:
                try:
                    payload = self.frame_q.get_nowait()
                except queue.Empty:
                    payload = None

                if payload is not None:
                    try:
                        ack = link.send_rgb(payload)
                        last_activity = time.monotonic()
                        self.evt_q.put(("ack", ack or ""))
                    except matrix_link.MatrixLinkError as e:
                        link.close()
                        link = None
                        self.evt_q.put(("disconnected", str(e)))
                elif time.monotonic() - last_activity >= HEARTBEAT_INTERVAL_S:
                    # Nothing queued to send -- ping anyway so a silent USB
                    # unplug or a hung board shows up within one interval
                    # instead of only the next time a frame goes out. A
                    # missing reply alone isn't fatal (see MatrixLink.ping()
                    # -- the board may just be busy); only an actual I/O
                    # failure means the connection is really gone.
                    try:
                        fw_id = link.ping()
                        last_activity = time.monotonic()
                        self.evt_q.put(("heartbeat", fw_id or ""))
                    except matrix_link.MatrixLinkError as e:
                        link.close()
                        link = None
                        self.evt_q.put(("disconnected", str(e)))

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.evt_q.get_nowait()
                if kind == "connected":
                    self._set_connected(True, payload)
                    self._set_status(f"Connected. {payload}" if payload else "Connected.", "info")
                    self.status_window.set_connected(self._connect_target, matrix_link.BAUD_RATE, payload)
                    # Only serial exists today (see matrix_link.py) -- once
                    # a WiFi transport is added, whichever "connect" path
                    # created it should set TRANSPORT_WIFI instead so
                    # _update_send_visibility can actually show Send.
                    self._transport = status_window.TRANSPORT_SERIAL
                    self._update_send_visibility()
                    # Push whatever's currently rendered right away, rather
                    # than leaving the panel showing nothing (or a stale
                    # frame from before a reconnect) until the user changes
                    # a setting or hits Send -- independent of "Live update",
                    # which only governs pushes made *while* dragging a
                    # slider, not this one-shot sync-on-connect.
                    self._push_frame()
                elif kind == "disconnected":
                    self._set_connected(False)
                    self._transport = None
                    self._update_send_visibility()
                    if payload:
                        self._set_status(f"Disconnected: {payload}", "error")
                        self._on_refresh_ports()
                    else:
                        self._set_status("Disconnected.", "info")
                    self.status_window.set_disconnected(payload or None)
                elif kind == "error":
                    self._set_connected(False)
                    self._transport = None
                    self._update_send_visibility()
                    self._set_status(f"Connection failed: {payload}", "error")
                    self._on_refresh_ports()
                    self.status_window.connect_failed(payload)
                elif kind == "warning":
                    self._set_status(payload, "warning")
                    self.status_window.set_warning(payload)
                elif kind == "ack":
                    self._set_status(f"Sent. ESP32: {payload}" if payload else "Sent (no response).", "info")
                    self.status_window.record_ack(payload)
                elif kind == "heartbeat":
                    self.status_window.record_heartbeat(payload)
        except queue.Empty:
            pass
        self.root.after(EVENT_POLL_MS, self._drain_events)

    def _poll_status_window_health(self):
        # qsize() is inherently racy against the worker thread, but this is
        # a debug display, not a control-flow decision -- an occasional
        # off-by-one is fine, and it still catches the two things that
        # actually matter: the worker thread dying, and a frame sitting
        # queued longer than expected.
        self.status_window.set_worker_alive(self._worker_thread.is_alive())
        self.status_window.set_queue_depth(self.frame_q.qsize())
        self.root.after(STATUS_POLL_MS, self._poll_status_window_health)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _set_status(self, text: str, kind: str = "info"):
        self.lbl_status.configure(text=text, foreground=STATUS_COLORS.get(kind, "#1a1a1a"))

    def _on_pick_color(self, which: str):
        attr, btn_attr, title, reload_source = _COLOR_PICKERS[which]
        current = getattr(self, attr)
        _, hex_str = colorchooser.askcolor(
            color=_to_hex(current), parent=self.root, title=f"Choose {title} color")
        if hex_str is None:
            return  # cancelled
        rgb = tuple(int(hex_str[i:i + 2], 16) for i in (1, 3, 5))
        setattr(self, attr, rgb)
        getattr(self, btn_attr).configure(bg=hex_str)
        if reload_source:
            self._reload_or_rerender("Secondary color updated.")
        else:
            self._on_settings_changed()

    def _on_close(self):
        if self._send_job is not None:
            self.root.after_cancel(self._send_job)
        if self.connected:
            self._push_frame(renderer.blank_payload())
        self.cmd_q.put(("quit", None))
        self._worker_thread.join(timeout=WORKER_JOIN_TIMEOUT_S)
        self.root.destroy()


def main():
    root = tk.Tk()
    MatrixApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
