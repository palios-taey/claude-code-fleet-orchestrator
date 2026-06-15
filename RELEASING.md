# Releasing

Releases are cut by pushing a `vX.Y.Z` git tag and publishing a GitHub release.

## Steps

1. **Bump the version.** Edit `fleet_orchestrator/version.py` to the new `X.Y.Z`.
   This is the single source of truth — `setup.py`, `fleet_orchestrator.__version__`,
   and `easy_setup` all read it. A release whose tag does not match `version.py`
   is rejected by the `version-tag-consistency` workflow (it once silently drifted:
   `version.py` stayed at `1.6.0` through the v1.7.0/v1.8.0/v1.8.1 releases).
2. Open a PR with the bump, let the gates (incl. r5 adversarial audit) pass, merge.
3. Tag the merge commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. `gh release create vX.Y.Z --title "..." --notes "..."`.

## Versioning

Semantic: bugfix → patch, backward-compatible feature → minor, breaking → major.
