import unittest

from validate_mexc_freshness import WireComparison, classify


class MexcAnalysisTests(unittest.TestCase):
    def test_age_at_arrival_and_age_at_sampling_are_distinct(self):
        q = dict(connection_state="streaming", quality="stale", source_timestamp_ms=1000)
        cases = [
            (1000, 4001, "old_receipt"),
            (4000, 4001, "recent_receipt_source_expired"),
            (4001, 7001, "recent_receipt_late_on_arrival"),
            (4001, 7002, "old_receipt_and_late_on_arrival"),
        ]
        for received, evaluated, expected in cases:
            with self.subTest(received=received, evaluated=evaluated):
                self.assertEqual(classify(dict(q, received_at_ms=received), evaluated), expected)
        self.assertEqual(classify(None, 5000), "absent")
        self.assertEqual(classify(dict(q, connection_state="reconnecting"), 5000), "disconnected")

    def test_wire_window_excludes_future_arrivals_and_counts_each_sample_once(self):
        wire = WireComparison(dict(start_ms=0, finish_ms=40000, frames={
            "TRX": [[21000, 21000, 1], [25000, 1000, 2]],
        }))
        quote = dict(source_timestamp_ms=1000)
        for stamp in (19000, 20999, 24000, 26000, 28001, 40001):
            wire.observe("TRX", quote, stamp, "stale")
        self.assertEqual(wire.counts["TRX"], dict(samples=4, monitor_gap_no_wire_frame=2,
                         monitor_gap_newer_fresh_wire_time=1, monitor_gap_other_wire_frame=1))

    def test_backwards_wire_clock_rejected(self):
        with self.assertRaisesRegex(ValueError, "clock moved backwards"):
            WireComparison(dict(start_ms=0, finish_ms=40000,
                                frames={"TRX": [[21000, 21000, 1], [19000, 22000, 2]]}))
