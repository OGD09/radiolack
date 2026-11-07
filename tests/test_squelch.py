"""Squelch learning: prototype rules, FFT/Gqrx latency and carrier protection."""

import unittest

from radiolack.squelch import (ACTIVE, QUIET, TRANSITION, UNKNOWN, FftActivity, SquelchLearner,
                               SquelchSettings, SquelchUpdate)

from tests.helpers import CONTEXT

CARRIER, NOISE = -46.0, -54.0


def simulate(learner, *, carrier_until, lag, end, carrier=CARRIER, noise=NOISE, guard=None):
    """FFT sees the carrier during [0, carrier_until); Gqrx's meter shows it ``lag`` s later.

    FFT frames are processed every 0.1 s and the meter is read every 0.2 s,
    as in the monitor. Returns (time, meter level, state, squelch, update) rows.
    """
    guard = learner.settings.fft_guard_s if guard is None else guard
    activity = FftActivity()
    history = []
    next_meter = 0.3
    for step in range(int(round(end * 10)) + 1):
        now = round(step / 10, 1)
        activity.update(now < carrier_until, now)
        if now + 1e-9 >= next_meter:
            seen = now - lag
            level = carrier if 0 <= seen < carrier_until else noise
            state = activity.state(now, guard)
            update = learner.observe(level, state, now)
            history.append((now, level, state, learner.record["squelch_dbfs"], update))
            next_meter = round(next_meter + 0.2, 1)
    return history


def provisional(squelch):
    return SquelchLearner(CONTEXT, squelch, clock_utc=lambda: "2026-11-07T00:00:00+00:00")


class PrototypeRuleTests(unittest.TestCase):
    def test_stable_quiet_meter_sets_squelch_three_db_above_noise(self):
        learner = provisional(-46)
        updates = [learner.observe(-60, QUIET, when) for when in [0, 0.2, 0.4, 0.6]]
        self.assertEqual(updates, [None, None, None, SquelchUpdate(-57, "noise", -60)])
        self.assertEqual(learner.record["squelch_dbfs"], -57)
        self.assertEqual(learner.record["noise_floor_dbfs"], -60)
        self.assertEqual(learner.record["source"], "noise")
        self.assertEqual(learner.record["provisional_squelch_dbfs"], -46)

    def test_squelch_is_not_learned_during_carrier_or_unstable_noise(self):
        learner = provisional(-46)
        for index in range(10):
            learner.observe(-25, ACTIVE, index / 5)
        self.assertEqual(learner.record["source"], "signal_fallback")
        for index, level in enumerate([-60, -50, -60, -50, -60, -50]):
            learner.observe(level, QUIET, index / 5 + 2)
        self.assertEqual(learner.record["source"], "signal_fallback")

    def test_learned_squelch_has_deadband_and_limited_upward_drift(self):
        record = {"squelch_dbfs": -57, "source": "noise", "receiver_context": {}}
        learner = SquelchLearner({}, -57, record)
        for when in [0, 0.2, 0.4, 0.6]:
            learner.observe(-59.5, QUIET, when)
        self.assertEqual(learner.record["squelch_dbfs"], -57)
        for index in range(8):
            learner.observe(-55, QUIET, 3 + index / 5)
        self.assertEqual(learner.record["squelch_dbfs"], -55)

    def test_lowering_the_squelch_is_immediate(self):
        record = {"squelch_dbfs": -40, "source": "noise", "receiver_context": {}}
        learner = SquelchLearner({}, -40, record)
        for when in [0, 0.2, 0.4, 0.6]:
            update = learner.observe(-60, QUIET, when)
        self.assertEqual(update.requested_dbfs, -57)
        self.assertEqual(learner.record["squelch_dbfs"], -57)

    def test_unsettled_readings_are_ignored_and_invalid_levels_rejected(self):
        learner = provisional(-46)
        for when in [0, 0.2, 0.4, 0.6, 0.8]:
            self.assertIsNone(learner.observe(-60, TRANSITION, when))
            self.assertIsNone(learner.observe(-60, UNKNOWN, when))
        self.assertEqual(learner.record["source"], "signal_fallback")
        with self.assertRaises(ValueError):
            learner.observe(float("nan"), QUIET, 1)


class LatencyTests(unittest.TestCase):
    def test_fft_activity_states_need_a_stable_guard_interval(self):
        activity = FftActivity()
        self.assertEqual(activity.state(0, 1.5), UNKNOWN)
        activity.update(True, 10.0)
        activity.update(True, 11.0)
        self.assertEqual(activity.state(11.4, 1.5), TRANSITION)
        self.assertEqual(activity.state(11.5, 1.5), ACTIVE)
        activity.update(False, 12.0)
        self.assertEqual(activity.state(13.4, 1.5), TRANSITION)
        self.assertEqual(activity.state(13.5, 1.5), QUIET)

    def test_prototype_rules_learned_the_carrier_tail_as_noise(self):
        """Documents the failure seen in the migrated history (squelch above the carrier)."""
        naive = SquelchSettings(fft_guard_s=0.0, carrier_separation_db=-1000.0,
                                carrier_headroom_db=-1000.0, max_raise_db=1000.0)
        learner = SquelchLearner(CONTEXT, -60, settings=naive)
        simulate(learner, carrier_until=3.0, lag=1.2, end=4.0)
        self.assertEqual(learner.record["noise_floor_dbfs"], CARRIER)
        self.assertGreater(learner.record["squelch_dbfs"], CARRIER, "the next visit would be muted")

    def test_fft_silent_while_meter_still_shows_the_carrier(self):
        learner = provisional(-60)
        history = simulate(learner, carrier_until=3.0, lag=1.2, end=10.0)
        updates = [(when, update) for when, _, _, _, update in history if update is not None]
        self.assertTrue(updates, "real noise must still be learned once the meter settles")
        self.assertTrue(all(when >= 4.5 and update.reason == "noise" for when, update in updates))
        self.assertEqual(learner.record["noise_floor_dbfs"], NOISE)
        self.assertLessEqual(max(squelch for *_, squelch, _ in history), CARRIER - 3)
        self.assertEqual(learner.record["carrier_meter_dbfs"], CARRIER)

    def test_lag_longer_than_the_guard_is_rejected_near_the_carrier(self):
        learner = provisional(-60)
        history = simulate(learner, carrier_until=6.0, lag=2.5, end=12.0)
        self.assertGreaterEqual(learner.rejected_near_carrier, 1)
        self.assertEqual(learner.record["noise_floor_dbfs"], NOISE)
        self.assertLessEqual(max(squelch for *_, squelch, _ in history), CARRIER - 3)

    def test_stored_carrier_level_protects_a_later_visit(self):
        saved = {"squelch_dbfs": -60.0, "source": "noise", "receiver_context": CONTEXT,
                 "carrier_meter_dbfs": -47.0}
        learner = SquelchLearner(CONTEXT, -60.0, saved)
        for when in [0, 0.2, 0.4, 0.6]:
            self.assertIsNone(learner.observe(-48.0, QUIET, when))
        self.assertEqual(learner.rejected_near_carrier, 1)
        self.assertEqual(learner.record["squelch_dbfs"], -60.0)


class CarrierGuardTests(unittest.TestCase):
    def test_memorized_squelch_above_the_carrier_is_lowered_during_the_listen(self):
        # Shape of the contaminated prototype memories (e.g. 130.475 MHz).
        saved = {"squelch_dbfs": -43.0, "noise_floor_dbfs": -45.3, "source": "noise",
                 "receiver_context": CONTEXT, "updated_at_utc": "2026-11-07T10:38:25+00:00"}
        learner = SquelchLearner(CONTEXT, -43.0, saved)
        history = simulate(learner, carrier_until=10.0, lag=0.5, end=10.0, carrier=-46.5)
        updates = [(when, update) for when, _, _, _, update in history if update is not None]
        self.assertEqual(len(updates), 1)
        when, update = updates[0]
        self.assertEqual((when, update.reason, update.requested_dbfs), (1.9, "carrier_guard", -49.5))
        self.assertIsNone(update.noise_dbfs, "a stored noise above the carrier is not credible")
        self.assertEqual(learner.record["carrier_meter_dbfs"], -46.5)
        self.assertEqual(learner.record["learning_version"], 2)
        self.assertEqual(learner.record["noise_floor_dbfs"], -45.3, "legacy estimate kept, not invented")

    def guard_updates(self, saved, carrier):
        """Carrier present for the whole visit: no quiet reading, so no noise measurement."""
        learner = SquelchLearner(CONTEXT, saved["squelch_dbfs"], saved)
        history = simulate(learner, carrier_until=10.0, lag=0.5, end=10.0, carrier=carrier)
        return learner, [update for *_, update in history if update is not None]

    def test_guard_lowers_to_a_stored_noise_clearly_below_the_carrier(self):
        # Shape of 120.775 MHz on hardware: strong busy station, noise stored but never re-measured.
        saved = {"squelch_dbfs": -28.6, "noise_floor_dbfs": -57.2, "source": "noise",
                 "receiver_context": CONTEXT, "updated_at_utc": "2026-11-07T10:41:36+00:00"}
        learner, updates = self.guard_updates(saved, carrier=-28.9)
        self.assertEqual(updates, [SquelchUpdate(-54.2, "carrier_guard", -57.2)])
        self.assertEqual(learner.record["noise_floor_dbfs"], -57.2, "the stored estimate is not rewritten")
        self.assertEqual(learner.record["carrier_meter_dbfs"], -28.9)

    def test_memory_left_at_the_carrier_ceiling_is_lowered_to_the_stored_noise(self):
        # Value written by the first version of the guard: carrier - 3 dB, far above the noise.
        saved = {"squelch_dbfs": -31.8, "noise_floor_dbfs": -57.2, "source": "carrier_guard",
                 "carrier_meter_dbfs": -28.8, "receiver_context": CONTEXT, "learning_version": 2}
        _, updates = self.guard_updates(saved, carrier=-28.9)
        self.assertEqual(updates, [SquelchUpdate(-54.2, "carrier_guard", -57.2)])

    def test_guard_leaves_a_squelch_already_near_the_stored_noise(self):
        saved = {"squelch_dbfs": -54.0, "noise_floor_dbfs": -57.2, "source": "noise",
                 "receiver_context": CONTEXT}
        learner, updates = self.guard_updates(saved, carrier=-28.9)
        self.assertEqual(updates, [], "-54.0 is within the 0.5 dB correction threshold of -54.2")
        self.assertEqual(learner.record["squelch_dbfs"], -54.0)

    def test_noise_learning_never_exceeds_the_carrier_ceiling(self):
        saved = {"squelch_dbfs": -60.0, "source": "noise", "receiver_context": CONTEXT,
                 "carrier_meter_dbfs": -50.0}
        learner = SquelchLearner(CONTEXT, -60.0, saved)
        values = []
        for step in range(60):
            update = learner.observe(-56.0, QUIET, step / 5)
            if update is not None:
                values.append(update.requested_dbfs)
        # +2 dB per update towards noise + 3 dB = -53, which equals the carrier ceiling;
        # the last 1 dB step is inside the deadband.
        self.assertEqual(values, [-58.0, -56.0, -54.0])
        self.assertLessEqual(learner.record["squelch_dbfs"], -53.0)


if __name__ == "__main__":
    unittest.main()
