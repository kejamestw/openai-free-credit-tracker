const el = id => document.getElementById(id);
const fmt = n => Number(n || 0).toLocaleString('zh-TW');
let catalog;

function level(pricing, rules) {
  const sample = (1000 * pricing.input + 1000 * pricing.output) / 1_000_000;
  if (sample >= rules.high_from) return ['高', 'high', sample];
  if (sample >= rules.low_below) return ['中', 'medium', sample];
  return ['低', 'low', sample];
}

function modelBadge(model, rules) {
  const [label, cls, sample] = level(model.pricing, rules);
  return `<span class="badge" tabindex="0">${model.id}<span class="tag ${cls}">${label}</span><span class="tip"><b>${model.id}｜費用 ${label}</b><br>每 100 萬 Token<br>輸入：US$${model.pricing.input}<br>快取輸入：US$${model.pricing.cached_input}<br>輸出：US$${model.pricing.output}<small>1,000 輸入 + 1,000 輸出 ≈ US$${sample.toFixed(5)}<br>公式：(輸入量 × 輸入單價 + 輸出量 × 輸出單價) ÷ 1,000,000</small></span></span>`;
}

function quotaCard(groupId, group) {
  return `<article class="card"><h2>${group.label} ${fmt(group.daily_quota_tier_1_2)}／日</h2><div id="${groupId}Pct" class="percent">0.0%</div><div class="track"><div id="${groupId}Bar" class="bar"></div></div><div class="stats"><span>已套用免費 <b id="${groupId}Used">0</b></span><span id="${groupId}Left">推估可用 ${fmt(group.daily_quota_tier_1_2)}</span></div><pre id="${groupId}Usage">今日尚無已確認免費資料</pre></article>`;
}

function paint(groupId, group, usage) {
  const quota = group.daily_quota_tier_1_2;
  const percent = usage.total / quota * 100;
  el(`${groupId}Pct`).textContent = `${percent.toFixed(1)}%`;
  el(`${groupId}Bar`).style.width = `${Math.min(100, percent)}%`;
  el(`${groupId}Used`).textContent = fmt(usage.total);
  el(`${groupId}Left`).textContent = `推估可用 ${fmt(Math.max(0, quota - usage.total))}`;
  const lines = Object.entries(usage.models).map(([name, value]) => `${name}: ${fmt(value.total)} | input=${fmt(value.input)} | output=${fmt(value.output)}`);
  el(`${groupId}Usage`).textContent = lines.join('\n') || '今日尚無已確認免費資料';
}

async function initialize() {
  catalog = await fetch('/api/catalog').then(r => r.json());
  el('quotaCards').innerHTML = Object.entries(catalog.groups).map(([id, group]) => quotaCard(id, group)).join('');
  el('standardModels').innerHTML = catalog.groups.standard.models.map(m => modelBadge(m, catalog.cost_levels)).join('');
  el('miniModels').innerHTML = catalog.groups.mini.models.map(m => modelBadge(m, catalog.cost_levels)).join('');
}

el('toggle').onclick = () => el('key').type = el('key').type === 'password' ? 'text' : 'password';
el('update').onclick = async () => {
  const key = el('key').value.trim();
  if (!key.startsWith('sk-admin-')) return el('status').textContent = '請輸入 sk-admin 開頭的 Admin API Key';
  el('status').textContent = '查詢中…';
  try {
    const response = await fetch('/api/data', {headers: {'X-Admin-Key': key}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || `HTTP ${response.status}`);
    Object.entries(catalog.groups).forEach(([id, group]) => paint(id, group, data.usage.groups[id]));
    el('listPrice').textContent = `US$${data.usage.list_price_estimate_usd.toFixed(4)}`;
    el('actualCost').textContent = data.costs.available ? `US$${data.costs.actual_usd.toFixed(4)}` : '無法取得';
    el('otherTokens').textContent = fmt(data.usage.other_usage.total);
    el('costNote').textContent = data.costs.error ? data.costs.error.message : '實際成本可能延遲更新，請以 OpenAI 帳務後台為準。';
    el('console').textContent = JSON.stringify(data.usage.debug, null, 2);
    el('status').textContent = '更新成功';
  } catch (error) {
    el('status').textContent = `失敗：${error.message}`;
  }
};
initialize().catch(error => el('status').textContent = `初始化失敗：${error.message}`);
