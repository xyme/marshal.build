## What and why

<!-- One paragraph: the problem, the change, and anything a reviewer should look at first. Link the issue if there is one. -->

## Checklist

- [ ] Tests appended to the existing suite file for the area (see CONTRIBUTING.md, "Conventions"), and `scripts/ci-local.sh` passes locally.
- [ ] Documentation updated where behavior changed (README, `docs/`, or the in-app docs under `frontend/src/content/docs/`).
- [ ] I have signed the CLA (the bot comments on the pull request if not).
- [ ] No secrets or installation identifiers in the diff: no AWS account ids, Cognito pool/client ids, hostnames, certificate ARNs, e-mail addresses or phone numbers. Use `123456789012` and `<placeholder>` values in fixtures and docs.
- [ ] If a dependency was added or upgraded: `scripts/supply-chain-audit.sh` is clean, or the new advisory is triaged in `scripts/audit-allowlist.txt` with a rationale.
