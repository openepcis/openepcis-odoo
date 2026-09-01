# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Minting SSCCs: the arithmetic, and the one thing that must never happen."""

import unittest

from ..utils import sscc


class TestSscc(unittest.TestCase):
    def test_an_sscc_is_eighteen_digits_with_a_check_digit(self):
        number = sscc.build("9521234", "1")
        self.assertEqual(len(number), 18)
        self.assertTrue(sscc.is_valid(number))

    def test_the_serial_reference_fills_the_room_the_prefix_leaves(self):
        # 7-digit prefix: 18 - 1 extension - 1 check - 7 = 9 digits of serial.
        self.assertEqual(sscc.build("9521234", "1")[8:17], "000000001")

    def test_a_longer_prefix_leaves_a_shorter_serial(self):
        self.assertTrue(sscc.is_valid(sscc.build("952123456789", "42")))

    def test_a_serial_that_no_longer_fits_is_refused_rather_than_truncated(self):
        # Truncating would hand two pallets the same number, which is the one
        # thing an SSCC must not do.
        with self.assertRaises(ValueError):
            sscc.build("952123456789", "12345")

    def test_the_extension_digit_is_the_issuers_to_choose(self):
        self.assertTrue(sscc.build("9521234", "1", extension_digit="3").startswith("3"))

    def test_a_prefix_that_cannot_be_one_says_why(self):
        self.assertIn("digits", sscc.problem_with_prefix("95a1234"))
        self.assertIn("empty", sscc.problem_with_prefix("").lower())
        self.assertIn("6 and 12", sscc.problem_with_prefix("12345"))

    def test_a_usable_prefix_has_nothing_to_say(self):
        self.assertEqual(sscc.problem_with_prefix("4068194"), "")
