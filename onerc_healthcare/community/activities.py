# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Community Activity Record: health talks, home visits, outreach sessions (SDD §5.12.7; MOH 717 / 711 and
the Mental Health clinic report count submitted records). Group counts only: no patient names.

Submittable. `TRANSITIONS` lists the docstatus changes and who may make them (DocPerms in the JSON hold
the same matrix; `if_owner` limits the authors to their own records):

| Action | From | To | Who | Rules |
|---|---|---|---|---|
| save | (new) / Draft | Draft | Clinical Officer, Mental Health Officer, Outreach Team Member (own); Medical In-Charge | `validate` |
| submit | Draft | Submitted | the same | `validate` |
| cancel (`cancel_activity`, POST) | Submitted | Cancelled | Medical In-Charge | reason required; sets cancel_reason, cancelled_by, cancelled_at |
| amend | Cancelled | new Draft | the author (own) or Medical In-Charge | standard amend |

`validate`: participants_total ≥ participants_male + participants_female (filled from them when 0, and
at least 1), no negative counts, the date not in the future, the facility a facility group, the activity
type enabled. The HIS Officer reads all records. Nobody deletes (NFR-9).
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime, today

from onerc_healthcare.constants import (
	COMMUNITY_ACTIVITY_DOCTYPE,
	COMMUNITY_ACTIVITY_TYPE_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_OUTREACH,
)

DRAFT, SUBMITTED, CANCELLED = 0, 1, 2
AUTHOR_ROLES = (ROLE_CLINICIAN, ROLE_MHO, ROLE_OUTREACH)
REASON_MAX_LENGTH = 1000


class CommunityActivityError(frappe.ValidationError):
	"""The activity record is not valid."""


@dataclass(frozen=True)
class ActivityTransition:
	label: str
	from_docstatus: tuple[int, ...]
	to_docstatus: int
	own_roles: tuple[str, ...]
	any_roles: tuple[str, ...]
	needs_reason: bool = False


TRANSITIONS: dict[str, ActivityTransition] = {
	"save": ActivityTransition(_("Save"), (DRAFT,), DRAFT, AUTHOR_ROLES, (ROLE_MIC,)),
	"submit": ActivityTransition(_("Submit"), (DRAFT,), SUBMITTED, AUTHOR_ROLES, (ROLE_MIC,)),
	"cancel": ActivityTransition(_("Cancel"), (SUBMITTED,), CANCELLED, (), (ROLE_MIC,), needs_reason=True),
	"amend": ActivityTransition(_("Amend"), (CANCELLED,), DRAFT, AUTHOR_ROLES, (ROLE_MIC,)),
}


def validate(doc) -> None:
	for field in ("participants_male", "participants_female", "participants_total"):
		value = cint(doc.get(field))
		if value < 0:
			frappe.throw(
				_("{0} cannot be negative.").format(_(doc.meta.get_label(field))), CommunityActivityError
			)
		doc.set(field, value)
	counted = doc.participants_male + doc.participants_female
	if not doc.participants_total:
		doc.participants_total = counted
	if doc.participants_total < counted:
		frappe.throw(
			_("Total participants ({0}) cannot be less than male plus female ({1}).").format(
				doc.participants_total, counted
			),
			CommunityActivityError,
			title=_("Participants"),
		)
	if doc.participants_total < 1:
		frappe.throw(_("Enter the number of participants."), CommunityActivityError, title=_("Participants"))
	if not doc.activity_date:
		doc.activity_date = today()
	if getdate(doc.activity_date) > getdate(today()):
		frappe.throw(_("The activity date cannot be in the future."), CommunityActivityError)
	validate_facility(doc.facility)
	if doc.is_new() or doc.has_value_changed("activity_type"):
		if not frappe.db.get_value(COMMUNITY_ACTIVITY_TYPE_DOCTYPE, doc.activity_type, "enabled"):
			frappe.throw(
				_("Activity type {0} is not enabled.").format(frappe.bold(doc.activity_type)),
				CommunityActivityError,
			)
	doc.location = (doc.location or "").strip()
	if not doc.location:
		frappe.throw(_("Enter where the activity took place."), frappe.MandatoryError)
	if not doc.lead_staff:
		doc.lead_staff = frappe.session.user
	if doc.docstatus == DRAFT:
		doc.cancel_reason = doc.cancelled_by = doc.cancelled_at = None


def validate_facility(facility: str | None) -> None:
	unit = frappe.db.get_value(
		"Healthcare Service Unit", facility, ["is_group", "parent_healthcare_service_unit"], as_dict=True
	)
	if not unit or not unit.is_group or not unit.parent_healthcare_service_unit:
		frappe.throw(
			_("Facility must be a facility group in Healthcare Service Units, not {0}.").format(
				frappe.bold(facility or "")
			),
			CommunityActivityError,
			title=_("Invalid Facility"),
		)


def before_cancel(doc) -> None:
	"""Cancel only through `cancel_activity`, which records the reason first."""
	if not (doc.cancel_reason or "").strip():
		frappe.throw(
			_("Give a reason to cancel {0}: use the Cancel button on the form.").format(doc.name),
			CommunityActivityError,
			title=_("Reason Required"),
		)


@frappe.whitelist(methods=["POST"])
def cancel_activity(name: str, reason: str) -> dict:
	"""Cancel a submitted activity record (Medical In-Charge), recording why."""
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter a reason to cancel."), CommunityActivityError, title=_("Reason Required"))
	if len(reason) > REASON_MAX_LENGTH:
		frappe.throw(
			_("The reason is {0} characters long; the limit is {1}.").format(len(reason), REASON_MAX_LENGTH),
			CommunityActivityError,
		)
	doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name, for_update=True)
	doc.check_permission("cancel")
	if doc.docstatus != SUBMITTED:
		frappe.throw(_("Only a submitted activity record can be cancelled."), CommunityActivityError)
	doc.cancel_reason = reason
	doc.cancelled_by = frappe.session.user
	doc.cancelled_at = now_datetime()
	doc.cancel()
	return {"name": doc.name, "docstatus": doc.docstatus}
