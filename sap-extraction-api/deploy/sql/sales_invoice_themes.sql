-- Pinned ingestion themes for SAP billing documents (product DTFOWS, transaction type Invoice):
--   VBRK (billing header) -> active_bt3h, process BT3H, theme SAP_VBRK_SI
--   VBRP (billing items)  -> active_bt3i, process BT3I, theme SAP_VBRP_SI
-- Referenced by config.ini: theme_name_vbrk = SAP_VBRK_SI, theme_name_vbrp = SAP_VBRP_SI.
--
-- Header and items are linked by DocumentNumber / DocumentID = VBELN and share one context ID
-- (grouptransactionref). LinkRef2 on the header = BELNR, the accounting document = documentnumber
-- of the BSID / BSAD row of the same invoice in active_bt3h. LinkRef is left for the UTR.
-- SNO is the row key together with DocumentNumber: constant 1 for the header, POSNR for an item.
--
-- The header fills the columns the AR Inbox dashboard reads (transactiontype = 'Invoice'):
--   BaseValue, DocumentValue, TotalDocumentValue = gross_value (NETWR + MWSBK, added by the worker:
--     columns_vbrk = ... netwr + mwsbk as gross_value). Not EXP(): the ingestion service replaces
--     column names inside EXP() as substrings, so EXP(totalbasevalue+...) breaks once basevalue exists.
--   SenderName = customer_name (KNA1 NAME1 of KUNRG, added by the worker: customer_name_vbrk = kunrg)
--   TaxPercentage, TDSValue, AdjustedAmount = 0 (not in the billing document)
--
-- Unmapped columns sent by the extraction (bukrs, gjahr, kunag, vkorg, fksto, ...) are added to
-- the target table by the ingestion service on the first run (theme rows created_by AUTO_SCHEMA).
-- Re-runnable: replaces the rows this file created and keeps the AUTO_SCHEMA rows.
--   psql -h <db-host> -p <port> -U postgres -d dm00005 -f deploy/sql/sales_invoice_themes.sql

BEGIN;

DELETE FROM theme_mapping
WHERE ((processcode = 'BT3H' AND themename = 'SAP_VBRK_SI')
    OR (processcode = 'BT3I' AND themename = 'SAP_VBRP_SI'))
  AND created_by = 'sap-extraction';

WITH rows_ (processcode, themename, targetfieldname, targetfielddatatype, sourcefieldname, sourcefieldfunction, dataformat) AS (
    VALUES
        -- VBRK -> active_bt3h (one row per billing document)
        ('BT3H', 'SAP_VBRK_SI', 'DocumentNumber',      'VARCHAR', 'vbeln',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentID',          'VARCHAR', 'vbeln',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'SNO',                 'VARCHAR', '1',             'CONS', NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentRef',         'VARCHAR', 'xblnr',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentType',        'VARCHAR', 'fkart',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentDate',        'DATE',    'fkdat',         NULL,   'YYYY-MM-DD'),
        ('BT3H', 'SAP_VBRK_SI', 'SenderAccount',       'VARCHAR', 'kunrg',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'SenderName',          'VARCHAR', 'customer_name', NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'Currency',            'VARCHAR', 'waerk',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TransactionCurrency', 'VARCHAR', 'waerk',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'ExchangeRate',        'FLOAT',   'kurrf',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TotalBaseValue',      'FLOAT',   'netwr',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TotalTaxValue',       'FLOAT',   'mwsbk',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TotalDocumentValue',  'FLOAT',   'gross_value',   NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'BaseValue',           'FLOAT',   'gross_value',   NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentValue',       'FLOAT',   'gross_value',   NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TaxPercentage',       'FLOAT',   '0',             'CONS', NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TDSValue',            'FLOAT',   '0',             'CONS', NULL),
        ('BT3H', 'SAP_VBRK_SI', 'AdjustedAmount',      'FLOAT',   '0',             'CONS', NULL),
        ('BT3H', 'SAP_VBRK_SI', 'PaymentTerms',        'VARCHAR', 'zterm',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'LinkRef2',            'VARCHAR', 'belnr',         NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'PK',                  'VARCHAR', 'passportid',    NULL,   NULL),

        -- VBRP -> active_bt3i (one row per billing item)
        ('BT3I', 'SAP_VBRP_SI', 'DocumentNumber',      'VARCHAR', 'vbeln',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'DocumentID',          'VARCHAR', 'vbeln',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'SNO',                 'VARCHAR', 'posnr',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'LineID',              'VARCHAR', 'posnr',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'DocumentDate',        'DATE',    'fkdat',      NULL,   'YYYY-MM-DD'),
        ('BT3I', 'SAP_VBRP_SI', 'SenderAccount',       'VARCHAR', 'kunrg',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'ItemCode',            'VARCHAR', 'matnr',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'ItemDescription',     'VARCHAR', 'arktx',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'Qty',                 'FLOAT',   'fkimg',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'QtyUOM',              'VARCHAR', 'vrkme',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'BaseValue',           'FLOAT',   'netwr',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'TaxValue',            'FLOAT',   'mwsbp',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'DocumentValue',       'FLOAT',   NULL,         'EXP(basevalue+taxvalue)', NULL),
        ('BT3I', 'SAP_VBRP_SI', 'TaxCode',             'VARCHAR', 'mwsk1',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'Currency',            'VARCHAR', 'waerk',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'TransactionCurrency', 'VARCHAR', 'waerk',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'OrderReference',      'VARCHAR', 'aubel',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'LinkRef',             'VARCHAR', 'vgbel',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'LocationID',          'VARCHAR', 'werks',      NULL,   NULL),
        ('BT3I', 'SAP_VBRP_SI', 'PK',                  'VARCHAR', 'passportid', NULL,   NULL)
)
INSERT INTO theme_mapping (productcode, processcode, themename, targetfieldname, createddate,
                           targetfielddatatype, lde, sourcefieldname, mandatory, sourcefieldfunction,
                           dataformat, created_by, modified_by, created_time, inprd_time, modifieddatetime)
SELECT 'DTFOWS', processcode, themename, targetfieldname, to_char(now(), 'YYYY-MM-DD'),
       targetfielddatatype, '0', sourcefieldname, NULL, sourcefieldfunction,
       dataformat, 'sap-extraction', 'sap-extraction', now(), now(), now()
FROM rows_;

COMMIT;

-- Expected: SAP_VBRK_SI 22 rows, SAP_VBRP_SI 20 rows
SELECT processcode, themename, count(*) FROM theme_mapping
WHERE themename IN ('SAP_VBRK_SI', 'SAP_VBRP_SI') AND created_by = 'sap-extraction'
GROUP BY 1, 2 ORDER BY 1, 2;
