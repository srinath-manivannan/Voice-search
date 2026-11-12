// The chat tab.
//
// Split out of app.js when that file passed 500 lines, and along the seam
// that was already there: this has its own socket, its own history and its
// own cost line, and shares only `el`, `state` and the tab switcher with the
// voice side. Loaded second, so those exist by the time this runs.

// --- chat -----------------------------------------------------------------
//
// Its own socket, not message types on the voice one. The voice socket carries
// fifty binary audio frames a second; multiplexing text onto it would couple
// two unrelated lifecycles for no gain, since only one tab is ever visible.

const chat = { socket: null, bubble: null, greeted: false, turns: 0 };

function bubble(text, mine) {
  const row = document.createElement('div');
  row.className = 'msg' + (mine ? ' me' : '');
  if (!mine) {
    const av = document.createElement('div');
    av.className = 'av';
    av.setAttribute('aria-label', 'Poorvika');
    row.appendChild(av);
  }
  const body = document.createElement('div');
  body.className = 'bubble';
  body.textContent = text;
  row.appendChild(body);
  el('messages').appendChild(row);
  el('messages').scrollTop = el('messages').scrollHeight;
  return body;
}

function connectChat() {
  if (chat.socket) return;
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/chat';
  chat.socket = new WebSocket(url);

  chat.socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    switch (message.type) {
      case 'ready':
        // The greeting is the same sentence the voice agent opens with - one
        // persona, two surfaces.
        if (!chat.greeted) { bubble(message.greeting, false); chat.greeted = true; }
        break;
      case 'start':
        chat.bubble = bubble('…', false);
        chat.bubble.classList.add('thinking');
        chat.bubble.textContent = '';
        break;
      case 'delta':
        if (chat.bubble) {
          chat.bubble.classList.remove('thinking');
          chat.bubble.textContent += message.text;
          el('messages').scrollTop = el('messages').scrollHeight;
        }
        break;
      case 'done':
        chat.bubble = null;
        el('chat-send').disabled = false;
        el('chat-input').disabled = false;
        el('chat-input').focus();
        addChatTurn(message);
        break;
      case 'error':
        // Remove the whole row - removing just the bubble leaves an orphan
        // avatar floating with nothing beside it.
        if (chat.bubble) chat.bubble.closest('.msg').remove();
        chat.bubble = null;
        bubble(message.message, false).classList.add('thinking');
        el('chat-send').disabled = false;
        el('chat-input').disabled = false;
        break;
    }
  };
  chat.socket.onclose = () => { chat.socket = null; };
}

function sendChat() {
  const input = el('chat-input');
  const text = input.value.trim();
  if (!text || !chat.socket || chat.socket.readyState !== WebSocket.OPEN) return;
  bubble(text, true);
  chat.socket.send(JSON.stringify({ text }));
  input.value = '';
  input.disabled = true;
  el('chat-send').disabled = true;
}

function addChatTurn(message) {
  chat.turns += 1;
  const row = document.createElement('tr');
  row.innerHTML =
    '<td>c' + chat.turns + '</td>' +
    '<td>' + (message.conversation_inr != null ? '₹' + message.conversation_inr.toFixed(3) : '—') + '</td>' +
    '<td>' + message.response_tokens + '</td>' +
    '<td class="' + (message.thinking_tokens ? 'warn' : '') + '">' + message.thinking_tokens + '</td>';
  el('turns').appendChild(row);
}

el('chat-send').addEventListener('click', sendChat);
el('chat-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') sendChat(); });

// --- tabs -----------------------------------------------------------------

function showTab(which) {
  const voice = which === 'voice';
  el('voice-panel').hidden = !voice;
  el('chat-panel').hidden = voice;
  el('tab-voice').setAttribute('aria-selected', String(voice));
  el('tab-chat').setAttribute('aria-selected', String(!voice));
  if (!voice) { connectChat(); el('chat-input').focus(); }
}

el('tab-voice').addEventListener('click', () => showTab('voice'));
el('tab-chat').addEventListener('click', () => showTab('chat'));
