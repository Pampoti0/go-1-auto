import sys
import unittest
from copy import deepcopy
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
        "aliases": ["product", "san pham", "sản phẩm", "trang san pham", "trang sản phẩm"],
        "patterns": ["product"],
    },
    {
        "id": "url:tutorial",
        "label": "tutorial",
        "scope": "url",
        "type": "url_group",
        "aliases": ["tutorial", "huong dan", "hướng dẫn", "bai huong dan", "bài hướng dẫn"],
        "patterns": ["tutorial"],
    },
    {
        "id": "url:blog",
        "label": "blog",
        "scope": "url",
        "type": "url_group",
        "aliases": ["blog", "bai blog", "bài blog", "content blog"],
        "patterns": ["blog"],
    },
    {
        "id": "url:pricing",
        "label": "pricing",
        "scope": "url",
        "type": "url_group",
        "aliases": ["pricing", "bang gia", "bảng giá", "price page"],
        "patterns": ["pricing"],
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
        "aliases": ["mysql", "vdb mysql", "vdbmysql", "mysql database"],
        "patterns": ["product/vdb-mysql"],
    },
    {
        "id": "url:vdbpostgres",
        "label": "vdb postgres",
        "scope": "url",
        "type": "url",
        "aliases": ["postgres", "postgresql", "vdb postgres", "postgres database"],
        "patterns": ["product/vdb-postgres"],
    },
    {
        "id": "campaign:brand",
        "label": "brand campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["brand", "brand campaign", "greennode", "green node"],
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
    {
        "id": "campaign:search-mysql-q3",
        "label": "Search_MySQL_Q3",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["search my sql q3", "search_mysql_q3", "mysql search q3"],
        "patterns": ["search my sql q3"],
    },
    {
        "id": "campaign:pmax-product-launch",
        "label": "PMAX_Product_Launch",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["pmax product launch", "pmax_product_launch", "product launch"],
        "patterns": ["pmax product launch"],
    },
]


def fake_catalog(urls=None):
    return deepcopy(FAKE_ENTITIES)


class IntentFilterHardeningTest(unittest.TestCase):
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

    def assertNoFilter(self, message, action="seo_query", scope="url", extra=None):
        spec = self.spec(message, action=action, scope=scope, extra=extra)
        self.assertEqual(scope, spec.get("scope"), msg=f"{message!r} => {spec}")
        self.assertEqual([], spec.get("include") or [], msg=f"{message!r} => {spec}")
        self.assertEqual([], spec.get("exclude") or [], msg=f"{message!r} => {spec}")
        self.assertFalse(server._filter_active(spec), msg=f"{message!r} => {spec}")
        return spec

    def assertFilter(
        self,
        message,
        include,
        *,
        exclude=None,
        action="seo_query",
        scope="url",
        match_mode="any",
        extra=None,
    ):
        spec = self.spec(message, action=action, scope=scope, extra=extra)
        self.assertEqual(scope, spec.get("scope"), msg=f"{message!r} => {spec}")
        self.assertEqual(list(include), spec.get("include") or [], msg=f"{message!r} => {spec}")
        self.assertEqual(list(exclude or []), spec.get("exclude") or [], msg=f"{message!r} => {spec}")
        self.assertEqual(match_mode, spec.get("match_mode"), msg=f"{message!r} => {spec}")
        self.assertTrue(server._filter_active(spec), msg=f"{message!r} => {spec}")
        return spec

    def assertOverride(self, message, expected_action, *, current_action="reply"):
        override = server._hard_intent_override(message, current_action)
        self.assertIsNotNone(override, msg=message)
        self.assertEqual(expected_action, override.get("action"), msg=f"{message!r} => {override}")
        return override

    def test_general_metric_language_does_not_become_url_or_campaign_filter(self):
        cases = [
            ("Phân tích số liệu SEO tháng gần nhất", "seo_query", "url"),
            ("xem số liệu traffic toàn site", "seo_query", "url"),
            ("traffic organic tháng này ra sao", "seo_query", "url"),
            ("clicks impressions đang tăng hay giảm", "seo_query", "url"),
            ("hiệu suất SEO 6 tháng gần nhất", "seo_query", "url"),
            ("báo cáo KPI SEO", "seo_query", "url"),
            ("GA4 có users giảm không", "seo_query", "url"),
            ("GSC impressions tháng vừa rồi", "seo_query", "url"),
            ("SEO ổn không", "seo_query", "url"),
            ("SEO tổng quan toàn site", "seo_query", "url"),
            ("phân tích kết quả PageSpeed lần chạy gần nhất", "query_results", "url"),
            ("PSI score trung bình bao nhiêu", "query_results", "url"),
            ("Core Web Vitals đang thế nào", "query_results", "url"),
            ("trang nào chậm nhất", "query_results", "url"),
            ("trang nào nhanh nhất", "query_results", "url"),
            ("LCP tệ nhất ở đâu", "query_results", "url"),
            ("CLS có vấn đề không", "query_results", "url"),
            ("PageSpeed tháng này có cải thiện không", "query_results", "url"),
            ("conversion tracking ổn không", "tracking_audit", "url"),
            ("tracking conversion có lỗi không", "tracking_audit", "url"),
            ("GA4 event có thiếu không", "tracking_audit", "url"),
            ("GTM firing đúng không", "tracking_audit", "url"),
            ("conversion action nào lỗi", "tracking_audit", "url"),
            ("audit tracking 30 ngày", "tracking_audit", "url"),
            ("Ads hiệu quả không", "ads_perf", "campaign"),
            ("Google Ads performance 7 ngày", "ads_perf", "campaign"),
            ("CPA đang cao không", "ads_perf", "campaign"),
            ("CTR ads có giảm không", "ads_perf", "campaign"),
            ("ngân sách quảng cáo có vượt không", "ads_perf", "campaign"),
            ("campaign nào đang chạy", "ads_list", "campaign"),
            ("danh sách campaign đang active", "ads_list", "campaign"),
            ("landing page nào convert tốt", "ldp_perf", "url"),
            ("trang đích nào có CPA cao", "ldp_perf", "url"),
            ("SEO và Ads tổng quan tháng này", "seo_query", "url"),
            ("GA4 GSC Ads tổng hợp", "seo_query", "url"),
            ("PSI và SEO có liên quan không", "query_results", "url"),
            ("traffic paid vs organic thế nào", "seo_query", "url"),
            ("số liệu conversion toàn account", "tracking_audit", "url"),
            ("báo cáo alerts hôm nay", "alerts", "url"),
            ("cảnh báo bất thường gần nhất", "alerts", "url"),
            ("priority fix toàn site", "priority_fix", "url"),
            ("nên tối ưu trang nào trước", "priority_fix", "url"),
            ("action plan tuần này", "action_plan", "url"),
            ("root cause traffic giảm", "diagnose_drop", "url"),
            ("vì sao conversion tụt", "diagnose_drop", "url"),
            ("experiment nào đang chạy", "experiment_plan", "url"),
            ("kế hoạch đo sau khi fix", "experiment_plan", "url"),
        ]
        for message, action, scope in cases:
            with self.subTest(message=message):
                self.assertNoFilter(message, action=action, scope=scope)

    def test_bad_llm_metric_terms_are_sanitized_to_no_filter(self):
        cases = [
            ("Phân tích số liệu SEO tháng gần nhất", ["so", "lieu", "seo", "thang"], "seo_query", "url"),
            ("xem số liệu traffic toàn site", ["traffic", "toan", "site"], "seo_query", "url"),
            ("traffic organic tháng này ra sao", ["traffic", "organic", "thang"], "seo_query", "url"),
            ("clicks impressions đang tăng hay giảm", ["clicks", "impressions", "dang"], "seo_query", "url"),
            ("hiệu suất SEO 6 tháng gần nhất", ["hieu", "suat", "seo"], "seo_query", "url"),
            ("GA4 có users giảm không", ["ga4", "users", "giam"], "seo_query", "url"),
            ("phân tích kết quả PageSpeed lần chạy gần nhất", ["pagespeed", "ket", "qua"], "query_results", "url"),
            ("PSI score trung bình bao nhiêu", ["psi", "score", "trung"], "query_results", "url"),
            ("Core Web Vitals đang thế nào", ["core", "web", "vitals"], "query_results", "url"),
            ("trang nào chậm nhất", ["trang", "cham", "nhat"], "query_results", "url"),
            ("trang nào nhanh nhất", ["trang", "nhanh", "nhat"], "query_results", "url"),
            ("LCP tệ nhất ở đâu", ["lcp", "te", "nhat"], "query_results", "url"),
            ("CLS có vấn đề không", ["cls", "van", "de"], "query_results", "url"),
            ("PageSpeed tháng này có cải thiện không", ["pagespeed", "cai", "thien"], "query_results", "url"),
            ("conversion tracking ổn không", ["conversion", "tracking"], "tracking_audit", "url"),
            ("tracking conversion có lỗi không", ["tracking", "conversion", "loi"], "tracking_audit", "url"),
            ("GA4 event có thiếu không", ["ga4", "event", "thieu"], "tracking_audit", "url"),
            ("GTM firing đúng không", ["gtm", "firing", "dung"], "tracking_audit", "url"),
            ("audit tracking 30 ngày", ["audit", "tracking", "ngay"], "tracking_audit", "url"),
            ("Ads hiệu quả không", ["ads", "hieu-qua"], "ads_perf", "campaign"),
            ("Google Ads performance 7 ngày", ["google", "ads", "performance"], "ads_perf", "campaign"),
            ("CPA đang cao không", ["cpa", "dang", "cao"], "ads_perf", "campaign"),
            ("CTR ads có giảm không", ["ctr", "ads", "giam"], "ads_perf", "campaign"),
            ("ngân sách quảng cáo có vượt không", ["ngan", "sach", "quang"], "ads_perf", "campaign"),
            ("campaign nào đang chạy", ["campaign", "nao", "dang", "chay"], "ads_list", "campaign"),
            ("danh sách campaign đang active", ["danh", "sach", "campaign", "dang"], "ads_list", "campaign"),
            ("landing page nào convert tốt", ["landing", "page", "convert"], "ldp_perf", "url"),
            ("trang đích nào có CPA cao", ["trang", "dich", "cpa", "cao"], "ldp_perf", "url"),
            ("báo cáo alerts hôm nay", ["bao", "cao", "alerts"], "alerts", "url"),
            ("priority fix toàn site", ["priority", "fix", "site"], "priority_fix", "url"),
        ]
        for message, bad_terms, action, scope in cases:
            with self.subTest(message=message):
                raw = {"scope": scope, "include": bad_terms, "exclude": list(reversed(bad_terms))}
                self.assertNoFilter(message, action=action, scope=scope, extra={"filter_spec": raw})

    def test_natural_url_entities_match_without_explicit_filter_cue(self):
        cases = [
            ("phân tích SEO product", ["product"]),
            ("product tháng này sao", ["product"]),
            ("vì sao product không convert", ["product"]),
            ("audit tracking product 7 ngày", ["product"]),
            ("làm experiment cho product", ["product"]),
            ("priority fix product", ["product"]),
            ("sản phẩm có traffic giảm không", ["product"]),
            ("trang sản phẩm chậm không", ["product"]),
            ("tutorial tháng này thế nào", ["tutorial"]),
            ("bài hướng dẫn có traffic không", ["tutorial"]),
            ("hướng dẫn có conversion thấp không", ["tutorial"]),
            ("pagespeed tutorial", ["tutorial"]),
            ("blog tháng này sao", ["blog"]),
            ("bài blog organic giảm không", ["blog"]),
            ("content blog có CTR thấp không", ["blog"]),
            ("pricing convert thế nào", ["pricing"]),
            ("bảng giá có CTR thấp không", ["pricing"]),
            ("price page có CPA cao không", ["pricing"]),
            ("vdb ổn không", ["product/vdb-"]),
            ("database có traffic tốt không", ["product/vdb-"]),
            ("virtual database đang tụt", ["product/vdb-"]),
            ("mysql đang tụt conversion", ["product/vdb-mysql"]),
            ("vdb mysql lcp cao không", ["product/vdb-mysql"]),
            ("mysql database traffic thế nào", ["product/vdb-mysql"]),
            ("postgresql page có chậm không", ["product/vdb-postgres"]),
            ("vdb postgres có impression giảm không", ["product/vdb-postgres"]),
            ("product và tutorial traffic thế nào", ["product", "tutorial"]),
            ("product hoặc blog có giảm không", ["product", "blog"]),
            ("tutorial blog pagespeed", ["tutorial", "blog"]),
            ("pricing và tutorial conversion ra sao", ["tutorial", "pricing"]),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertFilter(message, expected)

    def test_natural_entity_excludes_and_match_modes(self):
        cases = [
            ("product trừ blog", ["product"], ["blog"], "any"),
            ("tutorial ngoại trừ blog", ["tutorial"], ["blog"], "any"),
            ("vdb trừ mysql", ["product/vdb-"], ["product/vdb-mysql"], "any"),
            ("database trừ postgres", ["product/vdb-"], ["product/vdb-postgres"], "any"),
            ("product phải chứa cả tutorial", ["product", "tutorial"], [], "all"),
            ("url phải chứa cả product và tutorial", ["product", "tutorial"], [], "all"),
            ("trang chứa cả vdb mysql", ["product/vdb-mysql"], [], "all"),
            ("product và tutorial trừ blog", ["product", "tutorial"], ["blog"], "any"),
        ]
        for message, include, exclude, match_mode in cases:
            with self.subTest(message=message):
                self.assertFilter(message, include, exclude=exclude, match_mode=match_mode)

    def test_campaign_entities_and_campaign_names_match(self):
        cases = [
            ("phân tích campaign brand", ["greennode"]),
            ("ads brand campaign hiệu quả không", ["greennode"]),
            ("campaign greennode tháng này", ["greennode"]),
            ("brand trong ads có CPA cao không", ["greennode"]),
            ("campaign competitor ổn không", ["fpt", "viettel"]),
            ("đối thủ trong ads ra sao", ["fpt", "viettel"]),
            ("campaign FPT có CPA cao không", ["fpt", "viettel"]),
            ("campaign Viettel tháng này", ["fpt", "viettel"]),
            ("brand và competitor performance", ["greennode", "fpt", "viettel"]),
            ("campaign cloud server", ["cloud-server", "cloudserver"]),
            ("ads cloudserver performance", ["cloud-server", "cloudserver"]),
            ("cloud server trong ads có CTR thấp không", ["cloud-server", "cloudserver"]),
            ("campaign Search_MySQL_Q3", ["search-my-sql-q3"]),
            ("Search_MySQL_Q3 CPA cao không", ["search-my-sql-q3"]),
            ("campaign mysql search q3", ["search-my-sql-q3"]),
            ("campaign PMAX_Product_Launch", ["pmax-product-launch"]),
            ("product launch ads có hiệu quả không", ["pmax-product-launch"]),
            ("pmax_product_launch có conversion không", ["pmax-product-launch"]),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertFilter(message, expected, action="ads_perf", scope="campaign")

    def test_explicit_free_keywords_require_clear_filter_cue(self):
        no_cue_cases = [
            "custom-slug SEO ra sao",
            "black-friday traffic giảm",
            "summer-sale performance thế nào",
            "enterprise page có CPA cao không",
            "promo campaign có hiệu quả không",
            "signup funnel conversion thế nào",
            "checkout traffic giảm",
            "spring-launch CTR ra sao",
            "referral-source users tháng này",
            "utm-x có vấn đề gì không",
        ]
        for message in no_cue_cases:
            with self.subTest(message=message, cue="none"):
                scope = "campaign" if "campaign" in message else "url"
                action = "ads_perf" if scope == "campaign" else "seo_query"
                self.assertNoFilter(message, action=action, scope=scope)

        explicit_cases = [
            ("url chứa custom-slug", "url", ["custom-slug"], [], "any"),
            ("url custom-slug", "url", ["custom-slug"], [], "any"),
            ("lọc URL custom-slug", "url", ["custom-slug"], [], "any"),
            ("lọc trang custom-slug", "url", ["custom-slug"], [], "any"),
            ("url chứa promo và landing-x", "url", ["promo", "landing-x"], [], "any"),
            ("url phải chứa cả feature-x và signup", "url", ["feature-x", "signup"], [], "all"),
            ("lọc URL custom-slug trừ staging", "url", ["custom-slug"], ["staging"], "any"),
            ("url chứa promo trừ archive", "url", ["promo"], ["archive"], "any"),
            ("trừ URL staging", "url", [], ["staging"], "any"),
            ("campaign chứa exact-name", "campaign", ["exact-name"], [], "any"),
            ("campaign exact-name", "campaign", ["exact-name"], [], "any"),
            ("lọc campaign summer-sale", "campaign", ["summer-sale"], [], "any"),
            ("lọc campaign summer-sale trừ draft", "campaign", ["summer-sale"], ["draft"], "any"),
            ("campaign phải chứa cả brand-x và prospecting", "campaign", ["brand-x", "prospecting"], [], "all"),
            ("campaign chứa Search_MySQL_Q3", "campaign", ["search-my-sql-q3"], [], "any"),
            ("campaign chứa cloud server", "campaign", ["cloud-server", "cloudserver"], [], "any"),
        ]
        for message, scope, include, exclude, match_mode in explicit_cases:
            with self.subTest(message=message, cue="explicit"):
                action = "ads_perf" if scope == "campaign" else "seo_query"
                self.assertFilter(message, include, exclude=exclude, action=action, scope=scope, match_mode=match_mode)

    def test_llm_filter_specs_keep_only_known_or_explicitly_requested_terms(self):
        cases = [
            ("phân tích SEO", "url", ["seo", "data", "latest"], [], []),
            ("phân tích số liệu", "url", ["so", "lieu", "overview"], [], []),
            ("phân tích product", "url", ["product", "phan", "tich"], ["product"], []),
            ("product có traffic không", "url", ["traffic", "product"], ["product"], []),
            ("tutorial tháng này", "url", ["tutorial", "thang"], ["tutorial"], []),
            ("vdb mysql đang tụt", "url", ["mysql", "vdb", "giam"], ["product/vdb-mysql"], []),
            ("url chứa custom-slug", "url", ["custom-slug", "seo"], ["custom-slug"], []),
            ("lọc URL custom-slug", "url", ["custom-slug", "latest"], ["custom-slug"], []),
            ("url custom-slug trừ staging", "url", ["custom-slug"], ["custom-slug"], ["staging"]),
            ("campaign nào đang chạy", "campaign", ["campaign", "dang", "chay"], [], []),
            ("campaign brand", "campaign", ["brand", "campaign"], ["greennode"], []),
            ("campaign competitor", "campaign", ["competitor", "ads"], ["fpt", "viettel"], []),
            ("campaign cloud server", "campaign", ["cloud server", "ads"], ["cloud-server", "cloudserver"], []),
            ("campaign chứa exact-name", "campaign", ["exact-name", "ads"], ["exact-name"], []),
            ("lọc campaign summer-sale", "campaign", ["summer-sale", "ads"], ["summer-sale"], []),
            ("lọc campaign summer-sale trừ draft", "campaign", ["summer-sale"], ["summer-sale"], ["draft"]),
            ("trang nào chậm nhất", "url", ["trang", "cham", "slow"], [], []),
            ("tracking conversion ổn không", "url", ["tracking", "conversion", "event"], [], []),
        ]
        for message, scope, raw_include, expected_include, expected_exclude in cases:
            with self.subTest(message=message):
                raw = {"scope": scope, "include": raw_include, "exclude": expected_exclude}
                if expected_include or expected_exclude:
                    self.assertFilter(
                        message,
                        expected_include,
                        exclude=expected_exclude,
                        action="ads_perf" if scope == "campaign" else "seo_query",
                        scope=scope,
                        extra={"filter_spec": raw},
                    )
                else:
                    self.assertNoFilter(
                        message,
                        action="ads_perf" if scope == "campaign" else "seo_query",
                        scope=scope,
                        extra={"filter_spec": raw},
                    )

    def test_intent_overrides_do_not_introduce_meaningless_filters(self):
        cases = [
            ("Phân tích số liệu SEO tháng gần nhất", "seo_query", "url", []),
            ("SEO ổn không", "seo_query", "url", []),
            ("traffic tháng này ra sao", "seo_query", "url", []),
            ("phân tích kết quả PageSpeed lần chạy gần nhất", "query_results", "url", []),
            ("trang nào chậm nhất", "query_results", "url", []),
            ("PSI score trung bình", "query_results", "url", []),
            ("tracking conversion ổn không", "tracking_audit", "url", []),
            ("audit tracking 30 ngày", "tracking_audit", "url", []),
            ("làm experiment cho product", "experiment_plan", "url", ["product"]),
            ("làm experiment cho tracking", "experiment_plan", "url", []),
            ("vì sao product không convert", "diagnose_drop", "url", ["product"]),
            ("vì sao traffic giảm", "diagnose_drop", "url", []),
            ("tăng tốc vdb mysql", "fix_suggest", "url", ["product/vdb-mysql"]),
            ("tăng tốc trang chậm nhất", "fix_suggest", "url", []),
            ("alerts hôm nay", "alerts", "url", []),
            ("cảnh báo product", "alerts", "url", ["product"]),
            ("trang nào nên tối ưu trước", "priority_fix", "url", []),
            ("nên tối ưu product trước", "priority_fix", "url", ["product"]),
            ("kế hoạch tuần này", "action_plan", "url", []),
            ("kế hoạch cho tutorial", "action_plan", "url", ["tutorial"]),
            ("ads hiệu quả không", "ads_perf", "campaign", []),
            ("campaign nào đang chạy", "ads_list", "campaign", []),
            ("Google Ads performance 7 ngày", "ads_perf", "campaign", []),
        ]
        for message, expected_action, scope, expected_include in cases:
            with self.subTest(message=message):
                override = self.assertOverride(message, expected_action)
                if expected_include:
                    self.assertFilter(message, expected_include, action=override["action"], scope=scope)
                else:
                    self.assertNoFilter(message, action=override["action"], scope=scope)


if __name__ == "__main__":
    unittest.main()
