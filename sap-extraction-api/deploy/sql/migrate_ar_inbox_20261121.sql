-- One-off migration (2026-11-21) for a database that already holds SAP rows loaded before VBRK
-- became the invoice source of the AR Inbox dashboard (active_bt3h, transactiontype = 'Invoice').
-- Applied on the dev DB dm00005. A fresh database does not need it.
-- Run AFTER deploy/sql/sales_invoice_themes.sql and with the new config.ini:
--   psql -h <db-host> -p <port> -U postgres -d dm00005 -f deploy/sql/migrate_ar_inbox_20261121.sql
--
--  1. IT_Invoice rows of SAP billing documents (written by the old sync-bt3i fallback from
--     active_bsid) are now supplied by VBRK: backed up to bak_20261121_it_invoice_sap_* and removed
--     (header in active_bt3h, items in active_bt3i, mirror rows in active_pa), so the dashboard
--     lists each invoice once. Only documents present in SAP_VBRK_SI are touched.
--  2. transactiontype: VBRK / VBRP SalesInvoice -> Invoice; BSID Invoice -> CUSTOMER_ACCOUNT
--     (an SD invoice is also an open BSID item; BSAD is CUSTOMER_ACCOUNT already).
--  3. VBRK header columns the dashboard reads, same values a new ingestion run writes:
--     basevalue = documentvalue = totaldocumentvalue (net + tax), sendername = KNA1 NAME1,
--     taxpercentage / tdsvalue / adjustedamount = 0, BELNR moved from linkref to linkref2.
-- Re-runnable: backups are created once, later runs find nothing to move.

BEGIN;

-- 1. duplicates written by sync-bt3i
CREATE TEMP TABLE _dup_ctx ON COMMIT DROP AS
SELECT DISTINCT i.grouptransactionref
FROM active_bt3h i
WHERE i.processcode = 'BT3H' AND i.themename = 'IT_Invoice' AND i.transactiontype = 'Invoice'
  AND i.grouptransactionref IS NOT NULL
  AND trim(i.documentnumber) IN (SELECT trim(documentnumber) FROM active_bt3h WHERE themename = 'SAP_VBRK_SI');

CREATE TABLE IF NOT EXISTS bak_20261121_it_invoice_sap_bt3h AS
SELECT * FROM active_bt3h WHERE themename = 'IT_Invoice' AND grouptransactionref IN (SELECT grouptransactionref FROM _dup_ctx);
CREATE TABLE IF NOT EXISTS bak_20261121_it_invoice_sap_bt3i AS
SELECT * FROM active_bt3i WHERE themename = 'IT_Invoice' AND grouptransactionref IN (SELECT grouptransactionref FROM _dup_ctx);
CREATE TABLE IF NOT EXISTS bak_20261121_it_invoice_sap_pa AS
SELECT * FROM active_pa WHERE themename = 'IT_Invoice' AND grouptransactionref IN (SELECT grouptransactionref FROM _dup_ctx);

DELETE FROM active_bt3i WHERE themename = 'IT_Invoice' AND grouptransactionref IN (SELECT grouptransactionref FROM _dup_ctx);
DELETE FROM active_pa   WHERE themename = 'IT_Invoice' AND grouptransactionref IN (SELECT grouptransactionref FROM _dup_ctx);
DELETE FROM active_bt3h WHERE themename = 'IT_Invoice' AND grouptransactionref IN (SELECT grouptransactionref FROM _dup_ctx);

-- 2. transaction types (the row key includes transactiontype; no row exists yet under the new type)
UPDATE active_bt3h SET transactiontype = 'Invoice'
WHERE processcode = 'BT3H' AND themename = 'SAP_VBRK_SI' AND transactiontype = 'SalesInvoice';
UPDATE active_bt3i SET transactiontype = 'Invoice'
WHERE processcode = 'BT3I' AND themename = 'SAP_VBRP_SI' AND transactiontype = 'SalesInvoice';
UPDATE active_bt3h SET transactiontype = 'CUSTOMER_ACCOUNT'
WHERE processcode = 'BT3H' AND themename = 'SAP_BSID_INV' AND transactiontype = 'Invoice';
UPDATE active_pa SET transactiontype = 'CUSTOMER_ACCOUNT'
WHERE themename = 'SAP_BSID_INV' AND transactiontype = 'Invoice';

-- 3. VBRK header values for the dashboard
UPDATE active_bt3h h
SET basevalue      = h.totaldocumentvalue,
    documentvalue  = h.totaldocumentvalue,
    taxpercentage  = 0,
    tdsvalue       = 0,
    adjustedamount = 0,
    sendername     = COALESCE((SELECT NULLIF(trim(k.name1::text), '') FROM active_kna1 k
                               WHERE k.kunnr::text = h.senderaccount::text AND k.name1 IS NOT NULL
                               ORDER BY k.name1 LIMIT 1), h.sendername),
    -- BELNR moves to linkref2 (SET reads the old values); a later UTR in linkref is kept on a re-run
    linkref2       = COALESCE(h.linkref2, h.linkref),
    linkref        = CASE WHEN h.linkref2 IS NULL OR h.linkref = h.linkref2 THEN NULL ELSE h.linkref END
WHERE h.processcode = 'BT3H' AND h.themename = 'SAP_VBRK_SI';

COMMIT;

-- Expected: backups 41 / 40 / 41, no IT_Invoice row left for a VBRK document,
-- SAP_VBRK_SI Invoice 76 with sendername and basevalue, SAP_VBRP_SI Invoice 105, SAP_BSID_INV CUSTOMER_ACCOUNT
SELECT 'bak bt3h' AS item, count(*) FROM bak_20261121_it_invoice_sap_bt3h
UNION ALL SELECT 'bak bt3i', count(*) FROM bak_20261121_it_invoice_sap_bt3i
UNION ALL SELECT 'bak pa', count(*) FROM bak_20261121_it_invoice_sap_pa
UNION ALL SELECT 'bt3h ' || themename || ' ' || transactiontype || ' (with name / base)',
                 count(*) FILTER (WHERE sendername IS NOT NULL AND basevalue IS NOT NULL)
FROM active_bt3h WHERE themename IN ('SAP_VBRK_SI', 'SAP_BSID_INV', 'IT_Invoice') GROUP BY themename, transactiontype
UNION ALL SELECT 'bt3i ' || themename || ' ' || transactiontype, count(*)
FROM active_bt3i WHERE themename = 'SAP_VBRP_SI' GROUP BY themename, transactiontype;
