# YODAW Update Guide

## Updating YODAW

YODAW updates can be performed by reinstalling with the bootstrap script. Since YODAW uses an isolated installation directory, updating is safe and straightforward.

## Update Methods

### Using Bootstrap Script (Recommended)

To update YODAW to the latest version:

```bash
# Navigate to your YODAW source directory
cd /path/to/yodaw/source

# Fetch latest changes (if using git)
git pull origin main  # or your desired branch

# Run the bootstrap script with your existing installation directory
python3.12 scripts/bootstrap.py --install-dir ~/.yodaw
```

The bootstrap script will:
1. Detect if YODAW is already installed
2. Preserve your data directory (`var`)
3. Update the application code and dependencies
4. Keep your wrapper and uninstall scripts current

### Update with Custom Installation Directory

If you installed YODAW to a custom location:

```bash
python3.12 scripts/bootstrap.py --install-dir /opt/yodaw
```

## What Gets Updated

When you run the bootstrap script for an update:
- **Application code**: Updated in `lib/yodaw/`
- **Dependencies**: Reinstalled in the virtual environment (`lib/.venv/`)
- **Version information**: Updated in `lib/yodaw/version.py`
- **Wrapper scripts**: Refreshed in `bin/`
- **Data preserved**: Your `var/` directory (database, logs, etc.) is untouched

## What Does NOT Get Updated

The following are intentionally preserved during updates:
- **User data**: Mission history, configuration, etc. in `var/`
- **Custom modifications**: Any files you've added to the installation directory
- **System configuration**: Environment variables, shell aliases, etc.

## Version Information

After updating, you can check the new version:

```bash
cat ~/.yodaw/lib/yodaw/version.py
# or
~/.yodaw/bin/yodaw --version  # if you add a version command
```

## Rollback Procedure

If you need to revert to a previous version:

1. **Backup current installation** (optional but recommended):
   ```bash
   cp -r ~/.yodaw ~/.yodaw.backup_$(date +%Y%m%d_%H%M%S)
   ```

2. **Checkout previous version**:
   ```bash
   git checkout <previous-commit-or-tag>
   ```

3. **Reinstall**:
   ```bash
   python3.12 scripts/bootstrap.py --install-dir ~/.yodaw
   ```

## Update Frequency

YODAW follows semantic versioning. Check the release notes in the repository for breaking changes between versions.

## Automated Updates

For production deployments, consider automating updates with:
- Cron jobs that run the bootstrap script periodically
- Deployment scripts that trigger on new git tags
- Container image rebuilds for Docker/Kubernetes deployments