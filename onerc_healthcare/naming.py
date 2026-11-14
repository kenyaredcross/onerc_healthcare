# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Record ID series per operation (DECISIONS D19), configured in EMR Naming Settings.

Each operation is its own site and database, so Marley's shared prefixes (HLC-PAT-, HLC-ENC-, ...) would
give Kalobeyei and Dadaab identical record IDs. Every clinical record type gets a series that starts with
the site's ID prefix (KLB / DDB), e.g. ``{prefix}-ENC-.YYYY.-`` → ``KLB-ENC-2026-00001``.

The series is applied as property setters on each DocType's ``naming_series`` field (options + default).
EMR Naming Settings is the single place to change them; saving it re-applies every rule.
"""

import re

import frappe
from frappe import _
from frappe.model.naming import NamingSeries

from onerc_healthcare.setup.operations import get_operation, get_operation_config

SETTINGS_DOCTYPE = "EMR Naming Settings"
PREFIX_PLACEHOLDER = "{prefix}"
PREFIX_PATTERN = re.compile(r"^[A-Z0-9]{2,8}$")
MODULE = "Onerc Healthcare"

# Default series per record type. Codes follow Marley's own (HLC-ENC- -> ENC) where it has one.
DEFAULT_RULES = (
	("Patient", "{prefix}-PAT-.YYYY.-"),
	("Patient Visit", "{prefix}-PV-.YYYY.-"),
	("Patient Encounter", "{prefix}-ENC-.YYYY.-"),
	("Vital Signs", "{prefix}-VTS-.YYYY.-"),
	("Observation", "{prefix}-OBS-.YYYY.-"),
	("Sample Collection", "{prefix}-SC-.YYYY.-"),
	("Diagnostic Report", "{prefix}-DR-.YYYY.-"),
	("Lab Test", "{prefix}-LAB-.YYYY.-"),
	("Medication Request", "{prefix}-MR-.YYYY.-"),
	("Service Request", "{prefix}-SR-.YYYY.-"),
	("Clinical Procedure", "{prefix}-CPR-.YYYY.-"),
	("Inpatient Record", "{prefix}-INP-.YYYY.-"),
	("Discharge Summary", "{prefix}-DS-.YYYY.-"),
	("Medication Administration", "{prefix}-MA-.YYYY.-"),
	("Emergency Record", "{prefix}-ED-.YYYY.-"),
	("Patient Appointment", "{prefix}-APP-.YYYY.-"),
	# Phase 2 record types (each must have a Select `naming_series` field and autoname "naming_series:")
	("Referral", "{prefix}-REF-.YYYY.-"),
	("Dispensing Record", "{prefix}-DSP-.YYYY.-"),
	("Patient Consent", "{prefix}-CON-.YYYY.-"),
	("Data Access Request", "{prefix}-DAR-.YYYY.-"),
	("Screening Record", "{prefix}-SCR-.YYYY.-"),
	("Visitor Record", "{prefix}-VIS-.YYYY.-"),
	# Phase 3 record types (same rule: Select `naming_series` field, autoname "naming_series:")
	("MOH Report Run", "{prefix}-MOH-.YYYY.-"),
	("Service Delivered", "{prefix}-SVC-.YYYY.-"),
	("Community Activity Record", "{prefix}-CAR-.YYYY.-"),
)

# Series that Marley code sets directly, so they must stay valid options of the field.
PROTECTED_OPTIONS = {
	"Patient": ("ER-.YYYY.-",),  # healthcare/doctype/emergency_record/emergency_record.py
}


def resolve(series: str, prefix: str) -> str:
	return (series or "").strip().replace(PREFIX_PLACEHOLDER, prefix or "")


def validate_series(series: str, prefix: str) -> str:
	"""Resolve and validate a rule's series; returns the resolved series or throws."""
	resolved = resolve(series, prefix)
	if not resolved:
		frappe.throw(_("Series is required."))
	if "{" in resolved or "}" in resolved:
		frappe.throw(
			_("Series {0}: the only placeholder allowed is {1}.").format(
				frappe.bold(series), frappe.bold(PREFIX_PLACEHOLDER)
			)
		)
	NamingSeries(resolved).validate()
	return resolved


def preview(series: str, prefix: str) -> str:
	"""The first ID the series would produce, e.g. KLB-ENC-2026-00001 (no counter is touched)."""
	try:
		return NamingSeries(resolve(series, prefix)).get_preview()[0]
	except Exception:
		return ""


def supports_series(doctype: str) -> bool:
	if not frappe.db.exists("DocType", doctype):
		return False
	field = frappe.get_meta(doctype).get_field("naming_series")
	return bool(field and field.fieldtype == "Select")


def apply_rule(doctype: str, resolved: str) -> None:
	"""Make `resolved` the only selectable series (plus protected ones) and the default."""
	options = [resolved, *[o for o in PROTECTED_OPTIONS.get(doctype, ()) if o != resolved]]
	for prop, value in (("options", "\n".join(options)), ("default", resolved)):
		frappe.make_property_setter(
			{
				"doctype": doctype,
				"doctype_or_field": "DocField",
				"fieldname": "naming_series",
				"property": prop,
				"value": value,
				"property_type": "Text",
			},
			validate_fields_for_doctype=False,
			module=MODULE,
		)
	frappe.clear_cache(doctype=doctype)


def current_series(doctype: str) -> str | None:
	field = frappe.get_meta(doctype).get_field("naming_series")
	return field.default if field else None


def ensure_naming_settings() -> None:
	"""Seed EMR Naming Settings for this site and keep its defaults complete.

	First run: prefix = operation abbreviation, one rule per supported default record type.
	Later runs: add a default rule only for a record type that was never seeded before (e.g. a DocType
	that a later phase added). `seeded_doctypes` remembers what was offered, so a rule an admin deleted
	is not brought back. Everything else is changed on the settings page.
	"""
	op = get_operation()
	if not op or not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	seeded = set(filter(None, (settings.get("seeded_doctypes") or "").splitlines()))
	if not settings.id_prefix:
		settings.id_prefix = get_operation_config(op)["abbr"]
		settings.set("rules", [])
	elif not seeded:
		# Sites seeded before `seeded_doctypes` existed: everything that has a rule counts as seeded.
		seeded = {row.document_type for row in settings.rules}
	existing = {row.document_type for row in settings.rules}
	added = False
	for doctype, series in DEFAULT_RULES:
		if doctype in seeded or doctype in existing or not supports_series(doctype):
			continue
		settings.append("rules", {"document_type": doctype, "series": series, "enabled": 1})
		seeded.add(doctype)
		added = True
	seeded |= existing
	new_seeded = "\n".join(sorted(seeded))
	if added or not settings.get("seeded_doctypes") or settings.seeded_doctypes != new_seeded:
		settings.seeded_doctypes = new_seeded
		settings.flags.ignore_permissions = True
		settings.save()
