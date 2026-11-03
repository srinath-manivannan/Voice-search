// Browser half of the Phase 1 voice agent.
//
// Two audio contexts, deliberately. Capture runs at the model's input rate so
// the browser resamples the microphone for us; playback runs at the model's
// output rate. One shared context would force a resample on whichever side
// lost, and resampling speech badly is audible.

const state = {
  socket: null,
  micAnalyser: null,
  agentAnalyser: null,
  playbackBus: null,
  levelFrame: null,
  captureContext: null,
  playbackContext: null,
  stream: null,
  scheduled: [],      // playback nodes we may still need to abandon
  playheadAt: 0,
  running: false,
  turns: [],
  config: null,
};

const el = (id) => document.getElementById(id);

// The orb is the primary state indicator - it is what someone across the room
// sees. The text line underneath is the detail, not the signal.
const ORB_STATE = {
  '': 'idle', live: 'listening', agent: 'agent', bad: 'error',
};

function setStatus(text, kind) {
  const node = el('status');
  node.textContent = text;
  node.className = 'state ' + (kind || '');
  document.body.dataset.state = ORB_STATE[kind || ''] || 'idle';
}

function setOrb(name) {
  document.body.dataset.state = name;
}

function log(text, kind) {
  const line = document.createElement('div');
  line.className = 'line ' + (kind || '');
  line.textContent = text;
  el('transcript').appendChild(line);
  el('transcript').scrollTop = el('transcript').scrollHeight;
}

// --- audio level ----------------------------------------------------------
//
// What makes the orb read as alive is that it moves with the actual voice, not
// with a timer. Two analysers - one on the microphone, one on the agent's
// playback - and whichever is louder drives it.
//
// The value is written straight to a CSS custom property. It changes about
// sixty times a second, and routing that through any kind of application state
// would re-render the page at 60Hz to animate one blob.

function analyserFor(context, node) {
  const analyser = context.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.75;
  if (node) node.connect(analyser);
  return analyser;
}

function readLevel(analyser) {
  if (!analyser) return 0;
  const bins = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteTimeDomainData(bins);
  let sum = 0;
  for (let i = 0; i < bins.length; i++) {
    const v = (bins[i] - 128) / 128;
    sum += v * v;
  }
  // Root mean square, then a curve - speech sits low in a linear scale and the
  // orb would barely move.
  return Math.min(1, Math.sqrt(sum / bins.length) * 3.2);
}

function startLevelLoop() {
  const orb = document.querySelector('.orb');
  const tick = () => {
    const level = Math.max(readLevel(state.micAnalyser), readLevel(state.agentAnalyser));
    orb.style.setProperty('--level', level.toFixed(3));
    state.levelFrame = requestAnimationFrame(tick);
  };
  tick();
}

function stopLevelLoop() {
  if (state.levelFrame) cancelAnimationFrame(state.levelFrame);
  state.levelFrame = null;
  state.micAnalyser = state.agentAnalyser = state.playbackBus = null;
  const orb = document.querySelector('.orb');
  if (orb) orb.style.setProperty('--level', '0');
}

// --- playback -------------------------------------------------------------

function play(pcmBytes) {
  const ctx = state.playbackContext;
  if (!ctx) return;
  state.chunksPlayed = (state.chunksPlayed || 0) + 1;

  // A context can be suspended again by the browser at any point - a
  // background tab, a device change. Scheduling into a suspended context is
  // silent and raises nothing, so check every time rather than once at start.
  if (ctx.state === 'suspended') ctx.resume().catch(() => {});

  const samples = new Int16Array(pcmBytes);
  const buffer = ctx.createBuffer(1, samples.length, ctx.sampleRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000;

  if (!state.playbackBus) {
    state.playbackBus = ctx.createGain();
    state.agentAnalyser = analyserFor(ctx, state.playbackBus);
    state.playbackBus.connect(ctx.destination);
  }

  const node = ctx.createBufferSource();
  node.buffer = buffer;
  node.connect(state.playbackBus);

  // Schedule back-to-back. Starting every chunk at "now" would overlap them
  // into noise whenever chunks arrive faster than they play.
  const startAt = Math.max(ctx.currentTime, state.playheadAt);
  node.start(startAt);
  state.playheadAt = startAt + buffer.duration;

  state.scheduled.push(node);
  node.onended = () => {
    const i = state.scheduled.indexOf(node);
    if (i >= 0) state.scheduled.splice(i, 1);
  };
}

function flushPlayback() {
  // Barge-in. Everything already scheduled in the audio graph has to be
  // stopped explicitly - dropping references would let it play to the end.
  for (const node of state.scheduled) {
    try { node.stop(); } catch (_) { /* already finished */ }
  }
  state.scheduled = [];
  state.playheadAt = 0;
}

// --- session --------------------------------------------------------------

// Show what we are running before anyone clicks anything - if the word cap or
// the thinking budget is not what was intended, that should be visible before
// money is spent, not after.
// Sample rates and the greeting, nothing else. The page has no business
// knowing which model is behind it, and anything it knows is public.
async function loadConfig() {
  state.config = await (await fetch('/api/config')).json();
  return state.config;
}

async function start() {
  el('toggle').disabled = true;
  setStatus('connecting…');

  try {
    if (!state.config) await loadConfig();
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (error) {
    setStatus('microphone blocked', 'bad');
    log('Could not open the microphone: ' + error.message, 'bad');
    el('toggle').disabled = false;
    setCtaLabel('Start Call');
    return;
  }

  state.captureContext = new AudioContext({ sampleRate: state.config.capture_sample_rate });
  state.playbackContext = new AudioContext({ sampleRate: state.config.playback_sample_rate });

  // An AudioContext can be born suspended even when it was created inside a
  // click handler, and a suspended context accepts everything you schedule and
  // plays none of it - no error, no sound. Resume both explicitly.
  await Promise.all([state.captureContext.resume(), state.playbackContext.resume()]
    .map(p => p.catch(() => {})));

  await state.captureContext.audioWorklet.addModule('/static/capture-worklet.js');

  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  state.socket = new WebSocket(url);
  state.socket.binaryType = 'arraybuffer';

  state.socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      state.bytesIn = (state.bytesIn || 0) + event.data.byteLength;
      play(event.data);
      return;
    }
    const message = JSON.parse(event.data);
    switch (message.type) {
      case 'ready':      setStatus('listening', 'live'); break;
      case 'flush':      flushPlayback(); break;
      case 'barge_in':   flushPlayback(); log('— you interrupted —', 'dim'); break;
      case 'transcript': log(message.text, 'agent'); break;
      case 'state':
        if (message.agent_speaking) { setStatus('Poorvika is speaking', 'agent'); }
        else if (message.caller_speaking !== undefined) {
          setStatus(message.caller_speaking ? 'listening to you' : 'listening', 'live');
          setOrb(message.caller_speaking ? 'caller' : 'listening');
        }
        break;
      case 'turn':    addTurn(message); break;
      case 'summary': showCost(message.cost); break;
      case 'error':   setStatus(message.message, 'bad'); break;
    }
  };

  state.socket.onclose = () => { if (state.running) stop(); };
  state.socket.onerror = () => setStatus('connection failed', 'bad');

  state.socket.onopen = () => {
    const sourceNode = state.captureContext.createMediaStreamSource(state.stream);
    state.micAnalyser = analyserFor(state.captureContext, sourceNode);
    startLevelLoop();
    const worklet = new AudioWorkletNode(state.captureContext, 'capture-processor', {
      processorOptions: { frameMs: 20 },
    });
    worklet.port.onmessage = (event) => {
      if (state.socket && state.socket.readyState === WebSocket.OPEN) {
        state.socket.send(event.data.buffer);
      }
    };
    sourceNode.connect(worklet);
    state.running = true;
    el('toggle').disabled = false;
    setCtaLabel('End Call');
    el('toggle').classList.add('stop');
    el('sub').textContent = 'Speak whenever you like — you can talk over it';
  };
}

function stop() {
  state.running = false;
  stopLevelLoop();
  flushPlayback();
  if (state.socket) state.socket.close();
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());  // releases the OS mic light
  if (state.captureContext) state.captureContext.close();
  if (state.playbackContext) state.playbackContext.close();
  state.socket = state.stream = state.captureContext = state.playbackContext = null;
  setStatus('');
  setOrb('idle');
  setCtaLabel('Start Call');
  el('toggle').classList.remove('stop');
  el('toggle').disabled = false;
  el('sub').textContent = 'Click to start a voice conversation';
}

// --- metrics --------------------------------------------------------------

window.audioDiagnostics = () => ({
  playbackState: state.playbackContext && state.playbackContext.state,
  playbackRate: state.playbackContext && state.playbackContext.sampleRate,
  captureState: state.captureContext && state.captureContext.state,
  bytesReceived: state.bytesIn || 0,
  chunksPlayed: state.chunksPlayed || 0,
  scheduled: state.scheduled.length,
  playheadAhead: state.playbackContext
    ? +(state.playheadAt - state.playbackContext.currentTime).toFixed(2) : null,
});

function addTurn(turn) {
  state.turns.push(turn);
  const row = document.createElement('tr');
  const latency = turn.first_audio_ms == null ? '—' : Math.round(turn.first_audio_ms) + ' ms';
  const slow = turn.first_audio_ms != null && turn.first_audio_ms > 2000;
  row.innerHTML =
    '<td>' + turn.turn_id + '</td>' +
    '<td class="' + (slow ? 'bad' : '') + '">' + latency + '</td>' +
    '<td>' + turn.response_tokens + '</td>' +
    '<td class="' + (turn.thinking_tokens ? 'warn' : '') + '">' + turn.thinking_tokens + '</td>';
  el('turns').appendChild(row);
  setStatus('listening', 'live');
  setOrb('listening');
  updatePercentiles();
}

function updatePercentiles() {
  const values = state.turns.map((t) => t.first_audio_ms).filter((v) => v != null).sort((a, b) => a - b);
  if (!values.length) return;
  const at = (f) => values[Math.min(Math.round(f * (values.length - 1)), values.length - 1)];
  const p50 = at(0.5), p95 = at(0.95);
  el('p50').textContent = Math.round(p50) + ' ms';
  el('p95').textContent = Math.round(p95) + ' ms';
  el('p50').className = p50 <= 1000 ? 'good' : 'bad';
  el('p95').className = p95 <= 2000 ? 'good' : 'bad';
  el('verdict').textContent =
    (p50 <= 1000 && p95 <= 2000 ? 'PASS' : 'MISS') + ' · ' + values.length + ' turns';
}

function showCost(cost) {
  el('cost').hidden = false;
  el('cost-once').textContent = '₹' + cost.billed_once_inr_per_min.toFixed(2);
  el('cost-rebilled').textContent = '₹' + cost.rebilled_inr_per_min.toFixed(2);
}

el('toggle').addEventListener('click', () => (state.running ? stop() : start()));

el('share').addEventListener('click', async () => {
  await navigator.clipboard.writeText(location.href).catch(() => {});
  // Flash a tick without losing the icon: swap a class, not the contents.
  const button = el('share');
  button.classList.add('copied');
  setTimeout(() => button.classList.remove('copied'), 1200);
});
loadConfig().catch(() => setStatus('cannot reach the server', 'bad'));

// --- chat -----------------------------------------------------------------
//
// Its own socket, not message types on the voice one. The voice socket carries
// fifty binary audio frames a second; multiplexing text onto it would couple
// two unrelated lifecycles for no gain, since only one tab is ever visible.

const chat = { socket: null, bubble: null, greeted: false, turns: 0 };

// The button holds an icon beside its label, so only the label is rewritten -
// setting textContent would throw the SVG away.
function setCtaLabel(text) {
  el('toggle').querySelector('span').textContent = text;
}

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
