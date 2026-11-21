# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""pharmacy install hook (setup/install.py calls install() on after_install and after_migrate). Idempotent.

- Medication Request dispensing fields (`krcs_dispense_status`, `krcs_qty_dispensed`), FR-21, and the
  prescriber hold (`krcs_hold_status`, `krcs_hold_reason`), fix round L3.
- The PROVISIONAL medication catalogue (OI-9): batch stock items (Phase 6S), price-free (C-5).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.pharmacy.catalogue import seed_catalogue
from onerc_healthcare.pharmacy.dispensing import (
	HOLD_FIELD,
	HOLD_REASON_FIELD,
	HOLD_STATES,
	LINE_PENDING,
	LINE_STATES,
	MR_DOCTYPE,
	QTY_FIELD,
	STATUS_FIELD,
)

MR_CUSTOM_FIELDS = {
	MR_DOCTYPE: [
		{
			"fieldname": "krcs_dispensing_section",
			"label": "Dispensing",
			"fieldtype": "Section Break",
			"insert_after": "comment",
		},
		{
			"fieldname": STATUS_FIELD,
			"label": "Dispense Status",
			"fieldtype": "Select",
			"options": "\n".join(LINE_STATES),
			"default": LINE_PENDING,
			"read_only": 1,
			"no_copy": 1,
			"in_list_view": 1,
			"in_standard_filter": 1,
			"insert_after": "krcs_dispensing_section",
		},
		{
			"fieldname": "krcs_dispensing_column",
			"fieldtype": "Column Break",
			"insert_after": STATUS_FIELD,
		},
		{
			"fieldname": QTY_FIELD,
			"label": "Quantity Dispensed",
			"fieldtype": "Float",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "krcs_dispensing_column",
		},
		{
			"fieldname": HOLD_FIELD,
			"label": "Prescriber Hold",
			"fieldtype": "Select",
			"options": "\n".join(HOLD_STATES),
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"description": "Set with Hold / Stop / Resume (prescriber or Medical In-Charge)",
			"insert_after": QTY_FIELD,
		},
		{
			"fieldname": HOLD_REASON_FIELD,
			"label": "Hold Reason",
			"fieldtype": "Small Text",
			"read_only": 1,
			"no_copy": 1,
			"depends_on": f"eval:doc.{HOLD_FIELD}",
			"insert_after": HOLD_FIELD,
		},
	]
}


def install() -> None:
	create_custom_fields(MR_CUSTOM_FIELDS, ignore_validate=True)
	# Lines created before the field existed read as Pending.
	frappe.db.sql(
		f"UPDATE `tabMedication Request` SET `{STATUS_FIELD}`=%s WHERE IFNULL(`{STATUS_FIELD}`, '')=''",
		(LINE_PENDING,),
	)
	seed_catalogue()
