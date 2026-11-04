# Public document table CSV

Read one public document into a rectangular CSV, a provenance record, and a short handoff. Use the direct command when you do not need Dify. Use an existing Dify 1.19.0 runtime when you want the same checker inside that product. There is no model node and no model token.

Direct command, from the extracted archive:

    npm ci --ignore-scripts --prefix reader
    python3 src/direct_csv.py --url URL --fields Name,Value --start-reader

Existing Dify 1.19.0 runtime:

    READER_CORE=./reader READER_BIND=0.0.0.0 PORT=8765 DIFY_TABLE_ADMIT=1 node src/reader_boundary.mjs
    python3 install/use-existing.py --console http://127.0.0.1:5001 --admin-file /path/to/admin.txt --url URL --fields Name,Value

The direct route and the Dify route use one checker. Page numbers other than 1 are unsupported and do not call the reader. Combined spans are unsupported and are not flattened. A stopped reader is HTTP 503: the checker returns malformed, retryable false, and an empty CSV. It does not substitute a fixture table. Recovery is a new run after the reader is back. HTTP retry stays off.

install/cold-install.sh is the disposable acceptance tool. It creates a new owned state directory, exports the tracked Dify docker tree, and uses gateway mode nat. It is separate from the commands above. Pins and the 503 classification are in `COMPATIBILITY.json`. Licences are in `LICENSES.md`. Commands are in `COLD-INSTALL.txt`.
