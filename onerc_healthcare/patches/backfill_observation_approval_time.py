# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 3 fix round (R4 review L1): give approved Observations their approval time (data migration).

KRCS approves results with status "Approved", and Marley fills ``time_of_approval`` only for "Final", so
submitted Observations had none and MOH lab counts fell back to ``modified``, which later writes (Marley's
``add_note``) move. New approvals are stamped at submit (`lab.events.stamp_approval_time`); this patch fills
the older ones with the best time available:

1. the creation time of the Version that recorded the submit (``docstatus`` 0 -> 1);
2. otherwise ``modified``.

Direct writes (``modified`` is left alone). Idempotent: only submitted Observations with an empty
``time_of_approval`` change, so a second run does nothing.
"""

import json

import frappe

BATCH = 500


def _submitted_at(names: list[str]) -> dict[str, object]:
	"""{observation: creation of its submit Version} for the Observations that have one."""
	out: dict[str, object] = {}
	versions = frappe.db.sql(
		"""SELECT docname, creation, data FROM `tabVersion`
		WHERE ref_doctype = 'Observation' AND docname IN %(names)s AND data LIKE %(like)s
		ORDER BY creation ASC""",
		{"names": names, "like": "%docstatus%"},
		as_dict=True,
	)
	for v in versions:
		if v.docname in out:
			continue
		try:
			changed = json.loads(v.data or "{}").get("changed") or []
		except ValueError:
			continue
		if any(list(row)[:3] == ["docstatus", 0, 1] for row in changed if isinstance(row, list | tuple)):
			out[v.docname] = v.creation
	return out


def execute():
	rows = frappe.db.sql(
		"""SELECT name, modified FROM `tabObservation`
		WHERE docstatus = 1 AND time_of_approval IS NULL ORDER BY name""",
		as_dict=True,
	)
	from_version = 0
	for start in range(0, len(rows), BATCH):
		batch = rows[start : start + BATCH]
		submitted = _submitted_at([r.name for r in batch])
		for r in batch:
			when = submitted.get(r.name)
			from_version += bool(when)
			frappe.db.sql(
				"UPDATE `tabObservation` SET time_of_approval = %s WHERE name = %s AND time_of_approval IS NULL",
				(when or r.modified, r.name),
			)
	if rows:
		print(
			f"onerc_healthcare: observation approval time backfill: {len(rows)} set"
			f" ({from_version} from the submit Version, {len(rows) - from_version} from modified)"
		)
