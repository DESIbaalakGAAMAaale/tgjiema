# rc-v1.0.83 failure and supersession record

## Immutable identity

- tag: `rc-v1.0.83`
- tag object: `8ede125291ab050cc1a83bdb94706bae7a9b6050`
- peeled commit: `e3265cb04ff6a86787e0270ff7f9f7ba31413054`
- tag type: annotated
- signature: verified (SSH)
- status: `failed`
- superseded_by: `rc-v1.0.84`
- authorization: `forbidden`

The tag is immutable audit evidence. It must not be moved, deleted, overwritten, force-pushed, or recreated under the same name.

## Failure evidence

- workflow: `.github/workflows/release-gates.yml`
- Release Gates run: `30471565857`
- event: `push`
- conclusion: `failure`
- failed job: `compose-runtime-e2e`
- check run: `90643045640`
- exit code: `4`
- missing evidence: `runtime-e2e-evidence.json`
- old error mode: CockroachDB CLI received the unsupported psql-style `-t` option and its output was truncated with `head` under `set -o pipefail`.
- fix baseline commit: `7ad6c1f341e9f6f9422a5defad2c719f6716e262`

A valid signature proves tag authorship and integrity only; it does not turn a failed RC workflow into a releasable candidate.

## Failed-run artifacts

| Artifact | ID | Digest |
| --- | ---: | --- |
| `rc-candidate-evidence-e3265cb04ff6a86787e0270ff7f9f7ba31413054-30471565857` | `8731931587` | `sha256:7d1cdfa902ba03e085c7ac65d59d330554aafbce04020badb0b57cb620dc879b` |
| `oci-file-manifest-e3265cb04ff6a86787e0270ff7f9f7ba31413054` | `8731929122` | `sha256:cbb9dcb8f4e3b7cce860755bf0a9b342fce74c653b654455baf95638a17a5c8d` |
| `release-gates-signed-e3265cb04ff6a86787e0270ff7f9f7ba31413054` | `8731895239` | `sha256:6453e845bea8999df958fa8d83511d90c89c61c60a842d9d901a89d26cd533dd` |
| `runtime-config-binding-e3265cb04ff6a86787e0270ff7f9f7ba31413054-30471565857-1` | `8731876975` | `sha256:d818ffcc27559b71af03e7adb68a678811b5ea010fa7f87ab55a6dc1168eef01` |
| `docker-build-info-e3265cb04ff6a86787e0270ff7f9f7ba31413054-30471565857` | `8731866980` | `sha256:b2acfbc99dad10f8809329f5e549e95d226632df4583e083d9416662d3fc99f6` |
| `release-gates-sbom` | `8731847743` | `sha256:143f4da0329c9cc17d0c52f0403814304fa1c76eaad2ff8a47bdd7fec805c9e5` |
| `secretless-closed-loop-30471565857-1` | `8731846159` | `sha256:6f51751b687073eac2efb479b08a34af3c843965de3c7bec74e296c686be5b82` |

These artifacts are retained only for failed-RC audit and incident analysis. They cannot authorize production, cannot be injected into a later RC, and cannot substitute for exact-run artifacts from the superseding RC.
