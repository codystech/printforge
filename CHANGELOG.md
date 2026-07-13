# Changelog

## [Unreleased]

### Evolution Training Lab

- Add a versioned fail-closed Bambu Studio CLI adapter for trusted
  `cadquery-v1` candidates with immutable full-profile and pinned-binary
  fingerprints, sliced 3MF/log persistence, print-time/filament/layer/support
  evidence, warning capture, mocked CPU tests, and explicit runtime readiness.
- Block CadQuery candidate restore, production exemplar promotion, and Bambuddy
  delivery whenever deterministic geometry or slicer evidence is missing or
  failed; retain rejected candidates and captured evidence for inspection.
- Tighten fixed physical failure feedback by rejecting duplicate classes,
  requiring notes for `other`, and inheriting the candidate's slicer fingerprint.
- Add the opt-in `printforge-training-dataset-v2` contract with exact source,
  evidence coverage/masks, consent and provenance, evaluator/profile
  fingerprints, family-separated splits, immutable dataset IDs/checksums, and
  strict SFT/preference/mutation/repair/failure/print-outcome eligibility while
  retaining v1 export compatibility.
- Require physical results to match an existing run, candidate, and artifact
  checksum before joining them back into candidate, mutation, and memory
  evidence; add fixed physical-failure classes.
- Harden dataset v2 after review: separate evidence presence from pass/fail,
  require store-hashed human provenance audits for every included source, derive
  splits only from run families, make physical tuples printable-only and
  replay-idempotent with pending-first backlinks, revalidate print-outcome joins,
  allow only owned/licensed-for-training/public-domain rights, permanently
  exclude exact failed artifacts from SFT, and veto preference rows when exact
  verified physical evidence decisively contradicts their chosen direction.
- Add Training Lab controls for design family, off-by-default consent,
  reviewer/time/source/revision/license rights, and explicit v1-compatible versus
  strict-v2 export selection.
- Add the off-by-default `cadquery-v1` source contract, AST-only literal
  parameter parser, format-neutral candidate envelope, isolated Bubblewrap
  executor profile, content-addressed STEP/STL manifest, and deterministic
  B-rep/export/round-trip/mesh hard gates.
- Harden the dormant CadQuery boundary with parent-derived artifact validation,
  independent byte/file limits, symlink and unsafe-runtime-root rejection,
  explicit source/runtime availability, preserved export roles, and fail-closed
  deterministic ranking eligibility.
- Add GPU/CUDA/bitsandbytes preflight and a guarded, cache-isolated 10–50-step QLoRA smoke runner.
- Fail closed on dependency/model/review drift and hash immutable adapter evidence.
- Document model/dataset licensing, consent, immutable provenance, GPU proof, and honest ML status rules.
- Add scoped, bounded adaptive mutation learning with immutable retry-safe outcome records.
- Prevent rejected, failed, cancelled, or ineligible mutations from receiving positive credit.
- Make human-gated exemplar promotion idempotent and keep revocation independently available.
