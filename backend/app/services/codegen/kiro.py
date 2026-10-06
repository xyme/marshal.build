"""Deprecated import path — kept for import compatibility only.

The provider lives in `app.services.codegen.workspace_runner` and is selected
as codegen provider `runner`; `kiro` is accepted as a legacy alias. Import
from `workspace_runner` in new code.
"""

from app.services.codegen.workspace_runner import (  # noqa: F401
    ExternalStatus,
    WorkspaceRunnerProvider,
    codebuild_client,
    s3_client,
)

KiroWorkspaceProvider = WorkspaceRunnerProvider
