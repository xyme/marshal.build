"""Artifact storage resolution (cdk-artifacts spec R3).

inline-cfn artifacts live in the DB (content column); cdk-app artifact
content lives in S3 under `artifacts/<build_id>/<path>` in the codegen
workspace bucket (s3_key set, content NULL). This module is the single
resolver both ways — bundle, browser, and deploy all read through it.
"""

import asyncio
import logging

import boto3

from app.core.config import get_settings
from app.models import CodegenArtifact

logger = logging.getLogger("marshal.codegen")

PREVIEW_CAP_BYTES = 256 * 1024

_s3 = None


def s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=get_settings().aws_region)
    return _s3


def artifact_key(build_id, path: str) -> str:
    return f"artifacts/{build_id}/{path}"


def is_binary_path(path: str) -> bool:
    return path.endswith((".zip", ".tar.gz"))


async def artifact_bytes(artifact: CodegenArtifact) -> bytes:
    """Content bytes regardless of storage backend."""
    if artifact.content is not None:
        return artifact.content.encode()
    if not artifact.s3_key:
        raise ValueError(f"artifact {artifact.path} has neither content nor s3_key")
    bucket = get_settings().codegen_workspace_bucket

    def fetch() -> bytes:
        return s3_client().get_object(Bucket=bucket, Key=artifact.s3_key)["Body"].read()

    return await asyncio.to_thread(fetch)


async def artifact_text(artifact: CodegenArtifact) -> str:
    return (await artifact_bytes(artifact)).decode("utf-8")


async def artifact_bytes_by_key(s3_key: str) -> bytes:
    bucket = get_settings().codegen_workspace_bucket

    def fetch() -> bytes:
        return s3_client().get_object(Bucket=bucket, Key=s3_key)["Body"].read()

    return await asyncio.to_thread(fetch)


async def copy_response_to_artifacts(
    *, build_id, attempt_root: str, paths: list[str]
) -> dict[str, str]:
    """Server-side copy response/generated/* → artifacts/<build_id>/* (R3.1).

    Returns {path: s3_key}. The workspace prefix expires in 30 days; the
    artifacts prefix is the durable store.
    """
    bucket = get_settings().codegen_workspace_bucket

    def copy_all() -> dict[str, str]:
        client = s3_client()
        keys: dict[str, str] = {}
        for path in paths:
            source = f"{attempt_root}/response/generated/{path}"
            dest = artifact_key(build_id, path)
            client.copy_object(
                Bucket=bucket, Key=dest, CopySource={"Bucket": bucket, "Key": source}
            )
            keys[path] = dest
        return keys

    return await asyncio.to_thread(copy_all)
