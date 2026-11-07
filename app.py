"""Notification App.

Shows an always-on-top reminder popup at the times you choose (08:00 and 20:00
by default). The popup sits above every other window, including games running
in borderless or windowed mode, and stays up until you click "Done" or snooze it.

    pythonw app.py              run the scheduler (install.ps1 does this at logon)
    pythonw app.py --settings   open the settings window
    pythonw app.py --now        show the popup right away (to check it works)

Settings live in config.json next to this file. The running scheduler re-reads
it within a few seconds of a change, so there is nothing to restart.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import tkinter as tk
import winsound
from ctypes import wintypes
from datetime import datetime, time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "app.log"

CHECK_MS = 5_000  # how often the scheduler looks at the clock and config.json
MUTEX_NAME = "Local\\NotificationApp-Scheduler"

DEFAULTS: dict = {
    "times": ["08:00", "20:00"],
    "title": "Take your meds!",
    "message": "Time to take your medication now.",
    "snooze_minutes": 10,
    "repeat_seconds": 40,  # re-raise the popup and play the sound this often
    "sound": True,
    "grab_focus": False,  # pull focus from the game (alt-tabs exclusive fullscreen)
}
LIMITS = {"snooze_minutes": (1, 240), "repeat_seconds": (5, 600)}

# Popup colours
BG, FG, MUTED = "#1e1f24", "#f2f2f5", "#a9abb5"
ACCENT, ACCENT_ACTIVE, BUTTON, BUTTON_ACTIVE = "#4f8cff", "#3b73db", "#33353d", "#44464f"
FONT = "Segoe UI"


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

GA_ROOT = 2
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000

_mutex = None  # held for the life of the scheduler process


def claim_single_instance() -> bool:
    """True if this is the only scheduler running; False if one already is."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def scheduler_running() -> bool:
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def make_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def force_foreground(hwnd: int) -> None:
    """Take focus from whatever is in front (a game, usually).

    Windows only lets the foreground thread hand out focus, so briefly attach
    to its input queue to borrow that right.
    """
    foreground = user32.GetForegroundWindow()
    if not foreground or foreground == hwnd:
        return
    their_thread = user32.GetWindowThreadProcessId(foreground, None)
    our_thread = kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(our_thread, their_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(our_thread, their_thread, False)


# ---------------------------------------------------------------------------
# Settings file
# ---------------------------------------------------------------------------


def parse_time(text: object) -> time | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError:
        return None


def normalize(raw: object) -> dict:
    """Fill in defaults and clamp anything out of range."""
    config = dict(DEFAULTS)
    if isinstance(raw, dict):
        config.update({key: raw[key] for key in DEFAULTS if key in raw})

    raw_times = config["times"] if isinstance(config["times"], list) else DEFAULTS["times"]
    times = {parse_time(t) for t in raw_times} - {None}
    config["times"] = sorted(t.strftime("%H:%M") for t in times)

    for key, (low, high) in LIMITS.items():
        try:
            value = int(config[key])
        except (TypeError, ValueError):
            value = DEFAULTS[key]
        config[key] = max(low, min(high, value))

    for key in ("sound", "grab_focus"):
        config[key] = bool(config[key])
    for key in ("title", "message"):
        config[key] = str(config[key]).strip() or DEFAULTS[key]
    return config


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
        return dict(DEFAULTS)
    try:
        return normalize(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        logging.exception("Could not read %s, using defaults", CONFIG_PATH)
        return dict(DEFAULTS)


def save_config(config: dict) -> None:
    # Write then rename, so the scheduler never reads a half-written file.
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(normalize(config), indent=2) + "\n", encoding="utf-8")
    os.replace(temp, CONFIG_PATH)


def config_mtime() -> float | None:
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Schedule maths
# ---------------------------------------------------------------------------


def alert_times(config: dict) -> list[time]:
    return [t for t in map(parse_time, config["times"]) if t is not None]


def next_alert(now: datetime, times: list[time]) -> datetime | None:
    """The soonest alert time strictly after `now`."""
    candidates = [
        datetime.combine(now.date() + timedelta(days=day), t)
        for day in (0, 1)
        for t in times
    ]
    return min((c for c in candidates if c > now), default=None)


def alert_due(start: datetime, end: datetime, times: list[time]) -> bool:
    """Did any alert time fall in (start, end]? Covers sleep/resume gaps too."""
    if end <= start:
        return False
    day = start.date()
    while day <= end.date():
        if any(start < datetime.combine(day, t) <= end for t in times):
            return True
        day += timedelta(days=1)
    return False


# ---------------------------------------------------------------------------
# The popup
# ---------------------------------------------------------------------------


class Popup(tk.Toplevel):
    """Borderless always-on-top reminder that stays until Done or Snooze."""

    def __init__(
        self,
        master: tk.Misc,
        settings: dict,
        on_done: Callable[[], None],
        on_snooze: Callable[[], None],
    ) -> None:
        super().__init__(master)
        self.settings = settings
        self.on_done = on_done
        self.on_snooze = on_snooze
        self.alive = True
        self.nags = 0
        self._drag_offset = (0, 0)

        self.overrideredirect(True)  # no title bar, so it can't be minimised
        self.attributes("-topmost", True)
        self.configure(bg=ACCENT)  # shows through as a thin accent border

        body = tk.Frame(self, bg=BG, padx=32, pady=24)
        body.pack(padx=3, pady=3)

        title = tk.Label(
            body, text=settings["title"], font=(FONT, 22, "bold"), fg=FG, bg=BG,
            wraplength=480, justify="left",
        )
        title.pack(anchor="w")
        message = tk.Label(
            body, text=settings["message"], font=(FONT, 13), fg=MUTED, bg=BG,
            wraplength=480, justify="left",
        )
        message.pack(anchor="w", pady=(6, 0))
        self.clock = tk.Label(body, font=(FONT, 10), fg=MUTED, bg=BG)
        self.clock.pack(anchor="w", pady=(14, 20))

        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x")
        done = tk.Button(
            buttons, text="Done  ✓", command=self.done, font=(FONT, 12, "bold"),
            bg=ACCENT, fg="white", activebackground=ACCENT_ACTIVE, activeforeground="white",
            relief="flat", bd=0, padx=22, pady=8, cursor="hand2",
        )
        done.pack(side="right")
        snooze = tk.Button(
            buttons, text=f"Snooze {settings['snooze_minutes']} min", command=self.snooze,
            font=(FONT, 12), bg=BUTTON, fg=FG, activebackground=BUTTON_ACTIVE,
            activeforeground=FG, relief="flat", bd=0, padx=18, pady=8, cursor="hand2",
        )
        snooze.pack(side="right", padx=(0, 10))

        # The window has no title bar, so let it be dragged from anywhere else.
        for widget in (self, body, title, message, self.clock, buttons):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
        self.bind("<Return>", lambda _: self.done())

        self._place()
        # Let Tk map the window at its position first: forcing it visible via
        # SetWindowPos before then pins it to the top-left corner.
        self.after(250, self._keep_on_top)
        self.after(300, self._nag)

    def _place(self) -> None:
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = (self.winfo_screenwidth() - width) // 2
        y = max(40, (self.winfo_screenheight() - height) // 3)
        self.geometry(f"+{x}+{y}")

    def _hwnd(self) -> int:
        return user32.GetAncestor(self.winfo_id(), GA_ROOT) or 0

    def _keep_on_top(self) -> None:
        """Games and other topmost windows can push us down; climb back every second."""
        if not self.alive:
            return
        self.attributes("-topmost", True)
        self.lift()
        hwnd = self._hwnd()
        if hwnd:
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        self.clock.configure(text=datetime.now().strftime("It's %H:%M"))
        self.after(1000, self._keep_on_top)

    def _nag(self) -> None:
        """Sound (and optionally grab focus) now, then again every repeat_seconds."""
        if not self.alive:
            return
        if self.nags:
            logging.info("Alert not acknowledged, repeating")
        self.nags += 1
        if self.settings["sound"]:
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
        if self.settings["grab_focus"]:
            hwnd = self._hwnd()
            if hwnd:
                force_foreground(hwnd)
            self.focus_force()
        self.after(self.settings["repeat_seconds"] * 1000, self._nag)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag(self, event: tk.Event) -> None:
        dx, dy = self._drag_offset
        self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _close(self) -> None:
        self.alive = False
        self.destroy()

    def done(self) -> None:
        self._close()
        self.on_done()

    def snooze(self) -> None:
        self._close()
        self.on_snooze()


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    def __init__(self, root: tk.Tk, fire_now: bool) -> None:
        self.root = root
        self.settings = load_config()
        self.mtime = config_mtime()
        self.popup: Popup | None = None
        self.snooze_job: str | None = None
        self.last_check = datetime.now()
        self.announced: datetime | None = None
        logging.info("Alerts at %s", ", ".join(self.settings["times"]) or "(none)")

        if fire_now:
            self.show()
        self.tick()

    def reload_if_changed(self) -> None:
        mtime = config_mtime()
        if mtime != self.mtime:
            self.mtime = mtime
            self.settings = load_config()
            logging.info(
                "Settings changed; alerts at %s", ", ".join(self.settings["times"]) or "(none)"
            )

    def tick(self) -> None:
        try:
            self.reload_if_changed()
            now = datetime.now()
            times = alert_times(self.settings)
            idle = self.popup is None and self.snooze_job is None
            if idle and alert_due(self.last_check, now, times):
                logging.info("Alert due")
                self.show()
            self.last_check = now

            upcoming = next_alert(now, times)
            if upcoming != self.announced:
                self.announced = upcoming
                logging.info("Next alert at %s", upcoming or "(no times set)")
        except Exception:
            logging.exception("Scheduler check failed")
        self.root.after(CHECK_MS, self.tick)

    def show(self) -> None:
        self.popup = Popup(self.root, self.settings, self.acknowledged, self.snoozed)

    def acknowledged(self) -> None:
        self.popup = None
        logging.info("Alert acknowledged")

    def snoozed(self) -> None:
        self.popup = None
        minutes = self.settings["snooze_minutes"]
        logging.info("Alert snoozed for %d min", minutes)
        self.snooze_job = self.root.after(minutes * 60_000, self.snooze_over)

    def snooze_over(self) -> None:
        self.snooze_job = None
        self.show()


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------


class SettingsWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.test_popup: Popup | None = None
        settings = load_config()

        root.title("Notification App Settings")
        root.resizable(False, False)
        style = ttk.Style(root)
        style.configure("Heading.TLabel", font=(FONT, 11, "bold"))
        style.configure("Hint.TLabel", foreground="#666")

        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        row = 0

        # --- Times -----------------------------------------------------------
        ttk.Label(frame, text="Reminder times", style="Heading.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.times = tk.Listbox(
            frame, height=6, width=10, font=(FONT, 12), activestyle="none",
            exportselection=False,
        )
        self.times.grid(row=row, column=0, rowspan=3, sticky="nsw", pady=(6, 0))
        for t in settings["times"]:
            self.times.insert("end", t)
        self.times.bind("<Delete>", lambda _: self.remove_time())

        picker = ttk.Frame(frame)
        picker.grid(row=row, column=1, sticky="nw", padx=(12, 0), pady=(6, 0))
        self.hour = ttk.Spinbox(picker, from_=0, to=23, width=4, format="%02.0f", wrap=True)
        self.hour.set("12")
        self.hour.pack(side="left")
        ttk.Label(picker, text=" : ").pack(side="left")
        self.minute = ttk.Spinbox(picker, from_=0, to=59, width=4, format="%02.0f", wrap=True)
        self.minute.set("00")
        self.minute.pack(side="left")
        ttk.Label(picker, text="  (24-hour)", style="Hint.TLabel").pack(side="left")
        ttk.Button(frame, text="Add time", command=self.add_time).grid(
            row=row + 1, column=1, sticky="nw", padx=(12, 0), pady=(8, 0)
        )
        ttk.Button(frame, text="Remove selected", command=self.remove_time).grid(
            row=row + 2, column=1, sticky="nw", padx=(12, 0), pady=(4, 0)
        )
        row += 3

        # --- Text ------------------------------------------------------------
        ttk.Label(frame, text="Popup text", style="Heading.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(18, 4)
        )
        row += 1
        self.title_var = tk.StringVar(value=settings["title"])
        self.message_var = tk.StringVar(value=settings["message"])
        for label, var in (("Title", self.title_var), ("Message", self.message_var)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=var, width=44).grid(
                row=row, column=1, sticky="ew", padx=(12, 0), pady=2
            )
            row += 1

        # --- Behaviour -------------------------------------------------------
        ttk.Label(frame, text="Behaviour", style="Heading.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(18, 4)
        )
        row += 1
        self.snooze_var = tk.StringVar(value=str(settings["snooze_minutes"]))
        self.repeat_var = tk.StringVar(value=str(settings["repeat_seconds"]))
        for label, var, (low, high), unit in (
            ("Snooze length", self.snooze_var, LIMITS["snooze_minutes"], "minutes"),
            ("Repeat sound every", self.repeat_var, LIMITS["repeat_seconds"], "seconds"),
        ):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            box = ttk.Frame(frame)
            box.grid(row=row, column=1, sticky="w", padx=(12, 0), pady=2)
            ttk.Spinbox(box, from_=low, to=high, textvariable=var, width=6).pack(side="left")
            ttk.Label(box, text=f"  {unit}").pack(side="left")
            row += 1

        self.sound_var = tk.BooleanVar(value=settings["sound"])
        self.focus_var = tk.BooleanVar(value=settings["grab_focus"])
        ttk.Checkbutton(frame, text="Play a sound", variable=self.sound_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(6, 2)
        )
        row += 1
        ttk.Checkbutton(
            frame, text="Grab focus from the game", variable=self.focus_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1
        ttk.Label(
            frame,
            text="Needed for games in exclusive fullscreen, which nothing can draw over.\n"
            "It alt-tabs you out to the popup, so leave it off if you use borderless.",
            style="Hint.TLabel",
            justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", padx=(22, 0))
        row += 1

        # --- Actions ---------------------------------------------------------
        actions = ttk.Frame(frame)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        ttk.Button(actions, text="Test popup", command=self.test).pack(side="left")
        ttk.Button(actions, text="Close", command=root.destroy).pack(side="right")
        ttk.Button(actions, text="Save", command=self.save).pack(side="right", padx=(0, 8))
        row += 1

        self.status = ttk.Label(frame, text="", style="Hint.TLabel")
        self.status.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))
        if not scheduler_running():
            self.status.configure(
                text="The reminder isn't running in the background. Run install.ps1 to start it."
            )

    def add_time(self) -> None:
        try:
            hour, minute = int(self.hour.get()), int(self.minute.get())
        except ValueError:
            hour = minute = -1
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            messagebox.showerror("Invalid time", "Pick an hour 0-23 and a minute 0-59.", parent=self.root)
            return
        times = set(self.times.get(0, "end")) | {f"{hour:02d}:{minute:02d}"}
        self.times.delete(0, "end")
        for t in sorted(times):
            self.times.insert("end", t)

    def remove_time(self) -> None:
        for index in reversed(self.times.curselection()):
            self.times.delete(index)

    def collect(self) -> dict:
        return normalize({
            "times": list(self.times.get(0, "end")),
            "title": self.title_var.get(),
            "message": self.message_var.get(),
            "snooze_minutes": self.snooze_var.get(),
            "repeat_seconds": self.repeat_var.get(),
            "sound": self.sound_var.get(),
            "grab_focus": self.focus_var.get(),
        })

    def save(self) -> None:
        settings = self.collect()
        if not settings["times"] and not messagebox.askyesno(
            "No reminder times",
            "There are no times in the list, so you won't get any reminders.\nSave anyway?",
            parent=self.root,
        ):
            return
        try:
            save_config(settings)
        except OSError as error:
            messagebox.showerror("Couldn't save", str(error), parent=self.root)
            return
        # Reflect any clamping (e.g. a snooze of 0 becomes 1) back in the form.
        self.snooze_var.set(str(settings["snooze_minutes"]))
        self.repeat_var.set(str(settings["repeat_seconds"]))
        if scheduler_running():
            self.status.configure(text=f"Saved at {datetime.now():%H:%M:%S}. Takes effect within a few seconds.")
        else:
            self.status.configure(text="Saved, but the reminder isn't running. Run install.ps1 to start it.")

    def test(self) -> None:
        if self.test_popup is not None and self.test_popup.alive:
            return

        def closed() -> None:
            self.test_popup = None

        self.test_popup = Popup(self.root, self.collect(), closed, closed)


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--settings", action="store_true", help="open the settings window")
    parser.add_argument("--now", action="store_true", help="show the popup right away")
    args = parser.parse_args()

    make_dpi_aware()
    root = tk.Tk()

    if args.settings:
        SettingsWindow(root)
        root.mainloop()
        return

    if not claim_single_instance():
        # The background scheduler already has the schedule covered. For --now,
        # still show a one-off popup so the user can see what it looks like.
        if args.now:
            root.withdraw()
            Popup(root, load_config(), root.destroy, root.destroy)
            root.mainloop()
        return

    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    root.report_callback_exception = lambda *exc: logging.error("Unhandled error", exc_info=exc)
    logging.info("Notification App started")

    root.withdraw()  # the root window is just a host for the popups
    Scheduler(root, args.now)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    logging.info("Notification App stopped")


if __name__ == "__main__":
    main()
