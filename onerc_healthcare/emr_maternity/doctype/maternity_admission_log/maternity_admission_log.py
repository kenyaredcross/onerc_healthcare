# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Append-only status log row of a Maternity Admission (written by maternity.admission only; the parent's
validate refuses edited, removed or hand-added rows)."""

from frappe.model.document import Document


class MaternityAdmissionLog(Document):
	pass
