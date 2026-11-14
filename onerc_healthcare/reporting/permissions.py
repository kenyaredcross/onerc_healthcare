# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Frozen MOH report runs keep their files (review M2).

A Submitted, Superseded or Void run is frozen (`run.FROZEN_STATUSES`). Runs are not submittable, so
Frappe would still grant write on them from the DocPerm, and `File.has_permission` gives attach and
delete rights on a run's files to anyone who can write the run (and to each file's owner). Hooks
(wired by the integrator):

- ``has_permission["MOH Report Run"]`` → `run_has_permission`: write, create, cancel, submit and
  delete are refused on a frozen run. Reading is unchanged. (A ``has_permission`` hook can only deny:
  Frappe treats a falsy result, including None, as a refusal, so "no opinion" is True.)
- ``doc_events["File"]["before_insert"]`` → `file_before_insert` and ``["on_trash"]`` →
  `file_on_trash`: attaching a file to, or deleting a file from, a frozen run is refused for every user,
  whatever the File permission says. Patches (``frappe.flags.in_patch``) and the export code
  (``frappe.flags.krcs_moh_exporting``) are let through; exports are only built for runs that are not
  frozen anyway.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.constants import MOH_RUN_DOCTYPE

DENIED_ON_FROZEN = ("write", "create", "cancel", "submit", "delete")
EXPORT_FLAG = "krcs_moh_exporting"


class FrozenRunFileError(frappe.PermissionError):
	"""A file was attached to, or removed from, a Submitted, Superseded or Void run."""


def _frozen_statuses() -> tuple[str, ...]:
	from onerc_healthcare.reporting.run import FROZEN_STATUSES

	return FROZEN_STATUSES


def run_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	"""has_permission hook on MOH Report Run: no write-type rights on a frozen run."""
	if ptype not in DENIED_ON_FROZEN or not isinstance(doc, Document):
		return True
	if doc.is_new():
		return True  # a client-supplied status on a new run is ignored by the controller (always Draft)
	return doc.get("status") not in _frozen_statuses()


def _frozen_run_status(file_doc) -> str | None:
	"""Status of the frozen run `file_doc` is attached to, or None."""
	if file_doc.get("attached_to_doctype") != MOH_RUN_DOCTYPE or not file_doc.get("attached_to_name"):
		return None
	status = frappe.db.get_value(MOH_RUN_DOCTYPE, file_doc.attached_to_name, "status")
	return status if status in _frozen_statuses() else None


def _exempt() -> bool:
	return bool(frappe.flags.in_patch or frappe.flags.get(EXPORT_FLAG))


def file_before_insert(doc, method=None) -> None:
	"""doc_events File before_insert: nothing new is attached to a frozen run."""
	status = None if _exempt() else _frozen_run_status(doc)
	if status:
		frappe.throw(
			_("{0} is {1}: files cannot be attached to it. Revise it to make a correction.").format(
				doc.attached_to_name, _(status)
			),
			FrozenRunFileError,
			title=_("Not Allowed"),
		)


def file_on_trash(doc, method=None) -> None:
	"""doc_events File on_trash: a frozen run's files (exports, photos) are never removed."""
	status = None if _exempt() else _frozen_run_status(doc)
	if status:
		frappe.throw(
			_("{0} is {1}: its files cannot be removed.").format(doc.attached_to_name, _(status)),
			FrozenRunFileError,
			title=_("Not Allowed"),
		)
