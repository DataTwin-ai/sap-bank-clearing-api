# SAP Bank Posting with Clearing

API that posts bank receipts in SAP S/4HANA and clears the matching customer
invoice in one step, then writes the SAP document number back to the DataTwin
tables `active_bt3i` / `active_bs`.

- Module and usage: [bank_posting/README.md](bank_posting/README.md)
- Process guide: [bank_posting/BANK_POSTING_PROCESS.md](bank_posting/BANK_POSTING_PROCESS.md)
- Clearing and DB update details: [bank_posting/CLEARING_AND_DB_UPDATE.md](bank_posting/CLEARING_AND_DB_UPDATE.md)
- SAP function module: [bank_posting/sap_abap/](bank_posting/sap_abap/)

## Setup

```bash
pip install -r requirements.txt
cp config.example.ini config.ini     # fill in DB and SAP credentials
python bank_posting/server.py --port 8001
```

Shared helpers used by the module: `logger_setup.py`, `db_manager.py`,
`sap_odata_extractor.py`.
