# Issue 817 terminal evidence

Terminal evidence is captured as formatted text so reviewers can copy, search,
and inspect it without looking at screenshots. Long local logs are summarized
instead of committed raw when they contain machine-specific paths.

## Static gates

```text
$ git diff --check
[exit=0]

$ python3 scripts/check_dev_skills.py
dev-skills: ok (parent skill root not mounted; standalone mode)
[exit=0]

$ bash -n bootstrap.sh dev/entrypoint.sh tiny_runtime/install.sh tiny_runtime/bake/assemble-bundle.sh tiny_runtime/bake/verify-bundle.sh tiny_runtime/dev/entrypoint.sh tiny_runtime/dev/fake-openclaw
[exit=0]

$ python -m compileall -q supervisor.py tinyhat_cli tiny_runtime
[exit=0]
```

## Focused Tiny Runtime tests

```text
$ python -m unittest tests.test_tiny_runtime -v
test_attestation_is_non_secret_and_names_runtime_generation ... ok
test_assemble_bundle_round_trip ... ok
test_bundle_id_is_content_addressed ... ok
test_bundle_lock_uses_pinned_public_refs ... ok
test_dev_dockerfile_reads_component_refs_from_bundle_lock ... ok
test_activation_flips_current_symlink ... ok
test_activation_rolls_back_when_health_fails ... ok
test_adapter_itself_reaches_openclaw ... ok
test_adapter_prefers_bundle_local_openclaw_path ... ok
test_adapter_reports_missing_openclaw_without_raising ... ok
test_adapter_uses_injected_runner_for_official_commands ... ok
test_no_openclaw_access_outside_adapter ... ok
test_scanner_flags_a_violation ... ok
test_client_only_builds_me_urls ... ok
test_attestation_unit_uses_stable_current_path ... ok
test_gateway_unit_uses_stable_current_path_and_is_enabled_on_boot ... ok

----------------------------------------------------------------------
Ran 16 tests in 0.197s

OK
```

## Full local suite summary

The full raw unittest stream includes intentionally-created temp paths from
existing supervisor tests, so only the public-safe result summary is committed.

```text
$ python -m unittest tests.test_supervisor tests.test_tinyhat_cli tests.test_extraction_guards tests.test_command_lock tests.test_capability_check tests.test_tiny_runtime -v
...
test_gateway_unit_uses_stable_current_path_and_is_enabled_on_boot ... ok

----------------------------------------------------------------------
Ran 405 tests in 6.645s

OK
```

## Tiny Runtime Docker proof

```text
$ docker build -f tiny_runtime/dev/Dockerfile -t tinyhat-openclaw-runtime:tiny-runtime-m1 .
...
#9 naming to docker.io/library/tinyhat-openclaw-runtime:tiny-runtime-m1 done
#9 unpacking to docker.io/library/tinyhat-openclaw-runtime:tiny-runtime-m1 0.0s done
#9 DONE 0.1s
```

```json
{
  "bundle_id": "sha256:b6047b5ec6afe1d464883fd09568cb761d6ee5e2ad95a4a439f65fe18c3f30c1",
  "components": {
    "openclaw": {
      "package": "openclaw",
      "ref": "openclaw@2026.6.8"
    },
    "runtime": {
      "ref": "docker-dev",
      "repo": "https://github.com/tinyloophub/tinyhat--runtimes--openclaw.git"
    },
    "tinyhat_openclaw_plugin": {
      "ref": "9e564878f6057a6c66fa2047b265caa3389314e2",
      "repo": "https://github.com/tinyhat-ai/tinyhat.git"
    }
  },
  "identity": {
    "assignment_id": "dev-assignment",
    "computer_id": "dev-computer",
    "platform_base_url": "https://platform.example.invalid",
    "runtime_ref": "tiny-runtime-m1"
  },
  "openclaw": {
    "gateway": {
      "state": "healthy"
    },
    "models": {
      "state": "ready"
    },
    "plugin": {
      "state": "ready"
    },
    "schema": "openclaw_adapter_attestation_v1"
  },
  "runtime_generation": "tiny_runtime",
  "schema": "tiny_runtime_attestation_v1"
}
```

## Legacy dev image build

```text
$ docker build -f dev/Dockerfile -t tinyhat-openclaw-runtime:issue-817-legacy-dev-check .
...
#15 naming to docker.io/library/tinyhat-openclaw-runtime:issue-817-legacy-dev-check done
#15 unpacking to docker.io/library/tinyhat-openclaw-runtime:issue-817-legacy-dev-check done
#15 DONE 0.0s
```
