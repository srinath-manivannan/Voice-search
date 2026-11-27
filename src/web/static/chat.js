// The chat tab. The welcome is already in the page. The socket opens on send.

const chat = {
  socket: null, greeted: true, turns: 0, token: "", lastSeq: 0, outbox: null,
};

const CHAT_CHIPS = [
  "Help me find the right phone",
  "Best AC for my room",
  "Laptop under ₹50,000",
  "Check store availability",
];

const PRODUCT_PLACEHOLDER = "data:image/svg+xml," + encodeURIComponent(
  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'>" +
  "<rect width='80' height='80' rx='12' fill='#F6F7F9'/>" +
  "<rect x='18' y='22' width='44' height='36' rx='6' fill='#E6E8EC'/>" +
  "</svg>"
);

function chatClock() {
  return new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function chatUuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = Math.random() * 16 | 0;
    return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function bubble(text, mine) {
  const row = document.createElement("div");
  row.className = "msg" + (mine ? " me" : "");
  if (!mine) {
    const av = document.createElement("div");
    av.className = "av";
    av.setAttribute("aria-label", "Shopping Assistant");
    row.appendChild(av);
  }
  const body = document.createElement("div");
  body.className = "bubble";
  body.textContent = text;
  const time = document.createElement("time");
  time.textContent = chatClock();
  body.appendChild(time);
  row.appendChild(body);
  el("messages").appendChild(row);
  el("messages").scrollTop = el("messages").scrollHeight;
  return body;
}

function showChatTyping(on) {
  const existing = document.getElementById("chat-typing");
  if (!on) {
    if (existing) existing.remove();
    return;
  }
  if (existing) return;
  const row = document.createElement("div");
  row.id = "chat-typing";
  row.className = "typing";
  row.setAttribute("aria-hidden", "true");
  row.innerHTML = "<i></i><i></i><i></i>";
  el("messages").appendChild(row);
  el("messages").scrollTop = el("messages").scrollHeight;
}

function productsFrom(message) {
  if (message.products && message.products.length) return message.products;
  return (message.cards || []).map((card) => {
    const price = card.price && card.price.selling_inr;
    return {
      brand: card.brand || "",
      image_url: card.image_url || "",
      title: card.model_name || "",
      spec: card.benefit || card.variant_label || "",
      price_label: price ? "₹" + Number(price).toLocaleString("en-IN") : "",
      url: card.url || "",
      product_id: card.sku_id || "",
      variant_id: card.variant_id || "",
    };
  });
}

function renderChatProducts(body, products) {
  if (!products.length) return;
  const row = document.createElement("div");
  row.className = "chat-products";
  for (const product of products) {
    const card = document.createElement("article");
    card.className = "chat-product";
    const img = document.createElement("img");
    img.alt = "";
    img.src = product.image_url || PRODUCT_PLACEHOLDER;
    img.addEventListener("error", () => { img.src = PRODUCT_PLACEHOLDER; });
    card.appendChild(img);
    if (product.brand) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = product.brand;
      card.appendChild(badge);
    }
    const title = document.createElement("h3");
    title.textContent = product.title || "";
    card.appendChild(title);
    const spec = document.createElement("p");
    spec.textContent = product.spec || "";
    card.appendChild(spec);
    const price = document.createElement("p");
    price.className = "rupee";
    price.textContent = product.price_label || "";
    card.appendChild(price);
    const name = product.title || "this product";
    const details = document.createElement("button");
    details.type = "button";
    details.textContent = "View Details";
    details.addEventListener("click", () => sendChat(
      "Tell me the full specs, current offers, and store availability for " + name + "."
    ));
    const similar = document.createElement("button");
    similar.type = "button";
    similar.textContent = "Show Similar";
    similar.addEventListener("click", () => sendChat(
      "Show similar alternatives to " + name + " in the same budget."
    ));
    const view = document.createElement("button");
    view.type = "button";
    view.className = "view";
    view.textContent = "View";
    view.addEventListener("click", () => {
      if (product.url) window.open(product.url, "_blank", "noopener,noreferrer");
    });
    card.append(details, similar, view);
    row.appendChild(card);
  }
  body.appendChild(row);
}

function connectChat() {
  if (chat.socket && (chat.socket.readyState === WebSocket.OPEN || chat.socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat?v=2";
  const socket = new WebSocket(url);
  chat.socket = socket;
  socket.onopen = () => {
    socket.send(JSON.stringify({
      type: "hello",
      v: 2,
      token: chat.token || null,
      last_seq: chat.lastSeq,
      context: {
        host_origin: location.origin,
        host_session: "storefront",
        product: undefined,
      },
    }));
  };
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.seq) chat.lastSeq = Math.max(chat.lastSeq, message.seq);
    if (message.type === "welcome") {
      chat.token = message.token || chat.token;
      (message.replay || []).forEach((item) => socket.onmessage({ data: JSON.stringify(item) }));
      if (chat.outbox) {
        socket.send(JSON.stringify({
          type: "user_message", message_id: chat.outbox.id, text: chat.outbox.text,
        }));
      }
      return;
    }
    if (message.type === "ack") {
      showChatTyping(true);
      return;
    }
    if (message.type === "final") {
      showChatTyping(false);
      const text = message.display && message.display.text;
      const body = text ? bubble(text, false) : null;
      if (body) renderChatProducts(body, productsFrom(message));
      if (message.suggestions && message.suggestions.length) {
        renderChatSuggestions(message.suggestions);
      }
      return;
    }
    if (message.type === "done") {
      showChatTyping(false);
      chat.outbox = null;
      el("chat-send").disabled = false;
      el("chat-input").disabled = false;
      addChatTurn(message);
      return;
    }
    if (message.type === "error") {
      showChatTyping(false);
      bubble(message.message || "Sorry, something went wrong on our side.", false);
      el("chat-send").disabled = false;
      el("chat-input").disabled = false;
    }
  };
  socket.onclose = () => { chat.socket = null; };
}

function sendChat(text) {
  const input = el("chat-input");
  const value = (text || input.value || "").trim();
  if (!value || chat.outbox) return;
  bubble(value, true);
  input.value = "";
  input.disabled = true;
  el("chat-send").disabled = true;
  chat.outbox = { id: chatUuid(), text: value };
  connectChat();
  if (chat.socket && chat.socket.readyState === WebSocket.OPEN && chat.token) {
    chat.socket.send(JSON.stringify({
      type: "user_message", message_id: chat.outbox.id, text: value,
    }));
  }
}

function addChatTurn(message) {
  const spend = message.spend || {};
  if (spend.conversation_inr == null && message.response_tokens == null) return;
  chat.turns += 1;
  const row = document.createElement("tr");
  const cost = spend.conversation_inr != null ? spend.conversation_inr : message.conversation_inr;
  row.innerHTML =
    "<td>c" + chat.turns + "</td>" +
    "<td>" + (cost != null ? "₹" + Number(cost).toFixed(3) : "—") + "</td>" +
    "<td>" + (message.response_tokens || "—") + "</td>" +
    "<td>" + (message.thinking_tokens || "—") + "</td>";
  el("turns").appendChild(row);
}

function freshStorefrontChat() {
  if (chat.socket) chat.socket.close();
  chat.socket = null;
  chat.token = "";
  chat.lastSeq = 0;
  chat.outbox = null;
  const box = el("messages");
  const hero = el("chat-welcome");
  const chips = el("chat-chips");
  box.innerHTML = "";
  if (hero) box.appendChild(hero);
  if (chips) box.appendChild(chips);
  el("chat-input").disabled = false;
  el("chat-send").disabled = false;
}

function renderChatSuggestions(list) {
  const row = el("chat-chips");
  if (!row) return;
  row.innerHTML = "";
  for (const label of (list && list.length ? list : CHAT_CHIPS)) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => sendChat(label));
    row.appendChild(button);
  }
}

renderChatSuggestions(CHAT_CHIPS);

el("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendChat();
});
el("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    sendChat();
  }
});

document.addEventListener("tab:chat", (event) => {
  if (event.detail && event.detail.focusPanel) el("chat-input").focus();
});
