/* Shopping Assistant panel. The welcome is already in the document.
   The socket opens only when the shopper sends. */
(function () {
  "use strict";
  var configNode = document.getElementById("sa-config");
  var config = {};
  try { config = JSON.parse(configNode.textContent || "{}"); } catch (err) { config = {}; }
  var ASSISTANT = config.assistant_name || "Shopping Assistant";
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
    if (!parentOrigin || !window.parent || window.parent === window) return;
    payload.v = 1;
    payload.nonce = nonce;
    window.parent.postMessage(payload, parentOrigin);
  }

  function hostAllowed(origin) {
    if (!HOSTS.length) return origin.indexOf("http://localhost") === 0 || origin.indexOf("http://127.0.0.1") === 0;
    return HOSTS.indexOf(origin) >= 0;
  }

  function knownKeys(type) {
    if (type === "sa.init") return ["type", "v", "nonce", "host_session", "locale", "pincode", "product", "restore_token"];
    if (type === "sa.context") return ["type", "v", "nonce", "product"];
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
      if (!validId(data.product.product_id)) return false;
      if (data.product.variant_id && !validId(data.product.variant_id)) return false;
    }
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
    modal = window.innerWidth < 768;
    var dialog = document.body;
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-labelledby", "sa-title");
    if (modal) dialog.setAttribute("aria-modal", "true");
    else dialog.removeAttribute("aria-modal");
    post({ type: "sa.layout", mode: modal ? "sheet" : "panel", modal: modal });
  }

  function addBubble(text, mine) {
    var node = document.createElement("p");
    node.className = "sa-msg " + (mine ? "user" : "assistant");
    node.textContent = text;
    if (mine && tamil(text)) node.lang = "ta";
    logEl.appendChild(node);
    logEl.scrollTop = logEl.scrollHeight;
    return node;
  }

  function renderCards(cards) {
    cardsEl.innerHTML = "";
    (cards || []).forEach(function (card) {
      var item = document.createElement("article");
      item.className = "sa-card";
      item.setAttribute("role", "listitem");
      var price = card.price && card.price.selling_inr ? "₹" + Number(card.price.selling_inr).toLocaleString("en-IN") : "Price not confirmed";
      var stock = (card.stock && card.stock.label) || "Stock not confirmed right now";
      item.setAttribute("aria-label", "Option " + card.ref + ", " + card.model_name + ", " + price + ", " + stock);
      item.innerHTML = "<div class='sa-letter'>" + card.ref + "</div><div><h2></h2><p class='sa-price'></p><p class='sa-benefit'></p><p class='sa-stock'></p><div class='sa-actions'></div></div>";
      item.querySelector("h2").textContent = card.model_name || "";
      item.querySelector(".sa-price").textContent = price + (card.variant_label ? " · " + card.variant_label : "");
      item.querySelector(".sa-benefit").textContent = card.benefit || "";
      item.querySelector(".sa-stock").textContent = stock;
      var actions = item.querySelector(".sa-actions");
      var view = document.createElement("button");
      view.type = "button";
      view.textContent = "View";
      view.addEventListener("click", function () {
        post({ type: "sa.navigate", product_id: card.sku_id, variant_id: card.variant_id });
      });
      actions.appendChild(view);
      if (card.offer_line) {
        var offer = document.createElement("p");
        offer.textContent = card.offer_line;
        actions.parentNode.insertBefore(offer, actions);
      }
      if (card.emi_line) {
        var emi = document.createElement("p");
        emi.textContent = card.emi_line;
        actions.parentNode.insertBefore(emi, actions);
      }
      cardsEl.appendChild(item);
    });
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
    var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat";
    socket = new WebSocket(url);
    socket.onopen = function () {
      attempts = 0;
      setAlert("");
      socket.send(JSON.stringify({
        type: "hello",
        v: 2,
        token: token || null,
        last_seq: lastSeq,
        context: {
          host_origin: parentOrigin,
          host_session: sessionHost(),
          pincode: (config.pincode || undefined),
          product: product || undefined
        }
      }));
    };
    socket.onmessage = function (event) {
      var message = JSON.parse(event.data);
      if (message.seq && message.seq <= lastSeq && message.type !== "welcome") return;
      if (message.seq) lastSeq = Math.max(lastSeq, message.seq);
      onServer(message);
    };
    socket.onclose = function () {
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
      if (message.context_label) document.getElementById("sa-context").textContent = "Asking about: " + message.context_label;
      post({ type: "sa.persist", token: token });
      (message.replay || []).forEach(onServer);
      if (outbox && outbox.status !== "done") {
        socket.send(JSON.stringify({ type: "user_message", message_id: outbox.message_id, text: outbox.text }));
      }
      return;
    }
    if (message.type === "ack") {
      setStatus("Checking…");
      clearTimeout(slowTimer);
      slowTimer = window.setTimeout(function () {
        setStatus("Still checking live prices and stock…");
      }, 6000);
      return;
    }
    if (message.type === "delta") return;
    if (message.type === "final") {
      clearTimeout(slowTimer);
      setStatus("");
      var text = message.display && message.display.text;
      if (text) addBubble(text, false);
      renderCards(message.cards || []);
      if (message.suggestions && message.suggestions.length) renderSuggestions(message.suggestions);
      return;
    }
    if (message.type === "done") {
      clearTimeout(slowTimer);
      setStatus("");
      enableComposer();
      if (outbox) { outbox.status = "done"; saveOutbox(); outbox = null; }
      try { sessionStorage.removeItem("sa_outbox"); } catch (err) { /* ignore */ }
      return;
    }
    if (message.type === "error") {
      clearTimeout(slowTimer);
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
    enableComposer();
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
      addBubble(item.text, item.role === "user");
      if (item.cards) renderCards(item.cards);
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
      if (body.context_label) document.getElementById("sa-context").textContent = "Asking about: " + body.context_label;
      redraw(body.messages || []);
    }).catch(function () { /* history is optional; the welcome is already on screen */ });
  }

  SUGGESTIONS.forEach(function () {});
  renderSuggestions(SUGGESTIONS);

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
  document.getElementById("sa-close").addEventListener("click", function () {
    panelOpen = false;
    post({ type: "sa.close" });
  });
  document.getElementById("sa-min").addEventListener("click", function () {
    panelOpen = false;
    post({ type: "sa.layout", mode: "closed", modal: false });
    window.setTimeout(function () {
      if (!panelOpen && socket) socket.close(1000, "panel closed");
    }, 60000);
  });
  document.getElementById("sa-back").addEventListener("click", function () {
    if (product) post({ type: "sa.navigate", product_id: product.product_id, variant_id: product.variant_id });
    post({ type: "sa.close" });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
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
      product = data.product || null;
      if (data.pincode) config.pincode = data.pincode;
      try { sessionStorage.setItem("sa_host_session", data.host_session || ""); } catch (err) { /* ignore */ }
      if (data.restore_token) {
        token = data.restore_token;
        loadHistory(token);
      }
      loadOutbox();
      if (outbox && outbox.status !== "done") connectAndSend();
      input.focus();
    } else if (data.type === "sa.context") {
      product = data.product || product;
      document.getElementById("sa-context").textContent = "Asking about: this page";
    }
  });
  enableComposer();
})();
