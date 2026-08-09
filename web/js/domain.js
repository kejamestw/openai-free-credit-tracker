(function attachDomain(root, factory) {
  const domain = factory();
  if (typeof module === 'object' && module.exports) module.exports = domain;
  root.QuotaDomain = domain;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createDomain() {
  function safeNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function quotaProgress(usedValue, quotaValue) {
    const used = Math.max(0, safeNumber(usedValue));
    const quota = Math.max(0, safeNumber(quotaValue));
    const displayPercent = quota > 0 ? used / quota * 100 : 0;
    return {
      used,
      quota,
      displayPercent,
      barPercent: Math.min(100, Math.max(0, displayPercent)),
      remaining: Math.max(0, quota - used),
    };
  }

  function queryOutcome(costs) {
    return costs && costs.available ? 'success' : 'partial';
  }

  function escapeHTML(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function localFetchFailureKind(error, protocol) {
    const message = String(error && error.message ? error.message : error || '');
    if (protocol === 'file:') return 'file';
    if (/failed to fetch|networkerror|load failed|fetch failed/i.test(message)) return 'network';
    return 'unknown';
  }

  return { escapeHTML, localFetchFailureKind, queryOutcome, quotaProgress };
});
