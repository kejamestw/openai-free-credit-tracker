const el = id => document.getElementById(id);
const {
  configResetPreview,
  costNoteKey,
  historyDateRange,
  localFetchFailureKind,
  parseNavigationTarget,
  queryOutcome,
  quotaProgress,
} = globalThis.QuotaDomain;
const ADMIN_KEY_PATTERN = /^sk-admin-[A-Za-z0-9_-]{8,}$/;

let catalog = null;
let configDocument = null;
let defaultConfigDocument = null;
let configResetCandidate = null;
let messages = {};
let currentLocale = 'zh-TW';
let activeProfileId = null;
let updateSnapshot = null;
let updatePollTimer = null;
let retentionPreview = null;

function nestedValue(object, path) {
  return path.split('.').reduce((value, part) => value && value[part], object);
}

function t(path, variables = {}) {
  const template = nestedValue(messages, path) || path;
  return String(template).replace(/\{([^}]+)\}/g, (_match, name) => String(variables[name] ?? ''));
}

function translatePage() {
  document.documentElement.lang = currentLocale === 'en' ? 'en' : 'zh-Hant';
  document.querySelectorAll('[data-i18n]').forEach(node => {
    const translation = nestedValue(messages, node.dataset.i18n);
    if (translation) node.textContent = translation;
  });
  document.title = t('app.name');
  const active = document.querySelector('[data-view-target].active');
  if (active) el('viewTitle').textContent = t(`navigation.${active.dataset.viewTarget}`);
}

async function loadLocale(locale) {
  const supported = locale === 'en' ? 'en' : 'zh-TW';
  try {
    const response = await fetch(`/api/v1/locales/${supported}`, {
      cache: 'no-store',
      credentials: 'omit',
    });
    if (!response.ok) throw new Error(`locale HTTP ${response.status}`);
    messages = await response.json();
    currentLocale = supported;
  } catch (error) {
    if (supported !== 'zh-TW') return loadLocale('zh-TW');
    console.warn('Locale resources are unavailable.', error);
  }
  el('localeSelector').value = currentLocale;
  el('settingLanguage').value = currentLocale;
  translatePage();
}

function numberFormat(value, options = {}) {
  return new Intl.NumberFormat(currentLocale, options).format(Number(value || 0));
}

function currencyFormat(value) {
  return new Intl.NumberFormat(currentLocale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  }).format(Number(value || 0));
}

function setState(state, message) {
  el('globalStatus').dataset.state = state;
  el('status').textContent = message;
  document.body.dataset.queryState = state;
}

function safeDomId(value) {
  return String(value).replace(/[^A-Za-z0-9_-]/g, '-');
}

function pricingLevel(pricing, rules) {
  const sample = (1000 * Number(pricing.input) + 1000 * Number(pricing.output)) / 1_000_000;
  if (sample >= Number(rules.high_from)) return ['dashboard.cost_high', 'high', sample];
  if (sample >= Number(rules.low_below)) return ['dashboard.cost_medium', 'medium', sample];
  return ['dashboard.cost_low', 'low', sample];
}

function modelBadge(model, rules) {
  const [labelKey, className, sample] = pricingLevel(model.pricing, rules);
  const badge = document.createElement('span');
  badge.className = 'badge';
  badge.tabIndex = 0;
  badge.append(document.createTextNode(model.id));

  const tag = document.createElement('span');
  tag.className = `tag ${className}`;
  tag.textContent = t(labelKey);
  badge.append(tag);

  const tip = document.createElement('span');
  tip.className = 'tip';
  tip.setAttribute('role', 'tooltip');
  const heading = document.createElement('b');
  heading.textContent = `${model.id} · ${t(labelKey)}`;
  const details = document.createElement('small');
  details.textContent = t('dashboard.price_tooltip', {
    input: model.pricing.input,
    cached: model.pricing.cached_input,
    output: model.pricing.output,
    sample: sample.toFixed(5),
  });
  tip.append(heading, details);
  badge.append(tip);
  return badge;
}

function createQuotaCard(groupId, group) {
  const domId = safeDomId(groupId);
  const quota = Number(group.daily_quota_tier_1_2 || 0);
  const article = document.createElement('article');
  article.className = 'card';

  const header = document.createElement('div');
  header.className = 'quota-header';
  const title = document.createElement('div');
  const label = document.createElement('span');
  label.className = 'quota-label';
  label.textContent = t('dashboard.daily_quota');
  const heading = document.createElement('h3');
  heading.textContent = group.label;
  title.append(label, heading);
  const total = document.createElement('b');
  total.textContent = `${numberFormat(quota)} Token`;
  header.append(title, total);

  const percent = document.createElement('div');
  percent.id = `${domId}Pct`;
  percent.className = 'percent';
  percent.textContent = '0.0%';
  const track = document.createElement('div');
  track.id = `${domId}Track`;
  track.className = 'track';
  track.setAttribute('role', 'progressbar');
  track.setAttribute('aria-label', t('dashboard.quota_progress', { group: group.label }));
  track.setAttribute('aria-valuemin', '0');
  track.setAttribute('aria-valuemax', '100');
  track.setAttribute('aria-valuenow', '0');
  const bar = document.createElement('div');
  bar.id = `${domId}Bar`;
  bar.className = 'bar';
  track.append(bar);

  const stats = document.createElement('div');
  stats.className = 'stats';
  const used = document.createElement('span');
  used.append(document.createTextNode(`${t('dashboard.used')} `));
  const usedValue = document.createElement('b');
  usedValue.id = `${domId}Used`;
  usedValue.textContent = '0';
  used.append(usedValue);
  const remaining = document.createElement('span');
  remaining.id = `${domId}Left`;
  remaining.textContent = t('dashboard.remaining', { count: numberFormat(quota) });
  stats.append(used, remaining);

  const usage = document.createElement('pre');
  usage.id = `${domId}Usage`;
  usage.className = 'usage-lines';
  usage.textContent = t('dashboard.no_model_usage');
  article.append(header, percent, track, stats, usage);
  return article;
}

function paintQuota(groupId, group, usage) {
  const domId = safeDomId(groupId);
  const progress = quotaProgress(usage.total, group.daily_quota_tier_1_2);
  el(`${domId}Pct`).textContent = `${progress.displayPercent.toFixed(1)}%`;
  el(`${domId}Bar`).style.width = `${progress.barPercent}%`;
  el(`${domId}Track`).setAttribute('aria-valuenow', progress.barPercent.toFixed(1));
  el(`${domId}Used`).textContent = numberFormat(progress.used);
  el(`${domId}Left`).textContent = t('dashboard.remaining', { count: numberFormat(progress.remaining) });
  const lines = Object.entries(usage.models || {}).map(([name, value]) =>
    `${name}: ${numberFormat(value.total)} · input=${numberFormat(value.input)} · cached=${numberFormat(value.cached_input)} · output=${numberFormat(value.output)}`,
  );
  el(`${domId}Usage`).textContent = lines.join('\n') || t('dashboard.no_model_usage');
}

function renderCatalog() {
  const quotaCards = el('quotaCards');
  quotaCards.replaceChildren(...Object.entries(catalog.groups).map(([id, group]) => createQuotaCard(id, group)));
  quotaCards.setAttribute('aria-busy', 'false');
  for (const [groupId, containerId] of [['standard', 'standardModels'], ['mini', 'miniModels']]) {
    const group = catalog.groups[groupId];
    const container = el(containerId);
    container.replaceChildren(...(group ? group.models.map(model => modelBadge(model, catalog.cost_levels)) : []));
  }
  el('appVersion').textContent = catalog.version;
}

function formatUtcWindow(start, end) {
  const startText = new Date(start * 1000).toISOString().replace('.000Z', 'Z');
  const endText = new Date(end * 1000).toISOString().replace('.000Z', 'Z');
  return `${startText} → ${endText}`;
}

function describeFetchFailure(error) {
  const kind = localFetchFailureKind(error, globalThis.location?.protocol);
  if (kind === 'file') return t('errors.open_from_server');
  if (kind === 'network') return t('errors.local_server_unavailable');
  if (error.code && nestedValue(messages, `errors.${error.code}`)) return t(`errors.${error.code}`);
  return error.safeMessage || t('errors.unknown');
}

async function readJSON(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', credentials: 'omit', ...options });
  const data = await readJSON(response);
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.code = data.error?.code;
    error.safeMessage = data.error?.message;
    error.requestId = data.error?.request_id;
    error.status = response.status;
    throw error;
  }
  return data;
}

async function queryUsage(event) {
  event.preventDefault();
  const key = el('key').value.trim();
  if (!ADMIN_KEY_PATTERN.test(key)) {
    setState('failure', t('credential.invalid'));
    el('key').focus();
    return;
  }

  el('update').disabled = true;
  el('queryForm').setAttribute('aria-busy', 'true');
  setState('loading', t('dashboard.loading'));
  try {
    const data = await fetchJSON('/api/v1/data', { headers: { 'X-Admin-Key': key } });
    Object.entries(catalog.groups).forEach(([id, group]) => {
      paintQuota(id, group, data.usage.groups[id] || { total: 0, models: {} });
    });
    el('listPrice').textContent = currencyFormat(data.usage.list_price_estimate_usd);
    el('actualCost').textContent = data.costs.available ? currencyFormat(data.costs.actual_usd) : t('dashboard.unavailable');
    el('otherTokens').textContent = numberFormat(data.usage.other_usage.total);
    el('unpricedTokens').textContent = numberFormat(data.usage.unpriced_tokens);
    el('costNote').textContent = t(costNoteKey(data.usage, data.costs));
    el('dataWindow').textContent = formatUtcWindow(data.usage.start, data.usage.end);
    el('requestId').textContent = data.request_id;
    el('console').textContent = JSON.stringify({ sources: data.sources, debug: data.usage.debug }, null, 2);
    setState(queryOutcome(data.costs), t(data.costs.available ? 'dashboard.success' : 'dashboard.partial'));
  } catch (error) {
    el('requestId').textContent = error.requestId || '—';
    el('console').textContent = t('dashboard.failure_diagnostics');
    setState('failure', describeFetchFailure(error));
  } finally {
    el('key').value = '';
    el('update').disabled = false;
    el('queryForm').removeAttribute('aria-busy');
  }
}

function showView(viewName) {
  document.querySelectorAll('[data-view]').forEach(view => {
    const active = view.dataset.view === viewName;
    view.hidden = !active;
    view.classList.toggle('active', active);
  });
  document.querySelectorAll('[data-view-target]').forEach(button => {
    const active = button.dataset.viewTarget === viewName;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  el('viewTitle').textContent = t(`navigation.${viewName}`);
  if (viewName === 'history') void loadHistory();
  if (viewName === 'profiles') void loadProfiles();
  if (viewName === 'alerts') void loadAlerts();
  if (viewName === 'settings' && !configDocument) void loadConfig();
}

function setDefaultHistoryRange() {
  const range = historyDateRange(30);
  el('historyStart').value = range.start;
  el('historyEnd').value = range.end;
}

function selectedHistoryRange() {
  const start = Date.parse(`${el('historyStart').value}T00:00:00Z`);
  const inclusiveEnd = Date.parse(`${el('historyEnd').value}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(inclusiveEnd) || inclusiveEnd < start) {
    throw new Error(t('history.invalid_range'));
  }
  return { startUtc: Math.floor(start / 1000), endUtc: Math.floor(inclusiveEnd / 1000) + 86400 };
}

function setHistoryRange(days) {
  const range = historyDateRange(days);
  if (!range) return;
  el('historyStart').value = range.start;
  el('historyEnd').value = range.end;
  void loadHistory();
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function svgElement(name, attributes = {}) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function renderHistoryTrend(records) {
  const svg = el('historyTrend');
  const values = Array.isArray(records) ? records : [];
  const complete = values.filter(record => record.completeness === 'complete').length;
  const partial = values.filter(record => record.completeness === 'partial').length;
  const missing = values.filter(record => record.completeness === 'missing').length;
  const summary = values.length
    ? t('history.trend_summary', { days: values.length, complete, partial, missing })
    : t('history.trend_empty');
  el('trendSummary').textContent = summary;
  const title = svgElement('title', { id: 'trendSvgTitle' });
  title.textContent = t('history.trend_title');
  const description = svgElement('desc', { id: 'trendSvgDescription' });
  description.textContent = summary;
  svg.replaceChildren(title, description);

  const legend = el('trendLegend');
  legend.replaceChildren();
  if (!values.length) return;
  const groups = Object.keys(catalog?.groups || {});
  const palette = ['#3478ef', '#147655', '#8a4fb3', '#a05a00'];
  const numeric = [];
  values.forEach(record => groups.forEach(group => {
    const value = record.groups?.[group];
    if (Number.isFinite(value) && record.completeness !== 'missing') numeric.push(Number(value));
  }));
  const maximum = Math.max(1, ...numeric);
  const left = 42;
  const right = 940;
  const top = 18;
  const bottom = 224;
  const x = index => left + (values.length === 1 ? 0 : (index / (values.length - 1)) * (right - left));
  const y = value => bottom - (Number(value) / maximum) * (bottom - top);
  svg.append(
    svgElement('line', { x1: left, y1: bottom, x2: right, y2: bottom, class: 'trend-axis' }),
    svgElement('line', { x1: left, y1: top, x2: left, y2: bottom, class: 'trend-axis' }),
  );

  groups.forEach((group, groupIndex) => {
    let segment = [];
    const flush = () => {
      if (segment.length > 1) {
        const path = svgElement('path', {
          d: segment.map((point, index) => `${index ? 'L' : 'M'}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(' '),
          class: `trend-${safeDomId(group)}`,
        });
        path.style.stroke = palette[groupIndex % palette.length];
        path.style.fill = 'none';
        path.style.strokeWidth = '3';
        svg.append(path);
      } else if (segment.length === 1) {
        const point = svgElement('circle', {
          cx: segment[0][0], cy: segment[0][1], r: 3,
        });
        point.style.fill = palette[groupIndex % palette.length];
        svg.append(point);
      }
      segment = [];
    };
    values.forEach((record, index) => {
      const amount = record.groups?.[group];
      if (record.completeness === 'complete' && Number.isFinite(amount)) {
        segment.push([x(index), y(amount)]);
        return;
      }
      flush();
      if (record.completeness === 'partial' && Number.isFinite(amount)) {
        const marker = svgElement('circle', {
          cx: x(index), cy: y(amount), r: 4.5, class: 'trend-partial',
        });
        const markerTitle = svgElement('title');
        markerTitle.textContent = t('history.partial_point', {
          day: record.day, group, count: numberFormat(amount),
        });
        marker.append(markerTitle);
        svg.append(marker);
      }
    });
    flush();
    const item = document.createElement('li');
    item.style.setProperty('--legend-color', palette[groupIndex % palette.length]);
    item.textContent = group;
    legend.append(item);
  });
  values.forEach((record, index) => {
    if (record.completeness !== 'missing') return;
    const group = svgElement('g');
    const position = x(index);
    group.append(
      svgElement('line', { x1: position - 3, y1: bottom - 3, x2: position + 3, y2: bottom + 3, class: 'trend-missing' }),
      svgElement('line', { x1: position - 3, y1: bottom + 3, x2: position + 3, y2: bottom - 3, class: 'trend-missing' }),
    );
    const markerTitle = svgElement('title');
    markerTitle.textContent = t('history.missing_point', { day: record.day });
    group.append(markerTitle);
    svg.append(group);
  });
  [
    ['history.partial', '#bd6c00'],
    ['history.missing', '#c4323e'],
  ].forEach(([key, color]) => {
    const item = document.createElement('li');
    item.style.setProperty('--legend-color', color);
    item.textContent = t(key);
    legend.append(item);
  });
}

async function loadHistory(event) {
  if (event) event.preventDefault();
  el('historyStatus').textContent = t('common.loading');
  try {
    const { startUtc, endUtc } = selectedHistoryRange();
    const params = new URLSearchParams({ start_utc: startUtc, end_utc: endUtc });
    if (activeProfileId) params.set('profile_id', activeProfileId);
    if (el('historyProject').value) params.set('project_key', el('historyProject').value);
    const data = await fetchJSON(`/api/v1/history?${params}`);
    renderHistoryTrend(data.records || []);
    const projectLabel = el('historyProject').selectedOptions[0]?.textContent || t('history.all_projects');
    const rows = (data.records || []).flatMap(record => {
      const groups = record.groups ? Object.entries(record.groups) : [];
      if (!groups.length) return [{ ...record, group_id: '—', group_tokens: null }];
      return groups.map(([groupId, tokens]) => ({ ...record, group_id: groupId, group_tokens: tokens }));
    });
    el('historyRows').replaceChildren(...rows.map(row => {
      const tr = document.createElement('tr');
      const values = [row.day, projectLabel, row.group_id, row.group_tokens === null ? '—' : numberFormat(row.group_tokens), t(`history.${row.completeness}`)];
      values.forEach(value => { const td = document.createElement('td'); td.textContent = value ?? '—'; tr.append(td); });
      return tr;
    }));
    el('historyStatus').textContent = rows.length ? t('history.loaded', { count: rows.length }) : t('history.empty');
  } catch (error) {
    renderHistoryTrend([]);
    el('historyStatus').textContent = error.status === 404 ? t('errors.capability_unavailable') : describeFetchFailure(error);
  }
}

function clearRetentionPreview() {
  retentionPreview = null;
  el('applyRetention').hidden = true;
  el('retentionStatus').textContent = t('retention.default_safe');
}

async function previewRetention() {
  const retentionDays = Number(el('retentionDays').value);
  if (!Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 3650) {
    el('retentionStatus').textContent = t('retention.invalid_days');
    return;
  }
  el('previewRetention').disabled = true;
  el('applyRetention').hidden = true;
  el('retentionStatus').textContent = t('common.loading');
  try {
    const body = { retention_days: retentionDays };
    if (activeProfileId) body.profile_id = activeProfileId;
    retentionPreview = await fetchJSON('/api/v1/operations/retention/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    el('retentionStatus').textContent = t('retention.preview_result', {
      count: numberFormat(retentionPreview.row_count), cutoff: retentionPreview.cutoff,
    });
    el('applyRetention').hidden = retentionPreview.row_count < 1;
  } catch (error) {
    retentionPreview = null;
    el('retentionStatus').textContent = describeFetchFailure(error);
  } finally {
    el('previewRetention').disabled = false;
  }
}

async function applyRetention() {
  const preview = retentionPreview;
  if (!preview) return;
  if (!window.confirm(t('retention.confirm', {
    count: numberFormat(preview.row_count), cutoff: preview.cutoff,
  }))) return;
  el('applyRetention').disabled = true;
  try {
    const result = await fetchJSON('/api/v1/operations/retention/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preview_token: preview.preview_token, confirm: true }),
    });
    retentionPreview = null;
    el('applyRetention').hidden = true;
    el('retentionStatus').textContent = t('retention.applied', {
      count: numberFormat(result.deleted_rows), cutoff: result.cutoff,
    });
    await loadHistory();
  } catch (error) {
    retentionPreview = null;
    el('applyRetention').hidden = true;
    el('retentionStatus').textContent = error.code === 'retention_preview_stale'
      ? t('retention.stale')
      : describeFetchFailure(error);
  } finally {
    el('applyRetention').disabled = false;
  }
}

async function downloadExport(format) {
  try {
    const { startUtc, endUtc } = selectedHistoryRange();
    const body = { format, start_utc: startUtc, end_utc: endUtc, project_id_policy: 'mask' };
    if (activeProfileId) body.profile_id = activeProfileId;
    if (el('historyProject').value) body.project_key = el('historyProject').value;
    const response = await fetch('/api/v1/export', {
      method: 'POST', cache: 'no-store', credentials: 'omit',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!response.ok) {
      const payload = await readJSON(response);
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      error.code = payload.error?.code;
      error.safeMessage = payload.error?.message;
      throw error;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    const disposition = response.headers.get('Content-Disposition') || '';
    link.download = disposition.match(/filename="([^"]+)"/)?.[1] || `openai-credit-history.${format}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    el('historyStatus').textContent = error.status === 404 ? t('errors.capability_unavailable') : describeFetchFailure(error);
  }
}

async function loadProjects() {
  const params = activeProfileId ? `?profile_id=${encodeURIComponent(activeProfileId)}` : '';
  try {
    const data = await fetchJSON(`/api/v1/projects${params}`);
    const options = (data.projects || []).map(project => {
      const option = document.createElement('option');
      option.value = project.project_key;
      option.textContent = project.display_name;
      return option;
    });
    const allHistory = document.createElement('option');
    allHistory.value = '';
    allHistory.textContent = t('history.all_projects');
    el('historyProject').replaceChildren(allHistory, ...options.map(option => option.cloneNode(true)));
    const allAlert = document.createElement('option');
    allAlert.value = 'all';
    allAlert.textContent = t('history.all_projects');
    el('alertProject').replaceChildren(allAlert, ...options);
  } catch (_error) {
    // Profiles can still be used before the first project sync.
  }
}

async function loadProfiles() {
  try {
    const data = await fetchJSON('/api/v1/profiles');
    const profiles = data.profiles || [];
    activeProfileId = data.active_profile_id || activeProfileId;
    const selector = el('profileSelector');
    const profileOptions = profiles.map(profile => {
      const option = document.createElement('option');
      option.value = profile.profile_id;
      option.textContent = profile.display_name;
      option.selected = profile.profile_id === activeProfileId;
      return option;
    });
    if (!activeProfileId && profiles.length) {
      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = t('profile.select');
      placeholder.selected = true;
      placeholder.disabled = true;
      profileOptions.unshift(placeholder);
    }
    selector.replaceChildren(...profileOptions);
    selector.disabled = profiles.length === 0;
    el('profileList').replaceChildren(...profiles.map(profile => {
      const article = document.createElement('article');
      article.className = 'card';
      const row = document.createElement('div');
      row.className = 'profile-row';
      const summary = document.createElement('div');
      const title = document.createElement('h3');
      title.textContent = profile.display_name;
      const detail = document.createElement('p');
      detail.className = 'help';
      detail.textContent = `${profile.enabled ? t('profile.enabled') : t('profile.disabled')} · ${profile.credential_configured ? t('credential.saved') : t('credential.missing')}`;
      summary.append(title, detail);
      const actions = document.createElement('div');
      actions.className = 'actions';
      if (!profile.active && profile.enabled) {
        const activate = document.createElement('button');
        activate.className = 'secondary-button';
        activate.type = 'button';
        activate.textContent = t('profile.activate');
        activate.addEventListener('click', () => void activateProfile(profile.profile_id));
        actions.append(activate);
      }
      const toggle = document.createElement('button');
      toggle.className = 'secondary-button';
      toggle.type = 'button';
      toggle.textContent = t(profile.enabled ? 'common.disable' : 'common.enable');
      toggle.addEventListener('click', () => void updateProfile(profile.profile_id, { enabled: !profile.enabled }));
      const rename = document.createElement('button');
      rename.className = 'secondary-button';
      rename.type = 'button';
      rename.textContent = t('common.rename');
      rename.addEventListener('click', () => void renameProfile(profile));
      actions.append(rename);
      if (profile.credential_configured) {
        const credential = document.createElement('button');
        credential.className = 'secondary-button';
        credential.type = 'button';
        credential.textContent = t('credential.remove');
        credential.addEventListener('click', () => void deleteCredential(profile));
        actions.append(credential);
      }
      const remove = document.createElement('button');
      remove.className = 'danger-button';
      remove.type = 'button';
      remove.textContent = t('common.delete');
      remove.addEventListener('click', () => void deleteProfile(profile));
      actions.append(toggle, remove);
      row.append(summary, actions);
      article.append(row);
      return article;
    }));
    if (!profiles.length) {
      const empty = document.createElement('article');
      empty.className = 'card empty-state';
      empty.textContent = t('profiles.empty');
      el('profileList').append(empty);
    }
    await loadProjects();
    return profiles;
  } catch (error) {
    el('addProfile').disabled = error.status === 404;
    return [];
  }
}

async function applyInitialNavigation(profiles) {
  const navigation = parseNavigationTarget(
    globalThis.location?.pathname || '/',
    globalThis.location?.search || '',
    globalThis.location?.hash || '',
  );
  if (!navigation) return;

  if (navigation.profileId) {
    const selected = profiles.find(profile => profile.profile_id === navigation.profileId);
    if (!selected) return;
    if (activeProfileId !== selected.profile_id) {
      const activated = await activateProfile(navigation.profileId);
      if (!activated || activeProfileId !== navigation.profileId) return;
    } else {
      el('profileSelector').value = selected.profile_id;
    }
  }

  if (navigation.projectKey) {
    const value = navigation.projectKey === 'all' ? '' : navigation.projectKey;
    const optionExists = Array.from(el('historyProject').options)
      .some(option => option.value === value);
    if (optionExists) el('historyProject').value = value;
  }
  if (navigation.utcDay) {
    el('historyStart').value = navigation.utcDay;
    el('historyEnd').value = navigation.utcDay;
  }
  showView(navigation.view);
}

async function createProfile(event) {
  event.preventDefault();
  const name = el('profileName').value.trim();
  const adminKey = el('profileKey').value.trim();
  if (!name || !ADMIN_KEY_PATTERN.test(adminKey)) {
    el('profileFormStatus').textContent = t('credential.invalid');
    return;
  }
  el('saveProfile').disabled = true;
  el('profileFormStatus').textContent = t('common.loading');
  try {
    const profile = await fetchJSON('/api/v1/profiles', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: name, admin_key: adminKey }),
    });
    await activateProfile(profile.profile_id);
    el('profileDialog').close();
    el('profileForm').reset();
    await loadProfiles();
  } catch (error) {
    el('profileFormStatus').textContent = describeFetchFailure(error);
  } finally {
    el('profileKey').value = '';
    el('saveProfile').disabled = false;
  }
}

async function activateProfile(profileId) {
  try {
    await fetchJSON(`/api/v1/profiles/${encodeURIComponent(profileId)}/activate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    activeProfileId = profileId;
    clearRetentionPreview();
    await Promise.all([loadProfiles(), loadConfig()]);
    return true;
  } catch (error) {
    setState('failure', describeFetchFailure(error));
    return false;
  }
}

async function updateProfile(profileId, fields) {
  try {
    await fetchJSON(`/api/v1/profiles/${encodeURIComponent(profileId)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields),
    });
    await loadProfiles();
  } catch (error) {
    setState('failure', describeFetchFailure(error));
  }
}

async function renameProfile(profile) {
  const name = globalThis.prompt(t('profile.rename_prompt'), profile.display_name);
  if (name === null || !name.trim() || name.trim() === profile.display_name) return;
  await updateProfile(profile.profile_id, { display_name: name.trim() });
}

async function deleteProfile(profile) {
  const credentialScope = profile.credential_configured
    ? t('profile.delete_scope_credential')
    : t('profile.delete_scope_no_credential');
  if (!globalThis.confirm(t('profile.delete_scope_confirmation', {
    name: profile.display_name,
    credential: credentialScope,
  }))) return;
  if (!globalThis.confirm(t('profile.delete_final_confirmation', { name: profile.display_name }))) return;
  try {
    await fetchJSON(`/api/v1/profiles/${encodeURIComponent(profile.profile_id)}`, { method: 'DELETE' });
    await loadProfiles();
  } catch (error) {
    setState('failure', describeFetchFailure(error));
  }
}

async function deleteCredential(profile) {
  if (!globalThis.confirm(t('credential.remove_confirmation', { name: profile.display_name }))) return;
  try {
    await fetchJSON(`/api/v1/profiles/${encodeURIComponent(profile.profile_id)}/credential`, { method: 'DELETE' });
    await loadProfiles();
  } catch (error) {
    setState('failure', describeFetchFailure(error));
  }
}

async function loadAlerts() {
  const params = activeProfileId ? `?profile_id=${encodeURIComponent(activeProfileId)}` : '';
  try {
    const data = await fetchJSON(`/api/v1/alerts${params}`);
    const rules = data.rules || [];
    el('alertList').replaceChildren(...rules.map(rule => {
      const article = document.createElement('article');
      article.className = 'card';
      const title = document.createElement('h3');
      title.textContent = t('alerts.threshold_label', { percent: rule.threshold_percent });
      const detail = document.createElement('p');
      detail.className = 'help';
      detail.textContent = `${rule.group_id} · ${rule.enabled ? t('common.enable') : t('common.disable')}`;
      const remove = document.createElement('button');
      remove.className = 'danger-button';
      remove.type = 'button';
      remove.textContent = t('common.delete');
      remove.addEventListener('click', () => void deleteAlert(rule.rule_id));
      article.append(title, detail, remove);
      return article;
    }));
  } catch (error) {
    el('addAlert').disabled = error.status === 404;
  }
  await loadNotificationHistory();
}

function notificationScope(record) {
  if (record.event_kind === 'quota_threshold_test') return t('notification.scope_system');
  const project = record.project_key === 'all'
    ? t('history.all_projects')
    : t('notification.selected_project');
  return `${record.group_id} · ${project}`;
}

async function loadNotificationHistory() {
  const testButton = el('testNotification');
  if (!activeProfileId) {
    testButton.disabled = true;
    el('notificationHistoryRows').replaceChildren();
    el('notificationHistoryStatus').textContent = t('errors.no_active_profile');
    return;
  }
  testButton.disabled = false;
  const params = new URLSearchParams({ profile_id: activeProfileId, limit: '100' });
  try {
    const data = await fetchJSON(`/api/v1/alerts/history?${params}`);
    const records = data.records || [];
    el('notificationHistoryRows').replaceChildren(...records.map(record => {
      const row = document.createElement('tr');
      const kind = record.event_kind === 'quota_threshold_test'
        ? t('notification.kind_test')
        : t('notification.kind_threshold');
      const status = nestedValue(messages, `notification.status_${record.delivery_status}`)
        || t('notification.status_unknown');
      const values = [
        new Date(record.occurred_at).toLocaleString(currentLocale),
        kind,
        notificationScope(record),
        status,
      ];
      values.forEach(value => {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      });
      return row;
    }));
    el('notificationHistoryStatus').textContent = records.length
      ? t('notification.history_loaded', { count: records.length })
      : t('notification.history_empty');
  } catch (error) {
    el('notificationHistoryStatus').textContent = describeFetchFailure(error);
  }
}

async function testNotification() {
  if (!activeProfileId) return setState('warning', t('errors.no_active_profile'));
  const button = el('testNotification');
  button.disabled = true;
  el('notificationHistoryStatus').textContent = t('common.loading');
  try {
    await fetchJSON('/api/v1/notifications/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: activeProfileId }),
    });
    el('notificationHistoryStatus').textContent = t('notification.test_sent');
    await loadNotificationHistory();
  } catch (error) {
    el('notificationHistoryStatus').textContent = describeFetchFailure(error);
  } finally {
    button.disabled = false;
  }
}

async function saveAlert(event) {
  event.preventDefault();
  el('saveAlert').disabled = true;
  try {
    await fetchJSON('/api/v1/alerts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profile_id: activeProfileId,
        group_id: el('alertGroup').value,
        threshold_percent: Number(el('alertThreshold').value),
        project_key: el('alertProject').value,
        enabled: true,
      }),
    });
    el('alertDialog').close();
    await loadAlerts();
  } catch (error) {
    el('alertFormStatus').textContent = describeFetchFailure(error);
  } finally {
    el('saveAlert').disabled = false;
  }
}

async function deleteAlert(ruleId) {
  try {
    const suffix = activeProfileId ? `?profile_id=${encodeURIComponent(activeProfileId)}` : '';
    await fetchJSON(`/api/v1/alerts/${encodeURIComponent(ruleId)}${suffix}`, { method: 'DELETE' });
    await loadAlerts();
  } catch (error) {
    setState('failure', describeFetchFailure(error));
  }
}

function fillConfig(response) {
  configDocument = response.config;
  defaultConfigDocument = response.defaults || null;
  configResetCandidate = null;
  el('applyConfigDefaults').hidden = true;
  el('configResetStatus').textContent = t('settings.reset_safe_scope');
  const config = response.config;
  el('settingLanguage').value = config.ui.language;
  el('openBrowser').checked = config.ui.open_browser_on_start;
  el('requestTimeout').value = config.network.request_timeout_seconds;
  el('monitoringEnabled').checked = config.monitoring.enabled;
  el('monitoringInterval').value = config.monitoring.interval_seconds;
  el('freshnessThreshold').value = config.monitoring.freshness_threshold_seconds;
  el('startupEnabled').checked = config.startup.enabled;
  el('updateChannel').value = config.updates.channel;
  el('checkOnStart').checked = config.updates.check_on_start;
  el('retentionDays').value = config.history.retention_days ?? '';
  el('configPath').textContent = response.config_path;
  el('configSource').textContent = response.load_source;
  el('configWarning').hidden = !response.warning;
  el('configWarning').textContent = response.warning || '';
}

async function loadConfig() {
  try {
    fillConfig(await fetchJSON('/api/v1/config'));
  } catch (error) {
    el('settingsStatus').textContent = describeFetchFailure(error);
    el('saveSettings').disabled = true;
  }
}

async function saveConfig(event) {
  event.preventDefault();
  if (!configDocument) return;
  const interval = Number(el('monitoringInterval').value);
  const freshness = Number(el('freshnessThreshold').value);
  if (interval < 300 || freshness < interval) {
    el('settingsStatus').textContent = t('settings.invalid_monitoring');
    return;
  }
  configDocument.ui.language = el('settingLanguage').value;
  configDocument.ui.open_browser_on_start = el('openBrowser').checked;
  configDocument.network.request_timeout_seconds = Number(el('requestTimeout').value);
  configDocument.monitoring.enabled = el('monitoringEnabled').checked;
  configDocument.monitoring.interval_seconds = interval;
  configDocument.monitoring.freshness_threshold_seconds = freshness;
  configDocument.startup.enabled = el('startupEnabled').checked;
  configDocument.updates.channel = el('updateChannel').value;
  configDocument.updates.check_on_start = el('checkOnStart').checked;
  configDocument.history.retention_days = el('retentionDays').value ? Number(el('retentionDays').value) : null;
  el('saveSettings').disabled = true;
  el('settingsStatus').textContent = t('common.loading');
  try {
    const response = await fetchJSON('/api/v1/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(configDocument),
    });
    fillConfig(response);
    await loadLocale(configDocument.ui.language);
    el('settingsStatus').textContent = response.restart_required
      ? t('settings.saved_restart', { fields: response.restart_required_fields.join(', ') })
      : t('settings.saved');
  } catch (error) {
    el('settingsStatus').textContent = describeFetchFailure(error);
  } finally {
    el('saveSettings').disabled = false;
  }
}

function clearConfigResetPreview() {
  configResetCandidate = null;
  el('applyConfigDefaults').hidden = true;
  el('configResetStatus').textContent = t('settings.reset_safe_scope');
}

function previewConfigDefaults() {
  if (!configDocument || !defaultConfigDocument) {
    el('configResetStatus').textContent = t('settings.reset_unavailable');
    return;
  }
  const preview = configResetPreview(configDocument, defaultConfigDocument);
  configResetCandidate = preview.candidate;
  if (!preview.changedFields.length) {
    el('applyConfigDefaults').hidden = true;
    el('configResetStatus').textContent = t('settings.reset_already_default');
    return;
  }
  el('applyConfigDefaults').hidden = false;
  el('configResetStatus').textContent = t('settings.reset_preview', {
    fields: preview.changedFields.join(', '),
  });
}

async function applyConfigDefaults() {
  if (!configResetCandidate) return;
  if (!globalThis.confirm(t('settings.reset_confirmation'))) return;
  const candidate = configResetCandidate;
  el('applyConfigDefaults').disabled = true;
  el('configResetStatus').textContent = t('common.loading');
  try {
    const response = await fetchJSON('/api/v1/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(candidate),
    });
    fillConfig(response);
    await loadLocale(configDocument.ui.language);
    el('configResetStatus').textContent = t('settings.reset_applied');
  } catch (error) {
    el('configResetStatus').textContent = describeFetchFailure(error);
  } finally {
    el('applyConfigDefaults').disabled = false;
  }
}

async function checkForUpdate() {
  el('checkUpdate').disabled = true;
  el('updateStatus').textContent = t('update.checking');
  try {
    const data = await fetchJSON('/api/v1/update/check', { method: 'POST' });
    if (data.available) {
      await loadUpdateStatus();
    } else {
      el('updateStatus').textContent = t('update.current');
    }
  } catch (error) {
    el('updateStatus').textContent = error.status === 503 ? t('update.not_configured') : describeFetchFailure(error);
  } finally {
    el('checkUpdate').disabled = false;
  }
}

function safeReleaseNotesUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch (_error) {
    return null;
  }
}

function updateStateKey(state) {
  return String(state || 'idle').replaceAll('-', '_');
}

function renderUpdateStatus(snapshot) {
  updateSnapshot = snapshot;
  const state = snapshot?.state || 'idle';
  const version = snapshot?.version || '';
  let message = t(`update.state.${updateStateKey(state)}`, { version });
  if (snapshot?.critical) message += ` ${t('update.critical')}`;
  if (snapshot?.last_error_code) {
    message += ` ${t('update.error_code', { code: snapshot.last_error_code })}`;
  }
  if (snapshot && snapshot.installation_available === false) {
    message += ` ${t('update.installation_unavailable')}`;
  }
  el('updateStatus').textContent = message;

  const progress = snapshot?.progress || {};
  const showProgress = Number(progress.total_bytes) > 0 && !['idle', 'available'].includes(state);
  el('updateProgressWrap').hidden = !showProgress;
  el('updateProgress').value = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  el('updateProgressText').textContent = t('update.progress', {
    percent: el('updateProgress').value,
  });

  el('downloadUpdate').hidden = !(snapshot?.can_consent_download || snapshot?.can_download);
  el('installUpdate').hidden = !(snapshot?.can_consent_install || snapshot?.can_install);
  el('resumeUpdate').hidden = !snapshot?.can_resume;
  const busy = Boolean(snapshot?.operation);
  el('downloadUpdate').disabled = busy;
  el('installUpdate').disabled = busy;
  el('resumeUpdate').disabled = busy;

  const notes = safeReleaseNotesUrl(snapshot?.release_notes_url);
  el('releaseNotes').hidden = !notes;
  if (notes) el('releaseNotes').setAttribute('href', notes);
  else el('releaseNotes').removeAttribute('href');

  if (updatePollTimer !== null) window.clearTimeout(updatePollTimer);
  updatePollTimer = null;
  if (busy || ['downloading', 'installing', 'health-check'].includes(state)) {
    updatePollTimer = window.setTimeout(() => void loadUpdateStatus(true), 750);
  }
}

async function loadUpdateStatus(silent = false) {
  try {
    const snapshot = await fetchJSON('/api/v1/update/status');
    renderUpdateStatus(snapshot);
    return snapshot;
  } catch (error) {
    if (updatePollTimer !== null) window.clearTimeout(updatePollTimer);
    updatePollTimer = null;
    updateSnapshot = null;
    el('downloadUpdate').hidden = true;
    el('installUpdate').hidden = true;
    el('resumeUpdate').hidden = true;
    el('releaseNotes').hidden = true;
    el('releaseNotes').removeAttribute('href');
    el('updateProgressWrap').hidden = true;
    el('updateStatus').textContent = error.status === 503
      ? t('update.not_configured')
      : describeFetchFailure(error);
    return null;
  }
}

async function postUpdateAction(action, body = {}) {
  return fetchJSON(`/api/v1/update/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function downloadUpdate() {
  const snapshot = updateSnapshot;
  if (!snapshot?.version) return;
  el('downloadUpdate').disabled = true;
  try {
    if (snapshot.can_consent_download) {
      if (!window.confirm(t('update.confirm_download', { version: snapshot.version }))) return;
      await postUpdateAction('consent-download', { version: snapshot.version, confirm: true });
    }
    renderUpdateStatus(await postUpdateAction('download'));
  } catch (error) {
    el('updateStatus').textContent = describeFetchFailure(error);
  } finally {
    el('downloadUpdate').disabled = false;
  }
}

async function installUpdate() {
  const snapshot = updateSnapshot;
  if (!snapshot?.version) return;
  el('installUpdate').disabled = true;
  try {
    if (snapshot.can_consent_install) {
      if (!window.confirm(t('update.confirm_install', { version: snapshot.version }))) return;
      await postUpdateAction('consent-install', { version: snapshot.version, confirm: true });
    }
    renderUpdateStatus(await postUpdateAction('install'));
  } catch (error) {
    el('updateStatus').textContent = describeFetchFailure(error);
  } finally {
    el('installUpdate').disabled = false;
  }
}

async function resumeUpdate() {
  if (!window.confirm(t('update.confirm_resume'))) return;
  el('resumeUpdate').disabled = true;
  try {
    renderUpdateStatus(await postUpdateAction('resume'));
  } catch (error) {
    el('updateStatus').textContent = describeFetchFailure(error);
  } finally {
    el('resumeUpdate').disabled = false;
  }
}

async function syncNow() {
  if (!activeProfileId) {
    setState('warning', t('errors.no_active_profile'));
    showView('profiles');
    return;
  }
  el('syncNow').disabled = true;
  setState('loading', t('status.syncing'));
  try {
    const result = await fetchJSON('/api/v1/sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_id: activeProfileId, days: 30, resume: true }),
    });
    setState(result.status === 'completed' ? 'success' : 'partial', t('history.sync_complete'));
    await Promise.all([loadHistory(), loadProjects()]);
  } catch (error) {
    setState('failure', describeFetchFailure(error));
  } finally {
    el('syncNow').disabled = false;
  }
}

async function checkIntegrity() {
  el('integrityCheck').disabled = true;
  el('operationsStatus').textContent = t('common.loading');
  try {
    const result = await fetchJSON('/api/v1/operations/integrity?full=true');
    el('operationsStatus').textContent = t(result.ok ? 'operations.integrity_ok' : 'operations.integrity_failed');
  } catch (error) {
    el('operationsStatus').textContent = describeFetchFailure(error);
  } finally {
    el('integrityCheck').disabled = false;
  }
}

async function createBackup() {
  el('createBackup').disabled = true;
  el('operationsStatus').textContent = t('common.loading');
  try {
    const result = await fetchJSON('/api/v1/operations/backup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    el('operationsStatus').textContent = t('operations.backup_created', { name: result.backup_name });
  } catch (error) {
    el('operationsStatus').textContent = describeFetchFailure(error);
  } finally {
    el('createBackup').disabled = false;
  }
}

function bindEvents() {
  document.querySelectorAll('[data-view-target]').forEach(button => button.addEventListener('click', () => showView(button.dataset.viewTarget)));
  el('localeSelector').addEventListener('change', event => void loadLocale(event.target.value));
  el('queryForm').addEventListener('submit', queryUsage);
  el('toggle').addEventListener('click', () => {
    const showing = el('key').type === 'text';
    el('key').type = showing ? 'password' : 'text';
    el('toggle').textContent = t(showing ? 'dashboard.show_key' : 'dashboard.hide_key');
    el('toggle').setAttribute('aria-pressed', String(!showing));
  });
  el('historyForm').addEventListener('submit', loadHistory);
  el('historyRange7').addEventListener('click', () => setHistoryRange(7));
  el('historyRange30').addEventListener('click', () => setHistoryRange(30));
  el('historyRange90').addEventListener('click', () => setHistoryRange(90));
  el('historyRange365').addEventListener('click', () => setHistoryRange(365));
  el('exportCsv').addEventListener('click', () => void downloadExport('csv'));
  el('exportJson').addEventListener('click', () => void downloadExport('json'));
  el('settingsForm').addEventListener('submit', saveConfig);
  el('settingsForm').addEventListener('input', clearConfigResetPreview);
  el('previewConfigDefaults').addEventListener('click', previewConfigDefaults);
  el('applyConfigDefaults').addEventListener('click', applyConfigDefaults);
  el('checkUpdate').addEventListener('click', checkForUpdate);
  el('downloadUpdate').addEventListener('click', downloadUpdate);
  el('installUpdate').addEventListener('click', installUpdate);
  el('resumeUpdate').addEventListener('click', resumeUpdate);
  el('syncNow').addEventListener('click', syncNow);
  el('integrityCheck').addEventListener('click', checkIntegrity);
  el('createBackup').addEventListener('click', createBackup);
  el('testNotification').addEventListener('click', testNotification);
  el('previewRetention').addEventListener('click', previewRetention);
  el('applyRetention').addEventListener('click', applyRetention);
  el('retentionDays').addEventListener('input', clearRetentionPreview);
  el('addProfile').addEventListener('click', () => el('profileDialog').showModal());
  el('profileForm').addEventListener('submit', createProfile);
  el('addAlert').addEventListener('click', () => {
    if (!activeProfileId) return setState('warning', t('errors.no_active_profile'));
    el('alertDialog').showModal();
  });
  el('alertForm').addEventListener('submit', saveAlert);
  document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => el(button.dataset.closeDialog).close()));
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  }));
  el('profileSelector').addEventListener('change', event => {
    if (event.target.value) void activateProfile(event.target.value);
  });
}

async function initialize() {
  bindEvents();
  setDefaultHistoryRange();
  renderHistoryTrend([]);
  await loadLocale('zh-TW');
  setState('initial', t('dashboard.ready'));
  try {
    const [catalogResponse, _config, profiles] = await Promise.all([
      fetchJSON('/api/v1/catalog'),
      loadConfig(),
      loadProfiles(),
    ]);
    catalog = catalogResponse;
    renderCatalog();
    if (configDocument?.ui?.language && configDocument.ui.language !== currentLocale) {
      await loadLocale(configDocument.ui.language);
      renderCatalog();
    }
    await applyInitialNavigation(profiles);
    await loadUpdateStatus(true);
  } catch (error) {
    el('update').disabled = true;
    el('quotaCards').setAttribute('aria-busy', 'false');
    setState('failure', describeFetchFailure(error));
  }
}

void initialize();
