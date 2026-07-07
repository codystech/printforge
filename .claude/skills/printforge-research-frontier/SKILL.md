---
name: printforge-research-frontier
description: >
  Open-problem and novelty-positioning runbook for PrintForge. Load when picking
  the next big thing to build, assessing whether a PrintForge idea is novel,
  writing about PrintForge externally, planning research claims, or asked
  "what should PrintForge do next", "is this new", "is this state of the art",
  "what is the research frontier", "what can we publish", "what is defensible
  marketing", or "which project is most valuable next". Covers honest
  positioning, SOTA caveats, reproducibility standards, and frontier problem
  templates grounded in PrintForge code evidence.
---

# PrintForge research frontier

Use this skill to decide what is worth researching or claiming. Do not use it
to ship code directly. Any behavior-changing work must first load
`printforge-change-control`, then validate with the project validation standard.

Definitions:

- **SOTA**: state of the art, meaning the strongest public practice or research
  baseline known at the time of the claim.
- **FDM**: fused deposition modeling, the common filament-printing process where
  unsupported mid-air geometry, overhangs, wall thickness, clearance, and bed fit
  decide whether a generated model prints.
- **Ablation**: a controlled comparison that removes one mechanism at a time to
  prove which mechanism caused an improvement.
- **Reranker**: a filter that generates multiple candidates, measures each one,
  and selects the best-scoring candidate instead of trusting the first result.
- **Deterministic validator**: code that produces the same pass/fail answer for
  the same geometry, unlike an LLM or vision model review.

## Hard rules for claims

1. Claim only what the repo proves. Cite repo-relative file and line numbers.
2. Date-stamp volatile facts as **as of 2026-07-06**.
3. Treat external landscape claims as **unverified - confirm before relying**
   unless you have just checked primary sources.
4. For any external or public claim, require prompt + model + metrics + artifacts.
   Anecdotes and pretty renders are not enough.
5. Do not re-propose retired ideas: thin-wall analysis, AMF export, fake STEP
   export, scale/rotate/center helpers, cancel buttons, or format/QA library
   filters. Respect README "Later" for deferred work (`README.md:118-126`).
6. Never claim "PrintForge is SOTA" globally. Claim narrower mechanisms, and
   label them "candidate" until benchmarked.

## SOTA landscape guardrail

As of 2026-07-06, unverified - confirm before relying externally:

| Area | Common public baseline | Why PrintForge should not oversell |
|---|---|---|
| Text-to-CAD | LLM or specialized model emits parametric CAD code, such as CadQuery/OpenSCAD-like scripts, sometimes with geometric reward or benchmarks. Primary-source example to re-check: CAD-Coder (`https://arxiv.org/abs/2505.19713`, unverified); other text-to-CAD benchmarks exist — search current literature rather than trusting a remembered identifier. | "LLM writes OpenSCAD" is known territory. Novelty must come from printability and closed-loop validation, not code generation alone. |
| Image-to-3D | Diffusion/transformer image-to-3D systems generate meshes for visual asset workflows. Primary-source examples to re-check: Hunyuan3D 2.0/2.1/2.5 (`https://arxiv.org/abs/2501.12202`, `https://arxiv.org/abs/2506.15442`, `https://arxiv.org/abs/2506.16504`). | PrintForge currently wraps Hunyuan3D-2mini; that is integration, not model novelty. |
| Printability-aware generation | Research exists around manufacturability, support-free design, and additive-manufacturing constraints, but common image-to-3D demos optimize visual mesh quality first. | PrintForge can compete on FDM-grounded validation and feedback, but must prove it with a benchmark. |

## Part A: positioning

Use this table before writing a README, blog post, paper abstract, pitch, or issue
title. "Ahead" means candidate novelty only until the skeptic evidence exists.

| Bucket | Capability | Evidence in this repo | What you may claim now | What a skeptic would demand |
|---|---|---|---|---|
| Ahead candidate | Vision-QA loop with mesh-diff-driven closeups | `mesh_changes()` computes added/removed triangle-centroid regions (`app.py:337-353`); `vision_qa()` renders per-region closeups before wide views (`app.py:356-430`); README describes closeups diffed against previous state and up to 2 fixes (`README.md:41-43`). | Candidate: PrintForge combines visual review with deterministic changed-region localization for CAD refines. | Benchmark: fixed prompt set, same model, with/without closeups, metrics for requested-element presence, false fixes, render count, and human blind review. |
| Ahead candidate | Deterministic lock verification of LLM edits | Locked parts are rendered into prompt constraints (`app.py:465-477`); `module_block()` extracts module bodies by brace count (`app.py:480-496`); `lock_violations()` whitespace-normalizes locked modules and reports changes (`app.py:499-511`); refines force one correction round (`app.py:1043-1054`). | Candidate: PrintForge verifies locked module preservation with code diffs instead of trusting the LLM. | Benchmark: locked multi-part corpus, mutation attempts, false positive/negative rate, and preservation after QA rounds. |
| Ahead candidate | Intent lineage preventing regression-by-helpfulness | Parent metadata loads `intent` (`app.py:443-452`); saves last accepted prompts as `intent` (`app.py:524-538`, `app.py:1111-1112`); `intent_block()` injects "never revert" history (`app.py:543-551`); QA diffs against current state for refines (`app.py:1081-1091`). | Candidate: PrintForge persists accepted user intent and feeds it into later refines/QA. | Benchmark: regression tasks where a user intentionally removes/changes features, with/without lineage, measured restoration errors. |
| Ahead candidate | Bottom-up floating-region detection wired into generation feedback | `floating_starts()` slices bottom-up and reports islands with no support below (`parts.py:67-97`); QA injects findings into fix instructions (`app.py:407-418`); refines detect current floating starts before editing (`app.py:1015-1030`); responses include warnings (`app.py:1134-1146`); README documents the detector (`README.md:44-46`). | Candidate: PrintForge has FDM-specific support detection connected to the LLM repair loop. | Benchmark: generated STL set, slicer-reported floating regions as reference, precision/recall, repair success rate, and print outcome on real machines. |
| Ahead candidate | Deterministic assembly collision validation of LLM CAD | Prompt contract requires per-part toggles and `assembled_preview` (`prompts.py:34-42`); report can render assembled mode (`app.py:1113-1122`); `/validate` renders each part in assembled position and checks collision/clearance (`app.py:1433-1481`); README documents collision/clearance validation (`README.md:57-65`). | Candidate: PrintForge verifies LLM-generated assemblies with deterministic geometry checks. | Benchmark: assembly corpus with intentional collisions, tight fits, snap-fit exceptions, and measured false positives. |
| Known territory | LLM writes OpenSCAD | System prompt is an OpenSCAD generator contract (`prompts.py:1-14`); README says plain-English prompt to parametric OpenSCAD (`README.md:3-6`, `README.md:10-12`). | Say: PrintForge uses LLM-generated OpenSCAD. | Do not claim novelty without comparison to text-to-CAD systems. |
| Known territory | Image to 3D via off-the-shelf model | Organic mode uses Hunyuan3D-2mini defaults (`organic/generate.py:37-44`, `organic/generate.py:55-62`); README names Hunyuan3D-2 (`README.md:29-30`). | Say: PrintForge integrates local Hunyuan3D image-to-mesh. | Do not imply PrintForge trained or invented the model. |
| Known territory | Customizer sliders | `PARAM_RE` parses top-of-file customizer variables (`app.py:44-49`, `app.py:78-91`); prompt requires ranged variables (`prompts.py:5-7`). | Say: PrintForge exposes OpenSCAD parameters as sliders/text inputs. | Do not claim novelty; this is a standard parametric CAD affordance. Note: negative numeric defaults do not become sliders because `PARAM_RE` uses `[\d.]+` with no minus sign (`app.py:46-47`). |
| Marketing risk | Taste training improves outputs | Rating endpoint stores `rating` (`app.py:1353-1363`); `_taste_example()` retrieves the best thumbs-up model by keyword overlap and injects its SCAD if short enough (`app.py:1366-1389`); README describes liked examples (`README.md:54-55`). | Say: candidate taste retrieval exists. | Must prove with A/B prompts, held-out ratings, print outcomes, and enough users. Current evidence is n about 1 and retrieval is keyword overlap, not learned taste. |

Reproducibility standard for every ahead claim:

```sh
cd /home/cody/projects/printforge
# Prepare a benchmark row with:
# prompt, model/backend, input artifacts, printer profile, random seed if present,
# raw SCAD, rendered STL/3MF, validator outputs, slicer outcome, human rubric score.
# Then compare the mechanism ON vs OFF. Do not publish anecdotes.
```

Use `printforge-validation-and-qa` for the detailed evidence standard.

## Part B: frontier problems

Each frontier below is an executable research thesis, not a feature promise.
Route implementation through `printforge-change-control`.

### 1. Printability-aware organic generation

WHY current SOTA fails: unverified - confirm before relying. Image-to-3D models
usually optimize visual likeness and mesh quality, not FDM constraints such as
first-layer contact, mid-air starts, printable overhangs, removable supports,
bed fit, and durable thin features.

THIS repo's specific asset:

- Hunyuan3D-2mini local runner creates a mesh from an image (`organic/generate.py:37-62`).
- Postprocess rotates Y-up to Z-up, floors to Z=0, and shaves a flat slice when possible (`organic/generate.py:9-33`).
- Organic endpoint serializes GPU jobs and registers the output as a mesh (`app.py:1253-1309`).
- `floating_starts()` detects bottom-up islands (`parts.py:67-97`).
- Print reports measure dimensions, watertightness, part count, bed fit, and weight (`app.py:197-216`, `app.py:1113-1128`).

First three concrete steps in this repo:

1. Add a read-only experiment script under a new research branch that calls
   `organic/generate.py` with multiple seeds or backend candidates and stores
   outputs outside `library/`. Do not use the live `/organic` endpoint — AND
   note the direct runner contends for the SAME shared RTX 3090 the live
   endpoint uses: every run happens only inside an owner-scheduled GPU window,
   with the ollama brain unloaded first, per the "Non-Negotiable Safety" gate
   in `printforge-organic-quality-campaign`. Never launch it casually.
2. Run each candidate through `floating_starts()`, `print_report()`, connected
   component count, and a slicer-derived support/first-layer check if available.
3. Implement a reranker that selects the best printable candidate, then compare
   against the first candidate on the same image set.

Milestone:

You have a result when, on at least 30 fixed images, the reranked output reduces
floating-start count and slicer support warnings by at least 50% without a blind
human panel rating visual likeness worse by more than one rubric point. Measure
with saved meshes, validator JSON, slicer reports, prompts, backend names, and
human rubric sheets.

### 2. Hybrid parametric plus organic CAD

WHY current SOTA fails: unverified - confirm before relying. Code-CAD is precise
and editable but poor at sculpted organic form; mesh generators sculpt but do not
produce stable parametric dimensions, mating clearances, or editable features.

THIS repo's specific asset:

- Uploads carry roles: printable, reference, fit-cutout, assembly, negative (`app.py:1191-1208`).
- `_mesh_note()` turns roles into import contracts, excluding reference/fit meshes from printable output and deriving cutouts from them (`app.py:792-856`).
- STEP import preserves named CAD bodies and their bounds for port/cutout grounding (`app.py:601-635`, `app.py:833-845`).
- Cross-sections are sampled at five heights and injected so additions fuse to real mesh material (`app.py:648-681`, `app.py:846-855`).
- Base-mesh prompt rule 14 requires `import("<given path>")`, bans the bad z-scale footprint recipe, and provides the verified vertical emboss recipe (`prompts.py:43-79`).

First three concrete steps in this repo:

1. Build a benchmark set of organic bases plus parametric add-ons: labels, holes,
   standoffs, snap tabs, cutouts, and handles.
2. For each task, generate with roles/cross-sections enabled, then with those
   notes removed in a controlled branch or script.
3. Score fusion success, dimensional error, feature presence, and whether the
   organic mesh remained un-remodeled via `import()`.

Milestone:

You have a result when role-aware/cross-section-aware prompts beat the ablation
by at least 30 percentage points on successful fused features with no increase in
base-mesh damage. Measure from STL diffs, `mesh_changes()` regions, floating
starts, and blind human feature scoring.

### 3. Taste learning from rated library plus real print outcomes

WHY current SOTA fails: unverified - confirm before relying. Generic generators
rarely learn one user's maker taste, printer habits, or actual print outcomes;
many stop at prompt-to-mesh without closing the loop to "I liked this and it
printed well."

THIS repo's specific asset:

- Library metadata stores prompts, intent, parent links, QA, backend, reports,
  profiles, part state, and ratings (`app.py:524-538`, `app.py:1125-1131`,
  `app.py:1353-1363`; README `README.md:71-74`).
- `_taste_example()` is intentionally simple: keyword overlap against thumbs-up
  prompt/name text, requiring at least two overlapping words, then injects short
  SCAD (`app.py:1366-1389`).
- Direct Bambuddy archive upload exists (`app.py:1236-1250`; README `README.md:109-110`).
- Real library entries as of 2026-07-06 include `rating`, `qa`, `report`, and
  `profile` fields; this was verified by read-only grep of `library/*/meta.json`.

First three concrete steps in this repo:

1. Export a read-only dataset from `library/*/meta.json` plus `model.scad`,
   excluding test junk and preserving model IDs.
2. Replace keyword overlap in an experiment-only path with embeddings or a
   learned ranker that uses thumbs-up, thumbs-down, QA status, print report, and
   profile similarity.
3. Compare generated candidates with no taste example, current `_taste_example()`,
   and the new ranker on held-out prompts.

Milestone:

You have a result when held-out prompts selected by the owner produce a
statistically higher blind preference score and no worse printability metrics
than current keyword retrieval. Measure with paired A/B generations, hidden
condition labels, rating forms, validator outputs, and Bambuddy completion status
when available. Bambuddy print-outcome archive fields were not read here; mark
that subclaim "not executed here - re-verify" before publication.

### 4. Closed-loop print-failure learning

WHY current SOTA fails: unverified - confirm before relying. Most generative CAD
systems do not learn from the physical failure mode: detached support, fused snap
fit, bad clearance, weak feature, missing first-layer contact, or a print that
completed but was rejected by the user.

THIS repo's specific asset:

- `/send/{stl_id}` uploads 3MFs to Bambuddy using configured URL/API key (`app.py:1236-1250`).
- Generation metadata stores print reports and printer-profile snapshots (`app.py:1121-1128`).
- Ratings are stored in `meta.json` (`app.py:1357-1363`).
- `/config` exposes whether Bambuddy and organic features are configured (`app.py:1312-1314`).

First three concrete steps in this repo:

1. Add a read-only importer that fetches Bambuddy job completion/failure metadata
   into a separate research artifact, not into `library/`, until schema and
   privacy are approved.
2. Join job outcomes to `library/<id>/meta.json` by sent file name or explicit
   model ID, then label failure classes manually for the first small set.
3. Feed failure labels into prompts, retrieval, or candidate reranking, and test
   on repeated classes such as tight-fit assemblies or floating-start repairs.

Milestone:

You have a result when a held-out set of prompts from previously failed classes
shows a lower repeated-failure rate under the learned loop than the baseline.
Measure with Bambuddy completion data, user acceptance ratings, validator JSON,
and before/after failure taxonomy. This endpoint-level availability was not
queried here; mark Bambuddy archive schema claims "not executed here - re-verify."

### 5. Deterministic verification as a benchmark

WHY current SOTA fails: unverified - confirm before relying. Text-to-CAD
benchmarks often emphasize syntactic validity, geometric similarity, or visual
fidelity; maker workflows need "will it print, fit, preserve locked intent, and
avoid damaging an imported base."

THIS repo's specific asset:

- Render validation retries OpenSCAD failures once (`app.py:1070-1079`).
- Print report covers bounding box, watertightness, part count, weight, and bed
  fit (`app.py:197-216`).
- Floating starts are deterministic geometry checks (`parts.py:67-97`).
- Locks are deterministic module diffs (`app.py:480-511`).
- Assembly validation detects collisions and tight gaps (`app.py:1438-1481`).
- Mesh diffs localize additions/removals (`app.py:337-353`).
- Prompt rule 14 encodes empirically verified base-mesh fusion constraints (`prompts.py:43-79`).

First three concrete steps in this repo:

1. Freeze a benchmark corpus of prompts and inputs outside `library/`, including
   simple CAD, imported mesh edits, assemblies, and organic mesh add-ons.
2. Define JSON metrics: render pass, param parse pass, floating-start count,
   bed fit, watertightness, parts count, lock violations, assembly collisions,
   mesh-damage fraction, and human requested-feature score.
3. Run multiple backends/settings against the same corpus and publish scripts
   that compute metrics without touching the live service.

Milestone:

You have a result when another engineer can run one command on the frozen corpus
and reproduce a table comparing backends or mechanisms with identical prompts,
model names, inputs, validator outputs, and expected pass thresholds. The
benchmark is valuable even if PrintForge is not the top model because the
validators define practical printability targets.

## Research triage checklist

Before approving a frontier idea, answer all of these:

- Does it use a unique PrintForge asset, or is it just a standard LLM/CAD/image
  feature?
- Can the result be falsified by deterministic validators or blind human rubrics?
- Does the experiment avoid writing to `library/`, `uploads/`, `.env`,
  `presets.txt`, and `profiles.json`?
- Does it avoid retired work and respect README "Later"?
- Does it name the model/backend, prompt, profile, input artifacts, and metrics?
- Does any implementation plan load `printforge-change-control` first?

## When NOT to use this skill

| Need | Use instead |
|---|---|
| You are about to edit code, prompts, dependencies, UI, config, or deployment behavior. | `printforge-change-control` first. |
| You need to debug a broken generation, service failure, bad mesh, or endpoint symptom. | `printforge-debugging-playbook`. |
| You need settled incident history and root causes. | `printforge-failure-archaeology`. |
| You need architecture invariants or file ownership boundaries. | `printforge-architecture-contract`. |
| You need OpenSCAD syntax, CSG semantics, or prompt-rule recipes. | `printforge-openscad-reference`. |
| You need mesh, 3MF, STEP, slicing, or geometry implementation details. | `printforge-mesh-geometry-reference`. |
| You need environment setup, run stack, organic setup, or system operation. | `printforge-build-and-env` or `printforge-run-and-operate`. |
| You need diagnostics or measurement commands. | `printforge-diagnostics-and-tooling` or `printforge-proof-and-analysis-toolkit`. |
| You are running the organic-quality campaign itself. | `printforge-organic-quality-campaign`. |
| You are deciding whether a hunch graduates to an accepted result. | `printforge-research-methodology`. |

## Provenance and maintenance

Run these one-line checks before relying on this skill after repo drift:

```sh
cd /home/cody/projects/printforge && nl -ba README.md | sed -n '1,135p'
cd /home/cody/projects/printforge && nl -ba app.py | sed -n '44,91p;197,216p;303,430p;443,551p;592,683p;792,856p;990,1146p;1191,1314p;1353,1389p;1433,1481p'
cd /home/cody/projects/printforge && nl -ba parts.py | sed -n '67,120p'
cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '1,80p;247,275p'
cd /home/cody/projects/printforge && nl -ba organic/generate.py | sed -n '1,70p'
cd /home/cody/projects/printforge && nl -ba organic/setup.sh | sed -n '1,25p'
cd /home/cody/projects/printforge && find library -maxdepth 2 -name meta.json -print 2>/dev/null | wc -l
cd /home/cody/projects/printforge && rg -n '"rating"|"report"|"qa"|"profile"' library -g meta.json 2>/dev/null | sed -n '1,40p'
cd /home/cody/projects/printforge && rg -n 'Later|Thin-wall|AMF|STEP export|scale/rotate|cancel|format/QA' README.md .claude/skills/printforge-change-control/SKILL.md
cd /home/cody/projects/printforge && test -f .claude/skills/printforge-validation-and-qa/SKILL.md && sed -n '1,180p' .claude/skills/printforge-validation-and-qa/SKILL.md || echo 'validation skill missing - re-verify standard before publication'
```

External SOTA sources drift fastest. Re-check primary sources for text-to-CAD,
image-to-3D, and printability-aware generation immediately before public use.
