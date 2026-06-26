import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class FollowupHardeningTest(unittest.TestCase):
    def setUp(self):
        self._touched_context_keys = set()

    def tearDown(self):
        for key in self._touched_context_keys:
            server._SESSION_DATA_CONTEXT.pop(key, None)

    def remember_context(self, user_id, session_id, kind, month="2026-06"):
        key = server._data_context_key(user_id, session_id)
        if key:
            self._touched_context_keys.add(key)
        server._remember_data_context(user_id, session_id, kind, month)

    def assert_period_followup(self, msg):
        self.assertTrue(
            server._looks_comparison_period_question(msg),
            msg=f"{msg!r} should route as a comparison-period follow-up",
        )
        self.assertIsNone(server._all_keyword_intent(msg), msg=f"{msg!r} should not route to a new keyword intent")

    def assert_not_period_followup(self, msg):
        self.assertFalse(
            server._looks_comparison_period_question(msg),
            msg=f"{msg!r} should not be treated as a comparison-period follow-up",
        )

    def test_short_vietnamese_followups_are_comparison_period_questions(self):
        messages = [
            "đang so với đâu",
            "dang so voi dau",
            "đang so zới đâu",
            "so với gì",
            "so voi gi",
            "so vs gì",
            "kỳ nào",
            "ky nao",
            "kì nào vậy",
            "change này là sao",
            "change nay la sao",
            "change này sao á",
            "chaneg này là sao",
        ]

        for msg in messages:
            with self.subTest(msg=msg):
                self.assert_period_followup(msg)

    def test_period_followup_variants_with_report_language_are_comparison_period_questions(self):
        messages = [
            "report này đang so với kỳ nào",
            "báo cáo này so với mốc nào vậy",
            "cot change nay dang so voi gi",
            "change % này tính từ đâu",
            "tăng giảm này lấy mốc nào",
            "baseline month là tháng nào",
            "period compare là kỳ nào",
            "đợt trước cụ thể là tháng nào",
        ]

        for msg in messages:
            with self.subTest(msg=msg):
                self.assert_period_followup(msg)

    def test_unrelated_compare_questions_are_not_comparison_period_followups(self):
        messages = [
            "so với đối thủ thì sao?",
            "so sánh product và tutorial",
            "product vs tutorial bên nào tốt hơn?",
            "PageSpeed so với SEO có liên quan không?",
            "PSI với SEO khác nhau gì?",
            "so sánh giá H100 ngoài thị trường",
            "giá thị trường cloud server đang so với AWS thế nào?",
        ]

        for msg in messages:
            with self.subTest(msg=msg):
                self.assert_not_period_followup(msg)

    def test_general_business_language_is_not_caught_as_period_followup(self):
        messages = [
            "baseline experiment cho product tuần này",
            "baseline success metric của experiment là gì",
            "change request này xử lý sao",
            "change log release này là sao",
            "so với KPI mục tiêu thì chưa đạt à",
            "mốc launch product là ngày nào",
            "period pricing của gói cloud server là gì",
        ]

        for msg in messages:
            with self.subTest(msg=msg):
                self.assert_not_period_followup(msg)

    def test_latest_history_context_can_switch_psi_seo_psi_without_session_memory(self):
        history = [
            {"role": "assistant", "content": "PageSpeed Report — Tháng 2026-05. PSI Sheet tab 2026-05, LCP, CLS, TBT."},
            {"role": "assistant", "content": "SEO Sheet tab 2026-06. Clicks, impressions, views và users đều có change_%."},
            {"role": "assistant", "content": "Báo cáo PageSpeed — Tháng 2026-06. PSI Sheet tab 2026-06, LCP xấu dù đây là SEO blog quan trọng."},
        ]

        self.assertEqual(server._latest_data_context(history), "psi")
        reply = server._comparison_period_reply(history)
        self.assertIn("PageSpeed", reply)
        self.assertIn("không có một mốc so sánh cố định", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)

        history = history[:2]
        self.assertEqual(server._latest_data_context(history), "seo")
        reply = server._comparison_period_reply(history)
        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)

    def test_session_context_wins_over_noisy_history_when_switching_psi_seo_psi(self):
        user_id = "followup-hardening-switch-user"
        session_id = "followup-hardening-switch-session"
        noisy_history = [
            {"role": "assistant", "content": "SEO Sheet tab 2026-06. Clicks_change_% đang so với 2026-05."},
            {"role": "assistant", "content": "Với báo cáo PageSpeed vừa rồi, DeCho không dùng một mốc so sánh cố định."},
            {"role": "assistant", "content": "Báo cáo PageSpeed — Tháng 2026-06. PSI Sheet tab 2026-06, LCP, CLS, TBT."},
        ]

        self.remember_context(user_id, session_id, "psi", "2026-06")
        reply = server._comparison_period_reply(noisy_history, user_id, session_id)
        self.assertIn("PageSpeed", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)

        self.remember_context(user_id, session_id, "seo", "2026-06")
        reply = server._comparison_period_reply(noisy_history, user_id, session_id)
        self.assertIn("SEO tháng 2026-06", reply)
        self.assertIn("2026-05", reply)
        self.assertNotIn("không có một mốc so sánh cố định", reply)

        self.remember_context(user_id, session_id, "psi", "2026-06")
        reply = server._comparison_period_reply(noisy_history, user_id, session_id)
        self.assertIn("PageSpeed", reply)
        self.assertNotIn("SEO tháng 2026-06", reply)

    def test_data_context_is_isolated_by_user_and_session(self):
        shared_session = "followup-hardening-shared-session"
        user_a = "followup-hardening-user-a"
        user_b = "followup-hardening-user-b"
        other_session = "followup-hardening-other-session"

        self.remember_context(user_a, shared_session, "psi", "2026-06")
        self.remember_context(user_b, shared_session, "seo", "2026-06")
        self.remember_context(user_a, other_session, "seo", "2026-05")

        self.assertNotEqual(server._data_context_key(user_a, shared_session), server._data_context_key(user_b, shared_session))
        self.assertNotEqual(server._data_context_key(user_a, shared_session), server._data_context_key(user_a, other_session))

        reply_a_shared = server._comparison_period_reply([], user_a, shared_session)
        reply_b_shared = server._comparison_period_reply([], user_b, shared_session)
        reply_a_other = server._comparison_period_reply([], user_a, other_session)

        self.assertIn("PageSpeed", reply_a_shared)
        self.assertNotIn("SEO tháng", reply_a_shared)
        self.assertIn("SEO tháng 2026-06", reply_b_shared)
        self.assertIn("2026-05", reply_b_shared)
        self.assertIn("SEO tháng 2026-05", reply_a_other)
        self.assertIn("2026-04", reply_a_other)

    def test_data_context_requires_session_and_ignores_invalid_kind(self):
        self.assertIsNone(server._data_context_key("followup-hardening-user", None))

        user_id = "followup-hardening-invalid-kind-user"
        session_id = "followup-hardening-invalid-kind-session"
        key = server._data_context_key(user_id, session_id)
        self._touched_context_keys.add(key)
        server._SESSION_DATA_CONTEXT.pop(key, None)

        server._remember_data_context(user_id, None, "seo", "2026-06")
        server._remember_data_context(user_id, session_id, "ads", "2026-06")

        self.assertNotIn(key, server._SESSION_DATA_CONTEXT)

    def test_seo_session_reply_uses_previous_month_boundary(self):
        user_id = "followup-hardening-boundary-user"
        session_id = "followup-hardening-boundary-session"
        self.remember_context(user_id, session_id, "seo", "2026-01")

        reply = server._comparison_period_reply([], user_id, session_id)

        self.assertIn("SEO tháng 2026-01", reply)
        self.assertIn("2025-12", reply)
        self.assertIn("phiên chat hiện tại", reply)


if __name__ == "__main__":
    unittest.main()
