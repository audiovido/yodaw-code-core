# YODAW Troubleshooting Guide

## Common Issues and Solutions

### Python Version Problems

**Issue**: "Error: Python 3.12 not found"
**Solution**: Install Python 3.12 from python.org, use Homebrew (`brew install python@3.12`), or use pyenv.

**Issue**: Module not found errors after installation
**Solution**: Ensure you're using the correct Python interpreter from the virtual environment. Use the wrapper script (`~/.yodaw/bin/yodaw`) which automatically uses the correct Python.

### Installation Issues

**Issue**: Bootstrap script fails during dependency installation
**Solution**:
1. Check your internet connection
2. Ensure you have pip installed: `python3.12 -m ensurepip --upgrade`
3. Try with `--skip-deps` and install manually: `pip install -r requirements.txt`
4. Check for system-level dependencies that might be missing

**Issue**: "File name too long" errors during release creation
**Solution**: This was fixed in the bootstrap script by adding better exclusion patterns and preventing infinite recursion.

### Runtime Issues

**Issue**: YODAW fails to start or crashes immediately
**Solution**:
1. Check the logs: `~/.yodaw/bin/yodaw-uninstall` doesn't stop a running instance, but you can check stdout/stderr
2. Ensure port 8844 is available: `lsof -i :8844`
3. Check disk space and permissions in the `var` directory
4. Try starting with a clean `var` directory (backup first): `rm -rf ~/.yodaw/var/*`

**Issue**: Health check returns non-READY status
**Solution**:
- Check the `/api/v1/ready` endpoint for more details
- Look at the `config_ok` field in health response
- Check if YODAW_EMBED_COORDINATOR=0 is set incorrectly
- Verify storage backend configuration matches what's actually instantiated

**Issue**: Missions stay in QUEUED state
**Solution**:
1. Check if the coordinator is running: `curl http://127.0.0.1:8844/api/v1/runtime/status`
2. Look for worker registration issues
3. Check if any workers are registered and healthy
4. Verify repository access permissions

### Network and Connectivity Issues

**Issue**: Cannot connect to YODAW API
**Solution**:
1. Verify YODAW is running: `ps aux | grep yodaw`
2. Check the host and port: default is 127.0.0.1:8844
3. Check firewall settings
4. Try connecting locally: `curl http://127.0.0.1:8844/api/v1/health`

**Issue**: Outbound HTTP requests fail
**Solution**:
1. Check outbox relay status in `/api/v1/runtime/status`
2. Verify network connectivity from the YODAW process
3. Check DNS resolution
4. Look at outbox statistics for failed attempts

### Performance Issues

**Issue**: High CPU or memory usage
**Solution**:
1. Check for runaway missions: `curl http://127.0.0.1:8844/api/v1/missions`
2. Look at coordinator statistics for stuck workers
3. Check for infinite loops in user-submitted code
4. Consider adjusting worker concurrency limits

**Issue**: Slow mission processing
**Solution**:
1. Check worker registration and heartbeats
2. Look at mission queue depth
3. Verify system resources (CPU, memory, disk I/O)
4. Check if external dependencies (repositories, APIs) are responding slowly

### Data and Persistence Issues

**Issue**: Mission history appears missing
**Solution**:
1. Verify you're looking at the correct installation directory
2. Check that the `var` directory hasn't been deleted or corrupted
3. Check database integrity: `sqlite3 ~/.yodaw/var/yodaw.db "SELECT COUNT(*) FROM missions;"`

**Issue**: Database corruption errors
**Solution**:
1. Stop YODAW
2. Backup the database: `cp ~/.yodaw/var/yodaw.db ~/.yodaw/var/yodaw.db.backup`
3. Try to recover: `sqlite3 ~/.yodaw/var/yodaw.db ".dump" > dump.sql`
4. If recovery fails, you may need to start fresh (mission history will be lost)

### macOS Specific Issues

**Issue**: "Operation not permitted" errors
**Solution**:
1. Check if you're trying to install to a protected directory
2. Use a user-writable location like `~/.yodaw` or `/tmp/yodaw-install`
3. Avoid system directories like `/usr/local` without proper permissions

**Issue**: Python framework not found
**Solution**:
1. Ensure you installed the official Python 3.12 from python.org
2. Or use Homebrew: `brew install python@3.12`
3. The system Python on macOS is often too old

## Diagnostic Commands

Here are useful commands for diagnosing YODAW issues:

```bash
# Check if YODAW is running
ps aux | grep yodaw | grep -v grep

# Check version and installation
~/.yodaw/bin/yodaw --help

# Check health endpoint
curl -s http://127.0.0.1:8844/api/v1/health | python3 -m json.tool

# Check detailed runtime status
curl -s http://127.0.0.1:8844/api/v1/runtime/status | python3 -m json.tool

# Check mission queue
curl -s http://127.0.0.1:8844/api/v1/missions?limit=10 | python3 -m json.tool

# Check worker status
curl -s http://127.0.0.1:8844/api/v1/workers | python3 -m json.tool

# Check logs (if redirected to file)
tail -f ~/.yodaw/var/yodaw.log  # if you've configured logging to file

# Check installation directory size
du -sh ~/.yodaw

# Check var directory contents
ls -la ~/.yodaw/var/
```

## Getting Help

If you encounter issues not covered in this guide:

1. **Check the logs**: Always start by examining the output from the YODAW process
2. **Search existing issues**: Look through the repository's issue tracker
3. **Provide detailed information** when seeking help:
   - YODAW version (from `version.py`)
   - macOS version and architecture
   - Installation method used
   - Exact error messages
   - Steps to reproduce the issue
   - Relevant log snippets
   - Health and runtime status outputs

## Known Limitations

### macOS Specific
- Python 3.12 must be explicitly installed (system Python is too old)
- Some security software may interfere with file watchers
- Network extensions may affect outbound connectivity

### General
- YODAW is designed for single-node operation; clustering is not supported in this version
- The embedded coordinator is suitable for development and light production use
- For heavy production workloads, consider using a standalone coordinator