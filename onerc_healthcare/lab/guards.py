# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Override for Marley's `make_observation` (hooks.py `override_whitelisted_methods`). Owned by B2.

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
