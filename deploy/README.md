# Deployment

This repository intentionally does not document a specific host, LXC, network,
secret store or gateway configuration.

Production deployment is maintained in the operator's private Proxmox
documentation. Review the MCP infrastructure runbook there before deploying
this service.

The `Dockerfile` in this repository **is** the production build artifact: it
is built and deployed automatically by Coolify from `main`. `docker-compose.yaml`
remains a convenience for local development only.

`RED_TRANSPORTE_DATA_DIR` is optional. When unset, the service is fully
stateless. When set, it persists an encrypted secret store under that path —
declare it as a volume before the first deploy if you need admin-panel-managed
configuration to survive a redeploy.
