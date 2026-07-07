# PrintForge — agent entry point

This repo ships a skill library at `.claude/skills/`. **Read the relevant `SKILL.md` before acting** — Codex does not auto-load these; you must open them.

SAFETY (read first):
1. PrintForge is a **LIVE service with a second real user** — default to read-only, never break the running app.
2. Deploy = `systemctl --user restart printforge`, and it is **gated by `printforge-change-control`** — load that skill before editing ANY file.

Skill index (`.claude/skills/<name>/SKILL.md`):
- printforge-change-control — how changes are classified/gated/shipped; load BEFORE editing anything.
- printforge-architecture-contract — why it's built this way; the invariants that must hold.
- printforge-run-and-operate — start/stop/deploy/restart the live systemd user service.
- printforge-build-and-env — recreate every env from scratch (run.sh, Docker, organic/CUDA).
- printforge-config-and-flags — every env var, config file, and hardcoded knob.
- printforge-debugging-playbook — symptom-to-triage when something is BROKEN.
- printforge-failure-archaeology — past incidents; consult BEFORE re-investigating a bug.
- printforge-diagnostics-and-tooling — measurement scripts; get the NUMBERS before visual judgment.
- printforge-validation-and-qa — decide what evidence is ENOUGH; acceptance thresholds.
- printforge-proof-and-analysis-toolkit — first-principles proof recipes for geometry/behavior.
- printforge-openscad-reference — OpenSCAD/CSG runbook as implemented here.
- printforge-mesh-geometry-reference — STL/3MF/STEP/OBJ mesh and geometry processing.
- printforge-organic-quality-campaign — improving organic/image-to-mesh quality.
- printforge-research-methodology — turn a hunch/experiment into an accepted result.
- printforge-research-frontier — pick the next big thing to build; novelty positioning.
