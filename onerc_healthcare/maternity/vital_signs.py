# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Ward observations (Phase 5M M1): Vital Signs linked to a Maternity Admission (`krcs_maternity_admission`).

The admitting visit is closed (outcome Admitted), so a ward observation carries no `krcs_visit`; the triage
hooks of `visit/events.py` (plausible ranges, recorded time) still run and its `vital_signs_on_submit` leaves it
alone. Wired as a second Vital Signs `validate` hook (hooks.py, integrator).

Rules: the admission exists and belongs to the same patient; a new or submitted observation needs the admission
open (Admitted, In Labour or Delivered) and not archived; an observation is either triage (visit) or ward
(admission), never both. The company comes from the admission's facility.
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import (
	ADMISSION_OPEN_STATUSES,
	MATERNITY_ADMISSION_DOCTYPE,
	VITAL_SIGNS_ADMISSION_FIELD,
)


class WardObservationError(frappe.ValidationError):
	"""The ward observation's admission link is invalid."""


def validate_ward_observation(doc, method=None) -> None:
	name = doc.get(VITAL_SIGNS_ADMISSION_FIELD)
	if not name:
		return
	if doc.get("krcs_visit"):
		frappe.throw(
			_("Vital signs belong either to a visit (triage) or to a maternity admission (ward), not both."),
			WardObservationError,
			title=_("Visit or Admission"),
		)
	adm = frappe.db.get_value(
		MATERNITY_ADMISSION_DOCTYPE,
		name,
		["name", "patient", "status", "facility", "krcs_archived"],
		as_dict=True,
	)
	if not adm:
		frappe.throw(_("Maternity Admission {0} does not exist.").format(name), WardObservationError)
	if adm.patient != doc.patient:
		frappe.throw(
			_("Maternity Admission {0} belongs to a different patient.").format(frappe.bold(name)),
			WardObservationError,
			title=_("Wrong Admission"),
		)
	if getattr(doc, "_action", None) in ("save", "submit") and (
		adm.status not in ADMISSION_OPEN_STATUSES or adm.krcs_archived
	):
		frappe.throw(
			_(
				"Maternity Admission {0} is {1}; ward observations are recorded while the mother is admitted."
			).format(frappe.bold(name), _("archived") if adm.krcs_archived else _(adm.status)),
			WardObservationError,
			title=_("Admission Closed"),
		)
	if not doc.get("company") and adm.facility:
		doc.company = frappe.db.get_value("Healthcare Service Unit", adm.facility, "company")
