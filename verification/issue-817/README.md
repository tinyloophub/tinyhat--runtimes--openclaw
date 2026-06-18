# Issue 817 verification artifacts

These artifacts capture the command output used to verify the Tiny Runtime M1
follow-up fixes on this PR.

## Screenshots

- `tiny-runtime-tests.png` renders the focused runtime test output from
  `python -m unittest tests.test_tiny_runtime -v`.
- `docker-attestation.png` renders the runtime attestation output from
  `docker run --rm tinyhat-openclaw-runtime:tiny-runtime-m1`.
- `verification-summary.png` renders the static gates and final summary lines
  from the larger verification commands.

## Raw logs

- `tiny-runtime-tests.txt` contains the focused runtime test output.
- `full-suite.txt` contains the full local unit-suite output.
- `docker-build.txt` contains the Docker build output for
  `tinyhat-openclaw-runtime:tiny-runtime-m1`.
- `docker-attestation.txt` contains the Docker run attestation output.
- `legacy-dev-docker-build.txt` contains the legacy dev-image build output for
  `tinyhat-openclaw-runtime:issue-817-legacy-dev-check`.
- `static-gates.txt` contains static gate output for whitespace, dev-skill,
  shell syntax, and compile checks.
- `verification-summary.txt` contains the text source for the summary
  screenshot.
