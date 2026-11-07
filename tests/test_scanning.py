"""Window planning, coverage (edges and DC gaps) and confirmed window moves."""

from fractions import Fraction
import unittest

import numpy as np

from radiolack.detection import ChannelGrid
from radiolack.errors import GqrxError
from radiolack.receiver import Frame
from radiolack.scanning import BandExplorer, uncovered_channels, window_centers

from tests.helpers import GRID


def brute_force_missing(carriers, centers, rate):
    """Independent check, as in the prototype: usable bins are 3 kHz < |offset| < 0.32 rate."""
    carriers = np.asarray(carriers)[:, None]
    distance = np.abs(carriers - np.asarray(centers)[None, :])
    covered = ((distance > 3000) & (distance < rate * 0.32)).any(axis=1)
    return carriers[~covered, 0].tolist()


class WindowPlanTests(unittest.TestCase):
    def test_historical_plan_is_unchanged(self):
        centers = window_centers(2400000, 118000000, 137000000)
        self.assertEqual(len(centers), 32)
        self.assertEqual(centers[:2], [118240000, 118840000])
        self.assertEqual(centers[-1], 136760000)

    def test_full_airband_grid_is_covered_including_edges_and_dc_gaps(self):
        carriers = [118000000 + round(index * 25000 / 3) for index in range(2280)]
        self.assertEqual(carriers, GRID.channels())
        for rate in [1024000, 1800000, 2048000, 2400000]:
            for receiver_offset in [-0.15, 0.072, 0.15]:
                centers = [focus + round(rate * receiver_offset)
                           for focus in window_centers(rate, 118000000, 137000000)]
                self.assertEqual(brute_force_missing(carriers, centers, rate), [], (rate, receiver_offset))
            self.assertEqual(uncovered_channels(GRID, rate, window_centers(rate, 118000000, 137000000)), [])

    def test_narrow_band_uses_two_windows_so_dc_gaps_are_covered(self):
        grid = ChannelGrid(146400000, 146600000, 146400000, Fraction(12500), 3000)
        centers = window_centers(2400000, grid.low_hz, grid.high_hz)
        self.assertEqual(len(centers), 2)
        self.assertEqual(uncovered_channels(grid, 2400000, centers), [])

    def test_invalid_plan_inputs(self):
        with self.assertRaises(ValueError):
            window_centers(0, 118000000, 137000000)


class ExplorerTests(unittest.TestCase):
    def frame(self, center, epoch, at, rate=2400000):
        return Frame(1, epoch, center, rate, at, b"")

    def test_move_is_confirmed_only_by_a_new_hardware_center_after_settling(self):
        explorer = BandExplorer(GRID, 1.0)
        explorer.sync_rate(2400000)
        self.assertTrue(explorer.due(0))
        self.assertEqual(explorer.next_focus(), 118240000)
        explorer.mark_requested(epoch=1, now=10.0)
        self.assertEqual(explorer.cycle, 1)
        # Same epoch: only Gqrx's cursor moved, the hardware did not.
        self.assertFalse(explorer.confirm(self.frame(118240000, 1, 10.5), 1, 10.5))
        # New epoch but samples captured before settling.
        self.assertFalse(explorer.confirm(self.frame(118240000, 2, 10.2), 2, 10.2))
        # New epoch, settled, but the hardware landed far from the focus.
        self.assertFalse(explorer.confirm(self.frame(119500000, 2, 10.5), 2, 10.5))
        self.assertTrue(explorer.confirm(self.frame(118412800, 2, 10.5), 2, 10.5))
        self.assertEqual(explorer.scan_info()["window_number"], 1)
        self.assertFalse(explorer.due(11.0))
        self.assertTrue(explorer.due(11.6))

    def test_listen_advances_to_the_next_window_and_the_plan_wraps(self):
        explorer = BandExplorer(GRID, 1.0)
        explorer.sync_rate(2400000)
        for index in range(32):
            explorer.mark_requested(epoch=index, now=index)
            self.assertTrue(explorer.confirm(self.frame(explorer.pending.focus, index + 1, index + 1),
                                             index + 1, index + 1))
            explorer.after_listen()
            self.assertTrue(explorer.due(index + 1))
        self.assertEqual(explorer.next_focus(), 118240000)
        explorer.mark_requested(epoch=99, now=99)
        self.assertEqual(explorer.cycle, 2)

    def test_unconfirmed_move_times_out_with_an_explanation(self):
        explorer = BandExplorer(GRID, 1.0)
        explorer.sync_rate(2400000)
        explorer.mark_requested(epoch=1, now=0.0)
        explorer.check_timeout(5.9)
        with self.assertRaisesRegex(GqrxError, "did not move the receiver"):
            explorer.check_timeout(6.1)

    def test_rate_change_replans_and_disconnect_resets(self):
        explorer = BandExplorer(GRID, 1.0)
        explorer.sync_rate(2400000)
        explorer.mark_requested(epoch=1, now=0.0)
        explorer.reset()
        self.assertIsNone(explorer.pending)
        self.assertTrue(explorer.due(0.1))
        explorer.sync_rate(1024000)
        self.assertEqual(explorer.next_focus(), 118000000 + round(1024000 * 0.1))


if __name__ == "__main__":
    unittest.main()
