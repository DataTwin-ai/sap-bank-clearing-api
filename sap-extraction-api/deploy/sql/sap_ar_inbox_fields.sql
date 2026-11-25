-- AR Inbox fields for SAP open / cleared customer items in active_bt3h
-- (themes SAP_BSID_INV and SAP_BSAD_CA, filled by the 1-minute SAP sync).
--
-- The AR Inbox query reads sendername, basevalue, documentvalue, tdsvalue, adjustedamount and
-- documentref. The sync adds these source columns to every staged row (container.ini enrich_bsid /
-- enrich_bsad, ingestion.py _enrich_staging):
--   customer_name   KNA1-NAME1 of KUNNR          -> SenderName
--   dmbtr           amount in company currency   -> BaseValue, DocumentValue
--   local_currency  T001-WAERS of BUKRS           -> Currency, LocalCurrency   (dmbtr is in this currency;
--                                                    TransactionCurrency stays WAERS = document currency)
--   tds_value, adjusted_amount = 0                -> TDSValue, AdjustedAmount
--   doc_ref         XBLNR, else BELNR             -> DocumentRef
--
-- Part 2 fills the same fields once for rows already in active_bt3h (needs sap_ref_customer /
-- sap_ref_company, which the sync creates; run `python cli.py delta` once first if they are missing).
-- Re-runnable.

BEGIN;

-- 1. theme mapping ------------------------------------------------------------------------------
DELETE FROM theme_mapping
WHERE processcode = 'BT3H' AND themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA')
  AND lower(targetfieldname) IN ('sendername', 'basevalue', 'documentvalue', 'tdsvalue', 'adjustedamount',
                                 'currency', 'localcurrency', 'documentref');

WITH m (targetfieldname, targetfielddatatype, sourcefieldname) AS (
    VALUES
        ('SenderName',     'VARCHAR', 'customer_name'),
        ('BaseValue',      'FLOAT',   'dmbtr'),
        ('DocumentValue',  'FLOAT',   'dmbtr'),
        ('TDSValue',       'FLOAT',   'tds_value'),
        ('AdjustedAmount', 'FLOAT',   'adjusted_amount'),
        ('Currency',       'VARCHAR', 'local_currency'),
        ('LocalCurrency',  'VARCHAR', 'local_currency'),
        ('DocumentRef',    'VARCHAR', 'doc_ref')
), t (themename) AS (VALUES ('SAP_BSID_INV'), ('SAP_BSAD_CA'))
INSERT INTO theme_mapping (productcode, processcode, themename, targetfieldname, createddate,
                           targetfielddatatype, lde, sourcefieldname, mandatory, sourcefieldfunction,
                           dataformat, created_by, modified_by, created_time, inprd_time, modifieddatetime)
SELECT 'DTFOWS', 'BT3H', t.themename, m.targetfieldname, to_char(now(), 'YYYY-MM-DD'),
       m.targetfielddatatype, '0', m.sourcefieldname, NULL, NULL,
       NULL, 'sap-extraction', 'sap-extraction', now(), now(), now()
FROM m CROSS JOIN t;

-- 2. rows already in active_bt3h ---------------------------------------------------------------
WITH src AS (
    SELECT passportid, bukrs, kunnr, belnr, xblnr, dmbtr FROM active_bsid
    UNION ALL
    SELECT passportid, bukrs, kunnr, belnr, xblnr, dmbtr FROM active_bsad
)
UPDATE active_bt3h h
SET sendername     = COALESCE(NULLIF(TRIM(h.sendername), ''), NULLIF(TRIM(c.name1), '')),
    basevalue      = COALESCE(h.basevalue, s.dmbtr),
    documentvalue  = COALESCE(h.documentvalue, s.dmbtr),
    tdsvalue       = COALESCE(h.tdsvalue, 0),
    adjustedamount = COALESCE(h.adjustedamount, 0),
    currency       = COALESCE(NULLIF(TRIM(co.waers), ''), h.currency),
    localcurrency  = COALESCE(NULLIF(TRIM(co.waers), ''), h.localcurrency),
    documentref    = COALESCE(NULLIF(TRIM(h.documentref), ''), NULLIF(TRIM(s.xblnr), ''), s.belnr),
    modifieddatetime = now()
FROM src s
LEFT JOIN sap_ref_customer c ON c.kunnr = s.kunnr
LEFT JOIN sap_ref_company co ON co.bukrs = s.bukrs
WHERE h.passportid = s.passportid
  AND h.themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA');

COMMIT;

-- Check: every SAP row should have a name, amounts and the company currency
SELECT themename, count(*) AS rows_, count(sendername) AS with_name, count(basevalue) AS with_amount,
       count(documentref) AS with_ref, string_agg(DISTINCT currency, ',') AS currencies
FROM active_bt3h WHERE themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA') GROUP BY 1 ORDER BY 1;
