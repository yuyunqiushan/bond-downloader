"""专项债券下载工具的无界面 CLI/JSON 入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any

from config import AppConfig, FILE_FILTER_PRESETS, get_allowed_extensions, to_download_config
from service import download_resolved, push_to_gopeed, resolve_urls, resume_download


EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_CANCELLED = 130


class CLIParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def _add_common_options(parser: argparse.ArgumentParser, include_urls: bool = True) -> None:
    if include_urls:
        parser.add_argument("--url", action="append", default=[], help="公告详情页 URL，可重复")
        parser.add_argument("--input-json", action="append", default=[], help="JSON 文件路径，或 '-' 从 stdin 读取")
    parser.add_argument("--output-dir", help="下载根目录；默认使用已保存的 last_dir")
    parser.add_argument(
        "--filter",
        choices=["all", "pdf", "pdf_doc", "pdf_archive", "custom"],
        help="文件类型过滤器",
    )
    parser.add_argument("--extensions", help="自定义扩展名，逗号分隔")


def build_parser() -> argparse.ArgumentParser:
    parser = CLIParser(prog="bond-downloader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve", help="解析 URL 并输出 JSON 预览")
    _add_common_options(resolve_parser)

    download_parser = subparsers.add_parser("download", help="解析、预览并下载")
    _add_common_options(download_parser)
    download_parser.add_argument("--yes", action="store_true", help="跳过下载确认")
    download_parser.add_argument("--json", action="store_true", help="stdout 只输出最终 JSON")
    download_parser.add_argument("--gopeed", action="store_true", help="推送到 GoPeed 下载器而非内置引擎")
    download_parser.add_argument("--gopeed-url", default="http://127.0.0.1:9999", help="GoPeed API 地址")
    download_parser.add_argument("--gopeed-concurrency", type=int, default=5, help="GoPeed 并发连接数")

    resume_parser = subparsers.add_parser("resume", help="恢复未完成的下载")
    _add_common_options(resume_parser, include_urls=False)
    resume_parser.add_argument("--json", action="store_true", help="stdout 只输出最终 JSON")

    config_parser = subparsers.add_parser("config", help="查看或修改持久化配置")
    config_parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    config_parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _collect_input(paths: list[str], direct_urls: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
    urls = list(direct_urls)
    files: list[dict[str, Any]] = []
    for path in paths:
        data = _load_json(path)
        if isinstance(data, list):
            if all(isinstance(item, str) for item in data):
                urls.extend(data)
            elif all(isinstance(item, dict) for item in data):
                files.extend(data)
            else:
                raise ValueError(f"{path}: JSON 列表必须全部为 URL 字符串或文件对象")
        elif isinstance(data, dict):
            raw_urls = data.get("urls", [])
            raw_files = data.get("files", [])
            if isinstance(raw_urls, str):
                raw_urls = [raw_urls]
            if not isinstance(raw_urls, list) or not all(isinstance(item, str) for item in raw_urls):
                raise ValueError(f"{path}: urls 必须是字符串列表")
            if not isinstance(raw_files, list) or not all(isinstance(item, dict) for item in raw_files):
                raise ValueError(f"{path}: files 必须是对象列表")
            urls.extend(raw_urls)
            files.extend(raw_files)
        else:
            raise ValueError(f"{path}: JSON 顶层必须是数组或对象")
    return urls, files


def _apply_options(config: AppConfig, args: argparse.Namespace) -> set[str] | None:
    if getattr(args, "filter", None):
        config.file_filter = args.filter
    if getattr(args, "extensions", None) is not None:
        config.custom_exts = args.extensions
        if not getattr(args, "filter", None):
            config.file_filter = "custom"
    if config.file_filter == "custom" and not config.custom_exts.strip():
        raise ValueError("custom 过滤器必须提供 --extensions 或已保存的 custom_exts")
    return get_allowed_extensions(config)


def _output_dir(config: AppConfig, args: argparse.Namespace) -> str:
    value = getattr(args, "output_dir", None) or config.last_dir
    if not value:
        raise ValueError("必须提供 --output-dir，或先通过 config 保存 last_dir")
    return os.path.abspath(value)


def _emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _preview(result: dict[str, Any], stream) -> None:
    print(
        f"解析公告 {len(result['announcements'])} 个，附件 {len(result['files'])} 个，"
        f"解析失败 {len(result['parse_errors'])} 个",
        file=stream,
    )
    for item in result["files"]:
        print(f"  {item['relative_path']}", file=stream)
    for item in result["parse_errors"]:
        print(f"  解析失败 {item['url']}: {item['error']}", file=stream)


def _event_to_stderr(event: str, data: Any) -> None:
    if event == "task_started":
        print(f"开始: {data.filename}", file=sys.stderr)
    elif event == "task_completed":
        print(f"完成: {data.filename}", file=sys.stderr)
    elif event == "task_failed":
        print(f"失败: {data.filename}: {data.error}", file=sys.stderr)
    elif event == "progress" and data:
        print(
            f"进度: {data['completed'] + data['skipped']}/{data['total']}，失败 {data['failed']}",
            file=sys.stderr,
        )


def _resolve_command(args: argparse.Namespace, config: AppConfig) -> int:
    output_dir = _output_dir(config, args)
    extensions = _apply_options(config, args)
    urls, supplied_files = _collect_input(args.input_json, args.url)
    if supplied_files:
        raise ValueError("resolve 的 --input-json 只能包含 urls，不能包含 files")
    if not urls:
        raise ValueError("至少提供一个 --url 或包含 urls 的 --input-json")
    result = resolve_urls(urls, output_dir, extensions)
    _emit_json(result)
    if result["files"]:
        return EXIT_PARTIAL if result["parse_errors"] else EXIT_SUCCESS
    return EXIT_ERROR if result["parse_errors"] else EXIT_SUCCESS


def _download_command(args: argparse.Namespace, config: AppConfig) -> int:
    output_dir = _output_dir(config, args)
    extensions = _apply_options(config, args)
    urls, supplied_files = _collect_input(args.input_json, args.url)
    if not urls and not supplied_files:
        raise ValueError("至少提供一个 --url 或 --input-json")

    preview = {"announcements": [], "files": supplied_files, "parse_errors": []}
    if urls:
        resolved = resolve_urls(urls, output_dir, extensions)
        preview["announcements"].extend(resolved["announcements"])
        preview["files"].extend(resolved["files"])
        preview["parse_errors"].extend(resolved["parse_errors"])
    _preview(preview, sys.stderr if args.json else sys.stdout)

    if not preview["files"]:
        result = {"preview": preview, "download": None}
        if args.json:
            _emit_json(result)
        return EXIT_ERROR
    if not args.yes:
        print("确认下载以上文件？[y/N] ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer not in {"y", "yes"}:
            if args.json:
                _emit_json({"preview": preview, "cancelled": True})
            return EXIT_CANCELLED

    os.makedirs(output_dir, exist_ok=True)
    config.last_dir = output_dir
    config.save()

    if getattr(args, "gopeed", False):
        api_token = os.environ.get("GOPEED_API_TOKEN", "")
        pushed = push_to_gopeed(
            preview["files"], output_dir,
            api_url=args.gopeed_url,
            connections=args.gopeed_concurrency,
            api_token=api_token,
        )
        result = {"preview": preview, "gopeed": pushed}
        if args.json:
            _emit_json(result)
        else:
            if pushed.get("error"):
                print(f"GoPeed 推送失败: {pushed['error']}")
                return EXIT_ERROR
            print(f"已推送 {pushed['pushed']} 个任务到 GoPeed ({pushed['api_url']})")
            print(f"并发连接: {pushed['connections']}，保存目录: {pushed['output_dir']}")
        return EXIT_SUCCESS if not pushed.get("error") else EXIT_ERROR

    download = download_resolved(preview["files"], output_dir, to_download_config(config), _event_to_stderr)
    result = {"preview": preview, "download": download}
    if args.json:
        _emit_json(result)
    else:
        stats = download["stats"]
        print(f"下载完成：成功 {stats['completed']}，跳过 {stats['skipped']}，失败 {stats['failed']}")
    if download["stats"]["failed"] or preview["parse_errors"]:
        return EXIT_PARTIAL
    return EXIT_SUCCESS


def _resume_command(args: argparse.Namespace, config: AppConfig) -> int:
    output_dir = _output_dir(config, args)
    _apply_options(config, args)
    result = resume_download(output_dir, to_download_config(config), _event_to_stderr)
    if args.json:
        _emit_json(result)
    else:
        stats = result["stats"]
        print(f"恢复 {result['restored']} 个任务：成功 {stats['completed']}，失败 {stats['failed']}")
    return EXIT_PARTIAL if result["stats"]["failed"] else EXIT_SUCCESS


def _coerce_config_value(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        if raw.lower() in {"true", "1", "yes"}:
            return True
        if raw.lower() in {"false", "0", "no"}:
            return False
        raise ValueError(f"无效布尔值: {raw}")
    if isinstance(current, int):
        return int(raw)
    return raw


def _config_command(args: argparse.Namespace, config: AppConfig) -> int:
    for assignment in args.set:
        if "=" not in assignment:
            raise ValueError(f"配置项必须为 KEY=VALUE: {assignment}")
        key, raw = assignment.split("=", 1)
        if key not in config.__dataclass_fields__:
            raise ValueError(f"未知配置项: {key}")
        setattr(config, key, _coerce_config_value(getattr(config, key), raw))
    if config.file_filter not in {*FILE_FILTER_PRESETS, "custom"}:
        raise ValueError(f"无效 file_filter: {config.file_filter}")
    if args.set:
        config.save()
    data = asdict(config)
    if args.json:
        _emit_json(data)
    else:
        for key, value in data.items():
            print(f"{key}={value}")
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = AppConfig.load()
    try:
        if args.command == "resolve":
            return _resolve_command(args, config)
        if args.command == "download":
            return _download_command(args, config)
        if args.command == "resume":
            return _resume_command(args, config)
        return _config_command(args, config)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
