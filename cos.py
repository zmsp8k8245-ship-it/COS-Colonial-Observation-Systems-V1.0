import json
import os
import re
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

import requests
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

APP_NAME = "COS"
APP_FULL_NAME = "Colonial Observation Systems"
MAX_WORKERS = 8
BATCH_UPDATE_MS = 100
REQUEST_TIMEOUT = 5
CACHE_TTL_SECONDS = 21600
PROFILE_URL = "https://steamcommunity.com/profiles/{steam_id}"
STEAM_API_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
UID_RE = re.compile(r"\[U:1:(\d+)\]")
DEFAULT_NAME_COLOR = "#80c0ff"
DEFAULT_SWATCH_COLOR = "#3a3a3a"
UNTAGGED_CATEGORY = "Uncategorized"
ALL_CATEGORIES = "All"
FACTION_NONE = ""
FACTION_COLONIAL = "Colonial"
FACTION_WARDEN = "Warden"
FACTION_OPTIONS = [FACTION_NONE, FACTION_COLONIAL, FACTION_WARDEN]
FACTION_COLORS = {
    FACTION_COLONIAL: "#008000",
    FACTION_WARDEN: "#0000FF",
}


def steamid64(account_id: int) -> int:
    return 76561197960265728 + account_id


def normalize_color(value: str) -> str:
    color = str(value).strip() or DEFAULT_NAME_COLOR
    return color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else DEFAULT_NAME_COLOR


def color_for_faction(faction: str, fallback: str = DEFAULT_NAME_COLOR) -> str:
    return FACTION_COLORS.get(faction, fallback)


@dataclass
class Config:
    api_key: str
    log_path: str

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        api_key = str(raw.get("api_key", "")).strip()
        log_path = str(raw.get("log_path", "")).strip().rstrip(".")

        if not api_key:
            raise ValueError("Missing 'api_key' in config.json.")
        if not log_path:
            raise ValueError("Missing 'log_path' in config.json.")
        if not Path(log_path).exists():
            raise ValueError(f"Log file does not exist: {log_path}")

        return cls(api_key=api_key, log_path=log_path)


@dataclass
class PlayerAlias:
    nickname: str = ""
    color: str = DEFAULT_NAME_COLOR
    category: str = ""
    notes: str = ""
    faction: str = FACTION_NONE

    @classmethod
    def from_dict(cls, raw: dict) -> "PlayerAlias":
        return cls(
            nickname=str(raw.get("nickname", "")).strip(),
            color=normalize_color(raw.get("color", DEFAULT_NAME_COLOR)),
            category=str(raw.get("category", "")).strip(),
            notes=str(raw.get("notes", "")).strip(),
            faction=str(raw.get("faction", "")).strip() if str(raw.get("faction", "")).strip() in FACTION_OPTIONS else FACTION_NONE,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "nickname": self.nickname,
            "color": self.color,
            "category": self.category,
            "notes": self.notes,
            "faction": self.faction,
        }

    def is_tagged(self) -> bool:
        return any([self.nickname, self.category, self.notes, self.faction, self.color != DEFAULT_NAME_COLOR])


class AliasStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._aliases: dict[str, PlayerAlias] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            raw = {}

        aliases: dict[str, PlayerAlias] = {}
        for steam_id, value in raw.items():
            if isinstance(value, dict):
                aliases[str(steam_id)] = PlayerAlias.from_dict(value)

        with self._lock:
            self._aliases = aliases

    def get(self, steam_id: str) -> PlayerAlias:
        with self._lock:
            alias = self._aliases.get(steam_id)
        return alias if alias else PlayerAlias()

    def set(self, steam_id: str, alias: PlayerAlias) -> None:
        with self._lock:
            if alias.is_tagged():
                self._aliases[steam_id] = alias
            else:
                self._aliases.pop(steam_id, None)
        self.save()

    def save(self) -> None:
        with self._lock:
            payload = {key: value.to_dict() for key, value in sorted(self._aliases.items())}
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def categories(self) -> list[str]:
        with self._lock:
            values = sorted({alias.category for alias in self._aliases.values() if alias.category})
        return values

    def color_summary(self) -> dict[str, int]:
        with self._lock:
            summary: dict[str, int] = {}
            for alias in self._aliases.values():
                if alias.is_tagged():
                    summary[alias.color] = summary.get(alias.color, 0) + 1
        return summary

    def category_summary(self) -> dict[str, int]:
        with self._lock:
            summary: dict[str, int] = {}
            for alias in self._aliases.values():
                key = alias.category or UNTAGGED_CATEGORY
                if alias.is_tagged():
                    summary[key] = summary.get(key, 0) + 1
        return summary

    def faction_summary(self) -> dict[str, int]:
        with self._lock:
            summary: dict[str, int] = {}
            for alias in self._aliases.values():
                if alias.faction:
                    summary[alias.faction] = summary.get(alias.faction, 0) + 1
        return summary

    def export_to(self, target: Path) -> None:
        with self._lock:
            payload = {key: value.to_dict() for key, value in sorted(self._aliases.items())}
        with target.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def import_from(self, source: Path, merge: bool = True) -> int:
        with source.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        imported: dict[str, PlayerAlias] = {}
        for steam_id, value in raw.items():
            if isinstance(value, dict):
                alias = PlayerAlias.from_dict(value)
                if alias.is_tagged():
                    imported[str(steam_id)] = alias

        with self._lock:
            if merge:
                self._aliases.update(imported)
            else:
                self._aliases = imported
        self.save()
        return len(imported)


class SteamNameResolver:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get_name(self, steam_id: str) -> str:
        cached_name = self._get_cached_name(steam_id)
        if cached_name is not None:
            return cached_name

        try:
            response = requests.get(
                STEAM_API_URL,
                params={"key": self.api_key, "steamids": steam_id},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            players = response.json().get("response", {}).get("players", [])
            name = players[0].get("personaname", "<unavailable>") if players else "<unavailable>"
        except requests.RequestException:
            name = "<unavailable>"

        with self._lock:
            self._cache[steam_id] = (name, time.time())
        return name

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def _get_cached_name(self, steam_id: str) -> Optional[str]:
        with self._lock:
            cached = self._cache.get(steam_id)

        if not cached:
            return None

        name, timestamp = cached
        if time.time() - timestamp >= CACHE_TTL_SECONDS:
            return None
        return name


class LogWatcher(threading.Thread):
    def __init__(
        self,
        log_path: str,
        name_resolver: SteamNameResolver,
        update_queue: Queue,
        stop_event: threading.Event,
        max_workers: int = MAX_WORKERS,
    ):
        super().__init__(daemon=True)
        self.log_path = Path(log_path)
        self.name_resolver = name_resolver
        self.update_queue = update_queue
        self.stop_event = stop_event
        self.seen_accounts: set[str] = set()
        self.name_queue: Queue = Queue()
        self.workers = [
            threading.Thread(target=self._name_worker, daemon=True, name=f"name-worker-{index}")
            for index in range(max_workers)
        ]

    def run(self) -> None:
        for worker in self.workers:
            worker.start()

        try:
            with self.log_path.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(0, os.SEEK_END)
                self.update_queue.put(("__status__", f"Watching {self.log_path}"))

                while not self.stop_event.is_set():
                    line = handle.readline()
                    if not line:
                        time.sleep(0.05)
                        continue

                    account_id = self._extract_account_id(line)
                    if not account_id or account_id in self.seen_accounts:
                        continue

                    self.seen_accounts.add(account_id)
                    steam_id = str(steamid64(int(account_id)))
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    self.update_queue.put(("player", steam_id, "<loading>", timestamp))
                    self.name_queue.put(steam_id)
        except OSError as exc:
            self.update_queue.put(("__error__", f"Failed to open log file: {exc}"))

    def clear(self) -> None:
        self.seen_accounts.clear()

    def _extract_account_id(self, line: str) -> Optional[str]:
        if "IClientFriends::RequestUserInformation" not in line:
            return None
        match = UID_RE.search(line)
        return match.group(1) if match else None

    def _name_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                steam_id = self.name_queue.get(timeout=0.25)
            except Empty:
                continue

            name = self.name_resolver.get_name(steam_id)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.update_queue.put(("player", steam_id, name, timestamp))
            self.name_queue.task_done()


class ScrollableFrame(ttk.Frame):
    def __init__(self, container):
        super().__init__(container)
        self.canvas = tk.Canvas(self, bg="#1e1e1e", highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.inner_id, width=event.width))
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _bind_mousewheel(self, _event) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class AliasEditor(tk.Toplevel):
    def __init__(self, parent: tk.Misc, steam_id: str, steam_name: str, current_alias: PlayerAlias):
        super().__init__(parent)
        self.title(f"{APP_NAME} Tag Editor")
        self.configure(bg="#1e1e1e")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[PlayerAlias] = None
        self.nickname_var = tk.StringVar(value=current_alias.nickname)
        self.color_var = tk.StringVar(value=current_alias.color or DEFAULT_NAME_COLOR)
        self.category_var = tk.StringVar(value=current_alias.category)
        self.faction_var = tk.StringVar(value=current_alias.faction)
        self.notes_text: Optional[tk.Text] = None

        self._build_layout(steam_id, steam_name, current_alias.notes)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.wait_visibility()
        self.focus()

    def _build_layout(self, steam_id: str, steam_name: str, notes: str) -> None:
        body = tk.Frame(self, bg="#1e1e1e", padx=14, pady=14)
        body.pack(fill="both", expand=True)

        tk.Label(body, text=steam_name, fg="#ffffff", bg="#1e1e1e", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(body, text=steam_id, fg="#aaaaaa", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 10))

        tk.Label(body, text="Nickname", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        nickname_entry = tk.Entry(body, textvariable=self.nickname_var, width=36, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Segoe UI", 10))
        nickname_entry.pack(fill="x", pady=(4, 10))
        nickname_entry.focus_set()

        tk.Label(body, text="Category", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        tk.Entry(body, textvariable=self.category_var, width=36, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Segoe UI", 10)).pack(fill="x", pady=(4, 10))

        tk.Label(body, text="Faction", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        faction_menu = tk.OptionMenu(body, self.faction_var, *FACTION_OPTIONS)
        faction_menu.configure(bg="#2b2b2b", fg="#ffffff", activebackground="#3a3a3a", activeforeground="#ffffff", bd=0, highlightthickness=0, width=18)
        faction_menu["menu"].configure(bg="#2b2b2b", fg="#ffffff")
        faction_menu.pack(anchor="w", pady=(4, 10))

        tk.Label(body, text="Colour", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        color_row = tk.Frame(body, bg="#1e1e1e")
        color_row.pack(fill="x", pady=(4, 10))

        self.preview = tk.Label(color_row, width=3, bg=self.color_var.get(), relief="flat")
        self.preview.pack(side="left", padx=(0, 8), ipady=6)

        color_entry = tk.Entry(color_row, textvariable=self.color_var, width=12, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Consolas", 10))
        color_entry.pack(side="left")
        color_entry.bind("<KeyRelease>", lambda _event: self._refresh_preview())

        tk.Button(color_row, text="Pick", command=self.pick_color, bg="#3f7bd8", fg="#ffffff", activebackground="#5a92ea", activeforeground="#ffffff", bd=0, padx=10, cursor="hand2").pack(side="left", padx=(8, 0))

        tk.Label(body, text="Notes", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        self.notes_text = tk.Text(body, width=36, height=5, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Segoe UI", 10), wrap="word")
        self.notes_text.pack(fill="x", pady=(4, 12))
        self.notes_text.insert("1.0", notes)

        button_row = tk.Frame(body, bg="#1e1e1e")
        button_row.pack(fill="x")

        tk.Button(button_row, text="Reset", command=self.reset, bg="#555555", fg="#ffffff", activebackground="#6b6b6b", activeforeground="#ffffff", bd=0, padx=10, cursor="hand2").pack(side="left")
        tk.Button(button_row, text="Save", command=self.save, bg="#33aa55", fg="#ffffff", activebackground="#43bd66", activeforeground="#ffffff", bd=0, padx=12, cursor="hand2").pack(side="right")

    def _refresh_preview(self) -> None:
        color = self.color_var.get().strip()
        self.preview.configure(bg=color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else DEFAULT_SWATCH_COLOR)

    def pick_color(self) -> None:
        selected = colorchooser.askcolor(color=self.color_var.get(), parent=self)[1]
        if selected:
            self.color_var.set(selected)
            self._refresh_preview()

    def reset(self) -> None:
        self.nickname_var.set("")
        self.category_var.set("")
        self.faction_var.set(FACTION_NONE)
        self.color_var.set(DEFAULT_NAME_COLOR)
        if self.notes_text is not None:
            self.notes_text.delete("1.0", "end")
        self._refresh_preview()

    def save(self) -> None:
        color = normalize_color(self.color_var.get())
        if self.color_var.get().strip() and color != self.color_var.get().strip():
            messagebox.showerror(APP_NAME, "Colour must be a hex value like #ffcc00.", parent=self)
            return

        notes = self.notes_text.get("1.0", "end").strip() if self.notes_text is not None else ""
        faction = self.faction_var.get().strip()
        self.result = PlayerAlias(
            nickname=self.nickname_var.get().strip(),
            color=color_for_faction(faction, color),
            category=self.category_var.get().strip(),
            notes=notes,
            faction=faction,
        )
        self.destroy()

    def cancel(self) -> None:
        self.result = None
        self.destroy()


class BulkTagEditor(tk.Toplevel):
    def __init__(self, parent: tk.Misc, selected_count: int):
        super().__init__(parent)
        self.title(f"{APP_NAME} Bulk Tag Editor")
        self.configure(bg="#1e1e1e")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[dict[str, object]] = None
        self.category_var = tk.StringVar()
        self.faction_var = tk.StringVar(value=FACTION_NONE)
        self.color_var = tk.StringVar(value="")
        self.append_notes_var = tk.StringVar()
        self.clear_tags_var = tk.BooleanVar(value=False)

        self._build_layout(selected_count)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.wait_visibility()
        self.focus()

    def _build_layout(self, selected_count: int) -> None:
        body = tk.Frame(self, bg="#1e1e1e", padx=14, pady=14)
        body.pack(fill="both", expand=True)

        tk.Label(body, text=f"Apply changes to {selected_count} selected players", fg="#ffffff", bg="#1e1e1e", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 12))

        tk.Label(body, text="Category", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        tk.Entry(body, textvariable=self.category_var, width=36, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Segoe UI", 10)).pack(fill="x", pady=(4, 10))

        tk.Label(body, text="Faction", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        faction_menu = tk.OptionMenu(body, self.faction_var, *FACTION_OPTIONS)
        faction_menu.configure(bg="#2b2b2b", fg="#ffffff", activebackground="#3a3a3a", activeforeground="#ffffff", bd=0, highlightthickness=0, width=18)
        faction_menu["menu"].configure(bg="#2b2b2b", fg="#ffffff")
        faction_menu.pack(anchor="w", pady=(4, 10))

        tk.Label(body, text="Colour", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        color_row = tk.Frame(body, bg="#1e1e1e")
        color_row.pack(fill="x", pady=(4, 10))

        self.preview = tk.Label(color_row, width=3, bg=DEFAULT_SWATCH_COLOR, relief="flat")
        self.preview.pack(side="left", padx=(0, 8), ipady=6)

        color_entry = tk.Entry(color_row, textvariable=self.color_var, width=12, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Consolas", 10))
        color_entry.pack(side="left")
        color_entry.bind("<KeyRelease>", lambda _event: self._refresh_preview())

        tk.Button(color_row, text="Pick", command=self.pick_color, bg="#3f7bd8", fg="#ffffff", activebackground="#5a92ea", activeforeground="#ffffff", bd=0, padx=10, cursor="hand2").pack(side="left", padx=(8, 0))

        tk.Label(body, text="Append note", fg="#d8d8d8", bg="#1e1e1e", font=("Segoe UI", 9)).pack(anchor="w")
        tk.Entry(body, textvariable=self.append_notes_var, width=36, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Segoe UI", 10)).pack(fill="x", pady=(4, 10))

        tk.Checkbutton(
            body,
            text="Clear all tags on selected players",
            variable=self.clear_tags_var,
            bg="#1e1e1e",
            fg="#d0d0d0",
            activebackground="#1e1e1e",
            activeforeground="#ffffff",
            selectcolor="#1e1e1e",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 12))

        button_row = tk.Frame(body, bg="#1e1e1e")
        button_row.pack(fill="x")

        tk.Button(button_row, text="Cancel", command=self.cancel, bg="#555555", fg="#ffffff", activebackground="#6b6b6b", activeforeground="#ffffff", bd=0, padx=10, cursor="hand2").pack(side="left")
        tk.Button(button_row, text="Apply", command=self.save, bg="#33aa55", fg="#ffffff", activebackground="#43bd66", activeforeground="#ffffff", bd=0, padx=12, cursor="hand2").pack(side="right")

    def _refresh_preview(self) -> None:
        color = self.color_var.get().strip()
        self.preview.configure(bg=color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else DEFAULT_SWATCH_COLOR)

    def pick_color(self) -> None:
        selected = colorchooser.askcolor(color=self.color_var.get(), parent=self)[1]
        if selected:
            self.color_var.set(selected)
            self._refresh_preview()

    def save(self) -> None:
        color_input = self.color_var.get().strip()
        color = normalize_color(color_input) if color_input else ""
        if color_input and color != color_input:
            messagebox.showerror(APP_NAME, "Colour must be a hex value like #ffcc00.", parent=self)
            return

        self.result = {
            "category": self.category_var.get().strip(),
            "faction": self.faction_var.get().strip(),
            "color": color,
            "append_notes": self.append_notes_var.get().strip(),
            "clear_tags": self.clear_tags_var.get(),
        }
        self.destroy()

    def cancel(self) -> None:
        self.result = None
        self.destroy()


class RowWidget(ttk.Frame):
    def __init__(self, parent, steam_id: str, name: str, timestamp: str, alias: PlayerAlias, on_edit_alias):
        super().__init__(parent, style="Row.TFrame")
        self.steam_id = steam_id
        self.name = name
        self.timestamp = timestamp
        self.alias = alias
        self.on_edit_alias = on_edit_alias
        self.created_at = time.time()
        self.selected_var = tk.BooleanVar(value=False)

        self.select_box = tk.Checkbutton(
            self,
            variable=self.selected_var,
            bg="#1e1e1e",
            activebackground="#1e1e1e",
            selectcolor="#1e1e1e",
            highlightthickness=0,
            bd=0,
        )
        self.select_box.pack(side="left", padx=(2, 2))

        self.copy_button = tk.Button(self, text="/p", width=3, height=1, bg="#555555", fg="#ffffff", bd=0, activebackground="#777777", cursor="hand2", command=self.copy_private_message)
        self.copy_button.pack(side="left", padx=(2, 6), pady=1)

        self.swatch = tk.Label(self, width=2, bg=DEFAULT_SWATCH_COLOR)
        self.swatch.pack(side="left", padx=(0, 6), ipady=6)

        self.faction_badge = tk.Label(self, text="?", width=3, bg="#4b4b4b", fg="#ffffff", font=("Segoe UI", 8, "bold"))
        self.faction_badge.pack(side="left", padx=(0, 6), ipady=3)

        self.text_frame = tk.Frame(self, bg="#1e1e1e")
        self.text_frame.pack(side="left", fill="x", expand=True)

        self.name_var = tk.StringVar()
        self.name_label = tk.Label(self.text_frame, textvariable=self.name_var, fg=DEFAULT_NAME_COLOR, bg="#1e1e1e", font=("Segoe UI", 10), cursor="hand2", anchor="w")
        self.name_label.pack(anchor="w")
        self.name_label.bind("<Button-1>", lambda _event: self.open_profile())
        self.name_label.bind("<Enter>", lambda _event: self.name_label.configure(font=("Segoe UI", 10, "underline")))
        self.name_label.bind("<Leave>", lambda _event: self.name_label.configure(font=("Segoe UI", 10)))

        self.meta_var = tk.StringVar()
        self.meta_label = tk.Label(self.text_frame, textvariable=self.meta_var, fg="#9f9f9f", bg="#1e1e1e", font=("Segoe UI", 8), anchor="w")
        self.meta_label.pack(anchor="w")

        self.colonial_button = tk.Button(
            self,
            text="C",
            bg="#2f8f4e",
            fg="#ffffff",
            activebackground="#3eaa60",
            activeforeground="#ffffff",
            bd=0,
            width=2,
            cursor="hand2",
            command=lambda: self.on_edit_alias(self.steam_id, FACTION_COLONIAL),
        )
        self.colonial_button.pack(side="right", padx=(0, 4))

        self.warden_button = tk.Button(
            self,
            text="W",
            bg="#3f5fbf",
            fg="#ffffff",
            activebackground="#5474d3",
            activeforeground="#ffffff",
            bd=0,
            width=2,
            cursor="hand2",
            command=lambda: self.on_edit_alias(self.steam_id, FACTION_WARDEN),
        )
        self.warden_button.pack(side="right", padx=(0, 4))

        self.clear_faction_button = tk.Button(
            self,
            text="-",
            bg="#666666",
            fg="#ffffff",
            activebackground="#7a7a7a",
            activeforeground="#ffffff",
            bd=0,
            width=2,
            cursor="hand2",
            command=lambda: self.on_edit_alias(self.steam_id, FACTION_NONE),
        )
        self.clear_faction_button.pack(side="right", padx=(0, 6))

        self.edit_button = tk.Button(self, text="Edit", bg="#3f7bd8", fg="#ffffff", activebackground="#5a92ea", activeforeground="#ffffff", bd=0, padx=8, cursor="hand2", command=lambda: self.on_edit_alias(self.steam_id))
        self.edit_button.pack(side="right", padx=(0, 6))

        self.time_label = tk.Label(self, text=self.timestamp, fg="#aaaaaa", bg="#1e1e1e", font=("Segoe UI", 9))
        self.time_label.pack(side="right", padx=(8, 6))
        self.apply_alias(alias)

    def update_row(self, name: str, timestamp: str) -> None:
        self.name = name
        self.timestamp = timestamp
        self.time_label.configure(text=timestamp)
        self.apply_alias(self.alias)

    def apply_alias(self, alias: PlayerAlias) -> None:
        self.alias = alias
        self.name_var.set(self._truncate_name(self.display_name()))
        self.name_label.configure(fg=alias.color if alias.is_tagged() else DEFAULT_NAME_COLOR)
        self.swatch.configure(bg=alias.color if alias.is_tagged() else DEFAULT_SWATCH_COLOR)
        meta_parts = [self.steam_id]
        if alias.faction:
            meta_parts.append(alias.faction)
        if alias.category:
            meta_parts.append(alias.category)
        if alias.notes:
            meta_parts.append(self._truncate_name(alias.notes, 50))
        self.meta_var.set(" | ".join(meta_parts))
        self._apply_faction_badge(alias.faction)

    def copy_private_message(self) -> None:
        root = self.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append(f"/p {self.name} ")
        root.update_idletasks()

    def open_profile(self) -> None:
        webbrowser.open(PROFILE_URL.format(steam_id=self.steam_id))

    def display_name(self) -> str:
        return f"{self.alias.nickname} ({self.name})" if self.alias.nickname else self.name

    def search_blob(self) -> str:
        return " ".join([self.steam_id, self.name, self.alias.nickname, self.alias.category, self.alias.notes, self.alias.faction]).lower()

    def _apply_faction_badge(self, faction: str) -> None:
        if faction == FACTION_COLONIAL:
            self.faction_badge.configure(text="C", bg="#2f8f4e")
        elif faction == FACTION_WARDEN:
            self.faction_badge.configure(text="W", bg="#3f5fbf")
        else:
            self.faction_badge.configure(text="?", bg="#4b4b4b")

    @staticmethod
    def _truncate_name(name: str, limit: int = 64) -> str:
        return name if len(name) <= limit else f"{name[: limit - 3]}..."


class App(tk.Tk):
    def __init__(self, config: Config):
        super().__init__()
        self.title(f"{APP_NAME} - {APP_FULL_NAME}")
        self.geometry("860x700")
        self.configure(bg="#1e1e1e")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.status_var = tk.StringVar(value="Starting...")
        self.count_var = tk.StringVar(value="Players: 0")
        self.search_var = tk.StringVar()
        self.tagged_only_var = tk.BooleanVar(value=False)
        self.category_filter_var = tk.StringVar(value=ALL_CATEGORIES)
        self.sort_var = tk.StringVar(value="Tagged first")

        self.rows: dict[str, RowWidget] = {}
        self.update_queue: Queue = Queue()
        self.stop_event = threading.Event()
        self.alias_store = AliasStore(Path(__file__).resolve().parent / "aliases.json")
        self.name_resolver = SteamNameResolver(config.api_key)
        self.log_watcher = LogWatcher(config.log_path, self.name_resolver, self.update_queue, self.stop_event)

        self.legend_frame: Optional[tk.Frame] = None
        self.category_menu: Optional[tk.OptionMenu] = None

        self._configure_styles()
        self._build_layout(config.log_path)

        self.log_watcher.start()
        self.after(BATCH_UPDATE_MS, self.process_updates)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Row.TFrame", background="#1e1e1e")

    def _build_layout(self, log_path: str) -> None:
        topbar = tk.Frame(self, bg="#2b2b2b", height=44)
        topbar.pack(side="top", fill="x", padx=4, pady=4)

        tk.Label(topbar, text=APP_NAME, fg="#ffffff", bg="#2b2b2b", font=("Segoe UI", 12, "bold")).pack(side="left", padx=8)
        tk.Label(topbar, text=APP_FULL_NAME, fg="#d0d0d0", bg="#2b2b2b", font=("Segoe UI", 9)).pack(side="left", padx=(0, 12))
        tk.Label(topbar, text=Path(log_path).name, fg="#aaaaaa", bg="#2b2b2b", font=("Segoe UI", 9)).pack(side="left")

        tk.Button(topbar, text="Import Tags", command=self.import_aliases, bg="#555555", fg="#ffffff", activebackground="#666666", activeforeground="#ffffff", bd=0, padx=10, pady=4, cursor="hand2").pack(side="right", padx=4)
        tk.Button(topbar, text="Export Tags", command=self.export_aliases, bg="#555555", fg="#ffffff", activebackground="#666666", activeforeground="#ffffff", bd=0, padx=10, pady=4, cursor="hand2").pack(side="right", padx=4)
        tk.Button(topbar, text="Clear", command=self.clear_all, bg="#ff5555", fg="#ffffff", activebackground="#ff7777", activeforeground="#ffffff", font=("Segoe UI", 10, "bold"), bd=0, padx=12, pady=4, cursor="hand2").pack(side="right", padx=4)

        controls = tk.Frame(self, bg="#252525", padx=8, pady=8)
        controls.pack(side="top", fill="x", padx=4, pady=(0, 4))

        tk.Label(controls, text="Search", fg="#d0d0d0", bg="#252525", font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        search_entry = tk.Entry(controls, textvariable=self.search_var, bg="#2b2b2b", fg="#ffffff", insertbackground="#ffffff", relief="flat", font=("Segoe UI", 10))
        search_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.search_var.trace_add("write", lambda *_args: self.refresh_rows())

        tk.Label(controls, text="Category", fg="#d0d0d0", bg="#252525", font=("Segoe UI", 9)).grid(row=0, column=1, sticky="w")
        self.category_menu = tk.OptionMenu(controls, self.category_filter_var, ALL_CATEGORIES, command=lambda _value: self.refresh_rows())
        self.category_menu.configure(bg="#2b2b2b", fg="#ffffff", activebackground="#3a3a3a", activeforeground="#ffffff", bd=0, highlightthickness=0)
        self.category_menu["menu"].configure(bg="#2b2b2b", fg="#ffffff")
        self.category_menu.grid(row=1, column=1, sticky="ew", padx=(0, 8))

        tk.Label(controls, text="Sort", fg="#d0d0d0", bg="#252525", font=("Segoe UI", 9)).grid(row=0, column=2, sticky="w")
        sort_menu = tk.OptionMenu(controls, self.sort_var, "Tagged first", "Newest first", "Name", command=lambda _value: self.refresh_rows())
        sort_menu.configure(bg="#2b2b2b", fg="#ffffff", activebackground="#3a3a3a", activeforeground="#ffffff", bd=0, highlightthickness=0)
        sort_menu["menu"].configure(bg="#2b2b2b", fg="#ffffff")
        sort_menu.grid(row=1, column=2, sticky="ew", padx=(0, 8))

        tagged_check = tk.Checkbutton(controls, text="Tagged only", variable=self.tagged_only_var, command=self.refresh_rows, bg="#252525", fg="#d0d0d0", activebackground="#252525", activeforeground="#ffffff", selectcolor="#252525", font=("Segoe UI", 9))
        tagged_check.grid(row=1, column=3, sticky="w", padx=(0, 10))

        tk.Button(controls, text="Select Visible", command=self.select_visible, bg="#555555", fg="#ffffff", activebackground="#666666", activeforeground="#ffffff", bd=0, padx=10, cursor="hand2").grid(row=1, column=4, padx=(0, 6))
        tk.Button(controls, text="Clear Selection", command=self.clear_selection, bg="#555555", fg="#ffffff", activebackground="#666666", activeforeground="#ffffff", bd=0, padx=10, cursor="hand2").grid(row=1, column=5, padx=(0, 6))
        tk.Button(controls, text="Bulk Edit", command=self.bulk_edit_selected, bg="#3f7bd8", fg="#ffffff", activebackground="#5a92ea", activeforeground="#ffffff", bd=0, padx=12, cursor="hand2").grid(row=1, column=6)

        controls.grid_columnconfigure(0, weight=3)
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(2, weight=1)

        self.legend_frame = tk.Frame(self, bg="#202020", padx=8, pady=6)
        self.legend_frame.pack(side="top", fill="x", padx=4, pady=(0, 4))

        self.scroll_frame = ScrollableFrame(self)
        self.scroll_frame.pack(side="top", fill="both", expand=True, padx=4, pady=(0, 4))

        status_bar = tk.Frame(self, bg="#252525", height=28)
        status_bar.pack(side="bottom", fill="x")
        tk.Label(status_bar, textvariable=self.status_var, fg="#d0d0d0", bg="#252525", anchor="w", font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True, padx=8, pady=4)
        tk.Label(status_bar, textvariable=self.count_var, fg="#d0d0d0", bg="#252525", anchor="e", font=("Segoe UI", 9)).pack(side="right", padx=8, pady=4)

        self._refresh_category_options()
        self._refresh_legend()

    def process_updates(self) -> None:
        try:
            while True:
                message = self.update_queue.get_nowait()
                kind = message[0]

                if kind == "player":
                    _, steam_id, name, timestamp = message
                    if steam_id in self.rows:
                        self.rows[steam_id].update_row(name, timestamp)
                    else:
                        self.rows[steam_id] = RowWidget(self.scroll_frame.inner, steam_id, name, timestamp, self.alias_store.get(steam_id), self.edit_alias)
                    self.status_var.set(f"Last update: {timestamp}")
                    self.refresh_rows()
                elif kind == "__status__":
                    self.status_var.set(message[1])
                elif kind == "__error__":
                    self.status_var.set(message[1])
                    messagebox.showerror(APP_NAME, message[1])
        except Empty:
            pass

        if not self.stop_event.is_set():
            self.after(BATCH_UPDATE_MS, self.process_updates)

    def refresh_rows(self) -> None:
        query = self.search_var.get().strip().lower()
        tagged_only = self.tagged_only_var.get()
        category_filter = self.category_filter_var.get()

        ordered_rows = sorted(self.rows.values(), key=self._sort_key)

        visible_count = 0
        for row in ordered_rows:
            visible = self._matches_filters(row, query, tagged_only, category_filter)
            row.pack_forget()
            if visible:
                row.pack(fill="x", padx=2, pady=2)
                visible_count += 1

        self.count_var.set(f"Players: {visible_count}/{len(self.rows)}")
        self._refresh_legend()

    def _matches_filters(self, row: RowWidget, query: str, tagged_only: bool, category_filter: str) -> bool:
        if tagged_only and not row.alias.is_tagged():
            return False
        if category_filter != ALL_CATEGORIES:
            current_category = row.alias.category or UNTAGGED_CATEGORY
            if current_category != category_filter:
                return False
        if query and query not in row.search_blob():
            return False
        return True

    def _sort_key(self, row: RowWidget):
        mode = self.sort_var.get()
        if mode == "Newest first":
            return (-row.created_at, row.display_name().lower())
        if mode == "Name":
            return (row.display_name().lower(),)
        return (0 if row.alias.is_tagged() else 1, (row.alias.category or "zzz").lower(), row.display_name().lower())

    def _refresh_category_options(self) -> None:
        current = self.category_filter_var.get()
        categories = [ALL_CATEGORIES, UNTAGGED_CATEGORY, *self.alias_store.categories()]
        menu = self.category_menu["menu"]
        menu.delete(0, "end")
        for category in categories:
            menu.add_command(label=category, command=lambda value=category: self._set_category_filter(value))
        if current not in categories:
            self.category_filter_var.set(ALL_CATEGORIES)

    def _set_category_filter(self, value: str) -> None:
        self.category_filter_var.set(value)
        self.refresh_rows()

    def _refresh_legend(self) -> None:
        for child in self.legend_frame.winfo_children():
            child.destroy()

        category_summary = self.alias_store.category_summary()
        color_summary = self.alias_store.color_summary()
        faction_summary = self.alias_store.faction_summary()

        tk.Label(self.legend_frame, text="Legend", fg="#ffffff", bg="#202020", font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 10))
        if not category_summary and not color_summary and not faction_summary:
            tk.Label(self.legend_frame, text="No saved tags yet", fg="#9f9f9f", bg="#202020", font=("Segoe UI", 9)).pack(side="left")
            return

        for faction, count in list(sorted(faction_summary.items()))[:3]:
            bg = "#2f8f4e" if faction == FACTION_COLONIAL else "#3f5fbf"
            tk.Label(self.legend_frame, text=f"{faction}: {count}", fg="#ffffff", bg=bg, font=("Segoe UI", 8, "bold"), padx=8, pady=3).pack(side="left", padx=(0, 6))

        for category, count in list(sorted(category_summary.items()))[:5]:
            chip = tk.Label(self.legend_frame, text=f"{category}: {count}", fg="#e0e0e0", bg="#2f2f2f", font=("Segoe UI", 8), padx=8, pady=3)
            chip.pack(side="left", padx=(0, 6))

        for color, count in list(sorted(color_summary.items(), key=lambda item: item[0]))[:5]:
            wrapper = tk.Frame(self.legend_frame, bg="#202020")
            wrapper.pack(side="left", padx=(0, 6))
            tk.Label(wrapper, width=2, bg=color).pack(side="left", ipady=5)
            tk.Label(wrapper, text=str(count), fg="#e0e0e0", bg="#2f2f2f", font=("Segoe UI", 8), padx=6, pady=3).pack(side="left")

    def visible_rows(self) -> list[RowWidget]:
        query = self.search_var.get().strip().lower()
        tagged_only = self.tagged_only_var.get()
        category_filter = self.category_filter_var.get()
        return [row for row in self.rows.values() if self._matches_filters(row, query, tagged_only, category_filter)]

    def selected_ids(self) -> list[str]:
        return [steam_id for steam_id, row in self.rows.items() if row.selected_var.get()]

    def select_visible(self) -> None:
        for row in self.visible_rows():
            row.selected_var.set(True)
        self.status_var.set(f"Selected {len(self.selected_ids())} players")

    def clear_selection(self) -> None:
        for row in self.rows.values():
            row.selected_var.set(False)
        self.status_var.set("Cleared selection")

    def bulk_edit_selected(self) -> None:
        selected_ids = self.selected_ids()
        if not selected_ids:
            messagebox.showinfo(APP_NAME, "Select at least one player first.")
            return

        dialog = BulkTagEditor(self, len(selected_ids))
        self.wait_window(dialog)
        if dialog.result is None:
            return

        clear_tags = bool(dialog.result["clear_tags"])
        category = str(dialog.result["category"])
        faction = str(dialog.result["faction"])
        color = str(dialog.result["color"])
        append_notes = str(dialog.result["append_notes"])

        for steam_id in selected_ids:
            current = self.alias_store.get(steam_id)
            if clear_tags:
                updated = PlayerAlias()
            else:
                updated = PlayerAlias(
                    nickname=current.nickname,
                    color=color_for_faction(faction, color or current.color),
                    category=category or current.category,
                    notes=self._append_note(current.notes, append_notes),
                    faction=faction or current.faction,
                )
            self.alias_store.set(steam_id, updated)
            self.rows[steam_id].apply_alias(updated)

        self._refresh_category_options()
        self.refresh_rows()
        self.status_var.set(f"Bulk updated {len(selected_ids)} players")

    @staticmethod
    def _append_note(existing: str, new_note: str) -> str:
        if not new_note:
            return existing
        return f"{existing} | {new_note}" if existing else new_note

    def export_aliases(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Export aliases",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile="aliases-export.json",
        )
        if not target:
            return

        try:
            self.alias_store.export_to(Path(target))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Failed to export aliases: {exc}")
            return

        self.status_var.set(f"Exported aliases to {target}")

    def import_aliases(self) -> None:
        source = filedialog.askopenfilename(
            parent=self,
            title="Import aliases",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not source:
            return

        try:
            imported_count = self.alias_store.import_from(Path(source), merge=True)
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"Failed to import aliases: {exc}")
            return

        for steam_id, row in self.rows.items():
            row.apply_alias(self.alias_store.get(steam_id))

        self._refresh_category_options()
        self.refresh_rows()
        self.status_var.set(f"Imported {imported_count} aliases")

    def clear_all(self) -> None:
        for child in list(self.scroll_frame.inner.winfo_children()):
            child.pack_forget()
            child.destroy()
        self.rows.clear()
        self.log_watcher.clear()
        self.name_resolver.clear()
        self.count_var.set("Players: 0")
        self.status_var.set("Cleared results.")
        self._refresh_legend()

    def edit_alias(self, steam_id: str, faction_override: Optional[str] = None) -> None:
        row = self.rows[steam_id]
        if faction_override is not None:
            next_color = color_for_faction(faction_override, row.alias.color)
            if faction_override == FACTION_NONE and row.alias.color in FACTION_COLORS.values():
                next_color = DEFAULT_NAME_COLOR
            updated = PlayerAlias(
                nickname=row.alias.nickname,
                color=next_color,
                category=row.alias.category,
                notes=row.alias.notes,
                faction=faction_override,
            )
            self.alias_store.set(steam_id, updated)
            row.apply_alias(updated)
            self._refresh_category_options()
            self.refresh_rows()
            self.status_var.set(f"Set {steam_id} faction to {faction_override or 'Unknown'}")
            return

        dialog = AliasEditor(self, steam_id, row.name, self.alias_store.get(steam_id))
        self.wait_window(dialog)
        if dialog.result is None:
            return

        self.alias_store.set(steam_id, dialog.result)
        row.apply_alias(dialog.result)
        self._refresh_category_options()
        self.refresh_rows()
        self.status_var.set(f"Saved tag for {steam_id}")

    def on_close(self) -> None:
        self.stop_event.set()
        self.destroy()


def load_config(config_path: Path) -> Config:
    return Config.from_file(config_path)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "config.json"

    print("Run: steam://open/console")
    print("In Steam console: log_ipc 1")
    input("Press Enter when ready...")

    try:
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Config error: {exc}")
        input("Press Enter to exit...")
        return

    App(config).mainloop()


if __name__ == "__main__":
    main()
