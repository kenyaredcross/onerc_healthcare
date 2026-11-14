# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Device sync authentication (Phase 4, O1 owns this file; W0 stub).

`authenticate_device` runs for every request (hooks.auth_hooks). It must do nothing unless the request
carries `Authorization: krcs-outreach <grant>:<secret>` AND targets one of the outreach sync endpoints; for
those it validates the grant (not revoked, not expired, device active) and sets the session user.
"""


def authenticate_device() -> None:
	return None
