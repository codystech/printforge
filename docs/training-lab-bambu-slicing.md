# Training Lab Bambu Slicing Boundary

Phase 3 adds a versioned, fail-closed Bambu Studio CLI adapter to the isolated
Training Lab. It does not install Bambu Studio or Bubblewrap, enable CadQuery,
or touch production PrintForge on port 8093.

## What the adapter does

After a `cadquery-v1` candidate passes every trusted deterministic gate, the
adapter sends its printable STL parts to Bambu Studio in a networkless
Bubblewrap scratch directory. STEP remains the canonical B-rep artifact; STL is
the tessellated slicer input. Reference, negative, and fit-cutout roles never
become positive slicer inputs.

The adapter uses the Bambu Lab documented CLI options only:

```text
--debug 3
--outputdir /work
--arrange 0
--load-settings /work/profiles/machine.json;/work/profiles/process.json
--load-filaments /work/profiles/filament.json
--slice 0
--export-3mf /work/bambu-sliced.3mf
```

Source: [Bambu Studio command-line usage](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage).

Machine, process, and filament inputs must each be full instantiated JSON
profiles. The adapter reads exact bytes into an immutable bundle, copies those
bytes read-only into private scratch, and fingerprints the raw profile hashes
together with the adapter/argv contract and a pinned Bambu Studio version and
binary SHA-256. It never consults mutable Bambu user-profile directories.

## Persisted evidence

A successful slice records and persists:

- `bambu-sliced.3mf` with a model and plate slicing payload;
- `bambu-slicer.log`, captured from stdout and stderr because the documented CLI
  does not expose a separate log-file option;
- adapter version and complete evaluator/profile fingerprint;
- estimated print seconds, filament grams, layer count, support usage, and
  warnings.

The metric parser is versioned and fixture-tested. Bambu Studio does not promise
a stable metrics JSON API, so a successful process with missing metrics is still
`slice_metrics_incomplete` and fails closed.

## Hard rejection and delivery guards

Slicing never runs when the trusted CadQuery gate reports an invalid B-rep,
failed STEP export/round trip, failed STL tessellation/mesh check, build-volume
overflow, broken hard lock, or reference-role leakage. The reason remains on
the persisted candidate.

After slicing begins, timeout/non-zero exit, missing or empty output, invalid or
unsliced 3MF, oversized output, unpinned binary identity, missing profiles, and
incomplete metrics are hard rejection reasons. The candidate and any captured
log/output remain inspectable, but `promotion_blocked=true` and
`bambuddy_send_blocked=true`; the API and UI do not offer it as a production
exemplar or restorable best.

Physical feedback still requires the exact printable
`(run_id, candidate_id, artifact checksum)` tuple. Failed prints use the fixed
failure taxonomy; duplicate classes are rejected, and `other` requires notes.
The record inherits the candidate's immutable slicer fingerprint when one
exists.

## Runtime readiness

`PRINT_FORGE_BAMBU_SLICER_ENABLED` is an off-by-default request flag. Bootstrap
separates contract support, request state, and actual readiness. Readiness stays
false until all of these are proven together in the real sidecar environment:

1. a pinned `bambu-studio` executable and SHA-256;
2. Bubblewrap;
3. complete machine/process/filament snapshots;
4. a matching real smoke record for that adapter, binary, and profile bundle.

No real slicing was run during this phase. The tests use injected subprocess
runners and synthetic STL/3MF bytes only.

## Verification

Run the focused CPU-only tests:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  --with fastapi --with httpx --with trimesh --with numpy --with scipy \
  --with python-multipart --with networkx --with lxml --with shapely \
  --with rtree --with manifold3d --with cascadio \
  python -m unittest tests.test_slicer_phase3 -v
```

Then run the full Training Lab suite with the same dependency environment.
Expected result: all tests pass without accessing the GPU, network, Library,
uploads, Bambu user profiles, or real model data.

## Rollback

Keep `PRINT_FORGE_BAMBU_SLICER_ENABLED` unset/false. Reverting
`evolution_lab/slicer.py` and the CadQuery integration removes this dormant
adapter. Persisted candidates, sliced evidence, dataset rows, and physical
records remain immutable in Training Lab storage.
