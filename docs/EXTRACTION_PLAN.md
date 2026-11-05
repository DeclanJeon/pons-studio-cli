# Standalone CLI extraction

Scope: independent Python project, with headless production backend and regression tests; preserve the original Studio checkout. No React/Tauri assets, actual credentials, databases, production outputs or third-party instruction snapshots are copied.

Execution: user-approved sequential implementation/self-review (independent agent tooling unavailable). New repo intended as private DeclanJeon/pons-studio-cli; no public release or PyPI publication.

- [x] Inspect dependencies, GitHub identity and source tests.
- [x] Copy allowlisted Python source and tests to independent project root.
- [x] Package production contracts and remove implicit sibling-reference dependency.
- [x] Add explicit external skill source configuration and portable setup instructions.
- [x] Add README, notices, ignore rules, manual CI and packaging tests.
- [x] Run source tests, wheel/sdist checks and unpacked-wheel layout smoke tests without dependency installation.
- [ ] Inspect staged content for private files/secrets, commit and create private GitHub repo.
- [ ] Push and verify remote visibility, branch and commit.

Dependency map: pons_cli -> server HTTP API -> runner/store/media/providers. runner uses tool_client.py and two instruction contracts; bootstrap needs an explicitly prepared, pinned external skill source. No frontend runtime is required.

Rollback: delete only the newly-created project if extraction is rejected. Original source remains unchanged; fingerprints are checked before delivery. No existing repo/history is rewritten.
