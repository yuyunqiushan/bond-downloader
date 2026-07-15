import json
import os
import sys
import unittest
from email.message import Message
from urllib.error import HTTPError
from unittest import mock

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import bond_search


def payload(items, page=1, total=None):
    return {
        "code": "200",
        "msg": "success",
        "lgbInfoList": items,
        "pageParam": {"pageSize": 10, "pageNumber": page, "total": len(items) if total is None else total},
    }


def item(item_id, title, date="2026-07-15", channel="xxplwj_fxqpl"):
    doc = (
        "https://www.chinabond.com.cn/xxpl/zdfzxxpl_xxplwj/"
        f"{channel}/202607/t20260715_{item_id}.json"
    )
    return {"id": item_id, "title": title, "createTime": date, "property0": doc}


class NumberNormalizationTests(unittest.TestCase):
    def test_single_and_range_normalization(self):
        self.assertEqual(bond_search.normalize_issue("第25期")["canonical"], "二十五期")
        value = bond_search.normalize_issue("三十至二十五期")
        self.assertEqual((value["start"], value["end"], value["canonical"]),
                         (25, 30, "二十五至三十期"))
        self.assertEqual(bond_search.normalize_issue("101期")["canonical"], "一百零一期")

    def test_region_normalization(self):
        value = bond_search.normalize_region("广西壮族自治区")
        self.assertEqual(value["short"], "广西")
        self.assertEqual(value["candidates"], ["广西", "广西壮族自治区"])


class SearchTests(unittest.TestCase):
    def setUp(self):
        bond_search.clear_search_cache()

    @mock.patch.object(bond_search, "_request_json")
    def test_success_and_detail_url_and_cache(self, request_json):
        request_json.return_value = payload([
            item("1", "2026年浙江省政府专项债券（二十五至三十期）信息披露文件")
        ])
        result = bond_search.search_announcements("浙江省", 2026, "25-30期")
        self.assertEqual(result["metadata"]["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["match"]["issue"], "exact")
        self.assertIn("docPubUrl=https%3A%2F%2F", candidate["detail_url"])
        self.assertFalse(result["query"]["cache_hit"])

        cached = bond_search.search_announcements("浙江省", 2026, "25-30期")
        self.assertTrue(cached["query"]["cache_hit"])
        request_json.assert_called_once()
        params = request_json.call_args.args[0]
        self.assertEqual(params["infoName"], "二十五至三十期")
        self.assertEqual(params["issuer"], "浙江")

    @mock.patch.object(bond_search, "_request_json")
    def test_zero_narrow_result_uses_one_wide_search(self, request_json):
        request_json.side_effect = [payload([]), payload([
            item("2", "2026年浙江省政府专项债券（二十五至三十期）信息披露文件")
        ])]
        result = bond_search.search_announcements("浙江", "2026", "25期")
        self.assertTrue(result["metadata"]["wide_search_used"])
        self.assertEqual(result["candidates"][0]["match"]["issue"], "contains")
        self.assertEqual(request_json.call_count, 2)
        self.assertEqual(request_json.call_args_list[1].args[0]["infoName"], "专项债券")

    @mock.patch.object(bond_search, "_request_json")
    def test_pagination(self, request_json):
        first = [item(str(number), f"2026年浙江省政府专项债券（第{number}期）")
                 for number in range(1, 11)]
        second = [item("11", "2026年浙江省政府专项债券（第十一期）")]
        request_json.side_effect = [payload(first, page=1, total=11), payload(second, page=2, total=11)]
        result = bond_search.search_announcements("浙江", 2026, "11期", max_pages=3)
        self.assertEqual(result["metadata"]["pages_fetched"], 2)
        self.assertEqual(request_json.call_args_list[0].args[0]["_tp_lgbInfo"], 1)
        self.assertEqual(request_json.call_args_list[1].args[0]["_tp_lgbInfo"], 2)
        self.assertEqual(result["candidates"][0]["id"], "11")

    @mock.patch.object(bond_search, "_request_json")
    def test_range_matching_and_candidate_sorting(self, request_json):
        request_json.return_value = payload([
            item("older", "2026年浙江省政府专项债券（二十至三十期）信息披露文件", "2026-08-01"),
            item("other-channel", "2026年浙江省政府专项债券（第二十五期）发行结果", "2026-07-20", "xxplwj_fxjg"),
            item("exact", "2026年浙江省政府专项债券（第二十五期）信息披露文件", "2026-07-01"),
            item("wrong-region", "2026年广东省政府专项债券（第二十五期）信息披露文件"),
        ])
        result = bond_search.search_announcements("浙江", 2026, 25, category="发行前披露")
        self.assertEqual([value["id"] for value in result["candidates"]],
                         ["exact", "other-channel", "older"])
        self.assertEqual(result["candidates"][2]["match"]["issue"], "contains")

    @mock.patch.object(bond_search, "_request_json")
    def test_max_pages_truncation(self, request_json):
        request_json.return_value = payload(
            [item(str(n), f"2026年浙江省政府专项债券（第二十五期）") for n in range(10)],
            total=50,
        )
        result = bond_search.search_announcements("浙江", 2026, 25, max_pages=1)
        self.assertTrue(result["metadata"]["truncated"])


class HttpValidationTests(unittest.TestCase):
    def setUp(self):
        bond_search.clear_search_cache()

    def _response(self, value):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps(value).encode("utf-8")
        return response

    @mock.patch.object(bond_search, "urlopen")
    def test_invalid_business_response(self, urlopen):
        urlopen.return_value = self._response({"code": "500", "msg": "pageSize无效"})
        with self.assertRaisesRegex(bond_search.BondSearchError, "pageSize无效"):
            bond_search._request_json({"pageSize": 10}, retries=0)

    @mock.patch.object(bond_search.time, "sleep")
    @mock.patch.object(bond_search, "urlopen")
    def test_429_respects_retry_after(self, urlopen, sleep):
        headers = Message()
        headers["Retry-After"] = "2"
        error = HTTPError("https://example.invalid", 429, "rate", headers, None)
        urlopen.side_effect = [error, self._response(payload([]))]
        result = bond_search._request_json({"pageSize": 10}, retries=1)
        self.assertEqual(result["code"], "200")
        sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
