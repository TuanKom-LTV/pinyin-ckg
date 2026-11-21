/* PinYin · bộ dịch giao diện (Tiếng Việt → English / 中文).
   File frontend/i18n.js được tạo từ file này + i18n/dict.json bằng i18n/build.py.
   Cách chạy: chữ nào hiện lên trang (text, title, placeholder, chữ vẽ trên canvas, alert/confirm/prompt) đều tra từ điển.
   Số, mã cặp, khung thời gian trong câu được thay bằng {0},{1}… để 1 mẫu dịch dùng cho mọi giá trị. */
(function () {
  var D = window.PY_I18N || {};
  var LANGS = [["vi", "🇻🇳 Tiếng Việt"], ["en", "🇬🇧 English"], ["zh", "🇨🇳 中文"]];
  function get() {
    var l = null;
    try { l = localStorage.getItem("pinyin-lang"); } catch (e) {}
    if (!l) { var m = document.cookie.match(/(?:^|;\s*)lang=(vi|en|zh)/); l = m && m[1]; }
    return l === "en" || l === "zh" ? l : "vi";
  }
  var L = get(), K = L === "en" ? 0 : 1;
  window.PY_LANG = L;
  window.pySetLang = function (l) {
    try { localStorage.setItem("pinyin-lang", l); } catch (e) {}
    document.cookie = "lang=" + l + ";path=/;max-age=31536000;samesite=lax";
    location.reload();
  };
  // bảo đảm máy chủ biết ngôn ngữ (bài AI, trang đăng nhập) kể cả khi chỉ có localStorage
  if (!/(?:^|;\s*)lang=/.test(document.cookie) && L !== "vi") document.cookie = "lang=" + L + ";path=/;max-age=31536000;samesite=lax";

  // ---- nút chọn ngôn ngữ ----
  function switcher() {
    if (document.getElementById("pylang")) return;
    var host = document.getElementById("langsw"), s = document.createElement("select");
    s.id = "pylang"; s.setAttribute("aria-label", "Language");
    s.className = "pylang";
    LANGS.forEach(function (x) { var o = document.createElement("option"); o.value = x[0]; o.textContent = x[1]; if (x[0] === L) o.selected = true; s.appendChild(o); });
    s.onchange = function () { window.pySetLang(s.value); };
    s.setAttribute("data-noi18n", "1");
    if (host) host.appendChild(s);
    else { s.style.cssText = "position:fixed;top:10px;right:10px;z-index:9999"; document.body.appendChild(s); }
  }
  var css = document.createElement("style");
  css.textContent = ".pylang{width:auto;font:inherit;font-size:13px;font-weight:600;padding:6px 8px;border-radius:9px;border:1px solid var(--bd,#1c2a3d);background:var(--card,#0d1320);color:var(--tx,#e8ecf3);cursor:pointer}";
  (document.head || document.documentElement).appendChild(css);
  if (L === "vi") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", switcher); else switcher();
    window.pyT = function (s) { return s; };
    return;
  }
  document.documentElement.lang = L === "zh" ? "zh-CN" : "en";

  // ---- tra từ điển ----
  var VAR = /\b(?:XAUUSD|GBPUSD|EURUSD|AUDUSD|GBPJPY|EURJPY)\b|\d+(?:[.,:]\d+)*/g;
  var VN = /[À-ÃÈ-ÊÌÍÒ-ÕÙÚÝà-ãè-êìíò-õùúýĂăĐđĨĩŨũƠơƯưẠ-ỹ]/;
  var cache = new Map();
  var WD = [{ 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat", 8: "Sun" }, { 2: "周一", 3: "周二", 4: "周三", 5: "周四", 6: "周五", 7: "周六", 8: "周日" }];
  function tr(s) {
    if (s == null) return s;
    var raw = String(s);
    if (raw.length > 600) return raw;
    var hit = cache.get(raw);
    if (hit !== undefined) return hit;
    var t = raw.replace(/\s+/g, " ").trim(), out = raw;
    if (t) {
      var vals = [], key = t.replace(VAR, function (m) { vals.push(m); return "{" + (vals.length - 1) + "}"; });
      var e = D[key];
      if (e && e[K]) {
        var lead = raw.match(/^\s*/)[0], tail = raw.match(/\s*$/)[0];
        out = lead + e[K].replace(/\{W(\d+)\}/g, function (_, i) { return (WD[K][+vals[+i]] || vals[+i] || ""); })   // {W0}: "Thứ 2" → Mon / 周一
                         .replace(/\{(\d+)\}/g, function (_, i) { return vals[+i] != null ? vals[+i] : ""; }) + tail;
      } else if (VN.test(t) && window.PY_MISS) window.PY_MISS.add(key);
    }
    if (cache.size > 20000) cache.clear();
    cache.set(raw, out);
    return out;
  }
  window.pyT = tr;

  // ---- chữ trên trang ----
  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1, NOSCRIPT: 1 };
  var mine = new WeakSet();
  function textNode(n) {
    var p = n.parentNode;
    if (!p || SKIP[p.nodeName] || (p.closest && p.closest("[data-noi18n]"))) return;
    if (mine.has(n) && n.__py === n.data) return;
    var v = n.data, x = tr(v);
    if (x !== v) { n.data = x; mine.add(n); n.__py = x; }
  }
  var ATTR = ["title", "placeholder", "aria-label"];
  function attrs(el) {
    if (el.closest && el.closest("[data-noi18n]")) return;
    for (var i = 0; i < ATTR.length; i++) {
      var a = ATTR[i], v = el.getAttribute && el.getAttribute(a);
      if (v) { var x = tr(v); if (x !== v) el.setAttribute(a, x); }
    }
    if (el.nodeName === "INPUT" && (el.type === "button" || el.type === "submit") && el.value) { var y = tr(el.value); if (y !== el.value) el.value = y; }
  }
  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) return textNode(root);
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1) { if (SKIP[root.nodeName]) return; attrs(root); }
    var w = document.createTreeWalker(root, 5, null), n;   // SHOW_ELEMENT | SHOW_TEXT
    while ((n = w.nextNode())) { if (n.nodeType === 3) textNode(n); else attrs(n); }
  }
  var pend = new Set(), sched = false;
  function flush() { sched = false; var a = Array.from(pend); pend.clear(); for (var i = 0; i < a.length; i++) walk(a[i]); if (document.title) { var tt = tr(document.title); if (tt !== document.title) document.title = tt; } }
  var mo = new MutationObserver(function (ms) {
    for (var i = 0; i < ms.length; i++) {
      var m = ms[i];
      if (m.type === "childList") m.addedNodes.forEach(function (n) { pend.add(n); });
      else if (m.type === "characterData") pend.add(m.target);
      else if (m.type === "attributes") pend.add(m.target);
    }
    if (!sched) { sched = true; Promise.resolve().then(flush); }   // dịch trước khi trình duyệt vẽ
  });
  function start() {
    walk(document.body); switcher();
    if (document.title) document.title = tr(document.title);
    mo.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTR });
    var t = document.querySelector("title"); if (t) mo.observe(t, { childList: true, characterData: true, subtree: true });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();

  // ---- chữ vẽ trên biểu đồ (canvas) + hộp thoại ----
  var C = window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (C) {
    var ft = C.fillText, st = C.strokeText, mt = C.measureText;
    var cv = function (s) { return typeof s === "string" && /[A-Za-zÀ-ỹ]/.test(s) ? tr(s) : s; };
    C.fillText = function (s, x, y, w) { return w === undefined ? ft.call(this, cv(s), x, y) : ft.call(this, cv(s), x, y, w); };
    C.strokeText = function (s, x, y, w) { return w === undefined ? st.call(this, cv(s), x, y) : st.call(this, cv(s), x, y, w); };
    C.measureText = function (s) { return mt.call(this, cv(s)); };
  }
  var al = window.alert, cf = window.confirm, pr = window.prompt;
  window.alert = function (m) { return al.call(window, tr(m)); };
  window.confirm = function (m) { return cf.call(window, tr(m)); };
  window.prompt = function (m, d) { return pr.call(window, tr(m), d); };
})();
