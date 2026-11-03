// Microphone capture, on the audio thread.
//
// The AudioContext is created at the model's capture rate, so the browser
// resamples for us and this only has to convert float to signed 16-bit.
// Doing the conversion here rather than on the main thread keeps a busy UI
// from introducing gaps in the caller's audio.
//
// Frames are 20 ms to match everything else in the pipeline.

class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const frameMs = (options.processorOptions && options.processorOptions.frameMs) || 20;
    this.frameSamples = Math.round((sampleRate * frameMs) / 1000);
    this.buffer = new Int16Array(this.frameSamples);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      // Clamp before scaling: a sample slightly over 1.0 would wrap to a loud
      // negative and sound like a click.
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this.buffer[this.filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;

      if (this.filled === this.frameSamples) {
        // Copy, because the buffer is reused immediately.
        this.port.postMessage(this.buffer.slice());
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
