# systemd units

User unit files for the orchestrator background services, committed so the
running service definitions are reproducible instead of transient or local-only.

The units intentionally require an environment file at:

```bash
~/.config/claude-code-fleet-orchestrator/orchestrator-systemd.env
```

Copy the template and edit the placeholders for the target machine:

```bash
install -d -m 700 ~/.config/claude-code-fleet-orchestrator
install -m 600 deploy/systemd/orchestrator-systemd.env.example ~/.config/claude-code-fleet-orchestrator/orchestrator-systemd.env
cp deploy/systemd/fleet-orchestrator-api-gatea.service deploy/systemd/orch-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fleet-orchestrator-api-gatea.service orch-watch.service
systemctl --user status fleet-orchestrator-api-gatea.service orch-watch.service
```

Both units fail before starting if their required paths are unset. They source
`ORCH_DOTENV` after changing into `ORCH_REPO_ROOT`, matching the live service
shape while keeping checkout paths, virtualenv paths, and log paths out of the
committed unit files.

Use `systemctl --user cat <unit>` after installation to verify the loaded unit
matches the committed file plus the local env file.

`github-broker.service` is a CONTROL-deploy example for a separate Unix user
`github-broker`. This PR does not enable or start it. Worker UIDs must not read
that unit's EnvironmentFile or exec the inner `gh` except through the Unix
socket.
