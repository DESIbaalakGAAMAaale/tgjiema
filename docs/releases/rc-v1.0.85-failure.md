# rc-v1.0.85 failure and supersession record

## Immutable identity

- tag: `rc-v1.0.85`
- tag object: `e3ff8cb55f65911ade4e733326306f1524b611ce`
- peeled commit: `811b0890fa96aa7070e544938b0d420f31cefc20`
- tree: `dcfea306f144f8d908320c73f7b1564c797098ba`
- tag type: annotated
- signature: verified (SSH; GitHub `verified=true`, `reason=valid`)
- status: `failed`
- superseded_by: `rc-v1.0.86`
- authorization: `forbidden`

The tag is immutable audit evidence. It must not be moved, deleted, overwritten, force-pushed, recreated, or retried as a replacement release candidate.

## Exact failed-run identity

- workflow: `.github/workflows/release-gates.yml`
- Release Gates run: `30482688749`
- run attempt: `1`
- event: `push`
- ref: `refs/tags/rc-v1.0.85`
- source SHA: `811b0890fa96aa7070e544938b0d420f31cefc20`
- conclusion: `failure`
- failed job: `compose-runtime-e2e`
- failed check run: `90680806210`
- aggregate failure: `release-summary`
- aggregate check run: `90681620886`
- process exit code: `1`
- first failed phase: `start_infrastructure`
- phase command result: `docker compose up -d redis db_writer` exited `1`
- downstream required phases: `12` skipped by fail-closed DAG propagation
- cleanup/diagnostic phase: `final_identity_and_cleanup` failed; `evidence_signing` executed for diagnostics

## Root cause and superseding fix

The workflow first started the Secretless infrastructure with `docker-compose.yml` plus `docker-compose.secretless.yml`, but `scripts/compose_runtime_e2e.py` then hard-coded `docker-compose.prod.yml` for its own commands. The migration container consequently received the invalid fail-closed combination `APP_ENV=production` and `SECRETLESS_MODE=true`; `config/settings.py` correctly rejected it.

The superseding change makes one ordered Compose identity authoritative for infrastructure startup, every orchestrator command, preflight, digest calculation, and runtime evidence:

1. `docker-compose.prod.yml`
2. `docker-compose.secretless.yml`
3. `docker-compose.rc-candidate.yml`

The RC candidate overlay binds every application role to the exact `${TGJIEMA_IMAGE}` RepoDigest, declares no local application build, and supplies the production-only `r40_scheduler` role with the Secretless CockroachDB/provider environment.

## Release identity observed before failure

- candidate image RepoDigest: `ghcr.io/maxiuquan/tgjiema@sha256:acd51f20febc711628bb2515f6000da71526bf68730504b7ce715dac796a0cea`
- authoritative runtime config digest: `sha256:3b6c14b51250c2336a50802a3159785264a10a1ad61e5b58a42042d131191d23`
- legacy single-file Compose digest emitted by the failed runtime artifact: `sha256:2ef1443fcd888eb7b8ac0cc228ef7c7bd38fcf379c7fe338dcb61f479801fd4f`

The CockroachDB native CLI probe and the runtime dependency installation both succeeded before this independent Compose-identity failure. Provider simulator, MinIO, and the isolated CockroachDB service also started successfully.

## Evidence-envelope conflict

The authoritative run-level RC envelope correctly recorded:

```json
{
  "gate_level": "rc",
  "overall_conclusion": "failure",
  "promotion_eligible": false,
  "ref": "refs/tags/rc-v1.0.85",
  "source_sha": "811b0890fa96aa7070e544938b0d420f31cefc20",
  "run_id": 30482688749,
  "run_attempt": 1,
  "image_repo_digest": "ghcr.io/maxiuquan/tgjiema@sha256:acd51f20febc711628bb2515f6000da71526bf68730504b7ce715dac796a0cea",
  "runtime_config_digest": "sha256:3b6c14b51250c2336a50802a3159785264a10a1ad61e5b58a42042d131191d23"
}
```

The runtime stage-level diagnostic envelope incorrectly recorded `overall_conclusion=success` and `promotion_eligible=true` because `phase_evidence_signing()` hard-coded success and did not receive the preceding DAG results. This diagnostic envelope was not the authoritative promotion artifact and could not override the failed workflow, but it was conflicting evidence. The superseding fix aggregates all 15 required preceding phases and forces `failure/false` for any missing, duplicate, failed, skipped, or unknown phase status. Only one complete all-pass phase set may produce `success`, and eligibility additionally requires an RC tag push plus both immutable digests.

## Failed-run artifacts

| Artifact | ID | Digest |
| --- | ---: | --- |
| `rc-candidate-evidence-811b0890fa96aa7070e544938b0d420f31cefc20-30482688749` | `8736429278` | `sha256:939533061a0553d5e1b963cb6c7f1bd4b3416f8f02aac93ecea8efe45b387afe` |
| `runtime-e2e-evidence` | `8736416140` | `sha256:b80d4f4299c98c0a0b9b9775305d8ab04381aa135406b3d31dc19a81e7535664` |
| `oci-file-manifest-811b0890fa96aa7070e544938b0d420f31cefc20` | `8736376598` | `sha256:ae9316ef7630867090e62b1453d3e9114e35f92a9b4cc134a21a25125dc770c4` |
| `release-gates-signed-811b0890fa96aa7070e544938b0d420f31cefc20` | `8736354537` | `sha256:52b580e59b3c9e223a63fc6e86473748135b6e644db3ff19de75a0da980cb328` |
| `runtime-config-binding-811b0890fa96aa7070e544938b0d420f31cefc20-30482688749-1` | `8736340341` | `sha256:20ab7e822e435a0d34b59ab1bd0e27721756da876237a9c1695d82daeb91c8e5` |
| `docker-build-info-811b0890fa96aa7070e544938b0d420f31cefc20-30482688749` | `8736330416` | `sha256:ce0bec9ef1e5aeef67eecf0f64ab24cc14c48e42d73c899468ec80b8e3fedf60` |
| `release-gates-sbom` | `8736310851` | `sha256:89588cf2398663f83ee988b5ce643e54836544bd15b441075446cd16c4e09f95` |
| `secretless-closed-loop-30482688749-1` | `8736304248` | `sha256:4bc1547536a6136a403d6615f5feec763d11ecf2196014a59222bdf22e7213ba` |

All listed artifacts were bound by GitHub to run `30482688749` and head SHA `811b0890fa96aa7070e544938b0d420f31cefc20`, and were `expired=false` when this record was prepared. They are retained only for failed-RC audit and incident analysis. They cannot authorize production, cannot be injected into a later RC, and cannot substitute for exact-run artifacts from the superseding RC.

## Governance disclosure

Tag Ruleset `19532702` is active and requires creation/deletion/update/non-fast-forward restrictions plus signatures. Its current configuration includes repository owner `70981562` with `bypass_mode=always`; GitHub therefore recorded a creation-rule bypass when `rc-v1.0.85` was first pushed. No tag was moved, deleted, overwritten, force-pushed, or reused. This governance fact does not convert a failed run into an authorized release.

Local `git verify-tag` and GitHub's tag-object verification both remain valid for `rc-v1.0.83`, `rc-v1.0.84`, and `rc-v1.0.85`. Their immutable object identities are unchanged. Production authorization remains forbidden until a later, distinct RC completes every required exact-tag gate successfully.
