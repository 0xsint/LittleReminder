# Notification App

A small Windows reminder that pops up **on top of everything, including your
game**, at the times you choose (08:00 and 20:00 out of the box). It stays on
screen and keeps chiming until you click **Done**, or you can **Snooze** it.

It runs quietly in the background from the moment you log in. No console
window, no tray icon.

## Install

You need Python 3 for Windows (the Microsoft Store version is fine). No extra
packages are required.

1. Open PowerShell in this folder.
2. Create the virtual environment (first time only):

   ```powershell
   python -m venv .venv
   ```

3. Install the background task:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

That registers a scheduled task called **Notification App** that starts 30
seconds after you log in, and adds **Notification App Settings** to your Start
menu. Run `install.ps1` again any time you update `app.py`; it restarts the
task so the new version takes over.

To remove it:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

## Changing your times and other settings

Open **Start menu → Notification App Settings** (or run
`.venv\Scripts\pythonw.exe app.py --settings`).

| Setting | What it does |
| --- | --- |
| **Reminder times** | Pick an hour and minute (24-hour clock) and click **Add time**. Select a time and click **Remove selected** (or press Delete) to drop it. Add as many as you like. |
| **Title / Message** | The text shown on the popup. |
| **Snooze length** | How long **Snooze** hides the popup before it comes back (1–240 minutes). |
| **Repeat sound every** | While the popup is up, it chimes again this often (5–600 seconds). |
| **Play a sound** | Turn the chime off if you only want the visual popup. |
| **Grab focus from the game** | See *Games* below. Off by default. |

Click **Test popup** to preview your changes, then **Save**. The running app
notices the change within about five seconds, so there's nothing to restart.

Settings are stored in `config.json` next to `app.py`. You can edit it by hand
too; invalid times are ignored and out-of-range numbers are clamped.

```json
{
  "times": ["08:00", "20:00"],
  "title": "Take your meds!",
  "message": "Time to take your medication now.",
  "snooze_minutes": 10,
  "repeat_seconds": 40,
  "sound": true,
  "grab_focus": false
}
```

## Games

The popup is a borderless, always-on-top window that re-asserts itself every
second, so it shows over games running in **borderless** or **windowed** mode
(the default for most modern games) without taking focus away from you. Your
game keeps running; click **Done** when you've dealt with it. You can drag the
popup anywhere if it's covering something.

Games in **exclusive fullscreen** take over the display, and no ordinary
window can draw over them. You have two options:

- Switch the game to *borderless windowed* / *fullscreen windowed* in its
  video settings (recommended), or
- Turn on **Grab focus from the game**. When the reminder fires it pulls focus
  to the popup, which alt-tabs you out of the game so you can see it. It
  repeats that every *Repeat sound every* seconds until you respond.

Windows' own toast notifications are hidden while you play (Do Not Disturb /
Focus Assist), which is why this app uses its own window instead.

## Other details

- **Missed alerts:** if the PC was asleep or off at an alert time, the popup
  appears as soon as it wakes (once, not once per missed time).
- **Check it works:** `.venv\Scripts\pythonw.exe app.py --now` shows the popup
  immediately.
- **Log:** `app.log` records when alerts fire, repeat, are snoozed, and are
  acknowledged. It rotates at about 500 KB.
- **Only one copy runs:** starting `app.py` again while the background task is
  running just exits (or, with `--now`, shows a one-off popup).

## Files

| File | Purpose |
| --- | --- |
| `app.py` | The scheduler, popup and settings window |
| `install.ps1` | Installs/uninstalls the logon task and Start menu shortcut |
| `config.json` | Your settings (created on first run) |
| `app.log` | Activity log |
