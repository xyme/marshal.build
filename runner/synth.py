"""CDK synth + packaging inside the runner (cdk-artifacts spec R2).

Assembles a working CDK app from templated scaffolding + generated sources,
runs `cdk synth` with the vendored offline closure, and packages the cloud
assembly: synthesized template + zipped assets + the asset→parameter mapping
the platform's deployer binds at create_stack time.

Toolchain lives HERE (Node + pinned aws-cdk-lib baked into the runner image),
never on the backend.
"""

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

CDK_TEMPLATE_DIR = Path(os.environ.get("CDK_TEMPLATE_DIR", "/opt/cdk-template"))
CDK_CLOSURE_DIR = Path(os.environ.get("CDK_CLOSURE_DIR", "/opt/cdk-closure"))
STACK_NAME = "GeneratedApp"


class SynthFailure(Exception):
    def __init__(self, message: str, output: str = ""):
        self.output = output
        super().__init__(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assemble_workdir(workdir: Path, generated: dict[str, str]) -> None:
    """Templated scaffolding + generated sources + vendored node_modules."""
    workdir.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "cdk.json", "tsconfig.json"):
        shutil.copy(CDK_TEMPLATE_DIR / name, workdir / name)
    (workdir / "bin").mkdir(exist_ok=True)
    shutil.copy(CDK_TEMPLATE_DIR / "bin" / "app.ts", workdir / "bin" / "app.ts")
    for rel_path, content in generated.items():
        target = workdir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    # Vendored closure: no network, no arbitrary installs (supply-chain stance)
    os.symlink(CDK_CLOSURE_DIR / "node_modules", workdir / "node_modules")


def run_synth(workdir: Path) -> Path:
    """`cdk synth` → cdk.out. Raises SynthFailure with compiler output."""
    env = {
        **os.environ,
        "CDK_DISABLE_VERSION_CHECK": "1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    result = subprocess.run(
        ["npx", "cdk", "synth", "--no-version-reporting", "--no-lookups", "-o", "cdk.out"],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        output = (result.stderr or "") + "\n" + (result.stdout or "")
        raise SynthFailure("cdk synth failed", output=output.strip()[-8000:])
    out = workdir / "cdk.out"
    if not (out / f"{STACK_NAME}.template.json").exists():
        raise SynthFailure("synth produced no template", output="")
    return out


def package_assembly(cdk_out: Path, staging_dir: Path) -> tuple[str, list[dict]]:
    """Read the cloud assembly; zip assets; return (template_body, assets).

    assets: [{id, source_hash, zip_path, bucket_parameter, key_parameter,
              hash_parameter, bytes, sha256}] — the deployer's binding data.
    """
    template_body = (cdk_out / f"{STACK_NAME}.template.json").read_text()
    manifest = json.loads((cdk_out / "manifest.json").read_text())
    staging_dir.mkdir(parents=True, exist_ok=True)

    assets: list[dict] = []
    stack_artifact = (manifest.get("artifacts") or {}).get(STACK_NAME) or {}
    metadata = stack_artifact.get("metadata") or {}
    for entries in metadata.values():
        for entry in entries:
            if entry.get("type") != "aws:cdk:asset":
                continue
            data = entry.get("data") or {}
            if data.get("packaging") not in ("zip", "file"):
                raise SynthFailure(
                    f"unsupported asset packaging {data.get('packaging')!r}", output=""
                )
            source = cdk_out / data["path"]
            source_hash = data.get("sourceHash", "")
            zip_path = staging_dir / f"{source_hash}.zip"
            if data["packaging"] == "zip":
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for file in sorted(source.rglob("*")):
                        if file.is_file():
                            archive.write(file, file.relative_to(source))
            else:  # single file asset — wrap as the S3 object directly
                shutil.copy(source, zip_path)
            assets.append(
                {
                    "id": data.get("id", source_hash),
                    "source_hash": source_hash,
                    "zip_path": str(zip_path),
                    "bucket_parameter": data["s3BucketParameter"],
                    "key_parameter": data["s3KeyParameter"],
                    "hash_parameter": data["artifactHashParameter"],
                    "bytes": zip_path.stat().st_size,
                    "sha256": _sha256_file(zip_path),
                }
            )
    return template_body, assets
