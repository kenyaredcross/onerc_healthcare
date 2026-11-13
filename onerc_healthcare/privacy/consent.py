# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient consent (FR-37, BR-12, C-3; SDD §5.10).

- A Patient Consent is submitted once and never cancelled. The patient's *current* consent is the
  latest submitted record (by `recorded_at`, then creation).
- No consent, or consent withheld / unable / withdrawn, never blocks care: registration and visits
  proceed with a warning (OI-14), and these patients appear on the Consent Exceptions report.

APIs:
- `get_consent_status(patient)` (GET): current outcome, date and version, or outcome "none".
- `record_consent(...)` (POST): insert and submit a Patient Consent as the current user.
- `get_visit_consent_warning(patient)` (GET) / `visit_after_insert` (doc event): the visit warning.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from onerc_healthcare.constants import CONSENT_DOCTYPE

OUTCOME_GIVEN = "Given"
OUTCOME_WITHHELD = "Withheld"
OUTCOME_UNABLE = "Unable to Consent"
OUTCOME_WITHDRAWN = "Withdrawn"
OUTCOMES = (OUTCOME_GIVEN, OUTCOME_WITHHELD, OUTCOME_UNABLE, OUTCOME_WITHDRAWN)
OUTCOME_NONE = "none"

GIVEN_BY_PATIENT = "Patient"
GIVEN_BY_REPRESENTATIVE = "Representative"


def get_current_consent(patient: str, exclude: str | None = None) -> dict | None:
	"""The patient's latest submitted Patient Consent (dict) or None. No permission check."""
	filters = {"patient": patient, "docstatus": 1}
	if exclude:
		filters["name"] = ("!=", exclude)
	rows = frappe.get_all(
		CONSENT_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"outcome",
			"recorded_at",
			"statement",
			"statement_version",
			"given_by",
			"representative_name",
			"relationship",
			"reason",
		],
		order_by="recorded_at desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def consent_status(patient: str) -> dict:
	"""Status dict for `patient` (no permission check)."""
	row = get_current_consent(patient)
	if not row:
		return {
			"patient": patient,
			"outcome": OUTCOME_NONE,
			"date": None,
			"recorded_at": None,
			"version": None,
			"consent": None,
			"given_by": None,
			"reason": None,
			"needs_attention": True,
		}
	return {
		"patient": patient,
		"outcome": row.outcome,
		"date": getdate(row.recorded_at) if row.recorded_at else None,
		"recorded_at": row.recorded_at,
		"version": row.statement_version,
		"consent": row.name,
		"given_by": row.given_by,
		"reason": row.reason,
		"needs_attention": row.outcome != OUTCOME_GIVEN,
	}


def _check_status_permission(patient: str) -> None:
	if not patient or not frappe.db.exists("Patient", patient):
		frappe.throw(_("Patient {0} does not exist.").format(patient), frappe.DoesNotExistError)
	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	frappe.has_permission(CONSENT_DOCTYPE, "read", throw=True)


@frappe.whitelist(methods=["GET"])
def get_consent_status(patient: str) -> dict:
	"""Current consent of `patient`: outcome (or "none"), date, version. Needs read on the Patient and
	on Patient Consent."""
	_check_status_permission(patient)
	return consent_status(patient)


@frappe.whitelist(methods=["POST"])
def record_consent(
	patient: str,
	statement: str,
	outcome: str,
	given_by: str = GIVEN_BY_PATIENT,
	representative_name: str | None = None,
	relationship: str | None = None,
	reason: str | None = None,
) -> dict:
	"""Record and submit the patient's consent (Patient form dialog). Normal create/submit permissions."""
	doc = frappe.get_doc(
		{
			"doctype": CONSENT_DOCTYPE,
			"patient": patient,
			"statement": statement,
			"outcome": outcome,
			"given_by": given_by or GIVEN_BY_PATIENT,
			"representative_name": representative_name,
			"relationship": relationship,
			"reason": reason,
		}
	)
	doc.insert()
	doc.submit()
	return consent_status(patient)


def consent_warning(patient: str) -> dict | None:
	"""Warning for the visit form (not a block, OI-14): {"level", "message"} or None when consent is Given."""
	status = consent_status(patient)
	if status["outcome"] == OUTCOME_GIVEN:
		return None
	if status["outcome"] == OUTCOME_NONE:
		return {
			"level": "warning",
			"outcome": OUTCOME_NONE,
			"message": _(
				"No data protection consent is recorded for this patient. Care continues; record the"
				" patient's consent from the Patient record."
			),
		}
	return {
		"level": "info",
		"outcome": status["outcome"],
		"message": _("Consent: {0} on {1} ({2}). Care continues; the case is on the exceptions list.").format(
			_(status["outcome"]), frappe.format(status["date"], "Date"), status["reason"] or ""
		),
	}


@frappe.whitelist(methods=["GET"])
def get_visit_consent_warning(patient: str) -> dict | None:
	"""Visit form: the consent warning for `patient`, or None. Same permission check as the status."""
	_check_status_permission(patient)
	return consent_warning(patient)


def visit_after_insert(doc, method=None) -> None:
	"""Optional doc event (Patient Visit after_insert): show the consent warning; never blocks."""
	try:
		if not doc.get("patient"):
			return
		warning = consent_warning(doc.patient)
		if warning and warning["level"] == "warning":
			frappe.msgprint(
				warning["message"], title=_("Consent Not Recorded"), indicator="orange", alert=True
			)
	except Exception:
		frappe.logger("onerc_healthcare.privacy").exception("visit consent warning failed")
