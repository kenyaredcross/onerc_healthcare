# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Marley's workspaces open under their own sidebars (D26)."""

import frappe
from frappe.boot import get_bootinfo

from onerc_healthcare.boot import narrow_shared_workspaces
from onerc_healthcare.tests.base import EMRTestCase

# workspace -> the Marley sidebar whose Home row links it
OWN_SIDEBAR = {
	"Healthcare": "Healthcare",
	"Outpatient": "Outpatient",
	"Diagnostics": "Diagnostics",
	"Inpatient": "Inpatient",
	"Emergency": "Emergency",
	"Setup": "Healthcare Setup",
}


def module_for_workspace(module_sidebars: dict, name: str) -> str | None:
	"""Python twin of sidebar.js `module_for_workspace`: the first sidebar listing the workspace."""
	return next(
		(shell for shell, sb in module_sidebars.items() if name in (sb.get("workspaces") or [])), None
	)


class TestWorkspaceSidebars(EMRTestCase):
	def test_each_marley_workspace_opens_under_its_own_sidebar(self):
		sidebars = get_bootinfo().module_sidebars
		for workspace, sidebar in OWN_SIDEBAR.items():
			if sidebar in sidebars:
				self.assertEqual(module_for_workspace(sidebars, workspace), sidebar, workspace)

	def test_unlinked_and_other_app_workspaces_untouched(self):
		sidebars = {
			"A": {
				"app": "healthcare",
				"workspaces": ["X", "Y"],
				"items": [{"link_type": "Workspace", "link_to": "X"}],
			},
			"B": {"app": "healthcare", "workspaces": ["X", "Y"], "items": []},
			"C": {"app": "erpnext", "workspaces": ["X"], "items": []},
		}
		narrow_shared_workspaces(sidebars)
		self.assertEqual(sidebars["A"]["workspaces"], ["X", "Y"])
		self.assertEqual(sidebars["B"]["workspaces"], ["Y"])  # Y is linked by nobody, so it stays
		self.assertEqual(sidebars["C"]["workspaces"], ["X"])  # another app is never touched
