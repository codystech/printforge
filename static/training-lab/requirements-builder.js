// Guided locked-requirement builder for the Training Lab New Evolution Run modal.
// Renders [Category ▼] [Requirement ▼] [Smart value] [Severity ▼] [×] rows, quick
// presets, spec extraction, conflict/duplicate validation, a live summary and a
// two-way-synced Advanced JSON view. Emits the same canonical requirement array
// the backend (evolution_lab/requirements.py) normalizes and enforces.

const uid = () => 'req-' + Math.random().toString(36).slice(2, 10);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

export const SEVERITIES = [
  ['hard_lock', 'Hard lock'], ['required', 'Required'], ['preferred', 'Preferred'],
  ['avoid', 'Avoid'], ['forbidden', 'Forbidden'],
];
const SEVERITY_HELP = {
  hard_lock: 'Candidate is rejected if this is violated.',
  required: 'Strong scoring penalty; heavily disfavored if unmet.',
  preferred: 'Scoring bonus when met; not required.',
  avoid: 'Scoring penalty when present; not an automatic reject.',
  forbidden: 'Candidate is rejected if this appears.',
};

export const CATEGORIES = [
  ['dimensions', 'Dimensions'], ['identity', 'Identity and appearance'],
  ['text', 'Text and labels'], ['function', 'Function'], ['moving', 'Moving parts'],
  ['clearances', 'Clearances and tolerances'], ['printability', 'Printability'],
  ['parts', 'Parts and assembly'], ['export', 'Export'],
  ['safety', 'Safety and validation'], ['printing', 'Printing preferences'],
  ['references', 'Attached references'], ['custom', 'Custom'],
];

const SUPPORT_OPTS = ['No supports', 'Prefer no supports', 'Supports allowed', 'Tree supports only', 'Any supports allowed'];
// control: dim3 | number | int | text | chips | toggle | select | checkboxes | printer | material | custom
const R = (type, label, control, extra = {}) => ({ type, label, control, ...extra });
export const REQUIREMENTS = {
  dimensions: [
    R('maximum_overall_size', 'Maximum overall size', 'dim3'),
    R('exact_overall_size', 'Exact overall size', 'dim3'),
    R('minimum_wall_thickness', 'Minimum wall thickness', 'number', { unit: 'mm', default: 2 }),
    R('must_fit_selected_printer', 'Must fit selected printer', 'toggle'),
  ],
  identity: [
    R('required_shape', 'Required shape', 'text', { placeholder: 'e.g. Connected 67' }),
    R('required_style', 'Required style', 'text', { placeholder: 'e.g. rounded, low-poly' }),
    R('preserve_recognizable_form', 'Preserve recognizable form', 'toggle'),
    R('forbidden_visual_feature', 'Forbidden visual feature', 'text', { sev: 'forbidden', placeholder: 'e.g. sharp spikes' }),
  ],
  text: [
    R('required_text', 'Required text', 'text', { placeholder: 'e.g. SIX SEVEN' }),
    R('text_position', 'Text position', 'select', { options: ['Top', 'Bottom', 'Front', 'Back', 'Left', 'Right', 'Center'] }),
    R('minimum_text_height', 'Minimum text height', 'number', { unit: 'mm', default: 6 }),
    R('minimum_emboss_engraving_depth', 'Minimum emboss/engraving depth', 'number', { unit: 'mm', default: 0.8 }),
    R('preserve_exact_spelling', 'Preserve exact spelling', 'text', { placeholder: 'exact spelling to keep' }),
  ],
  function: [
    R('required_mechanisms', 'Required mechanisms', 'chips', { suggestions: ['Spinner', 'Slider', 'Button', 'Hinge', 'Snap fit', 'Rotating ring', 'Gear', 'Latch'] }),
    R('required_action', 'Required action', 'text', { placeholder: 'e.g. lid opens and latches' }),
    R('required_opening_or_cavity', 'Required opening or cavity', 'text', { placeholder: 'e.g. USB-C port cutout' }),
    R('preserve_existing_function', 'Preserve existing function', 'toggle'),
    R('forbidden_function', 'Forbidden function', 'text', { sev: 'forbidden' }),
  ],
  moving: [
    R('required_moving_part', 'Required moving part', 'chips', { suggestions: ['Spinner', 'Slider', 'Hinge', 'Rotating ring', 'Gear', 'Wheel'] }),
    R('must_remain_captive', 'Must remain captive', 'chips', { suggestions: ['Spinner', 'Slider', 'Pin', 'Axle'] }),
    R('travel_distance', 'Travel distance', 'number', { unit: 'mm', default: 10 }),
    R('required_rotation', 'Required rotation', 'number', { unit: 'deg', default: 360 }),
    R('must_not_bind', 'Must not bind', 'toggle'),
    R('no_hardware_required', 'No hardware required', 'toggle'),
  ],
  clearances: [
    R('minimum_moving_clearance', 'Minimum moving clearance', 'number', { unit: 'mm', default: 0.35 }),
    R('slider_clearance', 'Slider clearance', 'number', { unit: 'mm', default: 0.35 }),
    R('rotational_clearance', 'Rotational clearance', 'number', { unit: 'mm', default: 0.4 }),
    R('snap_fit_clearance', 'Snap-fit clearance', 'number', { unit: 'mm', default: 0.15 }),
    R('press_fit_allowance', 'Press-fit allowance', 'number', { unit: 'mm', default: 0.0 }),
    R('port_clearance', 'Port clearance', 'number', { unit: 'mm', default: 0.3 }),
    R('use_printer_calibration_values', 'Use printer calibration values', 'toggle'),
  ],
  printability: [
    R('no_floating_geometry', 'No floating geometry', 'toggle'),
    R('watertight_parts_required', 'Watertight parts required', 'toggle'),
    R('no_zero_thickness_geometry', 'No zero-thickness geometry', 'toggle'),
    R('minimum_feature_thickness', 'Minimum feature thickness', 'number', { unit: 'mm', default: 1 }),
    R('must_fit_build_volume', 'Must fit build volume', 'toggle'),
    R('support_preference', 'Support preference', 'select', { options: SUPPORT_OPTS, sev: 'preferred', default: 'Prefer no supports' }),
    R('required_print_orientation', 'Required print orientation', 'select', { options: ['Flat on bed', 'On its side', 'Vertical', 'As modeled'], sev: 'preferred' }),
  ],
  parts: [
    R('maximum_part_count', 'Maximum part count', 'int', { default: 4 }),
    R('exact_part_count', 'Exact part count', 'int', { default: 1 }),
    R('required_parts', 'Required parts', 'chips', { suggestions: ['Body', 'Lid', 'Spinner', 'Slider', 'Retainer'] }),
    R('forbidden_parts', 'Forbidden parts', 'chips', { sev: 'forbidden' }),
    R('separate_parts_required', 'Separate parts required', 'toggle'),
    R('single_piece_required', 'Single-piece required', 'toggle'),
    R('no_glue_required', 'No glue required', 'toggle'),
  ],
  export: [
    R('allowed_export_parts', 'Allowed export parts', 'chips', { suggestions: ['Body', 'Spinner', 'Slider', 'Retainer', 'Lid'] }),
    R('excluded_export_parts', 'Excluded export parts', 'chips', { sev: 'forbidden' }),
    R('reference_geometry_excluded', 'Reference geometry excluded', 'toggle'),
    R('required_export_formats', 'Required export formats', 'checkboxes', { options: ['STL', '3MF', 'OBJ', 'GLB', 'ZIP'] }),
    R('preserve_separate_parts', 'Preserve separate parts', 'toggle'),
    R('printable_parts_only', 'Printable parts only', 'toggle'),
  ],
  safety: [
    R('no_collisions', 'No collisions', 'toggle'),
    R('no_broken_locks', 'No broken locks', 'toggle'),
    R('no_scale_errors', 'No scale errors', 'toggle'),
    R('no_reference_export_leakage', 'No reference-export leakage', 'toggle'),
    R('qa_must_pass', 'QA must pass', 'toggle'),
    R('slicer_validation_required', 'Slicer validation required', 'toggle'),
  ],
  printing: [
    R('printer_profile', 'Printer profile', 'printer', { sev: 'preferred' }),
    R('material_profile', 'Material profile', 'material', { sev: 'preferred' }),
    R('nozzle_size', 'Nozzle size', 'select', { options: ['0.2', '0.4', '0.6', '0.8'], unit: 'mm', sev: 'preferred', default: '0.4' }),
    R('layer_height', 'Layer height', 'number', { unit: 'mm', sev: 'preferred', default: 0.2 }),
    R('support_preference', 'Support preference', 'select', { options: SUPPORT_OPTS, sev: 'preferred', default: 'Prefer no supports' }),
    R('multicolor_preference', 'Multicolor preference', 'select', { options: ['Single color', 'Multicolor allowed', 'Multicolor required'], sev: 'preferred' }),
    R('maximum_print_time', 'Maximum print time', 'number', { unit: 'hours', sev: 'preferred', default: 8 }),
  ],
  references: [
    R('reference_visible_in_preview', 'Reference visible in preview', 'toggle', { sev: 'preferred' }),
    R('reference_excluded_from_export', 'Reference excluded from export', 'toggle'),
    R('use_as_fit_cutout_reference', 'Use as fit/cutout reference', 'toggle'),
    R('use_as_negative_space_blocker', 'Use as negative-space blocker', 'toggle'),
    R('preserve_alignment', 'Preserve alignment', 'toggle'),
    R('derive_dimensions_from_reference', 'Derive dimensions from reference', 'toggle'),
  ],
  custom: [
    R('custom_note', 'Custom requirement', 'custom'),
  ],
};

const defFor = (category, type) => (REQUIREMENTS[category] || []).find(d => d.type === type) || null;
const labelOf = (pairs, value) => (pairs.find(p => p[0] === value) || [null, value])[1];

// --- searchable dropdown ---------------------------------------------------
function searchSelect(options, current, onChange, placeholder) {
  const wrap = el('div', 'ss');
  const input = el('input', 'field ss-input');
  input.type = 'text'; input.placeholder = placeholder || 'Search…'; input.autocomplete = 'off';
  input.setAttribute('role', 'combobox'); input.setAttribute('aria-expanded', 'false');
  const list = el('div', 'ss-list'); list.hidden = true; list.setAttribute('role', 'listbox');
  wrap.append(input, list);
  let value = current, active = -1, filtered = options.slice();
  const labelFor = v => (options.find(o => o[0] === v) || [null, ''])[1];
  const setDisplay = () => { input.value = labelFor(value); };
  const render = q => {
    const query = (q || '').toLowerCase();
    filtered = options.filter(o => o[1].toLowerCase().includes(query));
    list.replaceChildren();
    filtered.forEach((o, i) => {
      const item = el('div', 'ss-opt' + (o[0] === value ? ' selected' : '') + (i === active ? ' active' : ''), o[1]);
      item.setAttribute('role', 'option');
      item.addEventListener('mousedown', ev => { ev.preventDefault(); choose(o[0]); });
      list.appendChild(item);
    });
    if (!filtered.length) list.appendChild(el('div', 'ss-empty', 'No matches'));
  };
  const open = () => { list.hidden = false; input.setAttribute('aria-expanded', 'true'); active = filtered.findIndex(o => o[0] === value); render(''); input.select(); };
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); setDisplay(); };
  const choose = v => { value = v; setDisplay(); close(); onChange(v); };
  input.addEventListener('focus', open);
  input.addEventListener('input', () => { active = 0; render(input.value); });
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('keydown', ev => {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); active = Math.min(filtered.length - 1, active + 1); render(input.value); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); active = Math.max(0, active - 1); render(input.value); }
    else if (ev.key === 'Enter') { ev.preventDefault(); if (filtered[active]) choose(filtered[active][0]); }
    else if (ev.key === 'Escape') { close(); }
  });
  setDisplay();
  return { el: wrap, get: () => value, set: v => { value = v; setDisplay(); }, setOptions: o => { options = o; setDisplay(); } };
}

// --- value controls --------------------------------------------------------
function makeControl(def, value, onChange, ctx) {
  const wrap = el('div', 'req-value');
  const fire = () => onChange();
  const numInput = (v, step, ph) => { const i = el('input', 'field req-num'); i.type = 'number'; i.step = step || 'any'; if (v != null) i.value = v; if (ph) i.placeholder = ph; i.addEventListener('input', fire); return i; };
  const unit = u => el('span', 'req-unit', u);
  let read = () => ({});
  switch (def.control) {
    case 'dim3': {
      const v = value || {};
      const xs = numInput(v.x, 'any', 'W'), ys = numInput(v.y, 'any', 'D'), zs = numInput(v.z, 'any', 'H');
      wrap.append(xs, el('span', 'req-x', '×'), ys, el('span', 'req-x', '×'), zs, unit('mm'));
      read = () => ({ x: parseFloat(xs.value), y: parseFloat(ys.value), z: parseFloat(zs.value), unit: 'mm' });
      break;
    }
    case 'number': {
      const i = numInput(value?.n ?? def.default, 'any');
      wrap.append(i, unit(def.unit || 'mm'));
      read = () => ({ n: i.value === '' ? null : parseFloat(i.value), unit: def.unit || 'mm' });
      break;
    }
    case 'int': {
      const i = numInput(value?.n ?? def.default, '1'); i.min = '0'; i.classList.add('req-step');
      wrap.append(i);
      read = () => ({ n: i.value === '' ? null : parseInt(i.value, 10) });
      break;
    }
    case 'text': {
      const i = el('input', 'field'); i.type = 'text'; i.placeholder = def.placeholder || 'value'; if (value?.text) i.value = value.text; i.addEventListener('input', fire);
      wrap.append(i);
      read = () => ({ text: i.value.trim() });
      break;
    }
    case 'toggle': {
      const lbl = el('label', 'req-toggle');
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = value?.on !== false; cb.addEventListener('change', fire);
      lbl.append(cb, el('span', null, 'Required'));
      wrap.append(lbl);
      read = () => ({ on: cb.checked });
      break;
    }
    case 'select': {
      const opts = (def.options || []).map(o => [o, o + (def.unit ? ` ${def.unit}` : '')]);
      const ss = searchSelect(opts, value?.choice ?? def.default ?? (def.options?.[0]), fire, 'Choose…');
      wrap.append(ss.el);
      read = () => ({ choice: ss.get() });
      break;
    }
    case 'checkboxes': {
      const chosen = new Set(value?.items || []);
      const boxes = (def.options || []).map(o => {
        const lbl = el('label', 'req-check');
        const cb = el('input'); cb.type = 'checkbox'; cb.value = o; cb.checked = chosen.has(o); cb.addEventListener('change', fire);
        lbl.append(cb, el('span', null, o)); wrap.append(lbl); return cb;
      });
      read = () => ({ items: boxes.filter(b => b.checked).map(b => b.value) });
      break;
    }
    case 'chips': {
      const items = [...(value?.items || [])];
      const chipBox = el('div', 'req-chips');
      const renderChips = () => {
        chipBox.replaceChildren();
        items.forEach((it, i) => {
          const chip = el('span', 'req-chip', it);
          const x = el('button', 'req-chip-x', '×'); x.type = 'button';
          x.addEventListener('click', () => { items.splice(i, 1); renderChips(); fire(); });
          chip.append(x); chipBox.appendChild(chip);
        });
        const inp = el('input', 'req-chip-input'); inp.type = 'text'; inp.placeholder = items.length ? 'add…' : 'type + Enter';
        if (def.suggestions) { const dl = el('datalist'); dl.id = 'dl-' + uid(); def.suggestions.forEach(s => dl.appendChild(new Option(s, s))); inp.setAttribute('list', dl.id); chipBox.appendChild(dl); }
        inp.addEventListener('keydown', ev => {
          if (ev.key === 'Enter' && inp.value.trim()) { ev.preventDefault(); items.push(inp.value.trim()); renderChips(); fire(); chipBox.querySelector('.req-chip-input')?.focus(); }
          else if (ev.key === 'Backspace' && !inp.value && items.length) { items.pop(); renderChips(); fire(); }
        });
        inp.addEventListener('change', () => { if (inp.value.trim()) { items.push(inp.value.trim()); renderChips(); fire(); chipBox.querySelector('.req-chip-input')?.focus(); } });
        chipBox.appendChild(inp);
      };
      renderChips(); wrap.append(chipBox);
      read = () => ({ items: [...items] });
      break;
    }
    case 'printer': {
      const opts = (ctx.getProfiles() || []).map(p => [p.name, p.name]);
      const ss = searchSelect(opts.length ? opts : [['', 'No profiles']], value?.profile_name ?? (opts[0]?.[0] || ''), fire, 'Printer profile…');
      wrap.append(ss.el);
      read = () => ({ profile_name: ss.get() });
      break;
    }
    case 'material': {
      const mats = [...new Set((ctx.getProfiles() || []).map(p => p.material).filter(Boolean))];
      const opts = (mats.length ? mats : ['PLA', 'PETG', 'ABS', 'TPU']).map(m => [m, m]);
      const ss = searchSelect(opts, value?.material ?? opts[0][0], fire, 'Material…');
      wrap.append(ss.el);
      read = () => ({ material: ss.get() });
      break;
    }
    default: { // custom
      const i = el('input', 'field'); i.type = 'text'; i.placeholder = 'describe requirement'; if (value?.text) i.value = value.text; i.addEventListener('input', fire);
      wrap.append(i);
      read = () => ({ text: i.value.trim() });
    }
  }
  return { el: wrap, read };
}

// value summarizers (mirror backend summarize_value) ------------------------
export function summarizeValue(req) {
  const v = req.value || {};
  const d = defFor(req.category, req.type);
  if (d?.control === 'dim3' || (v.x != null && v.y != null && v.z != null)) return `${+v.x || 0} × ${+v.y || 0} × ${+v.z || 0} mm`;
  if (v.items?.length) return v.items.join(' + ');
  if (v.n != null && v.n !== '') return `${v.n}${d?.control === 'int' ? '' : ' ' + (v.unit || d?.unit || 'mm')}`.trim();
  if (v.choice) return v.choice + (d?.unit ? ` ${d.unit}` : '');
  if (v.text) return v.text;
  if (v.profile_name) return v.profile_name;
  if (v.material) return v.material;
  if (v.on === true) return 'Required';
  if (v.on === false) return 'Off';
  return '';
}
const reqLabel = req => (defFor(req.category, req.type)?.label) || req.label || req.type;
export function summarizeRequirement(req) { const s = summarizeValue(req); return s ? `${reqLabel(req)}: ${s}` : reqLabel(req); }

// --- presets ---------------------------------------------------------------
const mk = (category, type, value, severity) => ({ id: uid(), category, type, severity: severity || defFor(category, type)?.sev || 'hard_lock', label: defFor(category, type)?.label || type, value: value || {} });
export const PRESETS = {
  '': [],
  fidget: [
    mk('dimensions', 'maximum_overall_size', { x: 82, y: 56, z: 13, unit: 'mm' }),
    mk('identity', 'required_shape', { text: 'Connected 67' }),
    mk('text', 'required_text', { text: 'SIX SEVEN' }),
    mk('function', 'required_mechanisms', { items: ['Spinner', 'Captive slider'] }),
    mk('clearances', 'minimum_moving_clearance', { n: 0.35, unit: 'mm' }),
    mk('dimensions', 'minimum_wall_thickness', { n: 2.0, unit: 'mm' }),
    mk('parts', 'maximum_part_count', { n: 4 }),
    mk('export', 'allowed_export_parts', { items: ['Body', 'Spinner', 'Slider', 'Optional retainer'] }),
    mk('printability', 'no_floating_geometry', { on: true }, 'forbidden'),
    mk('printability', 'support_preference', { choice: 'Prefer no supports' }, 'preferred'),
  ],
  enclosure: [
    mk('function', 'required_opening_or_cavity', { text: 'port cutouts for the board' }),
    mk('parts', 'required_parts', { items: ['Body', 'Lid'] }),
    mk('dimensions', 'minimum_wall_thickness', { n: 2.0, unit: 'mm' }),
    mk('clearances', 'snap_fit_clearance', { n: 0.15, unit: 'mm' }),
    mk('printability', 'must_fit_build_volume', { on: true }),
    mk('printability', 'support_preference', { choice: 'Prefer no supports' }, 'preferred'),
  ],
  step_enclosure: [
    mk('references', 'use_as_fit_cutout_reference', { on: true }),
    mk('references', 'reference_excluded_from_export', { on: true }),
    mk('references', 'derive_dimensions_from_reference', { on: true }),
    mk('export', 'reference_geometry_excluded', { on: true }),
    mk('safety', 'no_reference_export_leakage', { on: true }),
    mk('dimensions', 'minimum_wall_thickness', { n: 2.0, unit: 'mm' }),
  ],
  snapbox: [
    mk('parts', 'required_parts', { items: ['Body', 'Lid'] }),
    mk('function', 'required_mechanisms', { items: ['Snap fit'] }),
    mk('clearances', 'snap_fit_clearance', { n: 0.15, unit: 'mm' }),
    mk('parts', 'no_glue_required', { on: true }),
    mk('dimensions', 'minimum_wall_thickness', { n: 2.0, unit: 'mm' }),
  ],
  slider: [
    mk('moving', 'required_moving_part', { items: ['Slider'] }),
    mk('moving', 'must_remain_captive', { items: ['Slider'] }),
    mk('moving', 'travel_distance', { n: 15, unit: 'mm' }),
    mk('clearances', 'slider_clearance', { n: 0.35, unit: 'mm' }),
    mk('moving', 'must_not_bind', { on: true }),
  ],
  rotating: [
    mk('moving', 'required_moving_part', { items: ['Rotating ring'] }),
    mk('moving', 'required_rotation', { n: 360, unit: 'deg' }),
    mk('clearances', 'rotational_clearance', { n: 0.4, unit: 'mm' }),
    mk('moving', 'must_not_bind', { on: true }),
    mk('moving', 'no_hardware_required', { on: true }),
  ],
  bracket: [
    mk('function', 'required_action', { text: 'wall-mount with two screw holes' }),
    mk('dimensions', 'minimum_wall_thickness', { n: 3.0, unit: 'mm' }),
    mk('printability', 'required_print_orientation', { choice: 'Flat on bed' }, 'preferred'),
    mk('printability', 'must_fit_build_volume', { on: true }),
  ],
  sign: [
    mk('text', 'required_text', { text: 'YOUR TEXT' }),
    mk('text', 'minimum_text_height', { n: 6, unit: 'mm' }),
    mk('text', 'minimum_emboss_engraving_depth', { n: 0.8, unit: 'mm' }),
    mk('printing', 'multicolor_preference', { choice: 'Multicolor required' }, 'preferred'),
    mk('export', 'required_export_formats', { items: ['3MF'] }),
  ],
  blank: [],
};
export const PRESET_LABELS = [
  ['', 'Requirement presets…'], ['fidget', 'Functional fidget toy'], ['enclosure', 'Electronics enclosure'],
  ['step_enclosure', 'STEP reference enclosure'], ['snapbox', 'Snap-fit box'], ['slider', 'Moving slider'],
  ['rotating', 'Rotating mechanism'], ['bracket', 'Wall-mount bracket'], ['sign', 'Multicolor sign'],
  ['blank', 'Custom blank'],
];

// --- spec extraction (client-side heuristics) ------------------------------
export function extractFromSpec(spec) {
  const out = [];
  const text = spec || '';
  let m;
  if ((m = text.match(/(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm/i)))
    out.push({ conf: 0.9, req: mk('dimensions', 'maximum_overall_size', { x: +m[1], y: +m[2], z: +m[3], unit: 'mm' }) });
  const quoted = [...text.matchAll(/"([^"]{1,40})"/g)].map(q => q[1]).filter(Boolean);
  const caps = [...text.matchAll(/\b([A-Z]{2,}(?:\s+[A-Z]{2,}){0,3})\b/g)].map(q => q[1]).filter(s => s.length >= 3 && !/^(STL|3MF|OBJ|GLB|ZIP|MM|USB|STEP|QA)$/.test(s));
  const phrase = quoted[0] || caps.find(c => c.includes(' ')) || null;
  if (phrase) out.push({ conf: quoted.length ? 0.85 : 0.55, req: mk('text', 'required_text', { text: phrase }) });
  const mech = ['Spinner', 'Slider', 'Hinge', 'Button', 'Gear', 'Rotating ring'].filter(x => new RegExp(x.split(' ')[0], 'i').test(text));
  if (mech.length) out.push({ conf: 0.7, req: mk('function', 'required_mechanisms', { items: mech }) });
  if (/captiv|captur|remain\s+in/i.test(text) && mech.length) out.push({ conf: 0.6, req: mk('moving', 'must_remain_captive', { items: mech }) });
  if ((m = text.match(/(\d+(?:\.\d+)?)\s*mm[^.]{0,20}?clearance/i)) || (m = text.match(/clearance[^.\d]{0,20}?(\d+(?:\.\d+)?)\s*mm/i)))
    out.push({ conf: 0.75, req: mk('clearances', 'minimum_moving_clearance', { n: +m[1], unit: 'mm' }) });
  if ((m = text.match(/(?:wall|thickness)[^.\d]{0,14}?(\d+(?:\.\d+)?)\s*mm/i)))
    out.push({ conf: 0.7, req: mk('dimensions', 'minimum_wall_thickness', { n: +m[1], unit: 'mm' }) });
  if ((m = text.match(/(\d+)\s*(?:printable\s+)?parts?\b/i)))
    out.push({ conf: 0.65, req: mk('parts', 'maximum_part_count', { n: +m[1] }) });
  if (/no\s+support|support-?free|without\s+support|prefer\s+no\s+support/i.test(text))
    out.push({ conf: 0.7, req: mk('printability', 'support_preference', { choice: 'Prefer no supports' }, 'preferred') });
  if (/no\s+float|floating\s+geometr|no\s+mid-?air/i.test(text))
    out.push({ conf: 0.7, req: mk('printability', 'no_floating_geometry', { on: true }, 'forbidden') });
  return out;
}

// --- the builder -----------------------------------------------------------
export class RequirementsBuilder {
  constructor(opts) {
    this.root = opts.root; this.jsonEl = opts.jsonEl; this.errorEl = opts.errorEl;
    this.summaryEl = opts.summaryEl; this.extractEl = opts.extractEl;
    this.onChange = opts.onChange || (() => {}); this.getProfiles = opts.getProfiles || (() => []);
    this.getSpec = opts.getSpec || (() => ''); this.draftKey = opts.draftKey || 'pf_lab_locked_requirements_draft';
    this.jsonMode = false; this.rows = [];
  }

  ctx() { return { getProfiles: this.getProfiles }; }

  addRow(req) {
    const data = req ? { ...req } : { id: uid(), category: 'dimensions', type: null, severity: 'hard_lock', value: {} };
    if (!data.id) data.id = uid();
    const row = el('div', 'req-row'); row.dataset.id = data.id;
    const catCell = el('div', 'req-cell');
    const typeCell = el('div', 'req-cell');
    const valueCell = el('div', 'req-cell req-value-cell');
    const sevCell = el('div', 'req-cell');
    const removeBtn = el('button', 'mini-button req-remove', '×'); removeBtn.type = 'button'; removeBtn.title = 'Remove requirement';

    const state = { data, control: null };
    const rebuildValue = () => {
      valueCell.replaceChildren();
      const d = defFor(state.data.category, state.data.type);
      if (!d) { valueCell.appendChild(el('span', 'req-hint', 'choose a requirement')); state.control = null; return; }
      state.control = makeControl(d, state.data.value, () => this.sync(), this.ctx());
      valueCell.appendChild(state.control.el);
    };
    const typeOptionsFor = c => (REQUIREMENTS[c] || []).map(d => [d.type, d.label]);
    const typeSelect = searchSelect(typeOptionsFor(data.category), data.type, v => {
      state.data.type = v; state.data.value = {};
      const d = defFor(state.data.category, v); if (d?.sev) { state.data.severity = d.sev; sevSel.value = d.sev; }
      rebuildValue(); this.sync();
    }, 'Requirement…');
    const catSelect = searchSelect(CATEGORIES, data.category, v => {
      state.data.category = v; state.data.type = null; state.data.value = {};
      typeSelect.setOptions(typeOptionsFor(v)); typeSelect.set(null);
      rebuildValue(); this.sync();
    }, 'Category…');
    const sevSel = el('select', 'field req-sev');
    for (const [val, lbl] of SEVERITIES) sevSel.appendChild(new Option(lbl, val));
    sevSel.value = data.severity || 'hard_lock';
    sevSel.title = SEVERITY_HELP[sevSel.value];
    sevSel.addEventListener('change', () => { state.data.severity = sevSel.value; sevSel.title = SEVERITY_HELP[sevSel.value]; this.sync(); });
    removeBtn.addEventListener('click', () => { this.rows = this.rows.filter(r => r !== controller); row.remove(); this.sync(); });

    catCell.appendChild(catSelect.el); typeCell.appendChild(typeSelect.el); sevCell.appendChild(sevSel);
    row.append(catCell, typeCell, valueCell, sevCell, removeBtn);
    this.root.appendChild(row);
    rebuildValue();
    const controller = {
      el: row,
      read: () => {
        if (!state.data.type) return null;
        return { id: state.data.id, category: state.data.category, type: state.data.type,
          label: defFor(state.data.category, state.data.type)?.label || state.data.type,
          severity: sevSel.value, value: state.control ? state.control.read() : {} };
      },
    };
    this.rows.push(controller);
    return controller;
  }

  clear() { this.rows = []; this.root.replaceChildren(); }

  setRequirements(list) {
    this.clear();
    const items = Array.isArray(list) ? list : [];
    if (!items.length) this.addRow();
    else items.forEach(r => this.addRow(r));
    this.sync();
  }

  applyPreset(key) { this.setRequirements((PRESETS[key] || []).map(r => ({ ...r, id: uid() }))); }

  guidedRequirements() { return this.rows.map(r => r.read()).filter(Boolean); }

  getRequirements() {
    if (this.jsonMode) { try { const v = JSON.parse(this.jsonEl.value || '[]'); return Array.isArray(v) ? v : []; } catch { return this.guidedRequirements(); } }
    return this.guidedRequirements();
  }

  setJsonMode(on) {
    if (on) { this.jsonEl.value = JSON.stringify(this.guidedRequirements(), null, 2); this.jsonMode = true; }
    else {
      try { const parsed = JSON.parse(this.jsonEl.value || '[]'); if (!Array.isArray(parsed)) throw new Error('Requirements JSON must be an array.'); this.jsonMode = false; this.setRequirements(parsed); this.setError(''); }
      catch (e) { this.setError('Invalid JSON: ' + e.message + ' — fix it or stay in JSON mode (your text is kept).'); return false; }
    }
    this.render(); return true;
  }

  sync() {
    if (!this.jsonMode) this.jsonEl.value = JSON.stringify(this.guidedRequirements(), null, 2);
    this.saveDraft(); this.render(); this.onChange();
  }

  setError(msg) { if (this.errorEl) this.errorEl.textContent = msg || ''; }

  // --- validation ----------------------------------------------------------
  validate() {
    const reqs = this.getRequirements();
    const missing = [], conflicts = [], duplicates = [];
    const seen = new Map();
    const num = t => { const r = reqs.find(x => x.type === t); return r ? (r.value?.n) : undefined; };
    for (const r of reqs) {
      const d = defFor(r.category, r.type); const v = r.value || {};
      let empty = false;
      if (!r.type) empty = true;
      else if (d?.control === 'dim3') empty = [v.x, v.y, v.z].some(n => n == null || n === '' || Number.isNaN(+n));
      else if (d?.control === 'number' || d?.control === 'int') empty = v.n == null || v.n === '' || Number.isNaN(+v.n);
      else if (d?.control === 'text' || d?.control === 'custom') empty = !(v.text && v.text.trim());
      else if (d?.control === 'chips' || d?.control === 'checkboxes') empty = !(v.items && v.items.length);
      else if (d?.control === 'select') empty = !v.choice;
      else if (d?.control === 'printer') empty = !v.profile_name;
      else if (d?.control === 'material') empty = !v.material;
      if (empty) missing.push(reqLabel(r));
      const key = r.type;
      if (seen.has(key)) duplicates.push(reqLabel(r)); else seen.set(key, r);
    }
    // conflicts
    const has = t => reqs.some(r => r.type === t);
    const supportChoices = reqs.filter(r => r.type === 'support_preference').map(r => r.value?.choice).filter(Boolean);
    if (supportChoices.some(c => /^No supports|Prefer no/.test(c)) && supportChoices.some(c => /Supports allowed|Any supports|Tree supports/.test(c)))
      conflicts.push('Support preference is both “no supports” and “supports allowed”.');
    if (has('single_piece_required')) {
      if (has('separate_parts_required')) conflicts.push('Single-piece required conflicts with Separate parts required.');
      const ex = num('exact_part_count'); if (ex != null && +ex > 1) conflicts.push(`Single-piece required conflicts with Exact part count ${ex}.`);
      const mx = num('maximum_part_count'); if (mx != null && +mx > 1) conflicts.push(`Single-piece required conflicts with Maximum part count ${mx}.`);
    }
    const ex = num('exact_part_count'), mx = num('maximum_part_count');
    if (ex != null && mx != null && +ex > +mx) conflicts.push(`Exact part count ${ex} exceeds Maximum part count ${mx}.`);
    if ((has('reference_excluded_from_export') || has('reference_geometry_excluded')) && has('reference_visible_in_preview')) {
      // visible-in-preview is fine with excluded-from-export; only flag the true contradiction:
    }
    if (has('reference_excluded_from_export') && reqs.some(r => r.type === 'allowed_export_parts' && (r.value?.items || []).some(i => /reference|ref\b/i.test(i))))
      conflicts.push('Reference excluded from export, but a reference part is in Allowed export parts.');
    const counts = {
      total: reqs.length,
      hard: reqs.filter(r => r.severity === 'hard_lock').length,
      forbidden: reqs.filter(r => r.severity === 'forbidden').length,
      preferred: reqs.filter(r => r.severity === 'preferred').length,
      conflicts: conflicts.length, missing: missing.length,
    };
    return { valid: conflicts.length === 0 && missing.length === 0, missing, conflicts, duplicates, counts, reqs };
  }

  render() {
    const { counts, conflicts, duplicates, missing, reqs } = this.validate();
    if (this.summaryEl) {
      this.summaryEl.replaceChildren();
      const stat = (label, n, cls) => { const s = el('span', 'req-stat' + (cls && n ? ' ' + cls : '')); s.append(el('strong', null, String(n)), el('small', null, label)); return s; };
      const stats = el('div', 'req-stats');
      stats.append(stat('total', counts.total), stat('hard locks', counts.hard),
        stat('preferred', counts.preferred), stat('conflicts', counts.conflicts, 'bad'), stat('missing', counts.missing, 'bad'));
      this.summaryEl.appendChild(stats);
      const chips = el('div', 'req-summary-chips');
      for (const r of reqs) {
        const chip = el('span', 'req-summary-chip sev-' + r.severity, summarizeRequirement(r));
        chips.appendChild(chip);
      }
      if (reqs.length) this.summaryEl.appendChild(chips);
      const notes = [];
      if (conflicts.length) notes.push(['conflict', 'Requirement conflict — resolve to start: ' + conflicts.join(' ')]);
      if (missing.length) notes.push(['missing', 'Missing values: ' + [...new Set(missing)].join(', ')]);
      if (duplicates.length) notes.push(['dup', 'Potential duplicate: ' + [...new Set(duplicates)].join(', ')]);
      for (const [k, msg] of notes) this.summaryEl.appendChild(el('div', 'req-note ' + k, msg));
    }
  }

  // --- draft persistence ---------------------------------------------------
  saveDraft() { try { localStorage.setItem(this.draftKey, JSON.stringify(this.guidedRequirements())); } catch {} }
  loadDraft() { try { const raw = localStorage.getItem(this.draftKey); if (raw) { const v = JSON.parse(raw); if (Array.isArray(v)) return v; } } catch {} return null; }
  clearDraft() { try { localStorage.removeItem(this.draftKey); } catch {} }

  // --- extract-from-spec review UI ----------------------------------------
  runExtract() {
    if (!this.extractEl) return;
    const proposals = extractFromSpec(this.getSpec());
    const existing = new Set(this.guidedRequirements().map(r => r.type));
    this.extractEl.replaceChildren();
    if (!proposals.length) { this.extractEl.appendChild(el('div', 'req-note missing', 'No structured requirements could be extracted from the specification.')); this.extractEl.hidden = false; return; }
    const head = el('div', 'req-extract-head');
    head.append(el('strong', null, `${proposals.length} proposed requirement${proposals.length === 1 ? '' : 's'} — review before applying`));
    const closeBtn = el('button', 'mini-button', '×'); closeBtn.type = 'button'; closeBtn.addEventListener('click', () => { this.extractEl.hidden = true; });
    head.appendChild(closeBtn);
    this.extractEl.appendChild(head);
    const checks = proposals.map(p => {
      const dup = existing.has(p.req.type);
      const line = el('label', 'req-extract-row');
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = !dup;
      const body = el('span');
      body.append(el('strong', null, summarizeRequirement(p.req)));
      const meta = el('small', null, ` — ${Math.round(p.conf * 100)}% confidence${dup ? ' · possible duplicate of an existing rule' : ''}`);
      body.appendChild(meta);
      line.append(cb, body); this.extractEl.appendChild(line);
      return { cb, req: p.req };
    });
    const actions = el('div', 'req-extract-actions');
    const apply = el('button', 'button', 'Add selected'); apply.type = 'button';
    apply.addEventListener('click', () => {
      const chosen = checks.filter(c => c.cb.checked).map(c => ({ ...c.req, id: uid() }));
      if (!chosen.length) { this.extractEl.hidden = true; return; }
      // append to guided rows (drop an empty leading row if present)
      const current = this.guidedRequirements();
      this.setRequirements([...current, ...chosen]);
      this.extractEl.hidden = true;
    });
    const cancel = el('button', 'button secondary', 'Cancel'); cancel.type = 'button';
    cancel.addEventListener('click', () => { this.extractEl.hidden = true; });
    actions.append(apply, cancel); this.extractEl.appendChild(actions);
    this.extractEl.hidden = false;
  }
}
