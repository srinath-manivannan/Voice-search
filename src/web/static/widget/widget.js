/* Shopping Assistant panel. The welcome is already in the document.
   The socket opens only when the shopper sends. */
(function () {
  "use strict";
  var configNode = document.getElementById("sa-config");
  var config = {};
  try { config = JSON.parse(configNode.textContent || "{}"); } catch (err) { config = {}; }
  var ASSISTANT = config.assistant_name || "Shopping Assistant";
  var HEADLINE = config.headline || ("Hi! I'm " + ASSISTANT + ". What can I help you with?");
  var LEAD = (document.getElementById("sa-lead") || {}).textContent || "";
  var HOSTS = config.host_origins || [];
  var SUGGESTIONS = config.suggestions || [
    "Is this available?",
    "Best phone under ₹25,000",
    "Compare similar",
    "EMI options"
  ];
  var BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
  var logEl = document.getElementById("sa-log");
  var chips = document.getElementById("sa-chips");
  var cardsEl = document.getElementById("sa-cards");
  var form = document.getElementById("sa-form");
  var input = document.getElementById("sa-input");
  var statusEl = document.getElementById("sa-status");
  var alertEl = document.getElementById("sa-alert");
  var title = document.getElementById("sa-title");
  if (title) title.textContent = ASSISTANT;
  document.title = ASSISTANT;
  // A visit straight to this page (not framed by a storefront) shows the chat
  // as a corner icon. Clicking it opens the same panel; close collapses it.
  var standalone = !window.parent || window.parent === window;
  var bubble = document.getElementById("sa-bubble");
  if (standalone) {
    document.documentElement.setAttribute("data-standalone", "1");
    document.documentElement.setAttribute("data-launcher", "closed");
  }
  if (config.color) {
    document.documentElement.style.setProperty("--sa-accent", config.color);
    document.documentElement.style.setProperty("--brand", config.color);
  }
  var surfaces = config.surfaces || ["chat"];

  var parentOrigin = "";
  var nonce = "";
  var token = "";
  var lastSeq = 0;
  var socket = null;
  var outbox = null;
  var attempts = 0;
  var panelOpen = true;
  var product = null;
  var modal = false;
  var hostEmbed = "";
  var slowTimer = 0;

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0;
      return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  function tamil(text) {
    return /[\u0B80-\u0BFF]/.test(text || "");
  }

  function post(payload) {
    if (!window.parent || window.parent === window) return;
    // sa.ready is the first message, sent before the host has introduced
    // itself, so there is no saved origin yet. The parent is whoever was
    // allowed to frame this document. After sa.init, every later message
    // is addressed to that exact origin.
    var target = parentOrigin;
    if (!target && document.referrer) {
      try { target = new URL(document.referrer).origin; } catch (err) { target = ""; }
    }
    if (!target) target = "*";
    payload.v = 1;
    payload.nonce = nonce;
    window.parent.postMessage(payload, target);
  }

  function hostAllowed(origin) {
    if (!HOSTS.length) return origin.indexOf("http://localhost") === 0 || origin.indexOf("http://127.0.0.1") === 0;
    return HOSTS.indexOf(origin) >= 0;
  }

  function knownKeys(type) {
    if (type === "sa.init") return ["type", "v", "nonce", "host_session", "locale", "pincode", "product", "restore_token", "embed"];
    if (type === "sa.context") return ["type", "v", "nonce", "product"];
    if (type === "sa.embed") return ["type", "v", "nonce", "embed"];
    return null;
  }

  function validId(value) {
    return typeof value === "string" && /^[A-Za-z0-9_-]{1,40}$/.test(value);
  }

  function acceptHost(data, origin) {
    if (!hostAllowed(origin)) return false;
    if (!data || data.v !== 1) return false;
    var keys = knownKeys(data.type);
    if (!keys) return false;
    var present = Object.keys(data);
    for (var i = 0; i < present.length; i++) if (keys.indexOf(present[i]) < 0) return false;
    if (nonce && data.nonce !== nonce) return false;
    if (data.product) {
      if (!data.product || typeof data.product !== "object") return false;
      if (!validId(data.product.product_id)) return false;
      if (data.product.variant_id && !validId(data.product.variant_id)) return false;
    }
    if (data.embed && data.embed !== "panel" && data.embed !== "sheet") return false;
    if (data.type === "sa.embed" && !data.embed) return false;
    if (data.restore_token && String(data.restore_token).length > 512) return false;
    if (data.pincode && !/^\d{6}$/.test(data.pincode)) return false;
    return true;
  }

  function setStatus(text) { statusEl.textContent = text || ""; }
  function setAlert(text) { alertEl.textContent = text || ""; }

  function enableComposer() {
    input.disabled = false;
    form.querySelector(".sa-send").disabled = false;
  }

  function focusables() {
    return Array.prototype.slice.call(document.querySelectorAll(
      "button, textarea, a[href]"
    )).filter(function (el) { return !el.disabled && el.offsetParent !== null; });
  }

  function applyMode() {
    // The iframe is 380px wide on a desktop storefront, so its own
    // innerWidth is not the host viewport. The loader says which layout
    // the host is actually showing. Until that arrives, a framed page
    // stays a non-modal dialog rather than trapping a desktop shopper.
    var mode = hostEmbed;
    var framed = window.parent && window.parent !== window;
    if (mode !== "panel" && mode !== "sheet") {
      mode = !framed && window.innerWidth < 768 ? "sheet" : "panel";
    }
    modal = mode === "sheet";
    var dialog = document.body;
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-labelledby", "sa-title");
    if (modal) dialog.setAttribute("aria-modal", "true");
    else dialog.removeAttribute("aria-modal");
    document.documentElement.setAttribute("data-embed", mode);
    post({ type: "sa.layout", mode: modal ? "sheet" : "panel", modal: modal });
  }

  function pageHost() {
    if (parentOrigin) return parentOrigin;
    if (!window.parent || window.parent === window) return location.origin;
    return "";
  }

  function clock() {
    try {
      return new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    } catch (err) {
      return "";
    }
  }

  function addBubble(text, mine) {
    var wrap = document.createElement("div");
    wrap.className = "sa-turn" + (mine ? " mine" : "");
    var node = document.createElement("p");
    node.className = "sa-msg " + (mine ? "user" : "assistant");
    node.textContent = text;
    if (mine && tamil(text)) node.lang = "ta";
    var time = document.createElement("time");
    time.className = "sa-time";
    time.dateTime = new Date().toISOString();
    time.textContent = clock();
    wrap.appendChild(node);
    wrap.appendChild(time);
    logEl.appendChild(wrap);
    logEl.scrollTop = logEl.scrollHeight;
    return node;
  }

  var PLACEHOLDER_IMG = "data:image/svg+xml," + encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'>" +
    "<rect width='80' height='80' rx='12' fill='#F6F7F9'/>" +
    "<rect x='18' y='22' width='44' height='36' rx='6' fill='#E6E8EC'/>" +
    "</svg>"
  );

  function showTyping(on) {
    var node = document.getElementById("sa-typing");
    if (!on) {
      if (node) node.remove();
      return;
    }
    if (!node) {
      node = document.createElement("p");
      node.id = "sa-typing";
      node.className = "sa-typing";
      node.setAttribute("aria-hidden", "true");
      node.innerHTML = "<i></i><i></i><i></i>";
      logEl.appendChild(node);
    }
    logEl.scrollTop = logEl.scrollHeight;
  }

  function fromCards(cards) {
    return (cards || []).map(function (card) {
      var price = card.price && card.price.selling_inr;
      return {
        brand: card.brand || "",
        image_url: card.image_url || "",
        title: card.model_name || "",
        spec: card.benefit || card.variant_label || "",
        price_label: price ? "₹" + Number(price).toLocaleString("en-IN") : "",
        url: card.url || "",
        product_id: card.sku_id || "",
        variant_id: card.variant_id || ""
      };
    });
  }

  function viewProduct(product) {
    var framed = window.parent && window.parent !== window;
    if (framed && product.product_id) {
      post({ type: "sa.navigate", product_id: product.product_id, variant_id: product.variant_id || "" });
      return;
    }
    if (product.url) window.open(product.url, "_blank", "noopener,noreferrer");
  }

  function renderProducts(messageNode, products) {
    if (!messageNode || !products || !products.length) return;
    var row = document.createElement("div");
    row.className = "sa-products";
    row.setAttribute("role", "list");
    products.forEach(function (product) {
      var item = document.createElement("article");
      item.className = "sa-product";
      item.setAttribute("role", "listitem");
      var img = document.createElement("img");
      img.alt = "";
      img.src = product.image_url || PLACEHOLDER_IMG;
      img.addEventListener("error", function () { img.src = PLACEHOLDER_IMG; });
      item.appendChild(img);
      if (product.brand) {
        var badge = document.createElement("span");
        badge.className = "sa-badge";
        badge.textContent = product.brand;
        item.appendChild(badge);
      }
      var heading = document.createElement("h3");
      heading.textContent = product.title || "";
      item.appendChild(heading);
      var spec = document.createElement("p");
      spec.className = "sa-spec";
      spec.textContent = product.spec || "";
      item.appendChild(spec);
      var price = document.createElement("p");
      price.className = "sa-rupee";
      price.textContent = product.price_label || "";
      item.appendChild(price);
      var actions = document.createElement("div");
      actions.className = "sa-actions";
      function act(label, className, click) {
        var button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        if (className) button.className = className;
        button.addEventListener("click", click);
        actions.appendChild(button);
      }
      var title = product.title || "this product";
      act("View Details", "", function () {
        send("Tell me the full specs, current offers, and store availability for " + title + ".");
      });
      act("Show Similar", "", function () {
        send("Show similar alternatives to " + title + " in the same budget.");
      });
      act("View", "sa-view", function () { viewProduct(product); });
      item.appendChild(actions);
      row.appendChild(item);
    });
    var wrap = messageNode.parentNode;
    var time = wrap && wrap.querySelector("time");
    if (wrap && time) wrap.insertBefore(row, time);
    else if (wrap) wrap.appendChild(row);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function renderCards(cards) {
    var nodes = logEl.querySelectorAll(".sa-msg.assistant");
    var last = nodes[nodes.length - 1];
    renderProducts(last, fromCards(cards));
  }

  function renderSuggestions(list) {
    chips.innerHTML = "";
    (list && list.length ? list : SUGGESTIONS).forEach(function (label) {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", function () { send(label); });
      chips.appendChild(button);
    });
  }

  function saveOutbox() {
    try { sessionStorage.setItem("sa_outbox", JSON.stringify(outbox)); } catch (err) { /* ignore */ }
  }

  function loadOutbox() {
    try { outbox = JSON.parse(sessionStorage.getItem("sa_outbox") || "null"); }
    catch (err) { outbox = null; }
  }

  function jitter(ms) { return Math.round(ms * (0.8 + Math.random() * 0.4)); }

  function connectAndSend() {
    if (!outbox) return;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "user_message", message_id: outbox.message_id, text: outbox.text }));
      return;
    }
    if (socket && socket.readyState === WebSocket.CONNECTING) return;
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat?v=2";
    var current = new WebSocket(url);
    socket = current;
    current.onopen = function () {
      if (socket !== current) return;
      attempts = 0;
      setAlert("");
      socket.send(JSON.stringify({
        type: "hello",
        v: 2,
        token: token || null,
        last_seq: lastSeq,
        context: {
          host_origin: pageHost(),
          host_session: sessionHost(),
          pincode: (config.pincode || undefined),
          product: product || undefined
        }
      }));
    };
    current.onmessage = function (event) {
      if (socket !== current) return;
      var message = JSON.parse(event.data);
      if (message.seq && message.seq <= lastSeq && message.type !== "welcome") return;
      if (message.seq) lastSeq = Math.max(lastSeq, message.seq);
      onServer(message);
    };
    current.onclose = function () {
      if (socket !== current) return;
      enableComposer();
      socket = null;
      if (!outbox || outbox.status === "done") return;
      if (!navigator.onLine) {
        setAlert("You're offline. We'll send your message when you're back online.");
        return;
      }
      if (attempts >= BACKOFF_MS.length) {
        setAlert("Message not sent. ");
        var retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Retry";
        retry.setAttribute("aria-label", "Retry sending your message");
        retry.addEventListener("click", function () { attempts = 0; connectAndSend(); });
        alertEl.appendChild(retry);
        return;
      }
      setStatus("Reconnecting…");
      var wait = jitter(BACKOFF_MS[attempts]);
      attempts += 1;
      window.setTimeout(connectAndSend, wait);
    };
  }

  function onServer(message) {
    if (message.type === "welcome") {
      token = message.token || token;
      post({ type: "sa.persist", token: token });
      (message.replay || []).forEach(onServer);
      if (outbox && outbox.status !== "done") {
        socket.send(JSON.stringify({ type: "user_message", message_id: outbox.message_id, text: outbox.text }));
      }
      return;
    }
    if (message.type === "ack") {
      setStatus("Checking…");
      showTyping(true);
      clearTimeout(slowTimer);
      slowTimer = window.setTimeout(function () {
        setStatus("Still checking live prices and stock…");
      }, 6000);
      return;
    }
    if (message.type === "delta") return;
    if (message.type === "final") {
      clearTimeout(slowTimer);
      showTyping(false);
      setStatus("");
      var text = message.display && message.display.text;
      var node = text ? addBubble(text, false) : null;
      var products = (message.products && message.products.length) ? message.products : fromCards(message.cards);
      if (node) renderProducts(node, products);
      if (message.suggestions && message.suggestions.length) renderSuggestions(message.suggestions);
      return;
    }
    if (message.type === "done") {
      clearTimeout(slowTimer);
      showTyping(false);
      setStatus("");
      enableComposer();
      if (outbox) { outbox.status = "done"; saveOutbox(); outbox = null; }
      try { sessionStorage.removeItem("sa_outbox"); } catch (err) { /* ignore */ }
      return;
    }
    if (message.type === "error") {
      clearTimeout(slowTimer);
      showTyping(false);
      enableComposer();
      setAlert(message.message || "Sorry, something went wrong on our side. You weren't charged.");
    }
  }

  function sessionHost() {
    try { return sessionStorage.getItem("sa_host_session") || ""; }
    catch (err) { return ""; }
  }

  function send(text) {
    text = (text || "").trim();
    if (!text) return;
    // A second tap while this message is still in flight would mint a new
    // id and a second reply. The composer is disabled until the socket
    // closes or the reply finishes, and the guard covers the tap that
    // lands in the same turn as the first.
    if (outbox && outbox.status !== "done") return;
    input.disabled = true;
    var sendButton = form.querySelector(".sa-send");
    if (sendButton) sendButton.disabled = true;
    outbox = { message_id: uuid(), text: text, created_at: Date.now(), status: "sending" };
    saveOutbox();
    addBubble(text, true);
    input.value = "";
    setAlert("");
    if (!navigator.onLine) {
      setAlert("You're offline. We'll send your message when you're back online.");
      return;
    }
    connectAndSend();
  }

  function redraw(messages) {
    messages.forEach(function (item) {
      if (!item.text) return;
      var node = addBubble(item.text, item.role === "user");
      var products = (item.products && item.products.length) ? item.products : fromCards(item.cards);
      if (item.role !== "user") renderProducts(node, products);
    });
  }

  function loadHistory(restore) {
    if (!restore || !parentOrigin) return;
    fetch("/api/v1/widget/history", {
      headers: { "Authorization": "Bearer " + restore, "X-Sa-Host": parentOrigin }
    }).then(function (response) { return response.json(); }).then(function (body) {
      if (body.token) {
        token = body.token;
        post({ type: "sa.persist", token: token });
      }
      redraw(body.messages || []);
    }).catch(function () { /* history is optional; the welcome is already on screen */ });
  }

  SUGGESTIONS.forEach(function () {});
  renderSuggestions(SUGGESTIONS);

  // The welcome is for the first visit. After that the chat opens on the
  // conversation itself, not on the same greeting again. Kept in the
  // browser, so a closed and reopened panel remembers it.
  var welcome = document.getElementById("sa-welcome");
  var WELCOME_KEY = "sa_welcome_seen";
  var seenBefore = false;
  try { seenBefore = window.localStorage.getItem(WELCOME_KEY) === "1"; } catch (err) { /* storage blocked: show it */ }
  if (welcome && seenBefore) welcome.hidden = true;
  if (!seenBefore) {
    try { window.localStorage.setItem(WELCOME_KEY, "1"); } catch (err) { /* storage blocked */ }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send(input.value);
    }
  });
  function setLauncher(open) {
    document.documentElement.setAttribute("data-launcher", open ? "open" : "closed");
    if (bubble) bubble.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) input.focus(); else if (bubble) bubble.focus();
  }
  if (standalone && bubble) {
    bubble.addEventListener("click", function () { setLauncher(true); });
  }
  document.getElementById("sa-close").addEventListener("click", function () {
    if (standalone) { setLauncher(false); return; }
    panelOpen = false;
    post({ type: "sa.close" });
  });
  document.getElementById("sa-min").addEventListener("click", function () {
    if (standalone) { setLauncher(false); return; }
    panelOpen = false;
    post({ type: "sa.layout", mode: "closed", modal: false });
    window.setTimeout(function () {
      if (!panelOpen && socket) socket.close(1000, "panel closed");
    }, 60000);
  });
  input.addEventListener("input", function () {
    input.style.height = "44px";
    var next = Math.min(input.scrollHeight, 120);
    input.style.height = Math.max(44, next) + "px";
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (menuPop && !menuPop.hidden) {
        menuPop.hidden = true;
        if (menu) menu.setAttribute("aria-expanded", "false");
        return;
      }
      post({ type: "sa.close" });
      return;
    }
    if (!modal || event.key !== "Tab") return;
    var items = focusables();
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  window.addEventListener("offline", function () {
    setAlert("You're offline. We'll send your message when you're back online.");
  });
  window.addEventListener("online", function () {
    setAlert("");
    if (outbox && outbox.status !== "done") connectAndSend();
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", function () {
      document.documentElement.style.setProperty("--sa-vvh", window.visualViewport.height + "px");
      input.scrollIntoView({ block: "nearest" });
    });
  }
  var handle = document.getElementById("sa-handle");
  var startY = 0;
  handle.addEventListener("touchstart", function (event) { startY = event.touches[0].clientY; }, { passive: true });
  handle.addEventListener("touchend", function (event) {
    if (event.changedTouches[0].clientY - startY > 40) post({ type: "sa.close" });
  });

  function freshChat() {
    token = "";
    lastSeq = 0;
    outbox = null;
    try { sessionStorage.removeItem("sa_outbox"); } catch (err) { /* ignore */ }
    if (socket && socket.readyState !== WebSocket.CLOSED) {
      try { socket.close(1000, "new chat"); } catch (err2) { /* ignore */ }
    }
    socket = null;
    var turns = logEl.querySelectorAll(".sa-turn, .sa-typing");
    for (var i = 0; i < turns.length; i++) turns[i].remove();
    cardsEl.innerHTML = "";
    setStatus("");
    setAlert("");
    enableComposer();
    post({ type: "sa.persist", token: "" });
  }

  var menu = document.getElementById("sa-menu");
  var menuPop = document.getElementById("sa-menu-pop");
  if (menu && menuPop) {
    menuPop.hidden = true;
    menu.setAttribute("aria-expanded", "false");
    menu.addEventListener("click", function (event) {
      event.stopPropagation();
      var open = menuPop.hidden;
      menuPop.hidden = !open;
      menu.setAttribute("aria-expanded", open ? "true" : "false");
    });
    menuPop.addEventListener("click", function (event) { event.stopPropagation(); });
    document.addEventListener("click", function () {
      if (menuPop.hidden) return;
      menuPop.hidden = true;
      menu.setAttribute("aria-expanded", "false");
    });
    document.getElementById("sa-new").addEventListener("click", function () {
      menuPop.hidden = true;
      menu.setAttribute("aria-expanded", "false");
      freshChat();
    });
    document.getElementById("sa-clear").addEventListener("click", function () {
      menuPop.hidden = true;
      menu.setAttribute("aria-expanded", "false");
      freshChat();
    });
  }

  var tabs = document.getElementById("sa-tabs");
  var stage = document.getElementById("sa-stage");
  var chatRoot = document.getElementById("sa-chat-root");
  if (tabs && stage && surfaces.indexOf("guided") >= 0 && surfaces.indexOf("voice") >= 0) {
    tabs.hidden = false;
    tabs.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("button") : null;
      if (!button || !tabs.contains(button)) return;
      var name = button.getAttribute("data-surface");
      var all = tabs.querySelectorAll("button");
      for (var i = 0; i < all.length; i++) {
        all[i].setAttribute("aria-selected", all[i] === button ? "true" : "false");
      }
      if (name === "chat") {
        stage.hidden = true;
        if (chatRoot) chatRoot.hidden = false;
        return;
      }
      if (chatRoot) chatRoot.hidden = true;
      stage.hidden = false;
      var frame = stage.querySelector("iframe");
      if (!frame) {
        frame = document.createElement("iframe");
        frame.title = "Shopping Assistant";
        stage.appendChild(frame);
      }
      var next = location.origin + "/?embed=1&panel=" + encodeURIComponent(name || "chat");
      if (frame.getAttribute("src") !== next) frame.src = next;
    });
  }

  nonce = uuid();
  applyMode();
  window.addEventListener("resize", applyMode);
  post({ type: "sa.ready" });
  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    if (!acceptHost(event.data, event.origin)) return;
    if (!parentOrigin) parentOrigin = event.origin;
    var data = event.data;
    if (data.type === "sa.init") {
      nonce = data.nonce || nonce;
      if (data.embed === "panel" || data.embed === "sheet") hostEmbed = data.embed;
      product = data.product || null;
      if (data.pincode) config.pincode = data.pincode;
      try { sessionStorage.setItem("sa_host_session", data.host_session || ""); } catch (err) { /* ignore */ }
      if (data.restore_token) {
        token = data.restore_token;
        loadHistory(token);
      }
      if (product && product.variant_id) {
        document.documentElement.setAttribute("data-variant", product.variant_id);
      }
      loadOutbox();
      if (outbox && outbox.status !== "done") connectAndSend();
      applyMode();
      document.documentElement.setAttribute("data-ready", "1");
      input.focus();
    } else if (data.type === "sa.embed") {
      hostEmbed = data.embed;
      applyMode();
    } else if (data.type === "sa.context") {
      product = data.product || null;
      if (product && product.variant_id) {
        document.documentElement.setAttribute("data-variant", product.variant_id);
      }
      // The open socket still has the previous variant on its hello.
      // Drop it so the next send resumes with this one. Spend stays on
      // the conversation token.
      if (socket && socket.readyState !== WebSocket.CLOSED) {
        var previous = socket;
        socket = null;
        try { previous.close(1000, "context changed"); } catch (err) { /* already closing */ }
      }
    }
  });
  enableComposer();
})();
