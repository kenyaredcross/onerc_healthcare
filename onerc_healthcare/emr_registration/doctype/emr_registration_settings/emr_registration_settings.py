# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""EMR Registration Settings (FR-3, OI-13)."""

from frappe.model.document import Document


class EMRRegistrationSettings(Document):
	def validate(self) -> None:
		from onerc_healthcare.registration.walk_in import parse_format, validate_facility

		parse_format(self.walk_in_id_format)
		if self.default_registration_facility:
			validate_facility(self.default_registration_facility)
