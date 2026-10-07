# Odoo → Paradigm P10

A browser app for reviewing Hopkinsville Odoo sales-order PDFs and exporting the nine-column P10 detail-import format. Runs in Docker with Portainer. Excel is not required.

**v2.0.1:** Docker web interface, persistent order reviews, native XLS export, and a private project-data setup upload. The public repository and image contain no internal product catalog, mapping tables or customer orders.

## Portainer quick start

1. Create a Docker Standalone stack in Portainer using [`portainer-stack.yml`](portainer-stack.yml). It defaults to the tested image `ghcr.io/markspawn/odoo2paradigm:sha-b4b0c3c`; no local build is needed.
2. Set `APP_PASSWORD` to your own password of at least 12 characters. The published image supports public pulls, so no registry login is needed; deploy the stack.
3. Open `http://YOUR-SERVER-IP:8091` and sign in.
4. Choose **Load project setup**. Upload your private `Hopkinsville_Order_Converter_Docker_v2.0.zip`, previously supplied separately. The app imports only its catalog, mapping rules and source metadata; it never executes code from that ZIP.
5. Upload an Odoo PDF, resolve flagged lines, then download the XLS for P10.

**Build verified:** all 22 tests, the Docker startup/restart checks, and AMD64/ARM64 image publication passed in [this GitHub run](https://github.com/Markspawn/odoo2paradigm/actions/runs/37514350269). Deployment to your own server and a controlled P10 import are the remaining checks.

See [Portainer deployment, backups and local-build options](docs/PORTAINER.md).

For plain Docker Compose, copy `.env.example` to `.env`, set a unique password, then run `docker compose up -d --build`.

## What the converter does

- Extracts text-based Odoo PDFs in the supplied order layout; scanned PDFs/OCR are not supported.
- Accepts up to 20 PDFs per upload, 32 MiB total and 100 pages per PDF.
- Uses the privately loaded P10 catalog and reviewed/held project mappings. Unresolved or conflicting lines require review.
- Preserves package quantities unless an explicit unit multiplier is approved.
- Checks the PDF subtotal and converted line extensions before export.
- Requires confirmation of length and price basis for linear-foot items.
- Saves order reviews, optional remembered mappings and source PDFs in `/data` across restarts.
- Creates real Excel 97–2003 `.xls` files, plus CSV and HTML/CSV/JSON review downloads.
- Detects duplicate PDF uploads by content hash. It cannot detect duplicate imports in P10.

The project setup replaces the catalog and project rules and rechecks existing orders. Catalog-only updates merge supplied products and retain omitted items. Remembered approvals are reused only when product specifications still match. Unsaved-to-mapping approvals reset on recheck. An interrupted product-data update blocks exports until setup is completed again.

## Import format

| Column | Meaning |
| --- | --- |
| ProductID | Verified P10 product code |
| LinearAmount1 | Feet |
| LinearAmount2 | Inches |
| PcsOrdered | Source quantity after the reviewed multiplier |
| Comment | Odoo order/line reference and review notes |
| SalesPrice | Reviewed price per piece or per linear foot |
| Cost | Blank unless supplied in the review |
| Color | P10 product color |
| Description | Source order description |

For per-LF items, the reviewer confirms the length and price basis. The app converts the source piece price to a per-foot price, retains eight decimal places and checks the extended amount. Discounts retain their negative value. Down-payment references remain in the audit and are excluded from product imports.

PDFs may show originally ordered quantities rather than the amount still open. Confirm remaining quantities and the destination order before exporting. Customer/order headers, tax and payments need separate handling in P10. The app does not connect to either ERP or submit imports. Validate one controlled import in P10 before production use.

## Private data

Internal setup files are uploaded after signing in and stored only on your server. Keep the private setup ZIP for future installations. Do not commit it or runtime files; Git and Docker ignore these locations. The Dockerfile explicitly copies only application code and public assets, so product data is not baked into the published image.

Existing `/data` volumes from the earlier Docker package continue to work; an image upgrade does not overwrite saved catalogs, rules or orders.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

The 22 tests use fictitious products and orders. They cover PDF extraction, totals, XLS round-trip values, guarded exports, persistence, setup validation, catalog rechecks, authentication and stale edits. GitHub Actions additionally builds and starts a real Docker image, checks health and restarts it before publishing AMD64/ARM64 images.

Implementation: Python 3.12, Flask with Waitress, pdfplumber, SQLite and xlwt. Run one container per data volume. This is an internal-team application with a shared password; it has no individual user accounts.

[GitHub workflow details](docs/GITHUB.md)
