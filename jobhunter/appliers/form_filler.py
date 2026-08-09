"""Heuristic application-form filler.

Every ATS renders a different form, but they all ask the same ~25 questions. This
module derives a human-readable label for each field, matches it against an ordered
rule list, and fills in the answer from your profile.

Design rules that matter:
  * **Never guess a required field.** If a required question can't be answered from
    the profile or your configured answers, the application is abandoned and routed
    to manual review. Submitting a wrong answer to a screening question is worse
    than not applying.
  * **Decline demographic questions by default.** EEO/diversity fields are answered
    "Decline to self-identify" unless you explicitly configure otherwise — the tool
    does not invent protected-characteristic data about you.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# JS runs in one round trip: per-field DOM walking over CDP is painfully slow.
_COLLECT_FIELDS_JS = """
() => {
  const labelFor = (el) => {
    const bits = [];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) bits.push(l.innerText);
    }
    const wrap = el.closest('label');
    if (wrap) bits.push(wrap.innerText);
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const l = p.querySelector('label, .label, legend, [class*="label" i], [class*="question" i]');
      if (l && !l.contains(el)) { bits.push(l.innerText); break; }
    }
    bits.push(el.getAttribute('aria-label') || '');
    bits.push(el.getAttribute('placeholder') || '');
    bits.push(el.getAttribute('name') || '');
    bits.push(el.id || '');
    return bits.filter(Boolean).join(' | ').replace(/\\s+/g, ' ').trim().slice(0, 300);
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none';
  };

  const out = [];
  const nodes = document.querySelectorAll('input, textarea, select, [contenteditable="true"]');
  nodes.forEach((el, i) => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) return;
    if (type !== 'file' && !visible(el)) return;

    if (!el.dataset.jhId) el.dataset.jhId = 'jh' + i;
    const required = el.required || el.getAttribute('aria-required') === 'true' ||
                     /\\*/.test(labelFor(el));
    let options = [];
    if (el.tagName === 'SELECT') {
      options = Array.from(el.options).map(o => ({ value: o.value, text: o.text.trim() }));
    }
    // Modern ATS forms (current Greenhouse, Ashby) render dropdowns as a text input
    // wired to a popup listbox rather than a <select>. They need click-type-pick,
    // not .fill(), and they carry no <option> elements to read.
    const combo = el.getAttribute('role') === 'combobox' ||
                  !!el.getAttribute('aria-autocomplete') ||
                  el.getAttribute('aria-haspopup') === 'listbox' ||
                  !!el.getAttribute('aria-controls');
    // The id of this input's own popup listbox. Without it we'd scan every option
    // node on the page and can click an entry belonging to a different dropdown.
    const controls = el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '';
    out.push({
      ref: el.dataset.jhId,
      tag: el.tagName.toLowerCase(),
      type,
      label: labelFor(el),
      required,
      options,
      combo,
      controls,
      value: el.value || '',
      name: el.getAttribute('name') || ''
    });
  });
  return out;
}
"""

_AFFIRMATIVE = ("yes", "true", "i agree", "agree", "i consent", "accept")
_DECLINE = (
    "decline to self identify", "decline to self-identify", "i don't wish to answer",
    "i do not wish to answer", "prefer not to say", "prefer not to disclose",
    "do not wish to disclose", "choose not to disclose", "not specified",
)


@dataclass
class Field:
    ref: str
    tag: str
    type: str
    label: str
    required: bool
    options: list[dict]
    value: str
    name: str
    combo: bool = False
    controls: str = ""

    @property
    def haystack(self) -> str:
        return f"{self.label} {self.name}".lower()

    @property
    def dedupe_key(self) -> str:
        """A combobox exposes two nested inputs for one question. Filling both
        double-types the answer and double-counts it as unanswered.

        Keyed on the human-visible label only — the label string also carries the
        `name`/`id` fallbacks, which differ between the outer and inner input.
        """
        visible = self.label.split("|")[0]
        normalised = re.sub(r"[^a-z0-9]+", " ", visible.lower()).strip()
        kind = "text" if self.type in ("input", "text", "tel", "email", "url") else self.type
        return f"{normalised}|{kind}"


@dataclass
class FillResult:
    filled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unresolved_required: list[str] = field(default_factory=list)
    resume_uploaded: bool = False

    @property
    def ok(self) -> bool:
        return not self.unresolved_required


class Profile:
    """Answer lookup: profile values first, then your configured free-text answers."""

    def __init__(self, config_profile: dict[str, Any], answers: dict[str, str] | None = None):
        self.p = config_profile or {}
        self.answers = {k.lower(): str(v) for k, v in (answers or {}).items()}

    def get(self, key: str, default: str = "") -> str:
        value = self.p.get(key, default)
        return "" if value is None else str(value)

    @property
    def first_name(self) -> str:
        return self.get("first_name") or self.get("full_name").split(" ")[0]

    @property
    def last_name(self) -> str:
        if self.get("last_name"):
            return self.get("last_name")
        parts = self.get("full_name").split(" ")
        return parts[-1] if len(parts) > 1 else ""

    def custom_answer(self, label: str) -> str | None:
        """Longest-key-first substring match, so specific answers beat generic ones."""
        lowered = label.lower()
        for key in sorted(self.answers, key=len, reverse=True):
            if key in lowered:
                return self.answers[key]
        return None


# Ordered — first match wins, so put specific patterns above general ones.
_RULES: list[tuple[re.Pattern[str], Callable[[Profile], str]]] = [
    (re.compile(r"first\s*name|given\s*name|^fname", re.I), lambda p: p.first_name),
    (re.compile(r"last\s*name|sur\s*name|family\s*name|^lname", re.I), lambda p: p.last_name),
    (re.compile(r"full\s*name|your\s*name|candidate\s*name|^name\b", re.I), lambda p: p.get("full_name")),
    (re.compile(r"e-?mail", re.I), lambda p: p.get("email")),
    (re.compile(r"phone|mobile|contact\s*(number|no)|telephone", re.I), lambda p: p.get("phone")),
    (re.compile(r"linked\s*in", re.I), lambda p: p.get("linkedin")),
    (re.compile(r"git\s*hub", re.I), lambda p: p.get("github")),
    (re.compile(r"portfolio|personal\s*(web)?site|website|blog", re.I), lambda p: p.get("portfolio")),
    (re.compile(r"current\s*(employer|company|organization)", re.I), lambda p: p.get("current_company")),
    (re.compile(r"current\s*(ctc|salary|compensation)", re.I), lambda p: p.get("current_ctc")),
    (re.compile(r"expected\s*(ctc|salary|compensation)|salary\s*expectation|desired\s*salary",
                re.I), lambda p: p.get("expected_ctc")),
    (re.compile(r"notice\s*period", re.I), lambda p: p.get("notice_period")),
    (re.compile(r"(total|years?\s*of)\s*(work\s*)?experience|^experience", re.I),
     lambda p: p.get("total_experience_years")),
    (re.compile(r"current\s*(location|city)|where.*(based|located)", re.I), lambda p: p.get("location")),
    (re.compile(r"^address|street", re.I), lambda p: p.get("address")),
    (re.compile(r"\bcity\b", re.I), lambda p: p.get("city") or p.get("location")),
    (re.compile(r"\bstate\b|province", re.I), lambda p: p.get("state")),
    (re.compile(r"country", re.I), lambda p: p.get("country")),
    (re.compile(r"zip|postal|pin\s*code", re.I), lambda p: p.get("postal_code")),
    (re.compile(r"how did you (hear|find)|referral source|source", re.I),
     lambda p: p.get("heard_from") or "Company website"),
    (re.compile(r"cover\s*letter|why do you want|tell us about", re.I), lambda p: p.get("cover_letter_text")),
]

# Questions whose answer is a yes/no or a dropdown choice.
_CHOICE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"authoriz|eligible to work|right to work|work permit", re.I), "work_authorized"),
    (re.compile(r"sponsor|visa", re.I), "needs_sponsorship"),
    (re.compile(r"relocat", re.I), "willing_to_relocate"),
    (re.compile(r"remote|work from home", re.I), "open_to_remote"),
    (re.compile(r"notice\s*period", re.I), "notice_period"),
    (re.compile(r"gender|sex\b", re.I), "_decline"),
    (re.compile(r"race|ethnic|hispanic|latino", re.I), "_decline"),
    (re.compile(r"veteran|military", re.I), "_decline"),
    (re.compile(r"disabilit", re.I), "_decline"),
    (re.compile(r"pronoun", re.I), "_decline"),
]

_CONSENT_RE = re.compile(
    r"privacy|terms|consent|gdpr|i agree|acknowledge|certify|confirm that", re.I
)
_RESUME_RE = re.compile(r"resume|cv\b|curriculum", re.I)
_COVER_FILE_RE = re.compile(r"cover", re.I)


def _resolve_text(fld: Field, profile: Profile) -> str:
    custom = profile.custom_answer(fld.label)
    if custom:
        return custom
    for pattern, resolver in _RULES:
        if pattern.search(fld.haystack):
            return resolver(profile)
    return ""


def _resolve_choice(fld: Field, profile: Profile) -> str | None:
    """Return the *text* of the option to select, or None if we can't answer.

    Comboboxes carry no <option> list, so when `fld.options` is empty we return the
    canonical answer text and let the combobox typing find the matching entry.
    """
    custom = profile.custom_answer(fld.label)
    option_texts = [o.get("text", "") for o in fld.options if o.get("text", "").strip()]

    def best(candidates: tuple[str, ...]) -> str | None:
        for candidate in candidates:
            for text in option_texts:
                if candidate in text.lower():
                    return text
        return None

    if custom:
        return best((custom.lower(),)) or (custom if not option_texts else None)

    for pattern, key in _CHOICE_RULES:
        if not pattern.search(fld.haystack):
            continue
        if key == "_decline":
            # Never invent protected-characteristic data — decline instead.
            return best(_DECLINE) or best(("other",)) or (
                None if option_texts else "Decline to self identify"
            )
        answer = profile.get(key)
        if not answer:
            return None
        wanted = answer.strip().lower()
        if not option_texts:
            return answer
        return best((wanted,)) or best(_AFFIRMATIVE if wanted in ("yes", "true") else ("no",))

    if _CONSENT_RE.search(fld.haystack):
        return best(_AFFIRMATIVE) or "Yes"
    return None


def _choice_alternatives(fld: Field, value: str) -> tuple[str, ...]:
    """Acceptable option texts, widest first match wins.

    A combobox's real option text rarely equals our canonical answer — "Decline to
    self identify" might actually read "I don't wish to answer". Matching against a
    family of phrasings is what makes these fields fillable at all.
    """
    lowered = value.strip().lower()
    if lowered in ("decline to self identify", "decline"):
        return _DECLINE + ("other", "not listed")
    if lowered in ("yes", "true"):
        return (value,) + _AFFIRMATIVE
    if lowered in ("no", "false"):
        return (value, "no", "not at this time", "i do not", "i don't")
    return (value,)


def _best_option(needle: str, options: list[str]) -> str | None:
    """Match an answer against dropdown option texts, strongest evidence first.

    Tiers matter: a bare substring test picks "British Indian Ocean Territory" for
    "India", because the country name is literally inside it. Exact and prefix matches
    are checked across *all* options before any weaker tier is considered.
    """
    if not needle or not options:
        return None

    lowered = [(text, text.strip().lower()) for text in options]

    for text, low in lowered:                                   # exact
        if low == needle:
            return text
    for text, low in lowered:                                   # "India +91"
        if low.startswith(needle) and (
            len(low) == len(needle) or not low[len(needle)].isalnum()
        ):
            return text
    boundary = re.compile(rf"(?<![\w']){re.escape(needle)}(?![\w'])")
    for text, low in lowered:                                   # whole word anywhere
        if boundary.search(low):
            return text
    # Only accept a loose substring when the option is not markedly longer than the
    # answer — that is what separates "India" ≈ "India" from "…Indian Ocean…".
    for text, low in lowered:
        if needle in low and len(low) <= len(needle) + 12:
            return text
    return None


def _is_choice_question(fld: Field) -> bool:
    """Does this field ask something we answer from the choice rules?"""
    return any(pattern.search(fld.haystack) for pattern, _ in _CHOICE_RULES) or bool(
        _CONSENT_RE.search(fld.haystack)
    )


class FormFiller:
    def __init__(self, page: Any, profile: Profile, resume_path: Path, cover_letter_path: Path | None = None):
        self.page = page
        self.profile = profile
        self.resume_path = Path(resume_path)
        self.cover_letter_path = Path(cover_letter_path) if cover_letter_path else None

    def collect(self) -> list[Field]:
        try:
            raw = self.page.evaluate(_COLLECT_FIELDS_JS) or []
        except Exception as exc:
            log.warning("could not enumerate form fields: %s", exc)
            return []

        fields: list[Field] = []
        seen: set[str] = set()
        for item in raw:
            field_obj = Field(**item)
            key = field_obj.dedupe_key
            if key in seen and field_obj.type != "file":
                continue
            seen.add(key)
            fields.append(field_obj)
        return fields

    def _locator(self, fld: Field) -> Any:
        return self.page.locator(f"[data-jh-id='{fld.ref}']").first

    def fill(self) -> FillResult:
        result = FillResult()
        for fld in self.collect():
            try:
                self._fill_one(fld, result)
            except Exception as exc:
                log.debug("field '%s' failed: %s", fld.label[:60], exc)
                if fld.required:
                    result.unresolved_required.append(f"{fld.label[:80]} (error: {exc})")
        return result

    def _fill_one(self, fld: Field, result: FillResult) -> None:
        locator = self._locator(fld)

        if fld.type == "file":
            self._fill_file(fld, locator, result)
            return

        if fld.tag == "select" or fld.type in ("radio", "checkbox"):
            self._fill_choice(fld, locator, result)
            return

        # Text rules first, so "How did you hear about this job?" still gets the plain
        # answer. Only fall back to the choice rules for actual screening questions.
        value = _resolve_text(fld, self.profile)
        if not value and (fld.combo or _is_choice_question(fld)):
            value = _resolve_choice(fld, self.profile) or ""

        if not value:
            if fld.required and not fld.value:
                result.unresolved_required.append(fld.label[:100] or fld.name)
            else:
                result.skipped.append(fld.label[:60])
            return

        if fld.combo:
            chosen = self._fill_combobox(fld, locator, str(value), _choice_alternatives(fld, str(value)))
            if chosen is None:
                if fld.required:
                    result.unresolved_required.append(
                        f"{fld.label[:80]} (could not pick '{value}' from the dropdown)"
                    )
                return
            result.filled.append(f"{fld.label[:40]} = {chosen[:40]}")
            return

        locator.fill(str(value), timeout=5000)
        result.filled.append(f"{fld.label[:40]} = {str(value)[:40]}")

    def _options_for(self, fld: Field) -> list[tuple[str, Any]]:
        """Option nodes belonging to *this* field's popup, never the whole page.

        Scoping by `aria-controls` is what stops a stray click landing in some other
        dropdown — an unscoped scan once selected "British Indian Ocean Territory"
        for a country field.
        """
        scopes: list[str] = []
        if fld.controls:
            scopes.append(f"#{fld.controls} [role='option']")
            scopes.append(f"#{fld.controls} li")
        scopes.append("[role='option']:visible")

        for selector in scopes:
            try:
                options = self.page.locator(selector)
                count = options.count()
            except Exception:
                continue
            if count == 0:
                continue

            found: list[tuple[str, Any]] = []
            for index in range(min(count, 80)):
                option = options.nth(index)
                try:
                    text = (option.inner_text() or "").strip()
                except Exception:
                    continue
                if text:
                    found.append((text, option))
            if found:
                return found
        return []

    def _fill_combobox(
        self, fld: Field, locator: Any, value: str, alternatives: tuple[str, ...]
    ) -> str | None:
        """Open the listbox, click the option matching our answer, return its text.

        These widgets ignore .fill() — their state only updates when an option is really
        selected, so a filled-looking box can still submit as empty.

        If nothing matches, this returns None and the caller routes the job to manual
        review. It never falls back to "click the first option": picking an arbitrary
        answer to a screening question is worse than not applying.
        """
        def pick() -> str | None:
            options = self._options_for(fld)
            for candidate in alternatives:
                needle = candidate.strip().lower()
                if not needle:
                    continue
                match = _best_option(needle, [text for text, _ in options])
                if match is None:
                    continue
                for text, option in options:
                    if text == match:
                        try:
                            option.click(timeout=4000)
                            self.page.wait_for_timeout(250)
                            return text
                        except Exception:
                            break
            return None

        try:
            # Close anything already open, so we never read a stale popup.
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(120)

            locator.click(timeout=5000)
            self.page.wait_for_timeout(450)

            # Short lists (yes/no, EEO) are fully rendered on open — match them directly.
            if (chosen := pick()):
                return chosen

            # Long or search-driven lists (country, city) need typing to filter, plus a
            # beat for the async lookup these widgets do.
            try:
                locator.fill("", timeout=2000)
            except Exception:
                pass
            locator.type(value, delay=25, timeout=5000)
            self.page.wait_for_timeout(1500)

            if (chosen := pick()):
                return chosen

            log.debug("no option matched %s for field '%s'", alternatives, fld.label[:60])
            self.page.keyboard.press("Escape")
            return None
        except Exception as exc:
            log.debug("combobox fill failed for '%s': %s", value, exc)
            return None

    def _fill_file(self, fld: Field, locator: Any, result: FillResult) -> None:
        if _COVER_FILE_RE.search(fld.haystack) and not _RESUME_RE.search(fld.haystack):
            if self.cover_letter_path and self.cover_letter_path.exists():
                locator.set_input_files(str(self.cover_letter_path))
                result.filled.append("cover letter uploaded")
            return

        # Anything else that takes a file on an application form wants the resume.
        if self.resume_path.exists():
            locator.set_input_files(str(self.resume_path))
            result.resume_uploaded = True
            result.filled.append(f"resume uploaded ({self.resume_path.name})")
        elif fld.required:
            result.unresolved_required.append(f"resume upload — file not found: {self.resume_path}")

    def _fill_choice(self, fld: Field, locator: Any, result: FillResult) -> None:
        choice = _resolve_choice(fld, self.profile)

        if fld.type == "checkbox":
            # Consent boxes get ticked; anything else is left alone unless answered "yes".
            if choice and choice.strip().lower() in _AFFIRMATIVE or _CONSENT_RE.search(fld.haystack):
                locator.check(timeout=5000)
                result.filled.append(f"checked: {fld.label[:50]}")
            elif fld.required:
                result.unresolved_required.append(fld.label[:100])
            return

        if not choice:
            if fld.required:
                result.unresolved_required.append(fld.label[:100] or fld.name)
            else:
                result.skipped.append(fld.label[:60])
            return

        if fld.tag == "select":
            try:
                locator.select_option(label=choice, timeout=5000)
            except Exception:
                locator.select_option(value=choice, timeout=5000)
            result.filled.append(f"{fld.label[:40]} = {choice[:40]}")
            return

        if fld.type == "radio":
            if choice.strip().lower() in (fld.value or "").lower() or choice.strip().lower() in fld.label.lower():
                locator.check(timeout=5000)
                result.filled.append(f"{fld.label[:40]} = {choice[:30]}")
