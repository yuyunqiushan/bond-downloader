import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import config
import main
import service


class ConfigTests(unittest.TestCase):
    def test_config_path_is_stable_across_working_directories(self):
        expected = os.path.join(os.path.dirname(config.__file__), ".config.json")
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as other_dir:
            try:
                os.chdir(other_dir)
                reloaded = importlib.reload(config)
                self.assertEqual(os.path.abspath(reloaded.CONFIG_PATH), os.path.abspath(expected))
            finally:
                os.chdir(original_cwd)
                importlib.reload(config)

    def test_extension_presets_and_custom_filter(self):
        self.assertEqual(config.get_allowed_extensions(config.AppConfig(file_filter="pdf")), {"pdf"})
        custom = config.AppConfig(file_filter="custom", custom_exts=".PDF, zip, PDF")
        self.assertEqual(config.get_allowed_extensions(custom), {"pdf", "zip"})
        self.assertIsNone(config.get_allowed_extensions(config.AppConfig(file_filter="all")))


class ResolveServiceTests(unittest.TestCase):
    def test_resolve_isolates_errors_deduplicates_and_does_not_create_directories(self):
        def parse(url, base_dir):
            if url == "bad":
                raise RuntimeError("解析失败")
            return [
                {
                    "url": "https://files.example/a.pdf",
                    "filename": "a.pdf",
                    "folder_title": "公告一",
                    "source_url": url,
                    "folder_path": os.path.join(base_dir, "公告一"),
                },
                {
                    "url": "https://files.example/a.pdf",
                    "filename": "a.pdf",
                    "folder_title": "公告一",
                    "source_url": url,
                    "folder_path": os.path.join(base_dir, "公告一"),
                },
                {
                    "url": "https://files.example/b.zip",
                    "filename": "b.zip",
                    "folder_title": "公告一",
                    "source_url": url,
                    "folder_path": os.path.join(base_dir, "公告一"),
                },
            ]

        with tempfile.TemporaryDirectory() as parent:
            output_dir = os.path.join(parent, "not-created")
            parser = mock.Mock()
            parser.parse.side_effect = parse
            with mock.patch.object(service, "URLParser", return_value=parser):
                result = service.resolve_urls(
                    ["good", "bad", "good"], output_dir, allowed_extensions={"pdf"}
                )

            self.assertFalse(os.path.exists(output_dir))
            self.assertEqual(parser.parse.call_count, 2)
            self.assertEqual(len(result["files"]), 1)
            self.assertEqual(result["files"][0]["filename"], "a.pdf")
            self.assertEqual(result["announcements"], [
                {"url": "good", "title": "公告一", "file_count": 1}
            ])
            self.assertEqual(result["parse_errors"], [
                {"url": "bad", "error": "解析失败"}
            ])


class CLIResolveTests(unittest.TestCase):
    def test_resolve_writes_json_with_mocked_parser(self):
        parser = mock.Mock()
        parser.parse.return_value = [{
            "url": "https://files.example/a.pdf",
            "filename": "a.pdf",
            "folder_title": "公告",
            "source_url": "source",
            "folder_path": "unused",
        }]
        stdout = io.StringIO()
        stderr = io.StringIO()
        app_config = config.AppConfig()

        with tempfile.TemporaryDirectory() as output_dir, \
                mock.patch.object(main.AppConfig, "load", return_value=app_config), \
                mock.patch.object(service, "URLParser", return_value=parser), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main([
                "resolve", "--url", "source", "--output-dir", output_dir, "--filter", "pdf"
            ])

        self.assertEqual(code, main.EXIT_SUCCESS)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["files"][0]["filename"], "a.pdf")
        self.assertEqual(payload["parse_errors"], [])
        self.assertEqual(stderr.getvalue(), "")

    def test_resolve_returns_partial_when_some_urls_fail(self):
        result = {
            "announcements": [{"url": "good", "title": "公告", "file_count": 1}],
            "files": [{"filename": "a.pdf"}],
            "parse_errors": [{"url": "bad", "error": "解析失败"}],
        }
        stdout = io.StringIO()
        with mock.patch.object(main.AppConfig, "load", return_value=config.AppConfig()), \
                mock.patch.object(main, "resolve_urls", return_value=result), \
                contextlib.redirect_stdout(stdout):
            code = main.main([
                "resolve", "--url", "good", "--url", "bad", "--output-dir", "."
            ])

        self.assertEqual(code, main.EXIT_PARTIAL)
        self.assertEqual(json.loads(stdout.getvalue()), result)

    def test_resolve_returns_error_when_all_urls_fail(self):
        result = {
            "announcements": [],
            "files": [],
            "parse_errors": [{"url": "bad", "error": "解析失败"}],
        }
        stdout = io.StringIO()
        with mock.patch.object(main.AppConfig, "load", return_value=config.AppConfig()), \
                mock.patch.object(main, "resolve_urls", return_value=result), \
                contextlib.redirect_stdout(stdout):
            code = main.main([
                "resolve", "--url", "bad", "--output-dir", "."
            ])

        self.assertEqual(code, main.EXIT_ERROR)
        self.assertEqual(json.loads(stdout.getvalue()), result)


class CLIDownloadTests(unittest.TestCase):
    def test_json_cancel_keeps_prompt_out_of_stdout(self):
        preview = {
            "announcements": [{"url": "source", "title": "公告", "file_count": 1}],
            "files": [{
                "url": "https://files.example/a.pdf",
                "filename": "a.pdf",
                "folder_title": "公告",
                "source_url": "source",
                "relative_path": os.path.join("公告", "a.pdf"),
            }],
            "parse_errors": [],
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO("n\n")
        app_config = config.AppConfig()
        with mock.patch.object(main.AppConfig, "load", return_value=app_config), \
                mock.patch.object(main, "resolve_urls", return_value=preview), \
                mock.patch.object(main, "download_resolved") as download, \
                mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main([
                "download", "--url", "source", "--output-dir", ".", "--json"
            ])

        self.assertEqual(code, main.EXIT_CANCELLED)
        self.assertTrue(json.loads(stdout.getvalue())["cancelled"])
        self.assertNotIn("确认下载", stdout.getvalue())
        self.assertIn("确认下载", stderr.getvalue())
        download.assert_not_called()

    def test_json_success_outputs_only_final_json(self):
        preview = {
            "announcements": [{"url": "source", "title": "公告", "file_count": 1}],
            "files": [{
                "url": "https://files.example/a.pdf",
                "filename": "a.pdf",
                "folder_title": "公告",
                "source_url": "source",
                "relative_path": os.path.join("公告", "a.pdf"),
            }],
            "parse_errors": [],
        }
        download_result = {
            "stats": {"completed": 1, "skipped": 0, "failed": 0},
            "files": [{"filename": "a.pdf", "status": "completed"}],
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        app_config = config.AppConfig()
        with tempfile.TemporaryDirectory() as output_dir, \
                mock.patch.object(main.AppConfig, "load", return_value=app_config), \
                mock.patch.object(app_config, "save"), \
                mock.patch.object(main, "resolve_urls", return_value=preview), \
                mock.patch.object(main, "download_resolved", return_value=download_result), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main([
                "download", "--url", "source", "--output-dir", output_dir,
                "--json", "--yes",
            ])

        self.assertEqual(code, main.EXIT_SUCCESS)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["download"], download_result)
        self.assertNotIn("解析公告", stdout.getvalue())
        self.assertIn("解析公告", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
