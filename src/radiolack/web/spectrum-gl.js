/*
 * Spectrum view drawn with WebGL2: background, grid, area under the trace,
 * peak hold, trace and peak markers in the span, and a decor outside it
 * that shows nothing measured. Its text (frequency and level scales) lives
 * on small 2D canvases that change only with the view.
 */
import { COVER_VERTEX_SHADER, createDataTexture, createProgram, getContext, uniformLocations, watchContext } from './gl.js';

// One fragment per pixel. Lines use the column-span technique: inside its
// column, a line through the column centres runs from the midpoint with the
// left neighbour, through the column's value, to the midpoint with the right
// neighbour; a pixel is covered by the part of that span within half the
// line width.
//
// Outside the span nothing is measured, and the decor says so: the span
// stands as a lit window whose light fades away from its edges into a darker
// background, where the grid goes on out of focus and fades too, and the
// band plan's colours rise faintly from the band strip. Nothing stands where
// the trace would be. Beyond the receiver's reach, hatching as well.
const PLOT_SHADER = `#version 300 es
precision highp float;
precision highp int;

uniform highp sampler2D u_columns;  // RG32F, one texel per column: y of the trace and of the peak hold
uniform sampler2D u_glow;           // RGBA8, one texel per column: its band's colour, alpha 0 without one
uniform float u_height;             // drawing buffer height, px
uniform float u_plotHeight;         // px from the top; the frequency scale lies below
uniform float u_traceWidth;         // px
uniform float u_peakWidth;          // px; 0 hides the peak hold
uniform bool u_fill;
uniform vec4 u_grid;                // first x, x spacing, first y, y spacing, px; a spacing of 0 hides
uniform vec2 u_span;                // x of the span's edges, px: the data lies between them
uniform vec2 u_reach;               // x of the edges of the receiver's reach, px
uniform float u_scale;              // px per CSS px
uniform float u_fade;               // 0 to 1: how far the span's data fades while the receiver moves
out vec4 colour;

const vec3 BACKGROUND = vec3(10.0, 12.0, 15.0) / 255.0;
const vec3 OUTSIDE = vec3(6.0, 7.0, 9.0) / 255.0;
const vec3 DARK = vec3(3.0, 4.0, 5.0) / 255.0;      // the background far from the span
const float LIGHT_FALLOFF = 90.0;   // CSS px from the span's edge over which its light falls to 1/e
const float GRID_ALPHA = 0.055;
const float SOFT_GRID_ALPHA = 0.026;
const float BASELINE_ALPHA = 0.08;
const float EDGE_ALPHA = 0.22;
const float HATCH_ALPHA = 0.05;
const float GLOW_ALPHA = 0.08;
const vec3 AREA = vec3(89.0, 167.0, 255.0) / 255.0;
const vec4 PEAK = vec4(242.0 / 255.0, 179.0 / 255.0, 61.0 / 255.0, 0.55);
const vec3 TRACE = vec3(214.0, 222.0, 232.0) / 255.0;

int lastColumn() { return textureSize(u_columns, 0).x - 1; }

vec2 columnAt(int x) {
  return texelFetch(u_columns, ivec2(clamp(x, 0, lastColumn()), 0), 0).rg;
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

// How much a line at distance d, blurred by sigma, covers a pixel: 1 on the line.
float blur(float d, float sigma) { return exp(-0.5 * d * d / (sigma * sigma)); }

// Distance from p to the nearest of the lines at first + k * spacing.
float lineDistance(float p, float first, float spacing) {
  return abs(p - first - spacing * floor((p - first) / spacing + 0.5));
}

vec3 plot(float x, float y, vec3 c) {
  if (onLine(x, u_grid.x, u_grid.y) || onLine(y, u_grid.z, u_grid.w)) c = mix(c, vec3(1.0), GRID_ALPHA);
  int column = int(x);
  // The trace ends flat at the span's edges: the columns past them hold no data.
  vec2 here = columnAt(column);
  vec2 left = float(column) - 0.5 < u_span.x ? here : columnAt(column - 1);
  vec2 right = float(column) + 1.5 >= u_span.y ? here : columnAt(column + 1);
  if (u_fill) c = mix(c, AREA, mix(0.30, 0.03, y / u_plotHeight) * clamp(y - here.x + 0.5, 0.0, 1.0));
  if (u_peakWidth > 0.0) c = mix(c, PEAK.rgb, PEAK.a * lineCoverage(y, left.y, here.y, right.y, u_peakWidth));
  c = mix(c, TRACE, lineCoverage(y, left.x, here.x, right.x, u_traceWidth));
  return mix(c, OUTSIDE, u_fade);
}

vec3 decor(float x, float y) {
  float light = exp(-min(abs(x - u_span.x), abs(x - u_span.y)) / (LIGHT_FALLOFF * u_scale));
  vec3 c = mix(DARK, BACKGROUND, light);
  vec4 band = texelFetch(u_glow, ivec2(clamp(int(x), 0, lastColumn()), 0), 0);
  c = mix(c, band.rgb, band.a * GLOW_ALPHA * y / u_plotHeight);
  float sigma = 1.6 * u_scale, soft = SOFT_GRID_ALPHA * (0.35 + 0.65 * light);
  if (u_grid.y > 0.0) c = mix(c, vec3(1.0), soft * blur(lineDistance(x, u_grid.x, u_grid.y), sigma));
  if (u_grid.w > 0.0) c = mix(c, vec3(1.0), soft * blur(lineDistance(y, u_grid.z, u_grid.w), sigma));
  if (x < u_reach.x || x >= u_reach.y) {
    float period = max(4.0, floor(8.0 * u_scale));
    if (mod(floor(x) + floor(y), period) < 1.0) c = mix(c, vec3(1.0), HATCH_ALPHA);
  }
  return c;
}

// A thin line at each edge of the span that lies inside the view.
vec3 edges(float x, vec3 c) {
  float width = float(lastColumn() + 1), extent = 0.5 * u_scale + 0.5, cover = 0.0;   // half the line, plus half a pixel
  if (u_span.x > 0.5) cover = clamp(extent - abs(x - u_span.x), 0.0, 1.0);
  if (u_span.y < width - 0.5) cover = max(cover, clamp(extent - abs(x - u_span.y), 0.0, 1.0));
  return mix(c, vec3(1.0), EDGE_ALPHA * cover);
}

void main() {
  float x = gl_FragCoord.x, y = u_height - gl_FragCoord.y;
  bool inside = x >= u_span.x && x < u_span.y;
  vec3 c = inside ? BACKGROUND : OUTSIDE;
  if (y >= u_plotHeight) {
    if (floor(y) == u_plotHeight) c = mix(c, vec3(1.0), BASELINE_ALPHA);
  } else {
    c = edges(x, inside ? plot(x, y, c) : decor(x, y));
  }
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
const PLOT_UNIFORMS = /** @type {const} */ (['u_columns', 'u_glow', 'u_height', 'u_plotHeight', 'u_traceWidth', 'u_peakWidth', 'u_fill',
  'u_grid', 'u_span', 'u_reach', 'u_scale', 'u_fade']);
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
 * @property {[number, number]} span   x of the span's edges; outside them, the decor
 * @property {[number, number]} reach  x of the edges of the receiver's reach; hatched beyond
 * @property {number} scale          px per CSS px
 * @property {number} fade           0 to 1: how far the span's data fades
 * @property {Uint8Array} glow       RGBA for each column: its band's colour, alpha 0 without one
 * @property {number} glowVersion    changes when glow does: the view uploads it only then
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
 * @property {WebGLTexture} glow
 */

export class SpectrumView {
  #canvas;
  #gl;
  #onRestore;
  #lost = false;
  /** @type {SpectrumResources | null} */
  #res = null;
  #glowVersion = NaN;             // the scene's glow in the texture: none yet

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
    if (this.#res) this.#allocate(this.#res);
  }

  /** @param {SpectrumScene} scene */
  draw(scene) {
    const gl = this.#gl, res = this.#res;
    if (this.#lost || !res) return;
    const { width, height } = this.#canvas;
    if (scene.columns.length < 2 * width) throw new RangeError('spectrum scene: too few columns');
    if (scene.glow.length < 4 * width) throw new RangeError('spectrum scene: too few band colours');
    gl.viewport(0, 0, width, height);
    gl.disable(gl.BLEND);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, res.columns);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, width, 1, gl.RG, gl.FLOAT, scene.columns);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, res.glow);
    if (scene.glowVersion !== this.#glowVersion) {
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, width, 1, gl.RGBA, gl.UNSIGNED_BYTE, scene.glow);
      this.#glowVersion = scene.glowVersion;
    }
    gl.useProgram(res.plot);
    const u = res.plotUniforms;
    gl.uniform1i(u.u_columns, 0);
    gl.uniform1i(u.u_glow, 1);
    gl.uniform1f(u.u_height, height);
    gl.uniform1f(u.u_plotHeight, scene.plotHeight);
    gl.uniform1f(u.u_traceWidth, scene.traceWidth);
    gl.uniform1f(u.u_peakWidth, scene.peakWidth);
    gl.uniform1i(u.u_fill, scene.fill ? 1 : 0);
    gl.uniform4fv(u.u_grid, scene.grid || [0, 0, 0, 0]);
    gl.uniform2f(u.u_span, scene.span[0], scene.span[1]);
    gl.uniform2f(u.u_reach, scene.reach[0], scene.reach[1]);
    gl.uniform1f(u.u_scale, scene.scale);
    gl.uniform1f(u.u_fade, scene.fade);
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
    const res = {
      plot, plotUniforms: uniformLocations(gl, plot, PLOT_UNIFORMS),
      marker, markerUniforms: uniformLocations(gl, marker, MARKER_UNIFORMS),
      coverArray, markerArray, markerBuffer, columns: createDataTexture(gl), glow: createDataTexture(gl),
    };
    this.#allocate(res);
    return res;
  }

  /**
   * Size the column textures to the drawing buffer; the band colours are uploaded again.
   * @param {SpectrumResources} res
   */
  #allocate(res) {
    const gl = this.#gl;
    if (this.#lost) return;
    gl.bindTexture(gl.TEXTURE_2D, res.columns);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, this.#canvas.width, 1, 0, gl.RG, gl.FLOAT, null);
    gl.bindTexture(gl.TEXTURE_2D, res.glow);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, this.#canvas.width, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    this.#glowVersion = NaN;
  }
}
