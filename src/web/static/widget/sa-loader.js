/* Shopping Assistant loader.
   Renders only the launcher. The iframe, and everything inside it, waits
   for a tap. No socket, no fetch, no audio, no permission. */
(function () {
  "use strict";
  var script = document.currentScript;
  var origin = script && script.getAttribute("data-sa-origin");
  if (!origin) return;
  origin = origin.replace(/\/$/, "");
  var HOST_KEY = "sa_conv";
  var root = document.createElement("div");
  root.id = "sa-launcher-root";
  var parent = document.body || document.documentElement;
  parent.appendChild(root);
  var shadow = root.attachShadow({ mode: "open" });
  shadow.innerHTML = [
    "<style>",
    ":host{all:initial;}",
    ".sa-wrap{position:fixed;right:20px;bottom:20px;right:max(20px,env(safe-area-inset-right));bottom:max(20px,env(safe-area-inset-bottom));z-index:2147483000;display:flex;align-items:center;gap:8px;}",
    ".sa-ask{background:#F36F21;color:#fff;border-radius:999px;padding:8px 12px;font:600 14px/1.2 'Segoe UI',sans-serif;}",
    "button{width:56px;height:56px;border-radius:999px;border:0;background:var(--sa-accent,#F36F21);box-shadow:0 8px 24px rgba(243,111,33,.28);cursor:pointer;}",
    "button:focus-visible{outline:3px solid var(--sa-focus,#F2A541);outline-offset:2px;}",
    "iframe{border:0;background:#fff;}",
    "@media (prefers-reduced-motion: reduce){* {animation:none !important;transition:none !important;}}",
    "</style>",
    "<div class='sa-wrap'>",
    "<span class='sa-ask' id='sa-ask'>Ask</span>",
    "<button type='button' aria-label='Open Shopping Assistant chat' aria-expanded='false'>",
    "<svg viewBox='0 0 32 32' width='28' height='28' aria-hidden='true'>",
    "<path fill='#fff' d='M7 6h14a5 5 0 0 1 5 5v7a5 5 0 0 1-5 5h-5.2l-3.2 3.4a1 1 0 0 1-1.7-.7V23H7a5 5 0 0 1-5-5v-7a5 5 0 0 1 5-5z'/>",
    "<circle cx='12' cy='14.5' r='1.3' fill='#F36F21'/>",
    "</svg></button></div>"
  ].join("");
  var button = shadow.querySelector("button");
  var ask = shadow.getElementById("sa-ask");
  var frame = null;
  var open = false;
  var nonce = "";
  var seen = 0;
  var windowStart = 0;
  try {
    if (localStorage.getItem("sa_ask_seen") === "1" && ask) ask.hidden = true;
  } catch (err) { /* storage blocked: the pill stays */ }

  function hostSession() {
    try {
      var key = "sa_host_session";
      var existing = sessionStorage.getItem(key);
      if (existing) return existing;
      var made = "hs_" + Math.random().toString(16).slice(2, 10);
      sessionStorage.setItem(key, made);
      return made;
    } catch (err) {
      return "";
    }
  }

  function restoreToken() {
    try { return sessionStorage.getItem(HOST_KEY) || ""; }
    catch (err) { return ""; }
  }

  function post(payload) {
    if (!frame || !frame.contentWindow) return;
    frame.contentWindow.postMessage(payload, origin);
  }

  function allow(data) {
    var now = Date.now();
    if (now - windowStart > 10000) { windowStart = now; seen = 0; }
    seen += 1;
    if (seen > 20) return false;
    if (!data || data.v !== 1 || typeof data.type !== "string") return false;
    var keys = Object.keys(data);
    var allowed = {
      "sa.ready": ["type", "v", "nonce"],
      "sa.persist": ["type", "v", "token", "nonce"],
      "sa.navigate": ["type", "v", "product_id", "variant_id", "nonce"],
      "sa.layout": ["type", "v", "mode", "modal", "unread", "nonce"],
      "sa.close": ["type", "v", "nonce"]
    }[data.type];
    if (!allowed) return false;
    for (var i = 0; i < keys.length; i++) if (allowed.indexOf(keys[i]) < 0) return false;
    return true;
  }

  function setInert(on) {
    var nodes = document.body ? document.body.children : [];
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i] === root) continue;
      if (on) nodes[i].setAttribute("inert", "");
      else nodes[i].removeAttribute("inert");
    }
  }

  function embedMode() {
    return window.innerWidth < 768 ? "sheet" : "panel";
  }

  function place() {
    if (!frame) return;
    var narrow = embedMode() === "sheet";
    if (narrow) {
      frame.style.cssText = "position:fixed;inset:0;width:100%;height:100dvh;z-index:2147483000;border:0;background:#fff;";
    } else {
      frame.style.cssText = "position:fixed;right:20px;bottom:88px;width:360px;height:min(560px,78vh);z-index:2147483000;border:0;border-radius:16px;box-shadow:0 16px 48px rgba(20,34,38,.18);background:#fff;";
    }
  }

  function openPanel() {
    if (!frame) {
      frame = document.createElement("iframe");
      frame.title = "Shopping Assistant chat";
      frame.src = origin + "/widget/v1/";
      frame.setAttribute(
        "sandbox",
        "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
      );
      shadow.appendChild(frame);
    }
    frame.hidden = false;
    open = true;
    button.setAttribute("aria-expanded", "true");
    place();
    try { localStorage.setItem("sa_ask_seen", "1"); } catch (err) { /* ignore */ }
    if (ask) ask.hidden = true;
  }

  function closePanel() {
    open = false;
    if (frame) frame.hidden = true;
    button.setAttribute("aria-expanded", "false");
    setInert(false);
    button.focus();
  }

  button.addEventListener("click", function () {
    if (open) closePanel();
    else openPanel();
  });
  window.addEventListener("resize", function () {
    place();
    if (open && nonce) post({ type: "sa.embed", v: 1, nonce: nonce, embed: embedMode() });
  });

  window.addEventListener("message", function (event) {
    if (event.origin !== origin) return;
    if (!frame || event.source !== frame.contentWindow) return;
    if (!allow(event.data)) return;
    var data = event.data;
    if (data.type === "sa.ready") {
      nonce = data.nonce || "";
      var product = null;
      if (script.getAttribute("data-product-id")) {
        product = {
          product_id: script.getAttribute("data-product-id"),
          variant_id: script.getAttribute("data-variant-id") || ""
        };
      }
      var init = {
        type: "sa.init",
        v: 1,
        nonce: nonce,
        locale: "en-IN",
        host_session: hostSession()
      };
      var pin = script.getAttribute("data-pincode");
      if (pin && /^\d{6}$/.test(pin)) init.pincode = pin;
      if (product) init.product = product;
      var token = restoreToken();
      if (token) init.restore_token = token;
      init.embed = embedMode();
      post(init);
    } else if (nonce && data.nonce && data.nonce !== nonce) {
      return;
    } else if (data.type === "sa.persist" && typeof data.token === "string" && data.token.length <= 512) {
      try {
        if (!data.token) sessionStorage.removeItem(HOST_KEY);
        else sessionStorage.setItem(HOST_KEY, data.token);
      } catch (err) { /* partitioned */ }
    } else if (data.type === "sa.layout") {
      setInert(!!data.modal);
      if (data.mode === "closed" && frame) frame.hidden = true;
      if (data.unread) {
        button.setAttribute("aria-label", "Open Shopping Assistant chat, " + data.unread + " new reply");
      }
    } else if (data.type === "sa.navigate") {
      document.dispatchEvent(new CustomEvent("sa-navigate", {
        detail: { product_id: data.product_id, variant_id: data.variant_id }
      }));
    } else if (data.type === "sa.close") {
      closePanel();
    }
  });

  var lastVariant = script.getAttribute("data-variant-id") || "";
  var lastProduct = script.getAttribute("data-product-id") || "";
  if (window.MutationObserver) {
    new MutationObserver(function () {
      var nextVariant = script.getAttribute("data-variant-id") || "";
      var nextProduct = script.getAttribute("data-product-id") || "";
      if (nextVariant === lastVariant && nextProduct === lastProduct) return;
      lastVariant = nextVariant;
      lastProduct = nextProduct;
      if (!open || !nonce || !frame) return;
      if (!nextProduct) return;
      post({
        type: "sa.context",
        v: 1,
        nonce: nonce,
        product: { product_id: nextProduct, variant_id: nextVariant }
      });
    }).observe(script, {
      attributes: true,
      attributeFilter: ["data-variant-id", "data-product-id"]
    });
  }
})();
