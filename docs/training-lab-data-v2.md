# Training Lab dataset v2

`printforge-training-dataset-v2` is an additive, training-quality export. It
does not replace or reinterpret existing `printforge-training-dataset-v1`
exports. Old clients may omit `schema_version` and continue receiving v1;
clients preparing neural jobs must explicitly request v2.

## What is persistent

Every newly created run carries a validated part-family key, explicit
training-consent decision, human reviewer and timezone-aware review time, plus
source/revision/license-rights provenance. Consent is off by default in the UI.
Every candidate carries a store-issued, content-hashed provenance audit bound to
its exact immutable source artifact; candidate labels cannot change the family
split or bypass the audit. Every candidate also carries source-artifact
and SHA-256 references, the model-contract version, source prompt and validated
specification, lineage and mutation, deterministic/slicer/physical evidence,
evaluator and slicer-profile fingerprints, artifact hashes, and missing-evidence
masks. Coverage records `present` separately from `passed`, so a measured
deterministic or slicer failure is retained as observed evidence rather than
incorrectly described as missing. The versioned Phase 3 Bambu adapter now fills
these fields for eligible `cadquery-v1` candidates; unavailable legacy and
runtime-disabled paths remain explicit rather than fabricating slicer evidence. This phase
does not run Bambu Studio.

The v2 row types are:

- `sft`: accepted `cadquery-v1` source with complete deterministic and slice evidence.
- `preference`: same-prompt chosen/rejected sources with identical evaluator and slicer fingerprints.
- `mutation`: parent state, bounded action, child result, reward delta, and joined physical outcomes.
- `repair`: verified failed parent plus an accepted repaired child.
- `failure`: source with a verified failure taxonomy and deterministic evidence.
- `print_outcome`: exact geometry/slicer/profile features joined to a physical result.

## Eligibility and consent

V2 fails closed. Demo, cancelled, generation-failed, hard-rejected, unconsented,
unaudited/unknown-provenance, missing-family, or incomplete-evidence records do
not become training rows. Every included source—including mutation and repair
parents—needs deterministic and successful slicer evidence plus evaluator and
slicer fingerprints. A model that only rendered is not an SFT example. An SFT
source with a verified physical failure is permanently excluded for that exact
candidate and artifact: metadata approval cannot relabel a failed print. A
repair must be a new candidate/source and printable-artifact checksum with its
own evidence trail. Evolving a
production-library model remains excluded until the user explicitly sets
`training_consent=true` and records acceptable provenance (`self-created`,
`verified`, or `licensed`). Training rights are an independent explicit
allowlist: `owned`, `licensed_for_training`, or `public_domain`; arbitrary
license-rights strings fail API validation and dataset eligibility.

Preference rows re-read exact physical records and their candidate,
mutation (when applicable), and memory backlinks. Consistent chosen-success /
rejected-failure evidence receives physical authority. Decisive verified
evidence in the opposite direction vetoes the row instead of being downgraded
to a weaker label.

All siblings share a deterministic split derived from `part_family_split_key`.
This prevents a parent, child, repair, or A/B sibling from leaking into a
different train/validation/test split.

## Physical evidence

`POST /training-lab/api/physical-validations` now requires all three identity
fields:

```json
{
  "run_id": "run_...",
  "candidate_id": "candidate_...",
  "artifact_checksum": "sha256:..."
}
```

The checksum must match an immutable artifact with the exact `printable` export
role already recorded for that candidate; source, reference, negative and
metadata artifacts are rejected. The tuple has a deterministic ID. Exact
replays return the existing record without duplicating candidate or memory
evidence, while a conflicting result for the same tuple is rejected.

The physical record is written as `pending` first. It becomes
`verified_join=true` only after the candidate, required mutation outcome, and
all applied memory rules have durable backlinks. A missing backlink leaves the
record pending and excludes it from datasets. Export revalidation also checks
that the referenced mutation record exists and that its run, candidate,
generation, artifact, and physical backlink match exactly; persisted flags by
themselves are not trusted. Failed prints require
one or more fixed failure classes; successful prints cannot carry failure
classes. This records evidence only and never activates a production rule.

## Export and verification

Request v2 by setting `schema_version`:

```json
{
  "dataset_type": "preference",
  "format": "jsonl",
  "schema_version": "printforge-training-dataset-v2"
}
```

The Training Lab export selector defaults to v1 compatibility and explicitly
labels v2 as strict training-quality data. The response includes an immutable content-derived `dataset_id`, exported-file
`checksum`, examples checksum, and identity checksum. Repeating the same export
returns the same record rather than overwriting it.

Run the isolated tests:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  --with fastapi --with httpx --with trimesh --with numpy --with scipy \
  --with python-multipart --with networkx --with lxml --with shapely \
  --with rtree --with manifold3d --with cascadio \
  python -m unittest discover -s tests -v
```

Expected result: all tests pass without model, slicer, GPU, library, or upload access.

## Rollback

Revert the code/NixOS generation if needed and request v1 exports again. Dataset,
candidate, physical, and mutation evidence remains intact under
`training_lab_data/`; do not delete it during rollback. No model weights are
updated and `actual_training=false` remains accurate.
