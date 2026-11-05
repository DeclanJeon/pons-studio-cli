# Standalone extraction validation

## Scope

The terminal client and its Python backend were copied into an independent repository. The original Studio checkout is preserved. No React/Tauri assets, personal databases, credentials, production outputs or upstream skill snapshots are included.

This is a development extraction, not completion of the full automatic-video-production design. Existing limitations are listed in README and docs/CLI.md. Verification was sequential self-review; no independent reviewer or live provider production is claimed.

## Local results

Environment: Linux, Python 3.14.4, existing FFmpeg/ffprobe and setuptools 78.1.1. No new packages were installed for this extraction. A temporary import path to already-installed pure-Python tomli-w was used because the system interpreter did not have it; it is not included in the repository or package.

| Check | Observed result |
| --- | --- |
| Portable Python suite, built-wheel integration enabled | 195 tests: 192 passed, 3 explicitly skipped external canonical-validator tests |
| Same suite with authorized pinned external instruction source | 195 tests passed, zero skips |
| Wheel build using existing setuptools | Successful |
| Source distribution build using existing setuptools | Successful |
| Unpacked-wheel import from outside the checkout | CLI help, backend import, console entry-point metadata and both runtime contracts loaded successfully |
| Python compileall | Successful |
| Targeted basedpyright, 13 files | 0 errors, 120 warnings; not a warning-free strict gate |
| Dependency/asset audit | No original Studio directory or third-party reference tree in wheel/sdist |
| Original-source preservation | Fingerprints of original Python source/tests and root setup documents checked unchanged |

The wheel test unpacks a built wheel and imports/runs help from a separate temporary working directory. It is not an actual package-manager installation, account login or full production run. Existing PTY tests launch the real headless backend with fake Codex and assert workspace/session state, consent, EOF/SIGINT and single-worker sharing.

The external canonical-validator profile reads an authorized local source only for technical fixture tests; it does not redistribute that source or demonstrate real creative/audio/video observations.

## Commands

With dependencies prepared in your own environment:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pons_cli server scripts
python3 -m pons_cli --help
python3 -m pons_cli doctor
```

Build using the project's declared PEP 517 backend (local validation used existing setuptools, without resolving/installing dependencies):

```bash
python3 -c "from setuptools.build_meta import build_wheel, build_sdist; build_wheel('dist'); build_sdist('dist')"
PONS_TEST_WHEEL="$PWD/dist/pons_studio_cli-0.3.0.dev0-py3-none-any.whl" \
  python3 -m unittest discover -s tests -v
```

For the optional external integration profile, set `PONS_TEST_SKILLS_SOURCE` to your authorized pinned source. Missing external instructions are reported as explicit skips rather than successful validation.

## Not verified / open risks

- Full autonomous video production and provider-specific OAuth/paid generation were not run.
- macOS, Windows, visual terminal screenshots and real installation into a new tool environment were not verified. Windows is not supported.
- Fine-grained project approvals, hard monetary caps, generic receipt reconciliation, live permission/elicitation bridging, timeline v2 and partial-rebuild semantics remain unfinished.
- Same-user loopback/browser bootstrap is not an adversarial-agent isolation guarantee. The extraction does not turn the original single-user backend into a secure public service.
- Static warnings remain in legacy/untyped JSON/SQLite/argparse boundaries and new setup integration. Ruff was unavailable; no lint-clean claim is made.
- Upstream instruction licenses and access rights must be handled separately. This repository does not invent an open-source license or vendor external instruction code.

Build products and raw local QA logs are ignored, not committed. The GitHub Actions workflow is manual-dispatch only; publishing the repository does not automatically start installation, provider calls or CI jobs.
