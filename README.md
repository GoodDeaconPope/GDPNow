# GDPNow

Plays several episodes at once in a grid, rotating through their scenes, then saves
screenshots and a script that turns the session into one grid video.

Windows only. Playback runs on [mpv](https://mpv.io); the session video is built with
[FFmpeg](https://ffmpeg.org).

## Download

Get `GDPNow-<version>-windows-x64.zip` from [Releases](../../releases). It contains
everything (Python, mpv, FFmpeg); nothing gets installed.

1. Extract the whole zip (right-click, **Extract All**). Running it from inside the zip
   does not work.
2. Open **GDPNow.exe**. If Windows shows "Windows protected your PC", click **More info**,
   then **Run anyway** (the app is not signed with a paid certificate).
3. Click **Choose video folder** and pick the folder with your videos.

Needs Windows 10 (version 1607 or newer) or Windows 11, 64-bit. The window uses Microsoft
Edge WebView2, which comes with Windows 11 and updated Windows 10; if it is missing,
GDPNow offers to install it or to use the text version instead.

## Using it

- **File names:** any name works as long as the episode number is in it: `330.mp4`,
  `E330.mkv`, `Episode 330 - title.mp4`, `075.mp4`, `D06.mp4`. The **Matching report**
  lists which file matched which episode.
- **Session:** pick a scene type, a screen, a grid (columns x rows), how many episodes
  take turns in each cell, how often they switch and for how long, then **Start**.
- **While it plays:**

  | Key | Action |
  | --- | --- |
  | `Q` / `Esc` | stop and save |
  | `E` | jump every cell to its CSP moment |
  | `+` / `-` | zoom in / out |
  | arrow keys | move the picture (when there is room) |
  | `0` | re-centre |

- **After a session:** the session folder (in `Pictures\gdpnow_nuts`) holds 10 screenshots
  and `run_grwm.bat`. **Make the video** builds `grwm.mp4` (NVIDIA NVENC when available,
  otherwise the CPU).
- **Episode map:** the **Episode map** page edits `systemFileMap.json`: every episode with
  the start (`RC`) and end (`ERC`) time of each scene, a timeline, and a player preview whose
  current time can be copied into a field.

Problems? GDPNow shows an error report: DM the pope on Discord and paste it.

## systemFileMap.json

```json
{
    "330.mp4": {
        "BJ": "05:44", "EBJ": "11:31",
        "RC": "08:52", "ERC": "10:35",
        "CSP": "45:05"
    }
}
```

Keys are episode numbers; each scene is a start time `X` and an end time `EX` in `MM:SS`.
Scene types used for sessions: `PC`, `RC`, `PM`, `D`, `M`, `CSP`, `BJ`.

## Running from source

Only needed to work on the code; the release zip already contains everything.
Python 3.12 on Windows with `mpv` and `ffmpeg` on `PATH`:

```
pip install -r requirements.txt
python gdpnow.py          # the original text version (typed questions)
python gdpnow_app.py      # the app window
```

Running from source, the video folder and the map to use are saved in
`%LOCALAPPDATA%\GDPNow\settings.json` (asked on first run).

## Building the release

```
python release\build_release.py --version 1.0.0
```

Produces `build\dist\GDPNow-1.0.0-windows-x64.zip`: the code from this repo plus an
embeddable Python 3.12 with its libraries, mpv, FFmpeg, Microsoft's WebView2 installer and
`GDPNow.exe` (compiled from `release\launcher\GDPNow.cs` with the C# compiler built into
Windows). The downloads it needs are listed at the top of `release\build_release.py`.

## Layout

| Path | What |
| --- | --- |
| `gdpnow.py` | the player: grid of mpv windows, scene rotation, hotkeys, screenshots, video script |
| `gdpnow_support.py` | file matching, settings, bundled-tool paths, encoder choice, error reports |
| `gdpnow_app.py`, `ui/` | the app window (pywebview + HTML/CSS/JS) |
| `systemFileMap.json` | the episode map shipped in the release |
| `release/` | release builder, launcher, release README, licenses, tests |
| `tools/` | map statistics and a grid debugging helper |

Third-party licenses for the bundled programs are in `release/LICENSES.txt`.
