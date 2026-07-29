# rc-v1.0.84 failure and supersession record

## Immutable identity

- tag: `rc-v1.0.84`
- tag object: `07cf43ce503616af89606dae93f9ec9efb52f469`
- peeled commit: `ecabbde6f60327e48970fbaac853fbf722306a6b`
- tree: `ce7a1fd40c23333eca5fa824899acff12503cb6d`
- tag type: annotated
- signature: verified (SSH; GitHub `verified=true`, `reason=valid`)
- status: `failed`
- superseded_by: `rc-v1.0.85`
- authorization: `forbidden`

The tag is immutable audit evidence. It must not be moved, deleted, overwritten, force-pushed, recreated, or retried as a replacement release candidate.

## Failure evidence

- workflow: `.github/workflows/release-gates.yml`
- Release Gates run: `30479539171`
- run attempt: `1`
- event: `push`
- ref: `refs/tags/rc-v1.0.84`
- source SHA: `ecabbde6f60327e48970fbaac853fbf722306a6b`
- conclusion: `failure`
- failed job: `compose-runtime-e2e`
- check run: `90669875064`
- exit code: `1`
- missing evidence: `runtime-e2e-evidence.json`
- root cause: `scripts/compose_runtime_e2e.py` imports `loguru`, but the job did not install repository runtime dependencies before execution (`ModuleNotFoundError: No module named 'loguru'`).
- downstream skipped: `publish-attestation`, `attestation-semantics-verify`, `verify-only-3x`
- aggregate failure: `release-summary` check run `90670284331`
- candidate image RepoDigest: `ghcr.io/maxiuquan/tgjiema@sha256:120998c6192195d90e00dfea6747805db2c29e89a8c8e971e7e4c678716c8d70`
- runtime config digest: `sha256:3b6c14b51250c2336a50802a3159785264a10a1ad61e5b58a42042d131191d23`

The corrected CockroachDB native CLI probe succeeded before this independent dependency failure. The run logged a ready CockroachDB v24.1.0 instance and a non-empty `SELECT version()` response without the unsupported `-t` option or an output-truncating `head` pipeline.

The run's typed RC envelope reported `promotion_eligible=false`. Its `overall_conclusion=success` value was generated before the complete RC job set reached terminal state and is therefore not an authoritative run verdict. The superseding fix binds envelope conclusion and promotion eligibility to the complete RC-only gate set.

## Failed-run artifacts

| Artifact | ID | Digest |
| --- | ---: | --- |
| `rc-candidate-evidence-ecabbde6f60327e48970fbaac853fbf722306a6b-30479539171` | `8735079991` | `sha256:b0a76c5edcff1242832d6c737aabe8cee917379cc8a48512a59a6dc0637793c8` |
| `oci-file-manifest-ecabbde6f60327e48970fbaac853fbf722306a6b` | `8735078202` | `sha256:8918c9803980231e8e7742b331c373bd54accefde91daa58faf47dd8298e2fa7` |
| `release-gates-signed-ecabbde6f60327e48970fbaac853fbf722306a6b` | `8735046924` | `sha256:d49cc35540d959e63d6899c243640992a20ce86b1d0b3612b6fb85d20ece15c4` |
| `runtime-config-binding-ecabbde6f60327e48970fbaac853fbf722306a6b-30479539171-1` | `8735038725` | `sha256:274a169fcdf1f4cdeadbd0706e3c3767c97a60181e5b1ce6399876b2e71067c6` |
| `docker-build-info-ecabbde6f60327e48970fbaac853fbf722306a6b-30479539171` | `8735029923` | `sha256:2c0ed48bd3ec7e465a8521564b8c65ed411f696e2f407873e3a89f5f8875da22` |
| `secretless-closed-loop-30479539171-1` | `8735017087` | `sha256:ec828c3c900411730ff532eaa8893da26a928af6cb1f9ade9f91ded3ae8aff02` |
| `release-gates-sbom` | `8735017010` | `sha256:cd7a04c2e7293e0d7bee91615167d9b7136f3fb4d96332bbda6e857a27c6148b` |

All listed artifacts were bound by GitHub to run `30479539171` and head SHA `ecabbde6f60327e48970fbaac853fbf722306a6b`, and were not expired when this record was prepared. They are retained only for failed-RC audit and incident analysis. They cannot authorize production, cannot be injected into a later RC, and cannot substitute for exact-run artifacts from the superseding RC.

## Governance disclosure

Tag Ruleset `19532702` is active and requires creation/deletion/update/non-fast-forward restrictions plus signatures. Its current configuration includes repository owner `70981562` with `bypass_mode=always`; GitHub therefore recorded a creation-rule bypass when `rc-v1.0.84` was first pushed. No tag was moved, deleted, overwritten, force-pushed, or reused. This governance fact does not convert a failed run into an authorized release.
