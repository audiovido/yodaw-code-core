# YODAW Uninstall Guide

## Safe Uninstallation

YODAW is designed to be cleanly removable without affecting your system or leaving unwanted files behind.

## Uninstall Methods

### Using the Uninstall Script (Recommended)

The easiest way to uninstall YODAW is using the provided uninstall script:

```bash
~/.yodaw/bin/yodaw-uninstall
```

This script will:
1. Confirm that you want to proceed with uninstallation
2. Remove the entire installation directory
3. Preserve your personal data (see "What Gets Removed" below)

### Manual Uninstallation

If you prefer to uninstall manually:

```bash
# Remove the entire installation directory
rm -rf ~/.yodaw
```

Replace `~/.yodaw` with your actual installation directory if you used a custom location.

## What Gets Removed

When you uninstall YODAW, the following are removed:
- **Application code**: All files in `lib/yodaw/`
- **Virtual environment**: All files in `lib/.venv/`
- **Wrapper scripts**: `bin/yodaw` and `bin/yodaw-uninstall`
- **Installation directory**: The entire directory you specified during installation

## What Does NOT Get Removed

YODAW intentionally preserves certain files to avoid accidental data loss:

### User Data
Your mission history, learned patterns, and other operational data are stored in the `var` directory and are NOT removed by default. This includes:
- SQLite database: `var/yodaw.db`
- Any additional files you've added to `var/`

If you want to remove user data as part of uninstallation, you can manually delete the `var` directory:
```bash
rm -rf ~/.yodaw/var
```

### System Files
YODAW never modifies system files outside of its installation directory, so there's nothing to clean up system-wide.

### Shell Configuration
YODAW does not modify your shell profile (`.zshrc`, `.bashrc`, etc.) during installation, so no cleanup is needed.

### Home Directory Files
YODAW does not create any files or directories in your home directory outside of the installation directory.

## Uninstalling a Running Instance

If YODAW is currently running when you attempt to uninstall:

1. **Using the uninstall script**: The script will detect if YODAW appears to be running and warn you, but it won't stop the process. You should stop YODAW first.

2. **Manual uninstallation**: Same as above - stop the process first.

To stop a running YODAW instance:
- Press `Ctrl+C` in the terminal where it's running
- Or send a SIGTERM signal: `kill <pid>` where `<pid>` is the YODAW process ID
- Or use the launcher if you installed via product-launcher: `yodaw stop`

## Clean Machine Considerations

If you're uninstalling from a clean machine or temporary environment:
1. Stop any running YODAW instances
2. Run the uninstall script or manually remove the installation directory
3. Optionally remove the `var` directory if you want to delete all user data
4. Verify removal: `ls ~/.yodaw` should show "No such file or directory"

## Troubleshooting Uninstallation

### "Permission denied" errors
If you encounter permission errors during uninstallation:
- Ensure you have ownership of the installation directory
- Try using `sudo` only if absolutely necessary (not recommended)
- Check for any running YODAW processes and stop them first

### Uninstall script not found
If you've manually deleted files and can't find the uninstall script:
- Simply remove the installation directory manually: `rm -rf ~/.yodaw`
- Or recreate the uninstall script from the bootstrap script if needed

### Leftover processes
If YODAW processes persist after uninstallation:
- Find them with: `ps aux | grep yodaw`
- Stop them with: `kill <pid>` or `pkill -f yodaw`
- This is rare as the uninstall doesn't affect running processes, but good practice to check

## Reinstallation After Uninstallation

You can reinstall YODAW at any time after uninstallation:
1. Simply run the bootstrap script again: `python3.12 scripts/bootstrap.py`
2. Your previous user data will not be restored (unless you backed up the `var` directory)
3. The installation will be fresh and clean