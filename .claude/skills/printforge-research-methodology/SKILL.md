---
name: printforge-research-methodology
description: >
  Research discipline for turning a PrintForge hunch, idea, experiment, or stuck
  investigation into an accepted result. Load when designing an experiment, deciding
  whether evidence is sufficient, writing before/after criteria, evaluating a proposed
  prompt rule or validator, deciding whether to adopt or retire an idea, or when an
  investigation is going in circles. Trigger phrases and symptoms: "I have a hunch",
  "can we prove this", "is this enough evidence", "experiment design", "pre-register",
  "adversarial refutation", "going in circles", "what should we work on", "retire this
  idea", "Later list", "QA_ROUNDS tradeoff", "z-scale diagnosis", "detector calibration",
  "2-layer lookback", "print archive benchmark". (For evaluating a concrete organic
  backend such as Sparc3D or TRELLIS, load printforge-organic-quality-campaign.)
---

# PrintForge research methodology

Use this skill to decide what is true enough to change PrintForge. It is not a debugging
shortcut and it is not permission to ship. If an idea changes project behavior, route the
change through **printforge-change-control** after the evidence is gathered.

Definitions:

| Term | Meaning here |
|---|---|
| Hypothesis | One proposed mechanism that explains the observations. |
| Negative | An observation where the symptom does **not** appear. Negatives matter as much as failures. |
| Pre-registration | Writing the expected numeric outcome before running the experiment. |
| Adversarial refutation | Actively trying to break your own hypothesis before accepting it. |
| Seam | A small replaceable boundary, such as an env flag or runner script, where an experiment can be swapped without rewriting the app. |
| Maintainer testimony | Historical claim from the outgoing maintainer that is not fully reproducible from current repo artifacts; treat as useful but not proof. |

## The evidence bar

Accept a result only when one mechanism explains **all** observations, including the
negatives, and survives an adversarial-refutation pass.

Checklist before changing code or prompt contracts:

- Write the mechanism in one sentence.
- List every observation in a table: symptom, model/file, expected if hypothesis true, actual.
- Add at least one negative control: a model, prompt, or file where the bug should not happen.
- Try to disprove the mechanism with the cheapest test first.
- If the mechanism explains only one symptom, keep investigating.
- If the fix changes behavior, continue in **printforge-change-control**.

Worked case: z-scale diagnosis.

| Observation | Required explanation |
|---|---|
| Flat slivers after a base-mesh refine | `scale([1,1,big]) import(...)` stretches the mesh bottom slice, not the outline. |
| Disconnected floating fragments | The clipped feature no longer overlaps/supports the intended base footprint. |
| Bambu Studio rejected the 3MF | The generated geometry included unsupported/disconnected floating regions. |
| Correct recipe now banned in prompt contract | `prompts.py` rule 14b bans z-scale footprint clipping and points to `linear_extrude(h) projection() import(path)` or cross-section coordinates. |

Code evidence: `prompts.py:43-79` contains base-mesh fusion rule 14; `prompts.py:53-58`
bans the z-scale footprint recipe; `git show 1461eb2 -- prompts.py` shows the broken
recipe being replaced. The exact user-visible chain from slivers to Bambu rejection is
maintainer testimony unless reproduced from the saved user artifact.

## Predict numbers before running

Do not run open-ended experiments. Before execution, write:

```text
Hypothesis:
Expected numeric observation:
Failure threshold:
Model/data used:
Why this model/data is not live user data:
```

Historical patterns to copy:

| Case | Pre-registered expectation | Result status |
|---|---|---|
| Floating-region detector calibration | Sub-4mm2 islands are spot-support noise; report only `report_area >= 4.0` and dedupe per XY column. | Confirmed in code: `parts.py:67-97`; `git show ee7391c -- parts.py` added `report_area=4.0`, 2-layer lookback, and dedupe. The "16 boat findings" count is maintainer testimony unless re-run on the saved boat artifact. |
| `QA_ROUNDS` tradeoff | Multi-round vision QA should fix creative-addition failures at about 5-10 minutes of codex cost. | Confirmed in code: `QA_ROUNDS` default is 2 at `app.py:28-29`; loop at `app.py:1092-1109`; `git show 23b53f1 -- app.py prompts.py` introduced the loop. The "robot + wheel landed in 2 rounds" result and elapsed cost are maintainer testimony unless re-run. |
| 2-layer lookback | Thin cylinders create section noise; keeping two previous layers should suppress false floats. | Confirmed in code: `parts.py:70-71` documents the purpose; `parts.py:75-97` keeps `below = ([polys] + below)[:2]`. |

Use the same standard for new work. Example:

```text
Hypothesis: A third QA round catches missing decorative modules better than two rounds.
Expected: on 6 non-live benchmark prompts, pass rate improves by >=2 prompts while median time increases by <=5 minutes.
Failure threshold: improvement <=1 prompt or any unrelated module deletion.
```

## Idea lifecycle

Move every idea through this path:

1. Park it in the official parking lot if it is not ready.
2. Run a cheap, isolated experiment behind a flag or seam.
3. Measure before/after using **printforge-validation-and-qa**.
4. Adopt it with code and README feature-list updates, or retire it with a written reason.

The official parking lot is `README.md` section `Later (deliberately not built yet)`.
As of 2026-07-06 it lists Sparc3D/TRELLIS.2 evaluation, thumbs-down negatives,
Bambu-native paint metadata, text-only figurines, custom profile editor UI, and
thin-wall analysis skipped because the slicer does it better (`README.md:118-126`).

Use existing seams:

| Seam | Verified location | Use |
|---|---|---|
| QA enable/rounds flags | `QA_CHECK` and `QA_ROUNDS` in `app.py:28-29`; loop in `app.py:1081-1109` | Try QA behavior changes without making them permanent defaults. |
| Codex edit-in-place | `call_codex_edit`, `app.py:119-145`; refine path `app.py:1015-1061` | Test refine strategies without full-file rewrites. |
| Organic runner script | `app.py:1294-1299` shells to `organic/generate.py`; `organic/generate.py:36-44` has `--model` and `--subfolder` args | Evaluate organic backends by swapping/parameterizing the runner, not by entangling model code in `app.py`. |

Retirement is a first-class outcome. Write the reason where future agents will look:

- README `Later` if it is an official deferred or intentionally skipped feature.
- **printforge-failure-archaeology** if it is an incident, dead end, or "do not re-propose" item.
- Commit or PR notes if code was changed.

Known retired/deferred examples:

| Idea | Status |
|---|---|
| Thin-wall analysis | Verified in README as intentionally skipped: "the slicer does it better" (`README.md:126`). |
| AMF export | Maintainer testimony: retired as a dead format. Confirm in failure archaeology before relying on it. |
| Cancel buttons for generation | Maintainer testimony: retired because `codex exec` is not cancellable in this app. Confirm in failure archaeology before relying on it. |
| Fake STEP export | README verifies STEP export is refused because this is a mesh-only pipeline (`README.md:67-70`). |

## Choose work from real signal

Prefer ideas with evidence from these sources, in this order:

| Source | How to use it |
|---|---|
| Real print failures of real models | Start from observed slicer/print failures, not rendered vibes. The boat history produced detector calibration, intent lineage, and edit-in-place refines. Treat exact historical counts as maintainer testimony unless re-run. |
| Collaborator feature requests | First check current state. Several requests historically were already built. Verify README, UI, and routes before coding. |
| Owner's print archive | Maintainer testimony says the archive had about 48 real prints as of 2026-07-06 and skewed toward household functional parts, enclosures, fitted holders, and Gridfinity. Re-verify through Bambuddy or exported records before using the count. Build for that distribution, not imagined users. |
| Upstream model releases | Evaluate Sparc3D/TRELLIS-like releases only through **printforge-organic-quality-campaign** scorecards. Do not swap organic backends because demos look good. |

Fast state check commands:

```sh
cd /home/cody/projects/printforge
grep -n '## Later' -A20 README.md
grep -n 'QA_CHECK\|QA_ROUNDS\|call_codex_edit\|def organic' app.py
grep -n 'Sparc3D\|TRELLIS\|Thin-wall' README.md
curl -fsS --max-time 3 http://localhost:8093/config
```

Only the `curl` command touches the live service, and it is a GET.

## AI-session investigation hygiene

Use these stop conditions:

| Situation | Stop condition | Next move |
|---|---|---|
| LLM fix loop | More than 2 fix rounds | Stop prompting. Make a deterministic hand fix in `.scad` or narrow the mechanism. |
| Investigation going in circles | Same hypothesis restated twice without a new discriminating observation | Write the observation table and re-derive from first principles. |
| Long generation/QA experiment | A command or HTTP client may time out before the job finishes | Fire once, then poll read-only state. Do not restart the service mid-job. |
| Live-user data temptation | The easiest artifact is in `library/` or `uploads/` | Do not mutate it. Copy facts out read-only, or create a `test: ` generation only through change-control-approved validation. |
| Costly codex generation | Each generation/refine can consume roughly 5-10 minutes of metered quota (maintainer testimony) | Batch hypotheses per run; do not spend a run on a question grep can answer. |

Read-only polling commands:

```sh
cd /home/cody/projects/printforge
pgrep -x codex                      # non-empty means a codex job is running
find library -mindepth 1 -maxdepth 1 -type d | wc -l
ls -td library/* 2>/dev/null | head
curl -fsS --max-time 3 http://localhost:8093/config
```

Never validate on the collaborator's live data by editing `library/`, `uploads/`,
`presets.txt`, `profiles.json`, or `.env`. Never run organic generation casually; it uses
the shared GPU and is serialized by `_organic_lock` (`app.py:1253-1299`).

## When NOT to use this skill

- Use **printforge-change-control** before editing behavior, changing prompt recipes,
  adding dependencies, deploying, or touching live-service blast radius.
- Use **printforge-debugging-playbook** when there is already a concrete symptom and you
  need fast triage.
- Use **printforge-failure-archaeology** when checking whether an idea was already tried,
  settled, or retired.
- Use **printforge-validation-and-qa** to run evidence collection and acceptance tests.
- Use **printforge-organic-quality-campaign** for Sparc3D/TRELLIS/organic backend
  evaluation.
- Use **printforge-proof-and-analysis-toolkit** for first-principles geometry proofs.
- Use **printforge-config-and-flags** before adding or changing env flags.

## Provenance and maintenance

Re-verify drift-prone claims with one-line commands:

```sh
cd /home/cody/projects/printforge && grep -n '## Later' -A20 README.md
cd /home/cody/projects/printforge && grep -n 'QA_CHECK\|QA_ROUNDS' app.py
cd /home/cody/projects/printforge && grep -n 'def call_codex_edit' -A30 app.py
cd /home/cody/projects/printforge && grep -n 'for _ in range(QA_ROUNDS)' -A20 app.py
cd /home/cody/projects/printforge && grep -n 'def floating_starts' -A35 parts.py
cd /home/cody/projects/printforge && grep -n 'When a BASE MESH\|z-scaling stretches\|label_plus_y' prompts.py
cd /home/cody/projects/printforge && git show --stat --oneline 1461eb2 23b53f1 ee7391c 747a568 633a536 d7431c2 9460631
cd /home/cody/projects/printforge && grep -n 'def organic' -A25 app.py && grep -n 'add_argument' organic/generate.py
cd /home/cody/projects/printforge && curl -fsS --max-time 3 http://localhost:8093/config
cd /home/cody/projects/printforge && find library -mindepth 1 -maxdepth 1 -type d | wc -l
cd /home/cody/projects/printforge && printf '%s\n' 'foo = 1; // [0:10]' 'foo = -1; // [-5:5]' 'label = "A"; // free text' | grep -nP '^(\\w+)\\s*=\\s*([\\d.]+|"[^"]*")\\s*;\\s*//\\s*(?:\\[([\\d.:\\-]+)\\]|(free text))'
```
