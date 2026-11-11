/*
 * Pairing page: sends the access code entered to the server, which pairs
 * this device, gives it its session cookie and then serves it the interface
 * at the same address; says why the server refused the code.
 */

const form = /** @type {HTMLFormElement} */ (document.getElementById('pair-form'));
const codeEl = /** @type {HTMLInputElement} */ (document.getElementById('pair-code'));
const submitEl = /** @type {HTMLButtonElement} */ (document.getElementById('pair-submit'));
const messageEl = /** @type {HTMLElement} */ (document.getElementById('pair-msg'));

/**
 * Why the server refused the code: its sentence, else its status.
 * @param {Response} response
 * @returns {Promise<string>}
 */
async function refusal(response) {
  const text = (await response.text()).trim();
  try {
    const body = JSON.parse(text);
    if (body !== null && typeof body === 'object' && typeof body.error === 'string') return body.error;
  } catch {
    // A sentence in plain text, such as when remote access is off.
  }
  return text || `The code was refused (HTTP ${response.status}).`;
}

/**
 * Send the code; the interface loads once the device is paired.
 * @param {string} code
 */
async function pair(code) {
  submitEl.disabled = true;
  messageEl.textContent = 'Checking the code…';
  try {
    const response = await fetch('pair', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
      credentials: 'same-origin',
      cache: 'no-store',
    });
    if (response.ok) {
      messageEl.textContent = 'Paired: loading Radiolack…';
      location.reload();
      return;
    }
    messageEl.textContent = await refusal(response);
  } catch {
    messageEl.textContent = 'Radiolack does not answer: check that the computer is on this network '
      + 'and that remote access is on in its settings.';
  }
  submitEl.disabled = false;
  codeEl.focus();
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const code = codeEl.value.trim();
  if (!code) {
    messageEl.textContent = 'Enter the access code.';
    codeEl.focus();
    return;
  }
  pair(code);
});
