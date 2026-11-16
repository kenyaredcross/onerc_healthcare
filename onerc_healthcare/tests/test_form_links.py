# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A form must not fill in, on screen, a link its user cannot read: the desk validates the value and the user
gets "Insufficient Permission". Found by the user: Dispense (Pharmacist) filled in the consultation."""

import json

import frappe

from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import as_user, users

APP_MODULES_SQL = "select name from `tabModule Def` where app_name = 'onerc_healthcare'"


class TestFetchedLinksReadable(EMRTestCase):
	def test_fetched_links_readable_by_roles_that_create_the_form(self):
		modules = [r[0] for r in frappe.db.sql(APP_MODULES_SQL)]
		doctypes = frappe.get_all(
			"DocType", filters={"module": ["in", modules], "istable": 0, "issingle": 0}, pluck="name"
		)
		problems = []
		for key, user in users().items():
			with as_user(user):
				for doctype in doctypes:
					if not frappe.has_permission(doctype, "create"):
						continue
					for df in frappe.get_meta(doctype).fields:
						if df.fieldtype != "Link" or not df.fetch_from or df.hidden:
							continue
						if not (
							frappe.has_permission(df.options, "select")
							or frappe.has_permission(df.options, "read")
						):
							problems.append(f"{key}: {doctype}.{df.fieldname} -> {df.options}")
		self.assertFalse(problems, json.dumps(problems, indent=1))

	def test_dispensing_record_does_not_fill_the_consultation_on_screen(self):
		field = frappe.get_meta("Dispensing Record").get_field("encounter")
		self.assertFalse(field.fetch_from)
		js = open(
			frappe.get_app_path(
				"onerc_healthcare", "emr_pharmacy", "doctype", "dispensing_record", "dispensing_record.js"
			)
		).read()
		self.assertNotIn('"encounter",', js)
