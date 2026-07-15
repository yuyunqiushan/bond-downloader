"""
专项债券资料下载器 - 核心下载引擎
支持：文件级并发、大文件分块多线程下载、跨会话断点续传、失败重试
"""
import os
import json
import time
import hashlib
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, Future
from urllib.parse import urlparse
from typing import Callable, Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict
from enum import Enum


class TaskStatus(Enum):
    QUEUED = "queued"         # 排队中
    DOWNLOADING = "downloading"  # 下载中
    PAUSED = "paused"         # 已暂停
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 失败
    SKIPPED = "skipped"       # 已跳过（已存在）


@dataclass
class FileTask:
    """单个文件下载任务"""
    task_id: str
    url: str
    file_path: str
    filename: str
    folder_title: str = ""          # 来源公告标题（用于分组显示）
    source_url: str = ""            # 来源页面 URL
    total_size: int = 0             # 总字节数
    downloaded: int = 0             # 已下载字节数
    status: TaskStatus = TaskStatus.QUEUED
    error: str = ""
    speed: float = 0.0              # 当前速度 B/s
    chunk_size: int = 0             # 使用的分块大小（大文件分块下载时）
    etag: str = ""
    last_modified: str = ""
    retries: int = 0
    max_retries: int = 3
    added_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['status'] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'FileTask':
        d = dict(d)
        d['status'] = TaskStatus(d.get('status', 'queued'))
        return cls(**d)


@dataclass
class DownloadConfig:
    """下载配置"""
    file_concurrency: int = 3           # 同时下载的文件数
    chunk_threads: int = 8              # 大文件分块线程数
    large_file_threshold: int = 50 * 1024 * 1024  # 50MB 以上用分块
    chunk_min_size: int = 8 * 1024 * 1024        # 每个分块最小 8MB
    connect_timeout: int = 30           # 连接超时
    read_timeout: int = 300             # 读取超时
    max_retries: int = 3                # 失败重试次数
    retry_backoff: float = 2.0          # 重试退避基数秒
    only_extensions: Optional[set] = None  # 文件类型过滤（None=全部）
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


class DownloadEngine:
    """下载引擎 - 调度器"""

    def __init__(self, base_dir: str, config: DownloadConfig = None,
                 event_callback: Callable = None):
        self.base_dir = base_dir
        self.config = config or DownloadConfig()
        self.event_callback = event_callback  # (event_type, data) -> None
        self.headers = {"User-Agent": self.config.user_agent}

        # 会话（连接池复用）
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self.config.file_concurrency * 2,
            pool_maxsize=self.config.file_concurrency * self.config.chunk_threads + 10,
            pool_block=False,
        )
        self._session.mount('https://', adapter)
        self._session.mount('http://', adapter)
        self._session.headers.update(self.headers)

        # 任务管理
        self._tasks: Dict[str, FileTask] = {}       # task_id -> FileTask
        self._task_order: List[str] = []            # 任务ID顺序
        self._lock = threading.Lock()

        # 执行状态
        self._running = False
        self._paused = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._active_futures: Dict[str, Future] = {}  # task_id -> Future
        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 待添加任务的源数（parser 正在工作时 >0，调度器不会误判为完成）
        self._pending_sources = 0
        self._sources_done_event = threading.Event()
        self._sources_done_event.set()  # 初始没有待添加的源

        # 统计（速度使用滑动窗口计算）
        self._start_time = 0.0
        self._last_progress_time = 0.0
        self._last_completed_bytes = 0
        self._current_speed = 0.0
        self._speed_samples = []  # [(timestamp, bytes_done)]

        # 状态文件
        os.makedirs(base_dir, exist_ok=True)
        self._state_file = os.path.join(base_dir, ".download_state.json")
        self._error_log = os.path.join(base_dir, ".download_errors.log")
        self._part_dir = os.path.join(base_dir, ".part")

    # ---------- 事件 ----------
    def _emit(self, event: str, data: Any = None):
        if self.event_callback:
            try:
                self.event_callback(event, data)
            except Exception:
                pass

    # ---------- 任务管理 ----------
    def _gen_task_id(self, url: str, file_path: str) -> str:
        key = f"{url}|{file_path}"
        return hashlib.md5(key.encode('utf-8')).hexdigest()[:12]

    def _sanitize_filename(self, name: str) -> str:
        """清理文件名中的非法字符"""
        import re
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()
        # 去除末尾空格/点（Windows 不允许）
        name = name.rstrip('. ')
        return name or "unnamed"

    def _match_extension(self, filename: str) -> bool:
        """检查文件扩展名是否符合过滤规则"""
        if self.config.only_extensions is None:
            return True
        ext = os.path.splitext(filename)[1].lower().lstrip('.')
        return ext in self.config.only_extensions

    def submit_file(self, url: str, filename: str, folder_title: str = "",
                    source_url: str = "", folder_path: str = None) -> Optional[FileTask]:
        """提交一个待下载文件，返回任务对象（已存在则跳过）"""
        filename = self._sanitize_filename(filename)

        if not self._match_extension(filename):
            return None

        if folder_path is None:
            safe_folder = self._sanitize_filename(folder_title) if folder_title else ""
            folder_path = os.path.join(self.base_dir, safe_folder) if safe_folder else self.base_dir

        os.makedirs(folder_path, exist_ok=True)
        file_path = os.path.join(folder_path, filename)

        task_id = self._gen_task_id(url, file_path)

        with self._lock:
            if task_id in self._tasks:
                return self._tasks[task_id]

            task = FileTask(
                task_id=task_id,
                url=url,
                file_path=file_path,
                filename=filename,
                folder_title=folder_title,
                source_url=source_url,
                max_retries=self.config.max_retries,
            )
            self._tasks[task_id] = task
            self._task_order.append(task_id)

        self._emit("task_added", task)
        return task

    def submit_urls_resolved(self, resolved_files: list):
        """批量提交 parser 解析好的文件列表
        resolved_files: [{"url", "filename", "folder_title", "source_url", "folder_path"}, ...]
        """
        for item in resolved_files:
            self.submit_file(
                url=item["url"],
                filename=item["filename"],
                folder_title=item.get("folder_title", ""),
                source_url=item.get("source_url", ""),
                folder_path=item.get("folder_path"),
            )

    def begin_adding_source(self):
        """标记开始添加一批源 URL（解析中），调度器会等待解析完成再判断是否结束"""
        with self._lock:
            self._pending_sources += 1
            self._sources_done_event.clear()

    def finish_adding_source(self):
        """标记一批源 URL 添加完成"""
        with self._lock:
            self._pending_sources = max(0, self._pending_sources - 1)
            if self._pending_sources == 0:
                self._sources_done_event.set()

    def get_task(self, task_id: str) -> Optional[FileTask]:
        return self._tasks.get(task_id)

    def get_all_tasks(self) -> List[FileTask]:
        with self._lock:
            return [self._tasks[tid] for tid in self._task_order]

    def get_stats(self) -> dict:
        with self._lock:
            tasks = list(self._tasks.values())
        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)
        skipped = sum(1 for t in tasks if t.status == TaskStatus.SKIPPED)
        queued = sum(1 for t in tasks if t.status == TaskStatus.QUEUED)
        downloading = sum(1 for t in tasks if t.status == TaskStatus.DOWNLOADING)
        paused_count = sum(1 for t in tasks if t.status == TaskStatus.PAUSED)
        total_bytes = sum(t.total_size for t in tasks if t.total_size)
        total_downloaded = sum(t.downloaded for t in tasks)
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "queued": queued,
            "downloading": downloading,
            "paused_count": paused_count,
            "total_bytes": total_bytes,
            "total_downloaded": total_downloaded,
            "speed": self._current_speed,
            "running": self._running and not self._paused,
            "paused": self._paused,
        }

    # ---------- 控制 ----------
    def start(self):
        """启动调度器"""
        if self._running:
            return
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.file_concurrency * (self.config.chunk_threads + 2),
            thread_name_prefix="dl"
        )
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()
        self._start_time = time.time()
        self._last_progress_time = time.time()
        self._speed_samples = []
        self._emit("started", None)
    def pause(self):
        """暂停"""
        if not self._running:
            return
        self._paused = True
        self._emit("paused", None)
        self._save_state()

    def resume(self):
        """继续"""
        if not self._running:
            self.start()
            return
        if not self._paused:
            return
        self._paused = False
        self._emit("resumed", None)

    def stop(self, wait: bool = True):
        """停止（不可继续）"""
        self._running = False
        self._paused = False
        self._stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._executor = None
        self._save_state()
        self._emit("stopped", None)

    def wait_finished(self, timeout: float = None):
        """等待所有任务完成/失败"""
        end = time.time() + timeout if timeout else None
        while True:
            if end and time.time() > end:
                return False
            with self._lock:
                active_tasks = sum(
                    1 for task in self._tasks.values()
                    if task.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING)
                )
                pending_sources = self._pending_sources
                active_futures = any(not future.done() for future in self._active_futures.values())
            if active_tasks == 0 and pending_sources == 0 and not active_futures:
                return True
            if not self._running and pending_sources == 0 and not active_futures:
                return True
            time.sleep(0.2)

    # ---------- 调度循环 ----------
    def _scheduler_loop(self):
        try:
            while self._running and not self._stop_event.is_set():
                if self._paused:
                    time.sleep(0.2)
                    continue

                # 清理完成的 future
                done_ids = []
                for tid, fut in list(self._active_futures.items()):
                    if fut.done():
                        done_ids.append(tid)
                        t = self._tasks.get(tid)
                        try:
                            fut.result()
                            # 正常完成的任务 _run_task 已标记 COMPLETED
                        except Exception as e:
                            if t is not None:
                                self._handle_task_failure(t, e)
                        self._emit("task_done", t)
                for tid in done_ids:
                    self._active_futures.pop(tid, None)

                # 派发新任务
                while len(self._active_futures) < self.config.file_concurrency:
                    task = self._pick_next_task()
                    if task is None:
                        break
                    # 检查已存在
                    if os.path.exists(task.file_path) and task.status != TaskStatus.PAUSED:
                        # 判断大小是否一致
                        try:
                            size = os.path.getsize(task.file_path)
                            if task.total_size and size == task.total_size:
                                task.status = TaskStatus.SKIPPED
                                task.downloaded = size
                                self._emit("task_skipped", task)
                                continue
                            elif size > 0 and not task.total_size:
                                # 未知大小但文件已存在，视为已下载
                                task.status = TaskStatus.SKIPPED
                                task.downloaded = size
                                self._emit("task_skipped", task)
                                continue
                        except OSError:
                            pass
                    # 文件不存在但同名解压目录存在（用户下完即解压删原件）
                    elif not os.path.exists(task.file_path) and os.path.isdir(
                        os.path.splitext(task.file_path)[0]
                    ):
                        task.status = TaskStatus.SKIPPED
                        self._emit("task_skipped", task)
                        continue

                    task.status = TaskStatus.DOWNLOADING
                    task.error = ""
                    fut = self._executor.submit(self._run_task, task)
                    self._active_futures[task.task_id] = fut
                    self._emit("task_started", task)

                # 进度节流更新速度（滑动窗口最近 3 秒）
                now = time.time()
                if now - self._last_progress_time >= 0.5:
                    with self._lock:
                        dl = sum(t.downloaded for t in self._tasks.values())
                    self._speed_samples.append((now, dl))
                    # 保留最近 5 秒的样本
                    cutoff = now - 5
                    self._speed_samples = [s for s in self._speed_samples if s[0] >= cutoff]
                    if len(self._speed_samples) >= 2:
                        t0, b0 = self._speed_samples[0]
                        t1, b1 = self._speed_samples[-1]
                        dt = t1 - t0
                        if dt > 0.1:
                            self._current_speed = (b1 - b0) / dt
                        else:
                            self._current_speed = 0
                    else:
                        self._current_speed = 0
                    self._last_progress_time = now
                    self._emit("progress", self.get_stats())

                # 检查是否全部完成
                with self._lock:
                    remaining = sum(1 for t in self._tasks.values()
                                    if t.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING))
                    pending = self._pending_sources
                if remaining == 0 and not self._active_futures and pending == 0:
                    # 处理被暂停任务的情况
                    paused_count = sum(1 for t in self._tasks.values() if t.status == TaskStatus.PAUSED)
                    if paused_count == 0:
                        break
                elif remaining == 0 and not self._active_futures and pending > 0:
                    # 还有源在解析，稍等
                    pass

                time.sleep(0.1)

            self._running = False
            self._emit("all_done", self.get_stats())
            self._save_state()
        except Exception as e:
            self._log_error(f"调度器异常: {e}")
            self._emit("fatal_error", str(e))

    def _pick_next_task(self) -> Optional[FileTask]:
        """挑选下一个排队中的任务"""
        with self._lock:
            for tid in self._task_order:
                t = self._tasks[tid]
                if t.status == TaskStatus.QUEUED:
                    return t
                if t.status == TaskStatus.PAUSED and not self._paused:
                    return t
        return None

    # ---------- 下载单个文件 ----------
    def _run_task(self, task: FileTask):
        """执行单个文件下载（在 executor 线程中）"""
        # 如果已暂停/停止，直接标记
        if self._paused or self._stop_event.is_set():
            task.status = TaskStatus.PAUSED
            return

        # 获取文件信息
        file_info = self._probe_file(task)
        if file_info is None:
            raise RuntimeError("无法获取文件信息")

        task.total_size = file_info.get('size', 0)
        task.etag = file_info.get('etag', '')
        task.last_modified = file_info.get('last_modified', '')

        # 判断是否分块下载
        if (task.total_size >= self.config.large_file_threshold
                and task.total_size > 0
                and file_info.get("supports_range", False)):
            self._download_chunked(task)
        else:
            self._download_single(task)

        # 校验
        if os.path.exists(task.file_path):
            actual = os.path.getsize(task.file_path)
            if task.total_size and actual != task.total_size:
                raise RuntimeError(f"文件大小不匹配: 期望 {task.total_size}, 实际 {actual}")
            task.downloaded = actual
            task.status = TaskStatus.COMPLETED
            self._emit("task_completed", task)
        else:
            raise RuntimeError("文件未生成")

    def _handle_task_failure(self, task: FileTask, error: Exception):
        """在调度器线程处理任务失败（决定重试或标记失败）"""
        if self._stop_event.is_set() or self._paused:
            task.status = TaskStatus.PAUSED
            self._save_state()
            return
        task.retries += 1
        if task.retries < task.max_retries:
            backoff = self.config.retry_backoff ** task.retries
            self._emit("task_retry", {"task": task, "attempt": task.retries, "error": str(error), "backoff": backoff})
            # 等退避时间后放回队列（用定时器线程）
            def requeue():
                time.sleep(backoff)
                if not self._stop_event.is_set() and not self._paused:
                    task.status = TaskStatus.QUEUED
                    task.error = ""
            threading.Thread(target=requeue, daemon=True).start()
            return
        task.status = TaskStatus.FAILED
        task.error = str(error)
        self._log_error(f"下载失败 [{task.filename}]: {error}")
        self._emit("task_failed", task)

    def _probe_file(self, task: FileTask) -> Optional[dict]:
        """探测文件大小、ETag、Last-Modified，支持 HEAD/GET 兜底"""
        # 先尝试 HEAD
        try:
            resp = self._session.head(
                task.url, timeout=(self.config.connect_timeout, self.config.read_timeout),
                allow_redirects=True
            )
            if resp.status_code < 400:
                size = int(resp.headers.get('Content-Length', 0) or 0)
                return {
                    'size': size,
                    'etag': resp.headers.get('ETag', ''),
                    'last_modified': resp.headers.get('Last-Modified', ''),
                    'supports_range': 'bytes' in resp.headers.get('Accept-Ranges', '').lower(),
                }
        except requests.RequestException:
            pass

        # HEAD 失败，尝试 Range: bytes=0-0
        try:
            headers = {"Range": "bytes=0-0"}
            resp = self._session.get(
                task.url, headers=headers, stream=True,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                allow_redirects=True
            )
            if resp.status_code < 400:
                cr = resp.headers.get('Content-Range', '')
                size = 0
                if '/' in cr:
                    try:
                        size = int(cr.rsplit('/', 1)[1])
                    except (ValueError, IndexError):
                        size = 0
                supports_range = resp.status_code == 206
                resp.close()
                return {
                    'size': size,
                    'etag': resp.headers.get('ETag', ''),
                    'last_modified': resp.headers.get('Last-Modified', ''),
                    'supports_range': supports_range,
                }
        except requests.RequestException:
            pass

        # 兜底：普通 GET
        try:
            resp = self._session.get(
                task.url, stream=True,
                timeout=(self.config.connect_timeout, self.config.read_timeout),
                allow_redirects=True
            )
            if resp.status_code < 400:
                size = int(resp.headers.get('Content-Length', 0) or 0)
                resp.close()
                return {
                    'size': size,
                    'etag': resp.headers.get('ETag', ''),
                    'last_modified': resp.headers.get('Last-Modified', ''),
                    'supports_range': False,
                }
        except requests.RequestException:
            pass

        return None

    def _download_single(self, task: FileTask):
        """单线程下载"""
        headers = {}
        mode = 'wb'
        existing = 0
        if os.path.exists(task.file_path):
            existing = os.path.getsize(task.file_path)
            if existing > 0 and task.total_size > existing:
                # 断点续传
                headers["Range"] = f"bytes={existing}-"
                mode = 'ab'
            elif existing > 0 and task.total_size and existing == task.total_size:
                task.downloaded = existing
                return
            else:
                existing = 0

        os.makedirs(os.path.dirname(task.file_path) or '.', exist_ok=True)
        resp = self._session.get(
            task.url, headers=headers or None, stream=True,
            timeout=(self.config.connect_timeout, self.config.read_timeout),
            allow_redirects=True
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        # 如果服务器忽略 Range 返回 200，重新覆盖下载
        if mode == 'ab' and resp.status_code == 200:
            mode = 'wb'
            existing = 0

        task.downloaded = existing
        last_emit = 0.0
        with open(task.file_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if self._stop_event.is_set() or self._paused:
                    return
                if not chunk:
                    continue
                f.write(chunk)
                chunk_len = len(chunk)
                task.downloaded += chunk_len
                now = time.time()
                if now - last_emit > 0.2:
                    task.speed = 0
                    self._emit("task_progress", task)
                    last_emit = now

        self._emit("task_progress", task)

    def _download_chunked(self, task: FileTask):
        """分块多线程下载"""
        os.makedirs(self._part_dir, exist_ok=True)
        chunk_dir = os.path.join(self._part_dir, task.task_id)
        os.makedirs(chunk_dir, exist_ok=True)

        size = task.total_size
        num_threads = min(self.config.chunk_threads, max(1, size // self.config.chunk_min_size))
        num_threads = max(1, num_threads)
        chunk_size = size // num_threads
        ranges = [(i * chunk_size, (i + 1) * chunk_size - 1) for i in range(num_threads)]
        ranges[-1] = (ranges[-1][0], size - 1)
        task.chunk_size = chunk_size

        # 检查已有分块（断点续传）
        completed_chunks = set()
        chunk_lock = threading.Lock()

        # 重新统计已存在的 chunk，初始化 task.downloaded
        already_done = 0
        for i in range(num_threads):
            chunk_path = os.path.join(chunk_dir, f"chunk_{i}")
            expected = ranges[i][1] - ranges[i][0] + 1
            if os.path.exists(chunk_path) and os.path.getsize(chunk_path) == expected:
                completed_chunks.add(i)
                already_done += expected
        task.downloaded = already_done

        # 准备线程
        def download_one_chunk(i: int):
            if self._stop_event.is_set() or self._paused:
                return
            if i in completed_chunks:
                return
            start, end = ranges[i]
            expected = end - start + 1
            chunk_path = os.path.join(chunk_dir, f"chunk_{i}")
            # 支持部分 chunk 的续传
            existing = 0
            if os.path.exists(chunk_path):
                existing = os.path.getsize(chunk_path)
                if existing >= expected:
                    with chunk_lock:
                        completed_chunks.add(i)
                    return
            headers = {"Range": f"bytes={start + existing}-{end}"}
            mode = 'ab' if existing > 0 else 'wb'
            last_emit = 0.0
            for attempt in range(task.max_retries):
                try:
                    resp = self._session.get(
                        task.url, headers=headers, stream=True,
                        timeout=(self.config.connect_timeout, self.config.read_timeout),
                        allow_redirects=True
                    )
                    if resp.status_code != 206:
                        raise RuntimeError(f"Range 请求需要 HTTP 206，实际为 {resp.status_code}")
                    with open(chunk_path, mode) as f:
                        # 下载时通过文件当前大小追踪进度（避免在多 chunk 并行时 task.downloaded 不准确）
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if self._stop_event.is_set() or self._paused:
                                return
                            if not chunk:
                                continue
                            f.write(chunk)
                            # 更新进度：重新统计所有 chunk 目录下已写入的字节
                            with self._lock:
                                total_written = 0
                                for ci in range(num_threads):
                                    cp = os.path.join(chunk_dir, f"chunk_{ci}")
                                    try:
                                        total_written += os.path.getsize(cp)
                                    except OSError:
                                        pass
                                task.downloaded = total_written
                            now = time.time()
                            if now - last_emit > 0.3:
                                self._emit("task_progress", task)
                                last_emit = now
                    # 校验 chunk 大小
                    if os.path.getsize(chunk_path) != expected:
                        raise RuntimeError(f"chunk {i} 大小不匹配")
                    with chunk_lock:
                        completed_chunks.add(i)
                    return
                except Exception as e:
                    if attempt < task.max_retries - 1 and not self._stop_event.is_set() and not self._paused:
                        time.sleep(self.config.retry_backoff ** (attempt + 1))
                        # 删除可能写了一半的 chunk 文件
                        try:
                            if os.path.exists(chunk_path):
                                os.remove(chunk_path)
                        except OSError:
                            pass
                        existing = 0
                        mode = 'wb'
                        headers["Range"] = f"bytes={start}-{end}"
                        continue
                    raise

        # 在调用者线程用局部 ThreadPoolExecutor 并行下各 chunk
        with ThreadPoolExecutor(max_workers=num_threads, thread_name_prefix=f"chk-{task.task_id[:6]}") as pool:
            futures = [pool.submit(download_one_chunk, i) for i in range(num_threads)]
            for fut in futures:
                fut.result()  # 抛异常直接外抛到 _run_task

        # 合并
        os.makedirs(os.path.dirname(task.file_path) or '.', exist_ok=True)
        with open(task.file_path, 'wb') as out:
            for i in range(num_threads):
                chunk_path = os.path.join(chunk_dir, f"chunk_{i}")
                with open(chunk_path, 'rb') as cin:
                    while True:
                        buf = cin.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
                try:
                    os.remove(chunk_path)
                except OSError:
                    pass
        # 清理 chunk 目录
        try:
            os.rmdir(chunk_dir)
        except OSError:
            pass

    # ---------- 状态持久化 ----------
    def _save_state(self):
        """保存未完成任务状态"""
        try:
            state = {
                "base_dir": self.base_dir,
                "paused": self._paused,
                "tasks": [],
            }
            for tid, t in self._tasks.items():
                if t.status in (TaskStatus.QUEUED, TaskStatus.DOWNLOADING,
                                TaskStatus.FAILED, TaskStatus.PAUSED):
                    td = t.to_dict()
                    state["tasks"].append(td)
            tmp = self._state_file + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._state_file)
        except Exception as e:
            self._log_error(f"保存状态失败: {e}")

    def load_state(self) -> int:
        """加载未完成任务状态，返回恢复的任务数"""
        if not os.path.exists(self._state_file):
            return 0
        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            return 0
        count = 0
        with self._lock:
            for td in state.get('tasks', []):
                try:
                    task = FileTask.from_dict(td)
                    if task.status == TaskStatus.DOWNLOADING:
                        task.status = TaskStatus.PAUSED
                    if os.path.exists(task.file_path) and task.total_size and os.path.getsize(task.file_path) == task.total_size:
                        task.status = TaskStatus.COMPLETED
                        task.downloaded = task.total_size
                    if task.task_id not in self._tasks:
                        self._tasks[task.task_id] = task
                        self._task_order.append(task.task_id)
                        count += 1
                except Exception:
                    continue
        return count

    def clear_state(self):
        """任务全部完成后清理状态文件"""
        try:
            if os.path.exists(self._state_file):
                os.remove(self._state_file)
        except OSError:
            pass
        # 清理 .part 目录
        try:
            if os.path.isdir(self._part_dir):
                for root, dirs, files in os.walk(self._part_dir, topdown=False):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except OSError:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except OSError:
                            pass
                os.rmdir(self._part_dir)
        except OSError:
            pass

    def _log_error(self, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self._error_log, 'a', encoding='utf-8') as f:
                f.write(f"[{ts}] {msg}\n")
        except OSError:
            pass

    def retry_failed(self):
        """把失败任务重新加入队列"""
        with self._lock:
            for tid in self._task_order:
                t = self._tasks[tid]
                if t.status == TaskStatus.FAILED:
                    t.status = TaskStatus.QUEUED
                    t.retries = 0
                    t.error = ""
        if not self._running:
            self.start()

    def close(self):
        """释放引擎所有资源：停止调度、等待线程结束、关闭网络会话。"""
        self.stop(wait=True)
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=5)
        try:
            self._session.close()
        except Exception:
            pass
