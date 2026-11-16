# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Maternity Admission (INP-12). Rules and lifecycle live in `onerc_healthcare.maternity.admission` (M1).

Inserted only by `maternity.admission.admit` (or the W0 test factory); status, the server fields and the
append-only status log change only through its actions. A Discharged admission is read-only except the archive
flag (Medical In-Charge).
"""

from frappe.model.document import Document

from onerc_healthcare.maternity import admission


class MaternityAdmission(Document):
	def onload(self):
		admission.onload(self)

	def before_insert(self):
		admission.before_insert(self)

	def validate(self):
		admission.validate_admission(self)

	def on_update(self):
		admission.on_admission_update(self)
