// Windows GUI and unattended installer. Build with the Windows .NET Framework compiler.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

class InstallBritannica : Form {
    TextBox location = new TextBox();
    Button browse = new Button(), install = new Button();
    Label status = new Label();
    string package = AppDomain.CurrentDomain.BaseDirectory;

    [STAThread]
    static int Main(string[] args) {
        if (args.Length > 0) {
            Console.OutputEncoding = new UTF8Encoding(false);
            try {
                if (args.Length != 4 || args[0] != "--package" || args[2] != "--reader")
                    throw new Exception("Usage: Install Britannica 11.exe --package FOLDER --reader FOLDER");
                RunInstall(args[1], args[3], delegate(string message) { Console.WriteLine(message); });
                return 0;
            } catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 1; }
        }
        Application.EnableVisualStyles();
        Application.Run(new InstallBritannica());
        return 0;
    }

    InstallBritannica() {
        Text = "Install Britannica 11";
        ClientSize = new Size(590, 255);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10);
        var heading = new Label { Text = "Britannica 11 for GoldenDict", Font = new Font(Font, FontStyle.Bold),
            AutoSize = true, Location = new Point(20, 18) };
        var instructions = new Label { Text = "Close GoldenDict, then choose its portable folder (the one containing goldendict.exe). Your existing settings will be kept.",
            Location = new Point(20, 52), Size = new Size(550, 48) };
        location.SetBounds(20, 111, 440, 29);
        browse.Text = "Browse…"; browse.SetBounds(470, 109, 100, 31);
        browse.Click += delegate {
            using (var dialog = new FolderBrowserDialog()) {
                dialog.Description = "Choose your portable GoldenDict folder";
                dialog.ShowNewFolderButton = false;
                if (dialog.ShowDialog(this) == DialogResult.OK) location.Text = dialog.SelectedPath;
            }
        };
        install.Text = "Install"; install.SetBounds(460, 199, 110, 35);
        status.SetBounds(20, 151, 550, 43);
        status.Text = "No separate Python or Node installation is needed.";
        install.Click += delegate { BeginInstall(); };
        Controls.AddRange(new Control[] { heading, instructions, location, browse, status, install });
    }

    void BeginInstall() {
        string reader = location.Text.Trim().Trim('"');
        install.Enabled = browse.Enabled = location.Enabled = false;
        var worker = new BackgroundWorker();
        string completedMessage = null;
        worker.DoWork += delegate {
            RunInstall(package, reader, delegate(string message) {
                if (message.StartsWith("Installed.")) completedMessage = message;
                BeginInvoke((Action)delegate { status.Text = message; });
            });
        };
        worker.RunWorkerCompleted += delegate(object sender, RunWorkerCompletedEventArgs e) {
            install.Enabled = browse.Enabled = location.Enabled = true;
            if (e.Error != null) {
                status.Text = "Installation was not completed.";
                MessageBox.Show(this, e.Error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
            } else {
                status.Text = completedMessage + " Initial indexing may take a few minutes.";
                install.Text = "Install again";
            }
        };
        worker.RunWorkerAsync();
    }

    static void RunInstall(string package, string reader, Action<string> progress) {
        string node = Path.Combine(package, "search", "runtime", "node.exe");
        string engine = Path.Combine(package, "installer", "install.cjs");
        var start = new ProcessStartInfo(node, "--no-warnings \"" + engine + "\" --package \"" + package.TrimEnd(Path.DirectorySeparatorChar) + "\" --reader \"" + reader.TrimEnd(Path.DirectorySeparatorChar) + "\"");
        start.UseShellExecute = false; start.CreateNoWindow = true;
        start.RedirectStandardOutput = true; start.RedirectStandardError = true;
        start.StandardOutputEncoding = Encoding.UTF8; start.StandardErrorEncoding = Encoding.UTF8;
        using (var process = new Process()) {
            process.StartInfo = start;
            var errors = new StringBuilder();
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) progress(e.Data); };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) errors.AppendLine(e.Data); };
            process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine(); process.WaitForExit();
            if (process.ExitCode != 0) throw new Exception(errors.ToString().Trim());
        }
    }
}
