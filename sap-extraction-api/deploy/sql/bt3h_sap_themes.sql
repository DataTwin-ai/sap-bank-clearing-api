-- Pinned ingestion themes for SAP delta tables -> active_bt3h (process BT3H, product DTFOWS).
-- Referenced by config.ini: theme_name_bsid = SAP_BSID_INV, theme_name_bsad = SAP_BSAD_CA.
-- Re-runnable: replaces the two themes. Already applied on the dev DB dm00005 (2026-11-20).
-- The ingestion service adds rows for unmapped extra columns (bukrs, cpudt, hkont, shkzg, ...)
-- itself on the first run (created_by AUTO_SCHEMA); they are not listed here.
--   psql -h <db-host> -p <port> -U postgres -d dm00005 -f deploy/sql/bt3h_sap_themes.sql

BEGIN;

DELETE FROM theme_mapping
WHERE processcode = 'BT3H' AND themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA');

WITH m (targetfieldname, targetfielddatatype, sourcefieldname, dataformat) AS (
    VALUES
        ('DocumentNumber',              'VARCHAR', 'belnr',      NULL),
        ('DocumentID',                  'VARCHAR', 'belnr',      NULL),
        ('DocumentRef',                 'VARCHAR', 'xblnr',      NULL),
        ('DocumentType',                'VARCHAR', 'blart',      NULL),
        ('DocumentDate',                'DATE',    'bldat',      'YYYY-MM-DD'),
        ('PostingDate',                 'DATE',    'budat',      'YYYY-MM-DD'),
        ('FinancialDate',               'DATE',    'budat',      'YYYY-MM-DD'),
        ('DueDate',                     'DATE',    'zfbdt',      'YYYY-MM-DD'),
        ('FinancialYear',               'VARCHAR', 'gjahr',      NULL),
        -- SNO is part of the row key with DocumentNumber. BELNR + BUZEI repeats across company
        -- codes / years, so the stable per-row passportid is used: a changed SAP row updates its row.
        ('SNO',                         'VARCHAR', 'passportid', NULL),
        ('SenderAccount',               'VARCHAR', 'kunnr',      NULL),
        ('AmountInLocalCurrency',       'FLOAT',   'dmbtr',      NULL),
        ('AmountInTransactionCurrency', 'FLOAT',   'wrbtr',      NULL),
        ('TotalDocumentValue',          'FLOAT',   'dmbtr',      NULL),
        ('TotalBaseValue',              'FLOAT',   'dmbtr',      NULL),
        ('Currency',                    'VARCHAR', 'waers',      NULL),
        ('TransactionCurrency',         'VARCHAR', 'waers',      NULL),
        ('PaymentTerms',                'VARCHAR', 'zterm',      NULL),
        ('TaxCode',                     'VARCHAR', 'mwskz',      NULL),
        ('LocationCode',                'VARCHAR', 'gsber',      NULL),
        ('Reference1',                  'VARCHAR', 'zuonr',      NULL),
        ('LinkRef',                     'VARCHAR', 'xref1',      NULL),
        ('LinkRef2',                    'VARCHAR', 'xref2',      NULL),
        ('LinkRef3',                    'VARCHAR', 'xref3',      NULL),
        ('PK',                          'VARCHAR', 'passportid', NULL)
),
bsad_only (targetfieldname, targetfielddatatype, sourcefieldname, dataformat) AS (
    VALUES
        ('SettlementDate',              'DATE',    'augdt',      'YYYY-MM-DD'),
        ('Reference2',                  'VARCHAR', 'augbl',      NULL)
),
rows_ (themename, targetfieldname, targetfielddatatype, sourcefieldname, dataformat) AS (
    SELECT 'SAP_BSID_INV', * FROM m
    UNION ALL SELECT 'SAP_BSAD_CA', * FROM m
    UNION ALL SELECT 'SAP_BSAD_CA', * FROM bsad_only
)
INSERT INTO theme_mapping (productcode, processcode, themename, targetfieldname, createddate,
                           targetfielddatatype, lde, sourcefieldname, mandatory, sourcefieldfunction,
                           dataformat, created_by, modified_by, created_time, inprd_time, modifieddatetime)
SELECT 'DTFOWS', 'BT3H', themename, targetfieldname, to_char(now(), 'YYYY-MM-DD'),
       targetfielddatatype, '0', sourcefieldname, NULL, NULL,
       dataformat, 'sap-extraction', 'sap-extraction', now(), now(), now()
FROM rows_;

-- Rows loaded before SNO = passportid (sno was the position in the batch) get the same key as new
-- runs. Skipped on a fresh database (active_bt3h has no passportid column before the first run).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'active_bt3h' AND column_name = 'passportid') THEN
        UPDATE active_bt3h SET sno = passportid
        WHERE processcode = 'BT3H' AND themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA')
          AND passportid IS NOT NULL AND sno IS DISTINCT FROM passportid;
    END IF;
END $$;

COMMIT;

-- Expected: SAP_BSID_INV 25 rows, SAP_BSAD_CA 27 rows
SELECT themename, count(*) FROM theme_mapping
WHERE processcode = 'BT3H' AND themename IN ('SAP_BSID_INV', 'SAP_BSAD_CA')
GROUP BY 1 ORDER BY 1;
