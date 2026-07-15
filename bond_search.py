"""中国债券信息网地方政府债公告检索。"""
from __future__ import annotations

import copy
import json
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

SEARCH_ENDPOINT = "https://www.chinabond.com.cn/cbiw/lgb/infoListByPath"
DETAIL_ENDPOINT = "https://www.chinabond.com.cn/dfz/#/information/listDetail"
PAGE_SIZE = 10

_CATEGORY_ALIASES = {
    "全部": ("zdfzxxpl_xxplwj", 2),
    "发行计划": ("xxplwj_fxjh", 3),
    "发行前披露": ("xxplwj_fxqpl", 3),
    "发行结果": ("xxplwj_fxjg", 3),
    "存续期披露": ("xxplwj_cxqpl", 3),
    "付息兑付与行权公告": ("xxplwj_fxdhyxqgg", 3),
    "其他公告通知": ("xxplwj_qtggtz", 3),
    "制度文件": ("xxplwj_zdwj", 3),
    "承销团管理": ("xxplwj_cxtgl", 3),
}
_CATEGORY_NAMES = {value[0]: name for name, value in _CATEGORY_ALIASES.items()}
_REGION_SUFFIXES = (
    "壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "市"
)
_CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                   "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_ISSUE_TOKEN = r"[零〇一二两三四五六七八九十百千万\d]+"
_ISSUE_RANGE_RE = re.compile(
    rf"第?({_ISSUE_TOKEN})(?:期)?\s*(?:至|到|[-—–~～])\s*第?({_ISSUE_TOKEN})期"
)
_ISSUE_SINGLE_RE = re.compile(rf"第?({_ISSUE_TOKEN})期")
_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


class BondSearchError(RuntimeError):
    """公告检索失败。"""


def clear_search_cache() -> None:
    """清空进程内查询缓存，主要供测试和长驻进程主动刷新。"""
    _CACHE.clear()


def normalize_region(region: str) -> dict[str, Any]:
    original = str(region).strip()
    if not original:
        raise ValueError("地区不能为空")
    short = original
    for suffix in _REGION_SUFFIXES:
        if short.endswith(suffix) and len(short) > len(suffix):
            short = short[:-len(suffix)]
            break
    candidates = list(dict.fromkeys((short, original)))
    return {"input": original, "short": short, "candidates": candidates}


def _chinese_to_int(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        number = int(value)
        if number <= 0:
            raise ValueError("期次必须大于 0")
        return number
    if not value or any(char not in _CHINESE_DIGITS and char not in _CHINESE_UNITS for char in value):
        raise ValueError(f"无法识别期次数字: {value}")
    total = section = number = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
        else:
            unit = _CHINESE_UNITS[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
    result = total + section + number
    if result <= 0:
        raise ValueError("期次必须大于 0")
    return result


def _int_to_chinese(number: int) -> str:
    if number <= 0 or number >= 10000:
        raise ValueError("期次仅支持 1 至 9999")
    digits = "零一二三四五六七八九"
    units = ("", "十", "百", "千")
    chars: list[str] = []
    zero_pending = False
    for position in range(3, -1, -1):
        divisor = 10 ** position
        digit = number // divisor % 10
        if digit:
            if zero_pending and chars:
                chars.append("零")
            if not (position == 1 and digit == 1 and not chars):
                chars.append(digits[digit])
            chars.append(units[position])
            zero_pending = False
        elif chars and number % divisor:
            zero_pending = True
    return "".join(chars)


def normalize_issue(issue: str | int) -> dict[str, Any]:
    text = str(issue).strip().replace("（", "").replace("）", "")
    if not text:
        raise ValueError("期次不能为空")
    text = re.sub(r"\s+", "", text)
    match = _ISSUE_RANGE_RE.search(text)
    if match:
        start, end = _chinese_to_int(match.group(1)), _chinese_to_int(match.group(2))
    else:
        match = _ISSUE_SINGLE_RE.search(text)
        token = match.group(1) if match else text.removeprefix("第").removesuffix("期")
        start = end = _chinese_to_int(token)
    if start > end:
        start, end = end, start
    canonical = f"{_int_to_chinese(start)}期" if start == end else (
        f"{_int_to_chinese(start)}至{_int_to_chinese(end)}期"
    )
    return {"input": str(issue), "start": start, "end": end, "canonical": canonical}


def _title_issue_ranges(title: str) -> list[tuple[int, int]]:
    normalized = title.replace("～", "至").replace("~", "至").replace("—", "至").replace("–", "至")
    ranges: list[tuple[int, int]] = []
    occupied: list[tuple[int, int]] = []
    for match in _ISSUE_RANGE_RE.finditer(normalized):
        try:
            start, end = _chinese_to_int(match.group(1)), _chinese_to_int(match.group(2))
        except ValueError:
            continue
        ranges.append((min(start, end), max(start, end)))
        occupied.append(match.span())
    for match in _ISSUE_SINGLE_RE.finditer(normalized):
        if any(left <= match.start() < right for left, right in occupied):
            continue
        try:
            number = _chinese_to_int(match.group(1))
        except ValueError:
            continue
        ranges.append((number, number))
    return ranges


def _issue_match(target: tuple[int, int], title: str) -> tuple[str, int]:
    start, end = target
    best = ("none", 0)
    for item_start, item_end in _title_issue_ranges(title):
        if (item_start, item_end) == target:
            return "exact", 300
        if start == end and item_start <= start <= item_end:
            best = max(best, ("contains", 200), key=lambda item: item[1])
        elif item_start <= start and end <= item_end:
            best = max(best, ("contains", 180), key=lambda item: item[1])
        elif max(start, item_start) <= min(end, item_end):
            best = max(best, ("overlap", 100), key=lambda item: item[1])
    return best


def _category_config(category: str | None) -> tuple[str, int, str]:
    if category is None:
        return "zdfzxxpl_xxplwj", 2, "全部"
    text = str(category).strip()
    if text in _CATEGORY_ALIASES:
        channel, depth = _CATEGORY_ALIASES[text]
        return channel, depth, text
    if text in _CATEGORY_NAMES:
        return text, 2 if text == "zdfzxxpl_xxplwj" else 3, _CATEGORY_NAMES[text]
    raise ValueError(f"不支持的公告类别: {category}")


def _retry_delay(error: HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.25)


def _request_json(params: dict[str, Any], retries: int = 2) -> dict[str, Any]:
    url = SEARCH_ENDPOINT + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            break
        except HTTPError as error:
            if error.code != 429 and not 500 <= error.code < 600:
                raise BondSearchError(f"公告检索 HTTP {error.code}") from error
            if attempt >= retries:
                raise BondSearchError(f"公告检索重试后仍失败: HTTP {error.code}") from error
            time.sleep(_retry_delay(error, attempt))
        except (OSError, json.JSONDecodeError) as error:
            raise BondSearchError(f"公告检索响应读取失败: {error}") from error
    if not isinstance(payload, dict):
        raise BondSearchError("公告检索响应不是 JSON 对象")
    if str(payload.get("code")) != "200":
        raise BondSearchError(f"公告检索失败: {payload.get('msg') or payload.get('code') or '未知错误'}")
    if not isinstance(payload.get("lgbInfoList"), list) or not isinstance(payload.get("pageParam"), dict):
        raise BondSearchError("公告检索响应缺少 lgbInfoList 或 pageParam")
    return payload


def _fetch_pages(base_params: dict[str, Any], max_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pages_fetched = 0
    reported_total = 0
    for page in range(1, max_pages + 1):
        params = dict(base_params, _tp_lgbInfo=page, t=int(time.time() * 1000))
        payload = _request_json(params)
        page_items = payload["lgbInfoList"]
        page_param = payload["pageParam"]
        pages_fetched += 1
        items.extend(item for item in page_items if isinstance(item, dict))
        try:
            reported_total = max(reported_total, int(page_param.get("total", 0)))
        except (TypeError, ValueError):
            raise BondSearchError("公告检索响应中的 total 无效") from None
        if not page_items or len(items) >= reported_total or len(page_items) < PAGE_SIZE:
            break
    return items, {"pages_fetched": pages_fetched, "reported_total": reported_total}


def _candidate(item: dict[str, Any], region: dict[str, Any], year: int,
               issue: dict[str, Any], requested_channel: str) -> dict[str, Any] | None:
    title = str(item.get("title") or item.get("title1") or "").strip()
    property0 = str(item.get("property0") or "").strip()
    if not title or not property0:
        return None
    region_match = next((value for value in region["candidates"] if value and value in title), "")
    year_match = f"{year}年" in title
    issue_kind, issue_score = _issue_match((issue["start"], issue["end"]), title)
    if not region_match or not year_match or issue_kind == "none":
        return None
    channel_match = requested_channel in property0
    score = 1000 + 500 + issue_score + (40 if channel_match else 0)
    channel = next((code for code in _CATEGORY_NAMES if f"/{code}/" in property0), "")
    return {
        "id": str(item.get("id") or ""),
        "title": title,
        "create_time": str(item.get("createTime") or ""),
        "doc_pub_url": property0,
        "detail_url": DETAIL_ENDPOINT + "?docPubUrl=" + quote(property0, safe=""),
        "channel": channel,
        "category": _CATEGORY_NAMES.get(channel, ""),
        "match": {
            "region": region_match,
            "year": year_match,
            "issue": issue_kind,
            "channel": channel_match,
            "score": score,
        },
    }


def search_announcements(region: str, year: int | str, issue: str | int,
                         category: str | None = None, max_pages: int = 3) -> dict[str, Any]:
    """按地区、年份和期次检索公告，返回所有匹配候选，不自动选择。"""
    region_info = normalize_region(region)
    try:
        year_number = int(year)
    except (TypeError, ValueError):
        raise ValueError("年份必须是四位整数") from None
    if not 2000 <= year_number <= 2100:
        raise ValueError("年份必须在 2000 至 2100 之间")
    issue_info = normalize_issue(issue)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages 必须是正整数")
    channel, depth, category_name = _category_config(category)
    cache_key = (region_info["short"], year_number, issue_info["start"], issue_info["end"],
                 channel, max_pages)
    if cache_key in _CACHE:
        result = copy.deepcopy(_CACHE[cache_key])
        result["query"]["cache_hit"] = True
        return result

    base_params = {
        "pageSize": PAGE_SIZE,
        "channelName": channel,
        "issuer": region_info["short"],
        "disClosureYear": str(year_number),
        "depth": depth,
        "lan": "",
        "infoName": issue_info["canonical"],
    }
    items, metadata = _fetch_pages(base_params, max_pages)
    narrow_count = len(items)
    wide_search_used = False
    if not items:
        wide_search_used = True
        base_params["infoName"] = "专项债券"
        items, metadata = _fetch_pages(base_params, max_pages)

    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        candidate = _candidate(item, region_info, year_number, issue_info, channel)
        if candidate:
            key = candidate["id"] or candidate["doc_pub_url"]
            unique[key] = candidate
    candidates = sorted(
        unique.values(),
        key=lambda item: (item["match"]["score"], item["create_time"], item["id"]),
        reverse=True,
    )
    result = {
        "query": {
            "region": region_info,
            "year": year_number,
            "issue": issue_info,
            "category": category_name,
            "channel_name": channel,
            "depth": depth,
            "page_size": PAGE_SIZE,
            "max_pages": max_pages,
            "cache_hit": False,
        },
        "metadata": {
            "endpoint": SEARCH_ENDPOINT,
            "wide_search_used": wide_search_used,
            "narrow_result_count": narrow_count,
            "pages_fetched": metadata["pages_fetched"],
            "reported_total": metadata["reported_total"],
            "candidate_count": len(candidates),
            "truncated": metadata["reported_total"] > metadata["pages_fetched"] * PAGE_SIZE,
        },
        "candidates": candidates,
    }
    _CACHE[cache_key] = copy.deepcopy(result)
    return result
