-- GDPNow on macOS: each grid cell is its own mpv window, and a global
-- keyboard hook would need the Accessibility permission. Instead every grid
-- mpv loads this script: the grid keys pressed in a cell go to the player as
-- client messages ("gdpnow-key", name, "down" | "up"), with key-up so a held
-- arrow pans smoothly as on Windows.
local keys = {
    q = "q", Q = "q", ESC = "esc", e = "e", E = "e",
    ["+"] = "+", ["="] = "+", KP_ADD = "+", ["-"] = "-", KP_SUBTRACT = "-",
    ["0"] = "0", KP0 = "0",
    LEFT = "left", RIGHT = "right", UP = "up", DOWN = "down",
}
for key, name in pairs(keys) do
    mp.add_forced_key_binding(key, "gdpnow-" .. key, function(t)
        mp.commandv("script-message", "gdpnow-key", name, t.event == "up" and "up" or "down")
    end, { complex = true })
end
