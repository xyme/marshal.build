"""Sandbox provider factory: SANDBOX_PROVIDER=direct|isb."""

from functools import lru_cache

from app.core.config import get_settings
from app.services.sandbox.base import LeaseInfo, ProviderCapacity, SandboxProvider
from app.services.sandbox.direct import DirectAccountProvider
from app.services.sandbox.isb import IsbHttpProvider

__all__ = [
    "LeaseInfo",
    "ProviderCapacity",
    "SandboxProvider",
    "get_sandbox_provider",
    "provider_by_name",
]


@lru_cache
def provider_by_name(name: str) -> SandboxProvider:
    """Provider by its RECORDED name (B20 R0.3): an existing lease is operated
    by the provider that created it — switching SANDBOX_PROVIDER between
    deploy and teardown must not strand the old provider's leases."""
    if name == "isb":
        return IsbHttpProvider()
    if name == "direct":
        return DirectAccountProvider()
    raise ValueError(f"Unknown sandbox provider: {name}")


@lru_cache
def get_sandbox_provider() -> SandboxProvider:
    return provider_by_name(get_settings().sandbox_provider.lower())
