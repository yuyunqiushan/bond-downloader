"""应用配置和下载参数转换。"""
import json
import os
from dataclasses import asdict, dataclass
from typing import Optional


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(MODULE_DIR, ".config.json")


@dataclass
class AppConfig:
    last_dir: str = ""
    file_concurrency: int = 3
    chunk_threads: int = 8
    large_file_threshold_mb: int = 50
    connect_timeout: int = 30
    read_timeout: int = 300
    max_retries: int = 3
    file_filter: str = "all"
    custom_exts: str = "pdf,zip,rar"

    def save(self) -> None:
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as file:
                json.dump(asdict(self), file, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except OSError:
            pass

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return cls()

        config = cls()
        for key, value in data.items():
            if key in config.__dataclass_fields__:
                setattr(config, key, value)
        return config


FILE_FILTER_PRESETS = {
    "all": None,
    "pdf": {"pdf"},
    "pdf_doc": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "wps"},
    "pdf_archive": {"pdf", "zip", "rar", "7z"},
}


def get_allowed_extensions(config: AppConfig) -> Optional[set[str]]:
    """返回小写且不含点的扩展名集合；None 表示不过滤。"""
    if config.file_filter == "custom":
        extensions = {
            item.strip().lower().lstrip(".")
            for item in config.custom_exts.split(",")
            if item.strip().lstrip(".")
        }
        return extensions or None
    return FILE_FILTER_PRESETS.get(config.file_filter)


def to_download_config(config: AppConfig):
    """把持久化配置转换为下载引擎配置。"""
    from downloader import DownloadConfig

    return DownloadConfig(
        file_concurrency=config.file_concurrency,
        chunk_threads=config.chunk_threads,
        large_file_threshold=config.large_file_threshold_mb * 1024 * 1024,
        connect_timeout=config.connect_timeout,
        read_timeout=config.read_timeout,
        max_retries=config.max_retries,
        only_extensions=get_allowed_extensions(config),
    )
