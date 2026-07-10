const SVG_NS = 'http://www.w3.org/2000/svg';
const svgEl = (name, attrs = {}) => {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  return element;
};

const num = value => Number.isFinite(Number(value)) ? Number(value) : null;
const candidateId = candidate => candidate?.candidate_id ?? candidate?.id ?? '';
const generation = candidate => num(candidate?.generation_number ?? candidate?.generation) ?? 0;
const score = candidate => num(candidate?.reward_score ?? candidate?.total_score ?? candidate?.score?.total ?? candidate?.score);
const status = candidate => String(candidate?.selection_status ?? candidate?.status ?? '').toLowerCase();
const isWinner = candidate => candidate?.winner === true || /winner|current.best|selected/.test(status(candidate));
const isRejected = candidate => candidate?.rejected === true || /reject|failed|invalid|loser/.test(status(candidate));

export function renderScoreChart(svg, candidates, options = {}) {
  svg.replaceChildren();
  const width = 520, height = 245, pad = { left: 32, right: 12, top: 12, bottom: 28 };
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const points = candidates.map(candidate => ({ candidate, generation: generation(candidate), score: score(candidate) })).filter(point => point.score !== null);
  const maxGeneration = Math.max(1, ...points.map(point => point.generation));
  const x = value => pad.left + value / maxGeneration * (width - pad.left - pad.right);
  const y = value => pad.top + (100 - Math.max(0, Math.min(100, value))) / 100 * (height - pad.top - pad.bottom);

  for (const value of [0, 25, 50, 75, 100]) {
    svg.appendChild(svgEl('line', { x1: pad.left, y1: y(value), x2: width - pad.right, y2: y(value), class: 'chart-grid' }));
    const label = svgEl('text', { x: pad.left - 6, y: y(value) + 3, 'text-anchor': 'end', class: 'chart-axis-label' });
    label.textContent = value;
    svg.appendChild(label);
  }
  for (let value = 0; value <= maxGeneration; value++) {
    const label = svgEl('text', { x: x(value), y: height - 8, 'text-anchor': 'middle', class: 'chart-axis-label' });
    label.textContent = value === 0 ? 'Base' : `G${value}`;
    svg.appendChild(label);
  }
  const target = num(options.target);
  if (target !== null) {
    svg.appendChild(svgEl('line', { x1: pad.left, y1: y(target), x2: width - pad.right, y2: y(target), class: 'chart-target' }));
  }

  const sorted = points.slice().sort((a, b) => a.generation - b.generation || String(candidateId(a.candidate)).localeCompare(candidateId(b.candidate)));
  const path = sorted.map((point, index) => `${index ? 'L' : 'M'} ${x(point.generation)} ${y(point.score)}`).join(' ');
  if (path) svg.appendChild(svgEl('path', { d: path, class: 'chart-candidates' }));
  let best = num(options.baseline);
  const bestPoints = [];
  if (best !== null) bestPoints.push({ generation: 0, score: best });
  for (let gen = 0; gen <= maxGeneration; gen++) {
    for (const point of sorted.filter(item => item.generation === gen && isWinner(item.candidate))) best = Math.max(best ?? 0, point.score);
    if (best !== null) bestPoints.push({ generation: gen, score: best });
  }
  const bestPath = bestPoints.map((point, index) => `${index ? 'L' : 'M'} ${x(point.generation)} ${y(point.score)}`).join(' ');
  if (bestPath) svg.appendChild(svgEl('path', { d: bestPath, class: 'chart-best' }));

  for (const point of sorted) {
    const state = isWinner(point.candidate) ? 'winner' : isRejected(point.candidate) ? 'rejected' : 'neutral';
    const circle = svgEl('circle', { cx: x(point.generation), cy: y(point.score), r: 4, class: `chart-point ${state}` });
    const title = svgEl('title');
    title.textContent = `${candidateId(point.candidate) || 'Candidate'}: ${point.score}/100`;
    circle.appendChild(title);
    svg.appendChild(circle);
  }
  return sorted.map(point => `Generation ${point.generation}, ${candidateId(point.candidate)}, score ${point.score}, ${status(point.candidate) || 'unselected'}`).join('. ');
}

export function renderLineage(container, candidates, options = {}) {
  container.replaceChildren();
  const visible = candidates.filter(candidate => options.showRejected !== false || !isRejected(candidate));
  if (!visible.length) {
    const empty = document.createElement('div'); empty.className = 'empty-note'; empty.textContent = 'No persisted lineage nodes available.'; container.appendChild(empty);
    return { fit() {} };
  }
  const stage = document.createElement('div');
  stage.className = 'lineage-stage';
  const generations = new Map();
  for (const candidate of visible) {
    const gen = generation(candidate);
    if (!generations.has(gen)) generations.set(gen, []);
    generations.get(gen).push(candidate);
  }
  const maxGeneration = Math.max(...generations.keys());
  const maxRows = Math.max(...[...generations.values()].map(items => items.length));
  const stageWidth = Math.max(720, (maxGeneration + 1) * 190 + 60);
  const stageHeight = Math.max(250, maxRows * 78 + 60);
  stage.style.width = `${stageWidth}px`;
  stage.style.height = `${stageHeight}px`;
  const edges = svgEl('svg', { width: stageWidth, height: stageHeight, viewBox: `0 0 ${stageWidth} ${stageHeight}` });
  stage.appendChild(edges);
  const positions = new Map();
  for (const [gen, items] of generations) {
    items.sort((a, b) => String(a.variant_label ?? a.variant ?? '').localeCompare(String(b.variant_label ?? b.variant ?? '')));
    items.forEach((candidate, index) => {
      const x = 25 + gen * 190;
      const y = 28 + index * 78;
      positions.set(candidateId(candidate), { x, y, candidate });
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `lineage-node${isWinner(candidate) ? ' current-best' : ''}${isRejected(candidate) ? ' rejected' : ''}`;
      button.style.left = `${x}px`; button.style.top = `${y}px`;
      const image = document.createElement('img');
      const thumb = candidate.thumbnail_url ?? candidate.thumb_url ?? candidate.thumbnails?.[0];
      if (thumb) image.src = typeof thumb === 'string' ? thumb : thumb.url;
      image.alt = '';
      const text = document.createElement('span');
      const strong = document.createElement('strong');
      strong.textContent = `${gen ? `G${gen}` : 'Baseline'}${candidate.variant_label ? candidate.variant_label : ''} · ${score(candidate) ?? '—'}`;
      const small = document.createElement('small');
      small.textContent = (candidate.mutation?.title ?? candidate.exact_mutation ?? status(candidate)) || 'candidate';
      text.append(strong, small); button.append(image, text);
      button.addEventListener('click', () => options.onSelect?.(candidate));
      stage.appendChild(button);
    });
  }
  for (const position of positions.values()) {
    const parentId = position.candidate.parent_candidate_id ?? position.candidate.parent_id ?? position.candidate.parent_model_id;
    const parent = positions.get(parentId);
    if (!parent) continue;
    const x1 = parent.x + 142, y1 = parent.y + 24, x2 = position.x, y2 = position.y + 24;
    const path = svgEl('path', { d: `M ${x1} ${y1} C ${x1 + 30} ${y1}, ${x2 - 30} ${y2}, ${x2} ${y2}`, class: `lineage-edge${isWinner(position.candidate) ? ' winner' : ''}` });
    edges.appendChild(path);
  }
  container.appendChild(stage);
  const controller = {
    fit() {
      const scale = Math.min(1, Math.max(.45, (container.clientWidth - 12) / stageWidth));
      stage.style.transform = `scale(${scale})`;
      container.style.height = `${Math.max(220, stageHeight * scale)}px`;
      container.scrollTo({ left: 0, top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    },
  };
  requestAnimationFrame(controller.fit);
  return controller;
}
