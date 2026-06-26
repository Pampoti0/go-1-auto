import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class SystemInsightsUtilitiesTest(unittest.TestCase):
    def test_data_report_messages_can_drop_noisy_history(self):
        history = [
            {"role": "user", "content": "đang so với đâu?"},
            {"role": "assistant", "content": "SEO tháng 2026-06 đang so với 2026-05; cột change_% nằm trong SEO Sheet."},
        ]

        msgs = server._analysis_messages("PSI DATA", history, "báo cáo PageSpeed", include_history=False)
        joined = "\n".join(str(m.get("content", "")) for m in msgs)

        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertIn("PSI DATA", joined)
        self.assertIn("báo cáo PageSpeed", joined)
        self.assertNotIn("SEO tháng", joined)
        self.assertNotIn("change_%", joined)

    def test_friendly_error_text_maps_common_auth_and_quota_errors(self):
        self.assertIn("Token Google", server._friendly_error_text("invalid_grant: Token has been expired or revoked"))
        self.assertIn("Thiếu quyền", server._friendly_error_text("HttpError 403 insufficient permission"))
        self.assertIn("quota", server._friendly_error_text("429 rate limit quota exceeded").lower())
        self.assertIn("timeout", server._friendly_error_text("ConnectTimeout: timed out").lower())

    def test_with_meta_includes_cache_source_and_note(self):
        key = "unit:meta"
        server._api_cache_meta[key] = {"key": key, "ts": time.time(), "ttl": 60, "source": "api"}
        try:
            out = server._with_meta({"ok": True}, "Unit Source", window="7 days", cache_key=key, note="read-only")
        finally:
            server._api_cache_meta.pop(key, None)

        self.assertTrue(out["ok"])
        self.assertEqual(out["_meta"]["source"], "Unit Source")
        self.assertEqual(out["_meta"]["window"], "7 days")
        self.assertEqual(out["_meta"]["note"], "read-only")
        self.assertEqual(out["_meta"]["cache"]["source"], "api")
        self.assertTrue(out["_meta"]["cache"]["fresh"])

    def test_cached_reads_from_disk_after_memory_clear(self):
        old_dir = server._API_CACHE_DIR
        old_enabled = server._API_DISK_CACHE
        with tempfile.TemporaryDirectory() as tmp:
            server._API_CACHE_DIR = Path(tmp)
            server._API_DISK_CACHE = True
            server._invalidate_cache()
            try:
                first = server._cached("unit:disk", 60, lambda: {"value": 1})
                server._api_cache.clear()
                server._api_cache_meta.clear()
                second = server._cached("unit:disk", 60, lambda: {"value": 2})
            finally:
                server._invalidate_cache()
                server._API_CACHE_DIR = old_dir
                server._API_DISK_CACHE = old_enabled

        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})

    def test_cached_can_return_stale_disk_without_blocking_refresh(self):
        old_dir = server._API_CACHE_DIR
        old_enabled = server._API_DISK_CACHE
        with tempfile.TemporaryDirectory() as tmp:
            server._API_CACHE_DIR = Path(tmp)
            server._API_DISK_CACHE = True
            server._invalidate_cache()
            try:
                p = server._cache_disk_path("unit:stale")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    '{"key":"unit:stale","ts":%s,"value":{"value":1}}' % (time.time() - 120),
                    encoding="utf-8",
                )
                val = server._cached(
                    "unit:stale",
                    1,
                    lambda: {"value": 2},
                    allow_stale=True,
                    stale_ttl=3600,
                    refresh_stale=False,
                )
                meta = server._cache_meta_for("unit:stale")
            finally:
                server._invalidate_cache()
                server._API_CACHE_DIR = old_dir
                server._API_DISK_CACHE = old_enabled

        self.assertEqual(val, {"value": 1})
        self.assertEqual(meta["source"], "stale-disk")
        self.assertFalse(meta["fresh"])

    def test_weekly_autopilot_text_is_actionable_without_raw_json(self):
        report = {
            "summary": {"alerts": 2, "high_alerts": 1, "opportunities": 3, "root_hypotheses": 4, "tracking_health": "warn"},
            "next_actions": [
                {
                    "priority": "P0",
                    "title": "Fix conversion tracking",
                    "why": "7.8M spend · 0 conversion",
                    "source": "Tracking",
                    "confidence": "high",
                }
            ],
        }
        text = server._weekly_autopilot_text(report)
        self.assertIn("Weekly Autopilot", text)
        self.assertIn("Fix conversion tracking", text)
        self.assertIn("Evidence", text)
        self.assertNotIn("/api/root-cause", text)

    def test_weekly_derived_reports_reuse_shared_inputs(self):
        old_ads_alerts = server._ads_campaign_alerts
        server._ads_campaign_alerts = lambda days=7: []
        try:
            opp = {
                "run": "2026-06",
                "month": "2026-06",
                "opportunities": [
                    {
                        "path": "/product/cloud-server",
                        "score": 90,
                        "sources": ["Ads", "PSI"],
                        "evidence": ["Ads cost 1M, 0 conversion", "PSI mobile 42/100"],
                        "confidence": "high",
                    },
                    {"path": "/blog", "score": 40, "sources": ["SEO"], "evidence": ["SEO traffic 10"], "confidence": "low"},
                ],
            }
            tracking = {
                "health": "bad",
                "issues": [
                    {
                        "scope": "landing_page",
                        "path": "/product/cloud-server",
                        "lv": "high",
                        "score": 95,
                        "text": "Missing conversion",
                        "evidence": ["Clicks 100, conversions 0"],
                        "confidence": "high",
                    }
                ],
            }

            alerts = server._alert_report_from_opportunity(opp, 50)
            root = server._root_cause_report_from_inputs(opp, tracking, 12)
            experiments = server._experiment_report_from_inputs(opp, tracking, 8)
        finally:
            server._ads_campaign_alerts = old_ads_alerts

        self.assertEqual(len(server._slice_opportunity_report(opp, 1)["opportunities"]), 1)
        self.assertEqual(alerts["alerts"][0]["lv"], "high")
        self.assertEqual(root["hypotheses"][0]["target"], "/product/cloud-server")
        self.assertEqual(experiments["experiments"][0]["type"], "tracking")

    def test_weekly_groups_skip_alerts_derived_from_opportunities(self):
        old_disk_cache = server._API_DISK_CACHE
        old_opportunity_report = server._opportunity_report
        old_tracking_report = server._conversion_tracking_report
        old_ads_alerts = server._ads_campaign_alerts
        server._API_DISK_CACHE = False
        server._invalidate_cache()
        try:
            server._opportunity_report = lambda limit=50: {
                "run": "2026-06",
                "month": "2026-06",
                "opportunities": [{
                    "path": "/blog",
                    "score": 98,
                    "sources": ["PSI", "SEO"],
                    "evidence": ["PSI mobile 43.0/100", "LCP 26704ms"],
                    "confidence": "medium",
                }],
            }
            server._conversion_tracking_report = lambda days=30: {"health": "ok", "issues": []}
            server._ads_campaign_alerts = lambda days=7: []

            report = server._weekly_autopilot_report()
        finally:
            server._invalidate_cache()
            server._API_DISK_CACHE = old_disk_cache
            server._opportunity_report = old_opportunity_report
            server._conversion_tracking_report = old_tracking_report
            server._ads_campaign_alerts = old_ads_alerts

        self.assertEqual(report["summary"]["alerts"], 0)
        group = report["entity_groups"][0]
        self.assertNotIn("alerts", group["detail_counts"])
        self.assertEqual(group["detail_counts"]["opportunities"], 1)

    def test_insight_entity_groups_merge_cross_tab_details(self):
        groups = server._insight_entity_groups(
            actions=[{
                "target": "/product/cloud-server/",
                "title": "Fix cloud server",
                "priority": "P1",
                "why": "LCP cao",
                "confidence": "medium",
                "source": "Weekly",
            }],
            alerts=[{
                "path": "/product/cloud-server",
                "lv": "high",
                "text": "/product/cloud-server: score cao",
                "evidence": ["Alert evidence"],
                "confidence": "high",
                "source": "PSI",
                "score": 90,
            }],
            opportunities=[{
                "path": "/product/cloud-server",
                "score": 88,
                "sources": ["PSI", "SEO"],
                "evidence": ["Opportunity evidence"],
                "confidence": "medium",
            }],
            root_causes=[{
                "target": "/product/cloud-server",
                "score": 86,
                "sources": ["Tracking"],
                "root_causes": ["Tracking thiếu tín hiệu"],
                "evidence": ["Root evidence"],
                "confidence": "high",
            }],
            experiments=[{
                "target": "/product/cloud-server",
                "title": "Experiment cloud server",
                "priority_score": 84,
                "baseline": ["Experiment baseline"],
                "confidence": "medium",
                "sources": ["Experiment"],
            }],
            tracking_issues=[{
                "path": "/product/cloud-server",
                "lv": "high",
                "score": 92,
                "text": "Missing conversion",
                "evidence": ["Tracking evidence"],
                "confidence": "high",
            }],
            limit=10,
        )

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["key"], "/product/cloud-server")
        self.assertEqual(group["priority"], "P0")
        self.assertEqual(group["score"], 92)
        self.assertEqual(group["confidence"], "high")
        self.assertEqual(group["sources"], ["Experiment", "PSI", "SEO", "Tracking", "Weekly"])
        self.assertEqual(group["detail_counts"]["actions"], 1)
        self.assertEqual(group["detail_counts"]["alerts"], 1)
        self.assertEqual(group["detail_counts"]["opportunities"], 1)
        self.assertEqual(group["detail_counts"]["root_causes"], 1)
        self.assertEqual(group["detail_counts"]["experiments"], 1)
        self.assertEqual(group["detail_counts"]["tracking_issues"], 1)
        self.assertIn("Tracking evidence", group["evidence"])

    def test_insight_paths_are_canonical_without_trailing_slash(self):
        self.assertEqual(server._path_only("https://greennode.ai/blog/"), "/blog")
        self.assertEqual(server._path_only("https://greennode.ai/blog?utm_source=x"), "/blog")
        self.assertEqual(server._path_only("https://greennode.ai/"), "/")

    def test_tracking_landing_pages_group_by_canonical_path(self):
        rows = [
            {"base_url": "https://greennode.ai/blog/", "impressions": 10, "clicks": 2, "cost": 100, "conversions": 0},
            {"base_url": "https://greennode.ai/blog", "impressions": 20, "clicks": 3, "cost": 200, "conversions": 0},
        ]
        groups = server._ads_metric_groups(
            rows,
            lambda r: server._path_only(r.get("base_url") or r.get("url") or ""),
            lambda r: server._path_only(r.get("base_url") or r.get("url") or ""),
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "/blog")
        self.assertEqual(groups[0]["cost"], 300)
        self.assertEqual(groups[0]["clicks"], 5)

    def test_root_cause_merges_duplicate_targets(self):
        opp = {
            "opportunities": [
                {"path": "/blog/", "score": 50, "sources": ["SEO"], "evidence": ["SEO traffic 10"], "confidence": "low"},
                {"path": "/blog", "score": 48, "sources": ["PSI"], "evidence": ["LCP 5000ms"], "confidence": "low"},
            ]
        }
        tracking = {"health": "warn", "issues": []}

        root = server._root_cause_report_from_inputs(opp, tracking, 12)

        self.assertEqual(len(root["hypotheses"]), 1)
        self.assertEqual(root["hypotheses"][0]["target"], "/blog")
        self.assertEqual(root["hypotheses"][0]["sources"], ["PSI", "SEO"])
        self.assertIn("SEO traffic 10", root["hypotheses"][0]["evidence"])
        self.assertIn("LCP 5000ms", root["hypotheses"][0]["evidence"])

    def test_notification_delivery_gap_is_grounded(self):
        text = server._capability_gap_reply("Có tự động gửi báo cáo tuần qua Slack webhook được không?")
        self.assertIsNotNone(text)
        self.assertIn("Chưa có tính năng", text)
        self.assertIn("chưa có bước tự đẩy thông báo", text)
        self.assertIn("DeCho chưa tự gửi được", text)
        self.assertNotIn("module outbound", text)
        self.assertNotIn("retry/backoff", text)

        self.assertIsNone(server._capability_gap_reply("báo cáo tuần"))

    def test_insights_cache_question_is_grounded(self):
        text = server._insights_grounded_reply("Insights có gọi API mới liên tục không, hay dùng cache?")

        self.assertIsNotNone(text)
        self.assertIn("Có cache", text)
        self.assertIn("Tải lại", text)
        self.assertIn("PageSpeed Sheet", text)
        self.assertIn("SEO Sheet", text)
        self.assertIn("Google Ads", text)
        self.assertIn("Clarity", text)
        self.assertNotIn(".cache/api", text)
        self.assertNotIn("API_STALE_CACHE_TTL", text)
        self.assertNotIn("local/dev", text)

        self.assertIsNone(server._insights_grounded_reply("SEO tháng này thế nào?"))

    def test_repair_pagespeed_report_prefix_fixes_clipped_titles(self):
        cases = {
            "Speed Report — greennode.ai": "Báo cáo PageSpeed — greennode.ai",
            "PageSpeed Report — greennode.ai": "Báo cáo PageSpeed — greennode.ai",
            "Actions ... PageSpeed Report — greennode.ai": "Actions ... Báo cáo PageSpeed — greennode.ai",
            "áo cáo PageSpeed — greennode.ai": "Báo cáo PageSpeed — greennode.ai",
            "  ## ổng quan\n\nScore": "  ## Tổng quan\n\nScore",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(server._repair_pagespeed_report_prefix(raw), expected)

    def test_pagespeed_report_guard_replaces_raw_analysis_code(self):
        headers = ["Timestamp", "URL", "Strategy", "Performance Score", "FCP (ms)", "LCP (ms)", "CLS", "TBT (ms)"]
        rows = [
            ["2026-06-25 09:00:00", "https://greennode.ai/blog", "MOBILE", "43", "900", "26704", "0.12", "801"],
            ["2026-06-25 09:00:00", "https://greennode.ai/product/vdb-postgresql", "MOBILE", "35", "1200", "4652", "1.002", "883"],
            ["2026-06-25 09:00:00", "https://greennode.ai/", "DESKTOP", "75", "500", "882", "0.019", "460"],
        ]
        raw = 'Đệ sẽ phân tích. ```python\nimport pandas as pd\ndata = """Timestamp | URL | Strategy"""\nprint(data)\n```'

        cleaned = server._guard_pagespeed_report_text(raw, "2026-06", headers, rows)

        self.assertIn("Báo cáo PageSpeed (2026-06)", cleaned)
        self.assertIn("/blog", cleaned)
        self.assertIn("Evidence: PSI Sheet tab 2026-06", cleaned)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("import pandas", cleaned)

    def test_repair_response_prefix_fixes_common_clipped_text(self):
        cases = {
            "ưa có tính năng này trong hệ thống nha Đại ca.": "Chưa có tính năng này trong hệ thống nha Đại ca.",
            "ưới đây là báo cáo PageSpeed đầy đủ": "Dưới đây là báo cáo PageSpeed đầy đủ",
            "áo cáo SEO tháng gần nhất": "Báo cáo SEO tháng gần nhất",
            "## óm tắt nhanh": "## Tóm tắt nhanh",
            "  ### huyến nghị\n- Fix CLS": "  ### Khuyến nghị\n- Fix CLS",
            "ên làm tiếp\n1. Check tracking": "Nên làm tiếp\n1. Check tracking",
            "hận xét nhanh": "Nhận xét nhanh",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(server._repair_response_prefix(raw), expected)

    def test_clean_reply_strips_thinking_and_repairs_prefix(self):
        self.assertEqual(
            server._clean_reply("<think>draft</think>ưới đây là báo cáo"),
            "Dưới đây là báo cáo",
        )
        self.assertEqual(
            server._clean_reply("Ảnh hưởng: UX kém, có thể误触 CTA."),
            "Ảnh hưởng: UX kém, có thể bấm nhầm CTA.",
        )
        self.assertEqual(
            server._clean_reply("Nội dung ổn 你好 nhưng cần tối ưu LCP."),
            "Nội dung ổn nhưng cần tối ưu LCP.",
        )

    def test_psi_comparison_reply_uses_decho_voice(self):
        reply = server._psi_comparison_reply()
        self.assertIn("Đệ", reply)
        self.assertIn("Đại ca", reply)
        self.assertIn("không có một mốc so sánh cố định", reply)
        self.assertNotIn("DeCho không dùng", reply)
        self.assertNotIn("Nó đang", reply)

    def test_comparison_period_followup_does_not_route_to_pagespeed(self):
        messages = [
            "đang so sánh với tháng mấy?",
            "đang so sánh với tháng nào?",
            "đang so với tháng nào?",
            "report này so với tháng nào?",
            "báo cáo này đang so với kỳ nào?",
            "baseline là tháng mấy?",
            "baseline month là gì?",
            "cột change % so với tháng nào?",
            "change đang so với kỳ nào vậy?",
            "change % là so với đâu?",
            "cột change này so với cái nào?",
            "tăng giảm là so với tháng trước hay tháng nào?",
            "tăng giảm đang tính từ đâu?",
            "đang compare với period nào?",
            "so với kỳ trước là kỳ nào?",
            "kỳ trước cụ thể là khi nào?",
            "đang so với đâu?",
        ]

        for msg in messages:
            with self.subTest(msg=msg):
                self.assertTrue(server._looks_comparison_period_question(msg))
                self.assertIsNone(server._all_keyword_intent(msg))

        reply = server._comparison_period_reply([
            {"role": "assistant", "content": "Report 2026-06** (44 URLs — tháng gần nhất)"}
        ])
        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)
        self.assertNotIn("không có dữ liệu SEO", reply.lower())

    def test_comparison_period_followup_does_not_catch_unrelated_compare(self):
        messages = [
            "so với đối thủ thì sao?",
            "so sánh product và tutorial",
            "so sánh giá H100 ngoài thị trường",
            "PageSpeed so với SEO có liên quan không?",
        ]
        for msg in messages:
            with self.subTest(msg=msg):
                self.assertFalse(server._looks_comparison_period_question(msg))

    def test_comparison_period_uses_current_report_month_not_baseline(self):
        reply = server._comparison_period_reply([
            {"role": "assistant", "content": "Report — Tháng 2026-06\nTổng quan: Views 2,400, Clicks 61, Impressions 10,800."},
            {"role": "assistant", "content": "Đệ so với tháng 5/2026 (2026-05) — tháng liền trước đó."},
        ])
        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)
        self.assertNotIn("2026-04", reply)

    def test_comparison_period_does_not_assign_seo_when_history_is_psi_only(self):
        reply = server._comparison_period_reply([
            {"role": "assistant", "content": "Đọc 150 dòng từ PSI Sheet tab 2026-06. LCP, CLS, TBT và Performance Score."}
        ])
        self.assertIn("PageSpeed", reply)
        self.assertIn("không có một mốc so sánh cố định", reply)
        self.assertIn("các lần đo PSI", reply)
        self.assertNotIn("không có kỳ so sánh tháng", reply)
        self.assertNotIn("SEO", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)

    def test_comparison_period_uses_latest_pagespeed_context_even_with_seo_word(self):
        reply = server._comparison_period_reply([
            {"role": "assistant", "content": "Report 2026-06** (44 URLs — tháng gần nhất)\nTổng quan: Views 131, Users 98, Clicks 77, Impressions 520."},
            {"role": "assistant", "content": "PageSpeed Report — Tháng 2026-06\nBlog/Tutorial LCP bùng nổ. Nếu đây là trang quan trọng (SEO blog), cần ưu tiên ngay. PSI Sheet tab 2026-06, LCP, CLS, TBT."},
        ])
        self.assertIn("PageSpeed", reply)
        self.assertIn("không có một mốc so sánh cố định", reply)
        self.assertNotIn("SEO", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)
        self.assertNotIn("2026-05", reply)

    def test_comparison_period_ignores_prior_pagespeed_baseline_answer(self):
        reply = server._comparison_period_reply([
            {"role": "assistant", "content": "Báo cáo PageSpeed — Tháng 2026-06\nMobile Score giảm. Blog/Tutorial LCP bùng nổ. Nếu đây là trang quan trọng (SEO blog), cần ưu tiên ngay. PSI Sheet tab 2026-06, LCP, CLS, TBT."},
            {"role": "assistant", "content": "Với báo cáo PageSpeed vừa rồi, DeCho không dùng một mốc so sánh cố định. Nó đang đọc các lần đo PSI có trong tab hiện tại. Nếu thấy giảm/tăng/phục hồi trong PageSpeed thì đó là so giữa các lần đo PSI đang có."},
        ])
        self.assertIn("PageSpeed", reply)
        self.assertIn("không có một mốc so sánh cố định", reply)
        self.assertNotIn("SEO", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)
        self.assertNotIn("2026-05", reply)

    def test_comparison_period_ignores_prior_comparison_answers_as_current_month(self):
        old = server._seo_list_tabs
        server._seo_list_tabs = lambda: ["2026-04", "2026-05", "2026-06"]
        try:
            reply = server._comparison_period_reply([
                {"role": "assistant", "content": "Với báo cáo SEO trong context hiện tại: SEO tháng 2026-05 đang so với 2026-04 (tháng liền trước)."},
                {"role": "assistant", "content": "Đệ so với tháng 5/2026 (2026-05) — tháng liền trước đó."},
            ])
        finally:
            server._seo_list_tabs = old

        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)
        self.assertNotIn("2026-04", reply)

    def test_comparison_period_followup_uses_latest_seo_tab_without_history(self):
        old = server._seo_list_tabs
        server._seo_list_tabs = lambda: ["2026-04", "2026-05", "2026-06"]
        try:
            reply = server._comparison_period_reply([])
        finally:
            server._seo_list_tabs = old

        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)

    def test_pagespeed_seo_relationship_question_is_grounded(self):
        reply = server._metric_relationship_reply("PageSpeed so với SEO có liên quan không?")
        self.assertIsNotNone(reply)
        self.assertIn("Có liên quan", reply)
        self.assertIn("PageSpeed/PSI", reply)
        self.assertIn("SEO/GSC/GA4", reply)
        self.assertIn("vừa chậm vừa có traffic", reply)
        self.assertNotIn("nói rõ hơn", reply.lower())

    def test_relationship_answer_is_not_used_as_latest_data_context(self):
        relation = server._metric_relationship_reply("PageSpeed so với SEO có liên quan không?")
        self.assertTrue(server._looks_metric_relationship_answer(relation))
        self.assertIsNone(server._latest_data_context([
            {"role": "assistant", "content": relation},
        ]))

    def test_session_data_context_overrides_noisy_history(self):
        uid = "test-user-context"
        sid = "test-session-context"
        server._SESSION_DATA_CONTEXT.pop(server._data_context_key(uid, sid), None)
        noisy_history = [
            {"role": "assistant", "content": "Báo cáo PageSpeed — Tháng 2026-06. PSI Sheet tab 2026-06, LCP, CLS, TBT."},
            {"role": "assistant", "content": "Với báo cáo SEO trong context hiện tại: SEO tháng 2026-06 đang so với 2026-05."},
        ]

        server._remember_data_context(uid, sid, "psi", "2026-06")
        reply = server._comparison_period_reply(noisy_history, uid, sid)
        self.assertIn("PageSpeed", reply)
        self.assertIn("không có một mốc so sánh cố định", reply)
        self.assertNotIn("SEO", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)

        server._remember_data_context(uid, sid, "seo", "2026-06")
        reply = server._comparison_period_reply(noisy_history, uid, sid)
        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)
        self.assertNotIn("không có một mốc so sánh cố định", reply)

    def test_session_data_context_is_scoped_by_user_and_session(self):
        sid = "shared-session-id"
        user_a = "user-a"
        user_b = "user-b"
        for uid in (user_a, user_b):
            server._SESSION_DATA_CONTEXT.pop(server._data_context_key(uid, sid), None)

        server._remember_data_context(user_a, sid, "psi", "2026-06")
        server._remember_data_context(user_b, sid, "seo", "2026-06")

        reply_a = server._comparison_period_reply([], user_a, sid)
        reply_b = server._comparison_period_reply([], user_b, sid)

        self.assertIn("PageSpeed", reply_a)
        self.assertNotIn("SEO", reply_a)
        self.assertIn("SEO tháng 2026-06", reply_b)

    def test_session_data_context_persists_to_disk_with_ttl(self):
        old_file = server._SESSION_CONTEXT_FILE
        old_ttl = server._SESSION_CONTEXT_TTL
        old_loaded = server._SESSION_CONTEXT_LOADED
        old_context = dict(server._SESSION_DATA_CONTEXT)
        try:
            with tempfile.TemporaryDirectory() as td:
                server._SESSION_CONTEXT_FILE = Path(td) / "session_context.json"
                server._SESSION_CONTEXT_TTL = 3600
                server._SESSION_CONTEXT_LOADED = False
                server._SESSION_DATA_CONTEXT.clear()

                server._remember_data_context("persist-user", "persist-session", "psi", "2026-06")
                self.assertTrue(server._SESSION_CONTEXT_FILE.exists())

                server._SESSION_DATA_CONTEXT.clear()
                server._SESSION_CONTEXT_LOADED = False
                reply = server._comparison_period_reply([], "persist-user", "persist-session")
                self.assertIn("PageSpeed", reply)

                key = server._data_context_key("persist-user", "persist-session")
                server._SESSION_DATA_CONTEXT[key] = {"kind": "seo", "month": "2026-06", "ts": time.time() - 120}
                server._SESSION_CONTEXT_TTL = 60
                self.assertIsNone(server._session_data_context("persist-user", "persist-session"))
        finally:
            server._SESSION_CONTEXT_FILE = old_file
            server._SESSION_CONTEXT_TTL = old_ttl
            server._SESSION_CONTEXT_LOADED = old_loaded
            server._SESSION_DATA_CONTEXT.clear()
            server._SESSION_DATA_CONTEXT.update(old_context)

    def test_pending_confirmation_is_scoped_by_user_and_session(self):
        sid = "shared-session-id"
        key_a = server._pending_key("user-a", sid)
        key_b = server._pending_key("user-b", sid)
        server._pending_by_session.pop(key_a, None)
        server._pending_by_session.pop(key_b, None)

        pend_a = server._pending_for("user-a", sid)
        pend_b = server._pending_for("user-b", sid)
        pend_a["op"]["action"] = "add_url"

        self.assertNotEqual(key_a, key_b)
        self.assertEqual(pend_b["op"], {})

    def test_job_result_summary_and_public_payload_stay_small(self):
        summary = server._job_result_summary({"summary": {"alerts": 3}, "next_actions": [{}, {}], "experiments": [{}]})
        self.assertEqual(summary["summary"], {"alerts": 3})
        self.assertEqual(summary["counts"]["next_actions"], 2)
        self.assertEqual(summary["counts"]["experiments"], 1)

        public = server._job_public({
            "id": "abc",
            "status": "done",
            "result": {"large": True},
            "result_raw": {"secret": True},
            "result_summary": summary,
        })
        self.assertEqual(public, {"id": "abc", "status": "done", "result_summary": summary})


if __name__ == "__main__":
    unittest.main()
