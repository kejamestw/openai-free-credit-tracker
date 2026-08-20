const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  configResetPreview,
  costNoteKey,
  escapeHTML,
  historyDateRange,
  localFetchFailureKind,
  parseNavigationTarget,
  queryOutcome,
  quotaProgress,
} = require('../web/js/domain.js');

test('cache-write fixture always receives the incomplete estimate warning', () => {
  const fixture = JSON.parse(fs.readFileSync(
    path.join(__dirname, 'fixtures', 'frontend_usage_cache_write.json'),
    'utf8',
  ));
  assert(fixture.usage.input_cache_write_tokens > 0);
  assert.equal(
    costNoteKey(fixture.usage, fixture.costs),
    'dashboard.cache_write_estimate_incomplete',
  );
  assert.equal(costNoteKey({}, { error: true }), 'dashboard.costs_partial');
  assert.equal(costNoteKey({}, { available: true }), 'dashboard.cost_note');
});

test('history range presets use inclusive UTC calendar days', () => {
  const now = new Date('2026-08-19T23:45:00Z');
  assert.deepEqual(historyDateRange(7, now), { start: '2026-08-13', end: '2026-08-19' });
  assert.deepEqual(historyDateRange(90, now), { start: '2026-05-22', end: '2026-08-19' });
  assert.equal(historyDateRange(0, now), null);
});

test('config default preview resets only editable non-sensitive fields', () => {
  const current = {
    schema_version: 1,
    ui: { language: 'en', open_browser_on_start: false, future_ui: true },
    network: { request_timeout_seconds: 120 },
    updates: { channel: 'beta', check_on_start: false },
    history: { retention_days: 90 },
    monitoring: { enabled: true, interval_seconds: 300, freshness_threshold_seconds: 600 },
    profiles: { active_profile_id: `prof_${'a'.repeat(32)}` },
    startup: { enabled: true },
    future_root: { preserved: true },
  };
  const defaults = {
    schema_version: 1,
    ui: { language: 'zh-TW', open_browser_on_start: true },
    network: { request_timeout_seconds: 45 },
    updates: { channel: 'stable', check_on_start: true },
    history: { retention_days: null },
    monitoring: { enabled: false, interval_seconds: 900, freshness_threshold_seconds: 1800 },
    profiles: { active_profile_id: null },
    startup: { enabled: false },
  };

  const preview = configResetPreview(current, defaults);
  assert.equal(preview.candidate.ui.language, 'zh-TW');
  assert.equal(preview.candidate.profiles.active_profile_id, current.profiles.active_profile_id);
  assert.deepEqual(preview.candidate.future_root, { preserved: true });
  assert.equal(preview.candidate.ui.future_ui, true);
  assert(preview.changedFields.includes('ui.language'));
  assert(!preview.changedFields.includes('profiles.active_profile_id'));
});

test('quota progress handles zero quota without invalid values', () => {
  assert.deepEqual(quotaProgress(25, 0), {
    used: 25,
    quota: 0,
    displayPercent: 0,
    barPercent: 0,
    remaining: 0,
  });
});

test('quota progress handles exactly and above 100 percent', () => {
  assert.deepEqual(quotaProgress(100, 100), {
    used: 100,
    quota: 100,
    displayPercent: 100,
    barPercent: 100,
    remaining: 0,
  });
  assert.deepEqual(quotaProgress(150, 100), {
    used: 150,
    quota: 100,
    displayPercent: 150,
    barPercent: 100,
    remaining: 0,
  });
});

test('quota progress clamps negative and non-numeric input', () => {
  assert.deepEqual(quotaProgress(-5, 100), {
    used: 0,
    quota: 100,
    displayPercent: 0,
    barPercent: 0,
    remaining: 100,
  });
  assert.equal(quotaProgress('bad', 100).used, 0);
});

test('query outcome distinguishes success and partial success', () => {
  assert.equal(queryOutcome({ available: true }), 'success');
  assert.equal(queryOutcome({ available: false }), 'partial');
});

test('catalog labels are escaped before HTML rendering', () => {
  assert.equal(escapeHTML('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(escapeHTML('A&B"'), 'A&amp;B&quot;');
});

test('local fetch failures are categorized for actionable UI messages', () => {
  assert.equal(localFetchFailureKind(new Error('Failed to fetch'), 'http:'), 'network');
  assert.equal(localFetchFailureKind(new Error('Load failed'), 'https:'), 'network');
  assert.equal(localFetchFailureKind(new Error('ignored'), 'file:'), 'file');
  assert.equal(localFetchFailureKind(new Error('OpenAI rejected the key'), 'http:'), 'unknown');
});

test('validated navigation maps dashboard views and bounded filters', () => {
  const profileId = `prof_${'a'.repeat(32)}`;
  const projectKey = `project-${'b'.repeat(24)}`;
  assert.deepEqual(
    parseNavigationTarget(
      '/dashboard',
      `?profile_id=${profileId}&view=history&utc_day=2026-08-19&project_key=${projectKey}`,
    ),
    {
      view: 'history',
      profileId,
      projectKey,
      utcDay: '2026-08-19',
    },
  );
  assert.equal(parseNavigationTarget('/dashboard', '?view=projects').view, 'history');
  assert.equal(parseNavigationTarget('/dash%62oard', '?view=projects').view, 'history');
  assert.equal(parseNavigationTarget('/dashboard', '?view=summary').view, 'dashboard');
  assert.equal(parseNavigationTarget('/alerts', `?profile_id=${profileId}`).view, 'alerts');
  assert.equal(parseNavigationTarget('/settings', '?section=notifications').view, 'settings');
});

test('navigation parser rejects values outside the backend deep-link allowlist', () => {
  const profileId = `prof_${'a'.repeat(32)}`;
  for (const [path, search, hash] of [
    ['/dashboard', '?view=settings'],
    ['/dashboard', '?view=alerts&view=history'],
    ['/dashboard', '?view=alerts&next=https%3A%2F%2Fevil.invalid'],
    ['/dashboard', '?profile_id=not-a-profile'],
    ['/dashboard', '?project_key=raw-project-id'],
    ['/dashboard', '?view=%ZZ'],
    ['/dashboard', '?view'],
    ['/dashboard', '?view=alerts', '#unexpected'],
    ['/dashboard', `?profile_id=${profileId}&utc_day=19-08-2026`],
    ['//evil.invalid/dashboard', '?view=alerts'],
  ]) {
    assert.equal(parseNavigationTarget(path, search, hash), null);
  }
});
