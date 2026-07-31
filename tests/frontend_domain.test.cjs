const test = require('node:test');
const assert = require('node:assert/strict');

const { escapeHTML, localFetchFailureKind, queryOutcome, quotaProgress } = require('../web/js/domain.js');

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
