"""
Connection Status Monitor
==========================
A secondary Toplevel window that opens alongside the main GUI and mirrors
its connection state for debugging: which transport is in use, the
port/baud in use, the firmware banner from the last successful ping/ack,
running frame-send counters, and a timestamped event log so a flaky
connection leaves a trail instead of just its last symptom.

While connected but idle (no image changes to send), gui.py's worker
thread pings the board every few seconds and reports it here as a
heartbeat -- without that, "Connected" would just be whatever the last
real send happened to say, and could sit there stale long after a USB
unplug until the user did something that tried to send a frame.

This window owns no state of its own and never touches MatrixLink or the
GUI's queues directly -- MatrixApp calls the update_* methods below from
the same GUI-thread handlers that already update the main window
(_set_connected, _drain_events, _push_frame), so there's no additional
thread-safety surface here beyond what gui.py already has.
"""

import time
import tkinter as tk
from tkinter import ttk

# Only serial exists today (see matrix_link.py) -- TRANSPORT_WIFI is wired
# in so this field has somewhere to go if/when a WiFi transport is added,
# but nothing sets it yet.
TRANSPORT_SERIAL = "Serial"
TRANSPORT_WIFI = "WiFi"

MAX_LOG_LINES = 300

_LOG_COLORS = {"info": "#1a1a1a", "warning": "#a06000", "error": "#b00020"}
DOT_CONNECTED = "#2e7d32"
DOT_DISCONNECTED = "#888888"

_FIELDS = [
    ("transport", "Transport"),
    ("port", "Port"),
    ("baud", "Baud rate"),
    ("firmware", "Firmware"),
    ("uptime", "Uptime"),
    ("last_heartbeat", "Last heartbeat"),
    ("sent", "Frames sent"),
    ("failed", "Frames failed"),
    ("last_ack", "Last ack"),
    ("last_warning", "Last warning"),
    ("last_error", "Last error"),
    ("worker", "Worker thread"),
    ("queue", "Frame queue"),
]


class StatusWindow:
    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.title("Connection Status Monitor")
        self.win.geometry("380x420")
        # Closing this window shouldn't close the app -- there's no state
        # here that would be lost by just hiding it.
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)

        self._connect_start = None   # time.monotonic() at last connect, for uptime
        self._frames_sent = 0
        self._frames_failed = 0

        self._build_ui()
        self.set_disconnected()
        self.win.after(1000, self._tick_uptime)

    def _build_ui(self):
        outer = ttk.Frame(self.win, padding=10)
        outer.pack(fill="both", expand=True)

        status_row = ttk.Frame(outer)
        status_row.pack(fill="x")
        self.dot = tk.Canvas(status_row, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=DOT_DISCONNECTED, outline="")
        self.lbl_state = ttk.Label(status_row, text="Disconnected", font=("", 10, "bold"))
        self.lbl_state.pack(side="left")

        grid = ttk.Frame(outer)
        grid.pack(fill="x", pady=(10, 0))
        grid.columnconfigure(1, weight=1)

        self._fields = {}
        for row, (key, label) in enumerate(_FIELDS):
            ttk.Label(grid, text=f"{label}:").grid(row=row, column=0, sticky="w", pady=1)
            val = ttk.Label(grid, text="--", foreground="#333")
            val.grid(row=row, column=1, sticky="w", padx=(6, 0), pady=1)
            self._fields[key] = val

        ttk.Label(outer, text="Event log", font=("", 9, "bold")).pack(anchor="w", pady=(10, 2))
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self.log = tk.Listbox(log_frame, yscrollcommand=scrollbar.set, font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.log.yview)

    # ------------------------------------------------------------------
    # Called by MatrixApp
    # ------------------------------------------------------------------

    def set_connecting(self, port: str, baud: int):
        self._set_field("transport", TRANSPORT_SERIAL)
        self._set_field("port", port)
        self._set_field("baud", str(baud))
        self.log_event(f"Connecting to {port}...", "info")

    def set_connected(self, port: str, baud: int, firmware: str):
        self._connect_start = time.monotonic()
        self.dot.itemconfig(self._dot_id, fill=DOT_CONNECTED)
        self.lbl_state.configure(text="Connected")
        self._set_field("transport", TRANSPORT_SERIAL)
        self._set_field("port", port)
        self._set_field("baud", str(baud))
        self._set_field("firmware", firmware or "(no banner)")
        self._set_field("last_heartbeat", "--")
        self.log_event(f"Connected on {port}" + (f" -- {firmware}" if firmware else ""), "info")

    def set_disconnected(self, reason: str = None):
        was_connected = self._connect_start is not None
        self._connect_start = None
        self.dot.itemconfig(self._dot_id, fill=DOT_DISCONNECTED)
        self.lbl_state.configure(text="Disconnected")
        self._set_field("uptime", "--")
        self._set_field("firmware", "--")
        self._set_field("last_heartbeat", "--")
        if reason:
            self._frames_failed += 1 if was_connected else 0
            self._set_field("failed", str(self._frames_failed))
            self._set_field("last_error", reason)
            self.log_event(f"Disconnected: {reason}", "error")
        else:
            self.log_event("Disconnected", "info")

    def connect_failed(self, message: str):
        self.dot.itemconfig(self._dot_id, fill=DOT_DISCONNECTED)
        self.lbl_state.configure(text="Disconnected")
        self._set_field("last_error", message)
        self.log_event(f"Connect failed: {message}", "error")

    def set_warning(self, message: str):
        self._set_field("last_warning", message)
        self.log_event(message, "warning")

    def record_ack(self, ack: str):
        self._frames_sent += 1
        self._set_field("sent", str(self._frames_sent))
        self._set_field("last_ack", ack or "(no response)")
        self.log_event(f"Sent -- {ack or '(no ack)'}", "info")

    def record_heartbeat(self, response: str):
        # Deliberately not logged to the event log -- this fires every few
        # seconds for as long as the board sits idle, and a log entry for
        # each tick would just bury the actually-interesting events. The
        # field itself is what proves the status is current.
        ts = time.strftime("%H:%M:%S")
        self._set_field("last_heartbeat", ts if response else f"{ts} (no response)")

    def set_worker_alive(self, alive: bool):
        self._set_field("worker", "Alive" if alive else "Dead")

    def set_queue_depth(self, depth: int):
        self._set_field("queue", "Empty" if depth == 0 else f"{depth} queued")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_field(self, key: str, value: str):
        self._fields[key].configure(text=value)

    def _tick_uptime(self):
        if self._connect_start is not None:
            elapsed = int(time.monotonic() - self._connect_start)
            self._set_field("uptime", f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        self.win.after(1000, self._tick_uptime)

    def log_event(self, message: str, kind: str = "info"):
        ts = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {message}")
        self.log.itemconfig("end", foreground=_LOG_COLORS.get(kind, "#1a1a1a"))
        if self.log.size() > MAX_LOG_LINES:
            self.log.delete(0)
        self.log.see("end")
