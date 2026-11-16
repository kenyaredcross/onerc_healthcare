# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Beds of the Maternity Wards (Healthcare Service Unit leaves).

Marley's Healthcare Service Unit `set_service_unit_properties` tests `service_unit_type != ""`, so a leaf unit saved
with no type (None) looks up "Healthcare Service Unit Type None" and the desk shows a "Not found" page. A bed added
under a Maternity Ward (for example from the admission's bed picker, which does not keep the type) gets type Bed
and its ward's company; any other leaf without a type gets a clear message instead (doc_events before_validate).
"""

import frappe
from frappe import _
from frappe.utils import cint

from onerc_healthcare.constants import MATERNITY_WARD_NAME

BED_UNIT_TYPE = "Bed"
HSU = "Healthcare Service Unit"


def service_unit_before_validate(doc, method=None) -> None:
	if cint(doc.is_group) or doc.service_unit_type:
		return
	parent = doc.parent_healthcare_service_unit
	if parent and str(parent).startswith(MATERNITY_WARD_NAME):
		doc.service_unit_type = BED_UNIT_TYPE
		if not doc.company:
			doc.company = frappe.db.get_value(HSU, parent, "company")
		return
	frappe.throw(
		_("Choose a Service Unit Type for {0} (for example Bed), or tick Is Group.").format(
			frappe.bold(doc.healthcare_service_unit_name or _("this unit"))
		),
		frappe.MandatoryError,
		title=_("Missing Value"),
	)
