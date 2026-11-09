/*
 * WebGL2 helpers shared by the spectrum and waterfall views.
 */

/** No WebGL2 context: the spectrum and the waterfall cannot be drawn. */
export class WebGLUnavailable extends Error {}

/**
 * Vertex shader of one triangle that covers the viewport. Draw it with
 * `drawArrays(TRIANGLES, 0, 3)`; it needs no vertex buffer.
 */
export const COVER_VERTEX_SHADER = `#version 300 es
void main() {
  vec2 corner = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(corner * 2.0 - 1.0, 0.0, 1.0);
}`;

/**
 * A WebGL2 context for a view that redraws its whole canvas each time.
 * @param {HTMLCanvasElement} canvas
 * @returns {WebGL2RenderingContext}
 */
export function getContext(canvas) {
  const gl = canvas.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false });
  if (!gl) throw new WebGLUnavailable('WebGL2 is not available');
  return gl;
}

/**
 * Compile and link a program.
 * @param {WebGL2RenderingContext} gl
 * @param {string} vertexSource
 * @param {string} fragmentSource
 * @returns {WebGLProgram}
 */
export function createProgram(gl, vertexSource, fragmentSource) {
  const program = gl.createProgram();
  if (!program) throw new Error('cannot create a WebGL program');
  /** @type {[number, string][]} */
  const stages = [[gl.VERTEX_SHADER, vertexSource], [gl.FRAGMENT_SHADER, fragmentSource]];
  for (const [type, source] of stages) {
    const shader = gl.createShader(type);
    if (!shader) throw new Error('cannot create a WebGL shader');
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(`WebGL shader: ${gl.getShaderInfoLog(shader)}`);
    }
    gl.attachShader(program, shader);
    gl.deleteShader(shader);        // freed with the program
  }
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(`WebGL program: ${gl.getProgramInfoLog(program)}`);
  }
  return program;
}

/**
 * Look up uniform locations by name.
 * @template {string} Name
 * @param {WebGL2RenderingContext} gl
 * @param {WebGLProgram} program
 * @param {readonly Name[]} names
 * @returns {Record<Name, WebGLUniformLocation | null>}
 */
export function uniformLocations(gl, program, names) {
  const out = /** @type {Record<Name, WebGLUniformLocation | null>} */ ({});
  for (const name of names) out[name] = gl.getUniformLocation(program, name);
  return out;
}

/**
 * A 2D texture read with texelFetch only: no filtering, no mipmaps.
 * @param {WebGL2RenderingContext} gl
 * @returns {WebGLTexture}
 */
export function createDataTexture(gl) {
  const texture = gl.createTexture();
  if (!texture) throw new Error('cannot create a WebGL texture');
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return texture;
}

/**
 * Follow the loss and return of a context. While it is lost the view draws
 * nothing; when it is back, `rebuild` makes the GPU resources again.
 * @param {HTMLCanvasElement} canvas
 * @param {WebGL2RenderingContext} gl
 * @param {(lost: boolean) => void} setLost
 * @param {() => void} rebuild
 */
export function watchContext(canvas, gl, setLost, rebuild) {
  canvas.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();         // asks the browser to restore the context
    setLost(true);
  });
  canvas.addEventListener('webglcontextrestored', () => {
    setLost(false);
    try {
      rebuild();
    } catch (error) {
      if (!gl.isContextLost()) throw error;     // lost again: the next restore rebuilds
    }
  });
}
