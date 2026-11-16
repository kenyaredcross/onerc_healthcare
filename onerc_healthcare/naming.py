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
	# Phase 4 record types (same rule)
	("Outreach Session", "{prefix}-OUT-.YYYY.-"),
	("Sync Conflict", "{prefix}-SCF-.YYYY.-"),
	# Phase 5M maternity record types (same rule); the admission number is the IP number
	("Maternity Admission", "{prefix}-MAT-.YYYY.-"),
	("Partograph", "{prefix}-PTG-.YYYY.-"),
	("Delivery Record", "{prefix}-DEL-.YYYY.-"),
)

# Marley's Emergency Record names provisional patients with this series directly
# (healthcare/doctype/emergency_record/emergency_record.py). User decision 2026-11-14 (D57): such patients
# get the site prefix instead (`prefix_emergency_patient`, Patient before_insert).
EMERGENCY_SERIES = "ER-.YYYY.-"

# Series that must stay valid options of the field besides the rule's own ({prefix} is resolved).
PROTECTED_OPTIONS = {
	"Patient": (EMERGENCY_SERIES, f"{PREFIX_PLACEHOLDER}-{EMERGENCY_SERIES}"),
}


def protected_options(doctype: str, prefix: str | None) -> list[str]:
	return [resolve(o, prefix) for o in PROTECTED_OPTIONS.get(doctype, ())]


def prefix_emergency_patient(doc, method=None) -> None:
	"""Patient before_insert: rename a Marley emergency patient (ER-2026-00001) to the site's series
	(KLB-ER-2026-00001). Marley set the name already (`flags.name_set`), so it is re-made here."""
	if doc.get("naming_series") != EMERGENCY_SERIES:
		return
	prefix = (
		frappe.db.get_single_value(SETTINGS_DOCTYPE, "id_prefix")
		if frappe.db.exists("DocType", SETTINGS_DOCTYPE)
		else None
	)
	if not prefix:
		return
	from frappe.model.naming import make_autoname

	doc.naming_series = resolve(f"{PREFIX_PLACEHOLDER}-{EMERGENCY_SERIES}", prefix)
	doc.name = make_autoname(doc.naming_series, doc.doctype, doc)
	doc.flags.name_set = True


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


def apply_rule(doctype: str, resolved: str, prefix: str | None = None) -> None:
	"""Make `resolved` the only selectable series (plus protected ones) and the default."""
	options = [resolved, *[o for o in protected_options(doctype, prefix) if o != resolved]]
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
	ensure_protected_options(settings)


def ensure_protected_options(settings) -> None:
	"""Re-apply a rule whose field lacks a protected option (one added in a later release, e.g. D57)."""
	for row in settings.rules:
		if (
			not row.enabled
			or row.document_type not in PROTECTED_OPTIONS
			or not supports_series(row.document_type)
		):
			continue
		field = frappe.get_meta(row.document_type).get_field("naming_series")
		have = set((field.options or "").split("\n"))
		if not set(protected_options(row.document_type, settings.id_prefix)) <= have:
			apply_rule(row.document_type, resolve(row.series, settings.id_prefix), settings.id_prefix)
