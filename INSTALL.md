# Install Notes

The standard local install path remains in [README.md](README.md) and
[SETUP.md](SETUP.md).

## User systemd services

Persistent operator services are documented in [deploy/systemd/README.md](deploy/systemd/README.md).
The committed units replace local-only definitions for the orchestrator API and
`orch-watch` with env-configured user services. They do not embed checkout,
virtualenv, Redis, Neo4j, or log paths in the unit files; missing launch paths
make the service fail loudly before `exec`.
