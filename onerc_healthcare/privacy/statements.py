# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Consent Statement lifecycle (FR-37, SDD §5.10). Status changes only through this module.

| Action     | From   | To      | Roles                                         | Notes                                      |
|------------|--------|---------|-----------------------------------------------|--------------------------------------------|
| (insert)   | -      | Draft   | Medical In-Charge, EMR System Administrator   | DocPerm create; status forced to Draft      |
| `activate` | Draft  | Active  | Medical In-Charge, EMR System Administrator   | needs text; retires the language's Active   |
| `retire`   | Active | Retired | system (side effect of `activate`)            | never called by a user                      |

Invariants:
- At most one Active statement per language.
- A statement's text and language are locked once any Patient Consent references it; a new wording is
  a new version, and earlier consents keep the version they were given under.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime, strip_html, today

from onerc_healthcare.constants import CONSENT_DOCTYPE, CONSENT_STATEMENT_DOCTYPE, ROLE_MIC, ROLE_SYSADMIN
from onerc_healthcare.privacy.lifecycle import (
	IllegalTransitionError,
	Transition,
	append_log,
	check_from_status,
	check_role,
	clean_text,
	has_any_role,
	save_from_workflow,
)

STATUS_DRAFT = "Draft"
STATUS_ACTIVE = "Active"
STATUS_RETIRED = "Retired"
STATUSES = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_RETIRED)

ACTION_ACTIVATE = "activate"
ACTION_RETIRE = "retire"

STATEMENT_ADMIN_ROLES = (ROLE_MIC, ROLE_SYSADMIN)

TRANSITIONS: dict[str, Transition] = {
	ACTION_ACTIVATE: Transition(_("Activate"), (STATUS_DRAFT,), STATUS_ACTIVE, STATEMENT_ADMIN_ROLES),
	ACTION_RETIRE: Transition(_("Retire"), (STATUS_ACTIVE,), STATUS_RETIRED),
}

# Fields only this module may change after insert.
PROTECTED_FIELDS = ("status", "activated_at", "retired_at")
# Fields locked once a Patient Consent references the statement.
LOCKED_WHEN_USED = ("text", "language")


class ConsentStatementLockedError(frappe.ValidationError):
	"""The statement has been used by a Patient Consent; its wording can no longer change."""


def is_statement_used(statement: str) -> bool:
	return bool(frappe.db.exists(CONSENT_DOCTYPE, {"statement": statement}))


def get_active_statement(language: str = "en") -> str | None:
	return frappe.db.get_value(
		CONSENT_STATEMENT_DOCTYPE, {"status": STATUS_ACTIVE, "language": language}, "name"
	)


def _lock_language(language: str) -> None:
	"""Serialise activations per language (two concurrent activations cannot both stay Active)."""
	frappe.db.sql("SELECT `name` FROM `tabLanguage` WHERE `name`=%s FOR UPDATE", (language,))


def activate(statement: str, comment: str | None = None, system: bool = False):
	"""Draft -> Active; the language's previously Active statement becomes Retired. Returns the doc."""
	rule = TRANSITIONS[ACTION_ACTIVATE]
	comment = clean_text(comment, _("Comment"))
	doc = frappe.get_doc(CONSENT_STATEMENT_DOCTYPE, statement, for_update=True)
	check_role(rule, doc, system)
	check_from_status(rule, doc)
	if not strip_html(doc.text or "").strip():
		frappe.throw(_("Enter the statement text before activating it."), IllegalTransitionError)

	_lock_language(doc.language)
	for other in frappe.get_all(
		CONSENT_STATEMENT_DOCTYPE,
		filters={"language": doc.language, "status": STATUS_ACTIVE, "name": ("!=", doc.name)},
		pluck="name",
	):
		_retire(other, replaced_by=doc.name)

	from_status = doc.status
	doc.status = STATUS_ACTIVE
	doc.activated_at = now_datetime()
	doc.effective_date = doc.effective_date or today()
	append_log(doc, from_status, STATUS_ACTIVE, ACTION_ACTIVATE, comment)
	save_from_workflow(doc)
	return doc


def _retire(statement: str, replaced_by: str) -> None:
	rule = TRANSITIONS[ACTION_RETIRE]
	doc = frappe.get_doc(CONSENT_STATEMENT_DOCTYPE, statement, for_update=True)
	check_from_status(rule, doc)
	doc.status = STATUS_RETIRED
	doc.retired_at = now_datetime()
	append_log(doc, STATUS_ACTIVE, STATUS_RETIRED, ACTION_RETIRE, _("Replaced by {0}").format(replaced_by))
	save_from_workflow(doc)


def get_available_actions(doc, user: str | None = None) -> list[dict]:
	out = []
	for action, rule in TRANSITIONS.items():
		if rule.is_system or doc.status not in rule.from_statuses or not has_any_role(rule.roles, user):
			continue
		out.append({"action": action, "label": rule.label})
	return out


@frappe.whitelist(methods=["POST"])
def activate_statement(statement: str, comment: str | None = None) -> dict:
	"""Form button: activate a Draft statement."""
	doc = activate(statement, comment=comment)
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["GET"])
def get_active_statements() -> list[dict]:
	"""Active statements (one per language) with their text, for the Record Consent dialog."""
	frappe.has_permission(CONSENT_STATEMENT_DOCTYPE, "read", throw=True)
	return frappe.get_all(
		CONSENT_STATEMENT_DOCTYPE,
		filters={"status": STATUS_ACTIVE},
		fields=["name", "version", "language", "text", "is_provisional", "provisional_note"],
		order_by="language asc",
	)
