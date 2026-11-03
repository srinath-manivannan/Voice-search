// Browser half of the Phase 1 voice agent.
//
// Two audio contexts, deliberately. Capture runs at the model's input rate so
// the browser resamples the microphone for us; playback runs at the model's
// output rate. One shared context would force a resample on whichever side
// lost, and resampling speech badly is audible.

const state = {
  socket: null,
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

// --- playback -------------------------------------------------------------

function play(pcmBytes) {
  const ctx = state.playbackContext;
  if (!ctx) return;

  const samples = new Int16Array(pcmBytes);
  const buffer = ctx.createBuffer(1, samples.length, ctx.sampleRate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000;

  const node = ctx.createBufferSource();
  node.buffer = buffer;
  node.connect(ctx.destination);

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
async function loadConfig() {
  state.config = await (await fetch('/api/config')).json();
  el('model').textContent = state.config.model;
  el('caps').textContent =
    '· ' + state.config.reply_word_cap + '-word cap · thinking budget ' + state.config.thinking_budget;
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
    el('toggle').textContent = '📞 Start Call';
    return;
  }

  state.captureContext = new AudioContext({ sampleRate: state.config.capture_sample_rate });
  state.playbackContext = new AudioContext({ sampleRate: state.config.playback_sample_rate });
  await state.captureContext.audioWorklet.addModule('/static/capture-worklet.js');

  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  state.socket = new WebSocket(url);
  state.socket.binaryType = 'arraybuffer';

  state.socket.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) { play(event.data); return; }
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
    el('toggle').textContent = '■ End Call';
    el('toggle').classList.add('stop');
    el('sub').textContent = 'Speak whenever you like — you can talk over it';
  };
}

function stop() {
  state.running = false;
  flushPlayback();
  if (state.socket) state.socket.close();
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());  // releases the OS mic light
  if (state.captureContext) state.captureContext.close();
  if (state.playbackContext) state.playbackContext.close();
  state.socket = state.stream = state.captureContext = state.playbackContext = null;
  setStatus('');
  setOrb('idle');
  el('toggle').textContent = '📞 Start Call';
  el('toggle').classList.remove('stop');
  el('toggle').disabled = false;
  el('sub').textContent = 'Click to start a voice conversation';
}

// --- metrics --------------------------------------------------------------

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
  const button = el('share');
  button.textContent = '✓';
  setTimeout(() => (button.textContent = '⤴'), 1200);
});
loadConfig().catch(() => setStatus('cannot reach the server', 'bad'));
