"""Offline report acceptance checks; no bot imports, storage writes or network."""
import unittest
from datetime import datetime
from report_direct_response import MOSCOW, render, summarize


def record(*events, **state):
    return {"state": {"segment": "new", **state}, "events": [
        {"event": event, "at": f"2026-11-22T{hour:02d}:00:00+03:00", **({"value": value} if value else {})}
        for event, hour, value in events]}


class ReportTests(unittest.TestCase):
    def test_starters_without_offer_are_not_lost(self):
        data = {"1": record(("start", 9, None)),
                "2": record(("start", 9, None), ("offer", 10, None), ("phone", 11, None))}
        result = summarize(data)
        self.assertEqual(result["start_cohort"], {"started": 2, "offered": 1, "phone": 1})
        self.assertEqual(result["counts"]["offered"], 1)

    def test_unique_users_not_event_count(self):
        rec = record(("offer", 10, None), ("offer", 11, None), ("phone", 12, None))
        self.assertEqual(summarize({"1": rec})["counts"]["offered"], 1)
        self.assertEqual(summarize({"1": rec})["counts"]["phone"], 1)

    def test_member_and_test_excluded(self):
        base = (("offer", 10, None), ("phone", 11, None))
        data = {"1": record(*base, segment="member"), "2": record(*base, test=True),
                "3": record(*base, ("member", 12, None))}
        self.assertEqual(summarize(data)["counts"]["offered"], 0)

    def test_format_default_is_not_a_selection(self):
        data = {"1": record(("offer", 10, None), fmt="training")}
        self.assertEqual(summarize(data)["counts"]["training"], 0)

    def test_selected_tour_and_booking(self):
        data = {"1": record(("offer", 10, None), ("phone", 11, None),
                            ("format", 12, "tour"), ("booking", 13, None),
                            ("confirmed", 14, None), fmt="tour", direct_selected_format=True)}
        result = summarize(data)
        self.assertEqual(result["by_format"]["tour"], {"selected": 1, "booked": 1, "confirmed": 1})
        self.assertNotIn("attended", result["counts"])

    def test_followup_order_is_not_causal(self):
        data = {"1": record(("offer", 10, None), ("followup", 12, None),
                            ("phone", 13, None), ("booking", 14, None)),
                "2": record(("offer", 10, None), ("phone", 11, None), ("followup", 12, None))}
        counts = summarize(data)["counts"]
        self.assertEqual(counts["followup"], 2)
        self.assertEqual(counts["phone_after_followup"], 1)
        self.assertEqual(counts["booked_after_followup"], 1)

    def test_cohort_uses_first_offer(self):
        data = {"1": record(("offer", 10, None), ("offer", 14, None), ("phone", 15, None))}
        since = datetime(2026, 11, 22, 12, tzinfo=MOSCOW)
        self.assertEqual(summarize(data, since=since)["counts"]["offered"], 0)

    def test_malformed_entries_and_empty_rates(self):
        result = summarize({"1": None, "2": {"events": [None, {"event": "offer", "at": "bad"}]}})
        text = render(result)
        self.assertIn("нет данных", text)
        self.assertNotIn("nan", text)
        self.assertIn("Визиты и покупки: не определяются", text)


if __name__ == "__main__":
    unittest.main()
