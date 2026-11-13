# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Record ID series per operation (D19). Saving applies every enabled rule."""

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.naming import (
	PREFIX_PATTERN,
	apply_rule,
	current_series,
	preview,
	resolve,
	supports_series,
	validate_series,
)


class EMRNamingSettings(Document):
	def validate(self) -> None:
		self.id_prefix = (self.id_prefix or "").strip().upper()
		if not PREFIX_PATTERN.match(self.id_prefix):
			frappe.throw(_("ID Prefix must be 2-8 capital letters or digits, e.g. KLB."))
		seen_types: set[str] = set()
		seen_series: dict[str, str] = {}
		for row in self.rules:
			if row.document_type in seen_types:
				frappe.throw(_("Row {0}: {1} appears more than once.").format(row.idx, row.document_type))
			seen_types.add(row.document_type)
			if not supports_series(row.document_type):
				frappe.throw(
					_("Row {0}: {1} is not named by a naming series, so its IDs cannot be set here.").format(
						row.idx, frappe.bold(row.document_type)
					)
				)
			row.series = (row.series or "").strip()
			resolved = validate_series(row.series, self.id_prefix)
			row.example = preview(row.series, self.id_prefix)
			if not row.enabled:
				continue
			key = resolved.rstrip(".-#")
			if key in seen_series:
				frappe.throw(
					_(
						"Row {0}: {1} and {2} would share the series {3}. Give each record type its own code."
					).format(row.idx, row.document_type, seen_series[key], frappe.bold(resolved))
				)
			seen_series[key] = row.document_type

	def on_update(self) -> None:
		changed = []
		for row in self.rules:
			if not row.enabled:
				continue
			resolved = resolve(row.series, self.id_prefix)
			if current_series(row.document_type) == resolved:
				continue
			apply_rule(row.document_type, resolved)
			existing = frappe.db.count(row.document_type)
			changed.append(
				_("{0}: new IDs like {1}").format(row.document_type, row.example)
				+ (_(" ({0} existing records keep their IDs)").format(existing) if existing else "")
			)
		if changed and not frappe.flags.in_install and not frappe.flags.in_migrate:
			frappe.msgprint("<br>".join(changed), title=_("Record ID series updated"), indicator="green")
