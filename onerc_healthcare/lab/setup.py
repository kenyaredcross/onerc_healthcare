# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""lab install hook (setup/install.py calls install() on after_install and after_migrate). Idempotent.

- LAB-07 / FR-15: no paper lab request form. The legacy "Lab Test Print" Print Format is disabled (written
  directly, never saved, so Marley's standard file is untouched; re-applied after a Marley update reloads
  it). Printing a Service Request is governed by the `print` DocPerm, removed by the integrator's
  permission fix-up.
- C-5: the provisional catalogue's Observation Templates consume no stock (``consume_stock`` off), so
  collecting and approving never needs a warehouse or creates a Stock Entry.
"""

import frappe

PAPER_REQUEST_PRINT_FORMATS = ("Lab Test Print",)


def install() -> None:
	disable_paper_request_prints()
	disable_catalogue_stock()


def disable_paper_request_prints() -> None:
	for name in PAPER_REQUEST_PRINT_FORMATS:
		if frappe.db.get_value("Print Format", name, "disabled") == 0:
			frappe.db.set_value("Print Format", name, "disabled", 1, update_modified=False)


def catalogue_templates() -> list[str]:
	"""Names of the provisional catalogue's Observation Templates (tests, panels and components)."""
	from onerc_healthcare.setup.operations import load_data

	data = load_data("lab_catalogue.json")
	names = [row["name"] for row in data["single_tests"]]
	for panel in data["panels"]:
		names.append(panel["name"])
		names += panel["components"]
	return list(dict.fromkeys(names))


def disable_catalogue_stock() -> None:
	for name in frappe.get_all(
		"Observation Template",
		filters={"name": ["in", catalogue_templates()], "consume_stock": 1},
		pluck="name",
	):
		frappe.db.set_value("Observation Template", name, "consume_stock", 0, update_modified=False)
