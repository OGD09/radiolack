/*
 * Waterfall view drawn with WebGL2. A history keeps one row of level codes
 * per line. Each pixel column shows the highest level among the bins it
 * covers, coloured through a 256-entry table: palette, contrast and zoom
 * recolour the whole history at once. Each line also keeps where it lies in
 * the capture, so that a capture that moves keeps the lines at their
 * frequencies. The view holds two histories, each at its own resolution:
 * the wide spectrum's (layer 0) and the fine spectrum's (layer 1), and draws
 * one. A span too narrow to see shows as a marker the height of the view.
 * While the time shown changes, a scale stretches the lines over the height
 * until the history is drawn again at the new speed.
 */
import { COVER_VERTEX_SHADER, createDataTexture, createProgram, getContext, uniformLocations, watchContext } from './gl.js';

const WATERFALL_SHADER = `#version 300 es
precision highp float;
precision highp int;

uniform highp sampler2D u_history;  // R8: one row per line, one texel per bin or group of bins
uniform highp sampler2D u_offsets;  // R32F, one texel per line: where its first bin lies, in bins from the capture's first bin
uniform highp sampler2D u_colours;  // RGBA8, 256 x 1: the colour of each level code
uniform float u_height;             // drawing buffer height, px: one line per px row, newest at the top
uniform float u_scale;              // lines per px row: other than 1 while the time shown changes
uniform int u_head;                 // row of the newest line
uniform int u_lines;                // lines in the history
uniform float u_firstTexel;         // texel position of the left edge
uniform float u_texelsPerPixel;
uniform float u_binsPerTexel;
uniform vec2 u_window;              // the texels of a line that show the air, first and end, in the line's own texels
uniform vec2 u_marker;              // x of the edges of the marker that stands for a narrow span, px; empty without one
out vec4 colour;

const vec4 MARKER = vec4(vec3(214.0, 222.0, 232.0) / 255.0 * 0.5, 1.0);    // the spectrum's marker, over black

const int MAX_TAPS = 64;            // texel reads per pixel; wider columns skip texels evenly

void main() {
  if (gl_FragCoord.x >= u_marker.x && gl_FragCoord.x < u_marker.y) {
    colour = MARKER;
    return;
  }
  ivec2 size = textureSize(u_history, 0);
  int age = int(floor(floor(u_height - gl_FragCoord.y) * u_scale));
  if (age >= u_lines) {
    colour = vec4(0.0, 0.0, 0.0, 1.0);
    return;
  }
  int row = (u_head - age + size.y) % size.y;
  float shift = texelFetch(u_offsets, ivec2(row, 0), 0).r / u_binsPerTexel;
  float x = floor(gl_FragCoord.x);
  int a = int(floor(u_firstTexel + x * u_texelsPerPixel - shift));
  int b = max(int(floor(u_firstTexel + (x + 1.0) * u_texelsPerPixel - shift)), a + 1);
  // The line's texels within its window whose middle lies within the capture's window, as it is now:
  // what left it is not shown.
  int first = int(u_window.x), end = min(size.x, int(u_window.y));
  a = max(a, max(first, int(ceil(u_window.x - shift - 0.5))));
  b = min(b, min(end, int(ceil(u_window.y - shift - 0.5))));
  if (b <= a) {
    colour = vec4(0.0, 0.0, 0.0, 1.0);
    return;
  }
  int stride = (b - a + MAX_TAPS - 1) / MAX_TAPS;
  float level = 0.0;
  for (int i = a; i < b; i += stride) level = max(level, texelFetch(u_history, ivec2(i, row), 0).r);
  colour = texelFetch(u_colours, ivec2(int(level * 255.0 + 0.5), 0), 0);
}`;

const UNIFORMS = /** @type {const} */ ([
  'u_history', 'u_offsets', 'u_colours', 'u_height', 'u_scale', 'u_head', 'u_lines', 'u_firstTexel', 'u_texelsPerPixel', 'u_binsPerTexel',
  'u_window', 'u_marker',
]);
const NO_MARKER = [0, 0];
const NO_OFFSET = new Float32Array(1);
/** The histories a view holds: the wide spectrum's, then the fine spectrum's. */
const LAYERS = 2;

/**
 * What to draw: the newest lines from the top of the canvas down, between
 * two bin positions (fractional bins from the start of the capture, bin i
 * spanning [i, i + 1)).
 * @typedef {object} WaterfallScene
 * @property {number} head          row of the newest line
 * @property {number} lines         lines in the history
 * @property {number} firstBin      bin position of the left edge
 * @property {number} binsPerPixel
 * @property {number} [scale]       lines per pixel row: 1, the default, but while the time shown changes
 * @property {number} [layer]       the history drawn: 0, the default, or 1
 * @property {[number, number] | null} [window]  the bins of each line that show the air, first and end; all of them by default
 * @property {[number, number] | null} [marker]  x of the edges of the marker that stands for a span too narrow to see, px
 */

/**
 * One history on the GPU.
 * @typedef {object} Layer
 * @property {number} bins          bins per line; 0 while unused
 * @property {number} group         bins per texel: more than 1 only beyond the GPU's texture width
 * @property {number} texels        texels per row
 * @property {Uint8Array} reduced   one line grouped into texels
 * @property {Float32Array} offsets each line's offset, in bins, as the GPU has it unless the context is lost
 */

/**
 * @typedef {object} LayerTextures
 * @property {WebGLTexture} history
 * @property {WebGLTexture} offsets
 */

/**
 * @typedef {object} WaterfallResources
 * @property {WebGLProgram} program
 * @property {Record<typeof UNIFORMS[number], WebGLUniformLocation | null>} uniforms
 * @property {WebGLVertexArrayObject} coverArray
 * @property {LayerTextures[]} layers
 * @property {WebGLTexture} colours
 */

export class WaterfallView {
  #canvas;
  #gl;
  #rows;
  #onRestore;
  #lost = false;
  /** @type {WaterfallResources | null} */
  #res = null;
  /** @type {Layer[]} */
  #layers;

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {number} rows  lines kept in each history
   * @param {() => void} onRestore  called when a lost context is back, to upload the histories and colours again;
   *   the view keeps the lines' offsets itself
   */
  constructor(canvas, rows, onRestore) {
    this.#canvas = canvas;
    this.#gl = getContext(canvas);
    this.#rows = rows;
    this.#layers = Array.from({ length: LAYERS }, () => ({
      bins: 0, group: 1, texels: 0, reduced: new Uint8Array(0), offsets: new Float32Array(rows),
    }));
    this.#onRestore = onRestore;
    this.#res = this.#build();
    watchContext(canvas, this.#gl, (lost) => { this.#lost = lost; }, () => {
      this.#res = null;
      this.#res = this.#build();
      this.#onRestore();
    });
  }

  get width() { return this.#canvas.width; }

  /** Lines shown: one per row of the drawing buffer. */
  get height() { return this.#canvas.height; }

  /**
   * Size the drawing buffer, within what the GPU allows. Its content is lost.
   * @param {number} width
   * @param {number} height
   */
  resize(width, height) {
    const max = this.#lost ? Infinity : this.#gl.getParameter(this.#gl.MAX_TEXTURE_SIZE);
    this.#canvas.width = Math.max(2, Math.min(max, Math.round(width)));
    this.#canvas.height = Math.max(2, Math.min(max, Math.round(height)));
  }

  /**
   * Set the number of bins per line of a history and clear it.
   * @param {number} bins
   * @param {number} [layer]
   */
  setBins(bins, layer = 0) {
    const state = this.#layer(layer);
    state.bins = bins;
    state.offsets.fill(0);
    if (this.#lost || !this.#res) return;
    this.#allocateHistory(state, this.#res.layers[layer].history);
    this.#uploadOffsets(state, this.#res.layers[layer].offsets);
  }

  /**
   * Store one line of level codes, taken in the capture as it is now.
   * @param {number} row
   * @param {Uint8Array} levels  one code per bin
   * @param {number} [layer]
   */
  uploadRow(row, levels, layer = 0) {
    const gl = this.#gl, res = this.#res, state = this.#layer(layer), shifted = state.offsets[row] !== 0;
    state.offsets[row] = 0;
    if (this.#lost || !res || !state.texels) return;
    this.#storeRow(state, res.layers[layer].history, row, levels);
    if (!shifted) return;
    gl.bindTexture(gl.TEXTURE_2D, res.layers[layer].offsets);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, row, 0, 1, 1, gl.RED, gl.FLOAT, NO_OFFSET);
  }

  /**
   * Store a whole history at once, row after row, where each line lies
   * staying as it is.
   * @param {Uint8Array} history
   * @param {number} [layer]
   */
  uploadAll(history, layer = 0) {
    const gl = this.#gl, res = this.#res, state = this.#layer(layer), bins = state.bins;
    if (this.#lost || !res || !state.texels) return;
    if (history.length !== bins * this.#rows) throw new RangeError('waterfall history: wrong size');
    const texture = res.layers[layer].history;
    if (state.group === 1) {
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, bins, this.#rows, gl.RED, gl.UNSIGNED_BYTE, history);
      return;
    }
    for (let row = 0; row < this.#rows; row += 1) this.#storeRow(state, texture, row, history.subarray(row * bins, (row + 1) * bins));
  }

  /**
   * Place every line of a history after the capture moved: its frequencies stay.
   * @param {Float32Array} offsets  one per row: where the line's first bin lies, in bins from the capture's first bin
   * @param {number} [layer]
   */
  setOffsets(offsets, layer = 0) {
    const state = this.#layer(layer);
    if (offsets.length !== this.#rows) throw new RangeError('waterfall offsets: wrong size');
    state.offsets.set(offsets);
    if (!this.#lost && this.#res) this.#uploadOffsets(state, this.#res.layers[layer].offsets);
  }

  /**
   * Set the colour of each level code.
   * @param {Uint8Array} rgba  256 colours, 4 bytes each
   */
  setColours(rgba) {
    const gl = this.#gl, res = this.#res;
    if (this.#lost || !res) return;
    gl.bindTexture(gl.TEXTURE_2D, res.colours);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 1, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
  }

  /** @param {WaterfallScene} scene */
  draw(scene) {
    const gl = this.#gl, res = this.#res, layer = scene.layer ?? 0, state = this.#layer(layer);
    if (this.#lost || !res || !state.texels) return;
    const { width, height } = this.#canvas, group = state.group;
    const [first, end] = scene.window || [0, state.bins];
    gl.viewport(0, 0, width, height);
    gl.disable(gl.BLEND);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, res.layers[layer].history);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, res.colours);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, res.layers[layer].offsets);
    gl.useProgram(res.program);
    const u = res.uniforms;
    gl.uniform1i(u.u_history, 0);
    gl.uniform1i(u.u_colours, 1);
    gl.uniform1i(u.u_offsets, 2);
    gl.uniform1f(u.u_height, height);
    gl.uniform1f(u.u_scale, scene.scale ?? 1);
    gl.uniform1i(u.u_head, scene.head);
    gl.uniform1i(u.u_lines, scene.lines);
    gl.uniform1f(u.u_firstTexel, scene.firstBin / group);
    gl.uniform1f(u.u_texelsPerPixel, scene.binsPerPixel / group);
    gl.uniform1f(u.u_binsPerTexel, group);
    gl.uniform2f(u.u_window, Math.floor(first / group), Math.ceil(end / group));
    gl.uniform2fv(u.u_marker, scene.marker || NO_MARKER);
    gl.bindVertexArray(res.coverArray);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindVertexArray(null);
    gl.activeTexture(gl.TEXTURE0);
  }

  /**
   * @param {number} layer
   * @returns {Layer}
   */
  #layer(layer) {
    const state = this.#layers[layer];
    if (!state) throw new RangeError(`waterfall layer: ${layer}`);
    return state;
  }

  /** @returns {WaterfallResources} */
  #build() {
    const gl = this.#gl;
    const program = createProgram(gl, COVER_VERTEX_SHADER, WATERFALL_SHADER);
    const coverArray = gl.createVertexArray();
    if (!coverArray) throw new Error('cannot create a WebGL vertex array');
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    const colours = createDataTexture(gl);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    const layers = this.#layers.map((state) => {
      const history = createDataTexture(gl);
      this.#allocateHistory(state, history);
      const offsets = createDataTexture(gl);
      this.#uploadOffsets(state, offsets);
      return { history, offsets };
    });
    return { program, uniforms: uniformLocations(gl, program, UNIFORMS), coverArray, layers, colours };
  }

  /**
   * @param {Layer} state
   * @param {WebGLTexture} texture
   */
  #uploadOffsets(state, texture) {
    const gl = this.#gl;
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, this.#rows, 1, 0, gl.RED, gl.FLOAT, state.offsets);
  }

  /**
   * @param {Layer} state
   * @param {WebGLTexture} texture
   * @param {number} row
   * @param {Uint8Array} levels
   */
  #storeRow(state, texture, row, levels) {
    const gl = this.#gl;
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, row, state.texels, 1, gl.RED, gl.UNSIGNED_BYTE, this.#grouped(state, levels));
  }

  /**
   * A history's texture, sized for its bins; none is allocated while it has none.
   * @param {Layer} state
   * @param {WebGLTexture} texture
   */
  #allocateHistory(state, texture) {
    const gl = this.#gl;
    if (this.#lost || !state.bins) return;
    const max = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    state.group = Math.ceil(state.bins / max);
    state.texels = Math.ceil(state.bins / state.group);
    state.reduced = new Uint8Array(state.group > 1 ? state.texels : 0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, state.texels, this.#rows, 0, gl.RED, gl.UNSIGNED_BYTE, null);
  }

  /**
   * One line as stored: grouped into the highest code of each group of bins
   * when a line is wider than the GPU allows.
   * @param {Layer} state
   * @param {Uint8Array} levels
   */
  #grouped(state, levels) {
    const group = state.group;
    if (group === 1) return levels;
    const out = state.reduced, bins = state.bins;
    for (let texel = 0, bin = 0; texel < out.length; texel += 1) {
      let top = 0;
      for (const end = Math.min(bins, bin + group); bin < end; bin += 1) if (levels[bin] > top) top = levels[bin];
      out[texel] = top;
    }
    return out;
  }
}
