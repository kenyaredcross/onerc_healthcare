# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""privacy install hook (setup/install.py calls install() on after_install and after_migrate). Idempotent.

- Seeds one provisional English Consent Statement, Active, when the site has no English statement at
  all (an admin's statements, edits and retirements are never touched).
- Seeds Data Protection Settings `response_days` = 7 (provisional) when it has never been set.
"""

import frappe

from onerc_healthcare.constants import CONSENT_STATEMENT_DOCTYPE
from onerc_healthcare.privacy.dar import DEFAULT_RESPONSE_DAYS, SETTINGS_DOCTYPE
from onerc_healthcare.privacy.lifecycle import append_log
from onerc_healthcare.privacy.statements import ACTION_ACTIVATE, STATUS_ACTIVE

SEED_LANGUAGE = "en"
SEED_VERSION = "EN-1.0"
PROVISIONAL_NOTE = "PROVISIONAL: wording to be confirmed with ODPC guidance (OI-14)."
SEED_TEXT = f"""<p><strong>{PROVISIONAL_NOTE}</strong></p>
<p>The Kenya Red Cross Society (KRCS) health facility collects and keeps your personal and health
information, such as your name, identification numbers, contact details, medical history, test results
and treatment, so that we can give you care, follow up your treatment and refer you to other services
when needed.</p>
<p>Your information is kept confidential. Only staff involved in your care, and staff who manage health
records, can see it, and every access to your record is logged. We share it only when needed for your
care, when required by law, or as anonymous statistics for health reporting.</p>
<p>Under the Data Protection Act, 2019 you may ask to see your record, ask for a mistake to be
corrected, and withdraw this consent at any time. Withholding or withdrawing consent does not stop you
from receiving care.</p>
<p>Do you agree that we collect and use your information in this way?</p>"""


def install() -> None:
	seed_consent_statement()
	seed_settings()


def seed_consent_statement() -> None:
	if not frappe.db.table_exists(CONSENT_STATEMENT_DOCTYPE) or not frappe.db.exists(
		"Language", SEED_LANGUAGE
	):
		return
	if frappe.db.exists(CONSENT_STATEMENT_DOCTYPE, {"language": SEED_LANGUAGE}) or frappe.db.exists(
		CONSENT_STATEMENT_DOCTYPE, SEED_VERSION
	):
		return
	doc = frappe.get_doc(
		{
			"doctype": CONSENT_STATEMENT_DOCTYPE,
			"version": SEED_VERSION,
			"language": SEED_LANGUAGE,
			"status": STATUS_ACTIVE,
			"effective_date": frappe.utils.today(),
			"activated_at": frappe.utils.now_datetime(),
			"is_provisional": 1,
			"provisional_note": PROVISIONAL_NOTE,
			"text": SEED_TEXT,
		}
	)
	append_log(doc, None, STATUS_ACTIVE, ACTION_ACTIVATE, "Seeded on install (provisional, OI-14)")
	doc.flags.from_workflow = True
	doc.insert(ignore_permissions=True)


def seed_settings() -> None:
	if not frappe.db.table_exists("Singles") or not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
		return
	if frappe.db.get_single_value(SETTINGS_DOCTYPE, "response_days"):
		return
	frappe.db.set_single_value(SETTINGS_DOCTYPE, "response_days", DEFAULT_RESPONSE_DAYS)
