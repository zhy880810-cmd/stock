#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Synthetic fixtures for Wilder ATR, split adjustment, and fail-closed verification. No network."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import update_daily as u  # noqa: E402


def bars_around_split():
    raw = []
    # 16 bars so Wilder has a seed plus one update. Split on the 10th date.
    for i in range(16):
        day = f"2026-07-{i+1:02d}"
        if i < 9:
            raw.append({"date": day, "open": 100 + i, "high": 110 + i, "low": 90 + i, "close": 100 + i})
        else:
            raw.append({"date": day, "open": 5.1, "high": 6.2, "low": 4.8, "close": 5.0 + i * 0.01})
    return raw


class WilderTests(unittest.TestCase):
    def test_seed_then_smooth(self):
        # 16 closes → 15 TRs. First 14 TRs are 2, the last TR is 16.
        closes = [10.0] + [10.0] * 14 + [10.0]
        highs = [10.0] + [12.0] * 14 + [26.0]
        lows = [10.0] + [10.0] * 14 + [10.0]
        # TR for i=1..14: max(2, |12-10|, |10-10|) = 2
        # TR for i=15: max(16, |26-10|, |10-10|) = 16
        atr = u.wilder_atr14(highs, lows, closes)
        self.assertAlmostEqual(atr, (2 * 13 + 16) / 14)

    def test_warmup_needs_fifteen_bars(self):
        self.assertIsNone(u.wilder_atr14([1, 2], [1, 1], [1, 2]))


class AdjustTests(unittest.TestCase):
    def test_forward_split_scales_entire_ohlc(self):
        raw = [
            {"date": "2026-07-14", "open": 200, "high": 220, "low": 180, "close": 200},
            {"date": "2026-07-15", "open": 10, "high": 11, "low": 9, "close": 10},
        ]
        out = u.adjust_ohlc_to_current(raw, [{"date": "2026-07-15", "ratio": 20}])
        self.assertAlmostEqual(out[0]["open"], 10)
        self.assertAlmostEqual(out[0]["high"], 11)
        self.assertAlmostEqual(out[0]["low"], 9)
        self.assertAlmostEqual(out[0]["close"], 10)
        self.assertAlmostEqual(out[1]["close"], 10)
        self.assertEqual(out[0]["factor"], out[0]["high"] and 20)

    def test_reverse_split_same_factor(self):
        raw = [
            {"date": "2025-02-07", "open": 1, "high": 1.2, "low": 0.8, "close": 1},
            {"date": "2025-02-10", "open": 10, "high": 11, "low": 9, "close": 10},
        ]
        out = u.adjust_ohlc_to_current(raw, [{"date": "2025-02-10", "ratio": 0.1}])
        self.assertAlmostEqual(out[0]["close"], 10)
        self.assertAlmostEqual(out[0]["high"], 12)
        self.assertAlmostEqual(out[1]["close"], 10)


class VerifyTests(unittest.TestCase):
    def _issuer_note(self):
        return [{"url": "https://example.test/issuer", "http": 200, "parsed": True, "events": 1}]

    def test_match_is_verified_and_gap_closes(self):
        raw = bars_around_split()
        splits = [{"date": "2026-07-10", "ratio": 20}]
        issuer = [{"date": "2026-07-10", "ratio": 20, "kind": "forward", "label": "20 for 1"}]
        audit = u.classify_adjustment("KORU", raw, splits, issuer, self._issuer_note(), quote_close=raw[-1]["close"])
        self.assertEqual(audit["status"], "verified")
        self.assertFalse(audit["hide_technical"])
        self.assertGreater(audit["atr14"], 0)
        self.assertEqual(audit["atr_method"], "wilder14")
        self.assertIn("前14个TR", audit["formula"])

    def test_empty_both_sides_is_not_verified(self):
        raw = bars_around_split()
        audit = u.classify_adjustment("SOXL", raw, [], [], self._issuer_note(), quote_close=raw[-1]["close"])
        self.assertEqual(audit["status"], "unknown")
        self.assertTrue(audit["hide_technical"])

    def test_mismatch_fails_closed(self):
        raw = bars_around_split()
        vendor = [{"date": "2026-07-10", "ratio": 10}]
        issuer = [{"date": "2026-07-10", "ratio": 20, "kind": "forward", "label": "20 for 1"}]
        audit = u.classify_adjustment("KORU", raw, vendor, issuer, self._issuer_note(), quote_close=7.68)
        self.assertEqual(audit["status"], "failed")
        self.assertTrue(audit["hide_technical"])

    def test_fetch_failure_is_pending(self):
        raw = bars_around_split()
        notes = [{"url": "https://www.direxion.com/press-release/direxion-to-split-nine-etfs", "http": None, "parsed": False, "error": "Timeout"}]
        audit = u.classify_adjustment("KORU", raw, [], [], notes, quote_close=8)
        self.assertEqual(audit["status"], "pending")
        self.assertTrue(audit["hide_technical"])

    def test_parser_requires_ticker_ratio_and_date(self):
        text = (
            "Direxion Daily MSCI South Korea Bull 3X ETF (KORU) | 20 for 1 | 1,900%\n"
            "Shares will begin trading on a split-adjusted basis on July 15, 2026.\n"
            "Some other fund | 1 for 10\n"
        )
        events = u.parse_issuer_events(text, "KORU")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-07-15")
        self.assertAlmostEqual(events[0]["ratio"], 20)
        self.assertEqual(u.parse_issuer_events("KORU moved a lot", "KORU"), [])

    def test_no_hardcoded_koru_ratio(self):
        with open(os.path.join(ROOT, "scripts", "update_daily.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("KORU_SPLIT", src)
        self.assertNotIn('"ratio": 20', src)
        self.assertNotIn("'ratio': 0.1", src)


class LayerSafetyTests(unittest.TestCase):
    def test_addon_boundary(self):
        self.assertTrue(u.addon_conflicts(4.97, 6))
        self.assertTrue(u.addon_conflicts(6, 6))
        self.assertFalse(u.addon_conflicts(45.11, 45))

    def test_unverified_hides_ref_keeps_agreed(self):
        row = {"symbol": "KORU", "price": 7.68, "fixed_sl": 6, "fixed_tp": [8.5, 10.5],
               "ref_sl": 4, "ref_tp": 15.94, "atr14": 1.53, "high_3m": 25.37}
        rs = u._resolve_sltp(row, 7.68)
        self.assertEqual(rs["sl"], 6)
        self.assertEqual(rs["sl_src"], "已定")
        self.assertIsNone(rs["ref"]["sl"])
        self.assertTrue(rs["ref"]["hidden"])

    def test_finalize_does_not_create_executable_tier(self):
        row = {"symbol": "TQQQ", "levels": [52, 50], "price": 55, "fixed_sl": 45, "fixed_tp": [60, 68],
               "kind": "levered", "atr14": 2, "low_3m": 40, "high_3m": 70, "low_20d": 48,
               "hist_audit": {"status": "verified", "hide_technical": False}}
        tech = u.compute_tech_levels(55, [52, 50], row)
        self.assertTrue(tech and tech.get("extra_tranches") is not None)
        out = u._finalize_buy_row(row, {"levels_advice": {"TQQQ": tech}}, None,
                                  {"TQQQ": {"stop_loss": 1, "take_profit": 2, "extra_tranches": [4.97]}})
        self.assertEqual(out["tranche_levels"], [52, 50])
        self.assertEqual(out["extra_tranches"], [])
        self.assertTrue(any(p.get("conflict") and p.get("price") == 4.97 for p in out["proposed_tranches"]))
        self.assertFalse(any(p.get("executable") for p in out["proposed_tranches"]))

    def test_auto_swap_does_not_mutate_plan(self):
        before = [dict(s) for s in u.PLAN_SPECS]
        entry = u.apply_auto_swap({"ticker": "AMD", "vs": "ADBE", "why": "fixture"}, "2026-11-16", {}, {})
        self.assertFalse(entry["applied"])
        self.assertEqual(u.PLAN_SPECS, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
