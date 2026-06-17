from __future__ import annotations

import queue
import threading
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from lead_generator.planning.leads import DEFAULT_KEYWORDS, LeadSearchConfig, parse_keywords, run_lead_search


class DateSelector(ctk.CTkFrame):
    def __init__(self, master, label: str, initial: date) -> None:
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text=label, font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0,
            column=0,
            padx=(0, 10),
            sticky="w",
        )
        current_year = date.today().year
        years = [str(year) for year in range(current_year - 10, current_year + 2)]
        months = [f"{month:02d}" for month in range(1, 13)]
        days = [f"{day:02d}" for day in range(1, 32)]

        self.year = ctk.CTkOptionMenu(self, values=years, width=88)
        self.month = ctk.CTkOptionMenu(self, values=months, width=72)
        self.day = ctk.CTkOptionMenu(self, values=days, width=72)
        self.year.set(str(initial.year))
        self.month.set(f"{initial.month:02d}")
        self.day.set(f"{initial.day:02d}")

        self.year.grid(row=0, column=1, padx=4, sticky="e")
        self.month.grid(row=0, column=2, padx=4, sticky="e")
        self.day.grid(row=0, column=3, padx=(4, 0), sticky="e")

    def selected_date(self) -> date:
        return date(int(self.year.get()), int(self.month.get()), int(self.day.get()))


class LeadGeneratorApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Planning Lead Generator")
        self.geometry("1080x760")
        self.minsize(920, 680)
        self.configure(fg_color="#0b1020")

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_requested = False

        self._build_layout()
        self.after(100, self._poll_messages)

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        shell = ctk.CTkFrame(self, corner_radius=28, fg_color="#111827", border_width=1, border_color="#243044")
        shell.grid(row=0, column=0, padx=22, pady=22, sticky="nsew")
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(3, weight=1)
        shell.grid_rowconfigure(5, weight=1)

        header = ctk.CTkFrame(shell, fg_color="transparent")
        header.grid(row=0, column=0, padx=26, pady=(24, 12), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Planning Lead Generator",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color="#f8fafc",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text="Search council planning portals from a GeoJSON list and save matched PDF leads.",
            font=ctk.CTkFont(size=14),
            text_color="#9ca3af",
        ).grid(row=1, column=0, pady=(4, 0), sticky="w")

        file_panel = ctk.CTkFrame(shell, corner_radius=20, fg_color="#172033")
        file_panel.grid(row=1, column=0, padx=26, pady=12, sticky="ew")
        file_panel.grid_columnconfigure(1, weight=1)

        self.geojson_entry = self._path_row(
            file_panel,
            row=0,
            label="GeoJSON file",
            button_text="Browse file",
            command=self._choose_geojson,
        )
        self.output_entry = self._path_row(
            file_panel,
            row=1,
            label="Output location",
            button_text="Browse folder",
            command=self._choose_output,
        )

        controls = ctk.CTkFrame(shell, corner_radius=20, fg_color="#172033")
        controls.grid(row=2, column=0, padx=26, pady=12, sticky="ew")
        controls.grid_columnconfigure((0, 1), weight=1)
        default_end = date.today()
        default_start = default_end - timedelta(days=30)
        self.start_selector = DateSelector(controls, "Start date", default_start)
        self.end_selector = DateSelector(controls, "End date", default_end)
        self.start_selector.grid(row=0, column=0, padx=16, pady=16, sticky="ew")
        self.end_selector.grid(row=0, column=1, padx=16, pady=16, sticky="ew")

        keyword_panel = ctk.CTkFrame(shell, corner_radius=20, fg_color="#172033")
        keyword_panel.grid(row=3, column=0, padx=26, pady=12, sticky="nsew")
        keyword_panel.grid_columnconfigure(0, weight=1)
        keyword_panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            keyword_panel,
            text="Keywords",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#f8fafc",
        ).grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")
        self.keyword_box = ctk.CTkTextbox(
            keyword_panel,
            corner_radius=16,
            border_width=1,
            border_color="#334155",
            fg_color="#0f172a",
            text_color="#e5e7eb",
            height=180,
        )
        self.keyword_box.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="nsew")
        self.keyword_box.insert("1.0", "\n".join(DEFAULT_KEYWORDS))

        status_panel = ctk.CTkFrame(shell, corner_radius=20, fg_color="#172033")
        status_panel.grid(row=4, column=0, padx=26, pady=12, sticky="ew")
        status_panel.grid_columnconfigure(0, weight=1)
        self.progress_label = ctk.CTkLabel(status_panel, text="0 complete / 0 councils", text_color="#cbd5e1")
        self.progress_label.grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")
        self.progress_bar = ctk.CTkProgressBar(status_panel, height=14, corner_radius=10)
        self.progress_bar.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="ew")
        self.progress_bar.set(0)
        button_row = ctk.CTkFrame(status_panel, fg_color="transparent")
        button_row.grid(row=0, column=1, rowspan=2, padx=16, pady=14, sticky="e")
        self.run_button = ctk.CTkButton(
            button_row,
            text="Start search",
            corner_radius=16,
            height=42,
            command=self._start_run,
        )
        self.run_button.grid(row=0, column=0, padx=(0, 10))
        self.cancel_button = ctk.CTkButton(
            button_row,
            text="Cancel",
            corner_radius=16,
            height=42,
            fg_color="#374151",
            hover_color="#4b5563",
            state="disabled",
            command=self._cancel_run,
        )
        self.cancel_button.grid(row=0, column=1)

        log_panel = ctk.CTkFrame(shell, corner_radius=20, fg_color="#172033")
        log_panel.grid(row=5, column=0, padx=26, pady=(12, 24), sticky="nsew")
        log_panel.grid_columnconfigure(0, weight=1)
        log_panel.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            log_panel,
            text="Run log",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#f8fafc",
        ).grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")
        self.log_box = ctk.CTkTextbox(
            log_panel,
            corner_radius=16,
            border_width=1,
            border_color="#334155",
            fg_color="#020617",
            text_color="#d1d5db",
            height=150,
        )
        self.log_box.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="nsew")
        self.log_box.configure(state="disabled")

    def _path_row(self, parent, *, row: int, label: str, button_text: str, command) -> ctk.CTkEntry:
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=row,
            column=0,
            padx=(16, 12),
            pady=12,
            sticky="w",
        )
        entry = ctk.CTkEntry(parent, corner_radius=14, height=40, fg_color="#0f172a", border_color="#334155")
        entry.grid(row=row, column=1, padx=(0, 12), pady=12, sticky="ew")
        ctk.CTkButton(parent, text=button_text, corner_radius=14, height=40, command=command).grid(
            row=row,
            column=2,
            padx=(0, 16),
            pady=12,
            sticky="e",
        )
        return entry

    def _choose_geojson(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose council GeoJSON",
            filetypes=[("GeoJSON files", "*.geojson *.json"), ("All files", "*.*")],
        )
        if path:
            self._set_entry(self.geojson_entry, path)

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Choose output location")
        if path:
            self._set_entry(self.output_entry, path)

    def _set_entry(self, entry: ctk.CTkEntry, value: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, value)

    def _start_run(self) -> None:
        try:
            config = self._read_config()
        except ValueError as exc:
            messagebox.showerror("Check inputs", str(exc))
            return

        self.cancel_requested = False
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_bar.set(0)
        self._clear_log()
        self._append_log("Starting search...")

        self.worker = threading.Thread(target=self._run_worker, args=(config,), daemon=True)
        self.worker.start()

    def _read_config(self) -> LeadSearchConfig:
        geojson_path = Path(self.geojson_entry.get().strip())
        output_root = Path(self.output_entry.get().strip())
        if not geojson_path.exists():
            raise ValueError("Choose a valid GeoJSON file.")
        if not output_root.exists():
            raise ValueError("Choose a valid output folder.")
        start_date = self.start_selector.selected_date()
        end_date = self.end_selector.selected_date()
        if start_date > end_date:
            raise ValueError("Start date must be before or equal to end date.")
        keywords = parse_keywords(self.keyword_box.get("1.0", "end"))
        if not keywords:
            raise ValueError("Enter at least one keyword.")
        return LeadSearchConfig(
            geojson_path=geojson_path,
            output_root=output_root,
            start_date=start_date,
            end_date=end_date,
            keywords=keywords,
        )

    def _run_worker(self, config: LeadSearchConfig) -> None:
        try:
            result = run_lead_search(
                config,
                log=lambda message: self.messages.put(("log", message)),
                progress=lambda complete, total: self.messages.put(("progress", (complete, total))),
                should_cancel=lambda: self.cancel_requested,
            )
            self.messages.put(("done", result))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def _cancel_run(self) -> None:
        self.cancel_requested = True
        self._append_log("Cancelling after the current council...")
        self.cancel_button.configure(state="disabled")

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(str(payload))
            elif kind == "progress":
                completed, total = payload
                self._set_progress(int(completed), int(total))
            elif kind == "done":
                self._finish_run()
                self._append_log(f"Output folder: {payload.output_dir}")
                messagebox.showinfo("Search complete", f"Saved {payload.leads_found} leads.")
            elif kind == "error":
                self._finish_run()
                messagebox.showerror("Search failed", str(payload))
                self._append_log(f"Error: {payload}")
        self.after(100, self._poll_messages)

    def _set_progress(self, completed: int, total: int) -> None:
        self.progress_label.configure(text=f"{completed} complete / {total} councils")
        self.progress_bar.set(0 if total == 0 else completed / total)

    def _finish_run(self) -> None:
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    def _append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")


def main() -> None:
    app = LeadGeneratorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
