"""Desktop UI.

Four tabs:
  Search          — pick sources and browser, find jobs, see what you've already applied
                    to, apply automatically or fill-and-review, mark manual applications
  My Details      — the saved form values used to fill company application forms
  Roles & Resumes — multiple roles, each with one or more resumes
  Activity        — live log

Everything long-running happens on a worker thread (see worker.py); the UI only ever
reads from a queue.
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..browser import detect_browsers
from ..config import Config, ConfigError, load_config
from ..filters import POSTED_WITHIN_CHOICES, apply_runtime_filters, describe, parse_locations
from ..models import MatchResult, Status
from ..sources import ALL_SOURCE_NAMES
from ..store import Store
from . import theme
from .worker import QueueLogHandler, Worker

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "New",
    Status.APPLIED.value: "✓ Applied",
    Status.ALREADY_APPLIED.value: "✓ Applied",
    Status.APPLIED_MANUALLY.value: "✓ You applied",
    Status.FILLED.value: "◐ Filled — review",
    Status.IRRELEVANT.value: "✗ Not relevant",
    Status.MANUAL.value: "⚠ Needs you",
    Status.FAILED.value: "⚠ Failed",
    Status.DRY_RUN.value: "· Dry run",
    Status.SKIPPED.value: "· Skipped",
}

SOURCE_LABELS = {
    "naukri": "Naukri",
    "linkedin": "LinkedIn",
    "greenhouse": "Greenhouse boards",
    "lever": "Lever boards",
    "ashby": "Ashby boards",
    "smartrecruiters": "SmartRecruiters",
    "workable": "Workable",
    "recruitee": "Recruitee",
    "career_pages": "Company career pages",
}

APPLY_MODES = {
    "Automatic — fill and submit": "auto",
    "Manual review — fill, I submit": "manual",
}

PROFILE_FIELDS: list[tuple[str, str]] = [
    ("Full name", "profile.full_name"),
    ("First name", "profile.first_name"),
    ("Last name", "profile.last_name"),
    ("Email", "profile.email"),
    ("Phone", "profile.phone"),
    ("Location", "profile.location"),
    ("City", "profile.city"),
    ("State", "profile.state"),
    ("Country", "profile.country"),
    ("Postal code", "profile.postal_code"),
    ("Address", "profile.address"),
    ("LinkedIn URL", "profile.linkedin"),
    ("GitHub URL", "profile.github"),
    ("Portfolio URL", "profile.portfolio"),
    ("Current company", "profile.current_company"),
    ("Current CTC", "profile.current_ctc"),
    ("Expected CTC", "profile.expected_ctc"),
    ("Notice period", "profile.notice_period"),
    ("Total experience (years)", "profile.total_experience_years"),
    ("Heard about role from", "profile.heard_from"),
]

CHOICE_FIELDS: list[tuple[str, str, list[str]]] = [
    ("Authorised to work", "profile.work_authorized", ["Yes", "No"]),
    ("Needs visa sponsorship", "profile.needs_sponsorship", ["No", "Yes"]),
    ("Willing to relocate", "profile.willing_to_relocate", ["Yes", "No"]),
    ("Open to remote", "profile.open_to_remote", ["Yes", "No"]),
]


class JobHunterApp(tk.Tk):
    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.title("JobHunter")
        self.geometry("1320x840")
        self.minsize(1040, 680)

        self.style = theme.apply_theme(self)
        self.worker = Worker()
        self.matches: dict[str, MatchResult] = {}
        self._match_seq = 0
        self.config_path = config_path
        self.cfg: Config | None = None
        self.store: Store | None = None
        self._pipeline: Any = None

        self.browsers = detect_browsers()

        self._build_ui()
        self._load_config(initial=True)
        self._attach_log_handler()
        self.after(150, self._poll)

    # --- setup ---

    def _attach_log_handler(self) -> None:
        handler = QueueLogHandler(self.worker.queue)
        handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

    def _load_config(self, initial: bool = False) -> None:
        try:
            self.cfg = load_config(self.config_path)
            self.store = Store(self.cfg.data_dir() / "jobhunter.sqlite3")
            self._pipeline = None
            self._set_status(f"Loaded {self.cfg.path}")
            self._populate_profile()
            self._populate_roles()
            self._populate_pages()
            self._populate_run_settings()
        except ConfigError as exc:
            self.cfg = None
            self._set_status(f"No config: {exc}")
            if initial:
                messagebox.showwarning(
                    "No configuration",
                    f"{exc}\n\nCopy config/config.example.yaml to config/config.yaml, "
                    "then fill in the 'My Details' and 'Roles & Resumes' tabs.",
                )

    def pipeline(self) -> Any:
        """Shared Pipeline instance — also the path Oracle writes go through."""
        if self._pipeline is None and self.cfg is not None:
            from ..pipeline import Pipeline

            self._pipeline = Pipeline(self.cfg, store=self.store)
        return self._pipeline

    def _build_ui(self) -> None:
        theme.header(
            self, "JobHunter",
            "Find matching jobs, apply automatically, and keep track of what you did by hand",
        ).pack(fill="x")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self._build_search_tab()
        self._build_profile_tab()
        self._build_roles_tab()
        self._build_career_pages_tab()
        self._build_log_tab()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=14, pady=8)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=190)
        self.progress.pack(side="right")

    # --- tab 1: search ---

    def _build_search_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Search  ")

        # --- row 1: where to look ---
        row1 = ttk.Frame(tab)
        row1.pack(fill="x", padx=14, pady=(14, 6))

        self.search_btn = ttk.Button(row1, text="🔍  Search jobs", style="Primary.TButton",
                                     command=self.on_search)
        self.search_btn.pack(side="left")

        ttk.Label(row1, text="Sources").pack(side="left", padx=(18, 6))
        self.source_vars: dict[str, tk.BooleanVar] = {
            name: tk.BooleanVar(value=True) for name in ALL_SOURCE_NAMES
        }
        self.source_button = ttk.Menubutton(row1, text="All sources")
        source_menu = tk.Menu(self.source_button, tearoff=False)
        for name in ALL_SOURCE_NAMES:
            source_menu.add_checkbutton(
                label=SOURCE_LABELS.get(name, name), variable=self.source_vars[name],
                command=self._update_source_button,
            )
        source_menu.add_separator()
        source_menu.add_command(label="Select all", command=lambda: self._set_all_sources(True))
        source_menu.add_command(label="Select none", command=lambda: self._set_all_sources(False))
        self.source_button.configure(menu=source_menu)
        self.source_button.pack(side="left")

        ttk.Label(row1, text="Browser").pack(side="left", padx=(18, 6))
        self.browser_var = tk.StringVar()
        self.browser_box = ttk.Combobox(
            row1, textvariable=self.browser_var, state="readonly", width=24,
            values=[b.label for b in self.browsers],
        )
        self.browser_box.pack(side="left")

        self.use_profile_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row1, text="Use my logged-in profile", variable=self.use_profile_var,
            command=self._warn_about_profile,
        ).pack(side="left", padx=(10, 0))

        # --- row 1b: what to look for ---
        row1b = ttk.Frame(tab)
        row1b.pack(fill="x", padx=14, pady=(2, 6))

        ttk.Label(row1b, text="Posted within").pack(side="left", padx=(0, 6))
        self.posted_within_var = tk.StringVar(value="Last week")
        ttk.Combobox(row1b, textvariable=self.posted_within_var, state="readonly", width=15,
                     values=list(POSTED_WITHIN_CHOICES)).pack(side="left")

        ttk.Label(row1b, text="Locations").pack(side="left", padx=(18, 6))
        self.locations_var = tk.StringVar()
        ttk.Entry(row1b, textvariable=self.locations_var, width=46).pack(side="left")
        ttk.Label(row1b, text="comma separated · blank = anywhere",
                  style="Muted.TLabel").pack(side="left", padx=8)

        # --- row 2: how to apply ---
        row2 = ttk.Frame(tab)
        row2.pack(fill="x", padx=14, pady=(2, 8))

        ttk.Label(row2, text="Apply mode").pack(side="left", padx=(0, 6))
        self.apply_mode_var = tk.StringVar(value=list(APPLY_MODES)[0])
        ttk.Combobox(row2, textvariable=self.apply_mode_var, state="readonly", width=28,
                     values=list(APPLY_MODES)).pack(side="left")

        self.dry_run_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="Dry run (never submit)", variable=self.dry_run_var).pack(
            side="left", padx=(14, 0))

        self.apply_sel_btn = ttk.Button(row2, text="▶  Apply to selected", style="Accent.TButton",
                                        command=self.on_apply_selected, state="disabled")
        self.apply_sel_btn.pack(side="left", padx=(20, 0))

        self.apply_all_btn = ttk.Button(row2, text="Apply to all new", command=self.on_apply_all,
                                        state="disabled")
        self.apply_all_btn.pack(side="left", padx=(8, 0))

        self.stop_btn = ttk.Button(row2, text="■  Stop", style="Warn.TButton", command=self.on_stop)
        self.stop_btn.pack(side="right")

        # --- row 3: filter ---
        row3 = ttk.Frame(tab)
        row3.pack(fill="x", padx=14)
        ttk.Label(row3, text="Filter").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh_tree())
        ttk.Entry(row3, textvariable=self.filter_var, width=40).pack(side="left", padx=8)
        self.hide_applied_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="Hide already applied", variable=self.hide_applied_var,
                        command=self._refresh_tree).pack(side="left", padx=(6, 0))
        self.count_var = tk.StringVar(value="No results yet")
        ttk.Label(row3, textvariable=self.count_var, style="Muted.TLabel").pack(side="left", padx=14)

        # --- results grid ---
        columns = ("status", "score", "title", "company", "location", "posted",
                   "applicants", "role", "resume", "source")
        widths = (118, 55, 280, 150, 165, 88, 95, 125, 105, 92)

        wrap = ttk.Frame(tab, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=10)

        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
        for col, width in zip(columns, widths):
            self.tree.heading(col, text=col.title(), command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor="w")

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("applied", background=theme.ROW_APPLIED)
        self.tree.tag_configure("manual", background=theme.ROW_MANUAL)
        self.tree.tag_configure("filled", background=theme.ROW_FILLED)
        self.tree.tag_configure("new", background=theme.ROW_NEW)
        self.tree.tag_configure("irrelevant", background=theme.ROW_IRRELEVANT,
                                foreground=theme.MUTED)

        self.tree.bind("<Double-1>", self._open_selected_url)
        self.tree.bind("<Button-3>", self._show_context_menu)

        self.row_menu = tk.Menu(self, tearoff=False)
        self.row_menu.add_command(label="Open posting in browser", command=self._open_selected_url)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="✓  I applied to this myself", command=self.on_mark_applied)
        self.row_menu.add_command(label="✗  Not relevant", command=self.on_mark_irrelevant)
        self.row_menu.add_command(label="↩  Undo mark", command=self.on_undo_mark)

        # --- footer actions ---
        footer = ttk.Frame(tab)
        footer.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(footer, text="✓  I applied to this myself", style="Accent.TButton",
                   command=self.on_mark_applied).pack(side="left")
        ttk.Button(footer, text="✗  Not relevant", style="Warn.TButton",
                   command=self.on_mark_irrelevant).pack(side="left", padx=8)
        ttk.Button(footer, text="↩  Undo", command=self.on_undo_mark).pack(side="left")
        ttk.Button(footer, text="Open posting", style="Ghost.TButton",
                   command=self._open_selected_url).pack(side="left", padx=8)
        ttk.Label(
            footer,
            text="Marks are saved to the database. 'Not relevant' also teaches the matcher — "
                 "reject a few jobs from the same company or with the same title word and "
                 "similar ones stop surfacing.",
            style="Muted.TLabel", wraplength=520,
        ).pack(side="left", padx=16)

    def _set_all_sources(self, value: bool) -> None:
        for var in self.source_vars.values():
            var.set(value)
        self._update_source_button()

    def _update_source_button(self) -> None:
        chosen = self.selected_sources()
        if len(chosen) == len(self.source_vars):
            label = "All sources"
        elif not chosen:
            label = "No sources!"
        elif len(chosen) <= 2:
            label = ", ".join(SOURCE_LABELS.get(s, s) for s in chosen)
        else:
            label = f"{len(chosen)} sources"
        self.source_button.configure(text=label)

    def selected_sources(self) -> list[str]:
        return [name for name, var in self.source_vars.items() if var.get()]

    def _warn_about_profile(self) -> None:
        if not self.use_profile_var.get():
            return
        label = self.browser_var.get() or "that browser"
        messagebox.showinfo(
            "Using your real browser profile",
            f"JobHunter will use your existing {label} profile, so sites you're already "
            "signed into (Naukri, LinkedIn) stay signed in.\n\n"
            f"{label} must be COMPLETELY CLOSED while a search or apply runs — including "
            "any icon in the system tray. Chromium browsers will not share a profile "
            "between two programs.",
        )

    # --- tab 2: the predefined form ---

    def _build_profile_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  My Details  ")

        ttk.Label(
            tab,
            text="These values are typed into company application forms. Keep them accurate — "
                 "they are submitted as fact on your behalf.",
            style="Muted.TLabel", wraplength=1150,
        ).pack(anchor="w", padx=16, pady=(14, 8))

        canvas = tk.Canvas(tab, highlightthickness=0, bg=theme.BG)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(16, 0))
        scrollbar.pack(side="right", fill="y")

        self.profile_vars: dict[str, tk.StringVar] = {}

        grid = ttk.Labelframe(inner, text="Personal details", padding=8)
        grid.pack(fill="x", pady=(0, 12), padx=(0, 16))
        for index, (label, key) in enumerate(PROFILE_FIELDS):
            row, col = divmod(index, 2)
            cell = ttk.Frame(grid)
            cell.grid(row=row, column=col, sticky="w", padx=10, pady=5)
            ttk.Label(cell, text=label, width=22).pack(side="left")
            var = tk.StringVar()
            self.profile_vars[key] = var
            ttk.Entry(cell, textvariable=var, width=38).pack(side="left")

        choices = ttk.Labelframe(inner, text="Standard screening answers", padding=8)
        choices.pack(fill="x", pady=(0, 12), padx=(0, 16))
        for index, (label, key, options) in enumerate(CHOICE_FIELDS):
            row, col = divmod(index, 2)
            cell = ttk.Frame(choices)
            cell.grid(row=row, column=col, sticky="w", padx=10, pady=5)
            ttk.Label(cell, text=label, width=22).pack(side="left")
            var = tk.StringVar()
            self.profile_vars[key] = var
            ttk.Combobox(cell, textvariable=var, values=options, width=35,
                         state="readonly").pack(side="left")

        files = ttk.Labelframe(inner, text="Default files", padding=8)
        files.pack(fill="x", pady=(0, 12), padx=(0, 16))
        self._file_row(files, "Default resume", "profile.resume_path")
        self._file_row(files, "Cover letter file", "profile.cover_letter_path")

        answers = ttk.Labelframe(
            inner, text="Custom question answers   (one per line:  question fragment = answer)",
            padding=8,
        )
        answers.pack(fill="both", expand=True, pady=(0, 12), padx=(0, 16))
        ttk.Label(
            answers,
            text="The left side is matched as a substring of the question on the form. Anything a "
                 "form requires that isn't answered here sends the job to your manual list instead "
                 "of being guessed at.",
            style="Muted.TLabel", wraplength=1050,
        ).pack(anchor="w", padx=6, pady=(2, 6))
        self.answers_text = tk.Text(answers, height=10, wrap="word", relief="flat",
                                    bg=theme.CARD, fg=theme.INK, insertbackground=theme.INK,
                                    highlightthickness=1, highlightbackground=theme.LINE)
        self.answers_text.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        buttons = ttk.Frame(inner)
        buttons.pack(fill="x", pady=(0, 22), padx=(0, 16))
        ttk.Button(buttons, text="💾  Save details", style="Primary.TButton",
                   command=self.on_save_profile).pack(side="left")
        ttk.Button(buttons, text="Reload from file", command=lambda: self._load_config()).pack(
            side="left", padx=8)

    def _file_row(self, parent: tk.Widget, label: str, key: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=5)
        ttk.Label(row, text=label, width=22).pack(side="left")
        var = tk.StringVar()
        self.profile_vars[key] = var
        ttk.Entry(row, textvariable=var, width=70).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Browse…", command=lambda v=var: self._pick_file(v)).pack(side="left")

    def _pick_file(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            title="Choose a file",
            filetypes=[("Documents", "*.pdf *.docx *.txt"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    # --- tab 3: roles & resumes ---

    def _build_roles_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Roles & Resumes  ")

        ttk.Label(
            tab,
            text="Each role has its own titles and resumes. Every job is scored against every role; "
                 "the best-matching role decides which resume gets uploaded. Give a role several "
                 "resumes and each job gets whichever variant its description matches best.",
            style="Muted.TLabel", wraplength=1150,
        ).pack(anchor="w", padx=16, pady=(14, 8))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 16))
        ttk.Label(left, text="Roles", style="Section.TLabel").pack(anchor="w")
        self.roles_list = tk.Listbox(left, width=26, height=20, exportselection=False,
                                     relief="flat", bg=theme.CARD, fg=theme.INK,
                                     selectbackground=theme.PRIMARY, selectforeground="#fff",
                                     highlightthickness=1, highlightbackground=theme.LINE)
        self.roles_list.pack(fill="y", expand=True)
        self.roles_list.bind("<<ListboxSelect>>", lambda _e: self._show_role())

        role_btns = ttk.Frame(left)
        role_btns.pack(fill="x", pady=6)
        ttk.Button(role_btns, text="Add", width=8, command=self.on_add_role).pack(side="left")
        ttk.Button(role_btns, text="Delete", width=8, command=self.on_delete_role).pack(
            side="left", padx=4)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        name_row = ttk.Frame(right)
        name_row.pack(fill="x", pady=(0, 8))
        ttk.Label(name_row, text="Role name", width=16).pack(side="left")
        self.role_name_var = tk.StringVar()
        ttk.Entry(name_row, textvariable=self.role_name_var, width=42).pack(side="left")
        self.role_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(name_row, text="Enabled", variable=self.role_enabled_var).pack(
            side="left", padx=12)

        ttk.Label(right, text="Job titles to look for (one per line)",
                  style="Section.TLabel").pack(anchor="w")
        self.role_titles_text = self._text(right, 6)

        ttk.Label(right, text="Extra keywords for this role (one per line)",
                  style="Section.TLabel").pack(anchor="w")
        self.role_keywords_text = self._text(right, 5)

        ttk.Label(right, text="Resumes for this role", style="Section.TLabel").pack(anchor="w")
        self.role_resumes_list = tk.Listbox(right, height=6, relief="flat", bg=theme.CARD,
                                            fg=theme.INK, selectbackground=theme.PRIMARY,
                                            selectforeground="#fff", highlightthickness=1,
                                            highlightbackground=theme.LINE)
        self.role_resumes_list.pack(fill="x", pady=(2, 4))

        resume_btns = ttk.Frame(right)
        resume_btns.pack(fill="x")
        ttk.Button(resume_btns, text="Add resume…", command=self.on_add_resume).pack(side="left")
        ttk.Button(resume_btns, text="Remove", command=self.on_remove_resume).pack(side="left", padx=6)

        save_row = ttk.Frame(right)
        save_row.pack(fill="x", pady=14)
        ttk.Button(save_row, text="💾  Save roles", style="Primary.TButton",
                   command=self.on_save_roles).pack(side="left")
        ttk.Button(save_row, text="Apply changes to this role",
                   command=self._capture_role).pack(side="left", padx=8)

        self._roles_data: list[dict[str, Any]] = []
        self._current_role: int | None = None

    def _text(self, parent: tk.Widget, height: int) -> tk.Text:
        widget = tk.Text(parent, height=height, wrap="word", relief="flat", bg=theme.CARD,
                         fg=theme.INK, insertbackground=theme.INK, highlightthickness=1,
                         highlightbackground=theme.LINE)
        widget.pack(fill="x", pady=(2, 10))
        return widget

    # --- tab 4: company career pages ---

    def _build_career_pages_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Career Pages  ")

        ttk.Label(
            tab,
            text="Add any company's careers URL. JobHunter works out which job board is behind "
                 "the page and reads it directly — most 'custom' career pages are really "
                 "Greenhouse, Lever or Ashby underneath. If the job list is drawn by JavaScript, "
                 "it re-opens the page in the browser and reads the rendered list instead.",
            style="Muted.TLabel", wraplength=1150,
        ).pack(anchor="w", padx=16, pady=(14, 8))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 16))
        ttk.Label(left, text="Career pages", style="Section.TLabel").pack(anchor="w")
        self.pages_list = tk.Listbox(left, height=16, exportselection=False, relief="flat",
                                     bg=theme.CARD, fg=theme.INK, selectbackground=theme.PRIMARY,
                                     selectforeground="#fff", highlightthickness=1,
                                     highlightbackground=theme.LINE)
        self.pages_list.pack(fill="both", expand=True)
        self.pages_list.bind("<<ListboxSelect>>", lambda _e: self._show_page())

        list_btns = ttk.Frame(left)
        list_btns.pack(fill="x", pady=6)
        ttk.Button(list_btns, text="Add", width=8, command=self.on_add_page).pack(side="left")
        ttk.Button(list_btns, text="Delete", width=8, command=self.on_delete_page).pack(
            side="left", padx=4)
        ttk.Button(list_btns, text="🔎  Test this page", style="Ghost.TButton",
                   command=self.on_test_page).pack(side="left", padx=4)

        right = ttk.Frame(body)
        right.pack(side="left", fill="y")

        for label, attr, width, hint in [
            ("Company name", "page_name_var", 40, ""),
            ("Careers URL", "page_url_var", 40, "https://company.com/careers"),
            ("Default location", "page_location_var", 40, "optional, e.g. Chennai, India"),
            ("Job link selector", "page_selector_var", 40, "optional CSS, only if detection fails"),
        ]:
            ttk.Label(right, text=label, style="Section.TLabel").pack(anchor="w", pady=(8, 0))
            var = tk.StringVar()
            setattr(self, attr, var)
            ttk.Entry(right, textvariable=var, width=width).pack(anchor="w")
            if hint:
                ttk.Label(right, text=hint, style="Muted.TLabel").pack(anchor="w")

        save_row = ttk.Frame(right)
        save_row.pack(fill="x", pady=16)
        ttk.Button(save_row, text="Apply changes", command=self._capture_page).pack(side="left")
        ttk.Button(save_row, text="💾  Save career pages", style="Primary.TButton",
                   command=self.on_save_pages).pack(side="left", padx=8)

        self._pages_data: list[dict[str, Any]] = []
        self._current_page: int | None = None

    def _populate_pages(self) -> None:
        if not self.cfg:
            return
        self._pages_data = [dict(p) for p in (self.cfg.get("sources.custom_career_pages", []) or [])]
        self._refresh_pages_list()

    def _refresh_pages_list(self) -> None:
        self.pages_list.delete(0, "end")
        for page in self._pages_data:
            self.pages_list.insert("end", f"{page.get('name', '?')}  —  {page.get('url', '')}")
        if self._pages_data and self._current_page is None:
            self.pages_list.selection_set(0)
            self._show_page()

    def _show_page(self) -> None:
        selection = self.pages_list.curselection()
        if not selection:
            return
        self._current_page = selection[0]
        page = self._pages_data[self._current_page]
        self.page_name_var.set(page.get("name", ""))
        self.page_url_var.set(page.get("url", ""))
        self.page_location_var.set(page.get("default_location", ""))
        self.page_selector_var.set(page.get("job_link_selector", ""))

    def _capture_page(self) -> None:
        if self._current_page is None:
            return
        page = self._pages_data[self._current_page]
        page["name"] = self.page_name_var.get().strip() or "Unnamed"
        page["url"] = self.page_url_var.get().strip()
        for key, var in (("default_location", self.page_location_var),
                         ("job_link_selector", self.page_selector_var)):
            value = var.get().strip()
            if value:
                page[key] = value
            else:
                page.pop(key, None)
        self._refresh_pages_list()
        self._set_status(f"'{page['name']}' updated (not yet saved)")

    def on_add_page(self) -> None:
        self._pages_data.append({"name": "New company", "url": ""})
        self._current_page = len(self._pages_data) - 1
        self._refresh_pages_list()
        self.pages_list.selection_clear(0, "end")
        self.pages_list.selection_set(self._current_page)
        self._show_page()

    def on_delete_page(self) -> None:
        if self._current_page is None or not self._pages_data:
            return
        name = self._pages_data[self._current_page].get("name", "this page")
        if messagebox.askyesno("Delete", f"Remove '{name}'?"):
            self._pages_data.pop(self._current_page)
            self._current_page = None
            self._refresh_pages_list()

    def on_test_page(self) -> None:
        """Fetch one page now and report what was found — detection can fail quietly,
        so this is how you find out before relying on it in a real search."""
        self._capture_page()
        if self._current_page is None:
            messagebox.showinfo("Pick a page", "Select or add a career page first.")
            return
        page = dict(self._pages_data[self._current_page])
        if not page.get("url"):
            messagebox.showerror("No URL", "Enter the careers URL first.")
            return
        if self.worker.busy:
            messagebox.showinfo("Busy", "A task is already running.")
            return

        self._begin(f"Testing {page.get('name')}…")
        cfg = self.cfg

        def task(worker: Worker) -> None:
            from ..browser import browser_from_config
            from ..sources.base import make_session
            from ..sources.career_page import CareerPageSource

            options = dict(cfg.section("sources.career_page_options")) if cfg else {}
            options.update({"pages": [page], "fetch_descriptions": False})

            with browser_from_config(cfg) as browser:
                source = CareerPageSource(options, session=make_session(), browser=browser)
                source.should_cancel = lambda: worker.cancelled
                jobs = source.fetch([])
            worker.send("page_test", (page.get("name", ""), jobs))

        self.worker.start(task)

    def _show_page_test(self, payload: tuple[str, list]) -> None:
        name, jobs = payload
        if not jobs:
            messagebox.showwarning(
                "Nothing found",
                f"No jobs found on {name}.\n\n"
                "Either the page needs a CSS selector for its job links, or the listing "
                "lives on a separate board — try pointing the URL straight at that.",
            )
            return
        sample = "\n".join(f"  • {j.title}  ({j.location or 'location n/a'})" for j in jobs[:8])
        extra = f"\n  …and {len(jobs) - 8} more" if len(jobs) > 8 else ""
        messagebox.showinfo(
            "Career page works",
            f"Found {len(jobs)} jobs on {name} (via {jobs[0].ats}):\n\n{sample}{extra}",
        )

    def on_save_pages(self) -> None:
        if not self.cfg:
            messagebox.showerror("No config", "Load a config file first.")
            return
        self._capture_page()

        cleaned = []
        for page in self._pages_data:
            if not page.get("url"):
                messagebox.showerror("Missing URL", f"'{page.get('name')}' has no careers URL.")
                return
            cleaned.append(page)

        self.cfg.set("sources.custom_career_pages", cleaned)
        self.source_vars["career_pages"].set(bool(cleaned))
        self._update_source_button()
        self._save_config(f"Saved {len(cleaned)} career page(s)")

    # --- tab 5: activity ---

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Activity  ")
        self.log_text = tk.Text(tab, wrap="word", state="disabled", height=30, relief="flat",
                                bg="#141a2e", fg="#c9d3f0", insertbackground="#c9d3f0",
                                font=("Consolas", 9))
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=14)
        vsb.pack(side="right", fill="y", pady=14)
        self.log_text.tag_configure("warn", foreground="#ffc078")
        self.log_text.tag_configure("err", foreground="#ff8787")
        self.log_text.tag_configure("ok", foreground="#69db7c")

    # --- config <-> widgets ---

    def _populate_run_settings(self) -> None:
        """Seed the Search-tab controls from config."""
        if not self.cfg:
            return
        key = str(self.cfg.get("apply.browser", "") or "").lower()
        chosen = next((b for b in self.browsers if b.key == key), None)
        self.browser_var.set(chosen.label if chosen else (self.browsers[0].label if self.browsers else ""))
        self.use_profile_var.set(bool(self.cfg.get("apply.use_existing_profile", False)))
        self.dry_run_var.set(bool(self.cfg.get("apply.dry_run", True)))

        mode = str(self.cfg.get("apply.mode", "auto"))
        for label, value in APPLY_MODES.items():
            if value == mode:
                self.apply_mode_var.set(label)
                break

        self.locations_var.set(", ".join(self.cfg.get("search.locations", []) or []))
        configured_age = self.cfg.get("search.posted_within_days")
        label = next(
            (name for name, days in POSTED_WITHIN_CHOICES.items() if days == configured_age),
            "Any time" if configured_age is None else "Last week",
        )
        self.posted_within_var.set(label)

        for name, var in self.source_vars.items():
            if name == "career_pages":
                var.set(bool(self.cfg.get("sources.custom_career_pages", [])))
            else:
                var.set(bool(self.cfg.get(f"sources.{name}.enabled", False)))
        self._update_source_button()

    def _push_run_settings(self) -> None:
        """Copy the Search-tab controls into the in-memory config before a run.

        Deliberately not saved to disk — these are per-run choices, and writing the
        file on every click would fight with hand-edited config.
        """
        if not self.cfg:
            return
        chosen = next((b for b in self.browsers if b.label == self.browser_var.get()), None)
        if chosen:
            self.cfg.set("apply.browser", chosen.key)
        self.cfg.set("apply.use_existing_profile", bool(self.use_profile_var.get()))
        self.cfg.set("apply.dry_run", bool(self.dry_run_var.get()))
        self.cfg.set("apply.mode", APPLY_MODES.get(self.apply_mode_var.get(), "auto"))

        # Posted-date and location apply to every source: pushed into the query where a
        # source supports it, and enforced in the Scorer for the ones that don't.
        days = POSTED_WITHIN_CHOICES.get(self.posted_within_var.get())
        locations = parse_locations(self.locations_var.get())
        apply_runtime_filters(self.cfg, posted_within_days=days, locations=locations)
        self._append_log(f"Filters: {describe(days, locations)}")

    def _populate_profile(self) -> None:
        if not self.cfg:
            return
        for key, var in self.profile_vars.items():
            value = self.cfg.get(key, "")
            var.set("" if value is None else str(value))

        answers = self.cfg.get("profile.answers", {}) or {}
        self.answers_text.delete("1.0", "end")
        self.answers_text.insert("1.0", "\n".join(f"{k} = {v}" for k, v in answers.items()))

    def on_save_profile(self) -> None:
        if not self.cfg:
            messagebox.showerror("No config", "Load a config file first.")
            return

        for key, var in self.profile_vars.items():
            value: Any = var.get().strip()
            if key.endswith("total_experience_years") and value:
                try:
                    value = float(value)
                except ValueError:
                    messagebox.showerror("Invalid value", "Total experience must be a number.")
                    return
            self.cfg.set(key, value)

        answers: dict[str, str] = {}
        for line in self.answers_text.get("1.0", "end").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip():
                answers[key.strip()] = value.strip()
        self.cfg.set("profile.answers", answers)
        self._save_config("Details saved")

    def _populate_roles(self) -> None:
        if not self.cfg:
            return
        roles = self.cfg.get("roles", []) or []
        if not roles:
            roles = [{
                "name": "Default",
                "titles": list(self.cfg.get("search.roles", []) or []),
                "keywords": list(self.cfg.get("search.keywords", []) or []),
                "resumes": ([{"path": str(self.cfg.resume_path), "label": "default"}]
                            if self.cfg.resume_path else []),
                "enabled": True,
            }]
        self._roles_data = [dict(r) for r in roles]
        self._refresh_roles_list()

    def _refresh_roles_list(self) -> None:
        self.roles_list.delete(0, "end")
        for role in self._roles_data:
            mark = "" if role.get("enabled", True) else "  (off)"
            self.roles_list.insert("end", f"{role.get('name', 'unnamed')}{mark}")
        if self._roles_data:
            self.roles_list.selection_set(0)
            self._show_role()

    def _show_role(self) -> None:
        selection = self.roles_list.curselection()
        if not selection:
            return
        self._current_role = selection[0]
        role = self._roles_data[self._current_role]

        self.role_name_var.set(role.get("name", ""))
        self.role_enabled_var.set(bool(role.get("enabled", True)))
        self.role_titles_text.delete("1.0", "end")
        self.role_titles_text.insert("1.0", "\n".join(role.get("titles", []) or []))
        self.role_keywords_text.delete("1.0", "end")
        self.role_keywords_text.insert("1.0", "\n".join(role.get("keywords", []) or []))
        self._refresh_resume_list(role)

    def _refresh_resume_list(self, role: dict) -> None:
        self.role_resumes_list.delete(0, "end")
        for spec in role.get("resumes", []) or []:
            path = spec if isinstance(spec, str) else spec.get("path", "")
            label = "" if isinstance(spec, str) else spec.get("label", "")
            exists = "" if Path(path).expanduser().exists() else "   [MISSING]"
            self.role_resumes_list.insert("end", f"{label or Path(path).stem}  —  {path}{exists}")

    def _capture_role(self) -> None:
        if self._current_role is None:
            return
        role = self._roles_data[self._current_role]
        role["name"] = self.role_name_var.get().strip() or "unnamed"
        role["enabled"] = bool(self.role_enabled_var.get())
        role["titles"] = [l.strip() for l in self.role_titles_text.get("1.0", "end").splitlines() if l.strip()]
        role["keywords"] = [l.strip() for l in self.role_keywords_text.get("1.0", "end").splitlines() if l.strip()]
        self._refresh_roles_list()
        self._set_status(f"Role '{role['name']}' updated (not yet saved)")

    def on_add_role(self) -> None:
        self._roles_data.append({"name": "New role", "titles": [], "keywords": [],
                                 "resumes": [], "enabled": True})
        self._refresh_roles_list()
        self.roles_list.selection_clear(0, "end")
        self.roles_list.selection_set(len(self._roles_data) - 1)
        self._show_role()

    def on_delete_role(self) -> None:
        if self._current_role is None or not self._roles_data:
            return
        name = self._roles_data[self._current_role].get("name", "this role")
        if messagebox.askyesno("Delete role", f"Delete '{name}'?"):
            self._roles_data.pop(self._current_role)
            self._current_role = None
            self._refresh_roles_list()

    def on_add_resume(self) -> None:
        if self._current_role is None:
            messagebox.showinfo("Pick a role", "Select or create a role first.")
            return
        paths = filedialog.askopenfilenames(
            title="Choose resume file(s)",
            filetypes=[("Resumes", "*.pdf *.docx *.txt"), ("All files", "*.*")],
        )
        role = self._roles_data[self._current_role]
        role.setdefault("resumes", [])
        for path in paths:
            role["resumes"].append({"path": path, "label": Path(path).stem})
        self._refresh_resume_list(role)

    def on_remove_resume(self) -> None:
        if self._current_role is None:
            return
        selection = self.role_resumes_list.curselection()
        if not selection:
            return
        role = self._roles_data[self._current_role]
        role["resumes"].pop(selection[0])
        self._refresh_resume_list(role)

    def on_save_roles(self) -> None:
        if not self.cfg:
            messagebox.showerror("No config", "Load a config file first.")
            return
        self._capture_role()

        cleaned = []
        for role in self._roles_data:
            if not role.get("titles"):
                messagebox.showerror("Missing titles",
                                     f"Role '{role.get('name')}' has no job titles.")
                return
            cleaned.append({
                "name": role.get("name", "unnamed"),
                "enabled": bool(role.get("enabled", True)),
                "titles": role.get("titles", []),
                "keywords": role.get("keywords", []),
                "resumes": role.get("resumes", []),
                **({"overrides": role["overrides"]} if role.get("overrides") else {}),
            })

        self.cfg.set("roles", cleaned)
        self._save_config(f"Saved {len(cleaned)} role(s)")

    def _save_config(self, message: str) -> None:
        assert self.cfg is not None
        try:
            target = self.cfg.path
            if target is None or target.name.endswith("example.yaml"):
                target = Path("config/config.yaml")
            saved = self.cfg.save(target)
            self._pipeline = None
            self._set_status(f"{message} → {saved}")
            messagebox.showinfo("Saved", f"{message}\n\nWritten to {saved}\n"
                                         f"(previous version kept as {saved.name}.bak)")
        except Exception as exc:
            messagebox.showerror("Could not save", str(exc))

    # --- searching ---

    def on_search(self) -> None:
        if not self.cfg:
            messagebox.showerror("No config", "Load a config file first.")
            return
        if self.worker.busy:
            messagebox.showinfo("Busy", "A task is already running.")
            return

        sources = self.selected_sources()
        if not sources:
            messagebox.showerror("No sources", "Pick at least one source from the Sources menu.")
            return

        problems = self.cfg.validate()
        blocking = [p for p in problems if "resume" in p.lower() or "Nothing to search" in p]
        if blocking:
            messagebox.showerror(
                "Configuration problem",
                "\n".join(blocking) + "\n\nFix this on the Roles & Resumes tab, then Save roles.",
            )
            return

        for warning in self.cfg.warnings():
            self._append_log(f"NOTE: {warning}")

        self._push_run_settings()
        self._begin("Searching…")
        self.tree.delete(*self.tree.get_children())
        self.matches.clear()

        cfg = self.cfg
        self._match_seq = 0

        def task(worker: Worker) -> None:
            from ..browser import browser_from_config
            from ..pipeline import Pipeline

            pipeline = Pipeline(cfg, progress=lambda m: worker.send("progress", m))
            # Naukri needs a logged-in browser; career pages may need one to render a
            # JavaScript job list.
            needs_browser = (
                ("naukri" in sources and cfg.get("sources.naukri.enabled", False))
                or ("career_pages" in sources and bool(cfg.get("sources.custom_career_pages", [])))
            )

            def stream(batch: list[MatchResult]) -> None:
                worker.send("batch", batch)

            kwargs = dict(
                include_seen=True, only=sources,
                on_batch=stream, should_cancel=lambda: worker.cancelled,
            )
            if needs_browser:
                with browser_from_config(cfg) as browser:
                    matches, errors = pipeline.discover_and_match(browser, **kwargs)
            else:
                matches, errors = pipeline.discover_and_match(None, **kwargs)

            worker.send("results", (matches, errors))

        self.worker.start(task)

    def _add_batch(self, batch: list[MatchResult]) -> None:
        """Append newly found matches to the grid while the search is still running."""
        for match in batch:
            self._match_seq += 1
            self.matches[f"m{self._match_seq}"] = match
        self._refresh_tree()

        if self.matches:
            self.apply_sel_btn.configure(state="normal")
            self.apply_all_btn.configure(state="normal")

    def _show_results(self, payload: tuple[list[MatchResult], dict[str, str]]) -> None:
        _matches, errors = payload
        # Rows already arrived via _add_batch; this only reports what went wrong.
        for name, err in errors.items():
            self._append_log(f"  ! source '{name}' failed: {err}", "warn")

        state = "normal" if self.matches else "disabled"
        self.apply_sel_btn.configure(state=state)
        self.apply_all_btn.configure(state=state)

    def _row_status(self, match: MatchResult) -> tuple[str, str]:
        if not self.store:
            return "new", "new"
        status, _, _ = self.store.status_for(match.job)
        if status in (Status.APPLIED.value, Status.ALREADY_APPLIED.value,
                      Status.APPLIED_MANUALLY.value):
            return status, "applied"
        if status == Status.IRRELEVANT.value:
            return status, "irrelevant"
        if status == Status.FILLED.value:
            return status, "filled"
        if status in (Status.MANUAL.value, Status.FAILED.value):
            return status, "manual"
        return status, "new"

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        needle = self.filter_var.get().strip().lower()
        hide_applied = self.hide_applied_var.get()
        shown = applied = rejected = 0

        for iid, match in self.matches.items():
            status, tag = self._row_status(match)
            if tag == "irrelevant":
                rejected += 1
            if tag == "applied":
                applied += 1
                if hide_applied:
                    continue
            job = match.job
            haystack = f"{job.title} {job.company} {job.location} {match.role_name}".lower()
            if needle and needle not in haystack:
                continue

            self.tree.insert(
                "", "end", iid=iid, tags=(tag,),
                values=(
                    STATUS_LABELS.get(status, status),
                    f"{match.score:.0%}",
                    job.title,
                    job.company,
                    job.location or "—",
                    job.posted_display,
                    job.applicants_display,
                    match.role_name,
                    match.resume_label or "default",
                    job.source,
                ),
            )
            shown += 1

        summary = f"{shown} shown · {len(self.matches)} matched · {applied} already applied"
        if rejected:
            summary += f" · {rejected} marked not relevant"
        self.count_var.set(summary)

    def _sort_by(self, column: str) -> None:
        rows = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]
        rows.sort(reverse=column == "score")
        for index, (_value, iid) in enumerate(rows):
            self.tree.move(iid, "", index)

    def _show_context_menu(self, event: Any) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.row_menu.tk_popup(event.x_root, event.y_root)

    def _open_selected_url(self, _event: Any = None) -> None:
        for iid in self.tree.selection():
            match = self.matches.get(iid)
            if match:
                webbrowser.open(match.job.target_url)

    # --- marking manual applications ---

    def _selected_matches(self) -> list[MatchResult]:
        return [self.matches[iid] for iid in self.tree.selection() if iid in self.matches]

    def on_mark_applied(self) -> None:
        """Record that you applied to these yourself — saved to the database."""
        selected = self._selected_matches()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return
        if not messagebox.askyesno(
            "Mark as applied",
            f"Mark {len(selected)} job(s) as applied by you?\n\n"
            "They will be saved to the database and never applied to automatically.",
        ):
            return

        pipeline = self.pipeline()
        for match in selected:
            pipeline.record_manual_application(match.job, "marked as applied from the app")
            self._append_log(f"marked applied: {match.job.title} — {match.job.company}", "ok")

        self._refresh_tree()
        self._set_status(f"Marked {len(selected)} job(s) as applied")

    def on_mark_irrelevant(self) -> None:
        """Teach the matcher: these aren't what you want."""
        selected = self._selected_matches()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return
        if not messagebox.askyesno(
            "Not relevant",
            f"Mark {len(selected)} job(s) as not relevant?\n\n"
            "They'll be hidden from future searches, and their company and title words "
            "will push similar jobs down the rankings.",
        ):
            return

        pipeline = self.pipeline()
        for match in selected:
            pipeline.record_irrelevant(match.job, "marked not relevant from the app")
            self._append_log(f"not relevant: {match.job.title} — {match.job.company}", "warn")

        self._refresh_tree()
        self._report_learning()

    def _report_learning(self) -> None:
        """Tell the user what the feedback has actually taught it so far."""
        if not self.store:
            return
        signals = self.store.feedback_signals()
        companies = [c for c, n in signals["companies"].items() if n >= 2]
        terms = [t for t, n in signals["title_terms"].items() if n >= 3]
        summary = f"Learned from {signals['total']} rejected job(s)."
        if companies:
            summary += f" Now filtering: {', '.join(sorted(companies)[:5])}."
        if terms:
            summary += f" Down-ranking titles with: {', '.join(sorted(terms)[:5])}."
        self._append_log(summary, "ok")
        self._set_status(summary)

    def on_undo_mark(self) -> None:
        """Undo either kind of mark on the selected rows."""
        selected = self._selected_matches()
        if not selected or not self.store:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return

        removed = 0
        for match in selected:
            removed += self.store.clear_manual_mark(match.job)
            removed += self.store.clear_feedback(match.job)

        self._refresh_tree()
        if removed:
            self._set_status(f"Cleared {removed} mark(s)")
        else:
            messagebox.showinfo(
                "Nothing to clear",
                "None of those were marked by hand. Applications the tool actually "
                "submitted stay on the record.",
            )

    # --- applying ---

    def on_apply_selected(self) -> None:
        selected = self._selected_matches()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows first.")
            return
        self._start_apply(selected)

    def on_apply_all(self) -> None:
        pending = [
            self.matches[iid] for iid in self.tree.get_children("")
            if iid in self.matches and self._row_status(self.matches[iid])[1] == "new"
        ]
        if not pending:
            messagebox.showinfo("Nothing to do", "No new jobs in the current view.")
            return
        self._start_apply(pending)

    def _start_apply(self, matches: list[MatchResult]) -> None:
        if self.worker.busy:
            messagebox.showinfo("Busy", "A task is already running.")
            return

        self._push_run_settings()
        mode = APPLY_MODES.get(self.apply_mode_var.get(), "auto")
        dry = self.dry_run_var.get()

        if mode == "manual":
            description = "Each form is filled and left open in the browser for you to submit."
        elif dry:
            description = "DRY RUN — forms are filled and screenshotted, nothing is submitted."
        else:
            description = "LIVE — applications will actually be SUBMITTED."

        if not messagebox.askyesno("Confirm", f"Apply to {len(matches)} job(s)?\n\n{description}"):
            return

        self._begin(f"Applying to {len(matches)} job(s)…")
        cfg = self.cfg
        assert cfg is not None

        def task(worker: Worker) -> None:
            from ..browser import browser_from_config
            from ..pipeline import Pipeline

            pipeline = Pipeline(cfg, progress=lambda m: worker.send("progress", m))
            with browser_from_config(cfg) as browser:
                ctx = pipeline._base_context(browser, dry)
                ctx.mode = mode
                for match in matches:
                    if worker.cancelled:
                        worker.send("progress", "Cancelled.")
                        break
                    outcome = pipeline.apply_one(match, ctx)
                    worker.send("outcome", outcome)
                    if mode == "manual" and outcome.status == Status.FILLED:
                        # Hold the browser open so the user can check and submit.
                        worker.send("await_user", outcome)
                        worker.wait_for_user()

        self.worker.start(task)

    # --- worker plumbing ---

    def on_stop(self) -> None:
        """Ask the worker to stop. It finishes the request already in flight, then quits
        at the next checkpoint — so this is 'stopping', not an instant kill."""
        if not self.worker.busy:
            return
        self.worker.cancel()
        self.worker.resume()  # release a manual-review wait, if one is pending
        self.stop_btn.configure(text="Stopping…", state="disabled")
        self._append_log("Stop requested — finishing the current request…", "warn")

    def _begin(self, message: str) -> None:
        self._set_status(message)
        self.progress.start(12)
        self.search_btn.configure(state="disabled")
        self.apply_sel_btn.configure(state="disabled")
        self.apply_all_btn.configure(state="disabled")
        self.stop_btn.configure(text="■  Stop", state="normal")

    def _end(self) -> None:
        self.progress.stop()
        self.search_btn.configure(state="normal")
        state = "normal" if self.matches else "disabled"
        self.apply_sel_btn.configure(state=state)
        self.apply_all_btn.configure(state=state)
        self.stop_btn.configure(text="■  Stop", state="normal")
        self._set_status("Ready")

    def _poll(self) -> None:
        for message in self.worker.drain():
            if message.kind == "progress":
                self._append_log(str(message.payload))
            elif message.kind == "batch":
                self._add_batch(message.payload)
            elif message.kind == "results":
                self._show_results(message.payload)
            elif message.kind == "outcome":
                self._refresh_tree()
            elif message.kind == "page_test":
                self._show_page_test(message.payload)
            elif message.kind == "await_user":
                self._prompt_manual_submit(message.payload)
            elif message.kind == "error":
                self._append_log(f"ERROR: {message.payload}", "err")
                messagebox.showerror("Task failed", str(message.payload))
            elif message.kind == "done":
                self._refresh_tree()
                self._end()
        self.after(150, self._poll)

    def _prompt_manual_submit(self, outcome: Any) -> None:
        job = outcome.job
        messagebox.showinfo(
            "Form filled — your turn",
            f"{job.title}\n{job.company}\n\n"
            "The application form is filled and open in the browser.\n"
            "Check it, submit it yourself, then click OK to continue.\n\n"
            "Afterwards use 'I applied to this myself' to record it.",
        )
        self.worker.resume()

    def _append_log(self, text: str, tag: str = "") -> None:
        lowered = text.lower()
        if not tag:
            if lowered.startswith("error") or "failed" in lowered:
                tag = "err"
            elif lowered.startswith("warning") or "note:" in lowered:
                tag = "warn"

        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", tag or ())
        self.log_text.see("end")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.configure(state="disabled")
        self._set_status(text[:110])

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)


def launch(config_path: str | None = None) -> int:
    app = JobHunterApp(config_path)
    app.mainloop()
    return 0
