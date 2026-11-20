-- Pinned ingestion themes for SAP billing documents (product DTFOWS, transaction type SalesInvoice):
--   VBRK (billing header) -> active_bt3h, process BT3H, theme SAP_VBRK_SI
--   VBRP (billing items)  -> active_bt3i, process BT3I, theme SAP_VBRP_SI
-- Referenced by config.ini: theme_name_vbrk = SAP_VBRK_SI, theme_name_vbrp = SAP_VBRP_SI.
--
-- Header and items are linked by DocumentNumber / DocumentID = VBELN and share one context ID
-- (grouptransactionref). LinkRef on the header = BELNR, the accounting document = documentnumber
-- of the BSID / BSAD row of the same invoice in active_bt3h.
-- SNO is the row key together with DocumentNumber: constant 1 for the header, POSNR for an item.
--
-- Unmapped columns sent by the extraction (bukrs, gjahr, kunag, vkorg, fksto, ...) are added to
-- the target table by the ingestion service on the first run (theme rows created_by AUTO_SCHEMA).
-- Re-runnable: replaces the two themes.
--   psql -h <db-host> -p <port> -U postgres -d dm00005 -f deploy/sql/sales_invoice_themes.sql

BEGIN;

DELETE FROM theme_mapping
WHERE (processcode = 'BT3H' AND themename = 'SAP_VBRK_SI')
   OR (processcode = 'BT3I' AND themename = 'SAP_VBRP_SI');

WITH rows_ (processcode, themename, targetfieldname, targetfielddatatype, sourcefieldname, sourcefieldfunction, dataformat) AS (
    VALUES
        -- VBRK -> active_bt3h (one row per billing document)
        ('BT3H', 'SAP_VBRK_SI', 'DocumentNumber',      'VARCHAR', 'vbeln',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentID',          'VARCHAR', 'vbeln',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'SNO',                 'VARCHAR', '1',          'CONS', NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentRef',         'VARCHAR', 'xblnr',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentType',        'VARCHAR', 'fkart',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'DocumentDate',        'DATE',    'fkdat',      NULL,   'YYYY-MM-DD'),
        ('BT3H', 'SAP_VBRK_SI', 'SenderAccount',       'VARCHAR', 'kunrg',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'Currency',            'VARCHAR', 'waerk',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TransactionCurrency', 'VARCHAR', 'waerk',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'ExchangeRate',        'FLOAT',   'kurrf',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TotalBaseValue',      'FLOAT',   'netwr',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TotalTaxValue',       'FLOAT',   'mwsbk',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'TotalDocumentValue',  'FLOAT',   NULL,         'EXP(totalbasevalue+totaltaxvalue)', NULL),
        ('BT3H', 'SAP_VBRK_SI', 'PaymentTerms',        'VARCHAR', 'zterm',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'LinkRef',             'VARCHAR', 'belnr',      NULL,   NULL),
        ('BT3H', 'SAP_VBRK_SI', 'PK',                  'VARCHAR', 'passportid', NULL,   NULL),

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

-- Expected: SAP_VBRK_SI 16 rows, SAP_VBRP_SI 20 rows
SELECT processcode, themename, count(*) FROM theme_mapping
WHERE themename IN ('SAP_VBRK_SI', 'SAP_VBRP_SI') AND created_by = 'sap-extraction'
GROUP BY 1, 2 ORDER BY 1, 2;
