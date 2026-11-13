# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""EMR test base class: per-test savepoint so tests never see each other's data. Never commit."""

import frappe
from frappe.tests import IntegrationTestCase

SAVEPOINT = "emr_test"


class EMRTestCase(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)

	def tearDown(self) -> None:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.set_user("Administrator")
		super().tearDown()
