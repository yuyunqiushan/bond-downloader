"""供 CLI 和对话 Skill 调用的纯 Python 下载服务。"""
from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Optional

from config import AppConfig, to_download_config
from downloader import DownloadConfig, DownloadEngine, FileTask, TaskStatus
from parsers import URLParser


def _normalize_extensions(extensions: Optional[Iterable[str]]) -> Optional[set[str]]:
    if extensions is None:
        return None
    normalized = {
        str(extension).strip().lower().lstrip(".")
        for extension in extensions
        if str(extension).strip().lstrip(".")
    }
    return normalized or None


def _matches_extension(filename: str, extensions: Optional[set[str]]) -> bool:
    if extensions is None:
        return True
    return os.path.splitext(filename)[1].lower().lstrip(".") in extensions


def resolve_urls(
    urls: Iterable[str],
    output_dir: str,
    allowed_extensions: Optional[Iterable[str]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """逐个解析公告 URL，隔离错误并返回去重后的附件预览。

    此函数不会创建输出目录或公告子目录。
    """
    output_dir = os.path.abspath(os.fspath(output_dir))
    extensions = _normalize_extensions(allowed_extensions)
    parser = URLParser()
    announcements: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    seen_files: set[tuple[str, str, str]] = set()

    for raw_url in urls:
        url = str(raw_url).strip()
        if not url or url in seen_sources:
            continue
        seen_sources.add(url)
        try:
            parsed = parser.parse(url, output_dir)
        except Exception as error:
            parse_errors.append({"url": url, "error": str(error)})
            continue

        kept = 0
        title = ""
        for item in parsed:
            filename = str(item.get("filename", "")).strip()
            download_url = str(item.get("url", "")).strip()
            folder_title = str(item.get("folder_title", "")).strip()
            if not title and folder_title:
                title = folder_title
            if not filename or not download_url or not _matches_extension(filename, extensions):
                continue
            key = (download_url, folder_title, filename)
            if key in seen_files:
                continue
            seen_files.add(key)
            relative_path = os.path.join(folder_title, filename) if folder_title else filename
            files.append({
                "url": download_url,
                "filename": filename,
                "folder_title": folder_title,
                "source_url": str(item.get("source_url") or url),
                "relative_path": relative_path,
            })
            kept += 1
        announcements.append({"url": url, "title": title, "file_count": kept})

    return {
        "announcements": announcements,
        "files": files,
        "parse_errors": parse_errors,
    }


def _download_config(config: AppConfig | DownloadConfig) -> DownloadConfig:
    if isinstance(config, DownloadConfig):
        return config
    if isinstance(config, AppConfig):
        return to_download_config(config)
    raise TypeError("config 必须是 AppConfig 或 DownloadConfig")


def _task_result(task: FileTask) -> dict[str, Any]:
    return {
        "url": task.url,
        "filename": task.filename,
        "folder_title": task.folder_title,
        "source_url": task.source_url,
        "file_path": task.file_path,
        "status": task.status.value,
        "error": task.error,
        "total_size": task.total_size,
        "downloaded": task.downloaded,
    }


def _run_engine(engine: DownloadEngine) -> dict[str, Any]:
    completed = False
    try:
        engine.start()
        engine.wait_finished()
        stats = engine.get_stats()
        tasks = [_task_result(task) for task in engine.get_all_tasks()]
        completed = stats["failed"] == 0
        return {"stats": stats, "files": tasks}
    finally:
        if completed:
            engine.clear_state()
        engine.close()


def download_resolved(
    files: Iterable[dict[str, Any]],
    output_dir: str,
    config: AppConfig | DownloadConfig,
    event_callback: Optional[Callable[[str, Any], None]] = None,
) -> dict[str, Any]:
    """下载已解析的附件；只有此阶段会创建目录。"""
    output_dir = os.path.abspath(os.fspath(output_dir))
    engine = DownloadEngine(output_dir, _download_config(config), event_callback)
    for item in files:
        engine.submit_file(
            url=str(item["url"]),
            filename=str(item["filename"]),
            folder_title=str(item.get("folder_title", "")),
            source_url=str(item.get("source_url", "")),
        )
    return _run_engine(engine)


def push_to_gopeed(
    files: Iterable[dict[str, Any]],
    output_dir: str,
    api_url: str = "http://127.0.0.1:9999",
    connections: int = 5,
    api_token: str = "",
) -> dict[str, Any]:
    """将解析后的附件 URL 批量推送到 GoPeed REST API 下载。"""
    import urllib.request
    import json as _json

    output_dir = os.path.abspath(os.fspath(output_dir))
    batch = {
        "reqs": [
            {
                "req": {"url": str(item["url"])},
                "opts": {
                    "name": str(item["filename"]),
                    "path": os.path.join(output_dir, str(item.get("folder_title", ""))) if item.get("folder_title") else output_dir,
                },
            }
            for item in files
        ],
        "opts": {"path": output_dir, "connections": max(1, min(256, connections))},
    }
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["X-Api-Token"] = api_token

    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/v1/tasks/batch",
        data=_json.dumps(batch).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = _json.load(resp)
    except Exception as error:
        return {"pushed": 0, "error": str(error), "api_url": api_url}

    return {
        "pushed": len(batch["reqs"]),
        "api_url": api_url,
        "output_dir": output_dir,
        "connections": connections,
        "response": body,
    }


def resume_download(
    output_dir: str,
    config: AppConfig | DownloadConfig,
    event_callback: Optional[Callable[[str, Any], None]] = None,
) -> dict[str, Any]:
    """从输出目录中的状态文件恢复未完成下载。"""
    output_dir = os.path.abspath(os.fspath(output_dir))
    engine = DownloadEngine(output_dir, _download_config(config), event_callback)
    restored = engine.load_state()
    if restored == 0:
        engine.close()
        return {"restored": 0, "stats": engine.get_stats(), "files": []}

    for task in engine.get_all_tasks():
        if task.status == TaskStatus.FAILED:
            task.status = TaskStatus.QUEUED
            task.retries = 0
            task.error = ""
    result = _run_engine(engine)
    result["restored"] = restored
    return result
