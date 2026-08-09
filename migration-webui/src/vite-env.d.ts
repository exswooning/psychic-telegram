/// <reference types="vite/client" />

// Typed here rather than relying on the ambient default so a mistyped env
// var is a compile error, not an undefined at runtime pointing the control
// plane at the wrong host.
interface ImportMetaEnv {
  /** Base URL of api_server.py (the control plane). Defaults to
   *  http://localhost:8090 — i.e. through the SSH tunnel, not the VPS's
   *  public interface. */
  readonly VITE_CP_BASE?: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}
