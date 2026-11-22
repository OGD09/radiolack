"""Checks of the names that the user gives the devices (devicenames.py)."""

import unittest

from radiolack.devicenames import NAME_MAX, NAMES_MAX, checked_name, device_key, renamed, saved_names


class DeviceNamesTest(unittest.TestCase):
    def test_a_device_is_known_by_its_kind_and_the_receiver_it_reads(self):
        self.assertEqual(device_key({"kind": "rtl_sdr", "device": "RTLSDR1"}), "rtl_sdr:RTLSDR1")
        self.assertEqual(device_key({"kind": "soapy", "device": "hackrf 0001"}), "soapy:hackrf 0001")
        self.assertEqual(device_key({"kind": "synthetic", "device": None}), "synthetic:")

    def test_a_name_is_a_short_text_without_control_characters(self):
        self.assertEqual(checked_name("  V3 ADS-B "), "V3 ADS-B")
        self.assertEqual(checked_name("Nooelec 2 m \N{EM DASH} toit"), "Nooelec 2 m \N{EM DASH} toit")
        self.assertEqual(checked_name(""), "")
        self.assertEqual(checked_name("x" * NAME_MAX), "x" * NAME_MAX)
        for refused in ("x" * (NAME_MAX + 1), "two\nlines", "tab\there", "bell\a", "right\N{RIGHT-TO-LEFT OVERRIDE}left",
                        None, 3, ["V3"]):
            with self.subTest(refused=refused), self.assertRaises(ValueError):
                checked_name(refused)

    def test_the_saved_names_leave_out_what_is_not_a_name(self):
        saved = {"rtl_sdr:RTLSDR1": "V3 ADS-B", "soapy:hackrf 0001": " HackRF ", "rtl_sdr:A": "", "rtl_sdr:B": "a\nb",
                 "rtl_sdr:C": 3, "no colon": "X", "": "Y", "rtl_sdr:" + "k" * 300: "Z"}
        self.assertEqual(saved_names(saved), {"rtl_sdr:RTLSDR1": "V3 ADS-B", "soapy:hackrf 0001": "HackRF"})
        for unreadable in (None, [], "names", 3):
            self.assertEqual(saved_names(unreadable), {})
        many = {f"rtl_sdr:{index}": f"R{index}" for index in range(NAMES_MAX + 5)}
        self.assertEqual(list(saved_names(many)), [f"rtl_sdr:{index}" for index in range(5, NAMES_MAX + 5)])

    def test_renaming_puts_the_name_last_and_an_empty_one_forgets_it(self):
        names = {"rtl_sdr:A": "First", "rtl_sdr:B": "Second"}
        self.assertEqual(list(renamed(names, "rtl_sdr:A", "Again").items()),
                         [("rtl_sdr:B", "Second"), ("rtl_sdr:A", "Again")])
        self.assertEqual(renamed(names, "rtl_sdr:A", ""), {"rtl_sdr:B": "Second"})
        self.assertEqual(names, {"rtl_sdr:A": "First", "rtl_sdr:B": "Second"})      # unchanged
        full = {f"rtl_sdr:{index}": "R" for index in range(NAMES_MAX)}
        self.assertEqual(len(renamed(full, "rtl_sdr:new", "New")), NAMES_MAX)
        self.assertNotIn("rtl_sdr:0", renamed(full, "rtl_sdr:new", "New"))


if __name__ == "__main__":
    unittest.main()
