# Portainer setup

This stack targets a **Docker Standalone** endpoint. Run one instance per data volume. It is not a Docker Swarm or Kubernetes stack.

## 1. Use the published image

The initial image is **`ghcr.io/markspawn/odoo2paradigm:sha-b4b0c3c`**. The 22 application tests, actual Docker build, startup/restart checks and AMD64/ARM64 publication all passed in [GitHub run 37514350269](https://github.com/Markspawn/odoo2paradigm/actions/runs/37514350269).

The Portainer stack defaults to this tested image. You do not need to build it locally. Public pull access was verified for this image, so no registry login is needed. If package visibility is changed later, configure credentials in Portainer's registry settings.

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

The stack uses a named volume, `converter_data`, which Portainer normally prefixes with the stack name. Keep the same stack name and volume when upgrading. `/healthz` should return `{"status":"ok","version":"2.0.1"}`. Docker reports health after startup.

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

## Updates

For a local build, back up data, copy the new source, rebuild the same image tag on the correct endpoint, then recreate the Portainer service using the local image. For a registry deployment, use a tested version tag or image digest, pull it, and redeploy. Verify `/healthz`, sign in and open a saved order afterward.

A fresh public image starts without internal product data. Image updates preserve local catalogs and mappings. Use the Catalog page for catalog-only updates or Load project setup for a full catalog/rules replacement. Both recheck saved orders. Deleting `/data` would lose order reviews.

## Future registry updates

For future updates, wait for both test and publish jobs to pass, then copy the image reference from the workflow/package page. Add GitHub Container Registry authentication to the Portainer endpoint for a package that requires authentication, then set `APP_IMAGE` to that verified image/tag. Configure registry credentials in Portainer's registry settings, not in the stack or app password.

Use a tag or digest from a successful publish. Git repository deployment alone does not replace building/publishing the image. The provided Portainer stack has no `build` instruction.

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
