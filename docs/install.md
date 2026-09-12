# YODAW Installation Guide

## Quick Start

The easiest way to install YODAW is using the bootstrap script:

```bash
# Clone the repository (if you haven't already)
git clone <repository-url>
cd yodaw

# Run the bootstrap script
python3.12 scripts/bootstrap.py
```

This will:
1. Detect system prerequisites
2. Create an isolated Python environment
3. Install dependencies
4. Create a wrapper script at `~/.yodaw/bin/yodaw`

## Installation Methods

### Bootstrap Script (Recommended)

The bootstrap script handles all installation steps automatically:

```bash
python3.12 scripts/bootstrap.py [options]
```

Options:
- `--source PATH`: Path to YODAW source (default: current directory)
- `--install-dir PATH`: Installation directory (default: ~/.yodaw)
- `--skip-deps`: Skip dependency installation
- `--create-release`: Create release tarball instead of installing
- `--output-dir PATH`: Output directory for release artifacts (default: ./dist)

### Manual Installation

For advanced users who prefer manual installation:

1. **Prerequisites**:
   - Python 3.12
   - git (optional, for version information)

2. **Create virtual environment**:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run YODAW**:
   ```bash
   python -m app.runtime
   ```

## Installation Directory Structure

After installation with the bootstrap script, YODAW will be installed in:

```
~/.yodaw/
├── bin/
│   ├── yodaw           # Wrapper script to run YODAW
│   └── yodaw-uninstall # Uninstall script
├── lib/
│   ├── .venv/          # Isolated Python environment
│   └── yodaw/          # YODAW source code
└── var/                # Data directory (logs, database, etc.)
```

## Verifying Installation

To verify that YODAW is installed correctly:

```bash
# Check version
~/.yodaw/bin/yodaw --help

# Or check the version file
cat ~/.yodaw/lib/yodaw/version.py
```

## First Run

After installation, you can start YODAW with:

```bash
~/.yodaw/bin/yodaw
```

This will start the YODAW API server on `http://127.0.0.1:8844` by default.

You can verify it's running with:

```bash
curl http://127.0.0.1:8844/api/v1/health
# Should return: {"service":"YODAW","status":"READY",...}
```

## Custom Installation Location

To install YODAW to a custom location:

```bash
python3.12 scripts/bootstrap.py --install-dir /opt/yodaw
```

Then run it with:

```bash
/opt/yodaw/bin/yodaw
```