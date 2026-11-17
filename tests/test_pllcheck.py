"""Tests for the check of the driver's computation (pllcheck.py): the
centres it chooses, and what it names from the carrier's moves, whatever
the crystal; and the receiver's side of it (rtltcp.py), the request and
the centres off the grid."""

import unittest

from radiolack import pllcheck, r82xx
from radiolack.pllcheck import BOTH, MAX_MOVE_HZ, NEITHER, SEPARATION_HZ, Point, choose_points, identify
from radiolack.rtltcp import TUNERS, RtlTcpSource

RATE_HZ = 2_400_000
IF_HZ = r82xx.rate_if_hz(RATE_HZ)
# One centre of the grid for each divider of the tuner at 2.4 MS/s, from 64 to 2.
GRID_REQUESTS_HZ = tuple(r82xx.on_grid(f, IF_HZ, 24e6, 1766e6) for f in
                         (30_000_000, 70_000_000, 145_000_000, 435_000_000, 860_000_000, 1_296_000_000))


def as_measured(computation: str, points: tuple[Point, ...], constant_hz: float = 0.0) -> list[float]:
    """Where a carrier measures at each point, from the label of its samples
    taken as the request, when the tuner follows ``computation``: the
    carrier's frequency, ``constant_hz``, which a crystal's error moves
    alike at every point, less where the tuner truly sits."""
    return [constant_hz - r82xx.tuned_hz(computation, point.request_hz, RATE_HZ)
            for point in points]


class ChoosePointsTest(unittest.TestCase):

    def test_chooses_centres_near_the_centre_in_use_where_the_computations_differ(self):
        for request in GRID_REQUESTS_HZ:
            with self.subTest(request=request):
                points = choose_points(request, RATE_HZ, lambda hz: float(hz + 7))
                self.assertEqual(len(points), pllcheck.MAX_POINTS)
                separations = []
                for point in points:
                    self.assertLessEqual(abs(point.request_hz - request), MAX_MOVE_HZ)
                    self.assertNotEqual(point.request_hz, request)
                    self.assertEqual(point.centre_hz, point.request_hz + 7)     # as the receiver gives it
                    separations.append(pllcheck.separation_hz(RATE_HZ, request, point.request_hz))
                self.assertTrue(all(abs(separation) >= SEPARATION_HZ for separation in separations))
                self.assertEqual(len(set(point.request_hz for point in points)), len(points))

    def test_takes_both_signs_of_the_difference_where_there_are(self):
        points = choose_points(GRID_REQUESTS_HZ[2], RATE_HZ, float)      # at 2 m
        signs = {pllcheck.separation_hz(RATE_HZ, GRID_REQUESTS_HZ[2], point.request_hz) > 0 for point in points}
        self.assertEqual(signs, {True, False})

    def test_skips_what_the_receiver_cannot_ask_and_gives_none_below_two(self):
        request = GRID_REQUESTS_HZ[2]
        points = choose_points(request, RATE_HZ, lambda hz: None if hz < request else float(hz))
        self.assertTrue(points)
        self.assertTrue(all(point.request_hz > request for point in points))
        self.assertEqual(choose_points(request, RATE_HZ, lambda hz: None), ())
        only = choose_points(request, RATE_HZ, float)[0].request_hz
        self.assertEqual(choose_points(request, RATE_HZ, lambda hz: float(hz) if hz == only else None), ())

    def test_gives_none_where_the_pll_cannot_lock(self):
        self.assertEqual(choose_points(24_000_000, RATE_HZ, float), ())      # an oscillator below 27.66 MHz


class IdentifyTest(unittest.TestCase):

    def test_names_the_computation_the_moves_follow_whatever_the_constant(self):
        for request in GRID_REQUESTS_HZ:
            points = (Point(0.0, request), *choose_points(request, RATE_HZ, float))
            for computation in pllcheck.COMPUTATIONS:
                for constant_hz in (0.0, 5_426.3, -7_000.1):   # a crystal 37 ppm off at 145 MHz, and others
                    with self.subTest(request=request, computation=computation, constant_hz=constant_hz):
                        offsets = as_measured(computation, points, constant_hz)
                        found = identify(RATE_HZ, points, offsets)
                        self.assertEqual((found.computation, found.reason), (computation, None))
                        self.assertTrue(all(abs(value) < 1e-6 for value in found.residuals_hz[computation]))

    def test_holds_within_the_tolerance_and_not_beyond(self):
        request = GRID_REQUESTS_HZ[2]
        points = (Point(0.0, request), *choose_points(request, RATE_HZ, float))
        offsets = as_measured("truncating", points)
        nudged = [offsets[0], offsets[1] + 4.9, *offsets[2:]]
        self.assertEqual(identify(RATE_HZ, points, nudged).computation, "truncating")
        nudged = [offsets[0], offsets[1] + 5.1, *offsets[2:]]
        found = identify(RATE_HZ, points, nudged)
        self.assertEqual((found.computation, found.reason), (None, NEITHER))

    def test_an_exact_tuner_follows_neither(self):
        request = GRID_REQUESTS_HZ[2]
        points = (Point(0.0, request), *choose_points(request, RATE_HZ, float))
        found = identify(RATE_HZ, points, [12.5 - point.request_hz for point in points])
        self.assertEqual((found.computation, found.reason), (None, NEITHER))

    def test_both_when_the_points_cannot_tell_them_apart(self):
        # Points that the check never chooses: where both computations agree.
        request = GRID_REQUESTS_HZ[2]
        points = (Point(0.0, request), Point(0.0, request + r82xx.GRID_HZ), Point(0.0, request - r82xx.GRID_HZ))
        found = identify(RATE_HZ, points, as_measured("rounding", points))
        self.assertEqual((found.computation, found.reason), (None, BOTH))

    def test_refuses_measures_that_do_not_match_the_points(self):
        points = (Point(0.0, GRID_REQUESTS_HZ[2]),) * 3
        for offsets in ([0.0, 0.0], [0.0] * 4):
            with self.assertRaises(ValueError):
                identify(RATE_HZ, points, offsets)
        with self.assertRaises(ValueError):
            identify(RATE_HZ, points[:2], [0.0, 0.0])


class ReceiverSideTest(unittest.TestCase):
    """What the source gives the check, for a tuner as rtl_tcp announced it."""

    @staticmethod
    def source(tuner_type: int = 5, **changes) -> RtlTcpSource:
        source = RtlTcpSource(sample_rate_hz=float(RATE_HZ))
        source.tuner = TUNERS[tuner_type]
        source.configure(changes)
        return source

    def test_the_request_on_the_grid_and_the_rate(self):
        source = self.source(centre_hz=145_000_000.0, ppm=-23.45)
        request, rate = source.pll_tuning()
        self.assertEqual(rate, RATE_HZ)
        self.assertEqual((request + IF_HZ) % r82xx.GRID_HZ, 0)

    def test_why_not_elsewhere(self):
        self.assertEqual(RtlTcpSource().pll_tuning(), "receiver")      # the tuner not known yet
        self.assertEqual(self.source(1).pll_tuning(), "receiver")      # an Elonics E4000
        self.assertEqual(self.source(hf_input="q", centre_hz=7_100_000.0).pll_tuning(), "direct")
        self.assertEqual(self.source(6, hf_input="tuner", centre_hz=20_000_000.0).pll_tuning(), "range")
        self.assertEqual(self.source(5, hf_input="tuner", centre_hz=20_000_000.0).pll_tuning(), "range")

    def test_the_centre_off_the_grid_asks_for_the_request(self):
        converters = ({}, {"converter": "up", "converter_lo_hz": 125_000_000.0},
                      {"converter": "down_inverted", "converter_lo_hz": 1_000_000_000.0})
        for ppm in (0.0, 37.42, -99.99):
            for converter in converters:
                with self.subTest(ppm=ppm, converter=converter):
                    source = self.source(ppm=ppm, **converter)
                    request = source.pll_tuning()[0]
                    found = 0
                    targets = range(request - MAX_MOVE_HZ, request + MAX_MOVE_HZ + 1, 50)
                    for target in targets:
                        centre = source.centre_off_grid(target)
                        if centre is None:
                            # The correction scales the request: one hertz in 1/ppm is never asked.
                            continue
                        found += 1
                        self.assertEqual(centre, round(centre))
                        source.configure({"tuner_grid": False, "centre_hz": centre})
                        self.assertEqual(source.pll_tuning()[0], target)
                        source.configure({"tuner_grid": True})
                    self.assertGreaterEqual(found, len(targets) - 2)

    def test_no_centre_off_the_grid_where_the_tuner_is_not_computed(self):
        self.assertIsNone(self.source(1).centre_off_grid(145_000_000))
        self.assertIsNone(self.source(centre_hz=145_000_000.0).centre_off_grid(23_000_000))    # below the range
        self.assertIsNone(self.source(6, hf_input="tuner", centre_hz=30_000_000.0).centre_off_grid(28_000_000))


if __name__ == "__main__":
    unittest.main()
