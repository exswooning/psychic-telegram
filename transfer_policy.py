"""Choose an available copy route by file type without widening source access."""

from __future__ import annotations

SANDBOX = ("s.sarafgloabalexim.com", "t.sarafgloabalexim.com")
NATIVE = "application/vnd.google-apps."
NATIVE_COPY_TYPES = {
    NATIVE + t for t in ("document", "spreadsheet", "presentation", "drawing")
}


def choose(settings, item, available):
    names = list(available)
    if getattr(settings, "transfer_mode", "") != "adaptive":
        return {
            "order": names,
            "reason": "configured fixed transfer mode",
            "adaptive": False,
        }
    if (
        getattr(settings, "adaptive_sandbox_only", True)
        and (settings.source_domain.lower(), settings.target_domain.lower()) != SANDBOX
    ):
        raise ValueError(
            "Adaptive transfer is qualified only for the explicit s. to t. sandbox pair"
        )
    names = [n for n in names if n not in ("move", "link_flip")]
    if getattr(settings, "source_read_only", True):
        names = [n for n in names if n != "server_side"]
    mime = item.get("mimeType", "")
    native = mime.startswith(NATIVE)
    threshold = max(0, int(getattr(settings, "adaptive_binary_server_threshold", 0)))
    size = int(item.get("size") or 0)
    if native:
        preferred = "server_side"
        if getattr(settings, "adaptive_require_native_copy", True):
            # Export/rebuild fallbacks must not silently flatten native features.
            names = [n for n in names if n == "server_side"]
        reason = (
            "native-preserving copy required"
            if preferred in names
            else "native-preserving route unavailable under source policy"
        )
    else:
        preferred = (
            "server_side" if threshold and size >= threshold else "download_upload"
        )
        reason = (
            "binary size threshold"
            if preferred == "server_side"
            else "binary byte-preserving streaming route"
        )
    order = sorted(names, key=lambda n: n != preferred)
    return {
        "order": order,
        "reason": reason,
        "adaptive": True,
        "mimeType": mime,
        "bytes": size,
        "preferred": preferred,
        "sourcePolicyApplied": True,
    }
