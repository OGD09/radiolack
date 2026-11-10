/*
 * Spectrum view drawn with WebGL2: background, grid, area under the trace,
 * peak hold, trace and peak markers. Its text (frequency and level scales)
 * lives on small 2D canvases that change only with the view.
 */
import { COVER_VERTEX_SHADER, createDataTexture, createProgram, getContext, uniformLocations, watchContext } from './gl.js';

// One fragment per pixel. Lines use the column-span technique: inside its
// column, a line through the column centres runs from the midpoint with the
// left neighbour, through the column's value, to the midpoint with the right
// neighbour; a pixel is covered by the part of that span within half the
// line width.
const PLOT_SHADER = `#version 300 es
precision highp float;
precision highp int;

uniform highp sampler2D u_columns;  // RG32F, one texel per column: y of the trace and of the peak hold
uniform float u_height;             // drawing buffer height, px
uniform float u_plotHeight;         // px from the top; the frequency scale lies below
uniform float u_traceWidth;         // px
uniform float u_peakWidth;          // px; 0 hides the peak hold
uniform bool u_fill;
uniform vec4 u_grid;                // first x, x spacing, first y, y spacing, px; a spacing of 0 hides
out vec4 colour;

const vec3 BACKGROUND = vec3(10.0, 12.0, 15.0) / 255.0;
const float GRID_ALPHA = 0.055;
const float BASELINE_ALPHA = 0.08;
const vec3 AREA = vec3(89.0, 167.0, 255.0) / 255.0;
const vec4 PEAK = vec4(242.0 / 255.0, 179.0 / 255.0, 61.0 / 255.0, 0.55);
const vec3 TRACE = vec3(214.0, 222.0, 232.0) / 255.0;

vec2 columnAt(int x) {
  int last = textureSize(u_columns, 0).x - 1;
  return texelFetch(u_columns, ivec2(clamp(x, 0, last), 0), 0).rg;
}

float lineCoverage(float y, float left, float value, float right, float width) {
  float a = 0.5 * (left + value), b = 0.5 * (value + right);
  float top = min(value, min(a, b)), bottom = max(value, max(a, b));
  float outside = max(top - y, y - bottom);
  return clamp(0.5 * width + 0.5 - outside, 0.0, 1.0);
}

// Whether one of the 1 px lines at round(first + k * spacing) covers the pixel centred on p.
bool onLine(float p, float first, float spacing) {
  if (spacing <= 0.0) return false;
  float k = floor((p - first) / spacing + 0.5);
  return floor(first + k * spacing + 0.5) == floor(p);
}

void main() {
  float x = gl_FragCoord.x, y = u_height - gl_FragCoord.y;
  vec3 c = BACKGROUND;
  if (y >= u_plotHeight) {
    if (floor(y) == u_plotHeight) c = mix(c, vec3(1.0), BASELINE_ALPHA);
    colour = vec4(c, 1.0);
    return;
  }
  if (onLine(x, u_grid.x, u_grid.y) || onLine(y, u_grid.z, u_grid.w)) c = mix(c, vec3(1.0), GRID_ALPHA);
  int column = int(x);
  vec2 here = columnAt(column), left = columnAt(column - 1), right = columnAt(column + 1);
  if (u_fill) c = mix(c, AREA, mix(0.30, 0.03, y / u_plotHeight) * clamp(y - here.x + 0.5, 0.0, 1.0));
  if (u_peakWidth > 0.0) c = mix(c, PEAK.rgb, PEAK.a * lineCoverage(y, left.y, here.y, right.y, u_peakWidth));
  c = mix(c, TRACE, lineCoverage(y, left.x, here.x, right.x, u_traceWidth));
  colour = vec4(c, 1.0);
}`;

const MARKER_VERTEX_SHADER = `#version 300 es
layout(location = 0) in vec2 a_corner;  // px from the top left
uniform vec2 u_size;                    // drawing buffer, px
void main() {
  gl_Position = vec4(a_corner.x / u_size.x * 2.0 - 1.0, 1.0 - a_corner.y / u_size.y * 2.0, 0.0, 1.0);
}`;

const MARKER_FRAGMENT_SHADER = `#version 300 es
precision mediump float;
uniform vec4 u_colour;
out vec4 colour;
void main() { colour = u_colour; }`;

/** Most peak markers drawn at once. */
export const MAX_MARKERS = 256;
const MARKER_COLOUR = [60 / 255, 207 / 255, 154 / 255, 0.9];
const PLOT_UNIFORMS = /** @type {const} */ (['u_columns', 'u_height', 'u_plotHeight', 'u_traceWidth', 'u_peakWidth', 'u_fill', 'u_grid']);
const MARKER_UNIFORMS = /** @type {const} */ (['u_size', 'u_colour']);

/**
 * What to draw, in drawing-buffer px.
 * @typedef {object} SpectrumScene
 * @property {Float32Array} columns  for each column, the y of the trace then of the peak hold, from the top
 * @property {number} plotHeight     whole px; the frequency scale lies below
 * @property {boolean} fill
 * @property {number} traceWidth
 * @property {number} peakWidth      0 hides the peak hold
 * @property {[number, number, number, number] | null} grid  first x, x spacing, first y, y spacing
 * @property {Float32Array} markers  triangle corners, x then y
 * @property {number} markerCount    triangles to draw
 */

/**
 * @typedef {object} SpectrumResources
 * @property {WebGLProgram} plot
 * @property {Record<typeof PLOT_UNIFORMS[number], WebGLUniformLocation | null>} plotUniforms
 * @property {WebGLProgram} marker
 * @property {Record<typeof MARKER_UNIFORMS[number], WebGLUniformLocation | null>} markerUniforms
 * @property {WebGLVertexArrayObject} coverArray
 * @property {WebGLVertexArrayObject} markerArray
 * @property {WebGLBuffer} markerBuffer
 * @property {WebGLTexture} columns
 */

export class SpectrumView {
  #canvas;
  #gl;
  #onRestore;
  #lost = false;
  /** @type {SpectrumResources | null} */
  #res = null;

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {() => void} onRestore  called when a lost context is back, to draw again
   */
  constructor(canvas, onRestore) {
    this.#canvas = canvas;
    this.#gl = getContext(canvas);
    this.#onRestore = onRestore;
    this.#res = this.#build();
    watchContext(canvas, this.#gl, (lost) => { this.#lost = lost; }, () => {
      this.#res = null;
      this.#res = this.#build();
      this.#onRestore();
    });
  }

  /** Columns of the drawing buffer: the length of a scene's columns is twice this. */
  get width() { return this.#canvas.width; }

  get height() { return this.#canvas.height; }

  /**
   * Size the drawing buffer, within what the GPU allows. Its content is lost.
   * @param {number} width
   * @param {number} height
   */
  resize(width, height) {
    const max = this.#lost ? width : this.#gl.getParameter(this.#gl.MAX_TEXTURE_SIZE);
    this.#canvas.width = Math.max(2, Math.min(max, Math.round(width)));
    this.#canvas.height = Math.max(2, Math.round(height));
    if (this.#res) this.#allocateColumns(this.#res.columns);
  }

  /** @param {SpectrumScene} scene */
  draw(scene) {
    const gl = this.#gl, res = this.#res;
    if (this.#lost || !res) return;
    const { width, height } = this.#canvas;
    if (scene.columns.length < 2 * width) throw new RangeError('spectrum scene: too few columns');
    gl.viewport(0, 0, width, height);
    gl.disable(gl.BLEND);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, res.columns);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, width, 1, gl.RG, gl.FLOAT, scene.columns);
    gl.useProgram(res.plot);
    const u = res.plotUniforms;
    gl.uniform1i(u.u_columns, 0);
    gl.uniform1f(u.u_height, height);
    gl.uniform1f(u.u_plotHeight, scene.plotHeight);
    gl.uniform1f(u.u_traceWidth, scene.traceWidth);
    gl.uniform1f(u.u_peakWidth, scene.peakWidth);
    gl.uniform1i(u.u_fill, scene.fill ? 1 : 0);
    gl.uniform4fv(u.u_grid, scene.grid || [0, 0, 0, 0]);
    gl.bindVertexArray(res.coverArray);
    gl.drawArrays(gl.TRIANGLES, 0, 3);

    const count = Math.min(MAX_MARKERS, scene.markerCount);
    if (count > 0) {
      gl.useProgram(res.marker);
      gl.uniform2f(res.markerUniforms.u_size, width, height);
      gl.uniform4fv(res.markerUniforms.u_colour, MARKER_COLOUR);
      gl.bindVertexArray(res.markerArray);
      gl.bindBuffer(gl.ARRAY_BUFFER, res.markerBuffer);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, scene.markers, 0, count * 6);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.drawArrays(gl.TRIANGLES, 0, count * 3);
    }
    gl.bindVertexArray(null);
  }

  /** @returns {SpectrumResources} */
  #build() {
    const gl = this.#gl;
    const plot = createProgram(gl, COVER_VERTEX_SHADER, PLOT_SHADER);
    const marker = createProgram(gl, MARKER_VERTEX_SHADER, MARKER_FRAGMENT_SHADER);
    const coverArray = gl.createVertexArray();
    const markerArray = gl.createVertexArray();
    const markerBuffer = gl.createBuffer();
    if (!coverArray || !markerArray || !markerBuffer) throw new Error('cannot create WebGL buffers');
    gl.bindVertexArray(markerArray);
    gl.bindBuffer(gl.ARRAY_BUFFER, markerBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, MAX_MARKERS * 6 * 4, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    const columns = createDataTexture(gl);
    this.#allocateColumns(columns);
    return {
      plot, plotUniforms: uniformLocations(gl, plot, PLOT_UNIFORMS),
      marker, markerUniforms: uniformLocations(gl, marker, MARKER_UNIFORMS),
      coverArray, markerArray, markerBuffer, columns,
    };
  }

  /** @param {WebGLTexture} texture */
  #allocateColumns(texture) {
    const gl = this.#gl;
    if (this.#lost) return;
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, this.#canvas.width, 1, 0, gl.RG, gl.FLOAT, null);
  }
}
