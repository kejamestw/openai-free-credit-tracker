const el = id => document.getElementById(id);
const fmt = value => Number(value || 0).toLocaleString('zh-TW');
const { escapeHTML, queryOutcome, quotaProgress } = globalThis.QuotaDomain;
const ADMIN_KEY_PATTERN = /^sk-admin-[A-Za-z0-9_-]{8,}$/;
let catalog;

function safeDomId(value) {
  return String(value).replace(/[^A-Za-z0-9_-]/g, '-');
}

function setState(state, message) {
  const status = el('status');
  status.dataset.state = state;
  status.textContent = message;
  document.body.dataset.queryState = state;
}

function level(pricing, rules) {
  const sample = (1000 * pricing.input + 1000 * pricing.output) / 1_000_000;
  if (sample >= rules.high_from) return ['高', 'high', sample];
  if (sample >= rules.low_below) return ['中', 'medium', sample];
  return ['低', 'low', sample];
}

function modelBadge(model, rules, groupId, index) {
  const [label, cls, sample] = level(model.pricing, rules);
  const modelId = escapeHTML(model.id);
  const tipId = `tip-${safeDomId(groupId)}-${index}`;
  return `<span class="badge" tabindex="0" aria-describedby="${tipId}">${modelId}<span class="tag ${cls}">${label}</span><span class="tip" id="${tipId}" role="tooltip"><b>${modelId}｜費用 ${label}</b><br>每 100 萬 Token<br>輸入：US$${model.pricing.input}<br>快取輸入：US$${model.pricing.cached_input}<br>輸出：US$${model.pricing.output}<small>1,000 輸入 + 1,000 輸出 ≈ US$${sample.toFixed(5)}<br>估算公式：(非快取輸入 × 輸入單價 + 快取輸入 × 快取單價 + 輸出 × 輸出單價) ÷ 1,000,000</small></span></span>`;
}

function quotaCard(groupId, group) {
  const domId = safeDomId(groupId);
  const label = escapeHTML(group.label);
  const quota = Number(group.daily_quota_tier_1_2 || 0);
  return `<article class="card"><div class="quota-header"><div><span class="quota-label">每日額度</span><h3>${label}</h3></div><b>${fmt(quota)} Token</b></div><div id="${domId}Pct" class="percent">0.0%</div><div id="${domId}Track" class="track" role="progressbar" aria-label="${label} 使用百分比" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="${domId}Bar" class="bar"></div></div><div class="stats"><span>已確認免費 <b id="${domId}Used">0</b></span><span id="${domId}Left">推估可用 ${fmt(quota)}</span></div><pre id="${domId}Usage">今日尚無已確認免費資料</pre></article>`;
}

function paint(groupId, group, usage) {
  const domId = safeDomId(groupId);
  const progress = quotaProgress(usage.total, group.daily_quota_tier_1_2);
  el(`${domId}Pct`).textContent = `${progress.displayPercent.toFixed(1)}%`;
  el(`${domId}Bar`).style.width = `${progress.barPercent}%`;
  el(`${domId}Track`).setAttribute('aria-valuenow', progress.barPercent.toFixed(1));
  el(`${domId}Used`).textContent = fmt(progress.used);
  el(`${domId}Left`).textContent = `推估可用 ${fmt(progress.remaining)}`;
  const lines = Object.entries(usage.models).map(([name, value]) => `${name}: ${fmt(value.total)} | input=${fmt(value.input)} | cached=${fmt(value.cached_input)} | output=${fmt(value.output)}`);
  el(`${domId}Usage`).textContent = lines.join('\n') || '今日尚無已確認免費資料';
}

function formatUtcWindow(start, end) {
  const startText = new Date(start * 1000).toISOString().replace('.000Z', 'Z');
  const endText = new Date(end * 1000).toISOString().replace('.000Z', 'Z');
  return `UTC 日界線：${startText} → ${endText}`;
}

async function readJSON(response) {
  try {
    return await response.json();
  } catch (_error) {
    return {};
  }
}

function renderCatalog() {
  el('quotaCards').innerHTML = Object.entries(catalog.groups)
    .map(([id, group]) => quotaCard(id, group))
    .join('');
  el('quotaCards').setAttribute('aria-busy', 'false');
  el('standardModels').innerHTML = catalog.groups.standard.models
    .map((model, index) => modelBadge(model, catalog.cost_levels, 'standard', index))
    .join('');
  el('miniModels').innerHTML = catalog.groups.mini.models
    .map((model, index) => modelBadge(model, catalog.cost_levels, 'mini', index))
    .join('');
  el('appVersion').textContent = catalog.version;
}

async function initialize() {
  setState('initial', '初始化本機模型目錄中…');
  const response = await fetch('/api/catalog', { cache: 'no-store', credentials: 'omit' });
  const data = await readJSON(response);
  if (!response.ok) throw new Error(data.error?.message || '無法載入模型目錄。');
  catalog = data;
  renderCatalog();
  setState('initial', '已就緒。輸入 Admin API Key 後即可查詢今日資料。');
}

el('toggle').addEventListener('click', () => {
  const showing = el('key').type === 'text';
  el('key').type = showing ? 'password' : 'text';
  el('toggle').textContent = showing ? '顯示 Key' : '隱藏 Key';
  el('toggle').setAttribute('aria-pressed', String(!showing));
});

el('queryForm').addEventListener('submit', async event => {
  event.preventDefault();
  const key = el('key').value.trim();
  if (!ADMIN_KEY_PATTERN.test(key)) {
    setState('failure', '請輸入格式正確、以 sk-admin- 開頭的 Admin API Key。');
    el('key').focus();
    return;
  }

  el('update').disabled = true;
  el('queryForm').setAttribute('aria-busy', 'true');
  setState('loading', '正在讀取 Usage 與 Costs API…');
  try {
    const response = await fetch('/api/data', {
      cache: 'no-store',
      credentials: 'omit',
      headers: { 'X-Admin-Key': key },
    });
    const data = await readJSON(response);
    if (!response.ok) {
      const error = new Error(data.error?.message || `本機服務回傳 HTTP ${response.status}`);
      error.requestId = data.error?.request_id;
      throw error;
    }

    Object.entries(catalog.groups).forEach(([id, group]) => paint(id, group, data.usage.groups[id]));
    el('listPrice').textContent = `US$${data.usage.list_price_estimate_usd.toFixed(4)}`;
    el('actualCost').textContent = data.costs.available ? `US$${data.costs.actual_usd.toFixed(4)}` : '無法取得';
    el('otherTokens').textContent = fmt(data.usage.other_usage.total);
    el('unpricedTokens').textContent = fmt(data.usage.unpriced_tokens);
    el('costNote').textContent = data.costs.error
      ? `Usage 已更新；${data.costs.error.message}`
      : 'Costs API 實際費用可能延遲，請以 OpenAI 帳務後台為準。';
    el('dataWindow').textContent = formatUtcWindow(data.usage.start, data.usage.end);
    el('requestId').textContent = data.request_id;
    el('console').textContent = JSON.stringify(data.usage.debug, null, 2);

    const outcome = queryOutcome(data.costs);
    if (outcome === 'partial') {
      setState('partial', '部分成功：免費用量已更新，但 Costs API 暫時無法取得。');
    } else {
      setState('success', '更新成功：Usage 與 Costs 資料皆已載入。');
    }
  } catch (error) {
    el('requestId').textContent = error.requestId || '未提供';
    el('console').textContent = '查詢失敗；請依上方訊息修正後重試。';
    setState('failure', `查詢失敗：${error.message}`);
  } finally {
    el('update').disabled = false;
    el('queryForm').setAttribute('aria-busy', 'false');
  }
});

initialize().catch(error => {
  el('update').disabled = true;
  el('quotaCards').setAttribute('aria-busy', 'false');
  setState('failure', `初始化失敗：${error.message}`);
});
