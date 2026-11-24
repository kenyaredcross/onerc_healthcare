# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Mental Health consultation note (FR-13, CON-13; provisional format D63, to be confirmed under OI-7).

The note is the `krcs_mh_*` fields on the Patient Encounter (clinical/setup.py `MH_NOTE_FIELDS`), so it
inherits the encounter's sensitivity rules: sensitive access logging, hidden from Midwife-only readers,
never sent to outreach. It is locked with the encounter on completion; corrections go in the addendum.

- `validate` (encounter validate): on a Mental Health encounter, PHQ-9 / GAD-7 are blank or a whole number
  in range (stored normalised, so blank stays different from 0) and the follow-up date is not in the past;
  on any other encounter every `krcs_mh_*` value is cleared, so the note never leaks into another clinic.
- `assert_complete` (before submit): suicide risk, risk of harm to others and mood are required; a safety
  plan is required when a risk is High or there is recent self-harm.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, today

from onerc_healthcare.clinical.setup import MH_NOTE_FIELDS
from onerc_healthcare.constants import DEPT_MENTAL_HEALTH

DEPARTMENT_FIELD = "krcs_department"
SUICIDE_RISK_FIELD = "krcs_mh_suicide_risk"
HARM_TO_OTHERS_FIELD = "krcs_mh_harm_to_others"
MOOD_FIELD = "krcs_mh_mood"
SELF_HARM_FIELD = "krcs_mh_recent_self_harm"
SAFETY_PLAN_FIELD = "krcs_mh_safety_plan"
FOLLOW_UP_FIELD = "krcs_mh_follow_up_date"
HIGH_RISK = "High"
# Field -> (maximum score, [(lowest score of the band, band)]).
SCORES = {
	"krcs_mh_phq9": (
		27,
		[(0, "Minimal"), (5, "Mild"), (10, "Moderate"), (15, "Moderately severe"), (20, "Severe")],
	),
	"krcs_mh_gad7": (21, [(0, "Minimal"), (5, "Mild"), (10, "Moderate"), (15, "Severe")]),
}
REQUIRED_TO_COMPLETE = (SUICIDE_RISK_FIELD, HARM_TO_OTHERS_FIELD, MOOD_FIELD)
LAYOUT_FIELDTYPES = ("Section Break", "Column Break", "Tab Break", "HTML")
# Every data-bearing field of the note.
NOTE_FIELDS = tuple(df["fieldname"] for df in MH_NOTE_FIELDS if df["fieldtype"] not in LAYOUT_FIELDTYPES)
LABELS = {df["fieldname"]: df.get("label") for df in MH_NOTE_FIELDS}
_WHOLE_NUMBER = re.compile(r"^\d+$")


class MentalHealthNoteError(frappe.ValidationError):
	"""The Mental Health consultation note is invalid or incomplete (FR-13)."""


def is_mental_health(doc) -> bool:
	return doc.get(DEPARTMENT_FIELD) == DEPT_MENTAL_HEALTH


def validate(doc) -> None:
	"""Encounter validate: check the note on a Mental Health encounter, clear it on any other."""
	if not is_mental_health(doc):
		clear(doc)
		return
	for fieldname, (maximum, _bands) in SCORES.items():
		doc.set(fieldname, normalise_score(doc.get(fieldname), maximum, _label(fieldname)))
	follow_up = doc.get(FOLLOW_UP_FIELD)
	if follow_up and _follow_up_checked(doc) and getdate(follow_up) < getdate(today()):
		frappe.throw(
			_("{0} cannot be in the past.").format(_(_label(FOLLOW_UP_FIELD))),
			MentalHealthNoteError,
			title=_("Mental Health Note"),
		)


def clear(doc) -> None:
	"""Blank every note field (an encounter of another department never carries the note)."""
	for fieldname in NOTE_FIELDS:
		if doc.get(fieldname):
			doc.set(fieldname, 0 if fieldname == SELF_HARM_FIELD else None)


def normalise_score(value, maximum: int, label: str) -> str | None:
	"""Blank stays None; otherwise a whole number 0..maximum, returned as its canonical string ("07" -> "7")."""
	text = cstr(value).strip()
	if not text:
		return None
	# DEFECT-MH-2: check the length before int(), so a huge string is a validation message, not a server error.
	if len(text) > 6 or not _WHOLE_NUMBER.match(text) or int(text) > maximum:
		frappe.throw(
			_("{0} must be a whole number from 0 to {1}, or blank if not done.").format(_(label), maximum),
			MentalHealthNoteError,
			title=_("Mental Health Note"),
		)
	return str(int(text))


def score_band(fieldname: str, value) -> str | None:
	"""Severity band of a stored PHQ-9 / GAD-7 score, None when blank."""
	text = cstr(value).strip()
	if fieldname not in SCORES or not _WHOLE_NUMBER.match(text):
		return None
	score = int(text)
	band = None
	for lowest, name in SCORES[fieldname][1]:
		if score >= lowest:
			band = name
	return band


def assert_complete(doc) -> None:
	"""Before completion (complete_consultation, referral send, before_submit): the note's required parts."""
	if not is_mental_health(doc):
		return
	missing = [f for f in REQUIRED_TO_COMPLETE if not cstr(doc.get(f)).strip()]
	if missing:
		frappe.throw(
			_("Complete the Mental Health assessment first. Missing: {0}.").format(
				", ".join(frappe.bold(_(_label(f))) for f in missing)
			),
			MentalHealthNoteError,
			title=_("Mental Health Note Incomplete"),
		)
	reasons = [
		_(_label(f)) + ": " + _(HIGH_RISK)
		for f in (SUICIDE_RISK_FIELD, HARM_TO_OTHERS_FIELD)
		if doc.get(f) == HIGH_RISK
	]
	if cint(doc.get(SELF_HARM_FIELD)):
		reasons.append(_(_label(SELF_HARM_FIELD)))
	if reasons and not cstr(doc.get(SAFETY_PLAN_FIELD)).strip():
		frappe.throw(
			_("A {0} is required ({1}).").format(
				frappe.bold(_(_label(SAFETY_PLAN_FIELD))), "; ".join(reasons)
			),
			MentalHealthNoteError,
			title=_("Safety Plan Required"),
		)


def _label(fieldname: str) -> str:
	return LABELS.get(fieldname) or fieldname


def _follow_up_checked(doc) -> bool:
	"""Checked when the date is entered or changed, and again at completion (a draft may wait for days)."""
	return doc.is_new() or doc.has_value_changed(FOLLOW_UP_FIELD) or getattr(doc, "_action", None) == "submit"
