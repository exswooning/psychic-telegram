# Workspace UI review

The new interface is an opt-in build at `/next/`. `/app/` stays on its installed bundle.

Build locally with `npm --prefix migration-webui run build:workspace`. Output is `migration-webui/dist-workspace`; do not commit that generated directory. Its runtime calls the same authenticated APIs as the existing app. It contains no mock API or tenant fixtures. Source remains local before GitHub push and artifact deployment to l3novo.

## Included

- A migration overview with tenant context, ledger progress, seven service summaries, person search, recent activity, attention filters and per-service evidence drawers.
- A complete, role-aware tool directory and keyboard search (Ctrl K / Command K).
- All existing page components and action flows, including server-side / download-upload selection, split hybrid email, setup, identities, users, sharing, verification, repair, mirror/deltas, domain handover, reports, worker nodes, safety and operations.
- Shared Material styling, light/dark themes, mobile navigation, skip link and named icon controls.
- Page and tab entrances, staggered rows, sliding drawers, expanding panels, live-status pulses, hover and press feedback, progress transitions and animated navigation. Motion uses short opacity/transform transitions and stops when the system requests reduced motion. Operational numbers are never interpolated or fabricated.
- Preview-aware login, signup, logout and cross-page links. An account-menu link returns to the current interface.
- A page failure boundary that leaves navigation usable.
- Tenant polling begins only after authentication; overview pages reuse the existing shared poll loop.

Completion and verification are displayed separately. Missing evidence remains missing; a saved result does not imply full feature-level 1:1 fidelity. Some service totals in the ledger are attempted totals rather than an independent inventory.

## Qualification

Run the complete UI suite with `VITE_WORKSPACE_UI=1 VITE_APP_BASE=/next/ npm --prefix migration-webui test -- --maxWorkers=3 --minWorkers=1`. New tests check route coverage, role visibility, search, missing/stale evidence and page recovery. Existing tests exercise workflow requests against mocks, including guarded write actions. Browser checks use an isolated local fixture server that refuses writes. These checks do not constitute a live end-to-end tenant migration or a certification of 1:1 fidelity.

Publish the versioned UI bundle behind the existing HTTPS/Cloudflare Access hostname, using `/next/` and the same protected API/WebSocket routes. Never ship the local fixture server or modify tenant credentials/data.

## Reviewed release — 29 October 2026

The complete UI suite passed 1,148 tests with no failures or skipped tests. Both the opt-in `/next/` build and the original `/app/` compatibility build compiled. Browser checks covered desktop and 390-pixel mobile layouts, service drawers, capability search and the Jobs, People, Verification, Setup, Settings, One-to-one and All tools pages, using isolated fixtures that refuse writes. The installed original UI is preserved by hash and the migration services are not restarted by the static preview installer.
