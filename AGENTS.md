# Self-hosted applications

Read README.md and SETUP.md before changing deployments. Guaranine owns the
Kubernetes platform; this repository owns application deployments and integrations.

- Keep services independently deployable under apps/. Use upstream images pinned
  by digest and the cluster's restricted Pod Security settings.
- Keep credentials, VPN profiles, state, and backups outside Git. The operator
  directory defaults to ~/.local/state/self-hosted with private permissions.
- Preserve existing application data and enrolled Tailscale identities on deploy.
- Make configuration changes in this repository before applying them to the cluster.
- Validate changes, then commit and push completed work to main.

SETUP.md owns deployment instructions. docs/ owns operations and service behavior.
