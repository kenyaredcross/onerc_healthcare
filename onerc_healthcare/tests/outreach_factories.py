# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 4 test factories (W0; builders may add helpers at the end, never change these signatures).

users = outreach_users()                 # {"member": ..., "member2": ..., "outsider": ..., "his": ..., "mic": ...}
area, leaf = make_catchment()            # catchment over a new T- residence subtree; leaf = a location in it
session = make_session(members=[users["member"]], catchment_area=area)
patient = make_patient(krcs_residence=leaf)   # inside the catchment
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, random_string, today

from onerc_healthcare.constants import (
	CATCHMENT_AREA_DOCTYPE,
	OUTREACH_SESSION_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_HIS,
	ROLE_MIC,
	ROLE_OUTREACH,
	ROLE_RECEPTIONIST,
)
from onerc_healthcare.registration.residence import (
	ensure_residence_root,
	get_residence_levels,
	get_residence_root_name,
)
from onerc_healthcare.tests.factories import get_facility, make_user

OUTREACH_USERS = {
	"member": (ROLE_OUTREACH,),
	"member2": (ROLE_OUTREACH,),
	"outsider": (ROLE_OUTREACH,),  # outreach role, not on the session team
	"his": (ROLE_HIS,),
	"mic": (ROLE_MIC,),
	"reception": (ROLE_RECEPTIONIST,),
	"clinician": (ROLE_CLINICIAN,),
}


def outreach_users() -> dict[str, str]:
	"""One user per key: t-or-<key>@test.krcs with exactly the listed roles."""
	return {key: make_user(f"or-{key}", list(roles)) for key, roles in OUTREACH_USERS.items()}


def make_residence(name: str | None = None, parent: str | None = None, is_group: int = 1) -> str:
	"""Insert a T- Residence Location under `parent` (default: the operation root) at the next level down."""
	ensure_residence_root()
	root = get_residence_root_name()
	parent = parent or root
	levels = get_residence_levels()
	parent_level = frappe.db.get_value("Residence Location", parent, "level")
	idx = levels.index(parent_level) + 1 if parent_level in levels else 0
	level = levels[min(idx, len(levels) - 1)]
	doc = frappe.get_doc(
		{
			"doctype": "Residence Location",
			"location_name": name or f"T-Res-{random_string(6)}",
			"parent_residence_location": parent,
			"level": level,
			"is_group": is_group if idx < len(levels) - 1 else 0,
		}
	).insert(ignore_permissions=True)
	return doc.name


def make_catchment(facility: str | None = None) -> tuple[str, str]:
	"""A Catchment Area over a new T- top-level residence. Returns (area name, a location inside it)."""
	top = make_residence()
	levels = get_residence_levels()
	leaf = make_residence(parent=top) if len(levels) > 1 else top
	area = frappe.get_doc(
		{
			"doctype": CATCHMENT_AREA_DOCTYPE,
			"area_name": f"T-Area-{random_string(6)}",
			"facility": facility or get_facility(),
			"locations": [{"residence_location": top}],
		}
	).insert(ignore_permissions=True)
	return area.name, leaf


def make_session(
	members: list[str] | tuple[str, ...] = (),
	catchment_area: str | None = None,
	facility: str | None = None,
	departments: list[str] | tuple[str, ...] = ("Outpatient",),
	start_date: str | None = None,
	days: int = 2,
	**kw,
):
	"""Insert an Outreach Session (Planned). The first member is the team lead (default: t-or-member)."""
	members = list(members) or [make_user("or-member", [ROLE_OUTREACH])]
	facility = facility or get_facility()
	if not catchment_area:
		catchment_area, _leaf = make_catchment(facility)
	start = start_date or today()
	doc = frappe.get_doc(
		{
			"doctype": OUTREACH_SESSION_DOCTYPE,
			"title": f"T-Outreach {random_string(6)}",
			"facility": facility,
			"catchment_area": catchment_area,
			"start_date": start,
			"end_date": add_days(start, days - 1),
			"team": [{"user": u, "is_lead": int(i == 0)} for i, u in enumerate(members)],
			"departments": [
				{"department": d} for d in departments if frappe.db.exists("Medical Department", d)
			],
			**kw,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc
