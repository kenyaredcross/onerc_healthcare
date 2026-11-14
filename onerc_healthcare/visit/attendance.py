# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""First attendance vs re-attendance (MOH 717; Phase 3 decision 2026-11-13). Owner: R3.

Rule: a visit is "New" when it is the patient's first visit to this facility in the calendar year,
otherwise "Re-attendance". "Seen here before (SmartCare)" at registration makes a would-be New visit a
re-attendance (basis: Reception override). Later corrections go through `engine.annotate`.

W0 stub: R3 implements `set_initial_attendance`. Keep the signature; it is called by the Patient Visit
controller on insert (after the patient checks) and when reception corrects the visit's patient.
"""


def set_initial_attendance(doc) -> None:
	"""Set doc.attendance_type / attendance_basis (and attendance_note) on an unsaved or corrected visit.

	Must not save, commit or touch other records.
	"""
	pass
