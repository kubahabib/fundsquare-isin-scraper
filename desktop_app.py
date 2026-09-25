"""Desktop window for the fundsquare.net ISIN scraper. See README.md for how to run it."""
from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

import customtkinter as ctk

from scraper import PROBLEM_COLUMNS, RESULT_COLUMNS, parse_url_list, run_scraper

COLOR_BG = "#101614"
COLOR_CARD = "#1A221F"
COLOR_TEXT = "#E8EEEB"
COLOR_MUTED = "#9AABA3"
COLOR_ACCENT = "#2E8B57"
COLOR_HOVER = "#1F6B42"
COLOR_BORDER = "#2C3A34"
COLOR_INPUT = "#121A18"
COLOR_SUCCESS_BG = "#1A3328"
COLOR_SUCCESS_TEXT = "#D8F3E4"

HINT_FUND = (
    "Paste the fund-tree link of the fund (for example ...fund-tree?idInstr=114412). "
    "The app opens every sub-fund and collects all ISINs. Any folderId in the link is ignored."
)
HINT_SUB = (
    "On fundsquare.net click the sub-fund, then copy the link from the address bar "
    "(it contains folderId=...). One link = one sub-fund; you can paste several."
)


class ScraperApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Data Authority Paris ISIN Scraper Utility")
        self.minsize(900, 640)
        self.configure(fg_color=COLOR_BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.results = None
        self.problems = None
        self._worker: threading.Thread | None = None

        self._build()
        self._style_tables()
        self.update_idletasks()
        width = min(1100, max(900, self.winfo_screenwidth() - 80))
        height = min(960, max(640, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        if sys.platform == "win32":
            self.state("zoomed")

    def _build(self) -> None:
        self.scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=COLOR_BG,
            scrollbar_fg_color=COLOR_BG,
            scrollbar_button_color=COLOR_BORDER,
            scrollbar_button_hover_color=COLOR_ACCENT,
        )
        self.scroll.grid(row=0, column=0, sticky="nsew")

        header = ctk.CTkFrame(self.scroll, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(18, 0))
        ctk.CTkLabel(
            header,
            text="Data Authority Paris ISIN Scraper Utility",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color=COLOR_SUCCESS_TEXT,
            anchor="center",
        ).pack(fill="x")
        ctk.CTkLabel(
            self.scroll,
            text="Streamline the extraction of ISIN identifiers from fundsquare.net fund structures.",
            text_color=COLOR_MUTED,
            font=ctk.CTkFont(size=14),
            anchor="center",
        ).pack(fill="x", padx=28, pady=(4, 14))

        grid = ctk.CTkFrame(self.scroll, fg_color="transparent")
        grid.pack(fill="x", padx=28)
        grid.grid_columnconfigure((0, 1), weight=1, uniform="cards")

        config = self._card(grid, 0)
        ctk.CTkLabel(
            config, text="⚙  Scraping Configuration", font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_TEXT
        ).pack(anchor="w", padx=18, pady=(16, 8))
        ctk.CTkLabel(config, text="Identify data to extract?", text_color=COLOR_TEXT).pack(anchor="w", padx=18)
        self.mode = tk.StringVar(value="fund")
        for label, value in (("Whole fund structure", "fund"), ("Specific sub-funds", "sub")):
            ctk.CTkRadioButton(
                config,
                text=label,
                variable=self.mode,
                value=value,
                fg_color=COLOR_ACCENT,
                hover_color=COLOR_HOVER,
                text_color=COLOR_TEXT,
                command=self._update_hint,
            ).pack(anchor="w", padx=18, pady=4)
        self.hint = ctk.CTkLabel(
            config, text=HINT_FUND, text_color=COLOR_MUTED, wraplength=460, justify="left"
        )
        self.hint.pack(anchor="w", padx=18, pady=(10, 16))

        urls = self._card(grid, 1)
        ctk.CTkLabel(
            urls, text="🔗  Fund Tree URLs", font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_TEXT
        ).pack(anchor="w", padx=18, pady=(16, 8))
        self.textbox = ctk.CTkTextbox(
            urls, height=120, fg_color=COLOR_INPUT, border_color=COLOR_BORDER, border_width=1, text_color=COLOR_TEXT
        )
        self.textbox.pack(fill="x", padx=18)
        self.scrape_btn = ctk.CTkButton(
            urls,
            text="SCRAPE ISINs",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_HOVER,
            height=40,
            command=self.start_scraping,
        )
        self.scrape_btn.pack(fill="x", padx=18, pady=(12, 6))
        self.status = ctk.CTkLabel(urls, text="Ready", text_color=COLOR_MUTED, anchor="w")
        self.status.pack(fill="x", padx=18, pady=(0, 14))

        self.progress = ctk.CTkProgressBar(self.scroll, progress_color=COLOR_ACCENT, fg_color=COLOR_BORDER)
        self.progress.pack(fill="x", padx=28, pady=(14, 0))
        self.progress.set(0)

        self.results_card = ctk.CTkFrame(
            self.scroll, fg_color=COLOR_CARD, border_color=COLOR_BORDER, border_width=1, corner_radius=16
        )
        ctk.CTkLabel(self.results_card, text="📊  Results", text_color=COLOR_TEXT, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=18, pady=(14, 0)
        )
        ctk.CTkLabel(
            self.results_card,
            text="Results Summary",
            text_color=COLOR_SUCCESS_TEXT,
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(anchor="w", padx=18, pady=(0, 8))
        self.summary = ctk.CTkLabel(
            self.results_card,
            text="",
            fg_color=COLOR_SUCCESS_BG,
            text_color=COLOR_SUCCESS_TEXT,
            corner_radius=10,
            anchor="w",
            justify="left",
            wraplength=960,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.summary.pack(fill="x", padx=18, pady=(0, 8))
        self.details = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.results_card,
            text="Show fund, sub-fund, share class and source links",
            variable=self.details,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_HOVER,
            text_color=COLOR_TEXT,
            command=self._apply_view,
        ).pack(anchor="w", padx=18, pady=(0, 6))
        actions = ctk.CTkFrame(self.results_card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(4, 8))
        self.copy_btn = ctk.CTkButton(
            actions,
            text="Copy ISINs",
            width=160,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_HOVER,
            command=self.copy_isins,
            state="disabled",
        )
        self.copy_btn.pack(side="left", padx=(0, 8))
        self.csv_btn = ctk.CTkButton(
            actions,
            text="Download CSV",
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_HOVER,
            command=self.export_csv,
            state="disabled",
        )
        self.csv_btn.pack(side="left", fill="x", expand=True)

        self.view_host = ctk.CTkFrame(self.results_card, fg_color="transparent")
        self.view_host.pack(fill="both", expand=True, padx=18, pady=(2, 6))
        self.isin_wrap = ctk.CTkFrame(self.view_host, fg_color="transparent")
        self.isin_box = ctk.CTkTextbox(
            self.isin_wrap,
            height=280,
            fg_color=COLOR_INPUT,
            border_color=COLOR_BORDER,
            border_width=1,
            text_color=COLOR_TEXT,
            font=ctk.CTkFont(family="Consolas" if sys.platform == "win32" else "Menlo", size=15),
            activate_scrollbars=True,
        )
        self.isin_box.pack(fill="x")
        self.isin_box.bind("<Key>", self._keep_isin_readonly)

        self.table_wrap = ctk.CTkFrame(self.view_host, fg_color="transparent")
        self.tree = ttk.Treeview(self.table_wrap, columns=RESULT_COLUMNS, show="headings", height=10)
        for column, width in zip(RESULT_COLUMNS, (140, 280, 220, 220, 160)):
            self.tree.heading(column, text=column)
            self.tree.column(column, width=width, anchor="w")
        tree_scroll = ttk.Scrollbar(self.table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.problems_title = ctk.CTkLabel(
            self.results_card, text="Links with problems", font=ctk.CTkFont(size=16, weight="bold"), text_color=COLOR_TEXT
        )
        self.problems_wrap = ctk.CTkFrame(self.results_card, fg_color="transparent")
        self.problems_tree = ttk.Treeview(self.problems_wrap, columns=PROBLEM_COLUMNS, show="headings", height=4)
        for column, width in zip(PROBLEM_COLUMNS, (360, 100, 420)):
            self.problems_tree.heading(column, text=column)
            self.problems_tree.column(column, width=width, anchor="w")
        problem_scroll = ttk.Scrollbar(self.problems_wrap, orient="vertical", command=self.problems_tree.yview)
        self.problems_tree.configure(yscrollcommand=problem_scroll.set)
        self.problems_tree.pack(side="left", fill="both", expand=True)
        problem_scroll.pack(side="right", fill="y")

    def _card(self, parent, column: int) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, border_color=COLOR_BORDER, border_width=1, corner_radius=16)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 8 if column == 0 else 0))
        return card

    def _style_tables(self) -> None:
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure(
            "Treeview",
            background=COLOR_INPUT,
            foreground=COLOR_TEXT,
            fieldbackground=COLOR_INPUT,
            borderwidth=0,
            rowheight=26,
            font=("Menlo", 12),
        )
        style.map("Treeview", background=[("selected", COLOR_ACCENT)], foreground=[("selected", "#FFFFFF")])
        style.configure(
            "Treeview.Heading",
            background=COLOR_CARD,
            foreground=COLOR_MUTED,
            relief="flat",
            font=("Helvetica", 11, "bold"),
        )
        style.map("Treeview.Heading", background=[("active", COLOR_BORDER)])

    def _update_hint(self) -> None:
        self.hint.configure(text=HINT_SUB if self.mode.get() == "sub" else HINT_FUND)

    def _keep_isin_readonly(self, event) -> str | None:
        ctrl = bool(event.state & (0x4 | 0x8 | 0x10))
        if ctrl and event.keysym.lower() in {"c", "a"}:
            return None
        if event.keysym in {"Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End", "Shift_L", "Shift_R"}:
            return None
        return "break"

    def start_scraping(self) -> None:
        urls = parse_url_list(self.textbox.get("1.0", "end"))
        if not urls:
            messagebox.showwarning("Input", "Paste at least one link first.")
            return
        if self._worker and self._worker.is_alive():
            return
        self.scrape_btn.configure(state="disabled")
        self.csv_btn.configure(state="disabled")
        self.copy_btn.configure(state="disabled")
        self.progress.set(0)
        self.status.configure(text="Starting...")
        single = self.mode.get() == "sub"
        self._worker = threading.Thread(target=self._run, args=(urls, single), daemon=True)
        self._worker.start()

    def _run(self, urls: list[str], single: bool) -> None:
        def progress(fraction: float, message: str) -> None:
            self.after(0, lambda: self._on_progress(fraction, message))

        try:
            results, problems = run_scraper(urls, single_sub_fund=single, progress=progress)
        except Exception as exc:  # noqa: BLE001 — show unexpected failures in the window
            self.after(0, lambda: self._on_error(str(exc)))
            return
        self.after(0, lambda: self._on_done(results, problems, len(urls)))

    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress.set(max(0.0, min(float(fraction), 1.0)))
        self.status.configure(text=message)

    def _on_error(self, message: str) -> None:
        self.scrape_btn.configure(state="normal")
        self.status.configure(text="Error")
        messagebox.showerror("Scraper", message)

    def _on_done(self, results, problems, n_links: int) -> None:
        self.results = results
        self.problems = problems
        self.scrape_btn.configure(state="normal")
        self.progress.set(1)
        self.status.configure(text="Done")
        if not self.results_card.winfo_ismapped():
            self.results_card.pack(fill="x", padx=28, pady=(12, 18))

        n_failed = problems.loc[problems["Status"] == "failed", "URL"].nunique() if not problems.empty else 0
        if results.empty:
            self.summary.configure(
                text=f"  {self._empty_reason(problems)}",
                fg_color="#3A2424",
                text_color="#F0A8A8",
            )
            self.csv_btn.configure(state="disabled")
            self.copy_btn.configure(state="disabled")
        else:
            unique = int(results["ISIN"].nunique())
            self.summary.configure(
                text=f"  Found {unique} unique ISINs from {n_links - int(n_failed)} of {n_links} links.",
                fg_color=COLOR_SUCCESS_BG,
                text_color=COLOR_SUCCESS_TEXT,
            )
            self.csv_btn.configure(state="normal")
            self.copy_btn.configure(state="normal")
        self._fill_tables()
        self._apply_view()
        self._show_problems()
        self.after(30, self._reveal_results)

    def _empty_reason(self, problems) -> str:
        if problems is None or problems.empty:
            return "No ISINs found."
        partial = problems[problems["Status"] == "partial"]
        failed = problems[problems["Status"] == "failed"]
        chosen = partial if not partial.empty else failed
        if chosen.empty:
            return "No ISINs found."
        return f"No ISINs found. {chosen.iloc[0]['Details']}"

    def _fill_tables(self) -> None:
        isins = [] if self.results is None or self.results.empty else self.results["ISIN"].tolist()
        self.isin_box.configure(state="normal")
        self.isin_box.delete("1.0", "end")
        self.isin_box.insert("1.0", "\n".join(isins))
        inner = self.isin_box._textbox
        inner.tag_configure("center", justify="center")
        inner.tag_add("center", "1.0", "end")

        self.tree.delete(*self.tree.get_children())
        if self.results is not None:
            for row in self.results.itertuples(index=False):
                self.tree.insert("", "end", values=tuple(row))

        self.problems_tree.delete(*self.problems_tree.get_children())
        if self.problems is not None and not self.problems.empty:
            for row in self.problems.itertuples(index=False):
                self.problems_tree.insert("", "end", values=tuple(row))

    def _show_problems(self) -> None:
        has_problems = self.problems is not None and not self.problems.empty
        if has_problems:
            self.problems_title.pack(anchor="w", padx=18, pady=(8, 4))
            self.problems_wrap.pack(fill="both", expand=False, padx=18, pady=(0, 14))
        else:
            self.problems_title.pack_forget()
            self.problems_wrap.pack_forget()

    def _size_isin_list(self) -> None:
        """Give the ISIN box a fixed height of about ten lines. The page scrolls around it."""
        self.update_idletasks()
        if not self.isin_wrap.winfo_ismapped():
            return
        linespace = tkfont.Font(font=self.isin_box._textbox.cget("font")).metrics("linespace") or 18
        scaling = float(ctk.ScalingTracker.get_widget_scaling(self.isin_box)) or 1
        pixels = linespace * 10 + int(28 * scaling)
        self.isin_box.configure(height=max(1, int(pixels / scaling)))

    def _reveal_results(self) -> None:
        self.update_idletasks()
        canvas = self.scroll._parent_canvas
        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        if not bbox or bbox[3] <= bbox[1]:
            return
        canvas.yview_moveto(min(1.0, max(0.0, self.results_card.winfo_y() / (bbox[3] - bbox[1]))))

    def _apply_view(self) -> None:
        if self.details.get() and self.results is not None and not self.results.empty:
            self.isin_wrap.pack_forget()
            self.table_wrap.pack(fill="x", pady=(4, 8))
        else:
            self.table_wrap.pack_forget()
            if self.results is not None and not self.results.empty:
                self.isin_wrap.pack(fill="x", pady=(4, 8))
                self._size_isin_list()
            else:
                self.isin_wrap.pack_forget()

    def copy_isins(self) -> None:
        if self.results is None or self.results.empty:
            return
        text = "\n".join(self.results["ISIN"].tolist())
        self.clipboard_clear()
        self.clipboard_append(text)
        self.copy_btn.configure(text="Copied")
        self.after(1600, lambda: self.copy_btn.configure(text="Copy ISINs"))

    def export_csv(self) -> None:
        if self.results is None or self.results.empty:
            return
        columns = RESULT_COLUMNS if self.details.get() else ["ISIN"]
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="fundsquare_isins.csv",
            title="Save ISIN list",
        )
        if not path:
            return
        self.results[columns].to_csv(path, index=False, encoding="utf-8-sig")
        messagebox.showinfo("Saved", path)


if __name__ == "__main__":
    ScraperApp().mainloop()
