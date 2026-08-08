# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Tests for the GS1 helpers.

Plain ``unittest``: no cursor, no registry, so this file runs both under Odoo's
test runner and under a bare test run in CI that has no database. A wrong check
digit should be caught in seconds, not after a full install.

The check-digit vectors are independent of the implementation. One is the EAN-13
that GS1's own check-digit calculator uses as its worked example; the rest are
checked against the property the specification actually states — that the
weighted total of a complete key is a multiple of ten — computed here a second
time, by hand, rather than taken from what this module produced. An
implementation that is wrong in the same way as its expectations would otherwise
look correct.

Constructed identifiers all use the GS1 952 test prefix, reserved for exactly
this and unable to collide with a real company's numbers.
"""

import dataclasses
import unittest

from ..utils.gs1 import (
    ANCHOR_AI,
    BAD_CHECK_DIGIT,
    BAD_LENGTH,
    EMPTY,
    KEY_LENGTHS,
    NOT_NUMERIC,
    KeyProblem,
    check_digit,
    clean,
    digital_link,
    is_valid,
    language_tag,
    problem_with,
    with_check_digit,
)

#: The worked example from GS1's published check-digit calculator.
GS1_WORKED_EXAMPLE = "4006381333931"

TEST_GTIN13 = "9520000000004"
TEST_GTIN14 = "09520000000011"
TEST_GLN = "9520000000004"
TEST_SSCC = "952000000000000002"


def weighted_total(key):
    """The 3/1 weighted sum of a *complete* key, counted from the right.

    Stated by the specification as the property a valid key satisfies: the total
    is a multiple of ten. Written out here independently of the module so the
    tests are not simply agreeing with the code.
    """
    total = 0
    for position, character in enumerate(reversed(key), start=1):
        total += int(character) * (1 if position % 2 else 3)
    return total


class TestCheckDigit(unittest.TestCase):
    def test_the_published_worked_example(self):
        self.assertEqual(check_digit(GS1_WORKED_EXAMPLE[:-1]), GS1_WORKED_EXAMPLE[-1])
        self.assertTrue(is_valid(GS1_WORKED_EXAMPLE, "GTIN"))

    def test_result_is_a_single_character(self):
        digit = check_digit("952000000000")
        self.assertIsInstance(digit, str)
        self.assertEqual(len(digit), 1)

    def test_completed_keys_satisfy_the_specified_property(self):
        # Independent of what check_digit computed: the total must be divisible
        # by ten for every length the connector deals with.
        for body in (
            "9520000",
            "95200000000",
            "952000000000",
            "9520000000000",
            "95200000000000000",
            "952123456789",
            "952999999999",
        ):
            with self.subTest(body=body):
                self.assertEqual(weighted_total(with_check_digit(body)) % 10, 0)

    def test_a_single_digit_change_is_caught(self):
        key = with_check_digit("952000000000")
        for position in range(len(key) - 1):
            altered = list(key)
            altered[position] = str((int(altered[position]) + 1) % 10)
            with self.subTest(position=position):
                self.assertFalse(is_valid("".join(altered), "GTIN"))

    def test_a_zero_body_still_yields_a_digit(self):
        self.assertEqual(check_digit("0000000"), "0")


class TestClean(unittest.TestCase):
    def test_removes_the_padding_people_paste(self):
        self.assertEqual(clean(" 9520-0000 00004 "), "9520000000004")

    def test_removes_the_dots_spreadsheets_add(self):
        self.assertEqual(clean("9520.0000.00004"), "9520000000004")

    def test_accepts_a_number(self):
        self.assertEqual(clean(9520000000004), "9520000000004")

    def test_none_is_empty(self):
        self.assertEqual(clean(None), "")

    def test_odoo_false_is_empty_and_not_the_word(self):
        # An unset Char field reads as False in Odoo, and str(False) would give
        # the truthy value "False" — which then travels on looking like a code.
        self.assertEqual(clean(False), "")
        self.assertFalse(is_valid(False, "GTIN"))


class TestProblemWith(unittest.TestCase):
    def test_a_good_key_has_no_problem(self):
        for key, kind in (
            (TEST_GTIN13, "GTIN"),
            (TEST_GTIN14, "GTIN"),
            (TEST_GLN, "GLN"),
            (TEST_SSCC, "SSCC"),
        ):
            with self.subTest(key=key, kind=kind):
                self.assertIsNone(problem_with(key, kind))

    def test_every_permitted_gtin_length_is_accepted(self):
        for length in KEY_LENGTHS["GTIN"]:
            body = "952" + "0" * (length - 4)
            with self.subTest(length=length):
                self.assertTrue(is_valid(with_check_digit(body), "GTIN"))

    def test_nothing_supplied(self):
        problem = problem_with("", "GTIN")
        self.assertEqual(problem.fault, EMPTY)
        self.assertEqual(problem.kind, "GTIN")

    def test_letters(self):
        problem = problem_with("95200000ABCD4", "GTIN")
        self.assertEqual(problem.fault, NOT_NUMERIC)

    def test_wrong_length_carries_both_sides_of_the_story(self):
        problem = problem_with("952000", "GTIN")
        self.assertEqual(problem.fault, BAD_LENGTH)
        self.assertEqual(problem.actual_length, 6)
        self.assertEqual(problem.allowed_lengths, (8, 12, 13, 14))

    def test_a_gtin_is_not_a_gln_however_well_formed(self):
        problem = problem_with(TEST_GTIN14, "GLN")
        self.assertEqual(problem.fault, BAD_LENGTH)
        self.assertEqual(problem.allowed_lengths, (13,))

    def test_a_bad_check_digit_names_the_right_one(self):
        wrong = TEST_GTIN13[:-1] + "7"
        problem = problem_with(wrong, "GTIN")
        self.assertEqual(problem.fault, BAD_CHECK_DIGIT)
        self.assertEqual(problem.correct_check_digit, TEST_GTIN13[-1])

    def test_the_checks_run_in_the_order_a_person_would_apply_them(self):
        # Too short *and* not numeric: report that it is not a number, because
        # its length is not yet the interesting fact.
        self.assertEqual(problem_with("abc", "GTIN").fault, NOT_NUMERIC)

    def test_an_unhandled_key_type_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            problem_with(TEST_GTIN13, "GRAI")

    def test_a_problem_cannot_be_altered_after_the_fact(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            KeyProblem(EMPTY, "GTIN").fault = BAD_LENGTH


class TestAnchors(unittest.TestCase):
    def test_a_party_and_a_location_are_told_apart(self):
        # Both are GLNs; only the anchor says which is meant, and the resolver
        # routes them separately.
        self.assertEqual(ANCHOR_AI["PARTY_GLN"], "417")
        self.assertEqual(ANCHOR_AI["LOCATION_GLN"], "414")

    def test_a_trade_item_anchors_on_01(self):
        self.assertEqual(ANCHOR_AI["GTIN"], "01")


class TestDigitalLink(unittest.TestCase):
    def test_builds_the_canonical_form(self):
        self.assertEqual(
            digital_link("https://id.epcis.cloud", "01", TEST_GTIN14),
            "https://id.epcis.cloud/01/09520000000011",
        )

    def test_tolerates_a_trailing_slash(self):
        self.assertEqual(
            digital_link("https://id.epcis.cloud/", "01", TEST_GTIN13),
            "https://id.epcis.cloud/01/9520000000004",
        )

    def test_cleans_the_key_on_the_way_in(self):
        self.assertEqual(
            digital_link("https://id.epcis.cloud", "417", "9520-000 000004"),
            "https://id.epcis.cloud/417/9520000000004",
        )

    def test_a_missing_part_yields_nothing_rather_than_half_a_uri(self):
        self.assertEqual(digital_link("", "01", TEST_GTIN13), "")
        self.assertEqual(digital_link("https://id.epcis.cloud", "01", ""), "")
        self.assertEqual(digital_link("https://id.epcis.cloud", "", TEST_GTIN13), "")


class TestLanguageTag(unittest.TestCase):
    def test_the_territory_is_dropped(self):
        self.assertEqual(language_tag("de_DE"), "de")
        self.assertEqual(language_tag("en_GB"), "en")
        self.assertEqual(language_tag("pt_BR"), "pt")

    def test_a_bare_language_passes_through(self):
        self.assertEqual(language_tag("fr"), "fr")

    def test_a_script_that_distinguishes_the_text_is_kept(self):
        self.assertEqual(language_tag("zh_CN"), "zh-Hans")
        self.assertEqual(language_tag("zh_TW"), "zh-Hant")
        self.assertEqual(language_tag("sr_RS@latin"), "sr-Latn")

    def test_a_hyphen_works_as_well_as_an_underscore(self):
        self.assertEqual(language_tag("de-DE"), "de")

    def test_empty_stays_empty(self):
        self.assertEqual(language_tag(""), "")
        self.assertEqual(language_tag(None), "")


if __name__ == "__main__":
    unittest.main()
