# Security policy

## Reporting a vulnerability

Report security issues by e-mail to **security@marshal.build**. Please do not
open a public GitHub issue for anything you believe is a vulnerability.

Include what you can of: the affected component and version (commit or tag),
steps to reproduce, the impact you observed, and whether the issue is already
public anywhere. Encrypted reports are welcome; ask for a key in a first
plain-text message.

What to expect:

- **Acknowledgement within 3 business days.**
- A triage decision and, where accepted, a fix plan with a target date.
- **Coordinated disclosure with a 90-day window** from the acknowledgement.
  We publish a GitHub security advisory when a fix is available, or at the
  end of the window if no fix is possible, and credit the reporter unless they
  prefer otherwise. If a fix needs longer, we say so before the window ends.

There is **no bug bounty program**.

## Supported versions

Security fixes land on `main` and in the latest tagged release. Older tags do
not receive fixes; upgrade to the latest release.

## Scope

In scope: the code in this repository as you deploy it yourself (the FastAPI
control plane, the Next.js frontend, the CDK stacks, the codegen runner, the
deploy and seed scripts, and the generated-agent templates).

Out of scope for public advisories: the hosted private beta at
`app.marshal.build`. It is an invitation-only installation run by the
maintainers, not a public service. Report issues you find there to the same
address; they are handled privately and any resulting code fix is disclosed
through this policy.

Also out of scope: findings in third-party services (AWS, Amazon Bedrock,
model providers) — report those to the vendor — and issues that require a
compromised administrator account or compromised AWS credentials as a
precondition.

## Automated checks already in place

- **Supply-chain gate on every image build** — `scripts/supply-chain-audit.sh`
  runs `pip-audit` and `npm audit` in CodeBuild before any image is built, with
  `AUDIT_STRICT=1`: a new HIGH or CRITICAL advisory fails the build unless it is
  triaged in `scripts/audit-allowlist.txt` with a written rationale. An SBOM is
  produced per build and retained.
- **Secret scanning in CI** — gitleaks runs on every push and pull request.
- **Dependabot** — weekly dependency update pull requests for the backend
  (uv), frontend and infra (npm), and GitHub Actions.
- **ECR scan-on-push** on all container repositories.

The threat model and control map for the control plane are in
[docs/security.md](docs/security.md).
