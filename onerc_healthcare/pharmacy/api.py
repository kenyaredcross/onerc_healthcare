# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Prescriber hold / stop / resume of a prescription line (fix round L3, DEFECT-3; FR-19, BR-6).

Who: the Clinical Officer or Mental Health Officer who prescribed the line (the Medication Request's
practitioner is their Healthcare Practitioner), or the Medical In-Charge for any line. A reason is always
required; it is stored on the line (`krcs_hold_reason`) and as a Comment on the Medication Request.

The rules (`dispensing.HOLD_TRANSITIONS`, table in the `pharmacy.dispensing` docstring):

- `hold_line`: an open line (Pending / Partly Dispensed) without a hold -> Held (Marley on-hold).
- `stop_line`: an open line, not yet stopped -> Stopped (Marley stopped). A stopped line counts as
  resolved, so `dispensed` is raised when it was the visit's last open line.
- `resume_line`: a Held line -> no hold; Marley's status is recomputed from the line state.

Locking: the visit, then the Medication Request (`dispensing.lock_line`); the checks use the locked rows.
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC
from onerc_healthcare.pharmacy import dispensing as line

PRESCRIBER_ROLES = (ROLE_CLINICIAN, ROLE_MHO)  # their own lines
ANY_LINE_ROLES = (ROLE_MIC,)  # any line
MAX_REASON_LENGTH = 1000


class IllegalLineActionError(frappe.ValidationError):
	"""The hold action is not allowed in the line's current dispense or hold state."""


def can_act_on_line(medication_request: str, user: str | None = None) -> bool:
	"""May `user` hold, stop or resume this line (role and, for prescribers, own line)?"""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	if roles & set(ANY_LINE_ROLES):
		return True
	if not roles & set(PRESCRIBER_ROLES):
		return False
	practitioner = frappe.db.get_value(line.MR_DOCTYPE, medication_request, "practitioner")
	return (
		bool(practitioner) and frappe.db.get_value("Healthcare Practitioner", practitioner, "user_id") == user
	)


def apply_line_action(medication_request: str, action: str, reason: str | None) -> dict:
	rule = line.HOLD_TRANSITIONS.get(action)
	if not rule:
		frappe.throw(_("Unknown prescription line action: {0}").format(action), IllegalLineActionError)
	if not frappe.db.exists(line.MR_DOCTYPE, medication_request):
		frappe.throw(
			_("Medication Request {0} does not exist.").format(medication_request), frappe.DoesNotExistError
		)
	# Permission first, from fields that never change (plain reads, before any lock).
	if not can_act_on_line(medication_request):
		frappe.throw(
			_(
				"Only the prescribing Clinical Officer / Mental Health Officer or the Medical In-Charge can {0} this line."
			).format(rule.label.lower()),
			frappe.PermissionError,
		)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(
			_("Enter the reason to {0} this line.").format(rule.label.lower()), title=_("Reason Required")
		)
	if len(reason) > MAX_REASON_LENGTH:
		frappe.throw(_("The reason is limited to {0} characters.").format(MAX_REASON_LENGTH))

	visit_row, mr = line.lock_line(medication_request)
	if mr.docstatus != 1:
		frappe.throw(_("Medication Request {0} is not a released prescription.").format(medication_request))
	state = mr.get(line.STATUS_FIELD) or line.LINE_PENDING
	hold = mr.get(line.HOLD_FIELD) or line.HOLD_NONE
	if state not in rule.from_line_states or hold not in rule.from_holds:
		frappe.throw(
			_("{0} is not possible: the line is {1}{2}.").format(
				frappe.bold(rule.label),
				_(state),
				f" ({_(hold)})" if hold else "",
			),
			IllegalLineActionError,
			title=_("Not Possible Now"),
		)
	frappe.db.set_value(
		line.MR_DOCTYPE,
		medication_request,
		{
			line.HOLD_FIELD: rule.to_hold,
			line.HOLD_REASON_FIELD: reason if rule.to_hold else None,
			"status": line.marley_status_for(state, rule.to_hold),
		},
	)
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": line.MR_DOCTYPE,
			"reference_name": medication_request,
			"content": _("{0}: {1}").format(rule.label, reason),
		}
	).insert(ignore_permissions=True)
	raised = False
	if action == line.ACTION_STOP and visit_row:
		raised = line.raise_if_complete(
			visit_row.name, _("Prescription line {0} stopped: {1}").format(medication_request, reason)
		)
	return {
		"medication_request": medication_request,
		"hold": rule.to_hold,
		"status": line.marley_status_for(state, rule.to_hold),
		"dispense_status": state,
		"visit_event_raised": raised,
	}


@frappe.whitelist(methods=["POST"])
def hold_line(medication_request: str, reason: str) -> dict:
	"""Put an open prescription line on hold (prescriber or Medical In-Charge; reason required)."""
	return apply_line_action(medication_request, line.ACTION_HOLD, reason)


@frappe.whitelist(methods=["POST"])
def stop_line(medication_request: str, reason: str) -> dict:
	"""Stop an open prescription line for good (prescriber or Medical In-Charge; reason required)."""
	return apply_line_action(medication_request, line.ACTION_STOP, reason)


@frappe.whitelist(methods=["POST"])
def resume_line(medication_request: str, reason: str) -> dict:
	"""Resume a held prescription line (prescriber or Medical In-Charge; reason required)."""
	return apply_line_action(medication_request, line.ACTION_RESUME, reason)
