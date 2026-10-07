# Repository and Docker publishing

Project: https://github.com/Markspawn/odoo2paradigm

The repository is public. Internal P10 catalog/mapping tables, credentials, customer PDFs and saved orders must remain outside Git. Tests use fictitious data. Load the private setup ZIP through the authenticated app after deployment.

The included `Test and publish Docker image` workflow runs on pushes to `main`, version tags, pull requests and manual dispatch. It:

1. Runs the application tests on Python 3.12.
2. Builds the Docker image and starts it with a read-only filesystem and persistent volume.
3. Checks health, restarts the container and checks health again.
4. After successful testing, publishes AMD64 and ARM64 images to GitHub Container Registry. Pull requests do not publish.

The version 2.1.0 [run 37648183433](https://github.com/Markspawn/odoo2paradigm/actions/runs/37648183433) passed both jobs. Its image is `ghcr.io/markspawn/odoo2paradigm:sha-d1688e1`, which is the Portainer stack default. Verify both jobs on subsequent revisions before upgrading. The `main` image is `ghcr.io/markspawn/odoo2paradigm:main`; tag pushes also publish matching version tags. A version tag or digest is preferable for a fixed deployment. The workflow uses GitHub's automatic token with package-write permission.

The package was verified publicly readable for the initial release. Version 2.1.0 was published successfully, but a fresh anonymous pull check timed out. Future package visibility changes could require authentication even when the source repository is public. If the package is private, configure your registry credentials in Portainer's registry settings. Do not put registry tokens into the stack file or application password. Alternatively, use the local Docker build route, which requires no registry login.

GitHub hosts the source and image workflow. The Python application runs on the Docker server; GitHub Pages is not involved.

Reference: https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images
