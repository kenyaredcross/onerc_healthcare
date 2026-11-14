# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Overrides for Marley's Service Request endpoints (hooks.py `override_whitelisted_methods`). Owned by B2.

`make_lab_test` / `make_clinical_procedure` (unused legacy routes, D5) are Administrator-only;
`make_appointment` requires read on the order and always enforces permissions. `make_observation`:

Marley's endpoint creates records with ``ignore_permissions`` and checks nothing, and it stops after an
encounter's first lab order. This override:

- allows the Laboratory Technician, Clinical Officer, Mental Health Officer (and Administrator), with read
  access to the Service Request;
- provisions the order through `lab.orders.ensure_observations` (every order, not just the first);
- returns what Marley returns, so Marley's form scripts keep working: ``(name, doctype)`` when it created
  the records, ``(name, doctype, "New")`` when they already existed.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.constants import ROLE_CLINICIAN, ROLE_LAB, ROLE_MHO
from onerc_healthcare.lab.orders import (
	ensure_observations,
	is_lab_request,
	is_provisioned,
	provisioned_target,
)

MAKE_OBSERVATION_ROLES = (ROLE_LAB, ROLE_CLINICIAN, ROLE_MHO)


def check_make_observation_access(service_request: str) -> None:
	user = frappe.session.user
	if user != "Administrator" and not set(frappe.get_roles(user)) & set(MAKE_OBSERVATION_ROLES):
		frappe.throw(
			_("Only the Laboratory Technician or the clinician can create observations for a lab order."),
			frappe.PermissionError,
		)
	frappe.has_permission("Service Request", "read", doc=service_request, throw=True)


@frappe.whitelist(methods=["POST"])
def make_observation(service_request: str, appointment: str | None = None):
	if not service_request:
		return None
	check_make_observation_access(service_request)
	sr = frappe.get_doc("Service Request", service_request)
	if not is_lab_request(sr):
		frappe.throw(_("Service Request {0} is not a laboratory order.").format(service_request))
	if sr.docstatus != 1:
		frappe.throw(_("Service Request {0} must be submitted first.").format(service_request))
	existed = is_provisioned(sr)
	ensure_observations(sr.name)
	target = provisioned_target(sr)
	if not target:
		return None
	return (*target, "New") if existed else target


# --- Marley Service Request endpoints that map an order into a new record (Phase 2 review M8) ---------
# Marley's versions read the order with `get_cached_doc` and check nothing, so any desk user could pull
# order and patient details by name. The Lab Test / Clinical Procedure routes are unused (D5).


def _administrator_only() -> None:
	if frappe.session.user != "Administrator":
		frappe.throw(
			_("This route is not used in the EMR; laboratory orders are handled through Observations."),
			frappe.PermissionError,
		)


@frappe.whitelist()
def make_lab_test(service_request: str):
	_administrator_only()
	from healthcare.healthcare.doctype.service_request.service_request import make_lab_test as _orig

	return _orig(service_request)


@frappe.whitelist()
def make_clinical_procedure(service_request: str, appointment: str | None = None):
	_administrator_only()
	from healthcare.healthcare.doctype.service_request.service_request import (
		make_clinical_procedure as _orig,
	)

	return _orig(service_request, appointment)


@frappe.whitelist()
def make_appointment(
	source_name: str,
	target_doc: Document | dict | str | None = None,
	ignore_permissions: bool = False,
):
	"""Marley's mapper with permissions always enforced (the client cannot pass ignore_permissions)."""
	from healthcare.healthcare.doctype.service_request.service_request import make_appointment as _orig

	frappe.has_permission("Service Request", "read", doc=source_name, throw=True)
	return _orig(source_name, target_doc, ignore_permissions=False)
