import { CandidateViewer } from '/training-lab/viewer.js';
import { renderLineage, renderScoreChart } from '/training-lab/charts.js';

const $ = id => document.getElementById(id);
const PIPELINE_STAGES = [
  ['prompt', 'Prompt'], ['spec', 'Design specification'], ['locks', 'Locked requirements'],
  ['generation_zero', 'Generation zero'],
  ['variants', 'Variant A + Variant B'], ['generation', 'Generation'],
  ['geometry_qa', 'Geometry QA'], ['slicer_qa', 'Slicer QA'],
  ['function_review', 'Function review'], ['reward', 'Reward score'],
  ['winner', 'Winner selection'], ['memory', 'Memory update'],
  ['next_generation', 'Next generation'],
];
const SCORE_CATEGORIES = [
  ['printability', 'Printability', 25], ['function', 'Function', 25],
  ['prompt_spec_adherence', 'Prompt & spec', 20], ['structural_quality', 'Structural', 10],
  ['user_experience', 'User experience', 10], ['simplicity_efficiency', 'Simplicity', 10],
];

const state = {
  bootstrap: null, run: null, candidates: [], events: [], eventKeys: new Set(),
  pair: [null, null], lastSequence: 0, pollTimer: null, lineage: null,
  viewers: [null, null], disposed: false, loadingRun: false,
  libraryModels: [], selectedModel: null, profiles: [], manualPair: null,
};
const isMobile = matchMedia('(max-width: 640px)').matches;

const finite = value => Number.isFinite(Number(value)) ? Number(value) : null;
const present = value => value !== undefined && value !== null && value !== '';
const text = (value, fallback = 'Unavailable') => present(value) ? String(value) : fallback;
const collection = value => Array.isArray(value) ? value : value && typeof value === 'object' ? Object.values(value) : [];
const candidateId = candidate => candidate?.candidate_id ?? candidate?.id ?? null;
const generation = candidate => finite(candidate?.generation_number ?? candidate?.generation) ?? 0;
const totalScore = candidate => finite(candidate?.reward_score ?? candidate?.total_score ?? candidate?.score?.total ?? (typeof candidate?.score === 'number' ? candidate.score : null));
const selection = candidate => String(candidate?.selection_status ?? candidate?.winner_status ?? candidate?.status ?? '').toLowerCase();
const isWinner = candidate => candidate?.winner === true || /winner|current.best|selected/.test(selection(candidate));
const isRejected = candidate => candidate?.rejected === true || /reject|failed|invalid|loser/.test(selection(candidate));
const isDemoRun = run => run?.mode === 'demo' || run?.demo === true || run?.is_demo === true;
const formatScore = value => finite(value) === null ? 'Not evaluated' : `${finite(value).toFixed(finite(value) % 1 ? 1 : 0)}/100`;
const formatDuration = seconds => {
  const value = finite(seconds);
  if (value === null) return 'Unavailable';
  if (value < 60) return `${Math.round(value)}s`;
  const mins = Math.floor(value / 60); return `${mins}m ${Math.round(value % 60)}s`;
};
const isoTime = value => {
  if (!present(value)) return '—';
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};
const statusClass = value => {
  const status = String(value ?? '').toLowerCase().replaceAll('_', ' ');
  if (/current best|best/.test(status)) return 'best';
  if (/winner|pass|complete|success|validated/.test(status)) return /winner/.test(status) ? 'winner' : 'complete';
  if (/active|running|generating|evaluating/.test(status)) return 'active';
  if (/warn|regress|inconclusive/.test(status)) return 'warning';
  if (/fail|reject|error|violat|abort/.test(status)) return 'failed';
  if (/skip/.test(status)) return 'skipped';
  return 'neutral';
};

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { Accept: 'application/json', ...(options.headers ?? {}) }, cache: 'no-store', ...options });
  const body = await response.text();
  if (!response.ok) {
    let detail = body;
    try { detail = JSON.parse(body).detail ?? body; } catch {}
    throw new Error(String(detail || response.statusText).slice(0, 400));
  }
  if (!body) return {};
  try { return JSON.parse(body); } catch { throw new Error('Backend returned non-JSON state'); }
}

const postJSON = (path, value = null) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: value === null ? null : JSON.stringify(value),
});

function setConnection(message, kind = '') {
  const element = $('connection-status');
  element.textContent = message;
  element.className = `connection-status ${kind}`.trim();
}

function addField(parent, label, value) {
  const wrapper = document.createElement('div');
  const dt = document.createElement('dt'); dt.textContent = label;
  const dd = document.createElement('dd'); dd.textContent = text(value);
  wrapper.append(dt, dd); parent.appendChild(wrapper);
}

function showEvidence(title, evidence, kicker = 'PERSISTED EVIDENCE') {
  $('evidence-kicker').textContent = kicker;
  $('evidence-title').textContent = title;
  const body = $('evidence-body'); body.replaceChildren();
  if (!present(evidence) || typeof evidence !== 'object') {
    const note = document.createElement('p'); note.className = 'empty-note'; note.textContent = present(evidence) ? String(evidence) : 'No evidence was persisted for this item.'; body.appendChild(note);
  } else {
    const pre = document.createElement('pre'); pre.textContent = JSON.stringify(evidence, null, 2); body.appendChild(pre);
  }
  $('evidence-dialog').showModal();
}

function featureFlags(bootstrap) {
  return bootstrap?.feature_flags ?? bootstrap?.flags ?? {};
}

function labEnabled(bootstrap) {
  const flags = featureFlags(bootstrap);
  return Boolean(bootstrap?.enabled ?? bootstrap?.training_lab_enabled
    ?? flags.PRINT_FORGE_TRAINING_LAB_ENABLED ?? flags.training_lab_enabled ?? false);
}

function renderDisabled(bootstrap, error = null) {
  $('workspace').hidden = true; $('empty-state').hidden = true; $('disabled-state').hidden = false;
  const title = $('disabled-state').querySelector('h2');
  const description = $('disabled-state').querySelector('p:not(.eyebrow)');
  if (error) {
    title.textContent = 'Training Lab state is unavailable';
    description.textContent = `The production Generate workflow is unaffected. ${error.message}`;
  }
  const list = $('flag-list'); list.replaceChildren();
  const flags = featureFlags(bootstrap);
  const entries = Object.entries(flags);
  for (const [key, value] of entries.length ? entries : [['PRINT_FORGE_TRAINING_LAB_ENABLED', false]]) {
    const badge = document.createElement('span'); badge.className = 'flag'; badge.textContent = `${key}=${String(value)}`; list.appendChild(badge);
  }
  $('lab-app').setAttribute('aria-busy', 'false');
}

function flattenCandidates(run) {
  const direct = collection(run?.candidates ?? run?.training_candidates);
  if (direct.length) return direct;
  return collection(run?.generations ?? run?.training_generations).flatMap(item => collection(item.candidates ?? item.variants));
}

function choosePair(candidates, run) {
  if (state.manualPair) {
    const pair = state.manualPair.map(id => candidates.find(candidate => candidateId(candidate) === id) ?? null);
    if (pair.some(Boolean)) return pair;
    state.manualPair = null;
  }
  const nonBaseline = candidates.filter(candidate => generation(candidate) > 0 || candidate.variant_label || candidate.variant);
  if (!nonBaseline.length) return [null, null];
  const requested = finite(run?.current_generation ?? run?.generation_number);
  const gen = requested ?? Math.max(...nonBaseline.map(generation));
  const current = nonBaseline.filter(candidate => generation(candidate) === gen);
  const byVariant = label => current.find(candidate => String(candidate.variant_label ?? candidate.variant ?? '').toUpperCase() === label);
  return [byVariant('A') ?? current[0] ?? null, byVariant('B') ?? current.find(candidate => candidate !== current[0]) ?? null];
}

function getRuns(bootstrap) {
  const runs = collection(bootstrap?.runs ?? bootstrap?.recent_runs);
  const active = bootstrap?.active_run;
  if (active && typeof active === 'object' && !runs.some(run => runId(run) === runId(active))) runs.unshift(active);
  return runs;
}
const runId = run => typeof run === 'string' ? run : run?.run_id ?? run?.id ?? null;

function populateHeader() {
  const bootstrap = state.bootstrap ?? {};
  const run = state.run ?? {};
  $('current-branch').textContent = text(bootstrap.current_branch ?? bootstrap.git?.current_branch);
  $('production-branch').textContent = text(bootstrap.production_branch ?? bootstrap.git?.production_branch);
  const status = run.status ?? 'No run';
  $('run-status').textContent = text(status);
  $('run-status').className = `status-label ${statusClass(status)}`;
  const currentGen = finite(run.current_generation ?? run.generation_number);
  const maxGen = finite(run.limits?.maximum_iterations ?? run.limits?.maximum_generations ?? run.config?.maximum_iterations ?? run.config?.maximum_generations ?? run.maximum_generations);
  $('generation-label').textContent = currentGen === null ? '—' : `${currentGen}${maxGen === null ? '' : ` / ${maxGen}`}`;
  $('stage-label').textContent = text(run.active_stage, 'Idle').replaceAll('_', ' ');
  $('best-score').textContent = formatScore(run.current_best_score ?? run.best_score ?? totalScore(state.candidates.find(isWinner)));
  const started = finite(run.started_at);
  const ended = finite(run.completed_at);
  $('elapsed-label').textContent = started === null ? '—' : formatDuration(Math.max(0, (ended ?? Date.now() / 1000) - started));
  $('failure-label').textContent = text(run.latest_failure ?? run.failure_reason ?? run.failure, 'None');
  $('failure-label').title = $('failure-label').textContent;
  $('demo-banner').hidden = !isDemoRun(run);
  document.body.classList.toggle('demo-mode', isDemoRun(run));
  const real = Boolean(runId(run) && !isDemoRun(run));
  $('resume-run').textContent = String(run.status ?? '').toLowerCase() === 'created' ? 'Start run' : 'Resume';
  $('resume-run').hidden = !real || /running|complete|cancelled/.test(String(run.status ?? '').toLowerCase());
  $('stop-run').hidden = !real || !/running|stopping/.test(String(run.status ?? '').toLowerCase());
  $('cancel-run').hidden = !real || !/running|stopping|cancelling/.test(String(run.status ?? '').toLowerCase());
  $('export-run').hidden = !real;
}

function stageData(run, key) {
  const source = run.pipeline_stages ?? run.pipeline ?? run.stages ?? {};
  if (Array.isArray(source)) return source.find(stage => (stage.key ?? stage.stage ?? stage.name) === key) ?? null;
  const persisted = source[key] ?? null;
  if (persisted) return persisted;
  const active = String(run.active_stage ?? '');
  if (active === key || (key === 'generation_zero' && active.startsWith('generation_zero')) || (key === 'geometry_qa' && active === 'evaluation')) return { status: 'active', candidate_id: run.active_candidate_id };
  return null;
}

function renderPipeline() {
  const container = $('pipeline'); container.replaceChildren();
  for (const [key, label] of PIPELINE_STAGES) {
    const data = stageData(state.run, key);
    const reported = typeof data === 'string' ? data : data?.status;
    const status = reported ?? 'not reported';
    const item = document.createElement('li'); item.className = `pipeline-step ${statusClass(status)}`; item.tabIndex = 0;
    const dot = document.createElement('span'); dot.className = 'pipeline-dot'; dot.textContent = /complete|pass|success/.test(String(status).toLowerCase()) ? '✓' : /fail|error/.test(String(status).toLowerCase()) ? '×' : /active|running/.test(String(status).toLowerCase()) ? '•' : '—';
    const name = document.createElement('span'); name.className = 'pipeline-label'; name.textContent = label;
    const stateLabel = document.createElement('span'); stateLabel.className = 'muted'; stateLabel.textContent = text(status, 'Not reported');
    item.append(dot, name, stateLabel);
    const open = () => showEvidence(label, data, 'PIPELINE EVIDENCE');
    item.addEventListener('click', open); item.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') open(); });
    container.appendChild(item);
  }
  $('pipeline-updated').textContent = state.run.updated_at ? `Updated ${isoTime(state.run.updated_at)}` : 'Update time unavailable';
}

function constraintEntries(run) {
  const source = run.constraints ?? run.locked_constraints ?? run.config?.locked_constraints ?? [];
  if (Array.isArray(source)) return source.map(item => typeof item === 'string' ? { name: item } : item);
  return Object.entries(source).map(([name, value]) => typeof value === 'object' ? { name, ...value } : { name, value });
}

function renderConstraints() {
  const container = $('constraints'); container.replaceChildren();
  const entries = constraintEntries(state.run);
  if (!entries.length) return appendEmpty(container, 'No constraints were persisted.');
  for (const constraint of entries) {
    const status = constraint.status ?? (constraint.violated ? 'violated' : constraint.preserved === true ? 'preserved' : 'unverifiable');
    const item = document.createElement('div'); item.className = `constraint ${statusClass(status) === 'failed' ? 'violated' : statusClass(status) === 'complete' ? 'preserved' : ''}`;
    const body = document.createElement('div');
    const strong = document.createElement('strong'); strong.textContent = text(constraint.name ?? constraint.title ?? constraint.constraint);
    const small = document.createElement('small'); small.textContent = text(constraint.value ?? constraint.expected ?? constraint.description, 'Value unavailable');
    body.append(strong, small);
    const badge = document.createElement('span'); badge.className = `status-label ${statusClass(status)}`; badge.textContent = text(status);
    item.append(body, badge); container.appendChild(item);
  }
}

function appendEmpty(container, message) {
  const empty = document.createElement('div'); empty.className = 'empty-note'; empty.textContent = message; container.appendChild(empty);
}

function candidateIssues(candidate) {
  return collection(candidate?.issues ?? candidate?.qa_findings ?? candidate?.findings);
}

function candidateArtifacts(candidate) {
  const artifacts = collection(candidate?.artifacts ?? candidate?.preview_assets ?? candidate?.generated_files);
  if (artifacts.length) return artifacts;
  const url = candidate?.preview_url ?? candidate?.stl_url ?? (candidate?.stl_id ? `/stl/${candidate.stl_id}` : null);
  return url ? [{ url, role: 'printable' }] : [];
}

async function resolveArtifacts(candidate) {
  const local = candidateArtifacts(candidate);
  if (local.length) return local;
  const id = candidateId(candidate);
  if (!id) return [];
  try {
    const response = await api(`/training-lab/api/candidates/${encodeURIComponent(id)}/artifacts`);
    return collection(response.artifacts ?? response.files ?? response);
  } catch { return []; }
}

function thumbnail(candidate) {
  const item = candidate?.thumbnail_url ?? candidate?.thumb_url ?? candidate?.thumbnails?.[0];
  return typeof item === 'string' ? item : item?.url ?? null;
}

function renderCandidate(candidate, index) {
  const suffix = index ? 'b' : 'a';
  const card = $(`candidate-card-${suffix}`);
  const name = $(`candidate-name-${suffix}`);
  const statusEl = $(`candidate-state-${suffix}`);
  const meta = $(`candidate-meta-${suffix}`); meta.replaceChildren();
  card.dataset.selection = candidate ? isWinner(candidate) ? 'winner' : isRejected(candidate) ? 'rejected' : '' : '';
  if (!candidate) {
    name.textContent = `Waiting for Variant ${index ? 'B' : 'A'}`;
    statusEl.textContent = 'Not generated'; statusEl.className = 'status-label neutral';
    appendEmpty(meta, 'No candidate persisted.');
    state.viewers[index]?.load([]);
    return;
  }
  name.textContent = text(candidate.name ?? candidateId(candidate), `Variant ${index ? 'B' : 'A'}`);
  const candidateStatus = isWinner(candidate) ? 'Winner' : isRejected(candidate) ? 'Rejected' : candidate.status ?? 'Evaluated';
  statusEl.textContent = candidateStatus; statusEl.className = `status-label ${statusClass(candidateStatus)}`;
  const report = candidate.print_report ?? candidate.report ?? {};
  const fields = [
    ['Reward', formatScore(totalScore(candidate))], ['Δ best', formatDelta(candidate.score_delta_current_best ?? candidate.delta_from_best)],
    ['QA', candidate.qa_status ?? candidate.geometry_qa?.status], ['Dimensions', Array.isArray(report.bbox_mm) ? `${report.bbox_mm.join(' × ')} mm` : candidate.dimensions],
    ['Parts', report.parts ?? candidate.part_count], ['Runtime', formatDuration(candidate.generation_duration ?? candidate.runtime_seconds)],
  ];
  for (const [label, value] of fields) {
    const item = document.createElement('div'); item.className = 'meta-item';
    const small = document.createElement('span'); small.textContent = label;
    const strong = document.createElement('strong'); strong.textContent = text(value);
    item.append(small, strong); meta.appendChild(item);
  }
  const actions = document.createElement('div'); actions.className = 'candidate-actions';
  const inspect = document.createElement('button'); inspect.className = 'mini-button'; inspect.type = 'button'; inspect.textContent = 'Inspect';
  inspect.onclick = () => showCandidate(candidate);
  actions.appendChild(inspect); meta.appendChild(actions);
  const image = $(`mobile-thumb-${suffix}`);
  const thumb = thumbnail(candidate);
  image.hidden = !thumb; if (thumb) image.src = thumb;
  if (state.viewers[index]) {
    const token = candidateId(candidate);
    resolveArtifacts(candidate).then(artifacts => {
      if (candidateId(state.pair[index]) !== token) return;
      const profile = candidate.printer_profile_snapshot ?? state.run.printer_profile ?? state.run.config?.printer_profile ?? {};
      const options = { issues: candidateIssues(candidate), bed: profile.bed_mm ?? profile.bed };
      if (!artifacts.length && isDemoRun(state.run)) state.viewers[index].loadDemo67(index ? 'B' : 'A', options);
      else state.viewers[index].load(artifacts, options);
    });
  }
}

function formatDelta(value) {
  const number = finite(value); if (number === null) return 'Unavailable'; return `${number > 0 ? '+' : ''}${number.toFixed(number % 1 ? 1 : 0)}`;
}

function mutations(candidate) {
  const source = candidate?.mutations ?? candidate?.candidate_mutations ?? candidate?.mutation ?? candidate?.exact_mutation ?? [];
  if (Array.isArray(source)) return source.map(item => typeof item === 'string' ? { title: item } : item);
  if (typeof source === 'string') return [{ title: source }];
  return source && typeof source === 'object' ? [source] : [];
}

function renderMutations() {
  const container = $('mutation-compare'); container.replaceChildren();
  state.pair.forEach((candidate, index) => {
    const column = document.createElement('div'); column.className = 'mutation-column';
    const heading = document.createElement('h3'); heading.textContent = `Variant ${index ? 'B' : 'A'}`; column.appendChild(heading);
    const list = mutations(candidate);
    if (!list.length) appendEmpty(column, candidate ? 'No mutation details persisted.' : 'Candidate unavailable.');
    for (const mutation of list) {
      const row = document.createElement('div'); row.className = 'mutation-row';
      const label = document.createElement('span'); label.textContent = text(mutation.title ?? mutation.type ?? mutation.parameter, 'Mutation');
      const value = document.createElement('strong');
      const oldValue = mutation.original_value ?? mutation.original ?? mutation.from;
      const newValue = mutation.mutated_value ?? mutation.new_value ?? mutation.to;
      value.textContent = present(oldValue) || present(newValue) ? `${text(oldValue, '?')} → ${text(newValue, '?')}` : text(mutation.value ?? mutation.description);
      row.append(label, value);
      const reasonText = mutation.reason ?? mutation.expected_benefit ?? mutation.expected_reward_effect;
      if (present(reasonText)) { const reason = document.createElement('div'); reason.className = 'mutation-reason'; reason.textContent = String(reasonText); row.appendChild(reason); }
      column.appendChild(row);
    }
    container.appendChild(column);
  });
}

function scoreBreakdown(candidate) {
  return candidate?.score_breakdown ?? candidate?.candidate_score?.categories ?? candidate?.score?.categories ?? candidate?.scores ?? {};
}

function categoryRecord(breakdown, key) {
  if (Array.isArray(breakdown)) return breakdown.find(item => String(item.category ?? item.key).toLowerCase().replaceAll(' ', '_') === key) ?? {};
  return breakdown[key] ?? breakdown[key.replace('_', ' ')] ?? {};
}

function renderReward() {
  const container = $('reward-totals'); container.replaceChildren();
  state.pair.forEach((candidate, index) => {
    const item = document.createElement('div'); item.className = 'reward-total';
    const small = document.createElement('small'); small.textContent = `Variant ${index ? 'B' : 'A'}`;
    const strong = document.createElement('strong'); const score = totalScore(candidate); strong.textContent = score === null ? '—' : score.toFixed(score % 1 ? 1 : 0);
    item.append(small, strong); container.appendChild(item);
  });
  const candidate = state.pair.find(isWinner) ?? state.pair[0] ?? state.pair[1];
  const breakdown = scoreBreakdown(candidate);
  const rows = $('reward-breakdown'); rows.replaceChildren();
  $('reward-confidence').textContent = present(candidate?.evidence_confidence ?? candidate?.score_confidence) ? `Confidence ${candidate.evidence_confidence ?? candidate.score_confidence}` : 'Confidence unavailable';
  if (!candidate) return appendEmpty(rows, 'No candidate reward has been persisted.');
  for (const [key, label, maximum] of SCORE_CATEGORIES) {
    const record = categoryRecord(breakdown, key);
    const earned = finite(record.earned ?? record.score ?? (typeof record === 'number' ? record : null));
    const possible = finite(record.possible ?? record.maximum) ?? maximum;
    const row = document.createElement('div'); row.className = 'reward-row'; row.tabIndex = 0;
    const head = document.createElement('div'); head.className = 'reward-row-head';
    const name = document.createElement('span'); name.textContent = label;
    const points = document.createElement('span'); points.textContent = earned === null ? `Not evaluated / ${possible}` : `${earned} / ${possible}`;
    head.append(name, points);
    const track = document.createElement('div'); track.className = 'score-track'; track.setAttribute('role', 'progressbar'); track.setAttribute('aria-label', label); track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', String(possible));
    const fill = document.createElement('div'); fill.className = 'score-fill'; fill.style.width = `${earned === null ? 0 : Math.max(0, Math.min(100, earned / possible * 100))}%`;
    if (earned !== null) track.setAttribute('aria-valuenow', String(earned)); else track.setAttribute('aria-valuetext', 'Not evaluated');
    track.appendChild(fill); row.append(head, track);
    const evidence = collection(record.evidence ?? record.evidence_sources ?? candidate.score_evidence?.[key]);
    const badges = document.createElement('div'); badges.className = 'evidence-badges';
    const labels = evidence.map(item => typeof item === 'string' ? item : item.label ?? item.source_type ?? item.type).filter(Boolean);
    for (const evidenceLabel of labels.length ? labels : ['UNVERIFIED']) {
      const badge = document.createElement('span'); badge.className = `evidence-badge ${String(evidenceLabel).toLowerCase().replaceAll(/[^a-z]+/g, '-')}`; badge.textContent = String(evidenceLabel).toUpperCase(); badges.appendChild(badge);
    }
    row.appendChild(badges);
    const open = () => showEvidence(`${label} score`, record, 'REWARD EVIDENCE');
    row.addEventListener('click', open); row.addEventListener('keydown', event => { if (event.key === 'Enter') open(); });
    rows.appendChild(row);
  }
}

function allIssues() {
  const runIssues = collection(state.run?.issues ?? state.run?.qa_findings);
  const candidate = state.candidates.flatMap(item => candidateIssues(item).map(issue => ({ candidate_id: candidateId(item), ...issue })));
  return [...runIssues, ...candidate];
}

function renderIssues() {
  const container = $('issues'); container.replaceChildren();
  const issues = allIssues(); $('issue-count').textContent = issues.length;
  if (!issues.length) return appendEmpty(container, 'No persisted issues. This is not proof that none exist.');
  for (const issue of issues) {
    const item = document.createElement('button'); item.type = 'button'; item.className = 'issue';
    const head = document.createElement('div'); head.className = 'issue-head';
    const name = document.createElement('strong'); name.textContent = text(issue.issue_type ?? issue.type ?? issue.title, 'Issue');
    const severity = document.createElement('span'); severity.className = `severity ${String(issue.severity ?? 'warning').toLowerCase()}`; severity.textContent = text(issue.severity, 'warning');
    head.append(name, severity);
    const detail = document.createElement('p'); detail.textContent = text(issue.description ?? issue.message ?? issue.recommended_repair, 'Details unavailable');
    item.append(head, detail);
    item.addEventListener('click', () => {
      const index = state.pair.findIndex(candidate => candidateId(candidate) === issue.candidate_id);
      if (index >= 0 && Array.isArray(issue.coordinates ?? issue.position)) state.viewers[index]?.focusIssue(issue);
      showEvidence(name.textContent, issue, 'ISSUE EVIDENCE');
    });
    container.appendChild(item);
  }
}

function memoryRules() {
  return collection(state.run?.memory_rules ?? state.run?.memory_updates ?? state.bootstrap?.memory_rules);
}

function renderMemory() {
  const container = $('memory'); container.replaceChildren();
  const rules = memoryRules();
  if (!rules.length) return appendEmpty(container, 'No memory rules were applied or learned.');
  for (const rule of rules) {
    const status = String(rule.status ?? 'hypothesis').toLowerCase();
    const item = document.createElement('button'); item.type = 'button'; item.className = `memory-rule ${status}`;
    const head = document.createElement('div'); head.className = 'memory-head';
    const title = document.createElement('strong'); title.textContent = text(rule.title ?? rule.rule_id, 'Untitled rule');
    const badge = document.createElement('span'); badge.className = `status-label ${statusClass(status)}`; badge.textContent = status;
    head.append(title, badge);
    const detail = document.createElement('p'); detail.textContent = text(rule.recommendation ?? rule.description, 'Recommendation unavailable');
    const confidence = Math.max(0, Math.min(1, finite(rule.confidence) ?? 0));
    const track = document.createElement('div'); track.className = 'confidence-track'; const fill = document.createElement('span'); fill.style.width = `${confidence * 100}%`; track.appendChild(fill);
    item.append(head, detail, track); item.addEventListener('click', () => showEvidence(title.textContent, rule, 'MEMORY EVIDENCE'));
    container.appendChild(item);
  }
}

function renderCharts() {
  state.lineage = renderLineage($('lineage'), state.candidates, { showRejected: $('show-rejected').checked, onSelect: showCandidate });
  const summary = renderScoreChart($('score-chart'), state.candidates, { baseline: state.run.baseline_score ?? state.run.initial_score, target: state.run.target_score ?? state.run.config?.target_reward_score });
  $('score-chart-summary').textContent = summary || 'No evaluated candidate scores are available.';
}

function showCandidate(candidate) {
  const title = text(candidate.name ?? candidateId(candidate), 'Candidate');
  showEvidence(title, candidate, 'CANDIDATE VERSION');
  const body = $('evidence-body');
  const actions = document.createElement('div'); actions.className = 'candidate-actions';
  const add = (label, handler, danger = false) => {
    const button = document.createElement('button'); button.type = 'button';
    button.className = `button ${danger ? 'danger' : 'secondary'}`; button.textContent = label;
    button.onclick = handler; actions.appendChild(button);
  };
  add('Compare with parent', () => {
    state.manualPair = [candidate.parent_candidate_id, candidateId(candidate)];
    $('evidence-dialog').close(); renderAll();
  });
  add('Restore as best', async () => {
    try { await postJSON(`/training-lab/api/candidates/${encodeURIComponent(candidateId(candidate))}/restore`); $('evidence-dialog').close(); await loadRun(runId(state.run)); }
    catch (error) { setConnection(`Restore failed: ${error.message}`, 'error'); }
  });
  add('Branch', async () => {
    try { const created = await postJSON(`/training-lab/api/candidates/${encodeURIComponent(candidateId(candidate))}/branch`); $('evidence-dialog').close(); await initialize(); await loadRun(runId(created)); }
    catch (error) { setConnection(`Branch failed: ${error.message}`, 'error'); }
  });
  add('Delete candidate', async () => {
    if (!confirm('Delete this isolated candidate and its artifacts? Protected or parent candidates cannot be deleted.')) return;
    try { await api(`/training-lab/api/candidates/${encodeURIComponent(candidateId(candidate))}`, { method: 'DELETE' }); $('evidence-dialog').close(); await loadRun(runId(state.run)); }
    catch (error) { setConnection(`Delete failed: ${error.message}`, 'error'); }
  }, true);
  body.appendChild(actions);
}

function eventKey(event) { return event.event_id ?? event.sequence ?? event.seq ?? `${event.timestamp ?? event.created_at ?? ''}|${event.event_type ?? event.type ?? ''}|${event.message ?? ''}`; }
function mergeEvents(events) {
  for (const event of events) {
    const key = eventKey(event); if (state.eventKeys.has(key)) continue;
    state.eventKeys.add(key); state.events.push(event);
    state.lastSequence = Math.max(state.lastSequence, finite(event.sequence ?? event.seq) ?? 0);
  }
  state.events.sort((a, b) => new Date(a.timestamp ?? a.created_at ?? 0) - new Date(b.timestamp ?? b.created_at ?? 0));
}

function renderEvents() {
  const container = $('event-log');
  const pinned = container.scrollHeight - container.scrollTop - container.clientHeight < 30;
  container.replaceChildren();
  const filter = $('log-filter').value;
  const events = state.events.filter(event => filter === 'all' || String(event.severity ?? 'info').toLowerCase() === filter);
  if (!events.length) appendEmpty(container, 'No persisted events match this filter.');
  for (const event of events) {
    const severity = String(event.severity ?? 'info').toLowerCase();
    const row = document.createElement('div'); row.className = `log-entry ${severity}`;
    const time = document.createElement('time'); time.textContent = isoTime(event.timestamp ?? event.created_at ?? event.ts);
    const level = document.createElement('span'); level.textContent = severity.toUpperCase();
    const message = document.createElement('b'); message.textContent = text(event.message ?? event.event_type ?? event.type, 'Event');
    row.append(time, level, message); row.addEventListener('click', () => showEvidence(message.textContent, event, 'EVENT DETAILS'));
    container.appendChild(row);
  }
  if (pinned) container.scrollTop = container.scrollHeight;
}

function renderSummary() {
  const container = $('run-summary'); container.replaceChildren();
  const summary = state.run.summary ?? {};
  const fields = [
    ['Baseline', state.run.baseline_model_id ?? summary.baseline_model], ['Final best', state.run.current_best_candidate_id ?? summary.final_best_model],
    ['Initial score', state.run.baseline_score ?? summary.initial_score], ['Final score', state.run.current_best_score ?? summary.final_score],
    ['Candidates', summary.candidates_generated ?? state.candidates.length], ['Rejected', summary.candidates_rejected ?? state.candidates.filter(isRejected).length],
    ['Runtime', formatDuration(summary.runtime_seconds ?? state.run.elapsed_seconds)], ['Estimated cost', present(summary.estimated_cost ?? state.run.estimated_cost) ? `$${finite(summary.estimated_cost ?? state.run.estimated_cost)?.toFixed(2) ?? summary.estimated_cost}` : 'Unavailable'],
    ['Physical status', summary.physical_status ?? state.run.physical_validation_status], ['Promotion', summary.promotion_recommendation ?? state.run.promotion_recommendation],
  ];
  for (const [label, value] of fields) addField(container, label, value);
}

function renderAll() {
  populateHeader(); renderPipeline(); renderConstraints();
  state.pair = choosePair(state.candidates, state.run);
  renderCandidate(state.pair[0], 0); renderCandidate(state.pair[1], 1);
  renderMutations(); renderReward(); renderIssues(); renderMemory(); renderCharts(); renderEvents(); renderSummary();
  $('lab-app').setAttribute('aria-busy', 'false');
}

async function loadRun(id, { quiet = false } = {}) {
  if (!id || state.loadingRun || state.disposed) return;
  state.loadingRun = true;
  if (!quiet) setConnection('Loading persisted run…');
  try {
    const response = await api(`/training-lab/api/runs/${encodeURIComponent(id)}`);
    state.run = response.run ?? response;
    state.candidates = flattenCandidates(state.run);
    if (!quiet) {
      state.events = []; state.eventKeys.clear(); state.lastSequence = 0;
      const url = new URL(location.href); url.searchParams.set('run', id); history.replaceState(null, '', url);
    }
    mergeEvents(collection(state.run.events ?? state.run.training_events));
    $('workspace').hidden = false; $('disabled-state').hidden = true; $('empty-state').hidden = true;
    renderAll(); setConnection('Persisted state connected', 'ok');
    schedulePoll();
  } catch (error) {
    setConnection(`Run unavailable: ${error.message}`, 'error');
    if (!quiet) { $('workspace').hidden = true; $('empty-state').hidden = false; }
  } finally { state.loadingRun = false; }
}

async function pollEvents() {
  const id = runId(state.run); if (!id || state.disposed) return;
  try {
    const response = await api(`/training-lab/api/runs/${encodeURIComponent(id)}/events?after_seq=${state.lastSequence}`);
    mergeEvents(collection(response.events ?? response)); renderEvents();
    setConnection('Persisted state connected', 'ok');
  } catch (error) { setConnection(`Reconnect pending: ${error.message}`, 'error'); }
}

function isTerminal(run) { return /complete|completed|failed|aborted|stopped|cancelled/.test(String(run?.status ?? '').toLowerCase()); }
function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!state.run || state.disposed) return;
  const delay = isTerminal(state.run) ? 10000 : 2500;
  state.pollTimer = setTimeout(async () => {
    await pollEvents();
    await loadRun(runId(state.run), { quiet: true });
    schedulePoll();
  }, delay);
}

function setupViewers() {
  if (isMobile) return;
  state.viewers = [new CandidateViewer($('viewer-a'), 'Variant A'), new CandidateViewer($('viewer-b'), 'Variant B')];
  state.viewers[0].setPartner(state.viewers[1]); state.viewers[1].setPartner(state.viewers[0]);
  for (const viewer of state.viewers) viewer.setSynchronized(true);
}

const selectedRunMode = () => document.querySelector('input[name="run-mode"]:checked')?.value ?? 'evolve_existing';

function setSelectedModel(model) {
  state.selectedModel = model;
  const box = $('selected-model');
  box.classList.toggle('ready', Boolean(model));
  box.replaceChildren();
  if (!model) box.textContent = 'Select a Library model.';
  else {
    const strong = document.createElement('strong'); strong.textContent = model.name ?? 'Untitled model';
    const code = document.createElement('code'); code.textContent = model.id; code.style.userSelect = 'text';
    const detail = document.createElement('span'); detail.textContent = ` · v${model.latest_version ?? 1} · ${String(model.status ?? 'ready').replaceAll('_', ' ')}`;
    box.append(strong, document.createTextNode(' · '), code, detail);
  }
  renderModelPicker(); validateRunForm();
}

function renderModelPicker() {
  const container = $('model-picker-results'); container.replaceChildren();
  const query = $('model-search').value.trim().toLowerCase();
  const models = state.libraryModels.filter(model => !query || `${model.name ?? ''} ${model.id} ${model.prompt ?? ''}`.toLowerCase().includes(query)).slice(0, 40);
  if (!models.length) return appendEmpty(container, query ? 'No Library models match.' : 'No Library models are available.');
  for (const model of models) {
    const button = document.createElement('button'); button.type = 'button'; button.className = `model-choice${state.selectedModel?.id === model.id ? ' selected' : ''}`;
    const img = document.createElement('img'); img.src = model.thumbnail_url ?? `/models/${model.id}/thumb`; img.alt = '';
    const body = document.createElement('span');
    const name = document.createElement('strong'); name.textContent = model.name ?? 'Untitled model';
    const detail = document.createElement('small'); detail.textContent = `v${model.latest_version ?? 1} · ${String(model.status ?? 'ready').replaceAll('_', ' ')}`;
    const code = document.createElement('code'); code.textContent = model.id;
    body.append(name, detail, code); button.append(img, body);
    button.onclick = () => setSelectedModel(model); container.appendChild(button);
  }
}

function addLockRow(lock = {}) {
  const row = document.createElement('div'); row.className = 'lock-row';
  const type = document.createElement('select'); type.className = 'field';
  for (const [value, label] of [['module', 'Module'], ['parameter', 'Parameter'], ['literal', 'Required text']]) type.appendChild(new Option(label, value));
  type.value = lock.type ?? 'module';
  const name = document.createElement('input'); name.className = 'field'; name.placeholder = 'Name / label'; name.value = lock.name ?? '';
  const value = document.createElement('input'); value.className = 'field'; value.placeholder = 'Required value (optional)'; value.value = lock.value ?? '';
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'mini-button'; remove.textContent = '×'; remove.title = 'Remove requirement';
  remove.onclick = () => { row.remove(); syncAdvancedLocks(); validateRunForm(); };
  for (const input of [type, name, value]) input.addEventListener('input', () => { syncAdvancedLocks(); validateRunForm(); });
  row.append(type, name, value, remove); $('lock-editor').appendChild(row);
  syncAdvancedLocks();
}

function loadStructuredLocks(locks) {
  $('lock-editor').replaceChildren();
  for (const lock of locks.length ? locks : [{}]) addLockRow(lock);
}

function structuredLocks() {
  return [...$('lock-editor').querySelectorAll('.lock-row')].map(row => {
    const [type, name, value] = row.querySelectorAll('select,input');
    const item = { type: type.value, name: name.value.trim() };
    if (value.value.trim()) item.value = value.value.trim();
    return item;
  }).filter(item => item.name || item.value);
}

function syncAdvancedLocks() {
  if (!$('advanced-locks').checked) $('new-locks').value = JSON.stringify(structuredLocks(), null, 2);
}

function collectLocks() {
  const locks = $('advanced-locks').checked ? JSON.parse($('new-locks').value || '[]') : structuredLocks();
  if (!Array.isArray(locks)) throw new Error('Locked requirements JSON must be an array.');
  for (const lock of locks) {
    if (!lock || typeof lock !== 'object' || !['module', 'parameter', 'literal'].includes(lock.type)) throw new Error('Each locked requirement needs a valid type.');
    if (lock.type !== 'literal' && !String(lock.name ?? '').trim()) throw new Error('Module and parameter requirements need a name.');
    if (lock.type === 'literal' && !String(lock.value ?? '').trim()) throw new Error('Required text needs a value.');
  }
  return locks;
}

function selectedProfile() {
  return state.profiles.find(profile => profile.name === $('new-profile').value) ?? null;
}

function validateRunForm() {
  const mode = selectedRunMode();
  $('starting-model-section').hidden = mode !== 'evolve_existing';
  let locksOk = true;
  try { collectLocks(); $('locks-error').textContent = ''; } catch (error) { locksOk = false; $('locks-error').textContent = error.message; }
  const iterations = finite($('limit-iterations').value);
  const runtime = finite($('limit-runtime').value);
  const failures = finite($('limit-failures').value);
  const noImprovement = finite($('limit-no-improvement').value);
  const target = $('limit-target').value === '' ? null : finite($('limit-target').value);
  const limitsOk = iterations >= 1 && runtime >= 1 && failures >= 1 && noImprovement >= 1 && (target === null || (target >= 0 && target <= 100));
  const valid = Boolean($('new-spec').value.trim() && selectedProfile() && locksOk && limitsOk && (mode === 'create_from_spec' || state.selectedModel));
  $('create-run-submit').disabled = !valid;
  $('run-estimate').textContent = limitsOk
    ? `Up to ${iterations} refinement iteration${iterations === 1 ? '' : 's'} within ${runtime} minute${runtime === 1 ? '' : 's'}; stop after ${failures} failed generation${failures === 1 ? '' : 's'} or ${noImprovement} iteration${noImprovement === 1 ? '' : 's'} without improvement${target === null ? '.' : `; target score ${target}.`}`
    : 'Enter valid bounded stop controls.';
  return valid;
}

async function validatePastedModel() {
  const id = $('new-source-id').value.trim().toLowerCase();
  $('model-picker-error').textContent = '';
  if (!/^[0-9a-f]{12}$/.test(id)) { $('model-picker-error').textContent = 'Enter the 12-character public Library ID.'; setSelectedModel(null); return; }
  try {
    const model = await api(`/models/${id}/metadata`);
    const cached = state.libraryModels.find(item => item.id === id);
    setSelectedModel(cached ?? model);
  } catch (error) {
    setSelectedModel(null); $('model-picker-error').textContent = `Starting model ${id} no longer exists or is unavailable.`;
  }
}

async function prepareRunDialog() {
  $('new-run-dialog').showModal(); $('model-picker-error').textContent = '';
  try {
    const [models, profiles] = await Promise.all([api('/models'), api('/profiles')]);
    state.libraryModels = collection(models); state.profiles = collection(profiles.profiles);
    const select = $('new-profile'); select.replaceChildren(new Option('Select printer profile…', ''));
    for (const profile of state.profiles) select.appendChild(new Option(profile.name, profile.name, false, profile.name === profiles.default));
    if (!select.value && profiles.default) select.value = profiles.default;
    renderModelPicker(); validateRunForm();
  } catch (error) { $('model-picker-error').textContent = `Could not load Library models: ${error.message}`; validateRunForm(); }
}

function setupControls() {
  $('refresh-run').addEventListener('click', () => state.run ? loadRun(runId(state.run)) : initialize());
  $('run-select').addEventListener('change', event => { if (event.target.value) loadRun(event.target.value); });
  $('open-demo').addEventListener('click', () => { const id = demoRunId(); if (id) loadRun(id); });
  $('new-run').addEventListener('click', prepareRunDialog);
  document.querySelectorAll('[data-close-new-run]').forEach(button => button.addEventListener('click', () => $('new-run-dialog').close()));
  document.querySelectorAll('input[name="run-mode"]').forEach(input => input.addEventListener('change', validateRunForm));
  $('model-search').addEventListener('input', renderModelPicker);
  $('paste-id-toggle').addEventListener('change', event => { $('paste-id-row').hidden = !event.target.checked; });
  $('validate-pasted-id').addEventListener('click', validatePastedModel);
  $('new-source-id').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); validatePastedModel(); } });
  $('add-lock').addEventListener('click', () => { addLockRow(); validateRunForm(); });
  $('advanced-locks').addEventListener('change', event => {
    if (!event.target.checked) {
      try { loadStructuredLocks(JSON.parse($('new-locks').value || '[]')); $('locks-error').textContent = ''; }
      catch (error) { event.target.checked = true; $('locks-error').textContent = error.message; }
    }
    $('lock-editor').hidden = event.target.checked; $('add-lock').hidden = event.target.checked; $('new-locks').hidden = !event.target.checked;
    validateRunForm();
  });
  for (const id of ['new-spec', 'new-profile', 'new-locks', 'limit-iterations', 'limit-runtime', 'limit-failures', 'limit-no-improvement', 'limit-target']) $(id).addEventListener('input', validateRunForm);
  $('new-run-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!validateRunForm()) return; setConnection('Validating and creating isolated run…');
    try {
      const mode = selectedRunMode();
      const profile = selectedProfile();
      const locks = collectLocks();
      if (mode === 'evolve_existing') {
        try { await api(`/models/${state.selectedModel.id}/metadata`); }
        catch { setSelectedModel(null); $('model-picker-error').textContent = 'The selected starting model no longer exists. Choose another model.'; throw new Error('Starting model was deleted before the run was created.'); }
      }
      const target = $('limit-target').value === '' ? null : Number($('limit-target').value);
      const created = await postJSON('/training-lab/api/runs', {
        run_mode: mode, source_model_id: mode === 'evolve_existing' ? state.selectedModel.id : null, source_prompt: '',
        validated_spec: $('new-spec').value, printer_profile: profile,
        material_profile: { material: profile.material, layer_height: profile.layer },
        locked_constraints: locks, attached_reference_roles: [], export_exclusions: [],
        active_backend: 'codex/cli-default', auto_start: false,
        limits: {
          variants_per_generation: 2,
          maximum_iterations: Number($('limit-iterations').value),
          target_reward_score: target,
          maximum_runtime_seconds: Number($('limit-runtime').value) * 60,
          repeated_generation_failure_limit: Number($('limit-failures').value),
          no_improvement_limit: Number($('limit-no-improvement').value),
          maximum_estimated_cost: 10, maximum_backend_calls: Math.max(10, Number($('limit-iterations').value) * 4 + 2),
          mutation_strength: 0.25, exploration_rate: 0.15,
        },
      });
      $('new-run-dialog').close(); await initialize(); await loadRun(runId(created));
    } catch (error) { setConnection(`Run creation failed: ${error.message}`, 'error'); }
  });
  $('resume-run').addEventListener('click', async () => {
    try { await postJSON(`/training-lab/api/runs/${encodeURIComponent(runId(state.run))}/start`); await loadRun(runId(state.run)); }
    catch (error) { setConnection(`Start failed: ${error.message}`, 'error'); }
  });
  $('stop-run').addEventListener('click', async () => {
    try { await postJSON(`/training-lab/api/runs/${encodeURIComponent(runId(state.run))}/stop-after-generation`); await loadRun(runId(state.run)); }
    catch (error) { setConnection(`Stop request failed: ${error.message}`, 'error'); }
  });
  $('cancel-run').addEventListener('click', async () => {
    if (!confirm('Cancel this run immediately? The in-progress candidate will be kept with its failure reason.')) return;
    try { await postJSON(`/training-lab/api/runs/${encodeURIComponent(runId(state.run))}/cancel`); await loadRun(runId(state.run)); }
    catch (error) { setConnection(`Cancellation failed: ${error.message}`, 'error'); }
  });
  $('export-run').addEventListener('click', async () => {
    try {
      const record = await postJSON('/training-lab/api/datasets', { dataset_type: 'all', format: 'zip', run_id: runId(state.run) });
      location.href = `/training-lab/api/datasets/${encodeURIComponent(record.id)}/download`;
    } catch (error) { setConnection(`Export failed: ${error.message}`, 'error'); }
  });
  $('open-benchmarks').addEventListener('click', async () => {
    try { showEvidence('Benchmark catalog and persisted results', await api('/training-lab/api/benchmarks'), 'REGRESSION GATE'); }
    catch (error) { setConnection(`Benchmarks unavailable: ${error.message}`, 'error'); }
  });
  $('sync-camera').addEventListener('change', event => state.viewers.forEach(viewer => viewer?.setSynchronized(event.target.checked)));
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => state.viewers.forEach(viewer => viewer?.preset(button.dataset.view))));
  $('fit-viewers').addEventListener('click', () => state.viewers.forEach(viewer => viewer?.fit()));
  document.querySelectorAll('[data-layer]').forEach(input => input.addEventListener('change', () => state.viewers.forEach(viewer => viewer?.setLayer(input.dataset.layer, input.checked))));
  $('lineage-fit').addEventListener('click', () => state.lineage?.fit());
  $('show-rejected').addEventListener('change', renderCharts);
  $('log-filter').addEventListener('change', renderEvents);
  window.addEventListener('beforeunload', dispose);
}

function demoRunId() {
  const bootstrap = state.bootstrap ?? {};
  return runId(bootstrap.demo_run) ?? bootstrap.demo_run_id ?? runId(getRuns(bootstrap).find(isDemoRun));
}

function fillRunSelect() {
  const select = $('run-select'); select.replaceChildren(new Option('Select run…', ''));
  for (const run of getRuns(state.bootstrap)) {
    const id = runId(run); if (!id) continue;
    const label = `${isDemoRun(run) ? 'DEMO · ' : ''}${run.name ?? id}${run.status ? ` · ${run.status}` : ''}`;
    select.appendChild(new Option(label, id));
  }
  const demoId = demoRunId(); $('open-demo').hidden = !demoId;
}

async function initialize() {
  clearTimeout(state.pollTimer); setConnection('Connecting to Training Lab…');
  try {
    state.bootstrap = await api('/training-lab/api/bootstrap');
    fillRunSelect(); populateHeader();
    if (!labEnabled(state.bootstrap)) { renderDisabled(state.bootstrap); setConnection('Training Lab disabled'); return; }
    const params = new URLSearchParams(location.search);
    const requested = params.get('run');
    const active = runId(state.bootstrap.active_run) ?? state.bootstrap.active_run_id;
    const id = requested ?? active ?? runId(getRuns(state.bootstrap)[0]);
    if (!id) {
      $('disabled-state').hidden = true; $('workspace').hidden = true; $('empty-state').hidden = false;
      $('lab-app').setAttribute('aria-busy', 'false'); setConnection('No persisted run selected'); return;
    }
    $('run-select').value = id;
    await loadRun(id);
  } catch (error) {
    state.bootstrap = {};
    renderDisabled({}, error); setConnection(`Backend unavailable: ${error.message}`, 'error');
  }
}

function dispose() {
  if (state.disposed) return;
  state.disposed = true; clearTimeout(state.pollTimer);
  state.viewers.forEach(viewer => viewer?.dispose());
}

setupViewers(); setupControls(); addLockRow(); validateRunForm(); initialize();
