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
// The button holds an icon beside its label, so only the label is rewritten -
// setting textContent would throw the SVG away.
function setCtaLabel(text) {
  el('toggle').querySelector('span').textContent = text;
}

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
  state.config = await (await fetch('/api/v1/config')).json();
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
      case 'summary':
        showCost(message.cost);
        showTrace(message.trace, message.trace_file);
        break;
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
  const cost = turn.cost;
  const rate = cost ? cost.total_inr_per_min : null;
  const target = cost ? cost.target_inr_per_min : 2.5;
  const latency = turn.first_audio_ms == null ? '—' : Math.round(turn.first_audio_ms) + ' ms';
  const slow = turn.first_audio_ms != null && turn.first_audio_ms > 2000;
  row.innerHTML =
    '<td>' + turn.turn_id + '</td>' +
    '<td class="' + (slow ? 'bad' : '') + '">' + latency + '</td>' +
    '<td>' + turn.response_tokens + '</td>' +
    '<td class="' + (turn.thinking_tokens ? 'warn' : '') + '">' + turn.thinking_tokens + '</td>' +
    // The whole bill per connected minute, GST in, against the target it
    // is measured against. On screen during the call, because after it is
    // too late.
    '<td class="' + rateClass(rate, target) + '">' +
      (rate == null ? '—' : '₹' + rate.toFixed(2)) + '</td>';
  el('turns').appendChild(row);
  if (cost) showCost(cost);
  setStatus('listening', 'live');
  setOrb('listening');
  updatePercentiles();
}

// Coloured against the TARGET - what a minute is supposed to cost - not
// against the ceiling, which is a runaway guard sitting far above it. Red at
// or over the target, amber in the last fifteen percent below it, green
// under that. Calls are over the target today, so this panel is mostly red,
// and it is meant to be: the honest reading is the one that shows the gap.
// The percentage next to the figure says how far over, because "red" alone
// does not tell you whether you are a rupee out or four.
function rateClass(rate, target) {
  if (rate == null) return '';
  if (rate >= target) return 'bad';
  if (rate >= target * 0.85) return 'warn';
  return 'good';
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

// The counters that answer "why did that call go wrong" without a console:
// did audio arrive, was any of it thrown away, and who did the interrupting.
function showTrace(trace, file) {
  if (!trace) return;
  el('trace').hidden = false;
  el('t-turns').textContent = trace.turns;
  el('t-chunks').textContent = trace.audio_chunks;
  el('t-dropped').textContent = trace.audio_dropped_abandoned;
  el('t-dropped').className = trace.audio_dropped_abandoned ? 'bad' : 'good';
  el('t-barge').textContent = trace.barge_ins_local;
  el('t-model').textContent = trace.interrupts_from_model;
  el('t-file').textContent = file || '—';
}

// The cost panel. One row per charge line, because "the call costs ₹2.40" is
// not actionable and "₹1.19 of it is the agent's voice and ₹0.56 is the
// standing prompt being re-processed" is: the first number is physics, the
// second is a setting. The bar widths are each line's share of the total, so
// the thing to go and fix is the widest one.
function showCost(cost) {
  el('cost').hidden = false;
  const target = cost.target_inr_per_min;

  el('cost-total').textContent = '₹' + cost.total_inr_per_min.toFixed(2);
  el('cost-total').className = rateClass(cost.total_inr_per_min, target);
  el('cost-ceiling').textContent = '₹' + target.toFixed(2);
  el('cost-pct').textContent =
    Math.round((cost.total_inr_per_min / target) * 100) + '% of target';
  el('cost-pct').className = rateClass(cost.total_inr_per_min, target);
  el('cost-call').textContent = '₹' + cost.total_inr.toFixed(2);
  el('cost-mins').textContent = cost.minutes.toFixed(2) + ' min · '
    + cost.turns + ' turns · ' + cost.turns_per_minute.toFixed(1) + '/min';
  el('cost-floor').textContent = '₹' + cost.voice_floor_inr_per_min.toFixed(2);

  const body = el('cost-lines');
  body.innerHTML = '';
  for (const line of cost.lines) {
    const tr = document.createElement('tr');
    const pct = Math.round(line.share * 100);
    tr.innerHTML =
      '<td>' + line.label + (line.key === 'thinking' && cost.thinking_rate_unconfirmed
        ? ' <span class="qm" title="Google does not document whether thinking '
          + 'bills at the text ($4.50) or audio ($12.00) output rate. Priced '
          + 'here at the text rate; if it is the audio rate this line is 2.7x.'
          + '">?</span>'
        : '') + '</td>' +
      '<td class="num">' + line.tokens.toLocaleString() + '</td>' +
      '<td class="num">₹' + line.inr_per_min.toFixed(2) + '</td>' +
      '<td class="bar"><i style="width:' + pct + '%"></i><b>' + pct + '%</b></td>';
    body.appendChild(tr);
  }
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


// --- tabs -------------------------------------------------------------------
//
// A real tablist, which two buttons and an aria-selected attribute were not.
// The pattern the ARIA practices guide specifies: one tab in the page's tab
// order at a time, arrows to move between them, Home and End to jump. Without
// it a keyboard user tabs into the list and cannot leave it except by tabbing
// through every tab, and a screen reader is never told how many there are.

const TABS = ['tab-voice', 'tab-chat'];

function showTab(which, focusPanel) {
  const voice = which === 'voice';
  el('voice-panel').hidden = !voice;
  el('chat-panel').hidden = voice;
  TABS.forEach((id) => {
    const selected = (id === 'tab-voice') === voice;
    const tab = el(id);
    tab.setAttribute('aria-selected', String(selected));
    // Roving tabindex: only the selected tab is in the page's tab order, so
    // Tab moves past the whole list rather than through it.
    tab.tabIndex = selected ? 0 : -1;
  });
  // Said once, here, rather than reached for from the chat module.
  // focusPanel is false when the arrow keys brought us here: in a tablist
  // the arrows move between TABS, and the panel is entered with Tab. Moving
  // focus into the panel on an arrow press traps a keyboard user, who
  // cannot then reach the other tab at all.
  if (!voice) {
    document.dispatchEvent(
      new CustomEvent('tab:chat', { detail: { focusPanel: Boolean(focusPanel) } })
    );
  }
}

function focusTab(index) {
  const id = TABS[(index + TABS.length) % TABS.length];
  showTab(id === 'tab-voice' ? 'voice' : 'chat', false);
  // After showTab, so the panel it reveals cannot take the focus back.
  el(id).focus();
}

TABS.forEach((id, index) => {
  el(id).addEventListener('click', () => showTab(id === 'tab-voice' ? 'voice' : 'chat', true));
  el(id).addEventListener('keydown', (event) => {
    const moves = {
      ArrowRight: index + 1, ArrowLeft: index - 1,
      Home: 0, End: TABS.length - 1,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    focusTab(moves[event.key]);
  });
});


// --- telling the server when the page breaks --------------------------------
//
// The rule asks for frontend errors as well as backend ones, and the browser
// does not get a DSN to report them with: a key in a page is a key anyone can
// read. So the page says what broke and the server reports it, through the
// same scrubber as everything else.
//
// A message, a file and a line. Deliberately no stack: a browser stack
// carries whatever was in scope, and on this page that is the conversation.

let reported = 0;

function reportToServer(message, source, line) {
  // Three per page load. A render loop that throws every frame would
  // otherwise post a thousand times a minute, which is a denial of service
  // we would be performing on ourselves.
  if (reported >= 3) return;
  reported += 1;
  fetch('/api/v1/client-errors', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      message: String(message).slice(0, 300),
      source: String(source || '').split('/').pop().slice(0, 200),
      line: Number(line) || 0,
    }),
  }).catch(() => {});
}

window.addEventListener('error', (event) => {
  reportToServer(event.message, event.filename, event.lineno);
});

window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  reportToServer(
    (reason && reason.message) || String(reason), 'promise', 0
  );
});

