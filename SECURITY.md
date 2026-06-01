# Security

## Security model

`claude-code-fleet-orchestrator` is built for a local-trust deployment:

- one operator
- one machine
- local Redis
- local Neo4j
- loopback API by default

The product assumes the caller is already trusted because the intended boundary is the machine itself, not an application-layer login.

## What the current code does

- `tasks_api` binds to `127.0.0.1` by default
- CLI tooling talks to that local API
- Redis and Neo4j credentials are environment-driven
- Neo4j may be no-auth or auth-required depending on your local setup
- there is no built-in user/session authentication layer for the HTTP API

That is intentional for the local single-user case. This repository is not a hosted control plane.

## If you expose it anyway

If you bind the API to a routable interface or expose the backing services over a network, you are changing the trust model yourself. At that point you need to provide your own controls, such as:

- reverse-proxy authentication
- firewall rules
- VPN-only access
- host-level service isolation

The repository does not currently ship a first-party auth or tenancy layer for that mode.

## Reporting a vulnerability

Email `security@palios-taey.dev` with:

- affected product + version
- reproduction steps
- impact
- any suggested fix

Do not file public GitHub issues for security reports.
