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
        "id": "url:solutions-cloud-gpu",
        "label": "solutions cloud gpu",
        "scope": "url",
        "type": "url",
        "aliases": [
            "cloud gpu",
            "cloud-gpu",
            "gpu cloud",
            "gpu-cloud",
            "cloud gpu solution",
            "solutions cloud gpu",
            "solutions/cloud-gpu",
        ],
        "patterns": ["solutions/cloud-gpu"],
    },
    {
        "id": "url:case-study-fpt",
        "label": "case study fpt",
        "scope": "url",
        "type": "url",
        "aliases": [
            "case study",
            "case-study",
            "case study fpt",
            "case-study/fpt",
            "fpt case study",
        ],
        "patterns": ["case-study/fpt"],
    },
    {
        "id": "url:pricing-gpu",
        "label": "gpu pricing",
        "scope": "url",
        "type": "url",
        "aliases": [
            "gpu pricing",
            "pricing gpu",
            "gpu price",
            "bang gia gpu",
            "bảng giá gpu",
            "pricing/gpu",
        ],
        "patterns": ["pricing/gpu"],
    },
    {
        "id": "url:blog-gpu-cloud-cost",
        "label": "gpu cloud cost",
        "scope": "url",
        "type": "url",
        "aliases": [
            "gpu cloud cost",
            "gpu-cloud-cost",
            "cloud gpu cost",
            "blog gpu cloud cost",
            "chi phi gpu cloud",
            "chi phí gpu cloud",
            "blog/gpu-cloud-cost",
        ],
        "patterns": ["blog/gpu-cloud-cost"],
    },
    {
        "id": "campaign:brand",
        "label": "brand campaign",
        "scope": "campaign",
        "type": "campaign_group",
        "aliases": ["brand", "brand campaign", "greennode", "green node", "vng cloud"],
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
        "id": "campaign:gg-search-gpu-cloud-q3",
        "label": "GG_Search_GPU_Cloud_Q3",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "gg search gpu cloud q3",
            "gg_search_gpu_cloud_q3",
            "search gpu cloud q3",
            "gpu cloud q3",
            "gpu cloud campaign",
        ],
        "patterns": ["GG_Search_GPU_Cloud_Q3"],
    },
    {
        "id": "campaign:brand-awareness-vng",
        "label": "Brand_Awareness_VNG",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "brand awareness vng",
            "brand_awareness_vng",
            "vng brand awareness",
            "awareness vng",
        ],
        "patterns": ["Brand_Awareness_VNG"],
    },
    {
        "id": "campaign:competitor-fpt-gpu",
        "label": "Competitor_FPT_GPU",
        "scope": "campaign",
        "type": "campaign",
        "aliases": [
            "competitor fpt gpu",
            "competitor_fpt_gpu",
            "fpt gpu competitor",
            "fpt gpu conquest",
        ],
        "patterns": ["Competitor_FPT_GPU"],
    },
    {
        "id": "campaign:pmax-gpu-trial",
        "label": "PMAX_GPU_Trial",
        "scope": "campaign",
        "type": "campaign",
        "aliases": ["pmax gpu trial", "pmax_gpu_trial", "gpu trial", "trial gpu"],
        "patterns": ["PMAX_GPU_Trial"],
    },
]


def fake_catalog(urls=None):
    return deepcopy(FAKE_ENTITIES)


class IntentFilterHardeningRound2Test(unittest.TestCase):
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

    def assertNoFilter(self, message, *, action="seo_query", scope="url", extra=None):
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

    def test_substring_traps_and_general_metric_language_stay_unfiltered(self):
        cases = [
            ("cấu trúc site có ảnh hưởng SEO không", "seo_query", "url"),
            ("cấu trúc URL có thay đổi gì tuần này không", "seo_query", "url"),
            ("structure report cho toàn site", "seo_query", "url"),
            ("site structure đang ảnh hưởng crawl không", "seo_query", "url"),
            ("trung bình PSI mobile là bao nhiêu", "query_results", "url"),
            ("điểm trung bình Core Web Vitals", "query_results", "url"),
            ("run rate ads tuần này thế nào", "ads_perf", "campaign"),
            ("runbook tracking conversion có thiếu gì không", "tracking_audit", "url"),
            ("ý tưởng trừu tượng về SEO thôi", "seo_query", "url"),
            ("báo cáo trừu tượng không phải lọc trừ", "seo_query", "url"),
            ("campaign đang active có bao nhiêu", "ads_list", "campaign"),
            ("danh sách campaign active và paused", "ads_list", "campaign"),
            ("campaign running status", "ads_list", "campaign"),
            ("urlify tracking parameter có gây lỗi không", "tracking_audit", "url"),
            ("curl command trong log có lỗi không", "tracking_audit", "url"),
            ("hurling không liên quan tới link", "seo_query", "url"),
            ("brand-new launch performance thế nào", "ads_perf", "campaign"),
            ("fpt-cloud benchmark có cần theo dõi không", "ads_perf", "campaign"),
            ("productivity của team SEO ra sao", "seo_query", "url"),
            ("blogger outreach tháng này thế nào", "seo_query", "url"),
            ("pricingly named test có vấn đề không", "seo_query", "url"),
            ("mySQLish keyword không phải mysql page", "seo_query", "url"),
            ("postgresqlite không phải postgres page", "seo_query", "url"),
            ("cloud-gpuish token không phải cloud gpu entity", "seo_query", "url"),
            ("case-studyish phrase không phải case study thật", "seo_query", "url"),
            ("gpu-pricingish copy test", "seo_query", "url"),
            ("chi phí cloudgpu tổng quan", "seo_query", "url"),
            ("brandish creative có hiệu quả không", "ads_perf", "campaign"),
            ("competitorship topic chung chung", "ads_perf", "campaign"),
            ("FPTCloud written liền không phải FPT campaign", "ads_perf", "campaign"),
            ("ViettelCloud written liền không phải Viettel campaign", "ads_perf", "campaign"),
            ("campaigning ideas cho Q3", "ads_perf", "campaign"),
            ("ad campaigner workload", "ads_perf", "campaign"),
            ("landingness score có nghĩa gì", "seo_query", "url"),
            ("pathway traffic overview", "seo_query", "url"),
            ("pageview tổng quan không phải page filter", "seo_query", "url"),
        ]
        for message, action, scope in cases:
            with self.subTest(message=message):
                self.assertNoFilter(message, action=action, scope=scope)

    def test_explicit_free_keywords_only_when_filter_cue_is_clear(self):
        no_cue_cases = [
            ("custom-slug SEO ra sao", "seo_query", "url"),
            ("enterprise-landing traffic giảm", "seo_query", "url"),
            ("black_friday có conversion không", "seo_query", "url"),
            ("spring/sale performance thế nào", "seo_query", "url"),
            ("promo campaign có hiệu quả không", "ads_perf", "campaign"),
            ("exact-name CPA cao không", "ads_perf", "campaign"),
            ("summer-sale ads ổn không", "ads_perf", "campaign"),
            ("brand-x awareness tuần này", "ads_perf", "campaign"),
            ("fpt-cloud conquest idea", "ads_perf", "campaign"),
            ("landing-x users tháng này", "seo_query", "url"),
            ("signup_flow tracking ổn không", "tracking_audit", "url"),
            ("checkout/dropoff có tăng không", "seo_query", "url"),
            ("urlish token trong copy", "seo_query", "url"),
            ("preurl suffix không phải cue", "seo_query", "url"),
            ("campaignish word không phải cue", "ads_perf", "campaign"),
            ("pathfinder page title", "seo_query", "url"),
        ]
        for message, action, scope in no_cue_cases:
            with self.subTest(message=message, cue="none"):
                self.assertNoFilter(message, action=action, scope=scope)

        explicit_cases = [
            ("url chứa custom-slug", "url", ["custom-slug"], [], "any"),
            ("lọc URL custom-slug", "url", ["custom-slug"], [], "any"),
            ("lọc url custom_slug", "url", ["custom_slug"], [], "any"),
            ("URL custom/path", "url", ["custom/path"], [], "any"),
            ("path /custom/path", "url", ["custom/path"], [], "any"),
            ("lọc URL 'enterprise-landing'", "url", ["enterprise-landing"], [], "any"),
            ('url chứa "black_friday"', "url", ["black_friday"], [], "any"),
            ("url chứa promo và landing-x", "url", ["promo", "landing-x"], [], "any"),
            ("url phải chứa cả feature-x và signup", "url", ["feature-x", "signup"], [], "all"),
            ("https://decho.vn/solutions/cloud-gpu", "url", ["solutions/cloud-gpu"], [], "any"),
            ("so sánh /pricing/gpu với /blog/gpu-cloud-cost", "url", ["pricing/gpu", "blog/gpu-cloud-cost"], [], "any"),
            ("lọc URL custom-slug trừ staging", "url", ["custom-slug"], ["staging"], "any"),
            ("url chứa promo ngoại trừ archive", "url", ["promo"], ["archive"], "any"),
            ("trừ URL staging", "url", [], ["staging"], "any"),
            ("campaign chứa exact-name", "campaign", ["exact-name"], [], "any"),
            ("lọc campaign summer-sale", "campaign", ["summer-sale"], [], "any"),
            ("campaign custom_name", "campaign", ["custom_name"], [], "any"),
            ("campaign brand-x", "campaign", ["brand-x"], [], "any"),
            ("campaign fpt-cloud", "campaign", ["fpt-cloud"], [], "any"),
            ("campaign phải chứa cả brand-x và prospecting", "campaign", ["brand-x", "prospecting"], [], "all"),
            ("lọc campaign summer-sale trừ draft", "campaign", ["summer-sale"], ["draft"], "any"),
            ("campaign chứa GG_Search_GPU_Cloud_Q3", "campaign", ["gg-search-gpu-cloud-q3"], [], "any"),
        ]
        for message, scope, include, exclude, match_mode in explicit_cases:
            with self.subTest(message=message, cue="explicit"):
                action = "ads_perf" if scope == "campaign" else "seo_query"
                self.assertFilter(message, include, exclude=exclude, action=action, scope=scope, match_mode=match_mode)

    def test_natural_url_entities_generalize_to_new_catalog_items(self):
        cases = [
            ("phân tích SEO product", ["product"]),
            ("sản phẩm có traffic giảm không", ["product"]),
            ("product, tutorial traffic thế nào", ["product", "tutorial"]),
            ("tutorial tháng này thế nào", ["tutorial"]),
            ("bài hướng dẫn có conversion thấp không", ["tutorial"]),
            ("blog organic có giảm không", ["blog"]),
            ("content blog CTR thấp không", ["blog"]),
            ("pricing convert thế nào", ["pricing"]),
            ("bảng giá có CPA cao không", ["pricing"]),
            ("price page traffic ra sao", ["pricing"]),
            ("vdb ổn không", ["product/vdb-"]),
            ("database có impressions tốt không", ["product/vdb-"]),
            ("virtual database đang tụt", ["product/vdb-"]),
            ("mysql đang tụt conversion", ["product/vdb-mysql"]),
            ("vdb mysql LCP cao không", ["product/vdb-mysql"]),
            ("mysql database traffic thế nào", ["product/vdb-mysql"]),
            ("postgres đang chậm không", ["product/vdb-postgres"]),
            ("postgresql page có impression giảm không", ["product/vdb-postgres"]),
            ("vdb postgres có conversion không", ["product/vdb-postgres"]),
            ("cloud gpu traffic thế nào", ["solutions/cloud-gpu"]),
            ("cloud-gpu có page speed thấp không", ["solutions/cloud-gpu"]),
            ("gpu cloud solution convert ra sao", ["solutions/cloud-gpu"]),
            ("solutions cloud gpu có bounce cao không", ["solutions/cloud-gpu"]),
            ("case study có traffic không", ["case-study/fpt"]),
            ("case-study FPT có conversion không", ["case-study/fpt"]),
            ("FPT case study impressions giảm không", ["case-study/fpt"]),
            ("gpu pricing có CPA cao không", ["pricing/gpu"]),
            ("pricing gpu CTR ra sao", ["pricing/gpu"]),
            ("bảng giá gpu convert tốt không", ["pricing/gpu"]),
            ("pricing/gpu có PSI thấp không", ["pricing/gpu"]),
            ("gpu cloud cost blog có traffic không", ["blog/gpu-cloud-cost"]),
            ("cloud gpu cost organic thế nào", ["blog/gpu-cloud-cost"]),
            ("blog gpu cloud cost CTR thấp không", ["blog/gpu-cloud-cost"]),
            ("chi phí gpu cloud có ranking không", ["blog/gpu-cloud-cost"]),
            ("cloud gpu và gpu pricing performance", ["solutions/cloud-gpu", "pricing/gpu"]),
            ("case study và blog gpu cloud cost", ["case-study/fpt", "blog/gpu-cloud-cost"]),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertFilter(message, expected)

    def test_campaign_entities_names_and_substring_traps(self):
        cases = [
            ("phân tích campaign brand", ["greennode"]),
            ("ads brand campaign hiệu quả không", ["greennode"]),
            ("campaign greennode tháng này", ["greennode"]),
            ("brand trong ads có CPA cao không", ["greennode"]),
            ("campaign competitor ổn không", ["fpt", "viettel"]),
            ("đối thủ trong ads ra sao", ["fpt", "viettel"]),
            ("campaign FPT có CPA cao không", ["fpt", "viettel"]),
            ("campaign Viettel tháng này", ["fpt", "viettel"]),
            ("campaign GPU Cloud Q3", ["gg-search-gpu-cloud-q3"]),
            ("GG_Search_GPU_Cloud_Q3 CPA cao không", ["gg-search-gpu-cloud-q3"]),
            ("search gpu cloud q3 conversion thế nào", ["gg-search-gpu-cloud-q3"]),
            ("gpu cloud campaign performance", ["gg-search-gpu-cloud-q3"]),
            ("campaign awareness VNG", ["brand-awareness-vng"]),
            ("Brand_Awareness_VNG có reach tốt không", ["brand-awareness-vng"]),
            ("vng brand awareness performance", ["greennode", "brand-awareness-vng"]),
            ("campaign Competitor_FPT_GPU", ["competitor-fpt-gpu"]),
            ("fpt gpu competitor CPA cao không", ["fpt", "viettel", "competitor-fpt-gpu"]),
            ("campaign PMAX_GPU_Trial", ["pmax-gpu-trial"]),
            ("pmax gpu trial có conversion không", ["pmax-gpu-trial"]),
            ("trial gpu ads hiệu quả không", ["pmax-gpu-trial"]),
            ("campaign brand-x", ["brand-x"]),
            ("campaign fpt-cloud", ["fpt-cloud"]),
            ("campaign viettel-cloud", ["viettel-cloud"]),
            ("campaign Brand_New_Q3", ["brand_new_q3"]),
            ("campaign FPTCloud", ["fptcloud"]),
            ("campaign phải chứa cả gpu-cloud và q3", ["gpu-cloud", "q3"], "all"),
            ("campaign GG_Search_GPU_Cloud_Q3 trừ draft", ["gg-search-gpu-cloud-q3"], ["draft"]),
            ("campaign competitor trừ viettel-cloud", ["fpt", "viettel"], ["viettel-cloud"]),
        ]
        for case in cases:
            if len(case) == 3 and isinstance(case[2], str):
                message, expected, match_mode = case
                exclude = []
            else:
                message, expected = case[:2]
                exclude = case[2] if len(case) > 2 else []
                match_mode = "any"
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    expected,
                    exclude=exclude,
                    action="ads_perf",
                    scope="campaign",
                    match_mode=match_mode,
                )

    def test_negative_filters_quotes_punctuation_and_match_modes(self):
        cases = [
            ("product trừ blog", ["product"], ["blog"], "any"),
            ("tutorial ngoại trừ blog", ["tutorial"], ["blog"], "any"),
            ("vdb trừ mysql", ["product/vdb-"], ["product/vdb-mysql"], "any"),
            ("database ngoại trừ postgres", ["product/vdb-"], ["product/vdb-postgres"], "any"),
            ("cloud gpu trừ gpu pricing", ["solutions/cloud-gpu"], ["pricing/gpu"], "any"),
            ("case study trừ blog gpu cloud cost", ["case-study/fpt"], ["blog/gpu-cloud-cost"], "any"),
            ("product và tutorial trừ blog", ["product", "tutorial"], ["blog"], "any"),
            ('"product" trừ "blog"', ["product"], ["blog"], "any"),
            ("product, tutorial; trừ blog.", ["product", "tutorial"], ["blog"], "any"),
            ("url phải chứa cả product và tutorial", ["product", "tutorial"], [], "all"),
            ("trang chứa cả cloud gpu và gpu pricing", ["solutions/cloud-gpu", "pricing/gpu"], [], "all"),
            ("path phải chứa cả /pricing/gpu và /blog/gpu-cloud-cost", ["pricing/gpu", "blog/gpu-cloud-cost"], [], "all"),
            ("campaign brand trừ competitor", ["greennode"], ["fpt", "viettel"], "any", "campaign"),
            ("campaign FPT trừ Brand_Awareness_VNG", ["fpt", "viettel"], ["brand-awareness-vng"], "any", "campaign"),
            ("campaign phải chứa cả gpu-cloud và q3", ["gpu-cloud", "q3"], [], "all", "campaign"),
            ('campaign "brand-x" trừ "draft"', ["brand-x"], ["draft"], "any", "campaign"),
            ("lọc campaign fpt-cloud trừ competitor", ["fpt-cloud"], ["fpt", "viettel"], "any", "campaign"),
            ("trừ URL /staging/path", [], ["staging/path"], "any"),
            ("url chứa /pricing/gpu ngoại trừ /blog/gpu-cloud-cost", ["pricing/gpu"], ["blog/gpu-cloud-cost"], "any"),
            ("lọc URL custom_slug ngoại trừ archive_old", ["custom_slug"], ["archive_old"], "any"),
        ]
        for case in cases:
            message, include, exclude, match_mode = case[:4]
            scope = case[4] if len(case) > 4 else "url"
            with self.subTest(message=message):
                self.assertFilter(
                    message,
                    include,
                    exclude=exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    match_mode=match_mode,
                )

    def test_bad_llm_filter_specs_drop_metric_and_substring_noise(self):
        cases = [
            ("Phân tích số liệu SEO tháng gần nhất", "url", ["so", "lieu", "seo", "thang"]),
            ("hiệu suất SEO 6 tháng gần nhất", "url", ["hieu", "suat", "seo"]),
            ("cấu trúc site có ảnh hưởng SEO không", "url", ["cau", "tru", "truc", "structure"]),
            ("structure report cho toàn site", "url", ["structure", "report", "site"]),
            ("trung bình PSI mobile là bao nhiêu", "url", ["trung", "binh", "psi"]),
            ("điểm trung bình Core Web Vitals", "url", ["trung", "binh", "core", "web"]),
            ("run rate ads tuần này thế nào", "campaign", ["run", "rate", "ads"]),
            ("runbook tracking conversion có thiếu gì không", "url", ["run", "runbook", "tracking"]),
            ("ý tưởng trừu tượng về SEO thôi", "url", ["tru", "truu", "tuong"]),
            ("báo cáo trừu tượng không phải lọc trừ", "url", ["tru", "loc", "bao-cao"]),
            ("campaign đang active có bao nhiêu", "campaign", ["campaign", "dang", "active"]),
            ("danh sách campaign active và paused", "campaign", ["danh", "sach", "active", "paused"]),
            ("urlify tracking parameter có gây lỗi không", "url", ["url", "urlify", "tracking"]),
            ("curl command trong log có lỗi không", "url", ["url", "curl", "command"]),
            ("brand-new launch performance thế nào", "campaign", ["brand", "brand-new", "performance"]),
            ("fpt-cloud benchmark có cần theo dõi không", "campaign", ["fpt", "fpt-cloud", "benchmark"]),
            ("mySQLish keyword không phải mysql page", "url", ["mysql", "mysqlish"]),
            ("postgresqlite không phải postgres page", "url", ["postgres", "postgresqlite"]),
            ("cloud-gpuish token không phải cloud gpu entity", "url", ["cloud-gpu", "cloud-gpuish"]),
            ("gpu-pricingish copy test", "url", ["gpu-pricing", "gpu-pricingish"]),
            ("campaigning ideas cho Q3", "campaign", ["campaign", "campaigning", "q3"]),
            ("pageview tổng quan không phải page filter", "url", ["page", "pageview"]),
            ("landingness score có nghĩa gì", "url", ["landing", "landingness", "score"]),
            ("pathway traffic overview", "url", ["path", "pathway", "traffic"]),
        ]
        for message, scope, bad_terms in cases:
            with self.subTest(message=message):
                raw = {"scope": scope, "include": bad_terms, "exclude": list(reversed(bad_terms))}
                self.assertNoFilter(
                    message,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_bad_llm_filter_specs_keep_only_real_entities_or_explicit_user_terms(self):
        cases = [
            ("product traffic thế nào", "url", ["product", "traffic", "latest"], ["product"], []),
            ("tutorial tháng này", "url", ["tutorial", "month", "data"], ["tutorial"], []),
            ("blog organic giảm không", "url", ["blog", "organic", "giam"], ["blog"], []),
            ("pricing convert thế nào", "url", ["pricing", "price", "convert"], ["pricing"], []),
            ("vdb mysql đang tụt", "url", ["vdb", "mysql", "run"], ["product/vdb-mysql"], []),
            ("postgresql page có chậm không", "url", ["postgres", "postgresql", "page"], ["product/vdb-postgres"], []),
            ("cloud gpu traffic thế nào", "url", ["cloud", "gpu", "cloud-gpu"], ["solutions/cloud-gpu"], []),
            ("case study FPT có conversion không", "url", ["case", "study", "fpt", "conversion"], ["case-study/fpt"], []),
            ("gpu pricing có CPA cao không", "url", ["gpu", "pricing", "cpa"], ["pricing/gpu"], []),
            ("gpu cloud cost blog có traffic không", "url", ["gpu", "cloud", "cost", "blog"], ["blog/gpu-cloud-cost"], []),
            ("url chứa custom-slug", "url", ["custom-slug", "latest"], ["custom-slug"], []),
            ("lọc URL custom_slug", "url", ["custom_slug", "seo"], ["custom_slug"], []),
            ("url custom/path trừ staging", "url", ["custom/path", "seo"], ["custom/path"], ["staging"]),
            ("campaign brand", "campaign", ["brand", "campaign", "latest"], ["greennode"], []),
            ("campaign competitor", "campaign", ["competitor", "ads"], ["fpt", "viettel"], []),
            ("campaign FPT", "campaign", ["fpt", "fpt-cloud"], ["fpt", "viettel"], []),
            ("campaign brand-x", "campaign", ["brand", "brand-x"], ["brand-x"], []),
            ("campaign fpt-cloud", "campaign", ["fpt", "fpt-cloud"], ["fpt-cloud"], []),
            ("campaign chứa exact-name", "campaign", ["exact-name", "ads"], ["exact-name"], []),
            ("lọc campaign summer-sale trừ draft", "campaign", ["summer-sale", "latest"], ["summer-sale"], ["draft"]),
        ]
        for message, scope, raw_include, expected_include, expected_exclude in cases:
            with self.subTest(message=message):
                raw = {"scope": scope, "include": raw_include, "exclude": expected_exclude}
                self.assertFilter(
                    message,
                    expected_include,
                    exclude=expected_exclude,
                    action="ads_perf" if scope == "campaign" else "seo_query",
                    scope=scope,
                    extra={"filter_spec": raw},
                )

    def test_intent_overrides_do_not_create_or_hide_filters(self):
        cases = [
            ("Phân tích số liệu SEO tháng gần nhất", "seo_query", "url", []),
            ("SEO ổn không", "seo_query", "url", []),
            ("traffic organic tháng này ra sao", "seo_query", "url", []),
            ("phân tích kết quả PageSpeed lần chạy gần nhất", "query_results", "url", []),
            ("trung bình PSI mobile là bao nhiêu", "query_results", "url", []),
            ("trang nào chậm nhất", "query_results", "url", []),
            ("tracking conversion ổn không", "tracking_audit", "url", []),
            ("runbook tracking conversion có thiếu gì không", "tracking_audit", "url", []),
            ("vì sao product không convert", "diagnose_drop", "url", ["product"]),
            ("vì sao cloud gpu tụt conversion", "diagnose_drop", "url", ["solutions/cloud-gpu"]),
            ("root cause gpu pricing conversion tụt", "diagnose_drop", "url", ["pricing/gpu"]),
            ("làm experiment cho product", "experiment_plan", "url", ["product"]),
            ("experiment cho case study FPT", "experiment_plan", "url", ["case-study/fpt"]),
            ("tăng tốc vdb mysql", "fix_suggest", "url", ["product/vdb-mysql"]),
            ("tăng tốc cloud gpu", "fix_suggest", "url", ["solutions/cloud-gpu"]),
            ("ưu tiên fix toàn site", "priority_fix", "url", []),
            ("nên tối ưu gpu cloud cost trước", "priority_fix", "url", ["blog/gpu-cloud-cost"]),
            ("kế hoạch tuần này", "action_plan", "url", []),
            ("kế hoạch cho tutorial", "action_plan", "url", ["tutorial"]),
            ("alerts hôm nay", "alerts", "url", []),
            ("cảnh báo pricing gpu", "alerts", "url", ["pricing/gpu"]),
            ("ads hiệu quả không", "ads_perf", "campaign", []),
            ("campaign nào đang active", "ads_list", "campaign", []),
            ("campaign FPT có CPA cao không", "ads_perf", "campaign", ["fpt", "viettel"]),
            ("campaign fpt-cloud có CPA cao không", "ads_perf", "campaign", ["fpt-cloud"]),
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
