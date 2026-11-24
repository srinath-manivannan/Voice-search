// Record on this device. Only a completed turn is sent to Poorvika's own worker.
class GuidedMicrophone {
  constructor({language, interrupted, text, failed}) {
    Object.assign(this, {language, interrupted, text, failed});
    this.generation = 0;
  }

  async start() {
    const version = ++this.generation;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {echoCancellation: true, noiseSuppression: true, channelCount: 1},
    });
    if (version !== this.generation) { stream.getTracks().forEach(t => t.stop()); return; }
    this.stream = stream;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = this.context = new AudioCtx({sampleRate: 16000});
    await ctx.resume();
    await ctx.audioWorklet.addModule('/static/capture-worklet.js');
    if (version !== this.generation) return;
    const node = new AudioWorkletNode(ctx, 'capture-processor');
    const source = ctx.createMediaStreamSource(stream);
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(node).connect(mute).connect(ctx.destination);
    let frames = [], preroll = [], loud = 0, quiet = 0, speaking = false;
    node.port.onmessage = ({data}) => {
      if (version !== this.generation || this.sending || guided.busy) return;
      let sum = 0;
      for (const n of data) sum += n * n;
      const amplitude = Math.sqrt(sum / data.length) / 32768;
      loud = amplitude > 0.022 ? loud + 1 : 0;
      if (!speaking) {
        preroll.push(data);
        if (preroll.length > 14) preroll.shift();
        if (loud < 4) return;
        frames = preroll;
        preroll = [];
        quiet = 0;
        speaking = true;
        this.interrupted();
        return;
      }
      frames.push(data);
      quiet = amplitude < 0.014 ? quiet + 1 : 0;
      if (quiet < 55 && frames.length < 1480) return;
      speaking = false;
      loud = 0;
      const body = new Blob(frames, {type: 'application/octet-stream'});
      frames = [];
      this.sending = true;
      const abort = this.abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), 20000);
      fetch('/api/v1/guided/transcribe?' + new URLSearchParams({
        session: guided.session, lang: this.language(),
      }), {method: 'POST', body, signal: abort.signal})
        .then(r => { if (!r.ok) throw Error('speech unavailable'); return r.json(); })
        .then(result => { if (version === this.generation && result.text) this.text(result.text); })
        .catch(() => { if (version === this.generation) this.failed(); })
        .finally(() => { clearTimeout(timer); if (version === this.generation) this.sending = false; });
    };
  }

  stop() {
    this.generation++;
    if (this.abort) this.abort.abort();
    if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    if (this.context) this.context.close().catch(() => {});
    this.context = this.stream = null;
    this.sending = false;
  }
}
