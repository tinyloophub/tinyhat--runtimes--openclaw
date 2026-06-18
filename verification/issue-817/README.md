# Issue 817 verification artifacts

These artifacts capture the command output used to verify the Tiny Runtime M1
follow-up fixes on this PR.

## Reader-facing evidence

- `terminal-evidence.md` contains formatted, public-safe terminal output for
  the static gates, focused runtime tests, full-suite summary, Docker proof,
  and legacy dev-image build.

## Raw logs

- `tiny-runtime-tests.txt` contains the focused runtime test output.
- `docker-build.txt` contains the Docker build output for
  `tinyhat-openclaw-runtime:tiny-runtime-m1`.
- `docker-attestation.txt` contains the Docker run attestation output.
- `legacy-dev-docker-build.txt` contains the legacy dev-image build output for
  `tinyhat-openclaw-runtime:issue-817-legacy-dev-check`.
- `static-gates.txt` contains static gate output for whitespace, dev-skill,
  shell syntax, and compile checks.

The full unittest stream is intentionally not committed because existing tests
emit local temp paths. `terminal-evidence.md` records the public-safe full-suite
summary instead.
