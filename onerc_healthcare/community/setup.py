# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R3: seed the Visit Service Type and Community Activity Type masters (SDD §5.12.7).

Called on install and migrate (W0 wiring). Idempotent and never commits: a seed row is inserted only
when no record of that name exists, so the HIS Officer's edits (category, MOH line, disabled) survive
every migrate. Seeded rows carry the provisional note until the HIS Officer confirms them (OI-5).
"""

import frappe

from onerc_healthcare.constants import COMMUNITY_ACTIVITY_TYPE_DOCTYPE, SERVICE_TYPE_DOCTYPE

PROVISIONAL_NOTE = "PROVISIONAL — HIS Officer to confirm (OI-5)"

# The nine medical social work service lines of the MOH 711 rehabilitation section (SDD §5.12.7).
# (name, category, MOH line)
SERVICE_TYPES = (
	("Psycho-social counselling", "Psychosocial", "Psycho-social counselling"),
	("Alcohol and drug abuse", "Psychosocial", "Alcohol and drug abuse"),
	("Mental illness", "Psychosocial", "Mental illness"),
	("Adolescent issues", "Social Work", "Adolescent issues"),
	("Psycho-social assessment", "Psychosocial", "Psycho-social assessments"),
	("Social investigation", "Social Work", "Social investigations"),
	("Social rehabilitation", "Rehabilitation", "Social rehabilitation"),
	("Outreach services and health talks", "Health Education", "Outreach services and health talks"),
	("Referral", "Social Work", "Referrals"),
)

# Seeded as Sensitive (mental health, alcohol and drug): reads are flagged in the access log (L13).
SENSITIVE_SERVICE_TYPES = (
	"Psycho-social counselling",
	"Alcohol and drug abuse",
	"Mental illness",
	"Psycho-social assessment",
)
# Bumped when a one-off update of already-seeded rows is added to `_apply_seed_updates`.
SEED_VERSION = 2
SEED_VERSION_KEY = "krcs_community_seed_version"

# (name, category, MOH line)
ACTIVITY_TYPES = (
	("Health Talk", "Health Talk", "Health talks"),
	("Home Visit", "Home Visit", "Home visits"),
	("Outreach session", "Outreach", "Outreach sessions"),
	("Community mobilisation", "Community Mobilisation", "Community mobilisation"),
)


def install() -> None:
	seed(SERVICE_TYPE_DOCTYPE, "service_type_name", SERVICE_TYPES, SENSITIVE_SERVICE_TYPES)
	seed(COMMUNITY_ACTIVITY_TYPE_DOCTYPE, "activity_type_name", ACTIVITY_TYPES)
	_apply_seed_updates()


def seed(doctype: str, name_field: str, rows, sensitive: tuple[str, ...] = ()) -> None:
	if not frappe.db.table_exists(doctype):
		return
	for name, category, moh_line in rows:
		if frappe.db.exists(doctype, name):
			continue
		values = {
			"doctype": doctype,
			name_field: name,
			"category": category,
			"moh_line": moh_line,
			"enabled": 1,
			"note": PROVISIONAL_NOTE,
		}
		if sensitive:
			values["sensitive"] = 1 if name in sensitive else 0
		frappe.get_doc(values).insert(ignore_permissions=True)


def _apply_seed_updates() -> None:
	"""One-off updates of rows seeded by earlier versions, once per site (later edits are kept)."""
	version = frappe.utils.cint(frappe.db.get_default(SEED_VERSION_KEY))
	if version >= SEED_VERSION:
		return
	if version < 2 and frappe.get_meta(SERVICE_TYPE_DOCTYPE).has_field("sensitive"):
		# L13: the Sensitive flag was added after the first seed.
		for name in SENSITIVE_SERVICE_TYPES:
			if frappe.db.exists(SERVICE_TYPE_DOCTYPE, name):
				frappe.db.set_value(SERVICE_TYPE_DOCTYPE, name, "sensitive", 1, update_modified=False)
	frappe.db.set_default(SEED_VERSION_KEY, str(SEED_VERSION))
