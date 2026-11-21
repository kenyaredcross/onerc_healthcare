# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Daily stock alerts (scheduler, hooks.py daily). Owned by S1.

To build: when a facility has alerts (below minimum level, expiring within EMR Stock Settings.expiry_alert_days,
expired still in stock, S11s waiting, outreach dispensing not deducted), a Notification Log to that facility's Store
Keepers and Pharmacists. The Stock Alerts report shares the queries.
"""


def daily() -> None:
	"""Scheduler entry point (hooks.py `scheduler_events["daily"]`). S1 stub."""
	return None
