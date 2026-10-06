"""Converge the published marketplace to the approved exact-three demo portfolio.

Dry-run is the default and performs no writes. Mutation requires the explicit
``--apply-reset`` flag. The command never hard-deletes catalog rows: it adopts
known legacy fixtures in place, updates the canonical portfolio payloads, and
archives every other *proven seed-owned* published fixture. An unrecognized,
duplicated, submission-originated, or manually edited published row aborts the
whole transaction before mutation.

Usage:
    cd backend && uv run python ../scripts/seed_marketplace.py --dry-run
    cd backend && uv run python ../scripts/seed_marketplace.py --apply-reset

Cloud one-off task:
    python /opt/marshal/scripts/seed_marketplace.py --dry-run
    python /opt/marshal/scripts/seed_marketplace.py --apply-reset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, "/app")

import boto3  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import MarketplaceSample, Template, User  # noqa: E402
from app.models.entities import PLATFORM_TENANT_ID  # noqa: E402
from fixtures.marketplace.fs_samples import FS_SAMPLES  # noqa: E402
from fixtures.marketplace.portfolio import (  # noqa: E402
    PORTFOLIO_KEYS,
    PORTFOLIO_NAMESPACE,
    PORTFOLIO_SNAPSHOTS,
    catalog_payload_sha256,
    fixture_sha256,
    portfolio_sha256,
    spec_sha256,
)
from fixtures.marketplace.samples import SAMPLES, snapshot_for  # noqa: E402

LEGACY_FIXTURES = {item["title"]: item for item in SAMPLES + FS_SAMPLES}
TARGET_TITLES = {item["title"] for item in PORTFOLIO_SNAPSHOTS}
LEGACY_ADOPTION_TITLES = {
    item["legacy_title"] for item in PORTFOLIO_SNAPSHOTS if item.get("legacy_title")
}
PORTFOLIO_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, f"https://marshal.build/{PORTFOLIO_NAMESPACE}"
)
PORTFOLIO_LOCK_KEY = 6_173_903_381_814_812_413
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEMO_PORTFOLIO_PREFIX = "artifacts/demo-portfolio/v1/"
DEMO_PORTFOLIO_MANIFEST_TYPE = "marshal-demo-portfolio-rehearsal"
DEMO_PORTFOLIO_MANIFEST_VERSION = 1


class ReconcileRefused(RuntimeError):
    """A fail-closed precondition prevented any catalog mutation."""


@dataclass(frozen=True)
class LegacyCatalogIdentity:
    title: str
    row_id: uuid.UUID
    row_sha256: str


# Owner-reviewed live catalog identity manifest. These random v1 row IDs cannot
# be reconstructed from fixture content; changing an ID or fingerprint requires
# a new explicit review. All 20 rows were authored by the same reviewed admin.
LEGACY_CATALOG_AUTHOR_ID = uuid.UUID("9458aa72-e2f5-4fcf-9073-b24b57fa6c38")
LEGACY_CATALOG_ALLOWLIST = (
    LegacyCatalogIdentity(
        "Approval Workflow Copilot",
        uuid.UUID("7ab4846c-09d8-4252-8e70-04033f0d0063"),
        "06e9b850b82a74845f3c1bbd0c39cbb61e4966058849eee5eaca63436c95e11b",
    ),
    LegacyCatalogIdentity(
        "Client Meeting Note Summarizer",
        uuid.UUID("57be99a2-ce9d-47a9-855f-f63a7c74787b"),
        "c8e2f099dd3dfef29227f2dbdfd895eb9f5d22726f08533c29b3792100b08b74",
    ),
    LegacyCatalogIdentity(
        "Compliance Checker",
        uuid.UUID("cedb161d-73d1-42fd-b7ae-82b04a1c847d"),
        "aa2fce13c214d43d9eb4313dbd91c550728ad8837f1b980420cf5c362a5a14ea",
    ),
    LegacyCatalogIdentity(
        "Content Writer Studio",
        uuid.UUID("d6eea326-a518-4fa0-87da-305e27c6eb0d"),
        "7a08ada5da53d5152e3adedb3681507cce9ce76c3e69ced80ba6bf2e707ba4b8",
    ),
    LegacyCatalogIdentity(
        "Credit Memo Drafter",
        uuid.UUID("b32c43e7-d1d7-4fde-a3d1-32f2d8ffac69"),
        "5c0db4f83c01ec55c3d5fed890bdbe80200a08145e4feb1800539f4a5c7cf1a3",
    ),
    LegacyCatalogIdentity(
        "Customer Feedback Triage",
        uuid.UUID("37516269-0566-44bb-aa0a-5e09627367d0"),
        "5a1ec5a47f9a54b43a370ce075060d56769bfcaf01c5269e3da75c8d643b6403",
    ),
    LegacyCatalogIdentity(
        "Fraud Alert Triage Console",
        uuid.UUID("be2d4c0e-eea3-4188-b670-32f0b132d4ad"),
        "acff0a1310d92063773838e21d0d38478ee7df9503588d07f7b1bf6a72bc77ba",
    ),
    LegacyCatalogIdentity(
        "Invoice Parser",
        uuid.UUID("8c2e83d4-ca32-4235-81f9-954df659c91a"),
        "4a67a8ed8e9f428247ae80d14b176ab711b461e89db170049370bfc947c27b0f",
    ),
    LegacyCatalogIdentity(
        "KYC Onboarding Assistant",
        uuid.UUID("704401b3-ed6f-4a09-80d9-ce56eead585e"),
        "52b39c6f973c7e41a8ccb9e829507ab6c6f22044f1d630edf4ff575bdc56487d",
    ),
    LegacyCatalogIdentity(
        "Loan Application Triage",
        uuid.UUID("8ac308af-bc0b-4edf-a526-a01e3977c6e5"),
        "484c5d45326a0bbe10df249af20a558584ca84cfeb5d1ad54bc7e1055780362b",
    ),
    LegacyCatalogIdentity(
        "Meeting Minutes Summarizer",
        uuid.UUID("c2dcb621-3585-44ab-91e6-436875869cac"),
        "f271627f8063d1658891154d12f6240cd4ae90b4f269fc63730115690d5fa3c7",
    ),
    LegacyCatalogIdentity(
        "Onboarding Buddy",
        uuid.UUID("e63507b6-a1b0-4bc8-a5be-0ef52aa524a7"),
        "aa0037fc6932d0db3d031cda9001b8eb871153abe74659145e47a297bf3b84b0",
    ),
    LegacyCatalogIdentity(
        "Policy Q&A Bot",
        uuid.UUID("76545c8d-4c3d-4804-834c-4df111f592fd"),
        "e284b1b2e1861c42d03be30a96b8031e7be42ec691338a9168675504ea1c9023",
    ),
    LegacyCatalogIdentity(
        "Portfolio Performance Reporter",
        uuid.UUID("9c80229e-4fba-4038-bfcc-b87b9cbfaa44"),
        "bc286121bf69c4677a0945165e5851cb16e3631cb889efaaa1697289b5e60cb7",
    ),
    LegacyCatalogIdentity(
        "Regulatory Change Digest",
        uuid.UUID("2a0949d5-846a-4b13-aaa8-05c82b5e9621"),
        "e67bd067f0fb55cf5579c45b89bbf78405b3aa27fb57bab919fc47c0d768ed7a",
    ),
    LegacyCatalogIdentity(
        "Regulatory Policy Q&A",
        uuid.UUID("875c9439-b649-4ba8-b678-27a5e95b65aa"),
        "ccc07f0141e1052ac76f9277310fa65d6e8cd404907d9a9e8c23ec55fd209a79",
    ),
    LegacyCatalogIdentity(
        "Release Notes Generator",
        uuid.UUID("7d19e633-eece-47e8-bafb-fae7c83e7250"),
        "fe490dc0f3a7f6d1b352536c5cbb2e63e294e02806bd6078f4238e8b7d32c41f",
    ),
    LegacyCatalogIdentity(
        "Sales Insights Analyst",
        uuid.UUID("5693f5ae-8559-4d87-8deb-fa475ca99c51"),
        "05eadb520ccdb0aa524e5c9f4396668d48ae7574f1d51b53209207607555a9e1",
    ),
    LegacyCatalogIdentity(
        "Suitability Check Assistant",
        uuid.UUID("f502c947-881e-4835-807f-f088ac7b2316"),
        "596707f19137cdbd35e4750597e4a2e390a8b8cea27d30231a225f30c9a7d7ea",
    ),
    LegacyCatalogIdentity(
        "Trade Surveillance Case Notes",
        uuid.UUID("570e6eac-8ab0-40cc-aaf6-c9fe865bd4a6"),
        "65b1adc1314437585116cf275c496ac84d8e6dd02704b3821e504b09235ec6a6",
    ),
)
LEGACY_CATALOG_BY_TITLE = {item.title: item for item in LEGACY_CATALOG_ALLOWLIST}
LEGACY_CATALOG_BY_ID = {item.row_id: item for item in LEGACY_CATALOG_ALLOWLIST}


@dataclass
class Action:
    kind: str
    row_id: uuid.UUID
    title: str
    portfolio_key: str | None = None
    changed_fields: list[str] = field(default_factory=list)
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": self.kind,
            "id": str(self.row_id),
            "title": self.title,
        }
        if self.portfolio_key:
            result["portfolio_key"] = self.portfolio_key
        if self.changed_fields:
            result["changed_fields"] = self.changed_fields
        if self.note:
            result["note"] = self.note
        return result


@dataclass
class Plan:
    rows: dict[uuid.UUID, MarketplaceSample]
    targets: dict[str, tuple[MarketplaceSample | None, dict[str, Any], uuid.UUID, str]]
    actions: list[Action]
    before_published: int
    projected_published: int
    template_ids: dict[str, uuid.UUID]
    author_id: uuid.UUID

    def as_dict(self, *, mode: str) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        return {
            "mode": mode,
            "tenant_id": str(PLATFORM_TENANT_ID),
            "portfolio_sha256": portfolio_sha256(),
            "before_published": self.before_published,
            "projected_published": self.projected_published,
            "action_counts": dict(sorted(counts.items())),
            "actions": [action.as_dict() for action in self.actions],
            "hard_deletes": 0,
        }


def _stable_id(key: str) -> uuid.UUID:
    return uuid.uuid5(PORTFOLIO_UUID_NAMESPACE, key)


def _canonical_static_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": fixture["title"],
        "description": fixture["description"],
        "long_description": fixture["long_description"],
        "category": fixture["category"],
        "complexity": fixture["complexity"],
        "models_used": fixture["models_used"],
        "spec_snapshot": fixture["spec_snapshot"],
        "keywords": fixture["keywords"],
    }


def _legacy_static_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": fixture["title"],
        "description": fixture["description"],
        "long_description": fixture["long_description"],
        "category": fixture["category"],
        "complexity": fixture["complexity"],
        "models_used": fixture["models_used"],
        "spec_snapshot": snapshot_for(fixture),
        "assets": {},
        "keywords": fixture["keywords"],
        "metadata_extra": fixture["metadata_extra"],
    }


def _row_static_payload(row: MarketplaceSample) -> dict[str, Any]:
    return {
        "title": row.title,
        "description": row.description,
        "long_description": row.long_description,
        "category": row.category,
        "complexity": row.complexity,
        "models_used": row.models_used or [],
        "spec_snapshot": row.spec_snapshot or {},
        "assets": row.assets or {},
        "keywords": row.keywords or [],
        "metadata_extra": row.metadata_extra or {},
    }


def _legacy_payload_sha256(payload: dict[str, Any]) -> str:
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return sha256(body).hexdigest()


def _legacy_fixture_sha256(fixture: dict[str, Any]) -> str:
    return _legacy_payload_sha256(_legacy_static_payload(fixture))


def _legacy_row_sha256(row: MarketplaceSample) -> str:
    return _legacy_payload_sha256(_row_static_payload(row))


def _has_submission_provenance(row: MarketplaceSample) -> bool:
    return any(
        value is not None
        for value in (
            row.source_project_id,
            row.submitted_at,
            row.reviewed_by,
            row.reviewed_at,
            row.review_feedback,
            row.resubmission_of,
        )
    )


def _matches_legacy(
    row: MarketplaceSample,
    fixture: dict[str, Any],
    rag_template_id: uuid.UUID,
    *,
    allowed_statuses: set[str] | None = None,
) -> bool:
    identity = LEGACY_CATALOG_BY_TITLE.get(fixture["title"])
    expected_template = rag_template_id if fixture["category"] == "chatbot" else None
    statuses = allowed_statuses or {"published"}
    return bool(
        identity is not None
        and row.id == identity.row_id
        and row.title == identity.title
        and row.author_id == LEGACY_CATALOG_AUTHOR_ID
        and _legacy_row_sha256(row) == identity.row_sha256
        and not _has_submission_provenance(row)
        and row.status in statuses
        and row.template_id == expected_template
    )


def _configured_evidence_bucket() -> str:
    return (
        os.environ.get("DEMO_PORTFOLIO_EVIDENCE_BUCKET")
        or get_settings().codegen_workspace_bucket
    )


def _get_s3_object_bytes(client: Any, bucket: str, key: str) -> bytes | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        stream = response["Body"]
        try:
            return stream.read()
        finally:
            stream.close()
    except (BotoCoreError, ClientError, KeyError, OSError):
        return None


def _verified_evidence_matches_fixture(
    row: MarketplaceSample, fixture: dict[str, Any]
) -> bool:
    """Prove every preserved verification binding against private S3 bytes.

    Any absent, stale, malformed, cross-bucket, or hash-mismatched object
    invalidates verification so convergence replaces it with the canonical
    unverified fixture state. Stored database hashes are never sufficient.
    """
    metadata = row.metadata_extra or {}
    verification = metadata.get("verification") or {}
    assets = row.assets or {}
    evidence = assets.get("evidence") or {}
    if verification.get("status") != "verified" or evidence.get("status") != "verified":
        return False

    expected_bucket = _configured_evidence_bucket()
    bucket = evidence.get("bucket")
    prefix = evidence.get("prefix")
    manifest_key = evidence.get("manifest_key")
    manifest_sha = evidence.get("manifest_sha256")
    run_id = verification.get("run_id")
    last_verified = metadata.get("last_verified")
    verified_at = verification.get("verified_at")
    if (
        not expected_bucket
        or bucket != expected_bucket
        or evidence.get("private") is not True
        or not isinstance(run_id, str)
        or not run_id
        or prefix != f"{DEMO_PORTFOLIO_PREFIX}{run_id}"
        or manifest_key != f"{prefix}/manifest.json"
        or not isinstance(manifest_sha, str)
        or not SHA256_RE.fullmatch(manifest_sha)
        or verification.get("manifest_key") != manifest_key
        or verification.get("manifest_sha256") != manifest_sha
        or not isinstance(last_verified, str)
        or not last_verified
        or verified_at != last_verified
    ):
        return False

    try:
        client = boto3.client("s3", region_name=get_settings().aws_region)
    except BotoCoreError:
        return False
    manifest_body = _get_s3_object_bytes(client, bucket, manifest_key)
    if manifest_body is None or sha256(manifest_body).hexdigest() != manifest_sha:
        return False
    try:
        manifest = json.loads(manifest_body)
    except (TypeError, ValueError):
        return False
    if not isinstance(manifest, dict):
        return False

    objects = manifest.get("objects")
    expected_object_keys = {
        f"{prefix}/journeys/{portfolio_key}.json" for portfolio_key in PORTFOLIO_KEYS
    }
    if not isinstance(objects, list) or len(objects) != len(expected_object_keys):
        return False
    descriptors: dict[str, dict[str, Any]] = {}
    for descriptor in objects:
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("key"), str):
            return False
        descriptors[descriptor["key"]] = descriptor
    if set(descriptors) != expected_object_keys:
        return False
    for object_key, descriptor in descriptors.items():
        object_sha = descriptor.get("sha256")
        object_bytes = descriptor.get("bytes")
        if (
            not isinstance(object_sha, str)
            or not SHA256_RE.fullmatch(object_sha)
            or not isinstance(object_bytes, int)
            or object_bytes < 0
        ):
            return False
        object_body = _get_s3_object_bytes(client, bucket, object_key)
        if (
            object_body is None
            or len(object_body) != object_bytes
            or sha256(object_body).hexdigest() != object_sha
        ):
            return False

    key = fixture["portfolio_key"]
    journeys = manifest.get("journeys")
    journey = journeys.get(key) if isinstance(journeys, dict) else None
    if (
        manifest.get("manifest_type") != DEMO_PORTFOLIO_MANIFEST_TYPE
        or manifest.get("manifest_version") != DEMO_PORTFOLIO_MANIFEST_VERSION
        or manifest.get("run_id") != run_id
        or manifest.get("verified_at") != last_verified
        or manifest.get("portfolio_fixture_sha256") != portfolio_sha256()
        or manifest.get("secrets_included") is not False
        or not isinstance(journey, dict)
    ):
        return False

    fixture_digest = fixture_sha256(fixture)
    spec_digest = spec_sha256(fixture["spec_snapshot"])
    catalog_digest = catalog_payload_sha256(fixture)
    expected_journey = {
        "fixture_sha256": fixture_digest,
        "catalog_payload_sha256": catalog_digest,
        "spec_hash": spec_digest,
        "build_id": verification.get("build_id"),
        "content_hash": verification.get("content_hash"),
        "semantic_vector_sha256": verification.get("semantic_vector_sha256"),
        "final_deployment_status": verification.get("deployment_status"),
    }
    if journey != expected_journey or (
        verification.get("fixture_sha256") != fixture_digest
        or verification.get("spec_hash") != spec_digest
        or verification.get("catalog_payload_sha256") != catalog_digest
    ):
        return False

    manifest_screenshots = manifest.get("screenshots")
    if not isinstance(manifest_screenshots, dict):
        return False
    manifest_screenshot = manifest_screenshots.get(key)
    screenshots = assets.get("screenshots") or []
    if manifest_screenshot is None:
        return screenshots == []
    if (
        not isinstance(manifest_screenshot, dict)
        or not isinstance(screenshots, list)
        or len(screenshots) != 1
        or not isinstance(screenshots[0], dict)
    ):
        return False
    screenshot = screenshots[0]
    screenshot_key = screenshot.get("s3_key")
    screenshot_sha = screenshot.get("sha256")
    if (
        screenshot_key != manifest_screenshot.get("key")
        or screenshot_sha != manifest_screenshot.get("sha256")
        or not isinstance(screenshot_key, str)
        or not screenshot_key.startswith(f"{prefix}/screenshots/")
        or not isinstance(screenshot_sha, str)
        or not SHA256_RE.fullmatch(screenshot_sha)
        or not isinstance(manifest_screenshot.get("bytes"), int)
        or manifest_screenshot["bytes"] < 0
    ):
        return False
    screenshot_body = _get_s3_object_bytes(client, bucket, screenshot_key)
    return bool(
        screenshot_body is not None
        and len(screenshot_body) == manifest_screenshot["bytes"]
        and sha256(screenshot_body).hexdigest() == screenshot_sha
    )


def _merged_metadata(
    fixture: dict[str, Any],
    row: MarketplaceSample | None,
    *,
    preserve_verified_evidence: bool,
) -> dict[str, Any]:
    merged = deepcopy(fixture["metadata_extra"])
    if row is None:
        return merged
    current = row.metadata_extra or {}
    if preserve_verified_evidence:
        merged["verification"] = deepcopy(current["verification"])
        merged["last_verified"] = current["last_verified"]
    return merged


def _merged_assets(
    fixture: dict[str, Any],
    row: MarketplaceSample | None,
    *,
    preserve_verified_evidence: bool,
) -> dict[str, Any]:
    merged = deepcopy(fixture["assets"])
    if row is None or not preserve_verified_evidence:
        return merged
    current = row.assets or {}
    merged["evidence"].update(deepcopy(current["evidence"]))
    merged["screenshots"] = deepcopy(current.get("screenshots") or [])
    merged["demo_url"] = current.get("demo_url")
    if current.get("sample_data_s3_key"):
        merged["sample_data_s3_key"] = current["sample_data_s3_key"]
    return merged


def _desired_fields(
    fixture: dict[str, Any],
    row: MarketplaceSample | None,
    template_ids: dict[str, uuid.UUID],
    author_id: uuid.UUID,
) -> dict[str, Any]:
    template_name = fixture.get("template_name")
    preserve_verified_evidence = bool(
        row is not None and _verified_evidence_matches_fixture(row, fixture)
    )
    result = _canonical_static_payload(fixture)
    result.update(
        {
            "template_id": template_ids.get(template_name) if template_name else None,
            "assets": _merged_assets(
                fixture,
                row,
                preserve_verified_evidence=preserve_verified_evidence,
            ),
            "metadata_extra": _merged_metadata(
                fixture,
                row,
                preserve_verified_evidence=preserve_verified_evidence,
            ),
            "author_id": row.author_id
            if row is not None and row.author_id
            else author_id,
            "status": "published",
        }
    )
    return result


def _changed_fields(row: MarketplaceSample, desired: dict[str, Any]) -> list[str]:
    changed = []
    for name, value in desired.items():
        current = getattr(row, name)
        if current != value:
            changed.append(name)
    if row.published_at is None:
        changed.append("published_at")
    return sorted(changed)


async def _resolve_admin(db: AsyncSession) -> User:
    rows = list(
        (
            await db.execute(
                select(User).where(
                    User.tenant_id == PLATFORM_TENANT_ID,
                    User.email == "admin@marshal.demo",
                )
            )
        ).scalars()
    )
    eligible = [
        row
        for row in rows
        if row.role == "admin" and row.kind == "human" and row.status == "active"
    ]
    if len(rows) != 1 or len(eligible) != 1:
        ids = [str(row.id) for row in rows]
        raise ReconcileRefused(
            "expected exactly one active human admin@marshal.demo in the platform tenant; "
            f"found ids={ids}"
        )
    if eligible[0].id != LEGACY_CATALOG_AUTHOR_ID:
        raise ReconcileRefused(
            "admin@marshal.demo does not match the reviewed legacy catalog author "
            f"{LEGACY_CATALOG_AUTHOR_ID}; found {eligible[0].id}"
        )
    return eligible[0]


async def _resolve_templates(db: AsyncSession) -> dict[str, uuid.UUID]:
    # Legacy chatbot fixtures were linked to this template. Resolve it for
    # ownership fingerprinting even though the deterministic handbook target
    # intentionally removes the runtime-RAG template link.
    required = {"Standard RAG Application"} | {
        str(item["template_name"])
        for item in PORTFOLIO_SNAPSHOTS
        if item.get("template_name")
    }
    result: dict[str, uuid.UUID] = {}
    for name in sorted(required):
        rows = list(
            (
                await db.execute(
                    select(Template).where(
                        Template.tenant_id == PLATFORM_TENANT_ID,
                        Template.name == name,
                        Template.status == "active",
                    )
                )
            ).scalars()
        )
        if len(rows) != 1:
            raise ReconcileRefused(
                f"expected exactly one active template {name!r}; found {[str(r.id) for r in rows]}"
            )
        result[name] = rows[0].id
    return result


def _portfolio_key(row: MarketplaceSample) -> str | None:
    value = (row.metadata_extra or {}).get("portfolio_key")
    return value if isinstance(value, str) and value else None


def _assert_legacy_catalog_allowlist(
    rows: list[MarketplaceSample], rag_template_id: uuid.UUID
) -> None:
    fixture_titles = set(LEGACY_FIXTURES)
    allowlist_titles = set(LEGACY_CATALOG_BY_TITLE)
    allowlist_ids = set(LEGACY_CATALOG_BY_ID)
    if (
        len(LEGACY_FIXTURES) != 20
        or len(LEGACY_CATALOG_ALLOWLIST) != 20
        or len(allowlist_titles) != 20
        or len(allowlist_ids) != 20
        or allowlist_titles != fixture_titles
    ):
        raise ReconcileRefused(
            "reviewed legacy catalog allowlist must contain exactly the 20 current "
            "fixture titles with unique IDs"
        )

    rows_by_id = {row.id: row for row in rows}
    missing = sorted(str(row_id) for row_id in allowlist_ids - set(rows_by_id))
    if missing:
        raise ReconcileRefused(
            "reviewed legacy catalog rows are missing; refusing a partial archive/adoption: "
            + json.dumps(missing)
        )
    wrong_title_ids = [
        {
            "title": row.title,
            "observed_id": str(row.id),
            "reviewed_id": str(LEGACY_CATALOG_BY_TITLE[row.title].row_id),
        }
        for row in rows
        if row.title in LEGACY_CATALOG_BY_TITLE
        and row.id != LEGACY_CATALOG_BY_TITLE[row.title].row_id
    ]
    if wrong_title_ids:
        raise ReconcileRefused(
            "legacy catalog title exists outside its reviewed immutable UUID: "
            + json.dumps(wrong_title_ids, sort_keys=True)
        )

    adoption_by_legacy_title = {
        fixture["legacy_title"]: fixture
        for fixture in PORTFOLIO_SNAPSHOTS
        if fixture.get("legacy_title")
    }
    invalid: list[dict[str, Any]] = []
    for identity in LEGACY_CATALOG_ALLOWLIST:
        row = rows_by_id[identity.row_id]
        adopted_fixture = adoption_by_legacy_title.get(identity.title)
        key = _portfolio_key(row)
        if key is not None:
            metadata = row.metadata_extra or {}
            if (
                adopted_fixture is None
                or key != adopted_fixture["portfolio_key"]
                or row.title != adopted_fixture["title"]
                or metadata.get("portfolio_namespace") != PORTFOLIO_NAMESPACE
                or row.author_id != LEGACY_CATALOG_AUTHOR_ID
                or _has_submission_provenance(row)
            ):
                invalid.append(
                    {
                        "reviewed_title": identity.title,
                        "id": str(row.id),
                        "observed_title": row.title,
                        "portfolio_key": key,
                        "reason": "invalid adopted identity",
                    }
                )
            continue
        fixture = LEGACY_FIXTURES[identity.title]
        if not _matches_legacy(
            row,
            fixture,
            rag_template_id,
            allowed_statuses={"published", "archived"},
        ):
            invalid.append(
                {
                    "reviewed_title": identity.title,
                    "id": str(row.id),
                    "observed_title": row.title,
                    "author_id": str(row.author_id) if row.author_id else None,
                    "fixture_sha256": _legacy_fixture_sha256(fixture),
                    "row_sha256": _legacy_row_sha256(row),
                    "reason": "title/author/template/status/fingerprint drift",
                }
            )
    if invalid:
        raise ReconcileRefused(
            "reviewed legacy catalog identity drift requires manual review: "
            + json.dumps(invalid, sort_keys=True)
        )


def _assert_no_ambiguous_duplicates(rows: list[MarketplaceSample]) -> None:
    protected_titles = TARGET_TITLES | LEGACY_ADOPTION_TITLES
    for title in sorted(protected_titles):
        matches = [row for row in rows if row.title == title]
        if len(matches) > 1:
            raise ReconcileRefused(
                f"duplicate protected title {title!r}: {[str(row.id) for row in matches]}"
            )
    published_by_title: dict[str, list[MarketplaceSample]] = {}
    for row in rows:
        if row.status == "published":
            published_by_title.setdefault(row.title, []).append(row)
    duplicates = {
        title: [str(row.id) for row in matches]
        for title, matches in published_by_title.items()
        if len(matches) > 1
    }
    if duplicates:
        raise ReconcileRefused(f"duplicate published catalog titles: {duplicates}")


def _resolve_target(
    fixture: dict[str, Any],
    rows: list[MarketplaceSample],
    rag_template_id: uuid.UUID,
) -> tuple[MarketplaceSample | None, uuid.UUID, str]:
    key = fixture["portfolio_key"]
    keyed = [row for row in rows if _portfolio_key(row) == key]
    if len(keyed) > 1:
        raise ReconcileRefused(
            f"duplicate portfolio_key {key!r}: {[str(row.id) for row in keyed]}"
        )
    if keyed:
        row = keyed[0]
        metadata = row.metadata_extra or {}
        if metadata.get("portfolio_namespace") != PORTFOLIO_NAMESPACE:
            raise ReconcileRefused(
                f"row {row.id} uses portfolio_key {key!r} without the canonical namespace"
            )
        if _has_submission_provenance(row):
            raise ReconcileRefused(
                f"portfolio key {key!r} is attached to a submission row {row.id}"
            )
        collisions = [
            other
            for other in rows
            if other.id != row.id
            and other.title in {fixture["title"], fixture.get("legacy_title")}
        ]
        if collisions:
            raise ReconcileRefused(
                f"portfolio row {row.id} conflicts with title rows {[str(r.id) for r in collisions]}"
            )
        return row, row.id, "keyed"

    legacy_title = fixture.get("legacy_title")
    if legacy_title:
        candidates = [row for row in rows if row.title == legacy_title]
        if len(candidates) > 1:
            raise ReconcileRefused(
                f"ambiguous legacy title {legacy_title!r}: {[str(row.id) for row in candidates]}"
            )
        if candidates:
            candidate = candidates[0]
            legacy_fixture = LEGACY_FIXTURES.get(legacy_title)
            if legacy_fixture is None or not _matches_legacy(
                candidate, legacy_fixture, rag_template_id
            ):
                raise ReconcileRefused(
                    f"legacy candidate {candidate.id} ({legacy_title}) is not the current "
                    "seed fixture; refusing to overwrite manually curated content"
                )
            return candidate, candidate.id, "adopt"

    title_collision = [row for row in rows if row.title == fixture["title"]]
    if title_collision:
        raise ReconcileRefused(
            f"unkeyed target title {fixture['title']!r} already exists at "
            f"{[str(row.id) for row in title_collision]}; refusing ambiguous adoption"
        )
    row_id = _stable_id(key)
    id_collision = [row for row in rows if row.id == row_id]
    if id_collision:
        raise ReconcileRefused(
            f"stable id {row_id} for {key!r} is already occupied by {id_collision[0].title!r}"
        )
    return None, row_id, "create"


async def build_plan(db: AsyncSession, *, lock: bool) -> Plan:
    bind = db.get_bind()
    if lock and bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": PORTFOLIO_LOCK_KEY}
        )

    query = select(MarketplaceSample).where(
        MarketplaceSample.tenant_id == PLATFORM_TENANT_ID
    )
    if lock:
        query = query.with_for_update()
    rows_list = list((await db.execute(query)).scalars())
    rows = {row.id: row for row in rows_list}
    _assert_no_ambiguous_duplicates(rows_list)

    unknown_keys = {
        _portfolio_key(row): str(row.id)
        for row in rows_list
        if _portfolio_key(row) and _portfolio_key(row) not in PORTFOLIO_KEYS
    }
    if unknown_keys:
        raise ReconcileRefused(
            f"unknown portfolio keys require manual review: {unknown_keys}"
        )

    admin = await _resolve_admin(db)
    template_ids = await _resolve_templates(db)
    rag_template_id = template_ids["Standard RAG Application"]
    _assert_legacy_catalog_allowlist(rows_list, rag_template_id)

    actions: list[Action] = []
    targets: dict[
        str, tuple[MarketplaceSample | None, dict[str, Any], uuid.UUID, str]
    ] = {}
    target_row_ids: set[uuid.UUID] = set()

    for fixture in PORTFOLIO_SNAPSHOTS:
        row, row_id, resolution = _resolve_target(fixture, rows_list, rag_template_id)
        desired = _desired_fields(fixture, row, template_ids, admin.id)
        targets[fixture["portfolio_key"]] = (row, desired, row_id, resolution)
        target_row_ids.add(row_id)
        if row is None:
            actions.append(
                Action(
                    kind="create",
                    row_id=row_id,
                    title=fixture["title"],
                    portfolio_key=fixture["portfolio_key"],
                    changed_fields=sorted(desired),
                    note=f"stable fixture sha256={fixture_sha256(fixture)}",
                )
            )
            continue
        changed = _changed_fields(row, desired)
        if resolution == "adopt":
            actions.append(
                Action(
                    kind="adopt",
                    row_id=row.id,
                    title=fixture["title"],
                    portfolio_key=fixture["portfolio_key"],
                    changed_fields=changed,
                    note=f"preserve legacy row id; previous title={row.title!r}",
                )
            )
        elif changed:
            actions.append(
                Action(
                    kind="update",
                    row_id=row.id,
                    title=fixture["title"],
                    portfolio_key=fixture["portfolio_key"],
                    changed_fields=changed,
                )
            )
        else:
            actions.append(
                Action(
                    kind="unchanged",
                    row_id=row.id,
                    title=row.title,
                    portfolio_key=fixture["portfolio_key"],
                )
            )

    for row in rows_list:
        if row.id in target_row_ids or row.status != "published":
            continue
        if _has_submission_provenance(row):
            raise ReconcileRefused(
                f"published submission/manual row {row.id} ({row.title!r}) cannot be archived automatically"
            )
        legacy = LEGACY_FIXTURES.get(row.title)
        if legacy is None or not _matches_legacy(row, legacy, rag_template_id):
            raise ReconcileRefused(
                f"published row {row.id} ({row.title!r}) is not an exact current seed fixture; "
                "manual review is required before an exact-three reset"
            )
        actions.append(
            Action(
                kind="archive",
                row_id=row.id,
                title=row.title,
                note="reviewed UUID/admin/fixture fingerprint; archival-only row retained",
            )
        )

    before_published = sum(row.status == "published" for row in rows_list)
    archived = sum(action.kind == "archive" for action in actions)
    newly_published = sum(
        row is None or row.status != "published"
        for row, _desired, _row_id, _resolution in targets.values()
    )
    projected = before_published - archived + newly_published
    if projected != 3:
        raise ReconcileRefused(
            f"preflight projected {projected} published rows, not exactly three "
            f"(before={before_published}, archives={archived}, publishes={newly_published})"
        )

    return Plan(
        rows=rows,
        targets=targets,
        actions=actions,
        before_published=before_published,
        projected_published=projected,
        template_ids=template_ids,
        author_id=admin.id,
    )


async def apply_plan(db: AsyncSession, plan: Plan) -> int:
    now = datetime.now(UTC)
    for key in PORTFOLIO_KEYS:
        row, desired, row_id, _resolution = plan.targets[key]
        if row is None:
            row = MarketplaceSample(
                id=row_id,
                tenant_id=PLATFORM_TENANT_ID,
                fork_count=0,
                view_count=0,
                published_at=now,
                **deepcopy(desired),
            )
            db.add(row)
        else:
            was_published = row.status == "published"
            for name, value in desired.items():
                setattr(row, name, deepcopy(value))
            if not was_published or row.published_at is None:
                row.published_at = now

    for action in plan.actions:
        if action.kind == "archive":
            plan.rows[action.row_id].status = "archived"

    await db.flush()
    published_rows = list(
        (
            await db.execute(
                select(MarketplaceSample).where(
                    MarketplaceSample.tenant_id == PLATFORM_TENANT_ID,
                    MarketplaceSample.status == "published",
                )
            )
        ).scalars()
    )
    published_keys = sorted(_portfolio_key(row) for row in published_rows)
    if len(published_rows) != 3 or published_keys != sorted(PORTFOLIO_KEYS):
        raise ReconcileRefused(
            "postcondition failed before commit: "
            f"published={len(published_rows)} keys={published_keys}"
        )
    for row in published_rows:
        if _has_submission_provenance(row):
            raise ReconcileRefused(
                f"postcondition failed: target row {row.id} has submission provenance"
            )
        snapshot = row.spec_snapshot or {}
        if any(
            not str(snapshot.get(name, "")).strip()
            for name in ("requirements_md", "design_md", "tasks_md")
        ):
            raise ReconcileRefused(
                f"postcondition failed: target row {row.id} has incomplete snapshot"
            )
    return len(published_rows)


async def run(*, apply_reset: bool) -> None:
    async with SessionLocal() as db:
        try:
            plan = await build_plan(db, lock=apply_reset)
            mode = "apply-reset" if apply_reset else "dry-run"
            print(json.dumps(plan.as_dict(mode=mode), indent=2, sort_keys=True))
            if not apply_reset:
                await db.rollback()
                print("marketplace portfolio dry-run complete; no writes performed")
                return
            after = await apply_plan(db, plan)
            await db.commit()
            print(
                json.dumps(
                    {
                        "result": "applied",
                        "before_published": plan.before_published,
                        "after_published": after,
                        "portfolio_sha256": portfolio_sha256(),
                        "hard_deletes": 0,
                    },
                    sort_keys=True,
                )
            )
        except Exception:
            await db.rollback()
            raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="preflight and print the exact plan without writing (default)",
    )
    mode.add_argument(
        "--apply-reset",
        action="store_true",
        help="explicitly apply the convergent exact-three archive/update reset",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    try:
        await run(apply_reset=bool(args.apply_reset))
    except ReconcileRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    asyncio.run(main())
