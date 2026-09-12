# YODAW Release Build Guide

## Creating Release Artifacts

YODAW provides a simple mechanism for creating release artifacts using the bootstrap script.

### Release Build Command

To create a release tarball:

```bash
python3.12 scripts/bootstrap.py --create-release
```

This will:
1. Create a temporary directory with the source code (excluding development artifacts)
2. Generate a versioned tarball: `dist/yodaw-{version}.tar.gz`
3. Create a VERSION file with build metadata

### Release Artifact Contents

The release tarball contains:
- Complete YODAW source code (excluding `.git`, `__pycache__`, `data/`, `workspace/`, etc.)
- All documentation
- Scripts (including bootstrap)
- Configuration files
- No build artifacts, test caches, or local development files

### Version Information

Each release includes version metadata in two places:

1. **Inside the tarball**: `yodaw/version.py` contains:
   - `__version__`: Product version (e.g., "0.1.0")
   - `__commit__`: Git commit SHA
   - `__branch__`: Git branch name
   - Build metadata (platform, architecture, timestamp)

2. **Separate VERSION file**: In the distribution directory alongside the tarball:
   ```
   0.1.0
   commit: cf6cf2a6ec167f95a1feb587f9c70684e3e2b1ec
   branch: yodaw/product-packaging
   platform: Darwin
   architecture: x86_64
   ```

### Reproducible Builds

The build process aims for reproducibility by:
- Using a fixed version number (0.1.0) that should be updated manually for releases
- Including the exact commit SHA used for the build
- Recording build platform and timestamp
- Excluding non-essential files that vary between development environments

### Manual Version Updates

To prepare a new release:
1. Update the version in `scripts/bootstrap.py` (search for `__version__ = "0.1.0"`)
2. Commit the change
3. Tag the commit: `git tag v0.1.0`
4. Run the release build command
5. Distribute the resulting tarball

### Verifying Release Artifacts

To verify a release artifact:

```bash
# Extract and check version
tar -xzf yodaw-0.1.0.tar.gz
cat yodaw-0.1.0/yodaw/version.py

# Or check the separate VERSION file
cat dist/VERSION
```

### Distribution

The release artifact (`yodaw-{version}.tar.gz`) can be distributed via:
- Standard download links
- Package managers (Homebrew, etc.)
- Internal distribution systems
- Container images (built from the tarball)

### Security Notes

- The release process does not include signing or notarization
- For production distribution, consider adding:
  - Code signing with Developer ID
  - Notarization through Apple's notary service
  - Checksum publication (SHA256) for integrity verification

See `docs/uninstall.md` for clean removal instructions.