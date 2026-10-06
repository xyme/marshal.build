"""Model provider adapters (S17). The Bedrock seam dispatches here for
non-Bedrock registry entries; each adapter honors the full seam contract:
preflight, clamped inference config, invocation recording, retry discipline.
"""
