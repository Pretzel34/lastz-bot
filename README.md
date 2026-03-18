# Last Z Bot

An automated bot for **Last Z** (Android game) that runs on emulators (MEmu, LDPlayer, Nox) on Windows. The bot automates farm tasks like resource collection, rallies, and daily rewards while you're away.

## Features

- **Multi-instance support** — Run multiple farms simultaneously on different emulator instances
- **Task automation** — Daily tasks, rallies, gathering, radar, etc.
- **Vision-based detection** — Template matching for reliable in-game navigation
- **Live preview** — Watch the current farm's screenshot in real-time
- **Configurable per-farm** — Different settings for different farms
- **Auto-updates** — When you launch the app, it checks GitHub Releases for new versions
- **Cross-platform architecture** — Designed to support Windows, macOS (in progress)

## Installation

### From Installer (Recommended)

1. Download the latest `LastZBot-vX.Y.Z.exe` from [Releases](https://github.com/Pretzel34/lastz-bot/releases)
2. Run the installer
3. Launch the app from your Start Menu or Desktop shortcut
4. Configure your emulator and farms in the **Bot Settings** tab

### From Source (Development)

1. Clone this repository:
   ```bash
   git clone https://github.com/Pretzel34/lastz-bot.git
   cd lastz-bot
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:
   ```bash
   python gui.py
   ```

## Configuration

### Bot Settings

- **Emulator** — Select MEmu, LDPlayer, or Nox
- **Emulator path** — Path to your emulator installation (auto-detected if possible)
- **Vision confidence** — Adjust template matching sensitivity (0.5–0.99)
- **Timeouts** — Customize boot times and task timeouts

### Per-Farm Settings

Each farm can have different tasks enabled/disabled:
- **Daily Tasks** — Idle rewards, free rewards, VIP rewards, radar, mail, fuel, recruits
- **Rally** — Quick join, create rally, boomer level
- **Gathering** — Wood, food, electricity, zents, resource level, max formations

## Building from Source

To build a standalone Windows `.exe`:

```bash
.\build_windows.bat
```

The executable will be created in `dist\LastZBot.exe`.

## Publishing a Release

1. Update `version.py` with the new version (e.g., `0.2.0`)
2. Commit your changes:
   ```bash
   git add .
   git commit -m "Version 0.2.0"
   ```
3. Tag the commit:
   ```bash
   git tag v0.2.0
   git push origin main --tags
   ```

GitHub Actions will automatically build the `.exe` and create a GitHub Release with the installer.

## Troubleshooting

### "Emulator not found"
- Ensure your emulator (MEmu, LDPlayer, or Nox) is installed
- In **Bot Settings**, use the "Auto-detect" button to find your emulator
- Or manually set the path to your emulator's installation directory

### "ADB connection failed"
- Ensure ADB is available on your system (usually bundled with emulators)
- Verify the emulator is running and has finished booting
- Check firewall settings — ADB uses localhost ports

### "Screenshot failed" / "Nothing happens"
- Verify the game is running and in the correct state
- Check **Vision confidence** setting in Bot Settings
- Review the logs in the **Run Bot** tab for error messages

## Contributing

To contribute:
1. Fork this repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -am 'Add my feature'`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request

## Reporting Issues

If you encounter a bug:
1. Check the logs in the **Run Bot** tab
2. Open an issue on [GitHub Issues](https://github.com/Pretzel34/lastz-bot/issues)
3. Include:
   - Your OS and Python version
   - Emulator type and version
   - Steps to reproduce
   - Log output or error messages

## License

This project is private and for personal use only.

## FAQ

**Q: Is this safe to use?**  
A: The bot only automates clicks and swipes that a human could do manually. It doesn't modify game files or inject code. Use at your own risk and check the game's ToS.

**Q: Can I run this on macOS or Linux?**  
A: Windows is currently fully supported. macOS support is in progress (will require Android SDK emulator or Genymotion). Linux is not planned.

**Q: How do I update?**  
A: On startup, the app checks GitHub Releases. If a newer version is available, you'll get a prompt to download and install it.

**Q: Can I distribute this to other players?**  
A: No, this is a private tool. You can share the source code with friends, but not compiled binaries.
