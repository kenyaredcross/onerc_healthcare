# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Boot-time corrections to the desk navigation (D26).

Marley ships eight sidebars (Healthcare, Outpatient, Diagnostics, ...) that all belong to one module,
"Healthcare". Frappe 16 lists every workspace of a module on each of that module's sidebars, and the desk
opens a workspace under the FIRST sidebar listing it (``module_for_workspace`` in sidebar.js). So the
Healthcare app icon (``/desk/healthcare``) opened the Healthcare page under the alphabetically first
sidebar, Diagnostics.

``boot_session`` narrows each Marley sidebar's ``workspaces`` to the workspaces its own rows link, when
such a sidebar exists: Healthcare keeps "Healthcare", Diagnostics keeps "Diagnostics", and so on. Only
that lookup reads the list on the client. Nothing is stored; the boot payload alone is adjusted.
"""

MARLEY_APP = "healthcare"


def boot_session(bootinfo) -> None:
	narrow_shared_workspaces(bootinfo.get("module_sidebars") or {})


def narrow_shared_workspaces(module_sidebars: dict, app: str = MARLEY_APP) -> None:
	"""For each workspace listed by several of `app`'s sidebars, keep it only on the sidebars that
	link it by a row. A workspace no sidebar links is left alone."""
	sidebars = {shell: sb for shell, sb in module_sidebars.items() if sb.get("app") == app}
	listed: dict[str, list[str]] = {}
	for shell, sidebar in sidebars.items():
		for workspace in sidebar.get("workspaces") or []:
			listed.setdefault(workspace, []).append(shell)

	for workspace, shells in listed.items():
		if len(shells) < 2:
			continue
		owners = [shell for shell in shells if _links(sidebars[shell], workspace)]
		if not owners:
			continue
		for shell in shells:
			if shell not in owners:
				sidebars[shell]["workspaces"] = [w for w in sidebars[shell]["workspaces"] if w != workspace]


def _links(sidebar: dict, workspace: str) -> bool:
	return any(
		item.get("link_type") == "Workspace" and item.get("link_to") == workspace
		for item in sidebar.get("items") or []
	)
