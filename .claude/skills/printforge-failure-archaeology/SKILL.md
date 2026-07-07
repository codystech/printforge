---
name: printforge-failure-archaeology
description: Consult BEFORE re-investigating any PrintForge bug, proposing a fix that touches prompts.py, vision QA, /validate, refine machinery, mesh import/export, organic mode, run/deploy operations, or re-proposing a deferred/rejected idea. Use for symptoms like floating regions, Bambu 3MF rejection, collapsed slivers, missing robot/computer/sail/text, QA reverted my change, full-file rewrite, straight-on render looked fine, textmetrics, STEP units, MakerWorld import, snap-fit validator false positive, AdGuard HuggingFace hang, codex PATH/systemd outage, or README Later/retired idea questions.
---

# PrintForge Failure Archaeology

Use this chronicle before debugging or changing PrintForge. It records settled battles, open wounds, and deliberately retired ideas so you do not spend another session proving the same thing.

Definitions: a **refine** edits an existing OpenSCAD model; **vision QA** is the render-review-fix loop in `app.py`; **CSG** means constructive solid geometry, where `union()`/`difference()` combine solids; **floating region** means a slicer layer island with no supporting material below; **3MF** is the multi-part print archive format Bambu Studio reads; **STEP** is CAD boundary-representation source, not a triangle mesh; **BRep** is exact CAD topology; **organic** means image-to-mesh generation via Hunyuan3D.

Rules:

- Treat `settled` entries as constraints. Do not reopen them without new evidence and `printforge-change-control`.
- Treat `open` entries as known limitations. Label experiments as candidate until measured.
- Treat `retired` entries as rejected product scope. Do not re-propose them as discoveries.
- If changing behavior, stop and load `printforge-change-control` first.
- Date-stamp live counts and service state: as of 2026-07-06, `/config` returned `{"bambuddy":true,"organic":true}` when verified here.

## Timeline

Use this compact history to locate the era of a regression.

| Date | Commit | What changed | Why |
|---|---:|---|---|
| 2026-07-05 | `1d086af` | v2 rebuild: parametric OpenSCAD generation | New foundation |
| 2026-07-05 | `5127b98` | Base-mesh remix and vision-grounded refine | Start mesh remixing |
| 2026-07-05 | `747a568` | Model-site import and reliable text embossing | Fix imported-mesh text failures |
| 2026-07-05 | `23b53f1` | Multi-round vision loop and creative-addition enforcement | Stop silent omissions |
| 2026-07-05 | `d7431c2` | QA close-ups of changed geometry | Make tiny additions inspectable |
| 2026-07-05 | `633a536` | Buried-addition and base-damage checks | Catch geometry hidden inside solids |
| 2026-07-05 | `99266d5` | Edit-mode refines, previous-state diffing, footprint recipe removal | Stop rewrite loss and QA reverts |
| 2026-07-05 | `1461eb2` | Replaced broken z-scale footprint recipe | End sliver-collapse factory |
| 2026-07-05 | `135463a` | Floating-region detector | Catch Bambu-style rejection before slicing |
| 2026-07-05 | `ee7391c` | Design-intent lineage and detector calibration | Preserve accepted changes |
| 2026-07-05 | `35596af` | SVG, spec preview, AMS colors, organic v1 | Expand inputs and output workflow |
| 2026-07-05 | `9460631` | Organic mode working on RTX 3090 | Make image-to-mesh end-to-end |
| 2026-07-05 | `8a27a99` | Organic sculpts render/export directly | Remove unnecessary LLM round-trip |
| 2026-07-05 | `1c6854d` | Parts Panel v2: lock/suppress/rename/regenerate | Make part intent enforceable |
| 2026-07-06 | `a48c8aa` | STEP/GLB import, roles, export/library QOL | Add format and assembly workflow |
| 2026-07-06 | `e072c3f` | Service PATH includes codex | Fix systemd codex lookup |
| 2026-07-06 | `e1ed7e3` | Validate findings feed Fix button, touching excluded | Reduce bad validator repair prompts |
| 2026-07-06 | `63653af` | STEP named-body extraction and Y-up camera rework | Exact cutout detection |
| 2026-07-06 | `141095b` | Empty-geometry upload guard and robust upload errors | Harden import UX |

## Prompt-Contract Failures

| TITLE | STATUS | Symptom | Root cause | Evidence | What NOT to do now |
|---|---|---|---|---|---|
| Z-scale footprint disaster | settled | Boat refine collapsed flag/chest/cleat into flat slivers; Bambu rejected disconnected floating fragments. | Prompt contract told the model to clip to footprint with `scale([1,1,big]) import(...)`; z-scaling stretches the bottom slice, not the outline. | `git show --stat 99266d5 1461eb2`; current fix is rule 14b in `prompts.py:53`-`58`; library `3e7accab949c` confirms the deterministic fixed boat. | Do not revive z-scale footprint clipping. Use cross-sections, or `linear_extrude(h) projection() import(path)` only when you accept slowness. |
| Emboss orientation algebra | settled | Raised hull-side text looked present in renders but failed or landed backward/buried. | LLM-derived rotations were unreliable on vertical imported-mesh sides; text was placed at global bbox top instead of local hull surface. | `git show --stat 747a568`; verified recipe is rule 14f in `prompts.py:69`-`79`; five horizontal sections are computed in `app.py:648`-`661`. | Do not ask the LLM to derive orientation algebra. Use the verified recipe or re-test physically before changing it. |
| Union buries interior geometry | settled | Requested sail extruded into cabin and vanished. | In CSG, `union()` cannot reveal a feature fully inside an existing solid. | `git show --stat 633a536`; QA changed-region notes require every requested element to match a visible region in `app.py:389`-`395`; rule 14e in `prompts.py:65`-`68`. | Do not add thin/planar features through occupied mesh volume. Put them into open air and fuse them into the base. |
| Base-mesh creative damage is not always damage | settled | QA could treat user-requested cuts/notches/reshapes as base damage. | Damage detection without intent cannot distinguish accidental deletion from creative edit. | Current QA note allows cuts/engraving/creative reshaping when requested in `app.py:396`-`404`; rule 14d allows requested subtractive edits in `prompts.py:61`-`64`. | Do not hard-block all base-mesh subtraction. Verify against the user request and design history. |
| Prose-as-parameters and negative numeric defaults | settled/open parser caveat | Bad sliders or missing sliders can appear when generated variables are not geometric tunables; negative defaults silently fail to parse as sliders. | Prompt needed to ban status/prose string parameters; current `PARAM_RE` value alternation is `[\d.]+|"[^"]*"`, so leading `-` is not accepted for defaults. | `git show --stat 782090e`; prompt ban is `prompts.py:16`-`18`; regex is `app.py:46`-`48`. Negative-default behavior is source-confirmed but not fixed. | Do not encode reports as string parameters. Do not assume negative defaults become sliders; route parser changes through `printforge-change-control`. |

## QA-Loop Evolution

| TITLE | STATUS | Symptom | Root cause | Evidence | What NOT to do now |
|---|---|---|---|---|---|
| Full-file rewrites drop features | settled | Refines of long models wiped unrelated modules like robot, wheel, or chest. | Asking an LLM to reprint the full file loses content; QA reprints were also risky. | `git show --stat 99266d5`; `call_codex_edit()` edits scratch `model.scad` in place in `app.py:119`-`145`; refine path uses it in `app.py:1015`-`1042`. | Do not reintroduce full-file rewrite refines for long models. Keep edit-in-place semantics. |
| QA reverting intentional changes | settled | Cabin removed by user was restored later; QA invented support junk in a hollow hull. | QA diffed against the pristine upload instead of the previous accepted state; objectives lacked lineage. | `git show --stat 99266d5 ee7391c`; previous-state base render is chosen in `app.py:1081`-`1091`; design-history block is `app.py:543`-`551`; library `27348cced127` meta contains accepted intent. | Do not diff refines against the original upload when current SCAD exists. Do not let QA satisfy one goal by violating design history. |
| Straight-on renders hide low relief | settled | Embossed text looked fine in verification but was absent or garbage on the print. | Straight-on orthographic views hide shallow relief. | `git show --stat 747a568`; `render_png()` supports perspective in `app.py:303`-`315`; QA adds oblique perspective views in `app.py:361` and `app.py:419`-`425`. | Do not verify relief from a straight-on render only. Use oblique perspective for humans and QA. |
| Whole-model renders hide small additions | settled | A 10mm robot was a smudge and QA could not judge placement. | Full views made small changed regions visually insignificant. | `git show --stat d7431c2`; mesh-diff clustering is `app.py:318`-`353`; close-up cameras are created in `app.py:369`-`387`. | Do not rely only on full-model screenshots for small requested additions. |
| Creative additions silently ignored | settled | “Add a robot behind the wheel + a computer” produced no robot and no new sliders. | Single-pass generation had no per-element present/placed/proportioned enforcement. | `git show --stat 23b53f1`; `QA_ROUNDS` default is 2 in `app.py:29`; QA loop is `app.py:1092`-`1109`; README describes up to 2 auto-fix rounds at `README.md:41`-`43`. | Do not remove the multi-round look-fix-rerender loop to save time without measuring quality loss. |
| Floating-region detector calibration | settled | Bambu-style floating regions appeared at mast bases and other features. | LLMs perched parts at guessed z heights; detector needed tolerance for section noise. | `git show --stat 135463a ee7391c`; `floating_starts()` is `parts.py:67`-`97`; QA injects floating-region repair notes in `app.py:406`-`418`; response warnings are `app.py:1133`-`1145`. | Do not treat disconnected/floating starts as cosmetic. After about 2 LLM fix rounds, make a deterministic SCAD repair. |

## Refine-Architecture Overhaul

| TITLE | STATUS | Symptom | Root cause | Evidence | What NOT to do now |
|---|---|---|---|---|---|
| qwen inadequate for multi-part | settled design decision | Local qwen works for simple brackets but fails more often on multi-part designs. | Multi-part OpenSCAD/refine tasks exceeded the fallback model’s practical reliability. | README states codex primary and qwen fallback at `README.md:10`-`12` and `README.md:82`-`85`; backend selection is `app.py:23`-`34`; image input requires codex in `app.py:151`-`164`; `LAST_BACKEND` is `app.py:148`-`162`. | Do not promote qwen to primary for multi-part/vision/image workflows without a measured campaign. |
| Deterministic lock verification | settled | Locked parts could still be modified if the LLM promised preservation but edited them. | Prompt-only locking is not enforcement. | `git show --stat 1c6854d`; locked module diff is `app.py:499`-`511`; forced correction round is `app.py:1043`-`1054`; README lock claim is `README.md:57`-`61`. | Do not trust an LLM statement that locked code is unchanged. Trust the diff. |
| Design intent as law | settled | Accepted user decisions got lost in later refines. | History was not injected into each edit/review. | `git show --stat ee7391c`; saved library meta stores `intent` in `app.py:524`-`538`; refine injects intent in `app.py:1033`-`1038`; QA also receives it in `app.py:1096`-`1099`. | Do not bypass intent loading when adding new refine paths. |

## Import, Format, and Validation Battles

| TITLE | STATUS | Symptom | Root cause | Evidence | What NOT to do now |
|---|---|---|---|---|---|
| textmetrics silently breaks on old OpenSCAD | settled | Text sizing produced garbage geometry without a hard OpenSCAD error. | Debian/OpenSCAD 2021.01 lacks required feature flags while prompts use `textmetrics()`. | `OPENSCAD_ARGS` default is `--enable=textmetrics --enable=manifold` in `app.py:33`-`34`; host run uses `nixpkgs#openscad-unstable` in `run.sh:11`-`15`; docker blanks args with warning in `compose.yaml:7`-`12`; README explains old docker behavior at `README.md:87`-`97`. | Do not debug text sizing on old host OpenSCAD. Use the nix shell path or accept docker’s cruder text sizing. |
| STEP units and fake STEP export | settled | Imported STEP appeared 1000x off or wrong-up; users might expect STEP export. | cascadio emits GLB-like mesh in meters and Y-up; pipeline output is mesh-only, not BRep. | `git show --stat a48c8aa 63653af`; STEP import scales and rotates in `app.py:613`-`616`; named bodies are extracted in `app.py:617`-`635`; STEP export refusal is `app.py:1185`-`1188`; README says mesh-only at `README.md:67`-`70`. | Do not fake STEP export. Do not remove x1000 scale or Y-up rotation without a STEP regression case. |
| Printables/Thingiverse/MakerWorld import quirks | settled | Marketplace import fails differently per site. | APIs differ: Printables GraphQL is keyless; Thingiverse requires a token; MakerWorld has no public API path here. | `git show --stat 747a568`; Printables endpoint and mutation are `app.py:703`-`721`; Thingiverse token requirement is `app.py:724`-`736`; MakerWorld/Cults/MMF hard messages are `app.py:754`-`761`. | Do not promise MakerWorld direct import. Tell users to download and attach the file. |
| Assembly validator vs snap-fits | open | `/validate` flags intentional snap-fit interference as collision. | Pairwise boolean collision cannot infer intentional compliant joints. | Validator is `app.py:1438`-`1481`; collisions above 0.5mm3 are reported in `app.py:1467`-`1474`; UI excludes `TOUCHING` from Fix feed in `static/index.html:322`-`334`; `git show --stat e1ed7e3`. The specific ~12mm3 snap-fit case is operator-reported, unverified here. | Do not teach the Fix button to blindly repair all collisions. Add explicit intentional-joint semantics first. |
| 3MF object counting trap | settled gotcha | `grep -c` appears to undercount objects in 3MF model XML. | `parts.py` writes object XML mostly as one line, so line-counting is wrong. | 3MF XML construction is `parts.py:33`-`64`, especially joined objects in `parts.py:52`-`57`; self-check validates two objects in `parts.py:100`-`120`. | Do not use `grep -c '<object'`. Use `grep -o '<object' file | wc -l` or parse XML. |

## Organic and NixOS Gauntlet

| TITLE | STATUS | Symptom | Root cause | Evidence | What NOT to do now |
|---|---|---|---|---|---|
| NixOS organic setup gauntlet | settled | Hunyuan3D install failed on CUDA/libGL/cv2/pymeshlab or produced wrong-up meshes. | Organic mode needs cu121 wheels, headless OpenCV, pymeshlab import patch, NixOS GL libraries, correct subfolder, Y-up to Z-up rotation, GPU serialization. | `git show --stat 35596af 9460631 8a27a99`; setup commands are `organic/setup.sh:6`-`17`; actual model subfolder is `organic/generate.py:41`-`57`; Y-up rotation/postprocess is `organic/generate.py:9`-`33`; app lock/GPU unload/env are `app.py:1253`-`1309`; `/config` returned organic true as of 2026-07-06. | Do not run organic jobs casually on the shared GPU. Do not remove `_organic_lock` or `_free_gpu()` without a measured replacement. |
| Organic weights cache path correction | settled documentation caveat | Setup comment says HuggingFace cache, but real first-run weights land elsewhere. | hy3dgen uses its own cache path. | `organic/setup.sh:3` still says `~/.cache/huggingface`; verified correction from prior investigation says actual cache is `~/.cache/hy3dgen`. Not executed here because running organic would use the GPU/network. | Do not delete `~/.cache/hy3dgen` assuming weights live only in the HF hub cache. Re-verify before cleanup. |
| AdGuard blocks HuggingFace CDN | open | New model weight downloads hang silently in the lab. | Lab AdGuard resolves `us.aws.cdn.hf.co` to `::`. | Operational report; no repo code contains this domain. The workaround is network configuration, not app code. | Do not debug organic model code first when new downloads hang. Check DNS/AdGuard and whitelist `us.aws.cdn.hf.co`. |

## Operations and User-Data Discipline

| TITLE | STATUS | Symptom | Root cause | Evidence | What NOT to do now |
|---|---|---|---|---|---|
| Port-collision outage and systemd PATH | settled/open remainder | Collaborator saw connection timeout; later systemd could not find `codex`. | Background `run.sh` tasks collided on port; systemd did not inherit login shell PATH. Linger state is volatile (checked `yes` on 2026-07-07; printforge-run-and-operate owns it). | `git show --stat e072c3f`; `run.sh` exports `~/.local/npm/bin` in `run.sh:5`-`6`; primary run command is `run.sh:11`-`15`; `/config` GET succeeded here. The exact outage and lingering state were not executed here. | Do not start `run.sh` in the background beside the live service. Do not run `systemctl` from this skill; use `printforge-run-and-operate`. |
| Benchmark pollution of user library | settled discipline | Test/benchmark generations mixed into a real user library. | No database boundary; `library/` is the product store. | As of 2026-07-06, `find library -mindepth 1 -maxdepth 1 -type d | wc -l` returned 53; `rg '\"name\": \"test:' library` finds multiple test-prefixed entries; library save path is `app.py:524`-`540`. | Do not leave benchmark models unmarked. Prefix test library entries with `test: ` or delete them through the app. |
| Secrets and dependency traps | settled gotchas | Inline secret handling was blocked; imports failed without runtime deps; watcher commands overmatched. | Secrets belong in `.env`; 3MF parsing needs `networkx`/`lxml`; uploads need `python-multipart`; process matching needs exact executable names. | `.env` sourcing is `run.sh:7`; deps are in `run.sh:12`-`14` and `Dockerfile:4`; multipart import is `app.py:15`; 3MF stack imports are `parts.py:1`-`6`. Classifier-block and `pgrep` incidents are operator-reported, unverified here. | Do not paste secrets into code/chat. Do not run broad `pgrep -f 'codex exec'`; use exact process matching when operating. |
| First physical print closed the loop | settled evidence, partly unverified | The pipeline produced at least one successful physical print and a fast junction-box benchmark. | End-to-end loop exists: generate, validate, export/send, archive. Physical archive details are outside repo. | Bambuddy upload path is `app.py:1236`-`1250`; README describes Bambuddy at `README.md:3`-`6` and `README.md:109`-`110`; library contains junction-box anchors including `5a18a208335a` and `bae70f4da950`. Bambuddy archive #51, 8.58g, and 99.9% print-time accuracy are operator-reported, unverified here. | Do not use this as proof that every output is printable. Keep using deterministic geometry checks and real slicer/print feedback. |

## Retired Ideas

Do not re-propose these as new product ideas. If a user explicitly asks for one, route through `printforge-change-control` and state the prior reason.

| Idea | Status | Reason rejected | Evidence |
|---|---|---|---|
| Thin-wall analysis | retired | Slicers do it better; app should not duplicate weak slicer heuristics. | README still lists it as intentionally skipped at `README.md:126`; retire reason is operator-reported. |
| AMF export | retired | Dead format; no current user value compared with STL/3MF/OBJ/GLB. | No AMF code found by `rg -n 'AMF|amf'`; retire reason is operator-reported. |
| Fake STEP export | retired | Dishonest for mesh-only pipeline; STEP export requires BRep. | Refusal is `app.py:1185`-`1188`; README is `README.md:67`-`70`. |
| Scale/rotate/center helpers | retired | Slicer job; app should generate correct geometry and let slicer handle transforms. | Retire reason is operator-reported; unit warnings only suggest refine scaling in `app.py:666`-`672`. |
| Cancel buttons for generation | retired | `codex exec` calls are not cancellable by the current app contract. | `subprocess.run(... timeout=420/900)` is synchronous in `app.py:102`-`145`; retire reason is operator-reported. |
| Format/QA library filters | retired | Search and metadata are enough for now. | README library search is `README.md:71`-`74`; retire reason is operator-reported. |

## Open Items

Track these explicitly. Do not mark them solved without fresh evidence.

| Item | Status | Current evidence | Next responsible skill |
|---|---|---|---|
| Snap-fit validator false positive | open | `/validate` boolean collision cannot understand intentional joints; see `app.py:1438`-`1481`. | `printforge-validation-and-qa` and `printforge-change-control` |
| AdGuard HuggingFace whitelist | open | Operational report; not represented in repo. | `printforge-build-and-env` |
| Lingering state (volatile) | open | Flips over time; `loginctl show-user cody -p Linger --value` returned `yes` on 2026-07-07. | `printforge-run-and-operate` |
| README “Later” list | open/deferred | Current list is `README.md:118`-`126`: CT deploy, Sparc3D/TRELLIS.2, thumbs-down negatives, Bambu paint metadata, text-to-image-to-3D, in-UI custom profile editor, thin-wall analysis note. | `printforge-research-methodology` or relevant implementation skill |

## When NOT to Use This Skill

- Use `printforge-change-control` before making behavior changes, deploying, touching live service operations, or modifying docs of record.
- Use `printforge-debugging-playbook` for active symptom triage and discriminating experiments.
- Use `printforge-architecture-contract` for current invariants and load-bearing module boundaries.
- Use `printforge-openscad-reference` for OpenSCAD syntax, CSG, customizer variables, and verified modeling recipes.
- Use `printforge-mesh-geometry-reference` for trimesh, 3MF, STEP, slicing, collision, and mesh math details.
- Use `printforge-build-and-env` for host/docker/organic setup.
- Use `printforge-run-and-operate` for systemd, deployment, logs, and live-service procedures.
- Use `printforge-validation-and-qa` for evidence standards and adding checks.
- Use `printforge-organic-quality-campaign`, `printforge-research-frontier`, or `printforge-research-methodology` for planned research and candidate evaluation.

## Provenance and Maintenance

Run these read-only checks when refreshing the chronicle:

- Commit timeline: `cd /home/cody/projects/printforge && git log --format='%h %ad %s' --date=short`
- Key commit stats: `cd /home/cody/projects/printforge && git show --stat --oneline 99266d5 1461eb2 633a536 d7431c2 23b53f1 747a568 135463a ee7391c 35596af 9460631 a48c8aa 1c6854d e1ed7e3 e072c3f 63653af 141095b`
- Prompt contract lines: `cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '1,90p'`
- Refine/QA anchors: `cd /home/cody/projects/printforge && rg -n 'PARAM_RE|call_codex_edit|vision_qa|mesh_changes|intent_block|lock_violations|QA_ROUNDS|current_scad' app.py`
- Import/export anchors: `cd /home/cody/projects/printforge && rg -n 'PRINTABLES_GQL|THINGIVERSE_TOKEN|MakerWorld|STEP export|bodies_detail|CUTOUT DETECTION REPORT|apply_scale\\(1000\\)' app.py`
- Validation anchors: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '1438,1481p' && nl -ba static/index.html | sed -n '322,334p'`
- Floating detector and 3MF writer: `cd /home/cody/projects/printforge && nl -ba parts.py | sed -n '33,120p'`
- Organic setup/code anchors: `cd /home/cody/projects/printforge && nl -ba organic/setup.sh | sed -n '1,40p' && nl -ba organic/generate.py | sed -n '1,70p' && rg -n '_organic_lock|_free_gpu|ORGANIC_LIBS|organic mode' app.py run.sh`
- Runtime/dependency anchors: `cd /home/cody/projects/printforge && nl -ba run.sh && nl -ba compose.yaml && nl -ba Dockerfile`
- README deferred list: `cd /home/cody/projects/printforge && nl -ba README.md | sed -n '76,126p'`
- Library counts and golden entries: `cd /home/cody/projects/printforge && find library -mindepth 1 -maxdepth 1 -type d | wc -l && rg -n 'boat FIXED|SHIP-IT|\"name\": \"test:' library`
- Live config if safe: `curl -fsS --max-time 3 http://localhost:8093/config`
- Reported/unverified operational claims to re-check with the owner or ops tools: systemd lingering state, historical port-collision outage, AdGuard `us.aws.cdn.hf.co` behavior, actual organic weight cache after first generate, Bambuddy archive #51 print metrics, classifier-blocked secret incident, exact `pgrep` watcher incident.
