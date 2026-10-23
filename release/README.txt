GDPNow
======

Plays several episodes at once in a grid, switching between their scenes,
and saves screenshots plus a script that turns the session into a video.
Everything it needs is inside this folder. Nothing gets installed.

START
  1. Unzip the whole folder somewhere, for example your Desktop or Documents.
     (Running it from inside the zip does not work.)
  2. Double-click "Start GDPNow.bat".
     If Windows says "Windows protected your PC", click "More info" and then
     "Run anyway". That warning shows for any program downloaded from the internet.
  3. Click "Choose video folder" and pick the folder with your videos.
     Subfolders are included.

  The window uses Microsoft Edge WebView2, which comes with Windows 11 and
  updated Windows 10. On a PC without it, GDPNow asks whether to install it
  (Microsoft's installer is included; it needs internet) or to use the text
  version instead, which plays the same way but asks its questions as text.

FILE NAMES
  Any name works as long as the episode number is in it:
      330.mp4, E330.mkv, "Episode 330 - something.mp4", 075.mp4, 75.mp4, D06.mp4
  The "Matching report" link (or video_matches.txt in this folder) lists which
  file was matched to which episode, and which files were not recognised.
  To fix a file that was not recognised, rename it to just its number, e.g. 330.mp4.

PLAYING
  Pick a scene type, a screen, a grid size and how many episodes take turns in
  each cell, then press Start. While it plays:
      Q or Esc        stop and save
      E               jump every cell to its CSP moment
      + and -         zoom in / out
      arrow keys      move the picture (when there is room)
      0               re-centre
  After you stop, the session folder (in Pictures\gdpnow_nuts) has the
  screenshots and run_grwm.bat. "Make the video" in the app (or double-clicking
  run_grwm.bat) builds grwm.mp4. On a PC with an NVIDIA graphics card this uses
  the card's video encoder; on other PCs it uses the processor and is slower.

EPISODE MAP
  The "Episode map" page lists every episode with the start and end time of each
  scene. You can add episodes and scenes, change times, and open any episode in
  the player to read the time off the screen (the clock button copies the
  player's current time into a field). The map file is app\systemFileMap.json;
  every save keeps the previous version as systemFileMap.json.bak.
  The times only line up if your copy of an episode is the same cut as the one
  the map was made from.

SPEED
  Each cell is its own video decoder. Big grids of 4K video need a graphics chip
  that decodes video in hardware (almost every PC from the last 8 years has one,
  Intel, AMD or NVIDIA; it is used automatically). If playback stutters, use a
  smaller grid.

OTHER
  extras\Console mode (typed questions).bat runs the original text version.
  Settings are saved in settings.json in this folder.
