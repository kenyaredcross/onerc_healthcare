# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""One row per Patient Visit status change: user, timestamp, department (SDD §4.4)."""

from frappe.model.document import Document


class PatientVisitStatusLog(Document):
	pass
