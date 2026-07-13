# Training Lab ML Phase 0

Phase 0 establishes evidence and policy before PrintForge is allowed to train a
neural model. It does not add a training API, install packages, reserve the GPU,
download weights, or change either running service.

## Current status

As of 2026-07-13:

- The non-training preflight and opt-in QLoRA smoke runner are implemented.
- The authoring sandbox exposed the `nvidia-smi` executable but not an NVIDIA
  device, so normal-host-boot, CUDA, bitsandbytes, BF16, VRAM, and temperature
  proof remain **pending on the real host**.
- No model weights were downloaded and no smoke job was run.
- No public CAD dataset was imported or approved.
- CadQuery source-length profiling is pending Phase 1 data; OpenSCAD lengths are
  not a substitute for measuring the future `cadquery-v1` source distribution.
- `actual_training=false`, `evaluated=false`, and `deployed=false`.

The existing `POST /training-lab/api/actual-training` compatibility endpoint
therefore remains unsupported. Phase 0 tooling is deliberately not reachable
from FastAPI.

## What the preflight proves

`evolution_lab.ml_preflight` uses only the Python standard library. It checks:

- `/dev/nvidia*` ownership evidence and `nvidia-smi` health;
- GPU name, driver, total/free VRAM, and temperature;
- PyTorch import, CUDA visibility, compute capability, and BF16 support;
- bitsandbytes importability;
- exact compatibility pins for PyTorch 2.6.0, Transformers 4.51.3, PEFT
  0.15.2, TRL 0.17.0, bitsandbytes 0.45.5, Datasets 3.6.0, and Accelerate
  1.6.0 (any missing or different version fails closed);
- at least 20 GiB total GPU memory, 18 GiB currently free VRAM, and 30 GiB
  free output-disk space.

`host_gpu_bound=true` means the NVIDIA device nodes, NVIDIA management tool, and
driver all agree that the host owns a GPU. It does not identify a named NixOS
specialization. A failed check is recorded as failed or unavailable; it is never
inferred as a pass.

Run the non-destructive check from the repository root:

```sh
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  python evolution_lab/ml_preflight.py
```

To preserve a dated report in the already-gitignored Training Lab store:

```sh
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  python evolution_lab/ml_preflight.py \
  --report training_lab_data/ml-preflight/2026-07-13.json \
  --require-ready
```

The second command exits with status 1 when the host is not ready. Every disk
target must be a child of `training_lab_data/`, and an existing report is never
replaced.

## QLoRA smoke guardrails

`evolution_lab.run_qlora_smoke` defaults to a dry-run plan. A real run requires
all of the following:

- `--execute`;
- 10 through 50 optimizer steps, enforced by the argument parser;
- an immutable 40-character Hugging Face model commit in `--revision`;
- the sole allowlisted model ID, `Qwen/Qwen2.5-Coder-7B-Instruct`;
- `--review-manifest` plus its independently recorded
  `--review-manifest-sha256`; the approved manifest pins the same model commit
  and verifies archived license/model-card checksums;
- `--confirm-gpu-window` after the operator confirms organic generation and
  local inference are idle;
- no active or unverifiable NVIDIA compute process;
- a passing preflight;
- a new output directory below `training_lab_data/`;
- `--allow-download` only when the operator has approved network, cache, and
  disk impact. Downloads are otherwise disabled with `local_files_only=True`
  and Hugging Face offline flags. Hugging Face, Transformers, Datasets, Torch,
  CUDA, token, temporary, and XDG caches are redirected beneath that new run
  directory; telemetry and implicit-token discovery are disabled.

The runner never unloads Ollama, cannot detect the named NixOS passthrough
specialization, and does not implement the GPU lock shared with organic
generation. These are explicit Phase 4 blockers, not simulated Phase 0
features. Therefore it must only be used in an explicitly scheduled GPU window.

Inspect the exact plan without importing an ML package:

```sh
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  python evolution_lab/run_qlora_smoke.py
```

After a dedicated ML environment exists and the model is cached, an operator may
run a scheduled smoke test using that environment's Python:

```sh
training_lab_data/ml-env/bin/python evolution_lab/run_qlora_smoke.py \
  --execute \
  --revision REPLACE_WITH_REVIEWED_40_CHARACTER_COMMIT \
  --review-manifest training_lab_data/model-reviews/REPLACE/review.json \
  --review-manifest-sha256 REPLACE_WITH_64_CHARACTER_SHA256 \
  --steps 10 \
  --output-dir training_lab_data/ml-smoke/REPLACE_WITH_UNIQUE_RUN_ID \
  --confirm-gpu-window
```

The smoke uses the documented QLoRA shape: 4-bit NF4, nested quantization, BF16
compute, PEFT preparation for k-bit training, and LoRA rank 32 / alpha 16 /
dropout 0.05 over `all-linear` modules. It writes an adapter plus a JSON report
containing package versions, elapsed time, loss, token throughput, peak process
RAM, allocator peak VRAM, polled whole-GPU peak VRAM/temperature, and adapter
size. It trains on an exact requested-length synthetic CadQuery prompt/completion
fixture and reports prompt/completion token counts; it does not claim the short
fixture represents measured production source lengths. Throughput is named
`end_to_end_tokens_per_second` because it includes model load, training, adapter
save, and evidence writes rather than timing the optimizer loop alone. Every adapter file gets
a SHA-256 entry in a create-only immutable manifest. The runner refuses every
existing output/report path.

The first durable optimizer-step marker sets `actual_training=true` because
weights changed, including when a later save/evaluation step fails. Only all
requested steps set `qlora_forward_backward_completed=true`. The smoke still
records `evaluated=false` and `deployed=false`; its loss is environment proof
only, never evidence that a model or print improved.

The interfaces were checked against the current official documentation:
[Transformers bitsandbytes quantization](https://huggingface.co/docs/transformers/quantization/bitsandbytes),
[PEFT quantized training](https://huggingface.co/docs/peft/main/developer_guides/quantization),
[PEFT LoRA reference](https://huggingface.co/docs/peft/main/package_reference/lora),
[TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer), and
[PyTorch BF16 support](https://docs.pytorch.org/docs/stable/generated/torch.cuda.is_bf16_supported.html).

## Model review

The first supported generator target is
[`Qwen/Qwen2.5-Coder-7B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct).
Its official model card currently declares Apache-2.0. Before a real smoke run,
archive the license and model card below `training_lab_data/`, then create an
approved `printforge-model-review-v1` manifest containing their SHA-256 hashes,
the exact commit URL, reviewer, and review time. Pass the manifest's SHA-256 on
the command line; a boolean assertion is not accepted. The mutable `main` branch
is not acceptable provenance.

The 7B target is supported only through 4-bit QLoRA. Full fine-tuning, 27B-plus
models, GRPO, PPO, and continuous self-training are outside the approved scope.
A 14B experiment remains blocked until a later measured smoke leaves safe VRAM
headroom.

## Dataset license, consent, and provenance policy

No external CAD artifact is eligible merely because a downstream dataset or
repository carries an Apache, MIT, or similar label. Each imported source needs
an artifact-level review that covers the original CAD/code, any generated
derivative, redistribution, modification, model-training use, and required
attribution. Ambiguous or conflicting terms mean **do not import**.

Every external dataset snapshot must have an immutable `provenance.json` with at
least:

```json
{
  "schema": "printforge-training-provenance-v1",
  "dataset_id": "publisher/name",
  "dataset_revision": "immutable revision or checksum",
  "retrieved_at": "RFC 3339 timestamp",
  "source_urls": ["https://authoritative-source.example/dataset"],
  "artifact_sha256": {"relative/path": "hex digest"},
  "upstream_owners": ["identified owner"],
  "upstream_license": "SPDX identifier or exact license name",
  "license_text_sha256": "hex digest",
  "training_and_derivative_rights": "reviewed finding",
  "attribution_requirements": ["required attribution"],
  "consent_basis": "public-license, creator-consent, or printforge-opt-in",
  "reviewed_by": "human reviewer",
  "reviewed_at": "RFC 3339 timestamp",
  "decision": "approved, rejected, or pending",
  "notes": "scope, exceptions, and unresolved questions"
}
```

Rules:

- Only `decision=approved` data may enter a training export.
- Dataset revisions and artifact hashes are immutable; a changed upstream
  snapshot requires a new review.
- PrintForge production-library data is excluded by default. Each model needs
  explicit training consent tied to its model/artifact checksum.
- Demo, hard-rejected, failed, cancelled, or evaluator/profile-mismatched data
  is never silently promoted into eligible training data.
- Generated and physical evidence keeps its candidate, run, evaluator, slicer
  profile, and artifact-checksum lineage.
- License/provenance metadata travels with derived SFT, preference, mutation,
  repair, failure, and print-outcome rows.
- Removal or consent withdrawal blocks future exports without deleting the
  historical audit record.

## Verification and rollback

Phase 0 is isolated from the services, so rollback is a code rollback only. Any
reports or smoke adapters under `training_lab_data/` are evidence and should be
retained; none is selected for inference. If a future host dependency causes a
problem, stop using its dedicated ML environment. No NixOS generation, systemd
unit, production model pointer, or port changes in Phase 0.
