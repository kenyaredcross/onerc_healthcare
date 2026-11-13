# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Clinician notifications for lab results (FR-17): an in-system Notification Log plus a realtime event.

Recipients are the requesting clinician (the order's Healthcare Practitioner user) and the user who
started the consultation (the visit's `start_consultation` log row). The Notification Log type is
"Alert", which Frappe never emails, so no patient data leaves the system.
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import ENCOUNTER_DOCTYPE, ROLE_MIC, VISIT_DOCTYPE

REALTIME_EVENT = "krcs_lab_results_ready"  # B1's encounter form listens for this
STATUS_LOG_DOCTYPE = "Patient Visit Status Log"


def clinician_users(service_request: str | None, visit: str | None) -> list[str]:
	users = []
	if service_request:
		practitioner = frappe.db.get_value("Service Request", service_request, "practitioner")
		if practitioner:
			users.append(frappe.db.get_value("Healthcare Practitioner", practitioner, "user_id"))
	if visit:
		users.append(
			frappe.db.get_value(
				STATUS_LOG_DOCTYPE,
				{"parent": visit, "parenttype": VISIT_DOCTYPE, "action": "start_consultation"},
				"user",
				order_by="idx desc",
			)
		)
	return [u for u in dict.fromkeys(users) if u and u not in ("Guest", "Administrator")]


def mic_users() -> list[str]:
	from frappe.utils.user import get_users_with_role

	return get_users_with_role(ROLE_MIC)


def notify(users: list[str], subject: str, encounter: str, payload: dict) -> None:
	"""Notification Log (deduplicated recipients) and realtime events for `users` and the open encounter."""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	users = [u for u in dict.fromkeys(users) if u]
	message = {"encounter": encounter, "subject": subject, **payload}
	if users:
		enqueue_create_notification(
			users,
			{
				"type": "Alert",
				"subject": subject,
				"document_type": ENCOUNTER_DOCTYPE,
				"document_name": encounter,
				"from_user": frappe.session.user,
			},
		)
		for user in users:
			frappe.publish_realtime(REALTIME_EVENT, message, user=user, after_commit=True)
	# An encounter form that is open refreshes its results section (FR-18).
	frappe.publish_realtime(
		REALTIME_EVENT, message, doctype=ENCOUNTER_DOCTYPE, docname=encounter, after_commit=True
	)


def result_ready(ctx, request: frappe._dict) -> None:
	subject = _("Lab result ready: {0} for {1}").format(request.template_dn, request.patient_name)
	notify(
		clinician_users(request.name, ctx.visit),
		subject,
		ctx.encounter,
		{"event": "ready", "service_request": request.name, "visit": ctx.visit},
	)


def result_rejected(ctx, request: frappe._dict, visit_closed: bool) -> None:
	subject = _("Lab result withdrawn: {0} for {1}; repeat pending").format(
		request.template_dn, request.patient_name
	)
	users = clinician_users(request.name, ctx.visit)
	if visit_closed:
		users += mic_users()
	notify(
		users,
		subject,
		ctx.encounter,
		{"event": "rejected", "service_request": request.name, "visit": ctx.visit},
	)
