# Working agreement

- Make and retain source changes on the user's local laptop checkout first.
- Build and test locally before publishing. Push the reviewed source commit to GitHub, then deploy that exact version to the l3novo Bitport laptop. Do not deploy to the old VPS unless explicitly requested.
- Keep the current UI available while a separately linked UI is under review. Preserve all existing routes, actions, role restrictions and backend safety checks in redesigns.
- Exercise migration, wipe, sharing, provisioning and repair controls only against an explicitly authorized sandbox when testing. Use isolated mocks for destructive UI tests. Never use customer tenant mutations as UI smoke tests.
- Do not force deployment over active jobs or repairs. Use a versioned artifact, verify hashes and health, retain rollback material, and report test limitations accurately.
