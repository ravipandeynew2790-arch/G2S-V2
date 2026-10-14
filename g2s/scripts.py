"""JavaScript snippets injected into pages by the engine."""

# ---------------------------------------------------------------------------
# Init script - runs in every frame BEFORE page scripts.
#   * accelerates setTimeout / setInterval (skips client side wait timers)
#   * lets us fake "user left the page to look at an ad" (visibility / focus)
#   * hooks window.turnstile so we can grab sitekey + callback of widgets
# __FACTOR__ and __NOACCEL__ are replaced from Python.
# ---------------------------------------------------------------------------
INIT_SCRIPT = r"""
(() => {
  try {
    if (window.__g2sInit) return;
    window.__g2sInit = true;
    const host = location.hostname || '';
    // never touch Cloudflare's own challenge frames
    if (/(^|\.)challenges\.cloudflare\.com$/.test(host)) return;

    // Decide at the first <script>: never patch anything on Cloudflare challenge pages.
    const install = () => {
    try {
      document.documentElement && document.documentElement.removeAttribute('webdriver');
      const origGetAttr = Element.prototype.getAttribute;
      Element.prototype.getAttribute = function(name) {
        if (String(name).toLowerCase() === 'webdriver') return null;
        return origGetAttr.apply(this, arguments);
      };
    } catch (e) {}
    try {
      const safeSetup = function(opts) {
        if (!opts) return;
        const form = document.getElementById(opts.formId || 'go-link');
        const token = opts.token || '';
        window.__g2sAroSecToken = token;
        if (!form || !token) return;
        let inp = form.querySelector('input[name="arolinks_sec_token"]');
        if (!inp) {
          inp = document.createElement('input');
          inp.type = 'hidden';
          inp.name = 'arolinks_sec_token';
          form.appendChild(inp);
        }
        inp.value = token;
      };
      let _aro = { setup: safeSetup };
      Object.defineProperty(window, 'AroShield', {
        configurable: true,
        enumerable: true,
        get: () => _aro,
        set: () => { _aro = { setup: safeSetup }; }
      });
    } catch (e) {}
    try {
      const fakeFs = (type, size, ok, err) => {
        if (typeof ok === 'function') {
          try { ok({}); } catch (e) {}
        }
      };
      window.webkitRequestFileSystem = fakeFs;
      window.requestFileSystem = fakeFs;
      document.cookie = 'ab=2; path=/; max-age=86400';
    } catch (e) {}
    const FACTOR = __FACTOR__;
    const NOACCEL = __NOACCEL__;
    const noAccelHost = NOACCEL.some(h => host === h || host.endsWith('.' + h));
    const isBaseHost = () => {
      const b = window.__g2sBaseHost;
      return Boolean(b && (host === b || host.endsWith('.' + b)));
    };
    const isFinalPage = () => Boolean(
      document.getElementById('go-link') ||
      document.querySelector('form[action*="/links/go"], a.get-link, #form-show')
    );
    const factor = () => {
      if (noAccelHost || isBaseHost()) return 1;
      if (window._cf_chl_opt) return 1;              // CF interstitial
      if (/myphp\.php/i.test(location.pathname || '')) return 1;
      if (isFinalPage()) return 1;                   // AdLinkFly final page (server-checked 5s timer)
      return FACTOR;
    };
    const nativeStr = (name) => 'function ' + name + '() { [native code] }';
    const wrap = (orig, name) => {
      const f = function (fn, d, ...a) {
        let ms = Number(d) || 0;
        if (ms >= 200) ms = Math.max(40, Math.round(ms * factor()));
        return orig.call(this, fn, ms, ...a);
      };
      f.toString = () => nativeStr(name);
      return f;
    };
    window.setTimeout = wrap(window.setTimeout, 'setTimeout');
    window.setInterval = wrap(window.setInterval, 'setInterval');
    window.__g2sTimerWrapped = true;

    // ---- visibility / focus spoofing ------------------------------------
    window.__g2sVis = 'visible';
    window.__g2sEnsureIframe = () => {
      let ifr = [...document.querySelectorAll('iframe')].find(f => {
        const r = f.getBoundingClientRect(); return r.width > 20 && r.height > 20;
      }) || document.getElementById('__g2s_ifr');
      if (!ifr && document.body) {
        ifr = document.createElement('iframe');
        ifr.id = '__g2s_ifr';
        ifr.tabIndex = 0;
        ifr.style.cssText = 'width:22px;height:22px;position:fixed;bottom:0;right:0;opacity:0.01;pointer-events:none;border:0;';
        document.body.appendChild(ifr);
      }
      return ifr;
    };
    try {
      Object.defineProperty(Document.prototype, 'hidden', {
        configurable: true, get() { return window.__g2sVis === 'hidden'; } });
      Object.defineProperty(Document.prototype, 'visibilityState', {
        configurable: true, get() { return window.__g2sVis; } });
      const hf = function () { return window.__g2sVis !== 'hidden'; };
      hf.toString = () => nativeStr('hasFocus');
      Document.prototype.hasFocus = hf;
      const origActive = Object.getOwnPropertyDescriptor(Document.prototype, 'activeElement') ||
                         Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'activeElement');
      if (origActive && origActive.get) {
        Object.defineProperty(Document.prototype, 'activeElement', {
          configurable: true,
          get() {
            if (window.__g2sVis === 'hidden') {
              const ifr = window.__g2sEnsureIframe && window.__g2sEnsureIframe();
              if (ifr) return ifr;
            }
            return origActive.get.call(this);
          }
        });
      }
    } catch (e) {}

    window.__g2sAway = (away) => {
      try {
        if (away) {
          window.__g2sVis = 'hidden';
          const ifr = window.__g2sEnsureIframe ? window.__g2sEnsureIframe() : null;
          try { ifr && ifr.focus(); } catch (e) {}
          window.dispatchEvent(new Event('blur'));
          document.dispatchEvent(new Event('visibilitychange'));
        } else {
          window.__g2sVis = 'visible';
          try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch (e) {}
          try { window.focus(); } catch (e) {}
          document.dispatchEvent(new Event('visibilitychange'));
          window.dispatchEvent(new Event('focus'));
          window.dispatchEvent(new Event('pageshow'));
        }
      } catch (e) {}
    };

    // ---- Turnstile hook ---------------------------------------------------
    window.__g2sTsWidgets = [];
    let _ts;
    try {
      Object.defineProperty(window, 'turnstile', {
        configurable: true,
        get() { return _ts; },
        set(v) {
          _ts = v;
          try {
            if (v && typeof v.render === 'function' && !v.__g2s) {
              const r = v.render.bind(v);
              v.render = function (el, opts) {
                try { window.__g2sTsWidgets.push({ opts: opts || {} }); } catch (e) {}
                return r(el, opts);
              };
              if (typeof v.getResponse === 'function') {
                const g = v.getResponse.bind(v);
                v.getResponse = function (id) { return window.__g2sTsToken || g(id); };
              }
              if (typeof v.isExpired === 'function') {
                const ie = v.isExpired.bind(v);
                v.isExpired = function (id) { return window.__g2sTsToken ? false : ie(id); };
              }
              v.__g2s = true;
            }
          } catch (e) {}
        }
      });
    } catch (e) {}
    };
    const isChl = (n) => /__cf_chl/.test(location.href) ||
      /^(just a moment|attention required|one more step)/i.test(document.title || '') ||
      Boolean(window._cf_chl_opt) ||
      (n && /_cf_chl_opt|orchestrate\/chl_page/.test(n.textContent || ''));
    let decided = false;
    const decide = (n) => {
      if (decided) return; decided = true;
      try { mo.disconnect(); } catch (e) {}
      if (!isChl(n)) install();
    };
    const mo = new MutationObserver((recs) => {
      for (const r of recs) for (const n of r.addedNodes) {
        if (n.nodeName === 'SCRIPT') { decide(n); return; }
      }
    });
    mo.observe(document, { childList: true, subtree: true });
    document.addEventListener('DOMContentLoaded', () => decide(null));
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
# Page scanner - returns everything the engine needs to decide the next move.
# arg = {base: "get2short.com", patterns: [[regex, score], ...], exclude: regex}
# ---------------------------------------------------------------------------
SCAN_JS = r"""
(arg) => {
  const out = { url: location.href, title: document.title || '' };
  document.querySelectorAll('[data-g2s-id]').forEach(el => el.removeAttribute('data-g2s-id'));
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) return false;
    let s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity || '1') < 0.2) return false;
    return true;
  };
  try {
    const vh = document.getElementById('verifyHuman');
    const ib = document.getElementById('initialButton');
    if (vh && ib && typeof window.onVerify === 'function' && ib.style.display === 'none') {
      window.onVerify('ok');
    }
  } catch (e) {}
  try {
    const ggd = document.getElementById('ggd-container');
    const tp2 = document.getElementById('tp-snp2');
    if (((ggd && vis(ggd)) || (tp2 && /(^|;\s*)user_eiop=/.test(document.cookie))) && window.__g2sAway) {
      window.__g2sAway(true);
    }
  } catch (e) {}
  out.loading = document.readyState === 'loading';
  try {
    if (/(^|;\s*)user_eiop=/.test(document.cookie)) {
      sessionStorage.setItem('__g2sPlugin', 'user_eiop');
    } else if (document.getElementById('startCountdownBtn') || /(^|;\s*)uopusi=/.test(document.cookie)) {
      sessionStorage.setItem('__g2sPlugin', 'uopusi');
    } else if (document.getElementById('VerifyBtn') || /(^|;\s*)user_in=/.test(document.cookie)) {
      sessionStorage.setItem('__g2sPlugin', 'user_in');
    }
  } catch (e) {}
  const txt = (el) => ((el.innerText || el.value || el.getAttribute('aria-label') || el.title || '') + '')
                        .replace(/\s+/g, ' ').trim();
  const bodyText = document.body ? (document.body.innerText || '') : '';
  out.text = bodyText.slice(0, 4000);
  out.challenge = !!window._cf_chl_opt || /^(just a moment|attention required|one more step)/i.test(out.title);

  // -- direct links to the shortener's final page (?wp=token or <sub.><base>/<alias>)
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const esc = arg.base.replace(/\./g, '\\.');
  const re = new RegExp('https?:(?:\\\\?/){2}(?:[\\w-]+\\.)*' + esc + '(?::\\d+)?(?:\\\\?/)[^\\s"\'<>]*?[?&]wp=[A-Za-z0-9%=_\\-]+', 'g');
  const found = new Set();
  (html.match(re) || []).forEach(u => found.add(u.replace(/\\\//g, '/').replace(/&amp;/g, '&')));
  document.querySelectorAll('a[href], form[action]').forEach(el => {
    const u = el.href || el.action || '';
    if (re.test(u)) found.add(u);
    re.lastIndex = 0;
    if (el.tagName === 'A' && u && location.hostname && !location.hostname.endsWith(arg.base)) {
      try {
        const pu = new URL(u, location.href);
        if ((pu.hostname === arg.base || pu.hostname.endsWith('.' + arg.base)) && pu.pathname && pu.pathname.length > 1) {
          found.add(pu.href);
        }
      } catch (e) {}
    }
  });
  try {
    const rtgA = document.querySelector('#rtg-snp21 a[href], a#rtg-snp21[href], #rtg-snp2 a[href], a#rtg-snp2[href], a#btn7[href], a#btn6[href*="?wp="]') ||
                 (document.getElementById('rtg-snp2') && document.getElementById('rtg-snp2').closest('a[href]')) ||
                 (document.getElementById('rtg-snp21') && document.getElementById('rtg-snp21').closest('a[href]'));
    if (rtgA && /^https?:\/\//i.test(rtgA.href)) found.add(rtgA.href);
    const gteA = (arg.base !== 'arolinks.com' && arg.base !== 'vplink.in' && sessionStorage.getItem('__g2sPlugin') !== 'uopusi')
      ? document.getElementById('gtelinkbtn')
      : null;
    if (gteA && /^https?:\/\//i.test(gteA.href || '')) {
      let gu = gteA.href;
      if (arg.base === 'inshorturl.in') gu = gu.replace(/^https?:\/\/inshorturl\.com\//i, 'https://inshorturl.in/');
      found.add(gu);
    }
    const tgMatch = html.match(/tagrget_url\s*=\s*["']([A-Za-z0-9+/=_-]+)["']/);
    if (tgMatch && tgMatch[1]) {
      document.cookie = 'user_step=1; path=/; max-age=180';
      let tu = atob(tgMatch[1].trim());
      const gm = tu.match(/^https?:\/\/(?:www\.)?google\.[a-z.]+\/url\?(?:.*&)?url=([^&]+)/i);
      if (gm) tu = decodeURIComponent(gm[1]);
      if (/^https?:\/\//i.test(tu)) {
        if (tu.split('#')[0] === location.href.split('#')[0]) {
          tu = tu.split('#')[0] + (tu.includes('?') ? '&' : '?') + 'step=1';
        }
        found.add(tu);
      }
    }
  } catch (e) {}
  out.wpLinks = [...found].filter(u => u.split('#')[0] !== location.href.split('#')[0]);

  // -- newwpsafelink (mtc1.jobsraavi.in / softurl.in) -----------------------
  out.newWpSafelink = null;
  try {
    let nwpData = null;
    const srMatch = html.match(/safelink_redirect=([A-Za-z0-9+/=%_-]+)/);
    if (srMatch) {
      nwpData = JSON.parse(atob(decodeURIComponent(srMatch[1])));
    } else {
      const nwpInp = document.querySelector('input[name="newwpsafelink"]');
      if (nwpInp && nwpInp.value) nwpData = JSON.parse(atob(nwpInp.value.trim()));
    }
    if (nwpData && nwpData.safelink && /^https?:\/\//i.test(nwpData.safelink)) {
      out.newWpSafelink = { url: nwpData.safelink, delay: Number(nwpData.delay) || 25 };
    }
  } catch (e) {}

  // -- final (AdLinkFly) page markers --------------------------------------
  out.finalMarkers = !!document.querySelector('form[action*="/links/go"], #go-link, a.get-link, #form-show');
  const gl = document.querySelector('a.get-link');
  out.getLinkHref = gl ? (gl.getAttribute('href') || '') : '';
  out.getLinkDisabled = gl ? /disabled/.test(gl.className) : true;

  if (!window.__g2sTimerWrapped && !out.challenge && !out.finalMarkers &&
      location.hostname && !location.hostname.endsWith(arg.base)) {
    try {
      const wrap = (orig) => function (fn, d, ...a) {
        let ms = Number(d) || 0;
        if (ms >= 200) ms = Math.max(40, Math.round(ms * 0.1));
        return orig.call(this, fn, ms, ...a);
      };
      window.setTimeout = wrap(window.setTimeout);
      window.setInterval = wrap(window.setInterval);
      window.__g2sTimerWrapped = true;
    } catch (e) {}
  }

  // -- turnstile ------------------------------------------------------------
  const keys = new Set();
  document.querySelectorAll('[data-sitekey]').forEach(el => {
    const k = el.getAttribute('data-sitekey') || '';
    if (/^[0-3]x[\w-]{10,}$/.test(k)) keys.add(k);
  });
  (window.__g2sTsWidgets || []).forEach(w => { if (w.opts && w.opts.sitekey) keys.add(w.opts.sitekey); });
  document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]').forEach(f => {
    const m = (f.src || '').match(/\/([0-3]x[\w-]{15,})\//); if (m) keys.add(m[1]);
  });
  let action = null, cdata = null;
  const tsEl = document.querySelector('.cf-turnstile[data-action], [data-sitekey][data-action]');
  if (tsEl) { action = tsEl.getAttribute('data-action'); cdata = tsEl.getAttribute('data-cdata'); }
  (window.__g2sTsWidgets || []).forEach(w => { if (w.opts) { action = action || w.opts.action || null; cdata = cdata || w.opts.cData || null; } });
  const inputs = [...document.querySelectorAll('[name="cf-turnstile-response"]')];
  const solvedInp = inputs.find(i => (i.value || '').length > 20);
  const tokVal = window.__g2sTsToken || (solvedInp ? solvedInp.value : null);
  const goForm = document.getElementById('go-link');
  const isUrlspayGate = arg.base === 'urlspay.com' && !goForm && !!(document.getElementById('step1Btn') || document.getElementById('goBtn'));
  out.urlspayGateway = isUrlspayGate;
  const goHasNoTs = ((arg.base === 'babylinks.in' || arg.base === 'vipshort.in' || arg.base === 'alpha-links.in' || arg.base === 'urllinkshort.in' || arg.base === 'urlspay.com' || arg.base === 'v2links.me') && !!goForm) || isUrlspayGate;
  out.turnstile = {
    present: !goHasNoTs && (keys.size > 0 || inputs.length > 0 || !!document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]')),
    sitekeys: [...keys], action, cdata,
    solved: !!tokVal,
    token: tokVal,
  };

  // -- clickable candidates ---------------------------------------------------
  const pats = arg.patterns.map(([r, s]) => [new RegExp(r, 'i'), s]);
  const excl = new RegExp(arg.exclude, 'i');
  const cands = [];
  let id = 0;
  const timerBoxVis = vis(document.getElementById('timerBox'));
  const waitBoxVis = vis(document.getElementById('WaitBox'));
  const hasTp2 = !!document.getElementById('tp-snp2');
  const hasCross2 = !!document.getElementById('cross-snp2');
  const pluginType = (() => {
    if (/(^|;\s*)user_eiop=/.test(document.cookie) && hasTp2) return 'user_eiop';
    if (hasCross2 && !hasTp2) return 'uopusi';
    return null;
  })();
  const rawStep1 = (pluginType === 'user_eiop')
    ? null
    : (document.getElementById('startCountdownBtn') || document.getElementById('VerifyBtn'));
  const step1Btn = (rawStep1 && vis(rawStep1) && !rawStep1.disabled) ? rawStep1 : null;
  const rawBtn6 = document.getElementById('btn6');
  const btn6Vis = !!(rawBtn6 && vis(rawBtn6) && !rawBtn6.disabled);
  document.querySelectorAll('button, a, input[type=submit], input[type=button], [role=button], [onclick]').forEach(el => {
    const inTp2 = el.id === 'tp-snp2' || !!el.closest('#tp-snp2');
    const inCross2 = el.id === 'cross-snp2' || !!el.closest('#cross-snp2');
    const inBtn17 = el.id === 'btn1' || el.id === 'btn7' || !!el.closest('#btn1, #btn7');
    const inBtn6 = el.id === 'btn6' || !!el.closest('#btn6');
    if (goForm && (el.id === 'ai3-get-link-btn' || el.classList.contains('get-link'))) {
      const gh = (el.getAttribute('href') || '').trim();
      if (!gh || gh === '#' || /^javascript:/i.test(gh)) return;
    }
    if ((hasTp2 || hasCross2) && (inBtn17 || inBtn6)) return;
    if (btn6Vis && inBtn17) return;
    if (step1Btn && (inTp2 || inCross2)) return;
    if (pluginType === 'user_eiop' && (el.id === 'startCountdownBtn' || inCross2)) return;
    if (pluginType === 'uopusi' && inTp2) return;
    if (pluginType === 'user_in' && inCross2) return;
    if (inTp2 && timerBoxVis) return;
    if (inCross2 && waitBoxVis) return;
    const t = txt(el);
    if (!t || t.length > 60) return;
    const cleanT = t.replace(/^[^\w]+|[^\w]+$/g, '').trim();
    if (!cleanT) return;
    // never use the blog's own navigation (pagination, next/prev post, menus, widgets)
    if (el.closest('nav, .nav-links, .post-navigation, .navigation, .pagination, .page-numbers, ' +
                   'header, footer, aside, .widget, .menu, .comments-area, .ast-pagination')) return;
    if (/\b(next|prev)\b/i.test(el.getAttribute('rel') || '')) return;
    if (excl.test(t) || excl.test(cleanT)) return;
    let score = 0;
    for (const [r, s] of pats) if (r.test(t) || r.test(cleanT)) { score = Math.max(score, s); }
    if (!score) return;
    if (!vis(el)) return;
    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                     /(^|\s)disabled(\s|$)/.test(el.className || '') ||
                     getComputedStyle(el).pointerEvents === 'none';
    if (disabled) return;
    const gid = 'c' + (id++);
    el.setAttribute('data-g2s-id', gid);
    const parentA = el.closest('a[href]');
    const childIdEl = el.querySelector ? el.querySelector('[id]') : null;
    const href = el.getAttribute('href') || (parentA ? parentA.href : '') || '';
    const elId = el.id || (childIdEl ? childIdEl.id : '') || (parentA ? parentA.id : '') || '';
    cands.push({ id: gid, elId, text: t, tag: el.tagName, href, score });
  });
  cands.sort((a, b) => (b.score - a.score) || ((b.href ? 1 : 0) - (a.href ? 1 : 0)));
  if (cands.length > 0 || document.readyState === 'loading') {
    window.__g2sNoCandSince = 0;
  } else if (location.hostname.includes('rupeeriser.') || location.hostname.includes('bcsakhi.')) {
    if (!window.__g2sNoCandSince) window.__g2sNoCandSince = Date.now();
    if (Date.now() - window.__g2sNoCandSince > 15000) {
      window.__g2sNoCandSince = Date.now();
      const unhide = (el) => {
        for (let cur = el; cur && cur !== document.body; cur = cur.parentElement) {
          if (getComputedStyle(cur).display === 'none') cur.style.setProperty('display', 'block', 'important');
          if (getComputedStyle(cur).visibility === 'hidden') cur.style.setProperty('visibility', 'visible', 'important');
          if (getComputedStyle(cur).opacity === '0') cur.style.setProperty('opacity', '1', 'important');
          cur.disabled = false;
        }
      };
      const fallbackTarget = (pluginType === 'user_eiop' && document.getElementById('tp-snp2')) ||
                             (pluginType === 'uopusi' && document.getElementById('cross-snp2')) ||
                             document.getElementById('btn7') ||
                             document.getElementById('cross-snp2') ||
                             document.getElementById('btn1') ||
                             document.getElementById('tp-snp2');
      if (fallbackTarget) {
        const wb = document.getElementById('WaitBox');
        if (wb) wb.style.display = 'none';
        const tb = document.getElementById('timerBox');
        if (tb) tb.style.display = 'none';
        unhide(fallbackTarget);
      }
    }
  }
  out.candidates = cands.slice(0, 8);

  // -- WP safelink forms (server-side timer token) ----------------------------
  out.safeForms = [];
  document.querySelectorAll('form').forEach((f, i) => {
    const ins = [...f.querySelectorAll('input[type=hidden], input:not([type])')];
    const tok = ins.find(x => /timer_token|safelink.*token/i.test(x.name || ''));
    const sl = ins.find(x => (/safelink|wpsafe|newwp/i.test(x.name || '')) &&
                             !(x.name === 'newwpsafelink' && out.newWpSafelink));
    const rtg = (f.id === 'rtg' && ins.find(x => x.name === 'folder') &&
                 !document.querySelector('#rtg-snp21 a[href^="http"], a#rtg-snp21[href^="http"]'));
    const tpInp = (f.id === 'tpForm') ? ins.find(x => /^(any\d*|get)$/.test(x.name || '')) : null;
    if (tpInp) {
      document.cookie = 'page=4; path=/';
      const pv = f.querySelector('input[name="page_value"]');
      if (pv && !pv.value) pv.value = '4';
    }
    if (!tok && !sl && !rtg && !tpInp) return;
    let fid = 'f' + i;
    if (tpInp) {
      fid += '_' + tpInp.name;
    } else if (rtg) {
      const sm = bodyText.match(/Step\s*(\d+)/i);
      if (sm) fid += '_s' + sm[1];
    }
    f.setAttribute('data-g2s-form', fid);
    out.safeForms.push({ id: fid, action: f.getAttribute('action') || '', method: (f.method || 'get').toLowerCase(),
                         token: tok ? tok.value : null, fields: ins.map(x => x.name).filter(Boolean),
                         hasTs: !!f.querySelector('[name="cf-turnstile-response"]') });
  });

  const tm = bodyText.match(/wait\s*(\d{1,3})|(\d{1,3})\s*(?:sec|seconds)\b/i);
  out.waitHint = tm ? (tm[1] || tm[2]) : null;
  out.adsHint = /click (?:on )?ads?|sponser|sponsor|visit (?:any )?ad/i.test(bodyText);
  out.atBottom = (window.innerHeight + window.scrollY) >= (document.documentElement.scrollHeight - 30);
  out.closeButtons = [...document.querySelectorAll('button, span, a, div')].filter(el => {
    const t = (el.innerText || '').trim();
    return (t === '×' || t === '✕' || t === 'X' || /^close$/i.test(t)) && vis(el);
  }).length;
  return out;
}
"""

INJECT_TOKEN_JS = r"""
(token) => {
  window.__g2sTsToken = token;
  window.__aroTs = token;
  let n = 0;
  let hasAroShield = !!window.__g2sAroSecToken;
  document.querySelectorAll('[name="cf-turnstile-response"]').forEach(i => { i.value = token; n++; });
  const goForm = document.getElementById('go-link') || document.querySelector('form[action*="/links/go"]');
  if (goForm && !location.hostname.endsWith('babylinks.in')) {
    let gi = goForm.querySelector('[name="cf-turnstile-response"]');
    if (!gi) {
      gi = document.createElement('input');
      gi.type = 'hidden';
      gi.name = 'cf-turnstile-response';
      goForm.appendChild(gi);
      n++;
    } else if (gi.value !== token) {
      gi.value = token;
      n++;
    } else if (n === 0) {
      n++;
    }
    gi.value = token;
    try {
      const html = document.documentElement ? document.documentElement.outerHTML : '';
      const mSec = html.match(/AroShield\.setup\(\{[\s\S]*?token:\s*["']([^"']+)["']/);
      const secVal = (mSec && mSec[1]) || window.__g2sAroSecToken || '';
      if (secVal) {
        hasAroShield = true;
        let secInp = goForm.querySelector('input[name="arolinks_sec_token"]');
        if (!secInp) {
          secInp = document.createElement('input');
          secInp.type = 'hidden';
          secInp.name = 'arolinks_sec_token';
          goForm.appendChild(secInp);
        }
        secInp.value = secVal;
      }
    } catch (e) {}
  }
  if (!hasAroShield && typeof window.aroTsOk === 'function') {
    try { window.aroTsOk(token); n++; } catch (e) {}
  }
  document.querySelectorAll('.cf-turnstile, [data-sitekey]').forEach(el => {
    const form = el.closest('form');
    if (form && !form.querySelector('[name="cf-turnstile-response"]')) {
      const i = document.createElement('input');
      i.type = 'hidden'; i.name = 'cf-turnstile-response'; i.value = token;
      form.appendChild(i); n++;
    }
    const cb = el.getAttribute('data-callback');
    if (cb && cb !== 'aroTsOk' && typeof window[cb] === 'function') {
      try { window[cb](token); n++; } catch (e) {}
    }
  });
  (window.__g2sTsWidgets || []).forEach(w => {
    try {
      const cb = w.opts && w.opts.callback;
      if (hasAroShield && (cb === window.aroTsOk || cb === 'aroTsOk')) return;
      if (typeof cb === 'function') { cb(token); n++; }
      else if (typeof cb === 'string' && typeof window[cb] === 'function') { window[cb](token); n++; }
    } catch (e) {}
  });
  return n;
}
"""

CLOSE_OVERLAYS_JS = r"""
() => {
  let n = 0;
  [...document.querySelectorAll('button, span, a, div')].forEach(el => {
    const t = (el.innerText || '').trim();
    if (t === '×' || t === '✕' || t === 'X' || /^close$/i.test(t)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.width < 80 && r.height < 80) { try { el.click(); n++; } catch (e) {} }
    }
  });
  return n;
}
"""

JS_CLICK = r"""
(gid) => {
  const el = document.querySelector('[data-g2s-id="' + gid + '"]');
  if (!el) return false;
  el.scrollIntoView({ block: 'center' });
  const parentA = el.closest('a[href]');
  if (el.id === 'btn7' || (parentA && parentA.id === 'btn7') || !!el.closest('#btn7')) {
    document.cookie = 'iotes=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
    const b1 = document.getElementById('btn1');
    if (b1 && b1.href && !b1.href.startsWith('javascript:')) {
      window.location.href = b1.href;
      return true;
    }
    if (parentA && parentA.href && !parentA.href.startsWith('javascript:') && !parentA.href.endsWith('#')) {
      window.location.href = parentA.href;
      return true;
    }
  }
  if (parentA && parentA.href && !parentA.href.startsWith('javascript:') && !parentA.href.endsWith('#')) {
    const oc = parentA.getAttribute('onclick') || '';
    if (/specialActionHandler/i.test(oc)) {
      el.click();
      return true;
    }
    if (el.id === 'cross-snp2' || parentA.id === 'cross-snp2' || /\/readmore/i.test(parentA.href) || document.getElementById('cross-snp2')) {
      document.cookie = '_uocat=value; path=/; max-age=86400';
    }
    if (/learn_more\.php/i.test(parentA.href)) {
      document.cookie = 'user_eiop=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
      document.cookie = 'user_in=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
      document.cookie = 'iotes=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
      try { el.click(); } catch (e) {}
      window.location.href = parentA.href;
      return true;
    }
    el.click();
    window.location.href = parentA.href;
    return true;
  }
  el.click();
  return true;
}
"""

SUBMIT_GO_FORM_JS = r"""
() => {
  try { document.cookie = 'ab=2; path=/; max-age=86400'; } catch (e) {}
  const gl = document.querySelector('a.get-link');
  if (gl && gl.href && !gl.href.endsWith('#') && !gl.href.startsWith('javascript:') &&
      !/effectivecpmnetwork|highperformanceformat|profitablecpmrate|achievablecpmrevenue/i.test(gl.href)) {
    gl.click();
    return true;
  }
  const f = document.querySelector('form[action*="/links/go"], #go-link');
  if (!f) return false;
  let csrf = '';
  try {
    const ci = f.querySelector('input[name="_csrfToken"]');
    if (ci && ci.value) csrf = ci.value;
    f.classList.add('go-link');
    const html = document.documentElement ? document.documentElement.outerHTML : '';
    const mSec = html.match(/AroShield\.setup\(\{[\s\S]*?token:\s*["']([^"']+)["']/);
    const secVal = (mSec && mSec[1]) || window.__g2sAroSecToken || '';
    if (secVal) {
      let secInp = f.querySelector('input[name="arolinks_sec_token"]');
      if (!secInp) {
        secInp = document.createElement('input');
        secInp.type = 'hidden';
        secInp.name = 'arolinks_sec_token';
        f.appendChild(secInp);
      }
      secInp.value = secVal;
    }
    const tok = window.__g2sTsToken || window.__aroTs || '';
    const unlocked = (f.querySelector('input[name="_Token[unlocked]"]')?.value || '');
    const allowsTs = !!f.querySelector('[name="cf-turnstile-response"], .cf-turnstile') ||
                     unlocked.includes('cf-turnstile-response') || !!secVal;
    if (tok && allowsTs && !location.hostname.endsWith('babylinks.in')) {
      window.__aroTs = tok;
      let gi = f.querySelector('[name="cf-turnstile-response"]');
      if (!gi) {
        gi = document.createElement('input');
        gi.type = 'hidden';
        gi.name = 'cf-turnstile-response';
        f.appendChild(gi);
      }
      gi.value = tok;
    }
  } catch (e) {}
  try {
    if (window.jQuery && window.jQuery.ajax && !location.hostname.endsWith('babylinks.in')) {
      const hdrs = csrf ? { 'X-CSRF-Token': csrf } : {};
      window.jQuery.ajax({
        dataType: 'json',
        type: 'POST',
        url: f.getAttribute('action') || '/links/go',
        headers: hdrs,
        data: window.jQuery(f).serialize(),
        success: function(result) {
          if (result && result.url) {
            window.finalLink = result.url;
            const a = document.querySelector('a.get-link');
            if (a) {
              a.setAttribute('href', result.url);
              a.classList.remove('disabled');
            }
          }
        }
      });
      return true;
    }
  } catch (e) {}
  try {
    const fd = new URLSearchParams(new FormData(f));
    const hdrs = {
      'X-Requested-With': 'XMLHttpRequest',
      'Accept': 'application/json, text/javascript, */*; q=0.01',
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
    };
    if (csrf) hdrs['X-CSRF-Token'] = csrf;
    fetch(f.action || '/links/go', {
      method: 'POST',
      headers: hdrs,
      body: fd.toString(),
      credentials: 'include'
    }).then(r => r.json()).then(result => {
      if (result && result.url) {
        window.finalLink = result.url;
        const a = document.querySelector('a.get-link');
        if (a) {
          a.setAttribute('href', result.url);
          a.classList.remove('disabled');
        }
      }
    }).catch(() => {});
    return true;
  } catch (e) {}
  return false;
}
"""

SUBMIT_FORM_JS = r"""
(fid) => {
  const f = document.querySelector('form[data-g2s-form="' + fid + '"]');
  if (!f) return false;
  HTMLFormElement.prototype.submit.call(f);   // native submit, skips onsubmit handlers
  return true;
}
"""
