# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient doc_events (wired in hooks.py). Owned by A1; the DOB estimate by R3 (Phase 3)."""

import frappe
from frappe import _

from onerc_healthcare.registration.dob import apply_dob_rules
from onerc_healthcare.registration.identifiers import OVERRIDE_REASON_FIELD, validate_patient


def patient_validate(doc, method=None) -> None:
	"""FR-1 to FR-3, NFR-10: identifier rules, walk-in identifier, population group default.

	Phase 3 (R3): exact or estimated date of birth first, so the duplicate check sees the final DOB.
	"""
	apply_dob_rules(doc)
	validate_patient(doc)


def patient_after_insert(doc, method=None) -> None:
	"""Record on the timeline why a possible duplicate was registered anyway (SDD 4.3)."""
	reason = doc.get(OVERRIDE_REASON_FIELD)
	if reason:
		doc.add_comment(
			"Info",
			_("Registered as a new patient despite possible matches. Reason: {0}").format(
				frappe.utils.escape_html(reason)
			),
		)
