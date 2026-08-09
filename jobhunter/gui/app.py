"""Desktop UI.

Four tabs:
  Search          — find jobs, see which you've already applied to, apply to a selection
  My Details      — the saved form values used to fill company application forms
  Roles & Resumes — multiple roles, each with one or more resumes
  Activity        — live log

Everything long-running happens on a worker thread (see worker.py); the UI only ever
reads from a queue.
"""

from __future__ import annotations

import logging
import queue
import webbrowser
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..config import Config, ConfigError, load_config
from ..models import MatchResult, Status
from ..store import Store
from .worker import QueueLogHandler, Worker

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "New",
    Status.APPLIED.value: "✓ Applied",
    Status.ALREADY_APPLIED.value: "✓ Applied",
    Status.MANUAL.value: "⚠ Manual",
    Status.FAILED.value: "⚠ Failed",
    Status.DRY_RUN.value: "· Dry run",
    Status.SKIPPED.value: "· Skipped",
}

# Fields of the "predefined form" — label, config key, and width hint.
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
        self.geometry("1180x760")
        self.minsize(900, 600)

        self.worker = Worker()
        self.matches: dict[str, MatchResult] = {}
        self.config_path = config_path
        self.cfg: Config | None = None
        self.store: Store | None = None

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
            self._set_status(f"Loaded {self.cfg.path}")
            self._populate_profile()
            self._populate_roles()
        except ConfigError as exc:
            self.cfg = None
            self._set_status(f"No config: {exc}")
            if initial:
                messagebox.showwarning(
                    "No configuration",
                    f"{exc}\n\nCopy config/config.example.yaml to config/config.yaml, "
                    "then fill in the 'My Details' and 'Roles & Resumes' tabs.",
                )

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        self._build_search_tab()
        self._build_profile_tab()
        self._build_roles_tab()
        self._build_log_tab()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=6)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status_var, foreground="#444").pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=170)
        self.progress.pack(side="right")

    # --- tab 1: search ---

    def _build_search_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Search  ")

        controls = ttk.Frame(tab)
        controls.pack(fill="x", padx=10, pady=10)

        self.search_btn = ttk.Button(controls, text="🔍  Search jobs", command=self.on_search)
        self.search_btn.pack(side="left")

        self.apply_sel_btn = ttk.Button(
            controls, text="Apply to selected", command=self.on_apply_selected, state="disabled"
        )
        self.apply_sel_btn.pack(side="left", padx=(8, 0))

        self.apply_all_btn = ttk.Button(
            controls, text="Apply to all new", command=self.on_apply_all, state="disabled"
        )
        self.apply_all_btn.pack(side="left", padx=(8, 0))

        self.dry_run_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls, text="Dry run (fill forms, don't submit)", variable=self.dry_run_var
        ).pack(side="left", padx=(16, 0))

        self.hide_applied_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls, text="Hide already applied", variable=self.hide_applied_var,
            command=self._refresh_tree,
        ).pack(side="left", padx=(12, 0))

        ttk.Button(controls, text="Stop", command=self.worker.cancel).pack(side="right")

        filter_row = ttk.Frame(tab)
        filter_row.pack(fill="x", padx=10)
        ttk.Label(filter_row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh_tree())
        ttk.Entry(filter_row, textvariable=self.filter_var, width=44).pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="No results yet")
        ttk.Label(filter_row, textvariable=self.count_var, foreground="#666").pack(side="left", padx=12)

        columns = ("status", "score", "title", "company", "location", "role", "resume", "source")
        widths = (92, 55, 300, 150, 175, 130, 110, 90)

        wrap = ttk.Frame(tab)
        wrap.pack(fill="both", expand=True, padx=10, pady=10)

        self.tree = ttk.Treeview(wrap, columns=columns, show="headings", selectmode="extended")
        for col, width in zip(columns, widths):
            self.tree.heading(col, text=col.title(), command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor="w")

        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("applied", background="#e8f5e9")
        self.tree.tag_configure("manual", background="#fff4e5")
        self.tree.tag_configure("new", background="#ffffff")

        self.tree.bind("<Double-1>", self._open_selected_url)
        ttk.Label(
            tab, text="Double-click a row to open the posting in your browser.",
            foreground="#666",
        ).pack(anchor="w", padx=10, pady=(0, 8))

    # --- tab 2: the predefined form ---

    def _build_profile_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  My Details  ")

        ttk.Label(
            tab,
            text="These values are typed into company application forms. Keep them accurate — "
                 "they are submitted as fact on your behalf.",
            foreground="#555", wraplength=1050,
        ).pack(anchor="w", padx=14, pady=(12, 8))

        canvas = tk.Canvas(tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(14, 0))
        scrollbar.pack(side="right", fill="y")

        self.profile_vars: dict[str, tk.StringVar] = {}

        grid = ttk.LabelFrame(inner, text="Personal details")
        grid.pack(fill="x", pady=(0, 12), padx=(0, 14))
        for index, (label, key) in enumerate(PROFILE_FIELDS):
            row, col = divmod(index, 2)
            cell = ttk.Frame(grid)
            cell.grid(row=row, column=col, sticky="w", padx=10, pady=5)
            ttk.Label(cell, text=label, width=22).pack(side="left")
            var = tk.StringVar()
            self.profile_vars[key] = var
            ttk.Entry(cell, textvariable=var, width=38).pack(side="left")

        choices = ttk.LabelFrame(inner, text="Standard screening answers")
        choices.pack(fill="x", pady=(0, 12), padx=(0, 14))
        for index, (label, key, options) in enumerate(CHOICE_FIELDS):
            row, col = divmod(index, 2)
            cell = ttk.Frame(choices)
            cell.grid(row=row, column=col, sticky="w", padx=10, pady=5)
            ttk.Label(cell, text=label, width=22).pack(side="left")
            var = tk.StringVar()
            self.profile_vars[key] = var
            ttk.Combobox(cell, textvariable=var, values=options, width=35, state="readonly").pack(side="left")

        files = ttk.LabelFrame(inner, text="Default files")
        files.pack(fill="x", pady=(0, 12), padx=(0, 14))
        self._file_row(files, "Default resume", "profile.resume_path")
        self._file_row(files, "Cover letter file", "profile.cover_letter_path")

        answers = ttk.LabelFrame(inner, text="Custom question answers  (one per line:  question fragment = answer)")
        answers.pack(fill="both", expand=True, pady=(0, 12), padx=(0, 14))
        ttk.Label(
            answers,
            text="The left side is matched as a substring of the question on the form. "
                 "Anything a form requires that isn't answered here sends the job to your manual list "
                 "instead of being guessed at.",
            foreground="#555", wraplength=980,
        ).pack(anchor="w", padx=10, pady=(6, 4))
        self.answers_text = tk.Text(answers, height=10, wrap="word")
        self.answers_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        buttons = ttk.Frame(inner)
        buttons.pack(fill="x", pady=(0, 20), padx=(0, 14))
        ttk.Button(buttons, text="💾  Save details", command=self.on_save_profile).pack(side="left")
        ttk.Button(buttons, text="Reload from file", command=lambda: self._load_config()).pack(side="left", padx=8)

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
            foreground="#555", wraplength=1050,
        ).pack(anchor="w", padx=14, pady=(12, 8))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 14))
        ttk.Label(left, text="Roles").pack(anchor="w")
        self.roles_list = tk.Listbox(left, width=26, height=20, exportselection=False)
        self.roles_list.pack(fill="y", expand=True)
        self.roles_list.bind("<<ListboxSelect>>", lambda _e: self._show_role())

        role_btns = ttk.Frame(left)
        role_btns.pack(fill="x", pady=6)
        ttk.Button(role_btns, text="Add", width=8, command=self.on_add_role).pack(side="left")
        ttk.Button(role_btns, text="Delete", width=8, command=self.on_delete_role).pack(side="left", padx=4)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        name_row = ttk.Frame(right)
        name_row.pack(fill="x", pady=(0, 8))
        ttk.Label(name_row, text="Role name", width=16).pack(side="left")
        self.role_name_var = tk.StringVar()
        ttk.Entry(name_row, textvariable=self.role_name_var, width=42).pack(side="left")
        self.role_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(name_row, text="Enabled", variable=self.role_enabled_var).pack(side="left", padx=12)

        ttk.Label(right, text="Job titles to look for (one per line)").pack(anchor="w")
        self.role_titles_text = tk.Text(right, height=6, wrap="word")
        self.role_titles_text.pack(fill="x", pady=(2, 10))

        ttk.Label(right, text="Extra keywords for this role (one per line)").pack(anchor="w")
        self.role_keywords_text = tk.Text(right, height=5, wrap="word")
        self.role_keywords_text.pack(fill="x", pady=(2, 10))

        ttk.Label(right, text="Resumes for this role").pack(anchor="w")
        self.role_resumes_list = tk.Listbox(right, height=6)
        self.role_resumes_list.pack(fill="x", pady=(2, 4))

        resume_btns = ttk.Frame(right)
        resume_btns.pack(fill="x")
        ttk.Button(resume_btns, text="Add resume…", command=self.on_add_resume).pack(side="left")
        ttk.Button(resume_btns, text="Remove", command=self.on_remove_resume).pack(side="left", padx=6)

        save_row = ttk.Frame(right)
        save_row.pack(fill="x", pady=14)
        ttk.Button(save_row, text="💾  Save roles", command=self.on_save_roles).pack(side="left")
        ttk.Button(save_row, text="Apply changes to this role", command=self._capture_role).pack(side="left", padx=8)

        self._roles_data: list[dict[str, Any]] = []
        self._current_role: int | None = None

    # --- tab 4: activity ---

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Activity  ")
        self.log_text = tk.Text(tab, wrap="word", state="disabled", height=30)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        vsb.pack(side="right", fill="y", pady=10)

    # --- config <-> widgets ---

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
            # Present the flat search.roles form as a single editable role.
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
        """Copy the editor widgets back into the in-memory role."""
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
        self._roles_data.append({"name": "New role", "titles": [], "keywords": [], "resumes": [], "enabled": True})
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
                messagebox.showerror(
                    "Missing titles", f"Role '{role.get('name')}' has no job titles."
                )
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
                # Never overwrite the shipped example — branch to the real config.
                target = Path("config/config.yaml")
            saved = self.cfg.save(target)
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

        problems = self.cfg.validate()
        blocking = [p for p in problems if "resume" in p.lower() or "Nothing to search" in p]
        if blocking:
            messagebox.showerror(
                "Configuration problem",
                "\n".join(blocking)
                + "\n\nFix this on the Roles & Resumes tab, then Save roles.",
            )
            return

        # Stale resume paths are survivable — say so in the log and carry on.
        for warning in self.cfg.warnings():
            self._append_log(f"NOTE: {warning}")

        self._begin("Searching…")
        self.tree.delete(*self.tree.get_children())
        self.matches.clear()

        cfg = self.cfg

        def task(worker: Worker) -> None:
            from ..browser import browser_from_config
            from ..pipeline import Pipeline

            pipeline = Pipeline(cfg, progress=lambda m: worker.send("progress", m))
            needs_browser = cfg.get("sources.naukri.enabled", False)

            if needs_browser:
                with browser_from_config(cfg) as browser:
                    matches, errors = pipeline.discover_and_match(browser, include_seen=True)
            else:
                matches, errors = pipeline.discover_and_match(None, include_seen=True)

            worker.send("results", (matches, errors))

        self.worker.start(task)

    def _show_results(self, payload: tuple[list[MatchResult], dict[str, str]]) -> None:
        matches, errors = payload
        self.matches = {f"m{i}": m for i, m in enumerate(matches)}
        self._refresh_tree()

        if errors:
            self._append_log("Some sources failed:")
            for name, err in errors.items():
                self._append_log(f"  ! {name}: {err}")

        state = "normal" if matches else "disabled"
        self.apply_sel_btn.configure(state=state)
        self.apply_all_btn.configure(state=state)

    def _row_status(self, match: MatchResult) -> tuple[str, str]:
        if not self.store:
            return "new", "new"
        status, _, _ = self.store.status_for(match.job)
        if status in (Status.APPLIED.value, Status.ALREADY_APPLIED.value):
            return status, "applied"
        if status in (Status.MANUAL.value, Status.FAILED.value):
            return status, "manual"
        return status, "new"

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        needle = self.filter_var.get().strip().lower()
        hide_applied = self.hide_applied_var.get()
        shown = 0

        for iid, match in self.matches.items():
            status, tag = self._row_status(match)
            if hide_applied and tag == "applied":
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
                    match.role_name,
                    match.resume_label or "default",
                    job.source,
                ),
            )
            shown += 1

        applied = sum(1 for m in self.matches.values() if self._row_status(m)[1] == "applied")
        self.count_var.set(f"{shown} shown · {len(self.matches)} matched · {applied} already applied")

    def _sort_by(self, column: str) -> None:
        rows = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]
        rows.sort(reverse=column == "score")
        for index, (_value, iid) in enumerate(rows):
            self.tree.move(iid, "", index)

    def _open_selected_url(self, _event: Any = None) -> None:
        for iid in self.tree.selection():
            match = self.matches.get(iid)
            if match:
                webbrowser.open(match.job.target_url)

    # --- applying ---

    def on_apply_selected(self) -> None:
        selected = [self.matches[iid] for iid in self.tree.selection() if iid in self.matches]
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

        dry = self.dry_run_var.get()
        mode = "DRY RUN — forms filled, nothing submitted" if dry else "LIVE — applications will be SUBMITTED"
        if not messagebox.askyesno(
            "Confirm", f"Apply to {len(matches)} job(s)?\n\nMode: {mode}"
        ):
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
                for match in matches:
                    if worker.cancelled:
                        worker.send("progress", "Cancelled.")
                        break
                    outcome = pipeline.apply_one(match, ctx)
                    worker.send("outcome", outcome)

        self.worker.start(task)

    # --- worker plumbing ---

    def _begin(self, message: str) -> None:
        self._set_status(message)
        self.progress.start(12)
        self.search_btn.configure(state="disabled")
        self.apply_sel_btn.configure(state="disabled")
        self.apply_all_btn.configure(state="disabled")

    def _end(self) -> None:
        self.progress.stop()
        self.search_btn.configure(state="normal")
        state = "normal" if self.matches else "disabled"
        self.apply_sel_btn.configure(state=state)
        self.apply_all_btn.configure(state=state)
        self._set_status("Ready")

    def _poll(self) -> None:
        for message in self.worker.drain():
            if message.kind == "progress":
                self._append_log(str(message.payload))
            elif message.kind == "results":
                self._show_results(message.payload)
            elif message.kind == "outcome":
                self._refresh_tree()
            elif message.kind == "error":
                self._append_log(f"ERROR: {message.payload}")
                messagebox.showerror("Task failed", str(message.payload))
            elif message.kind == "done":
                self._refresh_tree()
                self._end()
        self.after(150, self._poll)

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        # Keep the pane bounded; a long run produces thousands of lines.
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
