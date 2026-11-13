/*
 * Waterfall view drawn with WebGL2. The history keeps one row of level codes
 * per line. Each pixel column shows the highest level among the bins it
 * covers, coloured through a 256-entry table: palette, contrast and zoom
 * recolour the whole history at once. Each line also keeps where it lies in
 * the span, so that a span that moves keeps the lines at their frequencies.
 */
import { COVER_VERTEX_SHADER, createDataTexture, createProgram, getContext, uniformLocations, watchContext } from './gl.js';

const WATERFALL_SHADER = `#version 300 es
precision highp float;
precision highp int;

uniform highp sampler2D u_history;  // R8: one row per line, one texel per bin or group of bins
uniform highp sampler2D u_offsets;  // R32F, one texel per line: where its first bin lies, in bins from the span's first bin
uniform highp sampler2D u_colours;  // RGBA8, 256 x 1: the colour of each level code
uniform float u_height;             // drawing buffer height, px: one line per px row, newest at the top
uniform int u_head;                 // row of the newest line
uniform int u_lines;                // lines in the history
uniform float u_firstTexel;         // texel position of the left edge
uniform float u_texelsPerPixel;
uniform float u_binsPerTexel;
out vec4 colour;

const int MAX_TAPS = 64;            // texel reads per pixel; wider columns skip texels evenly

void main() {
  ivec2 size = textureSize(u_history, 0);
  int age = int(u_height - gl_FragCoord.y);
  if (age >= u_lines) {
    colour = vec4(0.0, 0.0, 0.0, 1.0);
    return;
  }
  int row = (u_head - age + size.y) % size.y;
  float shift = texelFetch(u_offsets, ivec2(row, 0), 0).r / u_binsPerTexel;
  float x = floor(gl_FragCoord.x);
  int a = int(floor(u_firstTexel + x * u_texelsPerPixel - shift));
  int b = max(int(floor(u_firstTexel + (x + 1.0) * u_texelsPerPixel - shift)), a + 1);
  // The line's texels whose middle lies within the span: what left the span is not shown.
  a = max(a, max(0, int(ceil(-shift - 0.5))));
  b = min(b, min(size.x, int(ceil(float(size.x) - shift - 0.5))));
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
  'u_history', 'u_offsets', 'u_colours', 'u_height', 'u_head', 'u_lines', 'u_firstTexel', 'u_texelsPerPixel', 'u_binsPerTexel',
]);
const NO_OFFSET = new Float32Array(1);

/**
 * What to draw: the newest lines from the top of the canvas down, between
 * two bin positions (fractional bins from the start of the span).
 * @typedef {object} WaterfallScene
 * @property {number} head          row of the newest line
 * @property {number} lines         lines in the history
 * @property {number} firstBin      bin position of the left edge
 * @property {number} binsPerPixel
 */

/**
 * @typedef {object} WaterfallResources
 * @property {WebGLProgram} program
 * @property {Record<typeof UNIFORMS[number], WebGLUniformLocation | null>} uniforms
 * @property {WebGLVertexArrayObject} coverArray
 * @property {WebGLTexture} history
 * @property {WebGLTexture} offsets
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
  #bins = 0;                        // bins per line
  #group = 1;                       // bins per texel: more than 1 only beyond the GPU's texture width
  #texels = 0;                      // texels per row
  #reduced = new Uint8Array(0);     // one line grouped into texels
  /** @type {Float32Array} */
  #offsets;                         // each line's offset, in bins, as the GPU has it unless the context is lost

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {number} rows  lines kept in the history
   * @param {() => void} onRestore  called when a lost context is back, to upload the history and colours again;
   *   the view keeps the lines' offsets itself
   */
  constructor(canvas, rows, onRestore) {
    this.#canvas = canvas;
    this.#gl = getContext(canvas);
    this.#rows = rows;
    this.#offsets = new Float32Array(rows);
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
   * Set the number of bins per line and clear the history.
   * @param {number} bins
   */
  setBins(bins) {
    this.#bins = bins;
    this.#offsets.fill(0);
    if (this.#lost || !this.#res) return;
    this.#allocateHistory(this.#res.history);
    this.#uploadOffsets(this.#res.offsets);
  }

  /**
   * Store one line of level codes, taken in the span as it is now.
   * @param {number} row
   * @param {Uint8Array} levels  one code per bin
   */
  uploadRow(row, levels) {
    const gl = this.#gl, res = this.#res, shifted = this.#offsets[row] !== 0;
    this.#offsets[row] = 0;
    if (this.#lost || !res || !this.#texels) return;
    this.#storeRow(res, row, levels);
    if (!shifted) return;
    gl.bindTexture(gl.TEXTURE_2D, res.offsets);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, row, 0, 1, 1, gl.RED, gl.FLOAT, NO_OFFSET);
  }

  /**
   * Store the whole history at once, row after row, where each line lies
   * staying as it is.
   * @param {Uint8Array} history
   */
  uploadAll(history) {
    const gl = this.#gl, res = this.#res, bins = this.#bins;
    if (this.#lost || !res || !this.#texels) return;
    if (history.length !== bins * this.#rows) throw new RangeError('waterfall history: wrong size');
    if (this.#group === 1) {
      gl.bindTexture(gl.TEXTURE_2D, res.history);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, bins, this.#rows, gl.RED, gl.UNSIGNED_BYTE, history);
      return;
    }
    for (let row = 0; row < this.#rows; row += 1) this.#storeRow(res, row, history.subarray(row * bins, (row + 1) * bins));
  }

  /**
   * Place every line after the span moved: its frequencies stay.
   * @param {Float32Array} offsets  one per row: where the line's first bin lies, in bins from the span's first bin
   */
  setOffsets(offsets) {
    if (offsets.length !== this.#rows) throw new RangeError('waterfall offsets: wrong size');
    this.#offsets.set(offsets);
    if (!this.#lost && this.#res) this.#uploadOffsets(this.#res.offsets);
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
    const gl = this.#gl, res = this.#res;
    if (this.#lost || !res || !this.#texels) return;
    const { width, height } = this.#canvas;
    gl.viewport(0, 0, width, height);
    gl.disable(gl.BLEND);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, res.history);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, res.colours);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, res.offsets);
    gl.useProgram(res.program);
    const u = res.uniforms;
    gl.uniform1i(u.u_history, 0);
    gl.uniform1i(u.u_colours, 1);
    gl.uniform1i(u.u_offsets, 2);
    gl.uniform1f(u.u_height, height);
    gl.uniform1i(u.u_head, scene.head);
    gl.uniform1i(u.u_lines, scene.lines);
    gl.uniform1f(u.u_firstTexel, scene.firstBin / this.#group);
    gl.uniform1f(u.u_texelsPerPixel, scene.binsPerPixel / this.#group);
    gl.uniform1f(u.u_binsPerTexel, this.#group);
    gl.bindVertexArray(res.coverArray);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindVertexArray(null);
    gl.activeTexture(gl.TEXTURE0);
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
    const history = createDataTexture(gl);
    this.#allocateHistory(history);
    const offsets = createDataTexture(gl);
    this.#uploadOffsets(offsets);
    return { program, uniforms: uniformLocations(gl, program, UNIFORMS), coverArray, history, offsets, colours };
  }

  /** @param {WebGLTexture} texture */
  #uploadOffsets(texture) {
    const gl = this.#gl;
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, this.#rows, 1, 0, gl.RED, gl.FLOAT, this.#offsets);
  }

  /**
   * @param {WaterfallResources} res
   * @param {number} row
   * @param {Uint8Array} levels
   */
  #storeRow(res, row, levels) {
    const gl = this.#gl;
    gl.bindTexture(gl.TEXTURE_2D, res.history);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, row, this.#texels, 1, gl.RED, gl.UNSIGNED_BYTE, this.#grouped(levels));
  }

  /** @param {WebGLTexture} texture */
  #allocateHistory(texture) {
    const gl = this.#gl;
    if (this.#lost || !this.#bins) return;
    const max = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    this.#group = Math.ceil(this.#bins / max);
    this.#texels = Math.ceil(this.#bins / this.#group);
    this.#reduced = new Uint8Array(this.#group > 1 ? this.#texels : 0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, this.#texels, this.#rows, 0, gl.RED, gl.UNSIGNED_BYTE, null);
  }

  /**
   * One line as stored: grouped into the highest code of each group of bins
   * when a line is wider than the GPU allows.
   * @param {Uint8Array} levels
   */
  #grouped(levels) {
    const group = this.#group;
    if (group === 1) return levels;
    const out = this.#reduced, bins = this.#bins;
    for (let texel = 0, bin = 0; texel < out.length; texel += 1) {
      let top = 0;
      for (const end = Math.min(bins, bin + group); bin < end; bin += 1) if (levels[bin] > top) top = levels[bin];
      out[texel] = top;
    }
    return out;
  }
}
