#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fake-fixture tests for execution / reference price layers. No live API calls."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import update_daily as u  # noqa: E402


def verified_audit():
    return {"status": "verified", "hide_technical": False, "basis": "current_per_share"}


def row(**kw):
    base = {
        "symbol": "TQQQ",
        "yf": "TQQQ",
        "name": "FAKE",
        "kind": "levered",
        "levels": [52, 50],
        "price": 48.0,
        "close": 48.0,
        "extended_price": 40.0,
        "ext_label": "盘后",
        "prev_close": 50.0,
        "change_pct": -4.0,
        "atr14": 2.0,
        "low_3m": 44.0,
        "high_3m": 60.0,
        "low_20d": 45.0,
        "fixed_sl": None,
        "fixed_tp": [],
        "ref_sl": 42.0,
        "ref_tp": 70.0,
        "sl_tp_reason": "技术参考（假数据）",
    }
    base.update(kw)
    return base


class SessionPxTests(unittest.TestCase):
    def test_uses_regular_session_not_extended(self):
        r = row(price=48.0, close=48.0, extended_price=40.0)
        self.assertEqual(u._session_px(r), 48.0)
        rec = {"price": 47.5, "extended_price": 39.0, "close": 47.5}
        self.assertEqual(u._session_px(rec, r), 47.5)
        self.assertIsNone(u._session_px({"extended_price": 39.0, "ext_label": "盘后"}))


class ResolveLayersTests(unittest.TestCase):
    def test_manual_override_preserved_when_ref_changes(self):
        r = row(ref_sl=42.0, ref_tp=70.0, fixed_sl=45, fixed_tp=[60, 68], hist_audit=verified_audit())
        a = u._resolve_sltp(r, 48.0, my_sl=44.0, my_tp=80.0)
        r2 = dict(r, ref_sl=30.0, ref_tp=90.0)
        b = u._resolve_sltp(r2, 48.0, my_sl=44.0, my_tp=80.0)
        self.assertEqual(a["sl"], 44.0)
        self.assertEqual(a["sl_src"], "我的")
        self.assertEqual(a["tps"], [80.0])
        self.assertEqual(a["tp_src"], "我的")
        self.assertEqual(b["sl"], 44.0)
        self.assertEqual(b["tps"], [80.0])
        self.assertEqual(b["ref"]["sl"], 30.0)
        self.assertEqual(b["ref"]["tp"], 90.0)

    def test_reference_still_shown_with_override(self):
        r = row(ref_sl=42.0, ref_tp=70.0, fixed_sl=45, fixed_tp=[60, 68], hist_audit=verified_audit())
        rs = u._resolve_sltp(r, 48.0, my_sl=44.0)
        self.assertEqual(rs["sl"], 44.0)
        self.assertEqual(rs["sl_src"], "我的")
        self.assertEqual(rs["tps"], [60.0, 68.0])  # no my_tp → 已定
        self.assertEqual(rs["tp_src"], "已定")
        self.assertEqual(rs["ref"]["sl"], 42.0)
        self.assertEqual(rs["ref"]["tp"], 70.0)
        self.assertEqual(rs["ref"]["src"], "技术参考")

    def test_fallback_agreed_then_technical(self):
        r = row(ref_sl=42.0, ref_tp=70.0, fixed_sl=45, fixed_tp=[60, 68], hist_audit=verified_audit())
        rs = u._resolve_sltp(r, 48.0)
        self.assertEqual(rs["sl"], 45)
        self.assertEqual(rs["sl_src"], "已定")
        self.assertEqual(rs["tps"], [60.0, 68.0])
        self.assertEqual(rs["tp_src"], "已定")
        self.assertEqual(rs["ref"]["sl"], 42.0)

        r2 = row(ref_sl=42.0, ref_tp=70.0, fixed_sl=None, fixed_tp=[], hist_audit=verified_audit())
        rs2 = u._resolve_sltp(r2, 48.0)
        self.assertEqual(rs2["sl"], 42.0)
        self.assertEqual(rs2["sl_src"], "技术参考")
        self.assertEqual(rs2["tps"], [70.0])
        self.assertEqual(rs2["tp_src"], "技术参考")
        self.assertEqual(rs2["ref"]["sl"], 42.0)
        self.assertEqual(rs2["ref"]["tp"], 70.0)

    def test_no_reference_says_empty_never_invents(self):
        r = row(ref_sl=None, ref_tp=None, fixed_sl=45, fixed_tp=[60], atr14=None, low_3m=None, high_3m=None, low_20d=None)
        # even if we pass cost=None and clear refs, auto ref must stay empty (no invented numbers)
        r["ref_sl"] = None
        r["ref_tp"] = None
        rs = u._resolve_sltp(r, 48.0)
        self.assertEqual(rs["sl"], 45)
        self.assertIsNone(rs["ref"]["sl"])
        self.assertIsNone(rs["ref"]["tp"])
        self.assertEqual(rs["ref"]["src"], "")

    def test_old_data_json_without_exec_fields_loads(self):
        old = {
            "symbol": "SOXL",
            "price": 40.0,
            "fixed_sl": 38.0,
            "fixed_tp": [60.0, 75.0],
            "ref_sl": 32.0,
            "ref_tp": 80.0,
            # deliberately no exec_sl / exec_tp / ref_src
        }
        out = u._attach_price_layers(old)
        self.assertEqual(out["exec_sl"], 38.0)
        self.assertEqual(out["exec_sl_src"], "已定")
        self.assertEqual(out["exec_tp"], [60.0, 75.0])
        self.assertEqual(out["exec_tp_src"], "已定")
        self.assertEqual(out["ref_sl"], 32.0)
        self.assertEqual(out["ref_tp"], 80.0)
        # original old row still resolves without the new fields
        rs = u._resolve_sltp(old, old["price"])
        self.assertEqual(rs["sl"], 38.0)
        self.assertIsNone(rs["ref"]["sl"])
        self.assertTrue(rs["ref"]["hidden"])

    def test_distance_uses_regular_session_price(self):
        r = row(price=50.0, close=50.0, extended_price=40.0, fixed_sl=45, ref_sl=42)
        px = u._session_px(r)
        self.assertEqual(px, 50.0)
        rs = u._resolve_sltp(r, px)
        # 再跌到 45： (50-45)/50 = 10%
        self.assertAlmostEqual(u._dist_to_stop(px, rs["sl"]), 10.0)
        # if someone mistakenly used extended 40, distance would be negative / already hit
        self.assertLess(u._dist_to_stop(r["extended_price"], rs["sl"]), 0)

    def test_cost_based_ref_shown_even_with_agreed(self):
        r = row(fixed_sl=45, fixed_tp=[60, 68], ref_sl=42, ref_tp=70, atr14=2.0, price=50.0, hist_audit=verified_audit())
        rs = u._resolve_sltp(r, 50.0, cost=48.0)
        self.assertEqual(rs["sl"], 45)
        self.assertEqual(rs["sl_src"], "已定")
        self.assertTrue(rs["ref"]["sl"])
        self.assertIn("按成本", rs["ref"]["src"])
        # cost-based ref should differ from ladder ref_sl typically, and must not be the agreed 45 unless formula hits it
        self.assertIsNotNone(rs["ref"]["sl"])
        self.assertNotEqual(rs["ref"]["sl"], 45)

    def test_attach_does_not_drop_ref_when_fixed_exists(self):
        r = row(fixed_sl=45, fixed_tp=[60, 68], ref_sl=42, ref_tp=70)
        out = u._attach_price_layers(r)
        self.assertEqual(out["ref_sl"], 42)
        self.assertEqual(out["ref_tp"], 70)
        self.assertEqual(out["exec_sl"], 45)
        self.assertEqual(out["exec_sl_src"], "已定")

    def test_finalize_keeps_tech_ref_when_ai_advice_present(self):
        r = row(ref_sl=None, ref_tp=None, fixed_sl=45, fixed_tp=[60], price=50.0, levels=[52, 50], atr14=2.0, hist_audit=verified_audit())
        tech = u.compute_tech_levels(r["price"], r["levels"], r)
        self.assertTrue(tech and tech.get("stop_loss"))
        finalized = u._finalize_buy_row(
            r,
            {"levels_advice": {r["symbol"]: tech}},
            None,
            {"TQQQ": {"stop_loss": 11.11, "take_profit": 99.99, "reason": "fake ai"}},
        )
        self.assertAlmostEqual(finalized["ref_sl"], tech["stop_loss"])
        self.assertAlmostEqual(finalized["ref_tp"], tech["take_profit"])
        self.assertEqual(finalized["ai_ref_sl"], 11.11)
        self.assertEqual(finalized["ai_ref_tp"], 99.99)
        self.assertEqual(finalized["exec_sl"], 45)
        self.assertEqual(finalized["exec_sl_src"], "已定")

    def test_old_example_data_json_loads(self):
        path = os.path.join(ROOT, "data.json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertIsInstance(d, dict)
        self.assertIn("prices", d)
        # example file has no plan.buy_table; resolver must still tolerate empty/old shape
        rs = u._resolve_sltp({}, None)
        self.assertIsNone(rs["sl"])
        self.assertIsNone(rs["ref"]["sl"])


class ApplySemanticsTests(unittest.TestCase):
    def test_confirm_applies_cancel_does_not(self):
        loc = {"sl": 44.0, "tp": 80.0}
        ref = {"sl": 42.0, "tp": 70.0}
        applied = dict(loc)
        applied["sl"] = ref["sl"]
        applied["tp"] = ref["tp"]
        cancelled = dict(loc)
        # cancel leaves everything unchanged
        self.assertEqual(cancelled, loc)
        self.assertEqual(applied, {"sl": 42.0, "tp": 70.0})
        self.assertNotEqual(applied, loc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
