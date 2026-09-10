"""Tests for the personal-data classifiers.

These two functions are here because both have already been wrong in production.
`classify_by_value` shipped once without a type check and reported every date and
currency column in the catalog as a phone number: 12 false positives out of 17 hits.
`cpf_has_valid_check_digits` is what separates an 11-digit CPF from an 11-digit
mobile number, and getting it wrong mislabels either personal data or a phone.

The false-positive cases matter more than the true positives. A detector that
over-reports personal data trains people to ignore it.
"""

import pytest

from crawler import (
    classify_by_name,
    classify_by_value,
    cpf_has_valid_check_digits,
    redact,
)

# Generated with the check-digit algorithm; these are well-formed, not real people's.
VALID_CPFS = ["529.982.247-25", "52998224725", "111.444.777-35", "11144477735"]

INVALID_CPFS = [
    "529.982.247-26",   # last check digit off by one
    "111.111.111-11",   # all identical digits, structurally rejected
    "123.456.789-00",   # plausible shape, wrong digits
    "5299822472",       # ten digits
    "529.982.247-255",  # twelve digits
    "",
    None,
]


@pytest.mark.parametrize("value", VALID_CPFS)
def test_valid_cpf_passes(value):
    assert cpf_has_valid_check_digits(value) is True


@pytest.mark.parametrize("value", INVALID_CPFS)
def test_invalid_cpf_fails(value):
    assert cpf_has_valid_check_digits(value) is False


def test_formatting_does_not_change_the_verdict():
    assert cpf_has_valid_check_digits("529.982.247-25") == cpf_has_valid_check_digits("52998224725")


class TestClassifyByValue:
    """Only text columns are sniffed. This is the regression that caused the outage."""

    @pytest.mark.parametrize(
        "samples, data_type",
        [
            (["2026-01-15", "2026-02-20"], "date"),
            ([52345.67, 50120.10], "double"),
            ([1234567890, 9876543210], "bigint"),
            (["2026-01-15 10:00:00"], "timestamp"),
        ],
    )
    def test_non_text_columns_are_never_classified(self, samples, data_type):
        assert classify_by_value(samples, data_type) is None

    def test_dates_stored_as_text_are_still_not_phones(self):
        # Ten to fifteen digits once separators are stripped is the phone rule, and a
        # date very nearly satisfies it. It must not.
        assert classify_by_value(["2026-01-15", "1998-07-30"], "string") is None

    def test_currency_as_text_is_not_a_phone(self):
        assert classify_by_value(["50263.8", "60749.22"], "string") is None

    def test_emails(self):
        assert classify_by_value(["a@b.com", "c.d@e.co.uk"], "string") == "email"

    def test_cpf_with_valid_digits_is_verified(self):
        assert classify_by_value(VALID_CPFS[:2], "string") == "national_id_verified"

    def test_eleven_digit_phone_is_not_claimed_as_a_national_id(self):
        # Same shape as a CPF, failing check digits. Falls through to phone rather
        # than asserting an identifier it cannot verify.
        assert classify_by_value(["55119876543", "55219876543"], "string") == "phone"

    def test_mixed_samples_do_not_classify(self):
        # Every sample has to match; one stray value means the column is something else.
        assert classify_by_value(["a@b.com", "not an email"], "string") is None

    @pytest.mark.parametrize("samples", [[], [None, None], ["", ""]])
    def test_empty_samples_classify_as_nothing(self, samples):
        assert classify_by_value(samples, "string") is None

    def test_free_text_is_not_personal_data(self):
        assert classify_by_value(["aberto", "em_analise", "pago"], "string") is None


class TestClassifyByName:
    @pytest.mark.parametrize(
        "column, expected",
        [
            ("cpf", "national_id"),
            ("nome", "person_name"),
            ("email", "email"),
            ("telefone", "phone"),
            ("data_nascimento", "birth_date"),
            ("endereco", "address"),
            ("ip_address", "network_id"),
            ("user_agent", "network_id"),
        ],
    )
    def test_known_names(self, column, expected):
        assert classify_by_name(column) == expected

    def test_ip_address_is_not_a_postal_address(self):
        # Regression: "address" matched inside "ip_address" because it was checked
        # first, labelling an IP as a street address. Different masking policy,
        # different GDPR treatment. Pattern order is load-bearing.
        assert classify_by_name("ip_address") == "network_id"

    @pytest.mark.parametrize(
        "column", ["id_apolice", "valor_premio", "tipo_seguro", "f_01", "tAcw", "status"]
    )
    def test_neutral_names_are_not_flagged(self, column):
        assert classify_by_name(column) is None

    def test_obfuscated_columns_defeat_name_matching(self):
        # The whole reason the model stage exists. f_01 holds names; nothing in the
        # name says so.
        assert classify_by_name("f_01") is None


class TestRedaction:
    def test_keeps_two_characters(self):
        assert redact("529.982.247-25") == "52***"

    def test_short_values_reveal_nothing(self):
        assert redact("ab") == "***"

    def test_none_stays_none(self):
        assert redact(None) is None
