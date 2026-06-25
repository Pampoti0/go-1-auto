import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import entity_resolver
import server


FAKE_ENTITIES = [
    {
        "id": "url:product",
        "label": "product",
        "scope": "url",
        "type": "url_group",
        "aliases": ["product", "san pham", "trang san pham"],
        "patterns": ["product"],
    },
    {
        "id": "url:tutorial",
        "label": "tutorial",
        "scope": "url",
        "type": "url_group",
        "aliases": ["tutorial", "huong dan", "bai huong dan"],
        "patterns": ["tutorial"],
    },
    {
        "id": "url:blog",
        "label": "blog",
        "scope": "url",
        "type": "url_group",
        "aliases": ["blog"],
        "patterns": ["blog"],
    },
    {
        "id": "url:vdb",
        "label": "vdb",
        "scope": "url",
        "type": "product_family",
        "aliases": ["vdb", "database", "virtual database"],
        "patterns": ["product/vdb-"],
    },
    {
        "id": "url:vdbmysql",
        "label": "vdb mysql",
        "scope": "url",
        "type": "url",
        "aliases": ["mysql", "vdb mysql", "vdbmysql"],
        "patterns": ["product/vdb-mysql"],
    },
    {
        "id": "campaign:brand",
        "label": "brand campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["brand", "brand campaign", "greennode"],
        "patterns": ["greennode"],
    },
    {
        "id": "campaign:competitor",
        "label": "competitor campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["competitor", "doi thu", "đối thủ", "fpt", "viettel"],
        "patterns": ["fpt", "viettel"],
    },
    {
        "id": "campaign:cloudserver",
        "label": "cloud server",
        "scope": "campaign",
        "type": "campaign_topic",
        "aliases": ["cloud server", "cloudserver"],
        "patterns": ["cloud server", "cloudserver"],
    },
]


def fake_catalog(urls=None):
    return [dict(e) for e in FAKE_ENTITIES]


class IntentFilterMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_catalog = entity_resolver.catalog
        entity_resolver.catalog = fake_catalog

    @classmethod
    def tearDownClass(cls):
        entity_resolver.catalog = cls._orig_catalog

    def spec(self, message, action="seo_query", scope="url", extra=None):
        data = {"action": action}
        if extra:
            data.update(extra)
        return server._filter_spec_from_action(data, message, scope)

    def assertIncludes(self, message, expected, action="seo_query", scope="url", extra=None):
        spec = self.spec(message, action, scope, extra)
        self.assertEqual(expected, spec.get("include"), msg=f"{message} => {spec}")

    def assertExcludes(self, message, expected, action="seo_query", scope="url", extra=None):
        spec = self.spec(message, action, scope, extra)
        self.assertEqual(expected, spec.get("exclude"), msg=f"{message} => {spec}")

    def test_no_filter_for_metric_language(self):
        cases = [
            "Phân tích số liệu SEO tháng gần nhất",
            "phân tích số liệu SEO",
            "xem dữ liệu SEO tháng này",
            "traffic tháng này sao",
            "SEO ổn không",
            "tình hình SEO thế nào",
            "báo cáo SEO tháng vừa rồi",
            "clicks/impressions tháng gần nhất",
            "views users tháng này",
            "phân tích hiệu suất SEO",
            "xem KPI SEO",
            "báo cáo traffic toàn site",
            "phân tích số liệu GA4 và GSC",
            "SEO chưa ổn hả",
            "số liệu traffic có giảm không",
            "phân tích kết quả PageSpeed lần chạy gần nhất",
            "trang nào chậm nhất",
            "trang nào tốt nhất",
            "pagespeed trang nào tệ",
            "tracking conversion ổn không",
            "campaign nào đang chạy",
            "ads hiệu quả không",
            "phân tích hiệu suất Google Ads 30 ngày",
            "landing page nào convert tốt",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                action = "ads_perf" if "ads" in msg.lower() or "campaign" in msg.lower() else "seo_query"
                scope = "campaign" if "campaign" in msg.lower() else "url"
                self.assertIncludes(msg, [], action=action, scope=scope)

    def test_entity_filter_without_explicit_cue(self):
        cases = [
            ("phân tích SEO product", ["product"]),
            ("phân tích SEO product và tutorial", ["product", "tutorial"]),
            ("product ổn không", ["product"]),
            ("tutorial tháng này sao", ["tutorial"]),
            ("vdb mysql ổn không", ["product/vdb-mysql"]),
            ("vì sao product không convert", ["product"]),
            ("làm experiment cho product", ["product"]),
            ("audit tracking product 7 ngày", ["product"]),
            ("báo cáo pagespeed product", ["product"]),
            ("tăng tốc vdb mysql", ["product/vdb-mysql"]),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                self.assertIncludes(msg, expected)

    def test_explicit_filter_allows_free_keywords(self):
        cases = [
            ("url cloud-server", ["cloud-server"]),
            ("url chứa cloud-server", ["cloud-server"]),
            ("báo cáo pagespeed url tutorial và product", ["product", "tutorial"]),
            ("trang /product/vdb-mysql", ["product/vdb-mysql"]),
            ("lọc URL custom-slug", ["custom-slug"]),
            ("campaign GG_Search_CloudServer", ["cloud-server", "cloudserver"]),
            ("campaign cloud server", ["cloud-server", "cloudserver"]),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                scope = "campaign" if "campaign" in msg.lower() else "url"
                action = "ads_perf" if scope == "campaign" else "query_results"
                self.assertIncludes(msg, expected, action=action, scope=scope)

    def test_exclude_and_match_mode(self):
        spec = self.spec("product trừ blog", "seo_query", "url")
        self.assertEqual(["product"], spec["include"])
        self.assertEqual(["blog"], spec["exclude"])
        self.assertEqual("any", spec["match_mode"])

        spec = self.spec("url phải chứa cả product và tutorial", "seo_query", "url")
        self.assertEqual(["product", "tutorial"], spec["include"])
        self.assertEqual("all", spec["match_mode"])

    def test_campaign_entities(self):
        cases = [
            ("phân tích campaign brand", ["greennode"]),
            ("campaign competitor ổn không", ["fpt", "viettel"]),
            ("campaign brand và competitor", ["greennode", "fpt", "viettel"]),
            ("ads campaign cloud server", ["cloud-server", "cloudserver"]),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                self.assertIncludes(msg, expected, action="ads_perf", scope="campaign")

    def test_bad_llm_filter_spec_is_sanitized(self):
        bad_specs = [
            ("Phân tích số liệu SEO tháng gần nhất", ["so", "lieu"]),
            ("phân tích dữ liệu SEO", ["du", "lieu"]),
            ("trang nào chậm nhất", ["cham"]),
            ("campaign nào đang chạy", ["dang"]),
            ("tracking conversion ổn không", ["tracking", "conversion"]),
        ]
        for msg, bad_terms in bad_specs:
            with self.subTest(msg=msg):
                scope = "campaign" if "campaign" in msg.lower() else "url"
                spec = self.spec(
                    msg,
                    action="seo_query",
                    scope=scope,
                    extra={"filter_spec": {"scope": scope, "include": bad_terms}},
                )
                self.assertEqual([], spec["include"], msg=f"{msg} => {spec}")

    def test_intent_overrides_common_queries(self):
        cases = [
            ("Phân tích số liệu SEO tháng gần nhất", "seo_query"),
            ("SEO ổn không", "seo_query"),
            ("phân tích kết quả PageSpeed lần chạy gần nhất", "query_results"),
            ("vì sao product không convert", "diagnose_drop"),
            ("làm experiment cho product", "experiment_plan"),
            ("tracking conversion ổn không", "tracking_audit"),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                override = server._hard_intent_override(msg, "reply")
                self.assertIsNotNone(override)
                self.assertEqual(expected, override["action"])


if __name__ == "__main__":
    unittest.main()
