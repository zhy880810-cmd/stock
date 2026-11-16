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


def month_bars(start, n, price, jump_on=None, jump_to=None):
    """Synthetic OHLC. Not a real market print."""
    from datetime import datetime, timedelta
    d0 = datetime.strptime(start, "%Y-%m-%d")
    out = []
    px = price
    for i in range(n):
        day = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        if jump_on and day >= jump_on:
            px = jump_to
        out.append({"date": day, "open": px, "high": px + 0.4, "low": max(px - 0.4, 0.05), "close": px,
                     "adj_close": px * 0.99})
    return out


class VerifyTests(unittest.TestCase):
    def test_synthetic_forward_split_adjusts_once(self):
        raw = month_bars("2026-07-01", 20, 100, jump_on="2026-07-15", jump_to=5)
        vendor = [{"date": "2026-07-15", "ratio": 20}]
        audit = u.review_corporate_window("KORU", raw, vendor, quote_close=5)
        self.assertEqual(audit["status"], "unknown")
        self.assertEqual(audit["components"]["splits"]["state"], "校验通过")
        self.assertEqual(audit["components"]["atr"]["state"], "未校验")
        self.assertTrue(audit["applied_splits"])
        self.assertIn("direxion.com", audit["confirmed"][0]["url"])
        long = month_bars("2026-06-01", 80, 100, jump_on="2026-07-15", jump_to=5)
        audit2 = u.review_corporate_window("KORU", long, vendor, quote_close=5)
        self.assertEqual(audit2["status"], "verified")
        self.assertIn("仅基于拆股后", audit2.get("atr_label") or "")
        self.assertGreater(audit2["post_split_sessions"], 15)

    def test_already_adjusted_is_not_divided_again(self):
        raw = month_bars("2026-07-01", 20, 5)
        vendor = [{"date": "2026-07-15", "ratio": 20}]
        audit = u.review_corporate_window("KORU", raw, vendor, quote_close=5)
        self.assertEqual(audit["applied_splits"], [])
        self.assertEqual(audit["components"]["splits"]["state"], "校验通过")
        self.assertNotEqual(audit["components"]["atr"]["state"], "校验通过")

    def test_mismatch_hides(self):
        raw = month_bars("2026-07-01", 20, 100, jump_on="2026-07-15", jump_to=5)
        audit = u.review_corporate_window("KORU", raw, [{"date": "2026-07-15", "ratio": 10}], quote_close=5)
        self.assertEqual(audit["status"], "failed")
        self.assertTrue(audit["hide_technical"])

    def test_empty_window_is_not_split_free(self):
        raw = month_bars("2026-08-01", 20, 8)
        audit = u.review_corporate_window("TQQQ", raw, [], quote_close=8)
        self.assertEqual(audit["status"], "unknown")
        self.assertTrue(audit["hide_technical"])
        self.assertIn("不能当成没有公司行为", audit["note"])
        self.assertEqual(audit["components"]["atr"]["state"], "未校验")

    def test_auto_adjust_true_refused(self):
        raw = month_bars("2026-07-01", 20, 5)
        audit = u.review_corporate_window("KORU", raw, [{"date": "2026-07-15", "ratio": 20}], quote_close=5, auto_adjust=True)
        self.assertEqual(audit["status"], "failed")
        self.assertIn("分红", audit["note"])

    def test_dividend_field_ignored(self):
        raw = month_bars("2026-07-01", 20, 5)
        only = u._ohlc_only(raw)
        self.assertNotIn("adj_close", only[0])

    def test_volatility_is_not_a_proven_split(self):
        raw = month_bars("2026-08-01", 20, 10)
        raw[10]["close"] = 14
        raw[10]["high"] = 14.2
        raw[10]["open"] = 14
        hits = u.suspicious_overnight(raw)
        self.assertEqual(hits, [])
        audit = u.review_corporate_window("SOXL", raw, [], quote_close=10)
        self.assertEqual(audit["status"], "unknown")
        self.assertNotIn("已证实", audit["note"])

    def test_unexplained_20x_hides_without_claiming_proof(self):
        raw = month_bars("2026-08-01", 20, 100, jump_on="2026-08-10", jump_to=5)
        audit = u.review_corporate_window("SOXL", raw, [], quote_close=5)
        self.assertTrue(audit["hide_technical"])
        self.assertNotEqual(audit["status"], "verified")
        self.assertTrue(audit.get("anomalies"))

    def test_parser_is_not_authority(self):
        audit = u.classify_adjustment("KORU", month_bars("2026-07-01", 20, 5), [{"date": "2026-07-15", "ratio": 20}], [], [], 5)
        self.assertEqual(audit["status"], "unknown")
        self.assertTrue(audit["hide_technical"])

    def test_confirmed_table_has_issuer_urls(self):
        urls = {e["source_url"] for e in u.CONFIRMED_ACTIONS}
        self.assertIn("https://www.direxion.com/press-release/direxion-to-split-nine-etfs", urls)
        self.assertIn("https://www.proshares.com/press-releases/proshares-announces-etf-share-splits5", urls)
        for e in u.CONFIRMED_ACTIONS:
            self.assertTrue(e["source_url"].startswith("http"))
            self.assertGreater(e["numerator"], 0)
            self.assertGreater(e["denominator"], 0)


class LayerSafetyTests(unittest.TestCase):
    def test_addon_boundary(self):
        self.assertTrue(u.addon_conflicts(4.97, 6))
        self.assertTrue(u.addon_conflicts(6, 6))
        self.assertFalse(u.addon_conflicts(45.11, 45))

    def test_ordinary_stock_not_gated(self):
        row = {"symbol": "NVDA", "price": 140, "ref_sl": 120, "ref_tp": 180, "atr14": 5}
        self.assertTrue(u.technical_allowed(row))
        rs = u._resolve_sltp(row, 140)
        self.assertEqual(rs["ref"]["sl"], 120)
        self.assertIsNone(rs["sl"])
        self.assertFalse(rs["ref"].get("hidden"))
        etf = {"symbol": "TQQQ", "price": 55, "ref_sl": 40, "ref_tp": 70, "fixed_sl": 45, "fixed_tp": [60]}
        self.assertFalse(u.technical_allowed(etf))
        rs2 = u._resolve_sltp(etf, 55)
        self.assertIsNone(rs2["ref"]["sl"])
        self.assertEqual(rs2["sl"], 45)

    def test_post_split_seed_excludes_d0_without_unified_prev(self):
        bars = []
        for i in range(16):
            px = 10 + i * 0.1
            bars.append({"date": f"2026-08-{i+1:02d}", "open": px, "high": px + 1, "low": px - 0.5, "close": px})
        atr_ex, n, note = u.wilder_post_split(bars, bars[0]["date"], prev_unified=False)
        self.assertIsNotNone(atr_ex)
        self.assertIn("仅基于拆股后", note)
        short = bars[:10]
        atr_short, _, note_s = u.wilder_post_split(short, short[0]["date"], prev_unified=False)
        self.assertIsNone(atr_short)
        self.assertIn("D0", note_s)
        atr_in, _, _ = u.wilder_post_split(bars[:14], bars[0]["date"], prev_close=9, prev_unified=True)
        self.assertIsNotNone(atr_in)

    def test_stale_coverage_stays_unchecked(self):
        raw = month_bars("2026-08-01", 20, 8)
        audit = u.review_corporate_window("KORU", raw, [], quote_close=8)
        self.assertEqual(audit["status"], "unknown")
        self.assertFalse(u.ACTION_COVERAGE["maintained_current"])
        fresh = {
            "maintained_current": True,
            "symbols": {"KORU": {"complete_through": "2026-08-31"}},
        }
        audit2 = u.review_corporate_window("KORU", raw, [], quote_close=8, coverage=fresh, table=[])
        self.assertEqual(audit2["status"], "verified")
        self.assertIn("仅基于拆股后", audit2.get("atr_label") or audit2.get("note"))

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
        self.assertEqual(tech.get("extra_tranches"), [])
        out = u._finalize_buy_row(row, {"levels_advice": {"TQQQ": tech}}, None,
                                  {"TQQQ": {"stop_loss": 1, "take_profit": 2, "extra_tranches": [4.97, 45.11]}})
        self.assertEqual(out["tranche_levels"], [52, 50])
        self.assertEqual(out["extra_tranches"], [])
        self.assertEqual(out["proposed_tranches"], [])

    def test_auto_swap_does_not_mutate_plan(self):
        before = [dict(s) for s in u.PLAN_SPECS]
        entry = u.apply_auto_swap({"ticker": "AMD", "vs": "ADBE", "why": "fixture"}, "2026-11-16", {}, {})
        self.assertFalse(entry["applied"])
        self.assertEqual(u.PLAN_SPECS, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
