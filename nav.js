/* ==========================================================================
   Mizzentop Admin Portal — global navigation bar
   --------------------------------------------------------------------------
   Drop-in, zero-dependency. Every page includes it with one line:

       <script src="/nav.js" defer></script>

   The menu itself is NOT defined here. It comes from GET /api/nav, which the
   server builds from PERMISSION_SILOS in app.py and filters to the pages the
   signed-in staff member may actually open. Adding a page to the portal is
   therefore a one-line change in app.py — never a change to 24 HTML files.

   The script also hides each page's now-redundant "<- Home" link, so the old
   per-page top bars keep working as page-title strips without a duplicate
   way back to the portal.
   ========================================================================== */
(function () {
  "use strict";

  if (window.__mznNavLoaded) return;
  window.__mznNavLoaded = true;

  var NAV_ID = "mzn-nav";

  /* ---------- styles ------------------------------------------------------ */

  var CSS = [
    "#" + NAV_ID + "{position:sticky;top:0;z-index:9500;background:#16223b;",
    "  font-family:'DM Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;",
    "  box-shadow:0 1px 0 rgba(255,255,255,.07),0 2px 12px rgba(0,0,0,.18);",
    "  -webkit-font-smoothing:antialiased;}",
    "#" + NAV_ID + " *{box-sizing:border-box;}",
    "#" + NAV_ID + " .mzn-bar{display:flex;align-items:center;gap:4px;padding:0 16px;height:50px;}",
    "#" + NAV_ID + " .mzn-brand{display:flex;align-items:center;gap:9px;text-decoration:none;",
    "  padding:6px 10px 6px 4px;margin-right:6px;border-radius:8px;flex-shrink:0;}",
    "#" + NAV_ID + " .mzn-brand:hover{background:rgba(255,255,255,.08);}",
    "#" + NAV_ID + " .mzn-brand img{width:26px;height:26px;display:block;}",
    "#" + NAV_ID + " .mzn-brand span{color:#fff;font-size:13.5px;font-weight:600;letter-spacing:.01em;white-space:nowrap;}",

    /* menu buttons */
    "#" + NAV_ID + " .mzn-links{display:flex;flex-direction:row;align-items:center;gap:2px;}",
    "#" + NAV_ID + " .mzn-grouplabel{display:none;}",
    "#" + NAV_ID + " .mzn-menu{position:relative;}",
    "#" + NAV_ID + " .mzn-btn{appearance:none;border:0;background:transparent;cursor:pointer;",
    "  color:rgba(255,255,255,.78);font:500 13px/1 'DM Sans',sans-serif;padding:9px 12px;",
    "  border-radius:8px;display:flex;align-items:center;gap:6px;white-space:nowrap;",
    "  transition:background .14s ease,color .14s ease;}",
    "#" + NAV_ID + " .mzn-btn:hover{background:rgba(255,255,255,.10);color:#fff;}",
    "#" + NAV_ID + " .mzn-btn .mzn-caret{width:8px;height:5px;opacity:.6;transition:transform .16s ease;}",
    "#" + NAV_ID + " .mzn-menu.open .mzn-btn{background:rgba(255,255,255,.14);color:#fff;}",
    "#" + NAV_ID + " .mzn-menu.open .mzn-caret{transform:rotate(180deg);}",
    "#" + NAV_ID + " .mzn-btn.mzn-here{color:#e5bb52;}",
    "#" + NAV_ID + " .mzn-btn.mzn-here::after{content:'';position:absolute;left:12px;right:12px;bottom:-1px;",
    "  height:2px;background:#c8992a;border-radius:2px;}",

    /* dropdown panel */
    "#" + NAV_ID + " .mzn-drop{position:absolute;top:calc(100% + 7px);left:0;min-width:236px;",
    "  background:#fff;border-radius:12px;padding:7px;",
    "  box-shadow:0 12px 32px rgba(16,24,40,.20),0 0 0 1px rgba(16,24,40,.08);",
    "  opacity:0;transform:translateY(-5px);pointer-events:none;transition:opacity .13s ease,transform .13s ease;}",
    "#" + NAV_ID + " .mzn-menu.open .mzn-drop{opacity:1;transform:none;pointer-events:auto;}",
    "#" + NAV_ID + " .mzn-drop a{display:flex;align-items:center;gap:9px;text-decoration:none;",
    "  color:#1a2744;font-size:13.5px;font-weight:500;padding:9px 11px;border-radius:8px;white-space:nowrap;}",
    "#" + NAV_ID + " .mzn-drop a:hover,#" + NAV_ID + " .mzn-drop a:focus{background:#f2f4f8;outline:none;}",
    "#" + NAV_ID + " .mzn-drop a.mzn-current{background:#eef1f7;color:#1a2744;font-weight:600;}",
    "#" + NAV_ID + " .mzn-drop a .mzn-dot{width:5px;height:5px;border-radius:50%;background:#c8992a;flex-shrink:0;opacity:0;}",
    "#" + NAV_ID + " .mzn-drop a.mzn-current .mzn-dot{opacity:1;}",

    /* right side */
    "#" + NAV_ID + " .mzn-spacer{flex:1 1 auto;}",
    "#" + NAV_ID + " .mzn-who{color:rgba(255,255,255,.5);font-size:12px;white-space:nowrap;",
    "  overflow:hidden;text-overflow:ellipsis;max-width:190px;margin-right:4px;}",
    "#" + NAV_ID + " .mzn-out{color:rgba(255,255,255,.62);font-size:12.5px;font-weight:500;text-decoration:none;",
    "  padding:8px 11px;border-radius:8px;white-space:nowrap;}",
    "#" + NAV_ID + " .mzn-out:hover{background:rgba(255,255,255,.10);color:#fff;}",

    /* hamburger (small screens) */
    "#" + NAV_ID + " .mzn-burger{display:none;}",
    "@media (max-width:900px){",
    "  #" + NAV_ID + " .mzn-bar{gap:0;padding:0 10px;}",
    "  #" + NAV_ID + " .mzn-burger{display:flex;}",
    "  #" + NAV_ID + " .mzn-who{display:none;}",
    "  #" + NAV_ID + " .mzn-links{display:none;position:absolute;top:50px;left:0;right:0;",
    "    background:#16223b;flex-direction:column;align-items:stretch;padding:8px;gap:2px;",
    "    max-height:calc(100vh - 50px);overflow-y:auto;box-shadow:0 14px 28px rgba(0,0,0,.28);}",
    "  #" + NAV_ID + ".mzn-open .mzn-links{display:flex;}",
    "  #" + NAV_ID + " .mzn-links .mzn-btn{display:none;}",
    "  #" + NAV_ID + " .mzn-menu{position:static;}",
    "  #" + NAV_ID + " .mzn-drop{position:static;opacity:1;transform:none;pointer-events:auto;",
    "    background:transparent;box-shadow:none;padding:0 0 8px;min-width:0;}",
    "  #" + NAV_ID + " .mzn-drop a{color:rgba(255,255,255,.85);padding:10px 12px;}",
    "  #" + NAV_ID + " .mzn-drop a:hover{background:rgba(255,255,255,.10);}",
    "  #" + NAV_ID + " .mzn-drop a.mzn-current{background:rgba(255,255,255,.16);color:#fff;}",
    "  #" + NAV_ID + " .mzn-grouplabel{display:block;color:#c8992a;font-size:10px;font-weight:600;",
    "    letter-spacing:.14em;text-transform:uppercase;padding:12px 12px 6px;}",
    "}",
    "@media print{#" + NAV_ID + "{display:none !important;}}"
  ].join("\n");

  /* ---------- tiny helpers ------------------------------------------------ */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function normalize(path) {
    if (!path) return "/";
    path = path.split("?")[0].split("#")[0];
    if (path.length > 1 && path.charAt(path.length - 1) === "/") path = path.slice(0, -1);
    return path.toLowerCase();
  }

  var HERE = normalize(window.location.pathname);

  /* ---------- hide the old per-page "Home" links --------------------------- */

  function hideLegacyHomeLinks() {
    var anchors = document.querySelectorAll('a[href="/"], a[href="/portal"]');
    for (var i = 0; i < anchors.length; i++) {
      var a = anchors[i];
      if (a.closest("#" + NAV_ID)) continue;              // never touch our own bar
      var txt = (a.textContent || "").toLowerCase();
      if (txt.indexOf("home") === -1) continue;           // leave logo/wordmark links alone
      if (a.querySelector("img, svg")) continue;          // leave crest links alone
      a.style.display = "none";
      a.setAttribute("data-mzn-hidden", "1");
      // Tidy up a separator left dangling next to it (e.g. the "/" breadcrumb).
      var sib = a.nextElementSibling;
      if (sib && /^[\/|·>-]+$/.test((sib.textContent || "").trim())) sib.style.display = "none";
    }
  }

  /* ---------- build ------------------------------------------------------- */

  function caret() {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("class", "mzn-caret");
    s.setAttribute("viewBox", "0 0 8 5");
    s.setAttribute("fill", "none");
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", "M1 1l3 3 3-3");
    p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "1.6");
    p.setAttribute("stroke-linecap", "round");
    p.setAttribute("stroke-linejoin", "round");
    s.appendChild(p);
    return s;
  }

  function build(data) {
    var style = el("style");
    style.id = "mzn-nav-style";
    style.textContent = CSS;
    document.head.appendChild(style);

    var nav = el("nav");
    nav.id = NAV_ID;
    nav.setAttribute("aria-label", "Portal navigation");

    var bar = el("div", "mzn-bar");

    // brand / home
    var brand = el("a", "mzn-brand");
    brand.href = "/";
    brand.title = "Portal home";
    var logo = el("img");
    logo.src = "/logo.svg";
    logo.alt = "";
    brand.appendChild(logo);
    brand.appendChild(el("span", null, "Mizzentop Admin"));
    bar.appendChild(brand);

    // hamburger
    var burger = el("button", "mzn-btn mzn-burger");
    burger.type = "button";
    burger.setAttribute("aria-label", "Menu");
    burger.textContent = "☰  Menu";
    bar.appendChild(burger);

    var links = el("div", "mzn-links");
    var menus = [];

    (data.groups || []).forEach(function (group) {
      if (!group.pages || !group.pages.length) return;

      var wrap = el("div", "mzn-menu");

      var btn = el("button", "mzn-btn");
      btn.type = "button";
      btn.setAttribute("aria-haspopup", "true");
      btn.setAttribute("aria-expanded", "false");
      btn.appendChild(document.createTextNode(group.label));
      btn.appendChild(caret());

      var drop = el("div", "mzn-drop");
      drop.setAttribute("role", "menu");

      // group label, shown only in the mobile stacked view
      wrap.appendChild(el("span", "mzn-grouplabel", group.label));

      var containsHere = false;
      group.pages.forEach(function (page) {
        var a = el("a");
        a.href = page.href;
        a.setAttribute("role", "menuitem");
        a.appendChild(el("span", "mzn-dot"));
        a.appendChild(document.createTextNode(page.label));
        if (normalize(page.href) === HERE) {
          a.classList.add("mzn-current");
          containsHere = true;
        }
        drop.appendChild(a);
      });

      if (containsHere) btn.classList.add("mzn-here");

      wrap.appendChild(btn);
      wrap.appendChild(drop);
      links.appendChild(wrap);

      menus.push(wrap);

      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var isOpen = wrap.classList.contains("open");
        closeAll();
        if (!isOpen) {
          wrap.classList.add("open");
          btn.setAttribute("aria-expanded", "true");
        }
      });
    });

    bar.appendChild(links);
    bar.appendChild(el("div", "mzn-spacer"));

    if (data.user && (data.user.name || data.user.email)) {
      bar.appendChild(el("span", "mzn-who", data.user.name || data.user.email));
    }
    var out = el("a", "mzn-out", "Sign Out");
    out.href = "/auth/logout";
    bar.appendChild(out);

    nav.appendChild(bar);
    document.body.insertBefore(nav, document.body.firstChild);

    function closeAll() {
      menus.forEach(function (m) {
        m.classList.remove("open");
        var b = m.querySelector(".mzn-btn");
        if (b) b.setAttribute("aria-expanded", "false");
      });
    }

    burger.addEventListener("click", function (e) {
      e.stopPropagation();
      nav.classList.toggle("mzn-open");
    });

    document.addEventListener("click", function (e) {
      if (!e.target.closest || !e.target.closest("#" + NAV_ID)) {
        closeAll();
        nav.classList.remove("mzn-open");
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeAll();
        nav.classList.remove("mzn-open");
      }
    });

    hideLegacyHomeLinks();
  }

  /* ---------- boot -------------------------------------------------------- */

  function boot() {
    fetch("/api/nav", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) { if (data && data.groups) build(data); })
      .catch(function () { /* nav is an enhancement; never break the page */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
