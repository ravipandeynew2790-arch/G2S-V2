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
    if (((ggd && vis(ggd)) || tp2) && window.__g2sAway) {
      window.__g2sAway(true);
    }
  } catch (e) {}
  try {
    if (document.getElementById('startCountdownBtn') || /(^|;\s*)uopusi=/.test(document.cookie)) {
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
  });
  try {
    const rtgA = document.querySelector('#rtg-snp21 a[href], a#rtg-snp21[href], a#btn7[href], a#btn6[href*="?wp="]');
    if (rtgA && /^https?:\/\//i.test(rtgA.href)) found.add(rtgA.href);
    const gteA = (arg.base !== 'arolinks.com' && sessionStorage.getItem('__g2sPlugin') !== 'uopusi')
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
      if (/^https?:\/\//i.test(tu)) found.add(tu);
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
  const goHasNoTs = arg.base === 'babylinks.in' && !!goForm;
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
  const step1Btn = document.getElementById('startCountdownBtn') || document.getElementById('VerifyBtn');
  const pluginType = (() => {
    if (arg.base === 'arolinks.com') return 'uopusi';
    try { return sessionStorage.getItem('__g2sPlugin'); } catch (e) { return null; }
  })();
  document.querySelectorAll('button, a, input[type=submit], input[type=button], [role=button], [onclick]').forEach(el => {
    const inTp2 = el.id === 'tp-snp2' || !!el.closest('#tp-snp2');
    const inCross2 = el.id === 'cross-snp2' || !!el.closest('#cross-snp2');
    if (step1Btn && (inTp2 || inCross2)) return;
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
    const href = el.getAttribute('href') || (parentA ? parentA.href : '') || '';
    cands.push({ id: gid, text: t, tag: el.tagName, href, score });
  });
  cands.sort((a, b) => (b.score - a.score) || ((b.href ? 1 : 0) - (a.href ? 1 : 0)));
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
  let n = 0;
  document.querySelectorAll('[name="cf-turnstile-response"]').forEach(i => { i.value = token; n++; });
  document.querySelectorAll('.cf-turnstile, [data-sitekey]').forEach(el => {
    const form = el.closest('form');
    if (form && !form.querySelector('[name="cf-turnstile-response"]')) {
      const i = document.createElement('input');
      i.type = 'hidden'; i.name = 'cf-turnstile-response'; i.value = token;
      form.appendChild(i); n++;
    }
    const cb = el.getAttribute('data-callback');
    if (cb && typeof window[cb] === 'function') { try { window[cb](token); n++; } catch (e) {} }
  });
  (window.__g2sTsWidgets || []).forEach(w => {
    try {
      const cb = w.opts && w.opts.callback;
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
  if (parentA && parentA.href && !parentA.href.startsWith('javascript:') && !parentA.href.endsWith('#')) {
    const oc = parentA.getAttribute('onclick') || '';
    if (/specialActionHandler/i.test(oc)) {
      el.click();
      return true;
    }
    if (/learn_more\.php/i.test(parentA.href)) {
      window.location.href = parentA.href;
      return true;
    }
    if (parentA.id === 'cross-snp2' || /\/readmore/i.test(parentA.href)) {
      document.cookie = '_uocat=value; path=/; max-age=86400';
      el.click();
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
  const gl = document.querySelector('a.get-link');
  if (gl && gl.href && !gl.href.endsWith('#') && !gl.href.startsWith('javascript:')) {
    gl.click();
    return true;
  }
  const f = document.querySelector('form[action*="/links/go"], #go-link');
  if (!f) return false;
  try {
    if (window.jQuery && !location.hostname.endsWith('babylinks.in')) {
      window.jQuery(f).triggerHandler('submit');
      return true;
    }
  } catch (e) {}
  try {
    const fd = new URLSearchParams(new FormData(f));
    fetch(f.action || '/links/go', {
      method: 'POST',
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
      },
      body: fd.toString(),
      credentials: 'include'
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
