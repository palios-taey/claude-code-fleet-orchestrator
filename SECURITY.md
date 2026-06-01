# Security

## Threat model

`claude-code-fleet-orchestrator` is a local single-user tool. It runs on your machine and connects your own CLIs, your own browser dashboard, and your own local data stores.

The API and internal services are unauthenticated by design because they are meant to be local to you:
- the orchestrator API should bind `127.0.0.1` by default
- Neo4j, Redis, and related internal services are expected to stay on localhost or other local-only machine paths
- there is no built-in auth layer because this is not a hosted or multi-user service

This repository is not designed as a hosted or multi-tenant control plane.

## If you deliberately expose it

If you override the default bind and expose the API across a network, you are responsible for securing that exposure yourself. Put it behind authentication, a reverse proxy, firewall rules, or equivalent controls appropriate for your environment.

There is no built-in auth for network-exposed deployments, and none is needed for the intended local single-user use case.

## Reporting a vulnerability

Email `security@palios-taey.dev` with:
- affected product + version
- reproduction steps
- impact
- any suggested fix

Do not file public GitHub issues for security reports.
