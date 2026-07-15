# Feature Requests

Capabilities requested by the user.

---

## [FEAT-20260715-001] conversational-bond-download

**Logged**: 2026-07-15T19:16:00+08:00
**Priority**: high
**Status**: in_progress
**Area**: backend

### Requested Capability
通过多轮对话按“地区 + 年份 + 期次”自动检索专项债券公告，或直接接收公告详情页 URL；先预览附件清单，确认后无界面下载并记住保存目录。

### User Context
替代现有 Tkinter GUI，使常用下载流程直接在 WorkBuddy 对话中完成。

### Complexity Estimate
complex

### Suggested Implementation
新增公告检索层、纯 Python 服务层、CLI/JSON 接口和项目级 Skill；复用 URLParser 与 DownloadEngine，移除 GUI 入口。

### Metadata
- Frequency: first_time
- Related Features: URLParser, DownloadEngine, AppConfig

---
