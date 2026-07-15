import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from downloader import DownloadConfig, DownloadEngine, FileTask, TaskStatus


class FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self._content = content

    def iter_content(self, chunk_size):
        if self._content:
            yield self._content


class DownloaderReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.engine = DownloadEngine(
            self.temp_dir.name,
            DownloadConfig(max_retries=1, chunk_threads=1, chunk_min_size=1),
        )
        self.addCleanup(self.engine._session.close)

    def _task(self, filename="a.bin"):
        return FileTask(
            task_id="task",
            url="https://files.example/a.bin",
            file_path=os.path.join(self.temp_dir.name, filename),
            filename=filename,
            max_retries=1,
        )

    def test_get_stats_has_paused_count_and_boolean_paused(self):
        task = self._task()
        task.status = TaskStatus.PAUSED
        self.engine._tasks[task.task_id] = task
        self.engine._task_order.append(task.task_id)
        self.engine._paused = True

        stats = self.engine.get_stats()

        self.assertEqual(stats["paused_count"], 1)
        self.assertIs(stats["paused"], True)

    def test_wait_finished_waits_for_pending_source(self):
        self.engine.begin_adding_source()
        timer = threading.Timer(0.05, self.engine.finish_adding_source)
        timer.start()
        started = time.monotonic()
        try:
            self.assertTrue(self.engine.wait_finished(timeout=1))
        finally:
            timer.join()
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    def test_wait_finished_waits_for_active_future(self):
        future = Future()
        self.engine._active_futures["task"] = future
        timer = threading.Timer(0.05, lambda: future.set_result(None))
        timer.start()
        started = time.monotonic()
        try:
            self.assertTrue(self.engine.wait_finished(timeout=1))
        finally:
            timer.join()
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    def test_large_file_without_range_uses_single_stream(self):
        task = self._task()
        self.engine.config.large_file_threshold = 1
        with mock.patch.object(
            self.engine,
            "_probe_file",
            return_value={"size": 10, "etag": "", "last_modified": "", "supports_range": False},
        ), mock.patch.object(self.engine, "_download_single") as single, \
                mock.patch.object(self.engine, "_download_chunked") as chunked, \
                mock.patch("downloader.os.path.exists", return_value=True), \
                mock.patch("downloader.os.path.getsize", return_value=10):
            self.engine._run_task(task)

        single.assert_called_once_with(task)
        chunked.assert_not_called()
        self.assertEqual(task.status, TaskStatus.COMPLETED)

    def test_extracted_directory_counted_as_skipped(self):
        task = self._task("downloaded.rar")
        task.status = TaskStatus.QUEUED
        self.engine._tasks[task.task_id] = task
        self.engine._task_order.append(task.task_id)
        # 创建同名解压目录（不创建 .rar 文件）
        os.makedirs(os.path.splitext(task.file_path)[0], exist_ok=True)
        self.addCleanup(os.rmdir, os.path.splitext(task.file_path)[0])

        with mock.patch.object(self.engine, "_emit"):
            self.engine.start()
            self.engine.wait_finished(timeout=2)
            self.engine.stop(wait=True)

        self.assertEqual(task.status, TaskStatus.SKIPPED)

    def test_chunked_download_rejects_http_200_for_range(self):
        task = self._task()
        task.total_size = 4
        self.engine._session.get = mock.Mock(return_value=FakeResponse(status_code=200, content=b"abcd"))

        with self.assertRaisesRegex(RuntimeError, "HTTP 206"):
            self.engine._download_chunked(task)


if __name__ == "__main__":
    unittest.main()
