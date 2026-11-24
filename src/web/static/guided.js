// The guided tab.
//
// A question, a row of buttons, and at the end up to five products. The
// server decides every step and sends the whole of what should be on
// screen; this file draws it and sends back the id of whichever button was
// tapped. It holds no rule of its own - which question comes next, what a
// budget means, whether a think is allowed are all the server's - so a
// phone line can run the same call with number keys and nothing here to
// port.
//
// Everything a customer reads arrives in Tamil from the server. The few
// sentences this file composes itself - stock, EMI, colours - are below in
// one place, in the same language.
//
// Shares `el`, the meter and the tab switcher with app.js, like chat.js.

const guided = {
  session: null, result: null, busy: false, started: false, waiting: null,
  saidWait: false, meterOn: false,
};

// --- the voice ----------------------------------------------------------------
//
// Every sentence the guided call says is a recording, made once. The server
// names the one that goes with what it has just put on screen - `audio` -
// and names none if the recording no longer says exactly that sentence, so
// there is nothing here that decides what may be played. This only plays
// it.
//
// Two rules, both about not talking over people:
//
//   A tap stops whatever is being said. Somebody who has already answered
//   does not want the rest of the question.
//
//   The waiting line is never cut off by the answer it was waiting for.
//   "Oru nimisham, paakku-" followed at once by the next sentence is worse
//   than either on its own, so what arrives during it waits its turn.

const VOICE_PATH = '/guided/voice/';
// How long a step may take before the waiting line is spoken. Longer than
// the 300 ms at which it is SHOWN: a line that takes 1.7 seconds to say
// should not be started for a wait that was going to last half a second.
const SPEAK_WAIT_AFTER_MS = 900;
// What is said when a step fails, by the status it failed with.
const FAILED_AUDIO = { 404: 'say_expired', 429: 'say_slow_down', 503: 'say_down' };

const SPEECH_PATH = '/api/v1/guided/speech/';

// `queue` is what is still to be said after the current clip: a recorded
// question is followed by the sentence built from live data ("one: Poco
// M8 5G, 24,999 rupees..."), which may still be being made when the
// question ends. `turn` goes up on every new screen, so a sentence that
// lands after the customer has moved on is never played over the next one.
const voice = {
  on: true, clips: {}, playing: null, playingKey: null, queue: [], last: null,
  blocked: false, turn: 0,
};

function urlFor(key) {
  // A recorded line is a name; a sentence made from live data is a hash.
  return /^[0-9a-f]{32}$/.test(key) ? SPEECH_PATH + key + '.wav' : VOICE_PATH + key + '.wav';
}

function clip(key) {
  // One element per line, kept: the second time a question is asked it is
  // already downloaded, whatever the cache headers say.
  if (!voice.clips[key]) voice.clips[key] = new Audio(urlFor(key));
  return voice.clips[key];
}

// Stopping dead mid-word is what the owner heard as "டக்குன்னு கட்".
// A quarter-second cosine fade is the difference between a voice being
// interrupted and a tape being cut - and six coarse volume steps were
// themselves audible as a zipper, so the slope is fine-grained now.
const fading = new Map();

function fadeOut(audio) {
  if (audio.pendingRender) { audio.pendingRender.abort(); return; }
  if (fading.has(audio)) return;
  const steps = 16;
  let left = steps;
  const slope = setInterval(() => {
    left -= 1;
    const share = Math.max(0, left / steps);
    try { audio.volume = 0.5 - Math.cos(share * Math.PI) / 2; } catch (e) { left = 0; }
    if (left <= 0) {
      clearInterval(slope);
      fading.delete(audio);
      audio.pause();
      try { audio.volume = 1; } catch (e) { /* gone */ }
    }
  }, 15);
  fading.set(audio, slope);
}

// A clip asked for while its own fade-out is still running must win the
// race, or the fade's last tick pauses the fresh playback mid-word.
function unfade(audio) {
  const slope = fading.get(audio);
  if (slope) {
    clearInterval(slope);
    fading.delete(audio);
  }
  try { audio.volume = 1; } catch (e) { /* fine */ }
}

function hush() {
  voice.queue = [];
  voice.turn += 1;
  if (!voice.playing) return;
  fadeOut(voice.playing);
  voice.playing = null;
  voice.playingKey = null;
}

function soundLabel() {
  const button = el('g-sound');
  button.setAttribute('aria-pressed', String(voice.on));
  button.textContent = !voice.on ? SAYS.soundOff
    : voice.blocked ? SAYS.soundBlocked : SAYS.soundOn;
}

function playNext() {
  const next = voice.queue.shift();
  if (next) { speak(next); return; }
  // The agent has finished its say: the customer's turn - after a breath,
  // so the last syllable's echo in the room is not heard as an answer.
  if (typeof listenSoon === 'function') setTimeout(listenSoon, 350);
}

// Half-duplex, like a polite salesperson: the shop never listens while it
// is talking. The owner's test, 2026-11-24, showed why - with the mic open
// during playback the recogniser heard the agent's own voice from the
// speakers, cut it mid-word ("நோக்-"), sent its words back as the
// customer's, and answered them with "மன்னிக்கணும் புரியல" in a loop.
function agentSpeaking() {
  return !!voice.playing || voice.queue.length > 0;
}

// Say this now - or, if the waiting line or an earlier line of this same
// screen is still being said, straight after it.
function speak(key) {
  if (!key) return;
  voice.last = key;
  if (!voice.on) return;
  if (voice.playing) {
    voice.queue.push(key);
    return;
  }
  if (typeof key === 'object') { speakText(key.text); return; }
  if (typeof stopListening === 'function') stopListening();
  const audio = clip(key);
  unfade(audio);
  const turn = voice.turn;
  voice.playing = audio;
  voice.playingKey = key;
  const finished = () => {
    if (voice.playing !== audio) return;
    voice.playing = null;
    voice.playingKey = null;
    if (voice.turn === turn) playNext();
  };
  audio.onended = finished;
  // A sentence the server could not say truthfully comes back empty; that
  // is not an error to anyone, it is the next line's turn.
  audio.onerror = finished;
  audio.currentTime = 0;
  audio.play().then(() => {
    if (voice.blocked) { voice.blocked = false; soundLabel(); }
    if (typeof watchForBargeIn === 'function') watchForBargeIn();
  }).catch(() => {
    // A browser will not play sound on a page nobody has touched yet, and
    // the first question is put before anyone has. Say so on the button
    // rather than fail silently: one tap on it, or on any answer, and
    // every line after that is spoken.
    if (voice.playing === audio) { voice.playing = null; voice.playingKey = null; }
    if (audio.error) { playNext(); return; }
    voice.blocked = true;
    soundLabel();
    // Nothing will play until a tap, so nothing is "being said": the
    // queue goes, and the mic must not wait on it forever.
    voice.queue = [];
    if (typeof listenSoon === 'function') setTimeout(listenSoon, 350);
  });
}

// One voice only. A sentence with no recording in the call's voice is
// shown on screen and not spoken - never handed to a device voice or a
// local synthesiser, which on this machine was a robotic, Hindi-sounding
// man reading Tamil. Silence is honest; a stranger's voice is not.
function speakText(text) {
  playNext();
}

function readView(view) {
  const prompt = view.say || view.question?.ask;
  // "சரிங்க." first when a typed or spoken turn was understood; the
  // overview sentence itself is on screen.
  if (view.overview?.audio) speak(view.overview.audio);
  if (view.audio) speak(view.audio);
  else if (prompt && !view.speak) speak({text: prompt});
  if (view.question?.note) speak({text: view.question.note});
  if (view.speak) speak(view.speak.key || {text: view.speak.text});
  if (convo.on && !agentSpeaking()) listenSoon();
}

// A new screen: whatever was queued for the last one is dropped, but a
// waiting line already being said is let finish - "oru nimisham, paak-"
// cut off by the answer it was waiting for is worse than either.
function newScreen() {
  voice.queue = [];
  voice.turn += 1;
  const waitingKey = guided.waiting && guided.waiting.audio;
  if (voice.playing && voice.playingKey !== waitingKey) {
    fadeOut(voice.playing);
    voice.playing = null;
    voice.playingKey = null;
  }
  if (voice.playing) {
    // Let it finish, then carry on with this screen's lines.
    const audio = voice.playing;
    const turn = voice.turn;
    audio.onended = () => {
      voice.playing = null;
      voice.playingKey = null;
      if (voice.turn === turn) playNext();
    };
  }
}

const rs = (amount) => '₹' + Number(amount).toLocaleString('en-IN');

// The handful of sentences this file composes itself - stock, EMI,
// colours - in both languages. Everything else arrives from the server
// already in the customer's language.
const TEXTS = {
  ta: {
    wait: 'ஒரு நிமிஷம், பாக்குறேன்.',
    failed: 'மன்னிக்கணும், இப்போ பாக்க முடியல. மறுபடி முயற்சி பண்ணுங்க.',
    retry: 'மறுபடி முயற்சி',
    others: '\u2190 மத்த தேர்வுகள்',
    back: '\u2190 பின்னாடி',
    restart: 'முதல்ல இருந்து',
    inStock: (branches) => !branches ? 'ஸ்டாக்ல இருக்கு'
      : 'ஸ்டாக்ல இருக்கு \u00b7 ' + branches + (branches === 1 ? ' கடைல' : ' கடைகள்ல'),
    outOfStock: 'இப்போ ஸ்டாக் இல்ல',
    stockUnknown: 'ஸ்டாக் கடைல உறுதி பண்ணிக்கோங்க',
    priceUnsure: 'விலை தோராயமானது \u2014 கடைல உறுதி பண்ணிக்கோங்க',
    colours: (count) => count + ' நிறங்கள்ல கிடைக்கும்',
    emi: (plan) => 'மாதம் ' + rs(plan.per_month) + ' முதல்'
      + (plan.months ? ' \u00b7 ' + plan.months + ' மாதம்' : '')
      + (plan.provider ? ' \u00b7 ' + plan.provider : ''),
    withOffer: (price) => 'ஆஃபரோட ' + rs(price),
    upTo: (amount) => rs(amount) + ' வரை',
    earlier: 'இந்த விவரம் சில நிமிஷம் முன்னாடி பார்த்தது.',
    stock: 'ஸ்டாக்', offers: 'ஆஃபர்', emiLabel: 'EMI',
    soundOn: 'குரல்: ஆன்',
    soundOff: 'குரல்: ஆஃப்',
    soundBlocked: 'குரல்ல கேக்க தொடுங்க',
    listening: 'கேக்கறேன்…',
    understanding: 'புரிஞ்சுக்கறேன்…',
    micBlocked: 'Mic அனுமதி இல்ல - தொட்டு தேர்ந்தெடுங்க',
    convo: 'பேசுங்க',
    typeHere: 'டைப் பண்ணுங்க… உதா: Samsung போன், ₹25,000 வரை',
    send: 'அனுப்பு',
    directions: 'வழி',
  },
  en: {
    wait: 'One moment, let me look.',
    failed: 'Sorry, something went wrong. Please try again.',
    retry: 'Try again',
    others: '\u2190 Other picks',
    back: '\u2190 Back',
    restart: 'Start over',
    inStock: (branches) => !branches ? 'In stock'
      : 'In stock \u00b7 ' + branches + (branches === 1 ? ' shop' : ' shops'),
    outOfStock: 'Out of stock right now',
    stockUnknown: 'Please confirm stock at the shop',
    priceUnsure: 'Price is approximate \u2014 confirm at the shop',
    colours: (count) => 'Available in ' + count + ' colours',
    emi: (plan) => 'From ' + rs(plan.per_month) + ' a month'
      + (plan.months ? ' \u00b7 ' + plan.months + ' months' : '')
      + (plan.provider ? ' \u00b7 ' + plan.provider : ''),
    withOffer: (price) => rs(price) + ' with the offer',
    upTo: (amount) => 'up to ' + rs(amount),
    earlier: 'These details are from a few minutes ago.',
    stock: 'Stock', offers: 'Offers', emiLabel: 'EMI',
    soundOn: 'Voice: on',
    soundOff: 'Voice: off',
    soundBlocked: 'Tap to hear the voice',
    listening: 'Listening…',
    understanding: 'Understanding…',
    micBlocked: 'No microphone access - please tap instead',
    convo: 'Speak',
    typeHere: 'Type here… e.g. Samsung phone, up to ₹25,000',
    send: 'Send',
    directions: 'Directions',
  },
};
let SAYS = TEXTS.ta;

function setLanguage(lang) {
  SAYS = TEXTS[lang] || TEXTS.ta;
  // Tamil script read with English pronunciation rules is noise to a
  // screen reader; the panel says which language it is in.
  el('guided-panel').lang = lang === 'en' ? 'en' : 'ta';
  el('g-back').textContent = SAYS.back;
  el('g-restart').textContent = SAYS.restart;
  el('g-text').placeholder = SAYS.typeHere;
  el('g-text-label').textContent = SAYS.typeHere;
  el('g-send').textContent = SAYS.send;
  soundLabel();
  convoLabel();
}

// How the five were arrived at, for whoever is watching the bill. English
// only, like the rest of the cost panel, and it names no model.
const HOW = {
  all: 'five or fewer were left, so nothing was asked of a model',
  model: 'the one think of this session',
  cache: 'these answers were worked out before, so this was free',
  rules: 'not yet earned a think, so sorted by rule — free',
};

function node(tag, className, text) {
  const made = document.createElement(tag);
  if (className) made.className = className;
  // textContent, always. Product names and offer labels are written by
  // suppliers, and a name is not markup however it is spelt.
  if (text != null) made.textContent = text;
  return made;
}

function clear(id) {
  const target = el(id);
  while (target.firstChild) target.removeChild(target.firstChild);
  return target;
}

async function guidedPost(path, body, requestId) {
  const response = await fetch('/api/v1/guided/' + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'Idempotency-Key': requestId },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failed = new Error(data.message || SAYS.failed);
    failed.status = response.status;
    throw failed;
  }
  return data;
}

function waiting(text) {
  el('g-wait').hidden = !text;
  if (text) el('g-wait-text').textContent = text;
}

// One step: send it, draw what comes back. The indicator appears only if
// the answer is not immediate, so a tap that costs nothing and takes ten
// milliseconds does not flash a spinner at anyone.
async function guidedStep(path, body, saying, continuing, requestId = crypto.randomUUID()) {
  if (guided.busy) return;
  guided.busy = true;
  // `continuing` is the page carrying on by itself - asking for the five
  // the moment the last question is answered. Nobody tapped, so nothing is
  // hushed, and a wait that has already been apologised for is not
  // apologised for twice.
  if (!continuing) {
    hush();
    stopListening();
    guided.saidWait = false;
  }
  document.querySelectorAll('.g-choice, .g-pick, .g-chip-btn').forEach((b) => { b.disabled = true; });
  const line = guided.waiting || { say: SAYS.wait, audio: null };
  const slow = setTimeout(() => waiting(saying || line.say), 300);
  const slower = setTimeout(() => {
    if (guided.saidWait) return;
    guided.saidWait = true;
    speak(line.audio);
  }, SPEAK_WAIT_AFTER_MS);
  let view = null;
  let failed = null;
  try {
    view = await guidedPost(path, body, requestId);
  } catch (error) {
    failed = error;
  }
  clearTimeout(slow);
  clearTimeout(slower);
  waiting(null);
  guided.busy = false;

  if (view) return draw(view);
  if (failed.status === 404) {
    // The session was forgotten. Say so and begin again rather than leave a
    // screen of buttons that no longer do anything.
    guided.session = null;
    return guidedStart(failed.message);
  }
  drawFailure(failed.message, () => guidedStep(path, body, saying, continuing, requestId));
  speak(FAILED_AUDIO[failed.status]);
}

async function guidedStart(note) {
  guided.started = true;
  guided.result = null;
  await guidedStep('start', {});
  if (note) el('g-cost').textContent = note;
}

function reset() {
  el('g-heard').hidden = true;
  clear('g-choices');
  clear('g-picks');
  clear('g-card').hidden = true;
  clear('g-questions').hidden = true;
  el('g-answer').hidden = true;
  el('g-back').hidden = true;
  el('g-restart').hidden = true;
}

function drawTrail(answered) {
  const trail = clear('g-trail');
  answered.forEach((given) => trail.appendChild(node('span', 'g-chip', given.label)));
}

function drawCost(view) {
  const meter = view.meter;
  meterCost(meter);
  const line = el('g-cost');
  line.hidden = false;
  const parts = [
    'Questions ₹0.00',
    // Speech is heard by the browser and matched by code: counted, and free.
    'Speaking ' + (meter.spoken_turns || 0) + ' · typing ' + (meter.typed_turns || 0)
      + ' turns ₹0.00',
    'Choosing ₹' + meter.total_inr.toFixed(2),
    '₹' + meter.total_inr_per_min.toFixed(2) + ' a minute, limit ₹'
      + meter.target_inr_per_min.toFixed(2),
  ];
  let text = parts.join(' · ');
  // How a RESULT was arrived at. A card answer has its own sources (rules,
  // capped...) with no line here - that lookup printed "undefined".
  if (view.how && view.step === 'result' && HOW[view.how.source]) {
    text += ' — ' + HOW[view.how.source];
    if (view.how.took_ms) text += ' (' + (view.how.took_ms / 1000).toFixed(1) + 's)';
  }
  // The shelf's four counts, each its own number: index records, of those
  // in stock, of those priced, and the distinct models shown as choices.
  const shelf = view.shelf_counts;
  if (shelf) {
    text += ' · shelf: ' + shelf.records + ' records, ' + shelf.in_stock + ' in stock, '
      + shelf.priced + ' priced, ' + shelf.models + ' models';
    if (typeof view.left === 'number') text += ', ' + view.left + ' matching';
  }
  line.textContent = text;
}

function drawFailure(message, again) {
  reset();
  el('g-ask').textContent = message || SAYS.failed;
  const retry = node('button', 'g-choice any', SAYS.retry);
  retry.addEventListener('click', again);
  el('g-choices').appendChild(retry);
  el('g-restart').hidden = !guided.session;
}

function draw(view, quietly) {
  guided.session = view.session;
  // The clock begins with the first answer. A page sitting open on the
  // welcome screen is nobody "on call", and a timer ticking there reads
  // as billing for nothing.
  if (!guided.meterOn && view.answered.length) {
    guided.meterOn = true;
    startMeter();
  }
  if (view.lang) setLanguage(view.lang);
  guided.waiting = view.waiting || guided.waiting;

  convo.lang = view.question?.id === 'language' ? 'auto' : (view.lang || 'ta');
  drawMic();
  const understood = el('g-overview');
  understood.hidden = !view.overview?.text;
  understood.textContent = view.overview?.text || '';
  if (view.step === 'heard') {
    // Nothing was pressed. The screen stays; the customer is told why.
    newScreen();
    readView(view);
    showHeard(view.heard, view.say);
    return;
  }

  if (view.step === 'dealer') {
    // The handover: the shops holding stock, under the card that stays.
    drawCost(view);
    newScreen();
    readView(view);
    const box = el('g-answer');
    box.hidden = false;
    box.classList.remove('unsure');
    box.textContent = view.say;
    // One line per shop holding stock, each with the way there. The estate
    // gives no phone number, so directions are the handover we can make.
    (view.branches || []).forEach((shop) => {
      const row = node('div', 'g-shop');
      row.appendChild(document.createTextNode('\u2022 ' + shop.name
        + (shop.state ? ' \u2014 ' + shop.state : '')
        + (shop.quantity ? ' (' + shop.quantity + ')' : '') + ' '));
      const where = ['Poorvika', shop.name, shop.area, shop.city].filter(Boolean).join(' ');
      const link = node('a', 'g-way', SAYS.directions);
      link.href = 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(where);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      row.appendChild(link);
      box.appendChild(row);
    });
    return;
  }

  if (view.step === 'answer') {
    // An answer to one of the card's questions: the card STAYS, the answer
    // lands under it - so no reset, which would wipe the card it is about.
    // Dynamic text, so there is no recording to play.
    drawCost(view);
    newScreen();
    // Picked and asked in one breath ("C option-க்கு EMI இருக்கா?"): the card
    // was never on screen, so it comes with the answer and is drawn first.
    if (view.product) {
      reset();
      drawCard(view.product);
      drawQuestions(view);
      el('g-restart').hidden = false;
    }
    // A known answer is a sentence made for it; "not sure, ask at the
    // shop" is a recorded line. One of the two is there.
    readView(view);
    const box = el('g-answer');
    box.hidden = false;
    box.textContent = view.say;
    box.classList.toggle('unsure', !view.known);
    if (view.left === 0) el('g-questions').hidden = true;
    document.querySelectorAll('.g-chip-btn').forEach((b) => { b.disabled = false; });
    return;
  }
  // "Ready" says nothing of its own: its line is the waiting line, and that
  // is spoken only if the wait turns out to be one.
  if (!quietly && view.step !== 'ready') {
    newScreen();
    readView(view);
  }
  reset();
  drawTrail(view.answered);
  drawCost(view);
  const answered = view.answered.length;

  if (view.step === 'question') {
    guided.result = null;
    el('g-ask').textContent = view.question.ask;
    if (view.question.note) {
      el('g-choices').appendChild(node('div', 'g-note-line', view.question.note));
    }
    view.question.choices.forEach((choice) => {
      const button = node('button', 'g-choice' + (choice.id === 'any' ? ' any' : ''));
      button.dataset.choice = choice.id;
      button.appendChild(node('span', null, choice.label));
      if (choice.count && choice.id !== 'any') {
        button.appendChild(node('span', 'n', String(choice.count)));
      }
      button.addEventListener('click', () => guidedStep('answer', {
        session: view.session, question: view.question.id, choice: choice.id,
      }));
      el('g-choices').appendChild(button);
    });
    if (view.question.id === 'budget') {
      const form = node('form', 'g-choice');
      const input = node('input');
      input.type = 'number'; input.min = '1'; input.max = '999999999'; input.required = true;
      input.placeholder = view.lang === 'en' ? 'Maximum ₹' : 'அதிகபட்சம் ₹';
      input.setAttribute('aria-label', input.placeholder);
      const submit = node('button', 'g-chip-btn', view.lang === 'en' ? 'Set limit' : 'உறுதி செய்');
      form.append(input, submit);
      form.onsubmit = event => { event.preventDefault(); guidedStep('answer', {
        session: view.session, question: 'budget', choice: 'cap:' + input.value,
      }); };
      el('g-choices').appendChild(form);
    }
    el('g-back').hidden = answered === 0;
    return;
  }

  if (view.step === 'ready') {
    // Nothing left to ask. Said, shown, and asked for in the same breath:
    // a "show me" button here would be one more tap with only one answer.
    el('g-ask').textContent = view.say;
    waiting(view.say);
    guided.busy = false;
    return guidedStep('recommend', { session: view.session }, view.say, true);
  }

  if (view.step === 'nothing') {
    el('g-ask').textContent = view.say;
    drawRelax(view);
    el('g-back').hidden = false;
    el('g-restart').hidden = false;
    return;
  }

  if (view.step === 'result') {
    guided.result = view;
    el('g-ask').textContent = view.say;
    view.picks.forEach((pick, index) => el('g-picks').appendChild(pickButton(view, pick, index)));
    (view.more || []).forEach((way) => {
      const button = node('button', 'g-choice any', way.label);
      button.dataset.choice = way.id;
      button.addEventListener('click', () => guidedStep('refine', {
        session: view.session, kind: way.id,
      }));
      el('g-picks').appendChild(button);
    });
    drawRelax(view);
    el('g-back').hidden = false;
    el('g-restart').hidden = false;
    return;
  }

  if (view.step === 'card') {
    el('g-ask').textContent = view.say;
    drawCard(view.product);
    drawQuestions(view);
    el('g-restart').hidden = false;
  }
}

function drawQuestions(view) {
  const area = el('g-questions');
  area.hidden = false;
  clear('g-questions');
  el('g-answer').hidden = true;
  area.appendChild(node('div', 'g-invite', view.questions.invite));
  const row = node('div', 'g-chips');
  view.questions.choices.forEach((choice) => {
    const chip = node('button', 'g-chip-btn', choice.label);
    chip.dataset.choice = choice.id;
    chip.addEventListener('click', () => guidedStep('ask', {
      session: view.session, question: choice.id,
    }));
    row.appendChild(chip);
  });
  area.appendChild(row);
}

function priceLine(product) {
  const line = node('div', 'pr', rs(product.price));
  if (product.was) line.appendChild(node('span', 'g-was', rs(product.was)));
  return line;
}

// The cards ran out: one change at a time, each with how many more it finds.
function drawRelax(view) {
  (view.relax || []).forEach((offer) => {
    const button = node('button', 'g-choice g-relax', offer.label);
    button.dataset.choice = offer.id;
    button.addEventListener('click', () => guidedStep('answer', {
      session: view.session, question: 'relax', choice: offer.id,
    }));
    el('g-picks').appendChild(button);
  });
}

function pickButton(view, pick, index) {
  const button = node('button', 'g-pick');
  button.dataset.choice = pick.ref;
  // A, B, C, D… - the letter a customer can say: "C option-க்கு EMI இருக்கா?"
  const letter = 'ABCDEFGHIJ'[index] || '';
  const title = node('div', 'nm');
  if (letter) title.appendChild(node('span', 'g-letter', letter));
  title.appendChild(document.createTextNode(pick.name));
  button.appendChild(title);
  button.appendChild(priceLine(pick));
  if (pick.why) button.appendChild(node('div', 'why', pick.why));
  if (pick.over_budget) button.appendChild(node('div', 'g-note over', pick.over_budget));
  const notes = [];
  if (pick.stock_confirmed) notes.push(SAYS.inStock(pick.branches));
  else notes.push(SAYS.stockUnknown);
  if (pick.colours > 1) notes.push(SAYS.colours(pick.colours));
  button.appendChild(node('div', 'g-note' + (pick.stock_confirmed ? ' in' : ''), notes.join(' · ')));
  if (!pick.price_confirmed) button.appendChild(node('div', 'g-note', SAYS.priceUnsure));
  button.addEventListener('click', () => guidedStep('pick', {
    session: view.session, ref: pick.ref,
  }));
  return button;
}

function section(card, title, lines, firstLineClass) {
  const block = node('div', 'sec');
  block.appendChild(node('b', null, title));
  lines.forEach((line, index) => {
    block.appendChild(node('div', index === 0 ? firstLineClass : null, line));
  });
  card.appendChild(block);
}

function drawCard(product) {
  const card = clear('g-card');
  card.hidden = false;
  card.appendChild(node('h2', null, product.name));
  card.appendChild(priceLine(product));
  if (product.over_budget) card.appendChild(node('div', 'g-note over', product.over_budget));
  if (product.price_with_best_offer) {
    card.appendChild(node('div', 'g-note in', SAYS.withOffer(product.price_with_best_offer)));
  }
  if (!product.price_confirmed) card.appendChild(node('div', 'g-note', SAYS.priceUnsure));

  if (product.specs.length) {
    const list = node('dl');
    product.specs.forEach(([label, value]) => {
      list.appendChild(node('dt', null, label));
      list.appendChild(node('dd', null, value));
    });
    card.appendChild(list);
  }

  const stock = product.stock;
  const stockLine = stock.state === 'in' ? SAYS.inStock(stock.branches)
    : stock.state === 'out' ? SAYS.outOfStock : SAYS.stockUnknown;
  const stockLines = [stockLine];
  if (product.colours > 1) stockLines.push(SAYS.colours(product.colours));
  // "in" and "out" are also the two colours; unknown stays neutral.
  section(card, SAYS.stock, stockLines, 'g-stock ' + stock.state);

  if (product.offers.length) {
    section(card, SAYS.offers, product.offers.map((offer) => [
      offer.label,
      offer.amount ? SAYS.upTo(offer.amount) : null,
      // The condition is part of the offer, not a footnote to it.
      offer.condition,
    ].filter(Boolean).join(' · ')));
  }
  if (product.emi) section(card, SAYS.emiLabel, [SAYS.emi(product.emi)]);
  if (!product.checked_live) card.appendChild(node('div', 'g-note', SAYS.earlier));

  if (guided.result) {
    const others = node('button', 'g-link', SAYS.others);
    // Back to a list they have already been read the introduction to.
    others.addEventListener('click', () => { hush(); draw(guided.result, true); });
    card.appendChild(others);
  }
}

// --- the free voice conversation --------------------------------------------
//
// The owner's rule, held by construction: the question phase costs not one
// paisa. The BROWSER's own speech recognition hears the customer (free, on
// Chrome's side of the fence), the words go to /guided/said, and CODE -
// never a model - presses the buttons they mean. Only two things can ever
// cost: the one think, and a question about an already-picked product.
//
// One tap on the button starts the conversation; after that it carries
// itself - every time the agent finishes a line, the shop listens again.

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const convo = {on: false, rec: null, checking: false, hosted: null, mode: '', lang: 'auto', misses: 0,
  supported: !!(Recognition || navigator.mediaDevices?.getUserMedia)};

function convoLabel() {
  const button = el('g-mic');
  button.classList.toggle('on', convo.on);
  button.setAttribute('aria-pressed', String(convo.on));
  el('g-mic-label').textContent = convo.on ? SAYS.listening : SAYS.convo;
}

function drawMic() {
  el('g-mic').hidden = !convo.supported;
}

function showHeard(heard, said) {
  const line = el('g-heard');
  if (!heard || (!heard.text && !said)) { line.hidden = true; return; }
  line.hidden = false;
  line.textContent = (heard.text ? '“' + heard.text + '”' : '')
    + (said ? (heard.text ? ' — ' : '') + said : '');
}

function micFailure() {
  convo.on = false;
  stopListening();
  convoLabel();
  showHeard({text: ''}, SAYS.micBlocked);
}

async function listenSoon() {
  if (!convo.on || convo.checking || convo.rec || convo.hosted || guided.busy ||
      agentSpeaking() || el('guided-panel').hidden) return;
  convo.checking = true;
  try {
    const language = convo.lang === 'en' ? 'en-IN' : 'ta-IN';
    // On-device first, when the device has the language pack; otherwise
    // the browser's own recognition, which is free to Poorvika too. The
    // hosted worker is the last resort, for a browser with neither - a
    // machine without it installed must still hear (owner, 2026-11-24:
    // "ennala pesa mudiyatha").
    let local = false;
    try {
      local = Recognition?.available &&
        await Recognition.available({langs: [language], processLocally: true}) === 'available';
    } catch { /* no on-device pack: the browser's recognition still hears */ }
    const native = convo.mode !== 'hosted' && !!Recognition;
    if (!convo.on || agentSpeaking()) return;
    if (!native) {
      const mic = convo.hosted = new GuidedMicrophone({
        language: () => convo.lang, interrupted: () => {}, text: sendSaid, failed: micFailure,
      });
      await mic.start();
      return;
    }
    const rec = convo.rec = new Recognition();
    if (local) rec.processLocally = true;
    rec.lang = language;
    rec.interimResults = false;
    rec.onresult = e => {
      if (convo.rec !== rec) return;
      const text = e.results[0]?.[0]?.transcript;
      if (text) sendSaid(text);
    };
    rec.onerror = e => {
      if (convo.rec !== rec) return;
      if (e.error === 'not-allowed') micFailure();
      else if (!['aborted', 'no-speech'].includes(e.error)) convo.mode = 'hosted';
    };
    rec.onend = () => {
      if (convo.rec === rec) { convo.rec = null; setTimeout(listenSoon, 100); }
    };
    rec.start();
  } catch { micFailure(); }
  finally { convo.checking = false; }
}

function stopListening() {
  const rec = convo.rec;
  convo.rec = null;
  if (rec) { try { rec.abort(); } catch { /* already closed */ } }
  if (convo.hosted) { convo.hosted.stop(); convo.hosted = null; }
}

async function sendSaid(text, kind = 'speech') {
  if (guided.busy || !guided.session) { listenSoon(); return; }
  guided.busy = true;
  stopListening();
  waiting(SAYS.understanding);
  let view = null;
  try {
    const response = await fetch('/api/v1/guided/said', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ session: guided.session, text: text.slice(0, 300), kind }),
    });
    view = await response.json().catch(() => null);
    if (!response.ok) view = null;
  } catch (error) {
    view = null;
  }
  waiting(null);
  guided.busy = false;
  if (!view) { showHeard({ text: text }, SAYS.failed); listenSoon(); return; }
  if (view.step === 'heard') {
    convo.misses += 1;
    if (convo.misses > 1) {
      showHeard(view.heard, view.say);
      listenSoon();
    } else {
      draw(view);
    }
    return;
  }
  convo.misses = 0;
  const first = view.heard && view.heard.pressed && view.heard.pressed[0];
  const target = first && [...document.querySelectorAll('[data-choice]')]
    .find((b) => b.dataset.choice.toLowerCase() === String(first).toLowerCase());
  if (target) target.classList.add('pressed');
  setTimeout(() => {
    draw(view);
    showHeard(view.heard, '');
    listenSoon();
  }, target ? 400 : 0);
}

// The Voice Call tab, at ₹0 (owner, 2026-11-24): the same guided
// conversation with the mic on. The click that got us here is the gesture a
// browser wants before it plays sound or opens a microphone. The greeting
// is said first; the mic opens when it ends, as everywhere else.
function startGuidedVoice() {
  showTab('guided', true);  // starts the guided session if none is open
  if (!convo.supported) return;  // no microphone here: taps and typing still work
  voice.blocked = false;
  soundLabel();
  convo.on = true;
  convoLabel();
  listenSoon();
}

// Typing continues the same conversation as tapping and speaking: the
// words go down the same free path as speech, and the reply is drawn the
// same way. A tap of "send" is a deliberate act, so the agent is hushed.
el('g-compose').addEventListener('submit', (event) => {
  event.preventDefault();
  const box = el('g-text');
  const text = box.value.trim();
  if (!text || guided.busy || !guided.session) return;
  box.value = '';
  hush();
  sendSaid(text, 'text');
});

// Barge-in, opt-in with ?bargein=1. The page is half-duplex by default: the
// owner heard the agent cut itself off when the mic was open over its own
// voice. With the flag, a second mic stream WITH the browser's echo
// cancellation watches while the agent talks; only sustained, loud speech
// (BARGE_FRAMES x 50 ms) fades the agent out, and normal listening follows.
const BARGE_LEVEL = 0.06;
const BARGE_FRAMES = 6;
const bargeIn = {
  enabled: new URLSearchParams(location.search).has('bargein'),
  stream: null, analyser: null, timer: null, loud: 0,
};

async function watchForBargeIn() {
  if (!bargeIn.enabled || !convo.on || bargeIn.timer || !agentSpeaking()) return;
  try {
    if (!bargeIn.stream) {
      bargeIn.stream = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true},
      });
      const Context = window.AudioContext || window.webkitAudioContext;
      const context = new Context();
      bargeIn.analyser = context.createAnalyser();
      bargeIn.analyser.fftSize = 1024;
      context.createMediaStreamSource(bargeIn.stream).connect(bargeIn.analyser);
    }
  } catch {
    bargeIn.enabled = false;  // no microphone: stay half-duplex
    return;
  }
  const samples = new Float32Array(bargeIn.analyser.fftSize);
  bargeIn.loud = 0;
  bargeIn.timer = setInterval(() => {
    if (!agentSpeaking() || !convo.on) { stopBargeWatch(); return; }
    bargeIn.analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (const value of samples) sum += value * value;
    bargeIn.loud = Math.sqrt(sum / samples.length) > BARGE_LEVEL ? bargeIn.loud + 1 : 0;
    if (bargeIn.loud >= BARGE_FRAMES) {
      stopBargeWatch();
      hush();  // a fade, never a cut
      setTimeout(listenSoon, 0);
    }
  }, 50);
}

function stopBargeWatch() {
  clearInterval(bargeIn.timer);
  bargeIn.timer = null;
}

function releaseBargeIn() {
  stopBargeWatch();
  if (bargeIn.stream) bargeIn.stream.getTracks().forEach((track) => track.stop());
  bargeIn.stream = null;
  bargeIn.analyser = null;
}

el('g-mic').addEventListener('click', () => {
  convo.on = !convo.on;
  convoLabel();
  if (convo.on) { hush(); listenSoon(); } else { stopListening(); releaseBargeIn(); }
});

el('g-sound').addEventListener('click', () => {
  if (voice.on && voice.blocked) {
    // The tap this browser was waiting for. Say the line they missed.
    voice.blocked = false;
    soundLabel();
    return speak(voice.last);
  }
  voice.on = !voice.on;
  if (!voice.on) hush();
  soundLabel();
});

// Nobody on the chat tab wants a question finishing behind it.
function leaveGuided() {
  convo.on = false; stopListening(); hush(); convoLabel(); releaseBargeIn();
}
['tab:voice', 'tab:chat'].forEach(name => document.addEventListener(name, leaveGuided));
window.addEventListener('pagehide', leaveGuided);

el('g-back').addEventListener('click', () => guidedStep('back', { session: guided.session }));
el('g-restart').addEventListener('click', () => guidedStart());

// The shell owns the tabs; this only reacts to its own becoming visible.
document.addEventListener('tab:guided', () => { if (!guided.started) guidedStart(); });

// The guided tab is the one the page opens on, so it starts with the page.
// Starting costs nothing: it opens a session and puts the first question.
if (!el('guided-panel').hidden) guidedStart();
