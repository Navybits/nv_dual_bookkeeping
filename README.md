# NV Dual Bookkeeping

**Version:** 18.0.1.0.0
**Author:** NV
**License:** LGPL-3
**Depends:** `account`

Implements a dual-bookkeeping system for Lebanese companies by adding an
**Official (O) / Non-Official (NO)** flag to every financial transaction.
Each journal maintains two independent `ir.sequence` streams. The Official
sequence master always lives in the designated Official Company — operating
company journals borrow from it, ensuring gap-free, conflict-free numbering
across all companies.

---

## Installation

1. Copy the `nv_dual_bookkeeping` folder into your Odoo addons path.
2. Enable Developer Mode: **Settings → Activate Developer Mode**.
3. Update the app list: **Apps → Update Apps List**.
4. Search for **"NV Dual Bookkeeping"** and click **Install**.

During installation the `post_init_hook` runs automatically and creates a
Non-Official sequence for every existing journal. Check the server log for
a summary line:

```
nv_dual_bookkeeping post_init_hook complete. Total journals: X | NO sequences created: Y …
```

---

## Configuration

### Step 1 — Set the Official Mirror Company

**Settings → Accounting → Dual Bookkeeping → Official Mirror Company**

Select the company that will act as the authoritative Official Books ledger.
All Official transactions posted in the operating company will be mirrored here.

Alternatively, set it per-company in **Settings → Companies → [company] →
Official Company**.

### Step 2 — Link Journals

**Accounting → Dual Bookkeeping → Link Official Journals**

The wizard opens pre-populated with all journals of the current company.

| Button | Behaviour |
|--------|-----------|
| Auto-Match by Code | Matches journals where `code` is identical in both companies |
| Auto-Match by Name | Case-insensitive substring match on journal name |
| Confirm | Persists all mappings; shows linked / unmatched count |

A journal that is unlinked will fall back to its own local sequence for
Official transactions (a warning is shown on the journal form).

### Step 3 — Assign User Groups

Go to **Settings → Users** and assign:

| Group | Purpose |
|-------|---------|
| **Can Mark Transactions as Non-Official** | Allows toggling the O/NO flag to Non-Official on draft transactions |
| **Dual Bookkeeping Sync Manager** | Allows manual sync triggers and retry from the Sync Dashboard |

### Step 4 — Optional Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Default new transactions as Official | ✓ True | Pre-fills the O/NO toggle to Official on all new transactions |
| Require explicit O/NO selection before posting | False | A document whose flag was never set cannot be posted: the flag counts as set when the user clicks the toggle, when it comes from the contact, or when it is copied (Pay wizard, credit note, duplicate, mirror); documents created in code are never blocked |
| Auto-post mirrored transactions | False | Posts mirrors automatically; **leave False** for review workflow |
| Block direct posting in Official Company | False | Prevents any non-mirror entry being posted in the official company |

---

## Key Behaviours

### Sequence Routing

| Scenario | Sequence used |
|----------|--------------|
| Post Official transaction, journal has `official_journal_id` | Official company journal's `sequence_id` (master) |
| Post Official transaction, journal has no `official_journal_id` | Journal's own local `sequence_id` (fallback) |
| Post Non-Official transaction | Journal's `sequence_no_id` (local, always) |

This means:
- All Official entries share a single gapless counter per journal regardless of which company posts them.
- Non-Official entries have their own counter that never interferes.

### Sync Engine

When an Official transaction is posted in an operating company:

1. Guard checks run (not a mirror, official company configured).
2. A mirror `account.move` is created in the official company (sudo, official company);
   the source keeps a link to it (**Mirror in Official Books**).
3. Each account is matched by code in the official company's chart of accounts.
4. Each tax is matched by name and type (sale / purchase), then by amount when exactly
   one tax of that type fits; a missing or ambiguous tax fails the sync with its name
   (it is never dropped).
5. The partner is matched by VAT, then name, then auto-created as a stub.
6. A `nv.sync.log` record is written with `state='synced'` and its operation.
7. If `auto_post_mirror=True`, the mirror is posted automatically.

The mirror follows its source's life-cycle:

| Source | Mirror | Log operation |
|--------|--------|---------------|
| Reset to draft | reset to draft, without its reconciliations (a draft mirror is kept) | Reset to Draft |
| Posted again | the same mirror is rewritten (lines, amounts, rates, same number) and its reconciliations redone | Update |
| Cancelled | cancelled | Cancel |
| Deleted (draft) | deleted; refused while the mirror is posted | Delete |

A reset, cancel or delete is refused, before anything changes, when the posted mirror's
date is locked in the Official Books company.

**Sync failures are non-blocking.** If the sync engine raises any exception:
- The original posting is NOT rolled back.
- An error log is written to `nv.sync.log`.
- A sticky bus notification warns the user to check the Sync Dashboard.

### Locking

- `is_official` **cannot be changed** after a transaction is posted (enforced in `write()`).
- Setting `is_official=False` requires the **Can Mark Transactions as Non-Official** group.

---

## Sync Dashboard

**Accounting → Dual Bookkeeping → Sync Dashboard**

| Column | Description |
|--------|-------------|
| Sync Date | When the sync was attempted |
| Source Number | Number of the source (kept when the source is deleted) |
| Operation | Mirror / Update / Reset to Draft / Cancel / Delete |
| Source Transaction | Original move in the operating company (clickable) |
| Mirror Transaction | Created move in the official company (clickable) |
| State | Synced (green) / Error (red) / Pending (orange) |
| Error Message | Shown for failed syncs |

**Retry All Failed** (header button): re-triggers every error record.
**Retry** (per row): re-triggers a single record.

Both buttons are restricted to the **Dual Bookkeeping Sync Manager** group.

---

## Menus

All menus live under **Accounting → Dual Bookkeeping**:

| Menu | Description |
|------|-------------|
| Official Transactions | account.move list filtered to is_official=True |
| Non-Official Transactions | account.move list filtered to is_official=False |
| Sync Dashboard | nv.sync.log list with retry actions |
| Link Official Journals | Journal mapping wizard |
| Summary Report | Pivot: journal × O/NO with amount_total measure |

---

## Troubleshooting

### "Journal has no Official sequence configured"
The journal's `official_journal_id` is not set. Open the journal, go to the
**Dual Bookkeeping** tab, and set **Official Company Journal**, or use the
**Link Official Journals** wizard.

### "Account '{code}' not found in official company"
The chart of accounts in the official company is missing account `{code}`.
Install the same chart of accounts in both companies, or manually create the
missing account with the same code.

### "Tax '{name}' (...) has no match / several matches in the Official Books company"
The official company has no tax (or several) with the same name and type as the
source's. Create or rename the tax there with the same name and type, then click
**Retry** on the error record.

### Mirror not created / Sync Dashboard shows error
1. Open the error record in the Sync Dashboard.
2. Read the **Error Message** field for the root cause.
3. Fix the underlying issue (missing account, missing journal mapping, etc.).
4. Click **Retry** on the error record.

### post_init_hook failed for some journals
Check the server log for `[ERROR]` lines from `post_init_hook`. These indicate
journals where the NO sequence could not be created. Fix the issue and either:
- Re-install the module (safe — the hook is idempotent for already-migrated journals), or
- Manually call `journal._create_no_sequence()` from an Odoo shell.

### Sequence gaps appear in the Official sequence
This usually means a transaction was posted directly in the official company
without going through the sync engine. Enable **Block direct manual posting in
Official Company** in settings to prevent this going forward.

---

## File Structure

```
nv_dual_bookkeeping/
├── __init__.py                          # imports + post_init_hook
├── __manifest__.py
├── models/
│   ├── __init__.py
│   ├── account_journal.py               # sequence_no_id, official_journal_id, auto-creation
│   ├── account_move.py                  # is_official flag, sequence override, sync trigger
│   ├── account_payment.py               # is_official flag, locking
│   ├── res_company.py                   # official_company_id, is_official_company
│   ├── res_config_settings.py           # settings model extension
│   └── nv_sync_log.py                   # sync log model
├── services/
│   ├── __init__.py
│   └── sync_engine.py                   # NvSyncEngine AbstractModel
├── wizard/
│   ├── __init__.py
│   ├── nv_journal_link_wizard.py
│   └── views/
│       └── nv_journal_link_wizard_views.xml
├── views/
│   ├── account_journal_views.xml
│   ├── account_move_views.xml
│   ├── account_payment_views.xml
│   ├── res_company_views.xml
│   ├── nv_sync_log_views.xml
│   ├── res_config_settings_views.xml
│   └── dual_bookkeeping_menus.xml
├── security/
│   ├── ir.model.access.csv
│   └── nv_dual_bookkeeping_security.xml
├── data/
│   └── nv_dual_bookkeeping_data.xml
├── migrations/
│   └── 19.0.1.0.2/post-mirror-links.py  # links existing mirrors to their source
├── tests/                               # run with infra/test-module.sh ... 19
└── README.md
```

---

## Changelog

### 19.0.1.0.2 (backport of the Odoo 20 rebuild decisions D2, D6, D7)

- **D2 — the mirror follows its source**: reset to draft, re-post (the same mirror is
  rewritten), cancel and delete are propagated to the mirror, refused when the
  mirror's period is locked in the Official Books company, and logged with their
  operation. The migration links the existing mirrors to their source (from their
  sync log, else by number), so a re-post never creates a second mirror.
- **D6 — a missing tax fails the sync**: taxes matched by name and type, then by
  amount when unique; otherwise the sync fails with the tax's name (the mirror used
  to be created without the tax). Retry once the tax exists.
- **D7 — "Require explicit O/NO selection" works**: the flag of a new form is unset
  until the user clicks the toggle or picks a contact; documents created in code,
  copies and payments of the Pay wizard (each batch in its documents' stream) count
  as set. Documents already in the database are not affected.
