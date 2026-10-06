"""Code generation & handoff (codegen-handoff spec, S8).

The provider seam mirrors §13.3's SandboxProvider pattern: `internal`
(Bedrock synthesis) ships at Alpha; `runner` is the reference engine behind
the published S3 workspace contract (docs/codegen-workspace-contract.md),
which any external engine can implement. `kiro` is accepted as a legacy
alias for `runner`. Call sites never know which provider runs.
"""

from app.services.codegen.provider import (  # noqa: F401
    PROVIDER_NAMES,
    BuildCtx,
    BuildPlan,
    CodegenProvider,
    CodegenUnavailable,
    PlannedFile,
    get_provider,
    normalize_provider_name,
    resolve_provider_name,
)
