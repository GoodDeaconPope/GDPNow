// GDPNow.exe: starts the GDPNow app window with the bundled Python, without a
// console window. Built by release\build_release.py with the C# compiler that
// ships with Windows (.NET Framework 4.x).
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Launcher
{
    [STAThread]
    static int Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string pythonw = Path.Combine(root, @"runtime\python\pythonw.exe");
        string app = Path.Combine(root, @"app\gdpnow_app.py");
        if (!File.Exists(pythonw) || !File.Exists(app))
        {
            MessageBox.Show(
                "GDPNow can't find its files.\n\n" +
                "If you opened it from inside the zip, extract the whole zip first " +
                "(right-click the zip, Extract All), then open GDPNow.exe from the extracted folder.",
                "GDPNow", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 1;
        }
        var start = new ProcessStartInfo(pythonw, "\"" + app + "\"")
        {
            WorkingDirectory = root,
            UseShellExecute = false,
        };
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        try
        {
            Process.Start(start);
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show("GDPNow could not start: " + e.Message + "\n\nDM the pope on Discord.",
                            "GDPNow", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
