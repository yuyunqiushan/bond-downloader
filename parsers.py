"""
专项债券资料下载器 - URL 解析器
负责把不同来源的详情页 URL 解析成具体的附件下载链接列表
"""
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import unquote, urlparse, parse_qs


class URLParser:
    """解析各类债券公告页面，提取附件列表"""

    def __init__(self, headers: dict = None):
        self.headers = headers or {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def _get_html(self, url: str, encoding: str = None) -> str:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        if encoding:
            resp.encoding = encoding
            return resp.text
        # 自动检测编码
        if resp.encoding and resp.encoding.lower() in ('iso-8859-1',):
            # 用 apparent_encoding 兜底
            resp.encoding = resp.apparent_encoding
        return resp.text

    def _get_json(self, url: str) -> dict:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _sanitize_filename(self, name: str) -> str:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().rstrip('. ')
        return name or "unnamed"

    def parse(self, url: str, base_dir: str) -> list:
        """解析一个详情页 URL，返回文件列表
        返回: [{"url", "filename", "folder_title", "source_url", "folder_path"}, ...]
        """
        url = url.strip()
        if not url:
            return []

        # 新版 chinabond JSON SPA 详情页
        if "chinabond.com.cn/dfz/#/information/listDetail" in url:
            return self._parse_chinabond_new(url, base_dir)
        # celma 公告页
        elif url.startswith("https://www.celma.org.cn/fxqgg"):
            return self._parse_celma(url, base_dir)
        # 注意：旧版 HTML 页面匹配要放在短前缀之前，避免被父前缀吞掉
        elif "chinabond.com.cn/xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl" in url:
            return self._parse_chinabond_old(url, base_dir)
        elif "chinabond.com.cn/xxpl/ywzc_fxyfxdh" in url:
            return self._parse_chinabond_old(url, base_dir)
        else:
            raise ValueError(f"无法识别的 URL 类型: {url}")

    def _parse_chinabond_new(self, url: str, base_dir: str) -> list:
        """解析 chinabond 新版 JSON 接口（docPubUrl 形式）"""
        match = re.search(r'docPubUrl=([^&]+)', url)
        if not match:
            # 旧版 title+id 形式
            match2 = re.search(r'title=([^&]+)', url)
            if match2:
                return self._parse_chinabond_old_id(url, base_dir)
            raise ValueError("无法从 URL 中提取 docPubUrl 或 title 参数")

        doc_pub_url = unquote(unquote(match.group(1)))
        data = self._get_json(doc_pub_url)

        # 获取文件夹名
        folder_title = ""
        if isinstance(data, dict):
            folder_title = data.get('title') or ""
        if not folder_title:
            json_filename = os.path.basename(doc_pub_url.split('?')[0])
            folder_title = re.sub(r'\.json$', '', json_filename, flags=re.I)
        folder_title = self._sanitize_filename(folder_title)
        folder_path = os.path.join(base_dir, folder_title)

        files = []
        file_list = data.get('files', []) if isinstance(data, dict) else []
        for attachment in file_list:
            filename = attachment.get('srcFile') or attachment.get('filename') or ""
            download_url = attachment.get('url') or attachment.get('downloadUrl') or ""
            if not filename or not download_url:
                continue
            filename = self._sanitize_filename(filename)
            files.append({
                "url": download_url,
                "filename": filename,
                "folder_title": folder_title,
                "source_url": url,
                "folder_path": folder_path,
            })
        return files

    def _parse_chinabond_old_id(self, url: str, base_dir: str) -> list:
        """解析 chinabond 旧版 JSON 接口（title+id 参数）"""
        title_match = re.search(r'title=([^&]+)', url)
        id_match = re.search(r'id=(\d+)', url)
        if not title_match or not id_match:
            raise ValueError("无法提取 title 或 id 参数")

        folder_title = self._sanitize_filename(unquote(title_match.group(1)))
        folder_path = os.path.join(base_dir, folder_title)

        file_list_url = f"https://www.chinabond.com.cn/cbiw/lgb/fileByInfoId?infoid={id_match.group(1)}"
        data = self._get_json(file_list_url)

        files = []
        for attachment in data:
            filename = attachment.get('SRCFILE') or attachment.get('srcFile') or ""
            download_url = attachment.get('DOWNLOADURL') or attachment.get('url') or ""
            if not filename or not download_url:
                continue
            filename = self._sanitize_filename(filename)
            files.append({
                "url": download_url,
                "filename": filename,
                "folder_title": folder_title,
                "source_url": url,
                "folder_path": folder_path,
            })
        return files

    def _parse_celma(self, url: str, base_dir: str) -> list:
        """解析 celma.org.cn 公告页"""
        html = self._get_html(url)
        soup = BeautifulSoup(html, "html.parser")

        page_title = ""
        h1 = soup.find("h1")
        if h1:
            page_title = h1.get_text().strip()
        page_title = self._sanitize_filename(page_title) or "celma"
        folder_path = os.path.join(base_dir, page_title)

        files = []
        fj_div = soup.find("div", class_="content-fj")
        if fj_div:
            for a in fj_div.find_all("a"):
                href = a.get("href", "")
                title = a.get("title") or a.get_text().strip()
                if not href or not title:
                    continue
                # 相对路径补全
                if not href.startswith(('http://', 'https://')):
                    from urllib.parse import urljoin
                    href = urljoin(url, href)
                title = self._sanitize_filename(title)
                files.append({
                    "url": href,
                    "filename": title,
                    "folder_title": page_title,
                    "source_url": url,
                    "folder_path": folder_path,
                })
        return files

    def _parse_chinabond_old(self, url: str, base_dir: str) -> list:
        """解析 chinabond 普通 HTML 附件页"""
        html = self._get_html(url)
        soup = BeautifulSoup(html, "html.parser")

        page_title = ""
        title_tag = soup.find("title")
        if title_tag:
            page_title = title_tag.get_text().strip()
        page_title = self._sanitize_filename(page_title)
        folder_path = os.path.join(base_dir, page_title)

        files = []
        for li in soup.find_all("li", class_="File fileList"):
            a = li.find("a")
            if not a:
                continue
            href = a.get("href", "")
            filename = a.get("download") or a.get_text().strip()
            if not href or not filename:
                continue
            from urllib.parse import urljoin
            href = urljoin(url, href)
            filename = self._sanitize_filename(filename)
            files.append({
                "url": href,
                "filename": filename,
                "folder_title": page_title,
                "source_url": url,
                "folder_path": folder_path,
            })
        return files
