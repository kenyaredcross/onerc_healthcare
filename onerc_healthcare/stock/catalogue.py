# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PROVISIONAL supplies and laboratory reagents catalogues (OI-9). Owned by S1.

`seed_catalogues()` is called by `stock.setup.install` on every migrate (W0 wiring). S1 seeds the Item Groups
`ITEM_GROUP_SUPPLIES` / `ITEM_GROUP_REAGENTS` and their Items (about 12 supplies, about 8 reagents) from a JSON data
file: exists-or-insert, never editing an existing Item, each created with `stock.setup.stock_item_values()`, not
sold or purchased, no price (C-5), the description carrying the provisional note (EMR Stock Settings).
"""


def seed_catalogues() -> list[str]:
	"""Create the missing provisional supplies and reagents Items. Returns the Items created. S1 stub."""
	return []
