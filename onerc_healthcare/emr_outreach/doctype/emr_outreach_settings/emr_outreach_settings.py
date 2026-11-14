# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""EMR Outreach Settings (Single; D58 / OI-10 answers). HIS Officer and Medical In-Charge edit it; the EMR System
Administrator reads it.

`get_settings()` is the one reader: every key of `constants.OUTREACH_DEFAULTS`, falling back to the default when
the Single has never been saved (or a value is blank). Sync (O2) reads `max_batch` and `conflict_score` here.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from onerc_healthcare.constants import OUTREACH_DEFAULTS, OUTREACH_SETTINGS_DOCTYPE

# key -> (minimum, maximum) accepted on save
LIMITS = {
	"offline_session_days": (1, 30),
	"purge_after_days": (0, 90),
	"summary_visits": (0, 10),
	"pin_min_length": (4, 12),
	"max_pin_attempts": (3, 10),
	"max_batch": (1, 200),
	"package_max_patients": (1, 50000),
	"conflict_score": (1, 20),
}


class EMROutreachSettings(Document):
	def validate(self):
		for key, default in OUTREACH_DEFAULTS.items():
			if self.get(key) in (None, ""):
				self.set(key, default)
			low, high = LIMITS.get(key, (0, None))
			value = cint(self.get(key))
			if value < low or (high is not None and value > high):
				frappe.throw(
					_("{0} must be between {1} and {2}.").format(
						frappe.bold(_(self.meta.get_label(key))), low, high
					),
					title=_("Invalid Setting"),
				)


def get_settings() -> frappe._dict:
	"""All outreach settings as ints, defaults filled in (OUTREACH_DEFAULTS)."""
	stored = {}
	if frappe.db.exists("DocType", OUTREACH_SETTINGS_DOCTYPE):
		stored = dict(
			frappe.db.sql(
				"select field, value from `tabSingles` where doctype=%s",
				(OUTREACH_SETTINGS_DOCTYPE,),
			)
		)
	out = frappe._dict()
	for key, default in OUTREACH_DEFAULTS.items():
		value = stored.get(key)
		out[key] = cint(value) if value not in (None, "") else default
	return out
