"""Tests for the observations: one channel for the signals found close
together, its transmissions, active time, highest margin and hours, the
channels kept and forgotten, those cleared, and what observations.json gives
back, the entries that are not valid left out."""

import unittest

from radiolack.observations import FORGET_S, GAP_S, HOURS, MAX_CHANNELS, Observations
from radiolack.scanners import MERGE_HZ, Signal

DAY_S = 86_400.0
START_S = 1_800_000_000.0       # a time.time() in 2027


def found(frequency_hz: float, margin_db: float = 20.0, width_hz: float = 600.0) -> Signal:
    """A listed signal found in a search; its time does not count."""
    return Signal(frequency_hz, width_hz, margin_db, 0.0)


def hours(**counts: int) -> list[int]:
    """The hour counters, such as hours(h9=1, h21=2)."""
    values = [0] * HOURS
    for name, count in counts.items():
        values[int(name[1:])] = count
    return values


def saved(**changes) -> dict:
    """A valid saved channel, with ``changes``."""
    return {"frequency_hz": 145_500_000, "first_s": START_S, "last_s": START_S + 60, "transmissions": 2,
            "active_s": 12.5, "margin_db": 18.0, "hours": hours(h9=2), **changes}


class RecordTest(unittest.TestCase):
    def test_a_signal_found_records_a_channel_with_one_transmission(self):
        observations = Observations()
        self.assertTrue(observations.record([found(145_500_040.4, 21.04)], START_S, 9))
        self.assertEqual(observations.describe(), [
            {"frequency_hz": 145_500_040, "first_s": START_S, "last_s": START_S, "transmissions": 1,
             "active_s": 0.0, "margin_db": 21.0, "hours": hours(h9=1)}])
        self.assertFalse(observations.record([], START_S + 1, 9))

    def test_a_transmission_lasts_while_found_again_within_the_gap(self):
        observations = Observations()
        for offset_s, margin_db in ((0.0, 15.0), (0.3, 25.0), (0.2 + GAP_S, 20.0)):
            observations.record([found(145_500_000, margin_db)], START_S + offset_s, 9)
        [channel] = observations.describe()
        self.assertEqual((channel["transmissions"], channel["active_s"], channel["margin_db"]), (1, 1.7, 25.0))
        self.assertEqual(channel["last_s"], round(START_S + 1.7))

    def test_a_channel_found_after_the_gap_begins_a_transmission_in_its_hour(self):
        observations = Observations()
        observations.record([found(145_500_000)], START_S, 9)
        observations.record([found(145_500_000)], START_S + 1.0, 9)
        observations.record([found(145_500_000)], START_S + 3_600.0, 10)
        observations.record([found(145_500_000)], START_S + 3_610.0, 10)
        [channel] = observations.describe()
        self.assertEqual((channel["transmissions"], channel["active_s"]), (3, 1.0))
        self.assertEqual(channel["hours"], hours(h9=1, h10=2))
        self.assertEqual((channel["first_s"], channel["last_s"]), (START_S, START_S + 3_610))

    def test_signals_close_together_are_one_channel_whose_frequency_follows_them(self):
        observations = Observations()
        observations.record([found(145_500_000)], START_S, 9)
        observations.record([found(145_501_000)], START_S + 0.3, 9)    # within MERGE_HZ
        observations.record([found(145_500_000 + 3 * MERGE_HZ)], START_S + 0.6, 9)     # farther: another
        self.assertEqual([channel["frequency_hz"] for channel in observations.stored()["channels"]],
                         [145_500_300, 145_504_500])

    def test_a_wide_signal_joins_a_channel_within_a_quarter_of_its_width(self):
        observations = Observations()
        observations.record([found(145_600_000, width_hz=16_000)], START_S, 9)
        observations.record([found(145_603_500, width_hz=16_000)], START_S + 0.3, 9)
        self.assertEqual(len(observations), 1)
        observations.record([found(145_603_500 + 4_100, width_hz=600)], START_S + 0.6, 9)   # narrow: another
        self.assertEqual(len(observations), 2)

    def test_two_signals_of_one_search_on_two_channels_each_count(self):
        observations = Observations()
        observations.record([found(145_400_000), found(145_500_000)], START_S, 9)
        observations.record([found(145_400_000), found(145_500_000)], START_S + 0.3, 9)
        self.assertEqual([(channel["frequency_hz"], channel["active_s"]) for channel in observations.describe()],
                         [(145_400_000, 0.3), (145_500_000, 0.3)])

    def test_a_clock_set_back_neither_begins_a_transmission_nor_adds_time(self):
        observations = Observations()
        observations.record([found(145_500_000)], START_S, 9)
        observations.record([found(145_500_000)], START_S - 600.0, 8)
        observations.record([found(145_500_000)], START_S - 599.5, 8)
        [channel] = observations.describe()
        self.assertEqual((channel["transmissions"], channel["active_s"], channel["hours"]), (1, 0.5, hours(h9=1)))
        self.assertEqual((channel["first_s"], channel["last_s"]), (START_S - 600, round(START_S - 599.5)))

    def test_the_latest_found_first_and_the_file_in_frequency_order(self):
        observations = Observations()
        observations.record([found(146_000_000)], START_S, 9)
        observations.record([found(145_000_000)], START_S + 10, 9)
        self.assertEqual([channel["frequency_hz"] for channel in observations.describe()], [145_000_000, 146_000_000])
        observations.record([found(146_000_000)], START_S + 20, 9)
        self.assertEqual([channel["frequency_hz"] for channel in observations.describe()], [146_000_000, 145_000_000])
        self.assertEqual([channel["frequency_hz"] for channel in observations.stored()["channels"]],
                         [145_000_000, 146_000_000])

    def test_the_least_recently_found_leave_beyond_the_limit(self):
        observations = Observations()
        for index in range(MAX_CHANNELS):
            observations.record([found(140_000_000 + index * 10_000)], START_S + index, 9)
        observations.record([found(140_000_000)], START_S + MAX_CHANNELS, 9)   # found again: stays
        observations.record([found(100_000_000), found(200_000_000)], START_S + MAX_CHANNELS + 1, 9)
        kept = {channel["frequency_hz"] for channel in observations.describe()}
        self.assertEqual(len(kept), MAX_CHANNELS)
        self.assertTrue({100_000_000, 200_000_000, 140_000_000} <= kept)
        self.assertFalse({140_010_000, 140_020_000} & kept)     # found the longest ago


class ForgetAndClearTest(unittest.TestCase):
    def test_channels_not_found_for_ninety_days_are_forgotten(self):
        observations = Observations()
        observations.record([found(145_000_000)], START_S, 9)
        observations.record([found(146_000_000)], START_S + DAY_S, 9)
        self.assertFalse(observations.forget(START_S + FORGET_S))
        self.assertTrue(observations.forget(START_S + FORGET_S + 1))
        self.assertEqual([channel["frequency_hz"] for channel in observations.describe()], [146_000_000])

    def test_clear_forgets_the_nearest_channel_within_the_merge_distance(self):
        observations = Observations()
        observations.record([found(145_500_000), found(145_503_000)], START_S, 9)
        self.assertFalse(observations.clear(145_500_000 - MERGE_HZ - 1))
        self.assertTrue(observations.clear(145_501_400))
        self.assertEqual([channel["frequency_hz"] for channel in observations.describe()], [145_503_000])
        self.assertFalse(observations.clear(145_501_400))
        self.assertTrue(observations.clear_all())
        self.assertEqual((observations.describe(), observations.clear_all()), ([], False))


class RestoreTest(unittest.TestCase):
    def test_the_file_gives_back_what_was_recorded(self):
        observations = Observations()
        observations.record([found(145_500_000, 21.0), found(433_500_000, 30.0)], START_S, 9)
        observations.record([found(145_500_000, 21.0)], START_S + 0.5, 9)
        restored = Observations(observations.stored(), START_S + DAY_S)
        self.assertEqual(restored.stored(), observations.stored())
        restored.record([found(145_500_000)], START_S + 1.0, 9)    # the transmission goes on
        self.assertEqual(restored.describe()[0]["transmissions"], 1)

    def test_nothing_saved_gives_no_channel(self):
        for content in ({}, {"channels": []}):
            with self.subTest(content=content):
                self.assertEqual(len(Observations(content, START_S)), 0)

    def test_saved_channels_that_are_not_valid_are_left_out(self):
        invalid = [None, [], "145500000", saved(frequency_hz=0), saved(frequency_hz=10 ** 400),
                   saved(frequency_hz=True), saved(frequency_hz="145500000"), saved(first_s=-1),
                   saved(first_s=START_S + 61), saved(last_s=float("inf")), saved(last_s=10 ** 12),
                   saved(transmissions=0), saved(transmissions=1.0), saved(transmissions=2 ** 60),
                   saved(active_s=-0.1), saved(active_s=float("nan")), saved(margin_db=-1), saved(margin_db=201),
                   saved(hours=hours()[:23]), saved(hours=[*hours()[:23], -1]), saved(hours=[*hours()[:23], 0.5]),
                   saved(hours=None), {key: value for key, value in saved().items() if key != "hours"}]
        with self.assertLogs("radiolack.observations", "WARNING") as logs:
            observations = Observations({"channels": [*invalid, saved(), saved(frequency_hz=146_000_000, extra=1)]},
                                        START_S + 60)
        self.assertIn(f"{len(invalid)} saved observations are not valid", logs.output[0])
        self.assertEqual([channel["frequency_hz"] for channel in observations.stored()["channels"]],
                         [145_500_000, 146_000_000])

    def test_saved_channels_not_found_for_ninety_days_are_forgotten(self):
        observations = Observations({"channels": [saved(), saved(frequency_hz=146_000_000, last_s=START_S + DAY_S)]},
                                    START_S + 60 + FORGET_S + 1)
        self.assertEqual([channel["frequency_hz"] for channel in observations.describe()], [146_000_000])

    def test_only_the_saved_channels_found_last_are_kept_beyond_the_limit(self):
        channels = [saved(frequency_hz=140_000_000 + index * 10_000, last_s=START_S + index)
                    for index in range(MAX_CHANNELS + 2)]
        with self.assertLogs("radiolack.observations", "WARNING"):
            observations = Observations({"channels": channels}, START_S)
        kept = [channel["frequency_hz"] for channel in observations.stored()["channels"]]
        self.assertEqual((len(kept), kept[0]), (MAX_CHANNELS, 140_020_000))

    def test_a_file_without_a_list_of_channels_gives_none(self):
        for content in ({"channels": {}}, {"channels": "none"}, []):
            with self.subTest(content=content), self.assertLogs("radiolack.observations", "WARNING"):
                self.assertEqual(len(Observations(content, START_S)), 0)


if __name__ == "__main__":
    unittest.main()
