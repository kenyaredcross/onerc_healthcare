# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Provision lab orders for the laboratory (FR-15, FR-16, BR-5; W0 spike, Phase 2 brief fact 1).

Marley's `make_observation` returns early for every lab order of an encounter after the first, because
the encounter already has a Diagnostic Report (`service_request.py` `check_observation_sample_exist`).
The second and later orders then get no sample row and no Observation, so they never reach the lab.

`ensure_observations(service_request)` produces, for **every** lab order, what `make_observation` produces
for the first one, using Marley's own module-level building blocks:

| Template | Marley first-order result | Here |
|---|---|---|
| single, sample required | row on the encounter's draft Sample Collection (new one if none); the Observation is created when the sample is marked collected | same |
| single, no sample | root Observation (reference: the encounter) | same |
| panel | root Observation; Observations for non-sample components and sub-panels; one row for the panel on the Sample Collection when any component needs a sample | same |
| every order | one Diagnostic Report per encounter, linked to the Sample Collection | same |

It is idempotent: an order that already has an open sample row or a non-cancelled root Observation is
left alone. The Healthcare Settings "process only if paid" check is not applied (no billing, C-5).
Records are created as the system (``ignore_permissions``); callers check access first
(`lab.guards.make_observation`, or B1's send-lab-orders endpoint).
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from onerc_healthcare.lab.tasks import LAB_TEMPLATE_DT, SR_REVOKED

SAMPLE_COLLECTION = "Sample Collection"
SAMPLE_ROW = "Observation Sample Collection"


def is_lab_request(sr) -> bool:
	return sr.template_dt == LAB_TEMPLATE_DT and bool(sr.template_dn)


def ensure_observations(service_request: str) -> None:
	"""Give lab order `service_request` its sample row / Observations and the encounter its Diagnostic
	Report, exactly as Marley does for an encounter's first order. Idempotent; no-op for anything that is
	not a submitted, unrevoked lab order."""
	sr = frappe.get_doc("Service Request", service_request, for_update=True)
	if not is_lab_request(sr) or sr.docstatus != 1 or sr.status == SR_REVOKED:
		return

	sample_collection = None
	if not is_provisioned(sr):
		from onerc_healthcare.visit.patients import assert_patient_active

		assert_patient_active(sr.patient)
		template = frappe.get_doc("Observation Template", sr.template_dn)
		if template.has_component:
			sample_collection = _provision_panel(sr)
		elif template.sample_collection_required:
			sample_collection = _provision_sample_row(sr, template)
		else:
			_create_root_observation(sr)
	_ensure_diagnostic_report(sr, sample_collection or open_collection_name(sr))


def is_provisioned(sr) -> bool:
	"""The order already has a non-cancelled sample row or root Observation."""
	if frappe.db.exists(
		SAMPLE_ROW,
		{"service_request": sr.name, "parenttype": SAMPLE_COLLECTION, "docstatus": ["!=", 2]},
	):
		return True
	return bool(root_observation(sr))


def root_observation(sr) -> str | None:
	"""Latest non-cancelled root Observation of the order."""
	return frappe.db.get_value(
		"Observation",
		{
			"patient": sr.patient,
			"service_request": sr.name,
			"parent_observation": ["is", "not set"],
			"docstatus": ["!=", 2],
		},
		"name",
		order_by="creation desc",
	)


def sample_row_parent(sr) -> str | None:
	return frappe.db.get_value(
		SAMPLE_ROW,
		{"service_request": sr.name, "parenttype": SAMPLE_COLLECTION, "docstatus": ["!=", 2]},
		"parent",
		order_by="creation desc",
	)


def open_collection_name(sr) -> str | None:
	"""The encounter's open (draft) Sample Collection for this patient, as Marley looks it up."""
	return frappe.db.get_value(
		SAMPLE_COLLECTION,
		{
			"reference_doc": sr.source_doc,
			"reference_name": sr.order_group,
			"patient": sr.patient,
			"docstatus": 0,
		},
		"name",
		order_by="creation asc",
	)


def _open_collection(sr):
	"""The draft Sample Collection to append to (locked), or a new unsaved one (Marley's helper)."""
	from healthcare.healthcare.doctype.service_request.service_request import create_sample_collection

	name = open_collection_name(sr)
	if name:
		return frappe.get_doc(SAMPLE_COLLECTION, name, for_update=True)
	return create_sample_collection(frappe.get_doc("Patient", sr.patient), sr)


def _provision_sample_row(sr, template) -> str:
	"""Single sample-required test: a row on the encounter's draft Sample Collection."""
	from healthcare.healthcare.doctype.service_request.service_request import create_sample_collection

	name = open_collection_name(sr)
	if not name:
		# Marley's helper appends the row and saves.
		return create_sample_collection(frappe.get_doc("Patient", sr.patient), sr, None, template).name
	collection = frappe.get_doc(SAMPLE_COLLECTION, name, for_update=True)
	collection.append(
		"observation_sample_collection",
		{
			"observation_template": sr.template_dn,
			"sample": template.sample,
			"sample_type": template.sample_type,
			"container_closure_color": frappe.db.get_value(
				"Observation Template", sr.template_dn, "container_closure_color"
			),
			"uom": template.uom,
			"status": "Open",
			"sample_qty": template.sample_qty,
			"service_request": sr.name,
		},
	)
	collection.save(ignore_permissions=True)
	return collection.name


def _create_root_observation(sr):
	"""Marley's `create_observation`, inserted as the system: the clinician who sends the order has read-only
	access to Observation, and access to the order was checked by the caller."""
	doc = frappe.new_doc("Observation")
	doc.posting_datetime = now_datetime()
	doc.patient = sr.patient
	doc.company = sr.company
	doc.appointment = None
	doc.observation_template = sr.template_dn
	doc.reference_doctype = "Patient Encounter"
	doc.reference_docname = sr.order_group
	doc.service_request = sr.name
	doc.insert(ignore_permissions=True)
	return doc


def _provision_panel(sr) -> str | None:
	"""Panel: Marley's `make_observation` has_component branch, unchanged except for the early return."""
	import json

	from healthcare.healthcare.doctype.observation.observation import new_observation
	from healthcare.healthcare.doctype.observation_template.observation_template import (
		get_observation_template_details,
	)
	from healthcare.healthcare.doctype.sample_collection.sample_collection import (
		create_component_observations,
		set_component_observation_data,
	)
	from healthcare.healthcare.utils import create_non_sample_observations, has_sample_required_component

	observation = _create_root_observation(sr)
	kwargs = {
		"patient": sr.patient,
		"company": sr.company,
		"doc": "Patient Encounter",
		"docname": sr.order_group,
	}
	sample_reqd, non_sample_reqd = get_observation_template_details(sr.template_dn)
	for comp in non_sample_reqd:
		comp_has_component = frappe.db.get_value("Observation Template", comp, "has_component")
		if comp_has_component and has_sample_required_component(comp):
			continue  # a mixed sub-panel is chained below with its sample rows
		sub_observation = new_observation(template=comp, parent=observation.name, **kwargs)
		if comp_has_component:
			create_non_sample_observations(comp, sub_observation, kwargs)

	if not (sample_reqd or has_sample_required_component(sr.template_dn)):
		return None

	template = frappe.get_doc("Observation Template", sr.template_dn)
	data = set_component_observation_data(sr.template_dn)
	create_component_observations(
		sr.patient,
		sr.company,
		sr.practitioner,
		data,
		observation.name,
		{"doc": "Patient Encounter", "docname": sr.order_group},
	)
	collection = _open_collection(sr)
	collection.append(
		"observation_sample_collection",
		{
			"observation_template": sr.template_dn,
			"sample": template.sample,
			"sample_type": template.sample_type,
			"container_closure_color": template.container_closure_color,
			"component_observations": json.dumps(data),
			"uom": template.uom,
			"status": "Open",
			"sample_qty": template.sample_qty,
			"component_observation_parent": observation.name,
			"service_request": sr.name,
		},
	)
	collection.save(ignore_permissions=True)
	return collection.name


def _ensure_diagnostic_report(sr, sample_collection: str | None) -> None:
	"""One Diagnostic Report per encounter (Marley's `insert_diagnostic_report`), linked to the collection."""
	from healthcare.healthcare.doctype.service_request.service_request import insert_diagnostic_report

	report = frappe.db.get_value(
		"Diagnostic Report", {"ref_doctype": sr.source_doc, "docname": sr.order_group}, "name"
	)
	if not report:
		insert_diagnostic_report(sr, sample_collection)
	elif sample_collection and not frappe.db.get_value("Diagnostic Report", report, "sample_collection"):
		frappe.db.set_value("Diagnostic Report", report, "sample_collection", sample_collection)


def provisioned_target(sr) -> tuple[str, str] | None:
	"""Where the lab works on the order: (name, doctype) of its sample collection, root Observation or the
	encounter's Diagnostic Report — the shape Marley's `make_observation` returns."""
	parent = sample_row_parent(sr)
	if parent:
		return parent, SAMPLE_COLLECTION
	observation = root_observation(sr)
	if observation:
		return observation, "Observation"
	report = frappe.db.get_value(
		"Diagnostic Report", {"ref_doctype": sr.source_doc, "docname": sr.order_group}, "name"
	)
	if report:
		return report, "Diagnostic Report"
	return None
