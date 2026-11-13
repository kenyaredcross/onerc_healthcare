# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient controller mixin (hooks.py extend_doctype_class). Owned by A1. Always call super()."""

import frappe

from onerc_healthcare.registration.identifiers import (
	is_identifier_unique_violation,
	raise_friendly_unique_error,
)


class PatientMixin:
	"""Turns the identifier unique-index error (two saves racing, FR-2) into a clear message (NFR-10)."""

	def insert(self, *args, **kwargs):
		try:
			return super().insert(*args, **kwargs)
		except frappe.UniqueValidationError as e:
			if is_identifier_unique_violation(e):
				raise_friendly_unique_error(self, e)
			raise

	def save(self, *args, **kwargs):
		try:
			return super().save(*args, **kwargs)
		except frappe.UniqueValidationError as e:
			if is_identifier_unique_violation(e):
				raise_friendly_unique_error(self, e)
			raise
