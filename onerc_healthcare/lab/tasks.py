# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Laboratory task state of an encounter (FR-16, FR-17, BR-5).

A *lab request* is a Service Request of the encounter with ``template_dt == "Observation Template"``,
``docstatus == 1`` and a status other than revoked. Service Request status is never used for progress
(it is set to "completed" at sample collection, before any result exists); progress comes from the
Observations only.

A request is **done** when one of its root Observations (``service_request`` set, no
``parent_observation``, not cancelled) is submitted, or, for a panel, when every result-bearing component
under a non-cancelled root is submitted (at least one), and no sample row of the request is still open.
The second rule covers approving each component of a panel from the Diagnostic Report widget, which
leaves the panel's own Observation in draft.

A rejection cancels the Observation before Marley inserts its replacement draft, so while an Observation is
being cancelled its request counts as done only through a submitted root (``strict``).

The encounter's laboratory task is **complete** when every lab request is done (vacuously true when none
is left, for example after the last one is cancelled). `is_lab_complete` is the reconciler's checker
(``visit.tasks.TASK_CHECKERS["lab"]``).
"""

from __future__ import annotations

import frappe
from frappe.query_builder import DocType

from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_DOCTYPE,
	VISIT_ENCOUNTER_FIELD,
)

LAB_TEMPLATE_DT = "Observation Template"
SR_REVOKED = "revoked-Request Status"
SAMPLE_COLLECTED = "Collected"

# Progress labels shown on the worklist and in the encounter (FR-16, FR-18)
SAMPLE_NOT_REQUIRED = "Not required"
SAMPLE_NOT_REQUESTED = "Not sent to laboratory"
SAMPLE_AWAITING = "Awaiting collection"
SAMPLE_COLLECTED_LABEL = "Collected"

RESULT_APPROVED = "Approved"
RESULT_REPEAT = "Repeat pending"
RESULT_AWAITING_SAMPLE = "Awaiting sample"
RESULT_NOT_STARTED = "Not started"
RESULT_AWAITING_RESULT = "Awaiting result"
RESULT_PARTIAL = "Results in progress"
RESULT_AWAITING_APPROVAL = "Awaiting approval"

_OBS_FIELDS = (
	"name",
	"parent_observation",
	"service_request",
	"docstatus",
	"has_component",
	"status",
	"result",
	"observation_template",
	"creation",
)


def active_lab_requests(encounter: str) -> list[frappe._dict]:
	"""The encounter's lab Service Requests that still count (submitted, not revoked), oldest first."""
	return frappe.get_all(
		"Service Request",
		filters={
			"order_group": encounter,
			"template_dt": LAB_TEMPLATE_DT,
			"docstatus": 1,
			"status": ["!=", SR_REVOKED],
		},
		fields=[
			"name",
			"patient",
			"patient_name",
			"template_dn",
			"practitioner",
			"order_date",
			"order_time",
			"creation",
			"sample_collection_required",
		],
		order_by="creation asc",
	)


def encounter_for_visit(visit: str) -> str | None:
	"""The visit's encounter (W0 `encounter` link, else B1's `krcs_visit` back-link)."""
	encounter = frappe.db.get_value(VISIT_DOCTYPE, visit, VISIT_ENCOUNTER_FIELD)
	if encounter:
		return encounter
	if frappe.get_meta(ENCOUNTER_DOCTYPE).has_field(ENCOUNTER_VISIT_FIELD):
		return frappe.db.get_value(
			ENCOUNTER_DOCTYPE, {ENCOUNTER_VISIT_FIELD: visit, "docstatus": ["!=", 2]}, "name"
		)
	return None


def needs_sample(template: str) -> bool:
	"""The template (or, for a panel, a component at any depth) needs a sample."""
	from healthcare.healthcare.utils import has_sample_required_component

	row = frappe.get_cached_value(
		"Observation Template", template, ["sample_collection_required", "has_component"], as_dict=True
	)
	if not row:
		return False
	if row.sample_collection_required:
		return True
	return bool(row.has_component and has_sample_required_component(template))


class EncounterLab:
	"""Snapshot of an encounter's lab requests, their Observation trees and sample rows.

	``lock=True`` re-reads the relevant Observations with ``SELECT ... FOR UPDATE SKIP LOCKED`` (by primary
	key, so only this encounter's rows are locked), so the decision uses the latest committed state. Call it
	after the visit row is locked (lock order Visit -> Encounter -> Observation).

	SKIP LOCKED: Frappe's ``check_if_latest`` locks the Observation being submitted *before* any hook runs,
	so a concurrent approval on the same encounter holds its own row while it waits for the visit lock this
	transaction holds. Waiting for that row would deadlock. A row locked elsewhere is an approval in flight:
	it counts as not done here (conservative), and that transaction decides once it gets the visit lock.
	Rows changed elsewhere since this transaction's snapshot give a retryable ER_CHECKREAD
	(``innodb_snapshot_isolation``), never a wrong decision; the reconciler covers any miss.
	"""

	def __init__(self, encounter: str, lock: bool = False):
		self.encounter = encounter
		self.requests = active_lab_requests(encounter)
		self.request_names = [r.name for r in self.requests]
		self.rows: dict[str, frappe._dict] = {}
		self.children: dict[str, list[str]] = {}
		self.sample_rows: dict[str, list[frappe._dict]] = {}
		if not self.requests:
			return
		self.patient = self.requests[0].patient
		self._load_observations(lock)
		self._load_sample_rows()

	# -- loading -------------------------------------------------------------------------------------

	def _load_observations(self, lock: bool) -> None:
		rows = frappe.get_all(
			"Observation",
			filters={"patient": self.patient},
			fields=list(_OBS_FIELDS),
			order_by="creation asc",
		)
		self.rows = {r.name: r for r in rows}
		for r in rows:
			if r.parent_observation:
				self.children.setdefault(r.parent_observation, []).append(r.name)
		if lock:
			names = self._tree_names()
			if names:
				fresh = self._locked_rows(names)
				for f in fresh:
					self.rows[f.name].update(f)
				for name in set(names) - {f.name for f in fresh}:
					self.rows[name].docstatus = 0  # locked by an approval in flight: not done yet

	@staticmethod
	def _locked_rows(names: list[str]) -> list[frappe._dict]:
		"""Latest committed state of `names`, locked; rows locked by another transaction are skipped."""
		obs = DocType("Observation")
		return (
			frappe.qb.from_(obs)
			.select(obs.name, obs.docstatus, obs.status, obs.result)
			.where(obs.name.isin(names))
			.for_update(skip_locked=True)
			.run(as_dict=True)
		)

	def _tree_names(self) -> list[str]:
		requests = set(self.request_names)
		names = []
		stack = [
			n for n, r in self.rows.items() if r.service_request in requests and not r.parent_observation
		]
		while stack:
			name = stack.pop()
			names.append(name)
			stack.extend(self.children.get(name, ()))
		return names

	def _load_sample_rows(self) -> None:
		collections = frappe.get_all(
			"Sample Collection",
			filters={"patient": self.patient, "docstatus": ["!=", 2]},
			pluck="name",
		)
		if not collections:
			return
		rows = frappe.get_all(
			"Observation Sample Collection",
			filters={
				"parent": ["in", collections],
				"parenttype": "Sample Collection",
				"service_request": ["in", self.request_names],
			},
			fields=["service_request", "status", "parent"],
			order_by="creation asc",
		)
		for row in rows:
			self.sample_rows.setdefault(row.service_request, []).append(row)

	# -- state ---------------------------------------------------------------------------------------

	def _docstatus(self, name: str, overrides: dict | None) -> int:
		if overrides and name in overrides:
			return overrides[name]
		return self.rows[name].docstatus

	def roots(self, request: str, overrides: dict | None = None) -> list[frappe._dict]:
		"""Non-cancelled root Observations of `request`, oldest first."""
		return [
			r
			for r in self.rows.values()
			if r.service_request == request
			and not r.parent_observation
			and self._docstatus(r.name, overrides) != 2
		]

	def leaves(self, root: str, overrides: dict | None = None) -> list[frappe._dict]:
		"""Non-cancelled result-bearing Observations under `root` (any depth)."""
		out, stack = [], list(self.children.get(root, ()))
		while stack:
			name = stack.pop()
			if self._docstatus(name, overrides) == 2:
				continue
			row = self.rows[name]
			if row.has_component:
				stack.extend(self.children.get(name, ()))
			else:
				out.append(row)
		return out

	def has_open_sample(self, request: str) -> bool:
		return any(r.status != SAMPLE_COLLECTED for r in self.sample_rows.get(request, ()))

	def is_done(self, request: str, overrides: dict | None = None, strict: bool = False) -> bool:
		roots = self.roots(request, overrides)
		if any(self._docstatus(r.name, overrides) == 1 for r in roots):
			return True
		if strict or self.has_open_sample(request):
			return False
		for root in roots:
			if root.has_component:
				leaves = self.leaves(root.name, overrides)
				if leaves and all(self._docstatus(leaf.name, overrides) == 1 for leaf in leaves):
					return True
		return False

	def states(self, overrides: dict | None = None, strict: tuple | set = ()) -> dict[str, bool]:
		return {r: self.is_done(r, overrides, strict=r in strict) for r in self.request_names}

	def is_complete(self, overrides: dict | None = None, strict: tuple | set = ()) -> bool:
		return all(self.states(overrides, strict).values())

	def request_of(self, observation: str) -> str | None:
		"""The lab request whose tree contains `observation` (via its root)."""
		name, seen = observation, set()
		while name and name in self.rows and name not in seen:
			seen.add(name)
			row = self.rows[name]
			if not row.parent_observation:
				return row.service_request if row.service_request in self.request_names else None
			name = row.parent_observation
		return None

	# -- progress labels (worklist, encounter results) ------------------------------------------------

	def sample_status(self, request: str, template: str) -> tuple[str, str | None]:
		"""(label, Sample Collection) for `request`."""
		rows = self.sample_rows.get(request)
		if rows:
			open_rows = [r for r in rows if r.status != SAMPLE_COLLECTED]
			row = open_rows[0] if open_rows else rows[-1]
			return (SAMPLE_AWAITING if open_rows else SAMPLE_COLLECTED_LABEL), row.parent
		if not needs_sample(template):
			return SAMPLE_NOT_REQUIRED, None
		if any(r.service_request == request and r.parent_observation for r in self.rows.values()):
			# Collected on a Sample Collection that was later cancelled: the results stand.
			return SAMPLE_COLLECTED_LABEL, None
		return SAMPLE_NOT_REQUESTED, None

	def result_status(self, request: str, template: str) -> tuple[str, str | None]:
		"""(label, root Observation) for `request`, from its Observations only."""
		roots = self.roots(request)
		root = roots[-1].name if roots else None
		if self.is_done(request):
			return RESULT_APPROVED, root
		rejected = any(
			r.docstatus == 2 and r.service_request == request and r.status == "Rejected"
			for r in self.rows.values()
		)
		if rejected:
			# Marley's repeat copy carries the rejected value, so "has a result" means nothing here.
			return RESULT_REPEAT, root
		if not roots:
			return (RESULT_AWAITING_SAMPLE if needs_sample(template) else RESULT_NOT_STARTED), None
		bearing = [r for root_row in roots for r in self._result_rows(root_row)]
		with_result = [r for r in bearing if _has_value(r.result)]
		if not with_result:
			return RESULT_AWAITING_RESULT, root
		if len(with_result) < len(bearing) or self.has_open_sample(request):
			return f"{RESULT_PARTIAL} ({len(with_result)}/{len(bearing)})", root
		return RESULT_AWAITING_APPROVAL, root

	def _result_rows(self, root: frappe._dict) -> list[frappe._dict]:
		if not root.has_component:
			return [root]
		return self.leaves(root.name)


def _has_value(value) -> bool:
	from healthcare.healthcare.api.nursing_common import has_value

	return bool(has_value(value))


def is_lab_complete(visit: str) -> bool:
	"""Reconciler checker (`visit.tasks.TASK_CHECKERS["lab"]`): every lab request of the visit's encounter
	is done. False when the visit has no encounter."""
	encounter = encounter_for_visit(visit)
	if not encounter:
		return False
	return EncounterLab(encounter).is_complete()
