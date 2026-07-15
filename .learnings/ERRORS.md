# Errors

Command failures and integration errors.

---

## [ERR-20260715-001] init_skill.py via Git Bash Windows Python

**Logged**: 2026-07-15T19:15:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
Windows Python interpreted a Git Bash `/c/...` script argument incorrectly when the command mixed a POSIX executable path with a second POSIX drive path.

### Error
```
python.exe: can't open file 'd:\\c\\Users\\...\\init_skill.py': [Errno 2] No such file or directory
```

### Context
- Executable was invoked through `/c/.../python.exe`.
- Script and output paths were also passed as `/c/...` and `/d/...`.

### Suggested Fix
Invoke Windows-native executables with Windows-style path arguments, preferably through PowerShell, or use a dedicated file tool.

### Metadata
- Reproducible: yes
- Related Files: .workbuddy/skills/special-bond-downloader

### Resolution
- **Resolved**: 2026-07-15T19:16:00+08:00
- **Notes**: Switched subsequent initialization command to PowerShell with quoted Windows paths.

---
