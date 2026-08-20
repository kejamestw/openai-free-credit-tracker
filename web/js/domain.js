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

  function costNoteKey(usage, costs) {
    if (usage && usage.list_price_estimate_incomplete === true) {
      return 'dashboard.cache_write_estimate_incomplete';
    }
    return costs && costs.error ? 'dashboard.costs_partial' : 'dashboard.cost_note';
  }

  const RESETTABLE_CONFIG_FIELDS = Object.freeze([
    'ui.language',
    'ui.open_browser_on_start',
    'network.request_timeout_seconds',
    'updates.channel',
    'updates.check_on_start',
    'history.retention_days',
    'monitoring.enabled',
    'monitoring.interval_seconds',
    'monitoring.freshness_threshold_seconds',
    'startup.enabled',
  ]);

  function pathValue(object, path) {
    return path.split('.').reduce((value, part) => value && value[part], object);
  }

  function setPathValue(object, path, value) {
    const parts = path.split('.');
    const leaf = parts.pop();
    const parent = parts.reduce((target, part) => target[part], object);
    parent[leaf] = value;
  }

  function configResetPreview(current, defaults) {
    if (!current || typeof current !== 'object' || !defaults || typeof defaults !== 'object') {
      return { candidate: null, changedFields: [] };
    }
    const candidate = JSON.parse(JSON.stringify(current));
    const changedFields = [];
    for (const path of RESETTABLE_CONFIG_FIELDS) {
      const defaultValue = pathValue(defaults, path);
      if (defaultValue === undefined) continue;
      if (JSON.stringify(pathValue(current, path)) !== JSON.stringify(defaultValue)) {
        changedFields.push(path);
      }
      setPathValue(candidate, path, defaultValue);
    }
    return { candidate, changedFields };
  }

  function historyDateRange(days, nowValue = new Date()) {
    if (!Number.isInteger(days) || days < 1 || days > 366) return null;
    const end = new Date(nowValue);
    if (!Number.isFinite(end.getTime())) return null;
    const start = new Date(end);
    start.setUTCDate(start.getUTCDate() - (days - 1));
    return {
      start: start.toISOString().slice(0, 10),
      end: end.toISOString().slice(0, 10),
    };
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

  const PROFILE_ID_PATTERN = /^prof_[0-9a-f]{32}$/;
  const ALERT_ID_PATTERN = /^alert_[A-Za-z0-9_-]{8,80}$/;
  const UTC_DAY_PATTERN = /^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])$/;
  const PROJECT_KEY_PATTERN = /^(?:all|unattributed|project-[0-9a-f]{24})$/;
  const NAVIGATION_RULES = Object.freeze({
    '/dashboard': Object.freeze({
      profile_id: PROFILE_ID_PATTERN,
      view: new Set(['summary', 'projects', 'history', 'alerts']),
      utc_day: UTC_DAY_PATTERN,
      project_key: PROJECT_KEY_PATTERN,
    }),
    '/profiles': Object.freeze({
      profile_id: PROFILE_ID_PATTERN,
      action: new Set(['select', 'edit']),
    }),
    '/settings': Object.freeze({ section: new Set(['general', 'monitoring', 'notifications']) }),
    '/alerts': Object.freeze({ profile_id: PROFILE_ID_PATTERN, alert_id: ALERT_ID_PATTERN }),
  });

  function navigationView(route, requestedView) {
    if (route === '/profiles') return 'profiles';
    if (route === '/settings') return 'settings';
    if (route === '/alerts') return 'alerts';
    if (requestedView === 'alerts') return 'alerts';
    if (requestedView === 'history' || requestedView === 'projects') return 'history';
    return 'dashboard';
  }

  function parseNavigationTarget(pathname, search = '', hash = '') {
    if (pathname === '/' && (!search || search === '?') && !hash) {
      return { view: 'dashboard', profileId: null, projectKey: null, utcDay: null };
    }
    if (typeof pathname !== 'string' || typeof search !== 'string' || typeof hash !== 'string') return null;
    if (hash || pathname.length + search.length > 2048) return null;
    if (!pathname.startsWith('/') || pathname.startsWith('//')) return null;
    if (/[\u0000-\u001f\u007f]/.test(pathname + search)) return null;
    if (/%(?![0-9A-Fa-f]{2})/.test(pathname + search)) return null;
    let route;
    try {
      route = decodeURIComponent(pathname);
    } catch (_error) {
      return null;
    }
    if (route.includes('\\') || `${route}/`.includes('/../') || `${route}/`.includes('/./')) return null;
    const rules = NAVIGATION_RULES[route];
    if (!rules) return null;

    const rawQuery = search.startsWith('?') ? search.slice(1) : search;
    const values = {};
    if (rawQuery) {
      const fields = rawQuery.split('&');
      if (fields.length > 8 || fields.some(field => !field || !field.includes('='))) return null;
      for (const [name, value] of new URLSearchParams(rawQuery)) {
        if (!(name in rules) || name in values || !value || value.length > 200) return null;
        const validator = rules[name];
        const valid = validator instanceof RegExp ? validator.test(value) : validator.has(value);
        if (!valid) return null;
        values[name] = value;
      }
      if (Object.keys(values).length !== fields.length) return null;
    }

    return {
      view: navigationView(route, values.view),
      profileId: values.profile_id || null,
      projectKey: values.project_key || null,
      utcDay: values.utc_day || null,
    };
  }

  return {
    configResetPreview,
    costNoteKey,
    escapeHTML,
    historyDateRange,
    localFetchFailureKind,
    parseNavigationTarget,
    queryOutcome,
    quotaProgress,
  };
});
