# Portainer setup

This stack targets a **Docker Standalone** endpoint. Run one instance per data volume. It is not a Docker Swarm or Kubernetes stack.

## 1. Use the published image

The stack uses **`ghcr.io/markspawn/odoo2paradigm:main`**, which follows the latest successfully published main-app build. The MI/COM option is included in the main app as of version 2.1.0. The 27 application tests, actual Docker build, startup/restart checks and AMD64/ARM64 publication all passed in [GitHub run 37648183433](https://github.com/Markspawn/odoo2paradigm/actions/runs/37648183433).

The Portainer stack defaults to this tested image. You do not need to build it locally. The package was verified publicly readable for the initial release. The 2.1.0 publish succeeded; a fresh anonymous pull check from the development environment timed out. If package visibility is changed later, configure credentials in Portainer's registry settings.

## 2. Create the Portainer stack

1. Select your Docker Standalone endpoint.
2. Open **Stacks → Add stack** and name it `hopkinsville-converter`.
3. Paste the full contents of `portainer-stack.yml` into the web editor.
4. Add these environment variables in Portainer:

| Variable | Value |
| --- | --- |
| `APP_PASSWORD` | Your own password, at least 12 characters |
| `HOST_PORT` | `8091`, or another unused host port |
| `HOST_BIND_IP` | Optional; defaults to `0.0.0.0` |
| `COOKIE_SECURE` | `false` for internal HTTP; `true` when accessed through HTTPS |
| `APP_IMAGE` | Leave unset for the published, tested image |

5. Deploy the stack, allowing Portainer to pull the image.
6. Open `http://YOUR-SERVER-IP:8091` and sign in using `APP_PASSWORD`.

Portainer environment variables hold the password; do not put it in the repository. The service listens on container port 8080. Host port 8091 was chosen to avoid a common 8080 conflict.

The stack uses a named volume, `converter_data`, which Portainer normally prefixes with the stack name. Keep the same stack name and volume when upgrading. `/healthz` should return `{"status":"ok","version":"2.1.0"}`. Docker reports health after startup.

## 3. Run a controlled order

On a fresh installation, first choose **Load project setup** on the home or Catalog page. Upload the private `Hopkinsville_Order_Converter_Docker_v2.0.zip` supplied separately; this loads the internal P10 catalog and reviewed/held mapping rules. The ZIP stays off GitHub. Existing data volumes already containing that setup do not need it again.

Upload a PDF and open its review. Resolve every flagged line using the correct P10 item, feet/inches, quantity multiplier and price basis. Version 2.1.0 also offers an optional MI/COM action for one line or the whole order, keeping the original item text in Description; normal product matching remains the default. Confirm the PDF quantities represent what should still be imported. Download the XLS only when the order is ready, then import it into a controlled P10 order and compare quantities, extended prices, discounts and subtotal.

A successful file download is not proof that P10 has accepted it. The tool cannot determine whether an order has already been imported into P10.

## Persistent data and backups

The `/data` volume contains:

- `orders.sqlite3` and its SQLite sidecar files: saved orders and reviews.
- `uploads/`: source PDFs.
- `catalog.json`, `project_mappings.json`, `user_mappings.json` when present: products and mapping rules.
- `sources.json`: private setup provenance.
- `session.key`: session signing key.
- `catalog.before_update.json`: the previous catalog, replaced by the next catalog update.
- `*.json.before_setup`: the previous project files from the latest setup upload.

Back up the entire data volume while the container is stopped so the database and uploaded files stay consistent. Restart afterward. Restore into a stopped instance, then start it and check saved orders. A catalog backup alone is not a complete application backup. Do not remove the volume when recreating the container.

If you prefer an Unraid bind mount, replace `converter_data:/data` with your chosen host directory, for example `/mnt/user/appdata/hopkinsville-converter:/data`. Prepare that directory with write access for UID/GID **10001:10001** before starting. Do not broadly change permissions on an existing unrelated directory.

## Upgrade for MI / COM review (2.1.0)

In your existing Portainer stack, set `APP_IMAGE=ghcr.io/markspawn/odoo2paradigm:main`, then update the stack with image pulling enabled. If your copied stack contains a fixed `image:` value instead of the `APP_IMAGE` variable, replace that value with this image reference. Keep the same `/data` volume and password. Your catalog, saved orders and reviews remain available; no new setup upload is required.

After signing in, expand **Optional: map this order to MI / COM** on the order review page, or use the **Use MI / Use COM** button for one line. The normal product-matching path remains the default. Check one resulting P10 import as part of deploying this conversion option.

## Updates

For a local build, back up data, copy the new source, rebuild the same image tag on the correct endpoint, then recreate the Portainer service using the local image. For the default registry deployment, keep `APP_IMAGE=ghcr.io/markspawn/odoo2paradigm:main`, pull the image, and redeploy. Verify `/healthz`, sign in and open a saved order afterward.

A fresh public image starts without internal product data. Image updates preserve local catalogs and mappings. Use the Catalog page for catalog-only updates or Load project setup for a full catalog/rules replacement. Both recheck saved orders. Deleting `/data` would lose order reviews.

## Future registry updates

For future updates, wait for both test and publish jobs to pass, then update the existing stack with image pulling enabled. Keep the same `:main` image reference; you do not need to copy a new commit tag each time. If registry authentication is required, configure it in Portainer. Configure registry credentials in Portainer's registry settings, not in the stack or app password.

The `:main` image advances only after successful testing and publishing. Git repository deployment alone does not replace building/publishing the image. The provided Portainer stack has no `build` instruction.

## Access and troubleshooting

Use on the internal network or behind an HTTPS reverse proxy/VPN. For HTTPS, set `COOKIE_SECURE=true`; this prevents login cookies from being sent over plain HTTP. `HOST_BIND_IP` can bind the port to a particular LAN interface or to `127.0.0.1` for a proxy on the same host. There is no automatic TLS setup.

| Symptom | Check |
| --- | --- |
| Pull access denied | Private registry credentials are configured; for a local build, the image exists on this endpoint and force-pull is off |
| Container exits immediately | `APP_PASSWORD` has at least 12 characters; inspect logs |
| Permission denied under `/data` | Bind-mount directory is writable by 10001:10001; use the named volume if unsure |
| Login repeats | `COOKIE_SECURE` matches HTTP/HTTPS access; reload an expired form |
| Port already allocated | Set `HOST_PORT` to an unused port |
| Export blocked | Resolve flagged lines/subtotal issues and reload stale reviews |
| Catalog update interrupted | Upload the private project setup ZIP again successfully; exports remain blocked across restarts until rechecking finishes |
| PDF cannot be parsed | Use the original text-based Odoo export, not a scan; keep the file for parser review |
| Missing orders after redeploy | Check the original data volume is still mounted at `/data` |

Documentation: [Portainer stacks](https://docs.portainer.io/sts/user/docker/stacks/add), [Docker environment interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

## Optional local build

If you prefer not to use GitHub Container Registry, run these commands on the Docker host managed by Portainer:

```sh
git clone https://github.com/Markspawn/odoo2paradigm.git
cd odoo2paradigm
docker build -t hopkinsville-order-converter:2.1.0 .
```

Set `APP_IMAGE=hopkinsville-order-converter:2.1.0` in the Portainer stack and disable forced image pulling. The build downloads the Python base image and packages. Normal conversion does not use a cloud OCR or AI service.

## Direct Odoo import (2.2)

Use the updated `portainer-stack.yml` and keep your existing data volume. Add these
stack environment variables in Portainer, then pull `:main` and redeploy:

- `ODOO_URL`: your HTTPS Odoo base URL, without a trailing path
- `ODOO_DB`: your production database name
- `ODOO_USERNAME`: the login that owns the API key
- `ODOO_API_KEY`: that user's API key (enter in Portainer; never commit it)

Existing stacks must also include these under `services.converter.environment`:

```yaml
      ODOO_URL: ${ODOO_URL:-}
      ODOO_DB: ${ODOO_DB:-}
      ODOO_USERNAME: ${ODOO_USERNAME:-}
      ODOO_API_KEY: ${ODOO_API_KEY:-}
```

Odoo 18: open your user Preferences / Account Security and generate an API key.
The user needs read access to sales orders, order lines and products for the intended
company. The connector calls only authentication, `search_read` and `read` over HTTPS;
it never updates Odoo. API permissions still follow the selected Odoo user.

After sign-in, use **Import from Odoo** and enter an exact order number. The full
order enters the usual product review, including optional MI/COM. Full ordered
quantities are retained. Nonzero delivered quantities (including negative net values)
produce a warning in the list, order review and audit. Each affected P10 line's Comment
also records the delivered quantity; no extra P10 import columns are added.

Delivery figures are Odoo's net `qty_delivered` at import time, not historical shipment
counts. An order shipped and fully returned may have net zero. Import again to check
for updates: identical snapshots reopen the existing review; changed snapshots create
another saved review without overwriting approvals. Only export the intended snapshot.
PDF imports cannot determine delivery status and retain their current behavior.

Prices use Odoo's untaxed line subtotal divided by ordered quantity to preserve discounts
and taxes included in prices. Quantities, units and custom lengths still need the usual
P10 review. Custom length fields outside the line description are not automatically read.
Nonzero down payments, cancelled orders, non-USD orders and reconciliation failures
block export. Customer header details, taxes and payment application still require P10
header/accounting handling. This is a detail-line importer, not an Odoo/P10 sync.

Credentials stay in the container environment and are excluded from saved snapshots,
exports and UI errors. With the API variables blank, the PDF workflow remains available.
A real Odoo connection and one P10 import must be verified in your environment.
