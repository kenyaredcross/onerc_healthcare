# Stock contract (Phase 6S W0)

This is the contract between the W0 integrator and the Phase 6S builders: **S1** Receipts, adjustments, reports and alerts; **S2** S11 requisition; **S3** Dispensing deduction.
- Names and values come from `constants.py` (the "Phase 6S stock contract (W0)" block). Never hard-code them.
- Covers SDD §5.12.8 (stock card, Internal Requisition Form S11), OI-20 and tracker STO-01..03. The decision is **D61** (stock); the plan is `handover/stock_plan.md`. MCH took D62.
- **Quantity only** (C-5): no prices, valuation or GL. Never delete (NFR-9).
- **Do not change this file, the DocType field names or the signatures below without the integrator.** Builders may add fields only at the end of a section, and may add Select options at the end; no renames, no type changes. A builder who believes something must change stops and reports.

## 1. How stock works underneath

- **ERPNext's stock engine**: Warehouse, Item, Batch, Stock Entry, Stock Reconciliation, Stock Ledger Entry. Users never open ERPNext stock screens. Our documents post Stock Entries **server-side** through `stock.postings` (with `ignore_permissions`: no EMR role has create/submit on Stock Entry).
- **Perpetual inventory is off** on the operation's company (`stock.setup.disable_perpetual_inventory`), so no GL Entry is ever created and the difference-account / valuation / warehouse-account checks do not apply. Every row has rate 0 and `allow_zero_valuation_rate=1` (a blank rate would pull an Item Price). Once stock has been posted, ERPNext can no longer switch it off: migrate then **stops with `PerpetualInventoryError`** if it finds perpetual inventory on.
- **Stock Settings** enforced on every migrate (`ERPNEXT_STOCK_SETTINGS`): `enable_serial_and_batch_no_for_item=1`, `pick_serial_and_batch_based_on="Expiry"`, `auto_create_serial_and_batch_bundle_for_outward=1`, `allow_negative_stock=0`, `auto_indent=0`, `use_serial_batch_fields=1` (Marley's ward_stock throws without it).
- **FEFO**: an outward row with no batch is picked by ERPNext at submit, earliest expiry first; **expired and disabled batches are never picked** (as of today). Short stock raises ERPNext's `NegativeStockError` ("Insufficient Stock"), a `frappe.ValidationError`. That is the final guard; our pre-checks (`stock.balances`) give the friendly message first.
- **ERPNext refuses to receive an expired batch** (`erpnext.exceptions.BatchExpiredError`) on a Stock Entry. S1: a receipt line with a past expiry is refused by our own validation first; S1 must check what a Stock Reconciliation (Opening Count) does with expired stock and report.
- **Fiscal years**: `stock.setup.ensure_fiscal_years` keeps a July–June Fiscal Year for today and a year ahead.
- **Marley** creates Stock Entries only from flags we keep off (`manage_inpatient_medication_stock=0`, consumables off). `Company.default_warehouse` stays empty.
- **Lock order**: the caller's locks first (Visit → Medication Request for dispensing; the EMR stock document for S11 / receipts / adjustments, `SELECT ... FOR UPDATE`), then ERPNext locks the Bins itself during the Stock Entry submit.
- **Stock Entries carry the EMR document**: custom fields `krcs_reference_doctype` / `krcs_reference_name` on Stock Entry and Stock Reconciliation (`STOCK_REFERENCE_*_FIELD`). The Stock Card reads them to show GRN / S11 / DSP / ADJ references. Every posting must set them.

## 2. Roles and permissions

`ROLE_STORE_KEEPER = "Store Keeper"` is in `ALL_ROLES` and `security.matrix.FACILITY_ROLES` (reads the facility masters like every desk role), **not** in `CLINICAL_ROLES`: no clinical DocType rights at all. `ROLE_SETTINGS`: desk access, home page `app/stores` (the Stores workspace the integrator adds; until then the route 404s). Security fix-up **v6** (`krcs_security_fixups_v6`, `security.setup.apply_phase6_fixups`) applies those settings once on existing sites and restores the standard ERPNext roles' delete rights on the newly customised stock DocTypes (as v2 did for the Phase 1 masters; EMR roles never get delete). Demo user: **Lokiru Ekeno** (`lokiru.ekeno@krcs-demo.test`).

**Who may do what** (constants; builders check these in their endpoints, the JSON DocPerms only open the forms):

| Action | Roles (constant) |
|---|---|
| Receipt: create, post | Store Keeper (`RECEIPT_POSTER_ROLES`) |
| Receipt: void (reason) | Medical In-Charge (`RECEIPT_VOID_ROLES`) |
| S11: request, receive, cancel at Draft/Requested | per destination, `S11_REQUESTER_ROLES[point_of_use]`: Pharmacy → Pharmacist; Laboratory → Laboratory Technician; Maternity Ward → Midwife; Outpatient → Triage Nurse, Clinical Officer; Outreach → Medical In-Charge, HIS Officer (`ALL_S11_REQUESTER_ROLES` = all of them) |
| S11: approve (may lower quantities), reject (reason), cancel at Approved | Medical In-Charge (`S11_APPROVER_ROLES`) |
| S11: issue (may issue less, with a reason) | Store Keeper (`S11_ISSUER_ROLES`) |
| Adjustment: create, send for approval | Store Keeper (`ADJUSTMENT_CREATOR_ROLES`); for a Pharmacy store also the Pharmacist (`ADJUSTMENT_PHARMACY_CREATOR_ROLES`) |
| Adjustment: approve (post), reject | Medical In-Charge (`ADJUSTMENT_APPROVER_ROLES`) |
| Read everything | HIS Officer, Head of Operations (`STOCK_READ_ROLES`) |

**DocPerms in our JSON** (no delete, submit, cancel or amend for anyone; r = read+print+report, e = export):

| Role | Stock Receipt | Stock Requisition | Stock Adjustment | EMR Stock Settings |
|---|---|---|---|---|
| Store Keeper | r w c e | r w e | r w c e | r |
| Pharmacist | | r w c | r w c | r |
| Laboratory Technician, Midwife, Triage Nurse, Clinical Officer | | r w c | | |
| Medical In-Charge | r e | r w c e | r w e | r w |
| HIS Officer | r e | r w c e | r e | r |
| Head of Operations | r | r | r | |
| EMR System Administrator | | | | r w |

Row-level filtering (a requester sees the S11s of their own point of use / facility) is not in W0; S2 may add `permission_query_conditions` / `has_permission` hooks (report the hook lines to the integrator).

**ERPNext DocTypes** (`security/matrix.py`, Custom DocPerm; `STOCK_ACTORS` = Store Keeper, Pharmacist, Laboratory Technician, Midwife, Triage Nurse, Clinical Officer, Medical In-Charge, HIS Officer, Head of Operations):

| DocType | Rights |
|---|---|
| Item | read for `STOCK_ACTORS`; **write (no create)** for Store Keeper and HIS Officer: the ERPNext "new Item" form defaults to a sales item with a price (C-5), so new catalogue Items come from S1's seeded lists |
| Warehouse, Batch | read for `STOCK_ACTORS` |
| Stock Entry, Stock Ledger Entry, Stock Reconciliation, Serial and Batch Bundle | **read only**, Store Keeper and Medical In-Charge (drill-down). Nobody creates, submits or cancels them. |

The ERPNext **Stock** workspace stays restricted to the ERPNext stock roles (`test_security`: no EMR role on it). Our users work in the integrator's "Stores" area.

## 3. Stores

Seeded on every migrate by `stock.setup.seed_stores` (exists-or-insert; an existing store is never edited except that empty KRCS fields are filled). Per facility of the operation:

```
All Warehouses - KLB
└── Natukobenyo Stores - KLB                (group; "<facility short> Stores - <abbr>")
    ├── Main Store - NTK - KLB              Main Store
    ├── Pharmacy - NTK - KLB                Pharmacy
    ├── In Transit - NTK - KLB              In Transit
    ├── Laboratory - NTK - KLB              Point of Use / Laboratory
    ├── Maternity Ward - NTK - KLB          Point of Use / Maternity Ward
    ├── Outpatient - NTK - KLB              Point of Use / Outpatient
    └── Outreach - NTK - KLB                Outreach
```
Names: `"<label> - <facility code> - <company abbr>"` (Dadaab: "Pharmacy - IFO1 - DDB", group "IFO 1 Stores - DDB"). ERPNext's own "Stores - KLB", "Work In Progress - KLB" etc. are left alone. `warehouse_type` is not set (not even Transit for In Transit: ERPNext's "add to transit" flow is not used).

**Warehouse custom fields:** `krcs_facility` (Link → Healthcare Service Unit, the facility group), `krcs_store_type` (Select `STORE_TYPES`: Main Store, Pharmacy, In Transit, Point of Use, Outreach), `krcs_point_of_use` (Select `POINTS_OF_USE`: Laboratory, Maternity Ward, Outpatient; Point of Use stores only). Stores are always found by these fields, never by name.

**Balances:** Main Store, Pharmacy and Outreach hold stock (`STOCK_HOLDING_STORE_TYPES`). In Transit holds only issued-not-yet-received S11 quantities (and unresolved shortfalls). **Points of use consume on receipt**: an S11 received there is posted as a Material Issue out of In Transit (S2), so they never keep a balance.

### `stock.stores` (integrator-owned; import only)

| Function | Signature and result |
|---|---|
| `store_for` | `store_for(facility: str, store_type: str, required: bool = True) -> str \| None`. `facility` = facility group HSU name (e.g. `visit.facility`); `store_type` = a `STORE_TYPE_*` other than Point of Use, **or** a point-of-use name (so every `S11_DESTINATIONS` value resolves directly). Missing: `StoreNotFoundError` (a `ValidationError`) when `required`, else None. `"Point of Use"` alone or an unknown type throws. |
| `facility_of_store` | `facility_of_store(warehouse: str) -> str \| None` |
| `store_info` | `store_info(warehouse: str) -> {facility, store_type, point_of_use, disabled} \| None` |
| `facility_stores` | `facility_stores(facility: str) -> [{name, store_type, point_of_use}]` in tree order |
| `group_name` / `store_name` | `group_name(facility_row, abbr)`, `store_name(facility_row, label, abbr)` (operations.json rows; used by the seed) |

## 4. Items and batches

- **Every Item of `STOCK_ITEM_GROUPS`** ("KRCS Medicines", "KRCS Medical Supplies", "KRCS Laboratory Reagents") is a batch stock item with expiry: `STOCK_ITEM_VALUES` = `is_stock_item=1, has_batch_no=1, has_expiry_date=1, create_new_batch=0`, plus `shelf_life_in_days` (mandatory with expiry; default `STOCK_ITEM_SHELF_LIFE_DAYS = 730`; batches carry their own expiry). `stock.setup.stock_item_values(shelf_life_in_days=None) -> dict` gives the full set.
- `stock.setup.ensure_stock_items()` converts on **every migrate** (not a patch: fresh installs skip patches). An Item that already has stock transactions but is not set up that way is left alone and printed/logged (ERPNext forbids the change). Returns `{"converted": [...], "skipped": [...]}`.
- `pharmacy/catalogue.py _ensure_item` now creates medicine Items that way (it first applies the Stock Settings, since a fresh install seeds the catalogue before `stock.setup` runs). The stock UOM stays the dispensing unit ("Nos"). `Medication Linked Item.change_in_item=0` keeps Marley's Medication `on_update` from rewriting the Item.
- Non-catalogue Items (e.g. the test factory's non-stock `T-Paracetamol` drug) are untouched: dispensing them is "Not applicable" for stock (S3).
- The **supplies and reagents catalogues** are S1's: `stock.catalogue.seed_catalogues() -> list[str]` (called by `stock.setup.install` on every migrate; a stub now).
- **Minimum level per store** (plan): ERPNext Item Reorder rows (`warehouse`, `warehouse_reorder_level`), level only; `auto_indent` stays 0 (ERPNext shows an orange "enable auto re-order" message when saving such an Item; harmless). S1 owns reading them.

### `stock.batches` (W0 helper; S1 owns the file afterwards and keeps these signatures)

A Batch is identified by (Item, batch number as printed). ERPNext names a Batch by `batch_id`, unique across Items, so the Batch is named by the printed number when that name is free or already that Item's, otherwise `"<printed number> (<item code>)"`. One printed number of one Item has one expiry date.

| Function | Signature and result |
|---|---|
| `batch_name` | `batch_name(item: str, batch_no: str) -> str` (what it has or would get; creates nothing) |
| `find_batch` | `find_batch(item: str, batch_no: str) -> str \| None` |
| `ensure_batch` | `ensure_batch(item, batch_no, expiry_date, reference_doctype=None, reference_name=None) -> str`. Creates the Batch with `expiry_date` (required) if missing; an existing one with another expiry raises `BatchExpiryMismatchError`. Manufacturing date = today (or the day before an already past expiry). The printed number goes in the Batch description. |

## 5. Postings and balances

### `stock.postings` (integrator-owned; builders call it and never build Stock Entries themselves)

| Function | Signature and result |
|---|---|
| `make_stock_entry` | `make_stock_entry(purpose, items, reference_doctype=None, reference_name=None, posting_date=None, posting_time=None, remarks=None, company=None) -> Stock Entry` (not inserted) |
| `post_stock_entry` | `post_stock_entry(purpose, items, **same kwargs) -> str`: insert + submit (ignore_permissions), returns the name |
| `cancel_stock_entry` | `cancel_stock_entry(name: str) -> None`: cancels a submitted entry (no-op otherwise). ERPNext refuses when the stock has moved on (the reversal would go negative). |

`purpose` ∈ `MATERIAL_RECEIPT`, `MATERIAL_ISSUE`, `MATERIAL_TRANSFER`. Row: `{"item_code", "qty" (> 0), "s_warehouse", "t_warehouse", "batch_no"}`. Inward rows need `batch_no` (an existing Batch). Outward rows without `batch_no` are FEFO-picked at submit; with `batch_no` that batch is used. `reference_doctype` / `reference_name` must name an existing document (it is a Dynamic Link). Cancelling a Stock Entry that a **submitted** document links to is refused by Frappe; the Dispensing Record (submittable) is already cancelled (docstatus 2) when its `on_cancel` runs, so S3 cancels the entry there.

### `stock.balances` (W0 read helpers; S1 may add functions at the end)

| Function | Signature and result |
|---|---|
| `available_batches` | `available_batches(item, warehouse) -> [{batch_no, qty, expiry_date}]`: unexpired, enabled batches with stock, earliest expiry first (ERPNext's own FEFO picker, so it matches what a submit will pick) |
| `available_qty` | `available_qty(item, warehouse) -> float`: what can be issued today |
| `actual_qty` | `actual_qty(item, warehouse) -> float`: book balance, expired included (Bin) |
| `batch_qty` | `batch_qty(batch, warehouse) -> float`: one batch, expired or not |

## 6. DocTypes and naming

Module **EMR Stock**: DocTypes in `emr_stock/doctype/`, logic in `stock/`. Named through EMR Naming Settings (`naming.DEFAULT_RULES`).

| DocType | Constant | Kind | Series |
|---|---|---|---|
| Stock Receipt | `STOCK_RECEIPT_DOCTYPE` | parent | `{prefix}-GRN-.YYYY.-` |
| Stock Receipt Item | `STOCK_RECEIPT_ITEM_DOCTYPE` | child (`items`) | |
| Stock Requisition | `STOCK_REQUISITION_DOCTYPE` | parent (the S11) | `{prefix}-S11-.YYYY.-` |
| Stock Requisition Item | `STOCK_REQUISITION_ITEM_DOCTYPE` | child (`items`) | |
| Stock Adjustment | `STOCK_ADJUSTMENT_DOCTYPE` | parent | `{prefix}-ADJ-.YYYY.-` |
| Stock Adjustment Item | `STOCK_ADJUSTMENT_ITEM_DOCTYPE` | child (`items`) | |
| Stock Status Log | `STOCK_STATUS_LOG_DOCTYPE` | child shared by the three parents (table field **`log`**) | |
| EMR Stock Settings | `STOCK_SETTINGS_DOCTYPE` | Single | |

**Not submittable** (on purpose): each lifecycle has more than two states and several actors, and every stock movement is a separate submitted Stock Entry that the EMR document only links to. Status changes only through the lifecycle module (`privacy.lifecycle`: a module-level `TRANSITIONS` dict of `Transition`, `check_role`, `check_from_status`, `append_log`, `save_from_workflow`, `guard_fields`, `guard_log`). The shared log uses exactly `privacy.lifecycle.LOG_FIELDS`, and the table field is named `log`, so `append_log(doc, ...)` and `guard_log(doc, before)` work unchanged. The documents are **not** in `CLINICAL_DOCTYPES` (no `krcs_archived`; they are not clinical): nothing is deleted because no role has delete, and a wrong document is Cancelled / Rejected / Voided instead.

### 6.1 Coded values (constants)

| Value set | Constant | Values |
|---|---|---|
| Receipt status | `RECEIPT_STATUSES` | Draft, Posted, Void |
| Receipt source | `RECEIPT_SOURCES` | KEMSA, UNHCR, Donor, Other |
| S11 status | `S11_STATUSES` | Draft, Requested, Approved, Rejected, Issued, Received, Cancelled (`S11_OPEN_STATUSES` = Draft, Requested, Approved, Issued; `S11_WAITING_STATUSES` = Requested, Approved, Issued) |
| S11 destination | `S11_DESTINATIONS` | Pharmacy, Laboratory, Maternity Ward, Outpatient, Outreach |
| Adjustment type | `ADJUSTMENT_TYPES` | Opening Count, Physical Count, Write-off |
| Write-off reason | `WRITE_OFF_REASONS` | Expired, Damaged, Lost, Transit loss |
| Adjustment status | `ADJUSTMENT_STATUSES` | Draft, Pending Approval, Posted, Rejected |
| Dispensing stock status | `DISPENSING_STOCK_STATUSES` | Deducted, Not deducted (short), Not applicable |
| New Not Dispensed reason | `NOT_DISPENSED_OUT_OF_STOCK` | "Out of stock" |
| EMR Stock Settings defaults | `STOCK_DEFAULTS` | `expiry_alert_days` = 90 |
| Reports (S1) | `REPORT_STOCK_CARD`, `REPORT_STOCK_BALANCE`, `REPORT_STOCK_ALERTS` | "Stock Card", "Stock Balance (EMR)", "Stock Alerts" |

### 6.2 Fields

Generated from the JSON (the source of truth). Section and column breaks are left out.

#### Stock Receipt

| Field | Type | Notes |
|---|---|---|
| `naming_series` | Select | options: GRN-.YYYY.-; required; set once; default GRN-.YYYY.-; hidden |
| `facility` | Link → Healthcare Service Unit | required; set once |
| `store` | Link → Warehouse | read-only |
| `received_date` | Date | required; default Today |
| `status` | Select | options: Draft, Posted, Void; read-only; default Draft |
| `source` | Select | options: KEMSA, UNHCR, Donor, Other; required |
| `donor` | Data |  |
| `lpo_tracking_no` | Data |  |
| `delivery_note_no` | Data |  |
| `items` | Table → Stock Receipt Item | required |
| `remarks` | Small Text |  |
| `posted_by` | Link → User | read-only |
| `posted_at` | Datetime | read-only |
| `stock_entry` | Link → Stock Entry | read-only |
| `void_reason` | Small Text | read-only |
| `voided_by` | Link → User | read-only |
| `voided_at` | Datetime | read-only |
| `void_stock_entry` | Link → Stock Entry | read-only |
| `log` | Table → Stock Status Log | read-only |

#### Stock Receipt Item

| Field | Type | Notes |
|---|---|---|
| `item` | Link → Item | required |
| `item_name` | Data | read-only; fetched from `item.item_name` |
| `uom` | Data | read-only; fetched from `item.stock_uom` |
| `batch_no` | Data | required |
| `expiry_date` | Date | required |
| `qty` | Float | required |
| `batch` | Link → Batch | read-only |
| `remarks` | Small Text |  |

#### Stock Requisition

| Field | Type | Notes |
|---|---|---|
| `naming_series` | Select | options: S11-.YYYY.-; required; set once; default S11-.YYYY.-; hidden |
| `facility` | Link → Healthcare Service Unit | required; set once |
| `point_of_use` | Select | options: Pharmacy, Laboratory, Maternity Ward, Outpatient, Outreach; required; set once |
| `issue_store` | Link → Warehouse | read-only |
| `destination_store` | Link → Warehouse | read-only |
| `status` | Select | options: Draft, Requested, Approved, Rejected, Issued, Received, Cancelled; read-only; default Draft |
| `transit_shortfall` | Check | read-only |
| `items` | Table → Stock Requisition Item | required |
| `remarks` | Small Text |  |
| `requested_by` | Link → User | read-only |
| `requested_at` | Datetime | read-only |
| `approved_by` | Link → User | read-only |
| `approved_at` | Datetime | read-only |
| `issued_by` | Link → User | read-only |
| `issued_at` | Datetime | read-only |
| `received_by` | Link → User | read-only |
| `received_at` | Datetime | read-only |
| `issue_stock_entry` | Link → Stock Entry | read-only |
| `receive_stock_entry` | Link → Stock Entry | read-only |
| `reject_reason` | Small Text | read-only |
| `cancel_reason` | Small Text | read-only |
| `log` | Table → Stock Status Log | read-only |

#### Stock Requisition Item

| Field | Type | Notes |
|---|---|---|
| `item` | Link → Item | required |
| `item_name` | Data | read-only; fetched from `item.item_name` |
| `uom` | Data | read-only; fetched from `item.stock_uom` |
| `qty_requested` | Float | required |
| `qty_approved` | Float | read-only |
| `qty_issued` | Float | read-only |
| `qty_received` | Float | read-only |
| `remarks` | Small Text |  |
| `short_issue_reason` | Small Text | read-only |

#### Stock Adjustment

| Field | Type | Notes |
|---|---|---|
| `naming_series` | Select | options: ADJ-.YYYY.-; required; set once; default ADJ-.YYYY.-; hidden |
| `facility` | Link → Healthcare Service Unit | required; set once |
| `store` | Link → Warehouse | required; set once |
| `adjustment_type` | Select | options: Opening Count, Physical Count, Write-off; required; set once |
| `write_off_reason` | Select | options: Expired, Damaged, Lost, Transit loss |
| `status` | Select | options: Draft, Pending Approval, Posted, Rejected; read-only; default Draft |
| `adjustment_date` | Date | required; default Today |
| `items` | Table → Stock Adjustment Item | required |
| `remarks` | Small Text |  |
| `submitted_by` | Link → User | read-only |
| `submitted_at` | Datetime | read-only |
| `reject_reason` | Small Text | read-only |
| `approved_by` | Link → User | read-only |
| `approved_at` | Datetime | read-only |
| `stock_entry` | Link → Stock Entry | read-only |
| `stock_reconciliation` | Link → Stock Reconciliation | read-only |
| `log` | Table → Stock Status Log | read-only |

#### Stock Adjustment Item

| Field | Type | Notes |
|---|---|---|
| `item` | Link → Item | required |
| `item_name` | Data | read-only; fetched from `item.item_name` |
| `uom` | Data | read-only; fetched from `item.stock_uom` |
| `batch` | Link → Batch |  |
| `batch_no` | Data |  |
| `expiry_date` | Date |  |
| `current_qty` | Float | read-only |
| `counted_qty` | Float |  |
| `qty` | Float |  |
| `reason` | Small Text |  |

#### Stock Status Log

| Field | Type | Notes |
|---|---|---|
| `from_status` | Data | read-only |
| `to_status` | Data | required; read-only |
| `action` | Data | required; read-only |
| `user` | Link → User | required; read-only |
| `timestamp` | Datetime | required; read-only |
| `comment` | Small Text | read-only |

#### EMR Stock Settings

| Field | Type | Notes |
|---|---|---|
| `expiry_alert_days` | Int | required; default 90 |
| `provisional_catalogue_note` | Small Text | default PROVISIONAL (OI-9): the supplies and reagents lists await KRCS confirmation. |

#### Dispensing Record (existing; W0 added a collapsible "Stock" section before Cancellation)

| Field | Type | Notes |
|---|---|---|
| `warehouse` | Link → Warehouse | read-only, no copy; label "Store" |
| `stock_entry` | Link → Stock Entry | read-only, no copy |
| `stock_status` | Select | options: Deducted, Not deducted (short), Not applicable; read-only, no copy |

No logic: S3 sets them (the record is submitted, so use `db_set` or set them before submit).

## 7. Lifecycles (builders own the `TRANSITIONS`)

### 7.1 Stock Receipt (S1, `stock/receipt.py`)

| Action | From → To | Who |
|---|---|---|
| (insert) | → Draft | `RECEIPT_POSTER_ROLES` |
| `post` | Draft → Posted | `RECEIPT_POSTER_ROLES`: `store` = the facility Main Store; every line's Batch via `ensure_batch` (expiry required, not past) written to `batch`; one Material Receipt to the Main Store (`stock_entry`); `posted_by` / `posted_at` |
| `void` | Posted → Void | `RECEIPT_VOID_ROLES`, `void_reason` required: cancels `stock_entry` (only if none of that stock has moved on); `voided_by` / `voided_at`. `void_stock_entry` is reserved (stays empty while the cancel approach is used). |

### 7.2 Stock Requisition, S11 (S2, `stock/requisition.py`)

| Action | From → To | Who / effect |
|---|---|---|
| (insert) | → Draft | requester roles of `point_of_use` (the factory inserts with `flags.from_factory`) |
| `request` | Draft → Requested | requester roles; `requested_by/at`; `issue_store` = Main Store, `destination_store` = `store_for(facility, point_of_use)` |
| `approve` | Requested → Approved | `S11_APPROVER_ROLES`; `qty_approved` per line (≤ requested; may be lowered); `approved_by/at` |
| `reject` | Requested → Rejected | `S11_APPROVER_ROLES`; `reject_reason` |
| `issue` | Approved → Issued | `S11_ISSUER_ROLES`; `qty_issued` ≤ approved (less needs `short_issue_reason`); Material Transfer Main Store → In Transit, FEFO (`issue_stock_entry`); `issued_by/at` |
| `receive` | Issued → Received | requester roles of the destination; `qty_received` ≤ issued; In Transit → destination store, or a Material Issue out of In Transit for a Point of Use (consumed on receipt) (`receive_stock_entry`); a shortfall stays In Transit and sets `transit_shortfall` for the MIC to resolve with a Transit-loss write-off; `received_by/at` |
| `cancel` | Draft / Requested → Cancelled (requester roles); Approved → Cancelled (`S11_APPROVER_ROLES`) | `cancel_reason` |

Plus the S11 print format matching the paper form, and the form buttons.

### 7.3 Stock Adjustment (S1, `stock/adjustment.py`)

| Action | From → To | Who / effect |
|---|---|---|
| (insert) | → Draft | `ADJUSTMENT_CREATOR_ROLES`, or `ADJUSTMENT_PHARMACY_CREATOR_ROLES` for a Pharmacy store |
| `send_for_approval` | Draft → Pending Approval | same; sets `current_qty` per line; `submitted_by/at` |
| `approve` | Pending Approval → Posted | `ADJUSTMENT_APPROVER_ROLES`: counts → a Stock Reconciliation at rate 0 per batch (`stock_reconciliation`; an Opening Count line of a new batch uses `batch_no` + `expiry_date` through `ensure_batch`); write-off → a Material Issue of the named batches (`stock_entry`); `approved_by/at` |
| `reject` | Pending Approval → Rejected | `ADJUSTMENT_APPROVER_ROLES`; `reject_reason` |

### 7.4 Dispensing (S3, `stock/dispensing.py` + `emr_pharmacy/doctype/dispensing_record/dispensing_record.py`)

- `validate`, after `lock_line`: Dispensed + a batch stock item → `available_qty(dispensed_item, store_for(visit.facility, STORE_TYPE_PHARMACY))` must cover `quantity`, else `stock.dispensing.StockShortError` (defined in W0): "Only N in stock (Pharmacy - NTK - KLB). Dispense N or record Not Dispensed: out of stock."
- `on_submit`: Material Issue (FEFO), reference the Dispensing Record; set `warehouse`, `stock_entry`, `stock_status`.
- `on_cancel`: `cancel_stock_entry`; records without an entry are skipped.
- Outreach (`outreach/apply.py _dispense`): never fails on stock; deducts from `store_for(session facility, STORE_TYPE_OUTREACH)`, else `STOCK_NOT_DEDUCTED_SHORT`.
- `NOT_DISPENSED_OUT_OF_STOCK` in the Not Dispensed reason pick list.

## 8. Hooks and setup (W0, integrator-owned)

- `setup/install.py _install_workstreams`: `stock.setup.install()` after the pharmacy, front desk and privacy setups and before security.
- `hooks.py scheduler_events["daily"]`: `onerc_healthcare.stock.alerts.daily` (S1 stub).
- No doc_events: dispensing logic lives in the Dispensing Record controller (S3). Report any hook you need with its exact text.

## 9. Test factories (`tests/stock_factories.py`)

Builders add helpers only at the end of the file, and say so.

| Function | Signature and result |
|---|---|
| `stock_users` | `stock_users() -> {key: user}` for keys storekeeper, pharmacist, lab, midwife, triage, clinician, mic, his, headops, reception (`t-stk-<key>@test.krcs`, one role each; reception has no stock rights) |
| `store` | `store(facility_index: int = 0, store_type: str = STORE_TYPE_PHARMACY) -> str` |
| `stock_item` | `stock_item(name=None, group=ITEM_GROUP_SUPPLIES, uom="Nos") -> str`: a batch stock Item "T-<name>" (group created if missing) |
| `stock_store` | `stock_store(item, qty, store, expiry=None, batch_no=None) -> str` (the Batch name): **the canonical way to put stock in a test**, a Material Receipt at rate 0. Default expiry a year ahead; a past expiry gives an expired batch in stock (received unexpired, then its expiry moved back, since ERPNext refuses expired receipts). |
| `top_up` | `top_up(item, qty, store) -> str \| None`: adds the shortfall + 50 as a new batch when the unexpired balance is below `qty`; ignores non-stock Items |
| `stock_for_dispensing` | `stock_for_dispensing(medication_request, qty, item=None, store_type=STORE_TYPE_PHARMACY) -> str \| None`: tops up the visit facility's store for a dispense |
| `dispensed_item` | `dispensed_item(values: dict) -> str \| None`: the substituted Item of a Dispensing Record's values |
| `stock_outreach` | `stock_outreach(facility, medication, qty) -> str \| None`: tops up the facility's Outreach store |
| `make_s11` | `make_s11(items=((item, qty), ...), point_of_use=STORE_TYPE_PHARMACY, facility_index=0, user=None, **values) -> Stock Requisition`: a Draft, inserted with `flags.from_factory` and ignore_permissions |

**Existing dispense helpers now stock first** (no signature change): `tests/test_pharmacy.py PharmacyTestCase.dispense` (class attribute `auto_stock = True`; a shortage test sets it False and stocks the store itself), `tests/reporting_factories.py dispense`, both `dispense` helpers of `tests/test_state_machines.py`, the inline dispense in `tests/test_referral.py`, `tests/test_outreach_sync.py SyncCase.drug` (Outreach store, when `dispensed` is given) and `scripts/smoke_test.py` (step "19-", Administrator stocks the Outreach store over REST before the sync).

## 10. Ownership

| Builder | Exclusive files |
|---|---|
| **S1 Receipts, adjustments, reports, alerts, catalogues** | `emr_stock/doctype/{stock_receipt, stock_receipt_item, stock_adjustment, stock_adjustment_item, emr_stock_settings}/` (controllers, JS; field names fixed by §6.2); `stock/{receipt, adjustment, batches, catalogue, alerts, reports}.py` (+ `stock/data/` for the catalogue JSON); `emr_stock/report/{stock_card, stock_balance_emr, stock_alerts}/`; additions at the end of `stock/balances.py`; `tests/test_stock_receipt.py`, `tests/test_stock_adjustment.py`, `tests/test_stock_reports.py` |
| **S2 S11 requisition** | `emr_stock/doctype/{stock_requisition, stock_requisition_item}/` (controller, JS, form buttons); `emr_stock/print_format/` (the S11 print format); `stock/requisition.py`; `tests/test_stock_requisition.py` |
| **S3 Dispensing** | `stock/dispensing.py`; `emr_pharmacy/doctype/dispensing_record/dispensing_record.{py,js}` (the stock hook points and the "Out of stock" reason); `pharmacy/dispensing.py` if needed; the `_dispense` step of `outreach/apply.py`; additions to `tests/test_pharmacy.py` and `tests/test_outreach_*.py` (and a `tests/test_stock_dispensing.py` if preferred) |
| **Integrator** | `constants.py`, `hooks.py`, `stock/{setup, stores, postings}.py`, `stock/balances.py` (existing functions), `emr_stock/doctype/stock_status_log/`, `security/*`, `naming.py`, `setup/install.py`, `setup/demo.py`, `pharmacy/catalogue.py`, the desk (Stores area, dock, sidebars, workspace), `tests/stock_factories.py` (builders append only), `tests/test_stock_setup.py`, `scripts/smoke_test.py` |

Shared rules: `apps/onerc_healthcare/scripts/db_lock.sh` around every bench DB command; tests on `emr-test.localhost` only (`run_tests.sh`, never inside `db_lock.sh`); no delete; no prices (C-5); never `save()` a standard Marley document in developer mode; module-level `TRANSITIONS` + append-only `log`; the builders do not commit.
