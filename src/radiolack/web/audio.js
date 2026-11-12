/*
 * Audio output: plays the engine's audio frames (stream.js) on this device,
 * through an AudioWorklet (audio-worklet.js) and a volume control. Browsers
 * may hold the audio until the user clicks, taps or presses a key on the
 * page; it then starts with that gesture.
 */

const WORKLET_URL = 'audio-worklet.js';
const PROCESSOR_NAME = 'radiolack-audio';
// The events that let a browser start the audio it holds (HTML's
// activation-triggering input events): a mouse press, a key press, and the
// end of a touch, since a tap counts only once the finger leaves the screen
// (Safari on the iPhone, for one).
const GESTURES = ['pointerdown', 'pointerup', 'touchend', 'keydown'];
const VOLUME_TIME_S = 0.02;     // volume changes glide over about this long, without clicks

/**
 * "off" until started, "starting" while the worklet loads, "blocked" while
 * the browser holds the audio until a gesture, "running" while it plays, and
 * "failed" when this browser cannot play it.
 * @typedef {'off' | 'starting' | 'blocked' | 'running' | 'failed'} AudioState
 */

export class AudioOutput {
  /** @type {AudioContext | null} */
  #context = null;
  /** @type {AudioWorkletNode | null} */
  #node = null;
  /** @type {GainNode | null} */
  #gain = null;
  /** @type {AudioState} */
  #state = 'off';
  #gainValue = 1;
  #onState;
  #resume = () => {
    // A gesture lets the browser start the audio it holds.
    if (this.#context) this.#context.resume().catch((error) => console.error('Radiolack: audio cannot start:', error));
  };

  /** @param {(state: AudioState) => void} onState */
  constructor(onState) {
    this.#onState = onState;
  }

  /** @returns {AudioState} */
  get state() {
    return this.#state;
  }

  /** Prepare the output: it plays as soon as the browser lets it. */
  async start() {
    if (this.#state !== 'off') return;
    this.#set('starting');
    try {
      // Safari's "playback" audio session, which iOS plays with the ringer
      // switch on silent; an iPhone also kept playing with its screen locked.
      // TypeScript's DOM types lack this API.
      const session = /** @type {Navigator & { audioSession?: { type: string } }} */ (navigator).audioSession;
      if (session) session.type = 'playback';
      const context = new AudioContext();
      this.#context = context;
      await context.audioWorklet.addModule(WORKLET_URL);
      const node = new AudioWorkletNode(context, PROCESSOR_NAME,
        { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
      const gain = new GainNode(context, { gain: this.#gainValue });
      node.connect(gain).connect(context.destination);
      this.#node = node;
      this.#gain = gain;
      context.addEventListener('statechange', () => this.#follow());
      this.#follow();
    } catch (error) {
      console.error('Radiolack: audio cannot play in this browser:', error);
      this.#stopListening();
      this.#set('failed');
    }
  }

  /**
   * Play an audio frame after the ones before it.
   * @param {import('./stream.js').AudioFrame} frame
   */
  play(frame) {
    if (this.#state !== 'running' || !this.#node) return;
    this.#node.port.postMessage({ type: 'frame', position: frame.position, rateHz: frame.rateHz, pcm: frame.pcm },
      [frame.pcm]);
  }

  /** Drop the audio buffered so far, such as when the engine's audio stops. */
  reset() {
    if (this.#node) this.#node.port.postMessage({ type: 'reset' });
  }

  /**
   * Set the volume: 0 is silent, 1 is full scale. The gain follows the
   * square of the volume, which sounds more even than a linear one.
   * @param {number} volume
   */
  setVolume(volume) {
    const clamped = Math.min(1, Math.max(0, volume));
    this.#gainValue = clamped * clamped;
    if (this.#gain && this.#context) {
      this.#gain.gain.setTargetAtTime(this.#gainValue, this.#context.currentTime, VOLUME_TIME_S);
    }
  }

  #follow() {
    const context = this.#context;
    if (!context || this.#state === 'failed') return;
    if (context.state === 'running') {
      this.#stopListening();
      this.#set('running');
    } else if (context.state === 'closed') {
      this.#stopListening();
      this.#set('failed');
    } else {
      // Suspended (or, in Safari, interrupted): wait for a gesture.
      for (const type of GESTURES) window.addEventListener(type, this.#resume, { capture: true });
      this.#set('blocked');
      context.resume().catch(() => {});     // works at once where the browser allows it
    }
  }

  #stopListening() {
    for (const type of GESTURES) window.removeEventListener(type, this.#resume, { capture: true });
  }

  /** @param {AudioState} state */
  #set(state) {
    if (state === this.#state) return;
    this.#state = state;
    this.#onState(state);
  }
}
