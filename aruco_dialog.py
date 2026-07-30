"""
ArUco Marker Generator Dialog
================================
A small modal Toplevel for picking a dictionary/ID/border-width and
generating an ArUco marker via aruco_gen.py. Only builds and validates the
form -- it has no opinion about what happens to the generated marker (that's
the on_generate callback the caller supplies), the same separation of
concerns status_window.py keeps from MatrixLink/the queues.
"""

import tkinter as tk
from tkinter import messagebox, ttk

import aruco_gen


class ArucoDialog:
    def __init__(self, root: tk.Tk, on_generate):
        """on_generate(dictionary_name: str, marker_id: int, border_bits: int)
        is called once the form validates and Generate is clicked. Any
        aruco_gen.ArucoGenError or OSError it raises (e.g. the PNG write
        failing) is shown as an error dialog instead of propagating -- the
        dialog otherwise closes itself right after a successful call."""
        self.on_generate = on_generate

        self.win = tk.Toplevel(root)
        self.win.title("Generate ArUco Marker")
        self.win.resizable(False, False)
        self.win.transient(root)
        self.win.grab_set()

        self.var_dict = tk.StringVar(value=aruco_gen.DEFAULT_DICTIONARY)
        self.var_id = tk.StringVar(value="0")
        self.var_border = tk.StringVar(value="1")

        self._build_ui()
        self._update_range_label()
        self.win.bind("<Return>", lambda _e: self._on_generate_click())

    def _build_ui(self):
        outer = ttk.Frame(self.win, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="Dictionary").grid(row=0, column=0, sticky="w")
        combo = ttk.Combobox(outer, textvariable=self.var_dict, state="readonly",
                              values=sorted(aruco_gen.DICTIONARIES), width=14)
        combo.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        combo.bind("<<ComboboxSelected>>", lambda _e: self._update_range_label())

        ttk.Label(outer, text="Marker ID").grid(row=1, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.var_id, width=10).grid(row=1, column=1, sticky="w", pady=(0, 2))
        self.lbl_range = ttk.Label(outer, text="", foreground="#666")
        self.lbl_range.grid(row=2, column=1, sticky="w")

        ttk.Label(outer, text="Border bits").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(outer, textvariable=self.var_border, width=10).grid(row=3, column=1, sticky="w", pady=(6, 0))

        ttk.Button(outer, text="Generate", command=self._on_generate_click).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _update_range_label(self):
        size = aruco_gen.dictionary_size(self.var_dict.get())
        self.lbl_range.configure(text=f"(0-{size - 1})")

    def _on_generate_click(self):
        try:
            marker_id = int(self.var_id.get())
            border_bits = int(self.var_border.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Marker ID and border bits must be whole numbers.",
                                  parent=self.win)
            return
        try:
            self.on_generate(self.var_dict.get(), marker_id, border_bits)
        except (aruco_gen.ArucoGenError, OSError) as e:
            messagebox.showerror("Could not generate marker", str(e), parent=self.win)
            return
        self.win.destroy()
