"""Colour palette and ttk styling.

Tk's default widgets are grey and flat. Switching to the 'clam' theme is what makes
ttk actually accept colours, so everything here depends on that happening first.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# --- palette ---
BG = "#eef1f8"          # window background
CARD = "#ffffff"        # panels
INK = "#1b2138"         # primary text
MUTED = "#6b7490"       # secondary text
PRIMARY = "#3b5bdb"     # indigo — primary actions
PRIMARY_DARK = "#2f49af"
ACCENT = "#0ca678"      # teal — apply / success
ACCENT_DARK = "#087f5b"
WARN = "#f08c00"        # amber — needs attention
DANGER = "#e03131"
LINE = "#d7ddea"

# Row tints in the results grid
ROW_APPLIED = "#e6f7ef"
ROW_MANUAL = "#fff6e5"
ROW_FILLED = "#e7f0ff"
ROW_NEW = "#ffffff"

HEADER_BG = "#1b2138"
HEADER_FG = "#ffffff"


def apply_theme(root: tk.Misc) -> ttk.Style:
    style = ttk.Style(root)
    # 'clam' is the only bundled theme that honours background colours on most widgets.
    if "clam" in style.theme_names():
        style.theme_use("clam")

    root.configure(bg=BG)

    style.configure(".", background=BG, foreground=INK, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD, relief="flat")
    style.configure("TLabel", background=BG, foreground=INK)
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("H1.TLabel", background=HEADER_BG, foreground=HEADER_FG,
                    font=("Segoe UI Semibold", 15))
    style.configure("Sub.TLabel", background=HEADER_BG, foreground="#aab3d0",
                    font=("Segoe UI", 9))
    style.configure("Section.TLabel", background=BG, foreground=MUTED,
                    font=("Segoe UI Semibold", 9))

    style.configure("TLabelframe", background=BG, foreground=MUTED, bordercolor=LINE)
    style.configure("TLabelframe.Label", background=BG, foreground=PRIMARY,
                    font=("Segoe UI Semibold", 10))

    # Buttons
    style.configure("TButton", background="#dde3f0", foreground=INK,
                    borderwidth=0, focusthickness=0, padding=(12, 7))
    style.map("TButton", background=[("active", "#ccd5ea"), ("disabled", "#e9ecf3")],
              foreground=[("disabled", "#a7aec2")])

    style.configure("Primary.TButton", background=PRIMARY, foreground="#ffffff",
                    font=("Segoe UI Semibold", 10), padding=(16, 9))
    style.map("Primary.TButton",
              background=[("active", PRIMARY_DARK), ("disabled", "#b6c0e4")])

    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=("Segoe UI Semibold", 10), padding=(13, 8))
    style.map("Accent.TButton",
              background=[("active", ACCENT_DARK), ("disabled", "#a9d9c8")])

    style.configure("Warn.TButton", background=WARN, foreground="#ffffff", padding=(11, 7))
    style.map("Warn.TButton", background=[("active", "#d97d00"), ("disabled", "#f0cf9c")])

    style.configure("Ghost.TButton", background=BG, foreground=PRIMARY, padding=(10, 6))
    style.map("Ghost.TButton", background=[("active", "#dde3f0")])

    # Inputs
    style.configure("TEntry", fieldbackground=CARD, bordercolor=LINE, padding=5)
    style.configure("TCombobox", fieldbackground=CARD, background=CARD, bordercolor=LINE, padding=4)
    style.configure("TCheckbutton", background=BG, foreground=INK)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("Card.TCheckbutton", background=CARD, foreground=INK)

    style.configure("TMenubutton", background="#dde3f0", foreground=INK, padding=(12, 7))
    style.map("TMenubutton", background=[("active", "#ccd5ea")])

    # Notebook
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", background="#dde3f0", foreground=MUTED,
                    padding=(20, 10), font=("Segoe UI Semibold", 10), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", CARD)], foreground=[("selected", PRIMARY)])

    # Results grid
    style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                    rowheight=27, borderwidth=0)
    style.configure("Treeview.Heading", background="#dde3f0", foreground=INK,
                    font=("Segoe UI Semibold", 9), padding=6, borderwidth=0)
    style.map("Treeview.Heading", background=[("active", "#ccd5ea")])
    style.map("Treeview", background=[("selected", PRIMARY)], foreground=[("selected", "#ffffff")])

    style.configure("TProgressbar", background=ACCENT, troughcolor="#dde3f0", borderwidth=0)
    style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))

    return style


def header(parent: tk.Misc, title: str, subtitle: str) -> tk.Frame:
    """The dark title bar across the top of the window."""
    bar = tk.Frame(parent, bg=HEADER_BG)
    inner = tk.Frame(bar, bg=HEADER_BG)
    inner.pack(fill="x", padx=18, pady=12)
    ttk.Label(inner, text=title, style="H1.TLabel").pack(anchor="w")
    ttk.Label(inner, text=subtitle, style="Sub.TLabel").pack(anchor="w", pady=(2, 0))
    return bar
