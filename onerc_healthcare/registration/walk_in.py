# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Walk-in (facility-generated) identifiers (FR-3, SDD 4.3).

The identifier is `<FACILITY CODE>-<YYYY>-<#####>` by default; the format lives in EMR Registration
Settings (OI-13 pending). The sequence comes from Frappe's `tabSeries` via `getseries`, which locks the
counter row (SELECT ... FOR UPDATE) inside the registering transaction, so concurrent registrations never
share a number and a rolled-back registration does not consume one. A number whose value is already
held (it can only get there by import or direct DB work) is skipped.

Registering facility, in order: the Patient's `krcs_registration_facility`; the user's default
Healthcare Service Unit (User Permission / user default); EMR Registration Settings
`default_registration_facility`; the operation's first facility in operations.json.
"""

import re

import frappe
from frappe import _
from frappe.model.naming import getseries
from frappe.utils import getdate

from onerc_healthcare.constants import IDENTIFIER_FACILITY, PATIENT_IDENTIFIER_DOCTYPE
from onerc_healthcare.registration.identifiers import normalise
from onerc_healthcare.setup.operations import get_facilities, get_operation

SETTINGS_DOCTYPE = "EMR Registration Settings"
HSU = "Healthcare Service Unit"
REGISTRATION_FACILITY_FIELD = "krcs_registration_facility"
DEFAULT_FORMAT = "{facility_code}-{YYYY}-{#####}"
SERIES_NAMESPACE = "krcs-walk-in:"
ALLOWED_TOKENS = ("facility_code", "YYYY", "YY", "MM")
MAX_SKIP = 1000

_TOKEN = re.compile(r"\{([^{}]*)\}")


def parse_format(fmt: str | None) -> tuple[str, int, str]:
	"""Split a format into (prefix template, sequence digits, suffix template). Throws if invalid."""
	fmt = (fmt or "").strip()
	tokens = _TOKEN.findall(fmt)
	sequences = [t for t in tokens if t and set(t) == {"#"}]
	if len(sequences) != 1:
		frappe.throw(
			_("Walk-in Identifier Format must contain exactly one sequence placeholder such as {0}.").format(
				"{#####}"
			),
			title=_("Invalid Walk-in Identifier Format"),
		)
	unknown = [t for t in tokens if t not in ALLOWED_TOKENS and t not in sequences]
	if unknown:
		frappe.throw(
			_("Unknown placeholder {0} in Walk-in Identifier Format. Allowed: {1} and {2}.").format(
				", ".join("{" + t + "}" for t in unknown),
				", ".join("{" + t + "}" for t in ALLOWED_TOKENS),
				"{#####}",
			),
			title=_("Invalid Walk-in Identifier Format"),
		)
	marker = "{" + sequences[0] + "}"
	start = fmt.index(marker)
	return fmt[:start], len(sequences[0]), fmt[start + len(marker) :]


def get_walk_in_format() -> str:
	return frappe.db.get_single_value(SETTINGS_DOCTYPE, "walk_in_id_format", cache=False) or DEFAULT_FORMAT


def is_registration_facility(facility: str | None) -> bool:
	if not facility:
		return False
	row = frappe.db.get_value(HSU, facility, ["is_group", "krcs_facility_code"], as_dict=True)
	return bool(row and row.is_group and row.krcs_facility_code)


def validate_facility(facility: str) -> None:
	if not is_registration_facility(facility):
		frappe.throw(
			_(
				"{0} is not a registration facility. Choose a facility group (Healthcare Service Unit) that "
				"has a Facility Code."
			).format(frappe.bold(facility)),
			title=_("Invalid Registration Facility"),
		)


def get_default_registration_facility() -> str | None:
	"""The registering facility when the Patient does not name one (see module docstring)."""
	candidates = [
		frappe.defaults.get_user_default(HSU),
		frappe.db.get_single_value(SETTINGS_DOCTYPE, "default_registration_facility", cache=False),
	]
	if get_operation():
		candidates.extend(get_facilities())
	for facility in candidates:
		if is_registration_facility(facility):
			return facility
	return None


def _render(template: str, facility_code: str, date) -> str:
	values = {
		"facility_code": facility_code,
		"YYYY": date.strftime("%Y"),
		"YY": date.strftime("%y"),
		"MM": date.strftime("%m"),
	}
	return _TOKEN.sub(lambda m: values[m.group(1)], template)


def generate_walk_in_identifier(facility: str, date=None) -> str:
	"""Next facility-generated identifier for `facility`, e.g. NTK-2026-00001 (FR-3)."""
	validate_facility(facility)
	code = frappe.db.get_value(HSU, facility, "krcs_facility_code").strip().upper()
	prefix_t, digits, suffix_t = parse_format(get_walk_in_format())
	date = getdate(date)
	prefix, suffix = _render(prefix_t, code, date), _render(suffix_t, code, date)
	# One counter per distinct prefix and suffix, i.e. per facility per year with the default format.
	key = f"{SERIES_NAMESPACE}{prefix}#{suffix}"
	for _attempt in range(MAX_SKIP):
		value = f"{prefix}{getseries(key, digits)}{suffix}"
		# Defence in depth (H1): never hand out a value that is already held, e.g. from imported data.
		if not frappe.db.exists(
			PATIENT_IDENTIFIER_DOCTYPE,
			{"identifier_type": IDENTIFIER_FACILITY, "normalised_value": normalise(value)},
		):
			return value
	frappe.throw(
		_("Could not find a free walk-in identifier after {0} attempts (last tried {1}).").format(
			MAX_SKIP, value
		)
	)
