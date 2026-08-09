"""Tests for form-field resolution — the logic that decides what gets typed into a
company's application form.

Everything here is browser-free. The functions under test are the ones that caused
real wrong answers during development against live Greenhouse forms.
"""

from __future__ import annotations

import pytest

from jobhunter.appliers.form_filler import (
    Field,
    FormFiller,
    Profile,
    _best_option,
    _choice_alternatives,
    _is_choice_question,
    _resolve_choice,
    _resolve_text,
)

PROFILE_DATA = {
    "full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe",
    "email": "jane@example.com", "phone": "+91 98765 43210",
    "location": "Chennai, India", "city": "Chennai", "country": "India",
    "linkedin": "https://linkedin.com/in/janedoe",
    "current_ctc": "12 LPA", "expected_ctc": "18 LPA", "notice_period": "30 days",
    "work_authorized": "Yes", "needs_sponsorship": "No", "willing_to_relocate": "Yes",
}


@pytest.fixture
def profile() -> Profile:
    return Profile(PROFILE_DATA, {"why do you want": "Strong fit for my background."})


def field(label: str, **kwargs) -> Field:
    defaults = dict(
        ref="jh1", tag="input", type="text", label=label, required=False,
        options=[], value="", name="",
    )
    defaults.update(kwargs)
    return Field(**defaults)


class TestOptionMatching:
    """`_best_option` picked "British Indian Ocean Territory" for "India" until it
    learned to try exact and prefix matches before any substring test."""

    COUNTRIES = ["British Indian Ocean Territory +246", "India +91", "Indonesia +62"]

    def test_prefix_match_beats_substring(self):
        assert _best_option("india", self.COUNTRIES) == "India +91"

    def test_distinct_country_still_resolves(self):
        assert _best_option("indonesia", self.COUNTRIES) == "Indonesia +62"

    def test_exact_match_wins(self):
        assert _best_option("no", ["No", "Not applicable", "November"]) == "No"

    def test_real_eeo_wording_is_found(self):
        options = ["Male", "Female", "I don't wish to answer"]
        assert _best_option("i don't wish to answer", options) == "I don't wish to answer"

    def test_no_match_returns_none(self):
        """Returning None routes the job to manual review instead of guessing."""
        assert _best_option("maybe", ["Yes", "No"]) is None

    def test_substring_inside_a_longer_word_is_rejected(self):
        """"india" sits inside "Indian", and the option is far longer than the answer —
        the exact regression that mis-selected a country."""
        assert _best_option("india", ["British Indian Ocean Territory +246"]) is None

    def test_qualified_affirmative_still_counts_as_yes(self):
        """An option that *starts* with the answer is the answer."""
        assert _best_option("yes", ["Yes, with notice"]) == "Yes, with notice"


class TestTextResolution:
    @pytest.mark.parametrize("label,expected", [
        ("First Name*", "Jane"),
        ("Last Name", "Doe"),
        ("Email*", "jane@example.com"),
        ("Phone", "+91 98765 43210"),
        ("LinkedIn Profile", "https://linkedin.com/in/janedoe"),
        ("Expected CTC", "18 LPA"),
        ("Notice Period", "30 days"),
    ])
    def test_standard_fields(self, profile, label, expected):
        assert _resolve_text(field(label), profile) == expected

    def test_custom_answer_takes_priority(self, profile):
        assert _resolve_text(field("Why do you want to work here?"), profile).startswith("Strong fit")

    def test_unknown_field_resolves_to_nothing(self, profile):
        assert _resolve_text(field("What is your favourite colour?"), profile) == ""

    def test_first_name_derived_from_full_name(self):
        bare = Profile({"full_name": "Alex Kumar Singh"})
        assert _resolve_text(field("First Name"), bare) == "Alex"
        assert _resolve_text(field("Last Name"), bare) == "Singh"


class TestChoiceResolution:
    def test_work_authorisation_uses_profile(self, profile):
        fld = field("Are you legally authorized to work in the United States?")
        assert _resolve_choice(fld, profile) == "Yes"

    def test_sponsorship_uses_profile(self, profile):
        assert _resolve_choice(field("Will you require visa sponsorship?"), profile) == "No"

    def test_demographic_questions_are_declined(self, profile):
        for label in ("Gender", "Race and Ethnicity", "Veteran Status", "Disability Status"):
            assert _resolve_choice(field(label), profile) == "Decline to self identify"

    def test_declining_matches_the_real_option_text(self, profile):
        fld = field("Gender", options=[{"value": "1", "text": "Male"},
                                       {"value": "2", "text": "I don't wish to answer"}])
        assert _resolve_choice(fld, profile) == "I don't wish to answer"

    def test_unanswerable_question_returns_none(self, profile):
        """The whole safety model rests on this returning None."""
        assert _resolve_choice(field("How many kittens do you own?"), profile) is None

    def test_screening_questions_are_recognised(self):
        assert _is_choice_question(field("Are you authorized to work here?"))
        assert _is_choice_question(field("Gender"))
        assert not _is_choice_question(field("First Name"))


class TestAlternatives:
    def test_decline_expands_to_known_phrasings(self):
        alts = _choice_alternatives(field("Gender"), "Decline to self identify")
        assert "prefer not to say" in alts
        assert "i don't wish to answer" in alts

    def test_yes_expands_to_agreement_wording(self):
        assert "i agree" in _choice_alternatives(field("Consent"), "Yes")

    def test_plain_value_is_left_alone(self):
        assert _choice_alternatives(field("Country"), "India") == ("India",)


class TestFieldDeduplication:
    """A combobox exposes two nested inputs for one question; filling both
    double-types the answer and double-counts it as unanswered."""

    def test_combobox_pair_collapses_to_one(self):
        outer = field("Country* | country", type="text", combo=True)
        inner = field("Country*", type="input", combo=True)
        assert outer.dedupe_key == inner.dedupe_key

    def test_genuinely_different_fields_are_kept(self):
        assert field("First Name*").dedupe_key != field("Last Name*").dedupe_key

    def test_same_label_different_widget_is_kept(self):
        assert field("Resume", type="file").dedupe_key != field("Resume", type="text").dedupe_key


class TestRequiredFieldSafety:
    def test_unanswered_required_field_blocks_submission(self, profile):
        """FillResult.ok drives the manual-queue decision in BrowserApplier."""
        filler = FormFiller.__new__(FormFiller)
        from jobhunter.appliers.form_filler import FillResult

        result = FillResult()
        result.unresolved_required.append("Why do you want this job?")
        assert not result.ok

    def test_clean_fill_is_ok(self):
        from jobhunter.appliers.form_filler import FillResult

        result = FillResult(filled=["First Name = Jane"], resume_uploaded=True)
        assert result.ok
