# GTA VI PortWatch Widget

A small always-on-top desktop widget that shows the **current leading project** from
[PortWatch VI](https://portwatch-vi.xyz/) and notifies you when the leader changes.

- Recreates the project card from the site: name, byline, progress %, stage and rank,
  evidence chips, summary, milestones and the four progress bars
- States clearly whether the leading project has **public or private code**
- Checks every 60 seconds (the site itself refreshes about every 3 hours)
- Desktop notification, sound and an in-widget banner when the leader changes
  (and optionally when the leader's percentage changes)

## Run it

Requires Python 3.9+ (tkinter is included with the standard Windows installer).

```
pip install pillow winotify      # optional: avatars + nicer Windows toasts
python portwatch_widget.py
```

## Build an .exe (Windows)

```
python -m pip install pyinstaller pillow winotify
python -m PyInstaller --onefile --windowed --name PortWatchWidget portwatch_widget.py
```

The exe is created at `dist/PortWatchWidget.exe`. It saves `portwatch_state.json` next to itself
so it remembers the last leader.

## Settings

Edit the constants at the top of `portwatch_widget.py`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `POLL_SECONDS` | `60` | How often to check the site |
| `RANKED_ONLY` | `False` | `True` = only projects with public code (what the site ranks) |
| `NOTIFY_ON_SCORE_CHANGE` | `True` | Also notify when the leader's % changes |
| `ALWAYS_ON_TOP` | `True` | Keep the widget above other windows |

## How it works

PortWatch publishes its data as `data/tracker.json`. The widget reads that file directly rather than
scraping the page, and recomputes each project's progress % using the site's published scoring scale.
If the site changes its data format, the widget may need updating.

## Credits and disclaimer

All project data belongs to [PortWatch VI](https://portwatch-vi.xyz/), a fan-made tracker. This widget is
unofficial and not affiliated with PortWatch, Rockstar Games, Take-Two or Microsoft. It contains no game
files and does not link to any.
