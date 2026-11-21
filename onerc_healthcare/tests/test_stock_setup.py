# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 6S W0: stock settings, perpetual inventory off, stores per facility, medicines as batch stock items, the
Store Keeper role and the test factories (quantity only, no GL)."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate, today

from onerc_healthcare.constants import (
	ALL_ROLES,
	CLINICAL_ROLES,
	ERPNEXT_STOCK_SETTINGS,
	FACILITY_STORES,
	ITEM_GROUP_MEDICINES,
	ITEM_GROUP_REAGENTS,
	POINT_OF_USE_LABORATORY,
	ROLE_STORE_KEEPER,
	STOCK_ADJUSTMENT_DOCTYPE,
	STOCK_DEFAULTS,
	STOCK_ITEM_SHELF_LIFE_DAYS,
	STOCK_RECEIPT_DOCTYPE,
	STOCK_REQUISITION_DOCTYPE,
	STOCK_SETTINGS_DOCTYPE,
	STOCK_STATUS_LOG_DOCTYPE,
	STORE_TYPE_MAIN,
	STORE_TYPE_PHARMACY,
	STORE_TYPE_POINT_OF_USE,
	STORE_TYPE_TRANSIT,
	STORE_TYPES,
)
from onerc_healthcare.security import matrix
from onerc_healthcare.setup.operations import get_company, get_operation_config
from onerc_healthcare.stock import balances, postings
from onerc_healthcare.stock import setup as stock_setup
from onerc_healthcare.stock.batches import BatchExpiryMismatchError, batch_name, ensure_batch, find_batch
from onerc_healthcare.stock.stores import (
	StoreNotFoundError,
	facility_of_store,
	facility_stores,
	group_name,
	store_for,
	store_name,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility
from onerc_healthcare.tests.stock_factories import (
	make_s11,
	stock_item,
	stock_store,
	stock_users,
	store,
	top_up,
)

PARA = "Paracetamol 500 mg Tablet"


class TestStockSettings(EMRTestCase):
	def test_erpnext_stock_settings(self):
		for field, value in ERPNEXT_STOCK_SETTINGS.items():
			self.assertEqual(str(frappe.db.get_single_value("Stock Settings", field)), str(value), field)

	def test_perpetual_inventory_off(self):
		"""C-5: quantity only, ERPNext posts no GL for stock."""
		self.assertEqual(frappe.db.get_value("Company", get_company(), "enable_perpetual_inventory"), 0)

	def test_perpetual_inventory_refused_once_stock_exists(self):
		"""With stock posted, switching perpetual inventory off is impossible: install refuses loudly."""
		stock_store(PARA, 5, store())
		company = get_company()
		frappe.db.set_value("Company", company, "enable_perpetual_inventory", 1)
		try:
			with self.assertRaises(stock_setup.PerpetualInventoryError):
				stock_setup.disable_perpetual_inventory(company)
		finally:
			frappe.clear_document_cache("Company", company)
			getattr(frappe.local, "enable_perpetual_inventory", {}).pop(company, None)

	def test_fiscal_years_cover_today_and_next_year(self):
		for day in (today(), add_days(today(), 366)):
			self.assertTrue(
				frappe.db.get_value(
					"Fiscal Year",
					{"disabled": 0, "year_start_date": ("<=", day), "year_end_date": (">=", day)},
				),
				day,
			)
		self.assertEqual(stock_setup.ensure_fiscal_years(), [])

	def test_emr_stock_settings_default(self):
		self.assertEqual(
			frappe.db.get_single_value(STOCK_SETTINGS_DOCTYPE, "expiry_alert_days"),
			STOCK_DEFAULTS["expiry_alert_days"],
		)


class TestStores(EMRTestCase):
	def test_stores_seeded_per_facility(self):
		cfg = get_operation_config()
		abbr = cfg["abbr"]
		for i, facility in enumerate(cfg["facilities"]):
			hsu = get_facility(i)
			group = group_name(facility, abbr)
			self.assertEqual(
				frappe.db.get_value("Warehouse", group, ["is_group", "parent_warehouse"]),
				(1, f"All Warehouses - {abbr}"),
			)
			stores = facility_stores(hsu)
			self.assertEqual(len(stores), len(FACILITY_STORES))
			for label, store_type, point_of_use in FACILITY_STORES:
				name = store_name(facility, label, abbr)
				row = frappe.db.get_value(
					"Warehouse",
					name,
					["parent_warehouse", "company", "krcs_store_type", "krcs_point_of_use", "is_group"],
					as_dict=True,
				)
				self.assertEqual(
					(row.parent_warehouse, row.company, row.krcs_store_type, row.is_group),
					(group, cfg["company_name"], store_type, 0),
					name,
				)
				self.assertEqual(row.krcs_point_of_use or None, point_of_use, name)
				self.assertEqual(facility_of_store(name), hsu)
				self.assertEqual(store_for(hsu, point_of_use or store_type), name)

	def test_store_names(self):
		code = get_operation_config()["facilities"][0]["code"]
		abbr = get_operation_config()["abbr"]
		self.assertEqual(store(0, STORE_TYPE_PHARMACY), f"Pharmacy - {code} - {abbr}")
		self.assertEqual(store(0, STORE_TYPE_MAIN), f"Main Store - {code} - {abbr}")
		self.assertEqual(store(0, STORE_TYPE_TRANSIT), f"In Transit - {code} - {abbr}")
		self.assertEqual(store(0, POINT_OF_USE_LABORATORY), f"Laboratory - {code} - {abbr}")

	def test_store_for_refuses_ambiguous_or_unknown(self):
		with self.assertRaises(frappe.ValidationError):
			store_for(get_facility(0), STORE_TYPE_POINT_OF_USE)
		with self.assertRaises(frappe.ValidationError):
			store_for(get_facility(0), "Kitchen")
		self.assertIsNone(store_for("T-no such facility", STORE_TYPE_PHARMACY, required=False))
		with self.assertRaises(StoreNotFoundError):
			store_for("T-no such facility", STORE_TYPE_PHARMACY)

	def test_seed_is_idempotent_and_never_edits(self):
		pharmacy = store(0, STORE_TYPE_PHARMACY)
		frappe.db.set_value("Warehouse", pharmacy, "phone_no", "T-0700")
		before = frappe.db.count("Warehouse")
		stock_setup.install()
		self.assertEqual(frappe.db.count("Warehouse"), before)
		self.assertEqual(frappe.db.get_value("Warehouse", pharmacy, "phone_no"), "T-0700")

	def test_store_type_field_options(self):
		options = frappe.get_meta("Warehouse").get_field("krcs_store_type").options.split("\n")
		self.assertEqual([o for o in options if o], list(STORE_TYPES))


class TestStockItems(EMRTestCase):
	def test_medicines_are_batch_stock_items_with_expiry(self):
		items = frappe.get_all(
			"Item",
			filters={"item_group": ITEM_GROUP_MEDICINES},
			fields=[
				"name",
				"is_stock_item",
				"has_batch_no",
				"has_expiry_date",
				"create_new_batch",
				"shelf_life_in_days",
			],
		)
		self.assertGreaterEqual(len(items), 20)
		for item in items:
			self.assertEqual(
				(item.is_stock_item, item.has_batch_no, item.has_expiry_date, item.create_new_batch),
				(1, 1, 1, 0),
				item.name,
			)
			self.assertGreater(item.shelf_life_in_days, 0, item.name)
		self.assertEqual(stock_setup.ensure_stock_items(), {"converted": [], "skipped": []})

	def test_conversion_converts_new_group_items_and_skips_items_with_stock(self):
		item = stock_item("conv", ITEM_GROUP_REAGENTS)
		frappe.db.set_value("Item", item, {"has_expiry_date": 0, "shelf_life_in_days": 0})
		result = stock_setup.ensure_stock_items()
		self.assertIn(item, result["converted"])
		self.assertEqual(
			frappe.db.get_value("Item", item, ["has_expiry_date", "shelf_life_in_days"]),
			(1, STOCK_ITEM_SHELF_LIFE_DAYS),
		)
		stock_store(item, 3, store())
		frappe.db.set_value("Item", item, "has_expiry_date", 0)
		self.assertEqual(stock_setup.ensure_stock_items()["skipped"], [item])
		self.assertEqual(frappe.db.get_value("Item", item, "has_expiry_date"), 0)


class TestStockPostings(EMRTestCase):
	def test_stock_store_posts_quantity_without_gl(self):
		pharmacy = store()
		gl_before = frappe.db.count("GL Entry")
		batch = stock_store(PARA, 12, pharmacy, expiry=add_days(today(), 100), batch_no="T-PCM-1")
		self.assertEqual(batch, "T-PCM-1")
		self.assertEqual(balances.available_qty(PARA, pharmacy), 12)
		self.assertEqual(balances.actual_qty(PARA, pharmacy), 12)
		self.assertEqual(frappe.db.count("GL Entry"), gl_before)
		sle = frappe.get_all(
			"Stock Ledger Entry",
			filters={"item_code": PARA, "warehouse": pharmacy, "is_cancelled": 0},
			fields=["actual_qty", "valuation_rate", "stock_value_difference"],
		)
		self.assertEqual(
			[(s.actual_qty, s.valuation_rate, s.stock_value_difference) for s in sle], [(12, 0, 0)]
		)

	def test_fefo_skips_expired_and_picks_earliest_expiry(self):
		pharmacy = store()
		expired = stock_store(PARA, 50, pharmacy, expiry=add_days(today(), -1))
		late = stock_store(PARA, 10, pharmacy, expiry=add_days(today(), 300))
		early = stock_store(PARA, 4, pharmacy, expiry=add_days(today(), 30))
		batches = balances.available_batches(PARA, pharmacy)
		self.assertEqual([b.batch_no for b in batches], [early, late])
		self.assertEqual(balances.available_qty(PARA, pharmacy), 14)
		self.assertEqual(balances.actual_qty(PARA, pharmacy), 64)
		# A FEFO Material Issue of 6: 4 from the early batch, 2 from the late one, none of the expired batch.
		s11 = make_s11(items=((PARA, 6),)).name
		name = postings.post_stock_entry(
			postings.MATERIAL_ISSUE,
			[{"item_code": PARA, "qty": 6, "s_warehouse": pharmacy}],
			reference_doctype=STOCK_REQUISITION_DOCTYPE,
			reference_name=s11,
		)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", name, ["krcs_reference_doctype", "krcs_reference_name"]),
			(STOCK_REQUISITION_DOCTYPE, s11),
		)
		self.assertEqual(balances.batch_qty(early, pharmacy), 0)
		self.assertEqual(balances.batch_qty(late, pharmacy), 8)
		self.assertEqual(balances.batch_qty(expired, pharmacy), 50)
		# More than the unexpired balance: ERPNext refuses (no negative stock, expired never picked).
		with self.assertRaises(frappe.ValidationError):
			postings.post_stock_entry(
				postings.MATERIAL_ISSUE, [{"item_code": PARA, "qty": 9, "s_warehouse": pharmacy}]
			)
		postings.cancel_stock_entry(name)
		self.assertEqual(balances.available_qty(PARA, pharmacy), 14)

	def test_transfer_between_stores(self):
		main, transit = store(0, STORE_TYPE_MAIN), store(0, STORE_TYPE_TRANSIT)
		stock_store(PARA, 20, main)
		postings.post_stock_entry(
			postings.MATERIAL_TRANSFER,
			[{"item_code": PARA, "qty": 15, "s_warehouse": main, "t_warehouse": transit}],
		)
		self.assertEqual((balances.available_qty(PARA, main), balances.available_qty(PARA, transit)), (5, 15))

	def test_batches_by_item_and_printed_number(self):
		other = stock_item("batchclash")
		first = ensure_batch(PARA, "T-SAME", add_days(today(), 60))
		self.assertEqual(first, "T-SAME")
		self.assertEqual(ensure_batch(PARA, "T-SAME", add_days(today(), 60)), first)
		second = ensure_batch(other, "T-SAME", add_days(today(), 90))
		self.assertEqual(second, f"T-SAME ({other})")
		self.assertEqual((find_batch(PARA, "T-SAME"), find_batch(other, "T-SAME")), (first, second))
		self.assertEqual(batch_name(other, "T-SAME"), second)
		self.assertEqual(
			getdate(frappe.db.get_value("Batch", second, "expiry_date")), getdate(add_days(today(), 90))
		)
		with self.assertRaises(BatchExpiryMismatchError):
			ensure_batch(PARA, "T-SAME", add_days(today(), 61))

	def test_top_up_ignores_non_stock_items(self):
		from onerc_healthcare.tests.clinical_factories import get_test_item

		self.assertIsNone(top_up(get_test_item(), 5, store()))
		self.assertTrue(top_up(PARA, 5, store()))
		self.assertIsNone(top_up(PARA, 5, store()))


class TestStoreKeeperRole(EMRTestCase):
	def test_role_registered(self):
		self.assertIn(ROLE_STORE_KEEPER, ALL_ROLES)
		self.assertNotIn(ROLE_STORE_KEEPER, CLINICAL_ROLES)
		self.assertIn(ROLE_STORE_KEEPER, matrix.FACILITY_ROLES)
		self.assertEqual(
			frappe.db.get_value("Role", ROLE_STORE_KEEPER, ["desk_access", "home_page", "two_factor_auth"]),
			(1, "app/stores", 0),
		)

	def test_permissions(self):
		users = stock_users()
		sk, rec, ph, mic = users["storekeeper"], users["reception"], users["pharmacist"], users["mic"]

		def can(doctype, ptype, user):
			return bool(frappe.has_permission(doctype, ptype, user=user))

		self.assertTrue(can(STOCK_RECEIPT_DOCTYPE, "create", sk))
		self.assertTrue(can(STOCK_ADJUSTMENT_DOCTYPE, "create", sk))
		self.assertTrue(can(STOCK_REQUISITION_DOCTYPE, "write", sk))
		self.assertFalse(can(STOCK_REQUISITION_DOCTYPE, "create", sk))
		self.assertTrue(can(STOCK_REQUISITION_DOCTYPE, "create", ph))
		self.assertFalse(can(STOCK_RECEIPT_DOCTYPE, "create", mic))
		for doctype in (STOCK_RECEIPT_DOCTYPE, STOCK_REQUISITION_DOCTYPE, STOCK_ADJUSTMENT_DOCTYPE):
			self.assertFalse(can(doctype, "read", rec), doctype)
			self.assertTrue(can(doctype, "read", users["his"]), doctype)
			self.assertFalse(can(doctype, "delete", sk), doctype)
		for doctype in ("Stock Entry", "Stock Ledger Entry", "Stock Reconciliation"):
			self.assertTrue(can(doctype, "read", sk), doctype)
			self.assertTrue(can(doctype, "read", mic), doctype)
			self.assertFalse(can(doctype, "read", ph), doctype)
			for user in (sk, mic):
				self.assertFalse(can(doctype, "create", user), doctype)
				self.assertFalse(can(doctype, "submit", user), doctype)
		self.assertTrue(can("Item", "write", sk))
		self.assertFalse(can("Item", "create", sk))
		self.assertFalse(can("Item", "write", ph))
		self.assertTrue(can("Warehouse", "read", ph))
		self.assertTrue(can("Batch", "read", users["lab"]))
		# No clinical content for the Store Keeper
		for doctype in ("Patient", "Patient Encounter", "Medication Request", "Dispensing Record"):
			self.assertFalse(can(doctype, "read", sk), doctype)

	def test_shared_status_log(self):
		for doctype in (STOCK_RECEIPT_DOCTYPE, STOCK_REQUISITION_DOCTYPE, STOCK_ADJUSTMENT_DOCTYPE):
			field = frappe.get_meta(doctype).get_field("log")
			self.assertEqual((field.fieldtype, field.options), ("Table", STOCK_STATUS_LOG_DOCTYPE), doctype)

	def test_make_s11_factory(self):
		doc = make_s11(items=((PARA, 10),))
		self.assertEqual(
			(doc.status, doc.point_of_use, doc.items[0].qty_requested), ("Draft", "Pharmacy", 10)
		)
		self.assertTrue(
			doc.name.startswith(frappe.db.get_single_value("EMR Naming Settings", "id_prefix") + "-S11-")
		)


class TestDispenseHelpersStock(EMRTestCase):
	"""The existing dispense helpers stock the facility Pharmacy first (W0), so they keep working once S3 blocks
	short dispensing."""

	def test_pharmacy_helper_stocks_the_visit_facility_pharmacy(self):
		from onerc_healthcare.tests.test_pharmacy import PharmacyTestCase
		from onerc_healthcare.tests.visit_factories import make_visit_users

		helper = PharmacyTestCase()
		helper.users = make_visit_users()
		rx = helper.prescribed_visit()
		mr = rx.mrs[PARA]
		facility = frappe.db.get_value("Patient Visit", rx.visit, "facility")
		pharmacy = store_for(facility, STORE_TYPE_PHARMACY)
		self.assertEqual(balances.available_qty(PARA, pharmacy), 0)
		helper.dispense(mr, qty=2)
		self.assertGreaterEqual(balances.available_qty(PARA, pharmacy), 2)
