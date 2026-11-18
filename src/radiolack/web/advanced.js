/*
 * A SoapySDR device's own controls in the Receiver menu
 * (later-milestones.md, section 1, pull request 4): the antenna sockets,
 * when it has several, and the Advanced section, one slider per gain stage
 * under the device's name for it, then the device's settings as it
 * describes them, a box, a choice or a number, each with its description.
 * The engine reads back what the device holds after each change, which the
 * controls then show. Every text of the device's goes into the page as
 * text, never as markup.
 */

/** @typedef {import('./stream.js').SourceState} SourceState */
/** @typedef {import('./stream.js').DeviceValue} DeviceValue */

/**
 * A gain stage's slider: the gain held, as a position among those offered.
 * @typedef {object} StageControl
 * @property {string} name
 * @property {number[]} gainsDb
 * @property {number} index        the slider's position
 * @property {string} valueText    such as "16.0 dB"
 */

/**
 * One of the device's settings.
 * @typedef {object} SettingControl
 * @property {string} key
 * @property {string} name
 * @property {string} help              the device's description
 * @property {'bool' | 'choice' | 'number'} type
 * @property {{ value: string, name: string }[]} options    a choice's
 * @property {number} min               a number's; 0 otherwise
 * @property {number} max
 * @property {number} step              0: any value
 * @property {boolean} integer
 * @property {DeviceValue | null} value the device's, null while unknown
 */

/**
 * @typedef {object} AdvancedView
 * @property {boolean} antennaShown         the device has several sockets
 * @property {string[]} antennas
 * @property {string} antenna               the socket in use, '' while unknown
 * @property {boolean} shown                the Advanced section shows
 * @property {StageControl[]} stages       several stages: one slider each
 * @property {SettingControl[]} settings
 * @property {string} structure             what the controls are; rebuilt only when it changes
 */

/**
 * @param {number[]} values
 * @param {number} value
 */
export function nearestIndex(values, value) {
  let best = 0;
  values.forEach((candidate, index) => {
    if (Math.abs(candidate - value) < Math.abs(values[best] - value)) best = index;
  });
  return best;
}

/**
 * The device's controls for a source state: none for a source other than a
 * SoapySDR device, nor for a device without them.
 * @param {SourceState | null} source
 * @returns {AdvancedView}
 */
export function advancedView(source) {
  if (!source || source.kind !== 'soapy') {
    return { antennaShown: false, antennas: [], antenna: '', shown: false, stages: [], settings: [], structure: '' };
  }
  const stages = source.gainStages.map((stage) => {
    const gain = source.stageGainsDb[stage.name];
    const index = nearestIndex(stage.gainsDb, gain ?? stage.gainsDb[0]);
    return {
      name: stage.name, gainsDb: stage.gainsDb, index,
      valueText: gain === undefined ? '' : `${stage.gainsDb[index].toFixed(1)} dB`,
    };
  });
  const settings = source.deviceSettingInfo.map((setting) => ({
    key: setting.key, name: setting.name, help: setting.description, type: setting.type, options: setting.options,
    min: setting.range ? setting.range[0] : 0, max: setting.range ? setting.range[1] : 0,
    step: setting.range ? setting.range[2] : 0, integer: setting.integer,
    value: source.deviceSettings[setting.key] ?? null,
  }));
  const structure = JSON.stringify([stages.map((s) => [s.name, s.gainsDb]),
    settings.map((s) => [s.key, s.name, s.help, s.type, s.options, s.min, s.max, s.step, s.integer])]);
  return {
    antennaShown: source.antennas.length > 1,
    antennas: source.antennas.slice(),
    antenna: source.antenna ?? '',
    shown: stages.length > 0 || settings.length > 0,
    stages,
    settings,
    structure,
  };
}

/**
 * A number typed for a setting, within its range and on its steps, or null
 * for one that is not.
 * @param {string} text
 * @param {SettingControl} setting
 * @returns {number | null}
 */
export function settingNumber(text, setting) {
  const typed = text.trim() === '' ? NaN : Number(text);
  if (!Number.isFinite(typed) || typed < setting.min || typed > setting.max) return null;
  let value = setting.step > 0 ? setting.min + Math.round((typed - setting.min) / setting.step) * setting.step : typed;
  value = Math.min(value, setting.max);
  return setting.integer ? Math.round(value) : value;
}

/**
 * A setting's value as its control shows it.
 * @param {SettingControl} setting
 */
export function settingText(setting) {
  if (setting.value === null) return '';
  return String(setting.value);
}

/**
 * What a control asks of the engine.
 * @typedef {object} AdvancedHandlers
 * @property {(name: string, gainDb: number) => void} onStage
 * @property {(key: string, value: DeviceValue) => void} onSetting
 * @property {(control: HTMLElement) => boolean} typing   the user is typing in this field
 */

/**
 * @param {string} tag
 * @param {string} [className]
 */
function make(tag, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  return element;
}

/**
 * A note under a control with the device's description, as text.
 * @param {string} id
 * @param {string} help
 */
function note(id, help) {
  const element = make('p', 'note');
  element.id = id;
  element.textContent = help;
  return element;
}

/**
 * The controls of the Advanced section, made anew.
 * @param {AdvancedView} view
 * @param {AdvancedHandlers} handlers
 * @returns {HTMLElement[]}
 */
function build(view, handlers) {
  /** @type {HTMLElement[]} */
  const controls = [];
  view.stages.forEach((stage, index) => {
    const field = make('div', 'field');
    const label = make('label');
    const id = `src-stage-${index}`;
    label.setAttribute('for', id);
    label.append(document.createTextNode(`${stage.name} `), make('output'));
    const slider = /** @type {HTMLInputElement} */ (make('input'));
    slider.type = 'range';
    slider.id = id;
    slider.min = '0';
    slider.step = '1';
    slider.max = String(Math.max(0, stage.gainsDb.length - 1));
    slider.dataset.stage = String(index);
    slider.addEventListener('input', () => {
      const output = label.querySelector('output');
      if (output) output.textContent = `${stage.gainsDb[Number(slider.value)].toFixed(1)} dB`;
    });
    slider.addEventListener('change', () => handlers.onStage(stage.name, stage.gainsDb[Number(slider.value)]));
    field.append(label, slider);
    controls.push(field);
  });
  view.settings.forEach((setting, index) => {
    const id = `src-setting-${index}`;
    const noteId = `${id}-note`;
    /** @type {HTMLElement} */
    let field;
    if (setting.type === 'bool') {
      field = make('div');
      const label = make('label', 'check');
      const box = /** @type {HTMLInputElement} */ (make('input'));
      box.type = 'checkbox';
      box.id = id;
      box.dataset.setting = String(index);
      box.addEventListener('change', () => handlers.onSetting(setting.key, box.checked));
      label.append(box, document.createTextNode(` ${setting.name}`));
      field.append(label);
    } else {
      field = make('div', 'field');
      const label = make('label');
      label.setAttribute('for', id);
      label.textContent = setting.name;
      /** @type {HTMLInputElement | HTMLSelectElement} */
      let control;
      if (setting.type === 'choice') {
        const select = /** @type {HTMLSelectElement} */ (make('select'));
        select.append(...setting.options.map((option) => new Option(option.name, option.value)));
        select.addEventListener('change', () => handlers.onSetting(setting.key, select.value));
        control = select;
      } else {
        const input = /** @type {HTMLInputElement} */ (make('input'));
        input.type = 'number';
        input.min = String(setting.min);
        input.max = String(setting.max);
        input.step = setting.step > 0 ? String(setting.step) : 'any';
        input.addEventListener('change', () => {
          const value = settingNumber(input.value, setting);
          if (value !== null) handlers.onSetting(setting.key, value);
          else input.value = settingText(setting);
        });
        control = input;
      }
      control.id = id;
      control.dataset.setting = String(index);
      field.append(label, control);
    }
    const input = field.querySelector('input, select');
    if (setting.help && input) {
      input.setAttribute('aria-describedby', noteId);
      field.append(note(noteId, setting.help));
    }
    controls.push(field);
  });
  return controls;
}

/**
 * Show ``view`` in ``container``, the Advanced section's controls: made
 * anew only when the device's controls change, their values written only
 * where they differ, a field the user types in left alone.
 * @param {HTMLElement} container
 * @param {AdvancedView} view
 * @param {boolean} enabled   the controls may change: the stream is open and the engine lists them as settable
 * @param {{ stages: boolean, settings: boolean }} settable
 * @param {AdvancedHandlers} handlers
 */
export function renderAdvanced(container, view, enabled, settable, handlers) {
  if (container.dataset.structure !== view.structure) {
    container.replaceChildren(...build(view, handlers));
    container.dataset.structure = view.structure;
  }
  view.stages.forEach((stage, index) => {
    const slider = /** @type {HTMLInputElement | null} */ (container.querySelector(`[data-stage="${index}"]`));
    if (!slider) return;
    const position = String(stage.index);
    if (slider.value !== position && document.activeElement !== slider) slider.value = position;
    const disabled = !(enabled && settable.stages);
    if (slider.disabled !== disabled) slider.disabled = disabled;
    const output = slider.parentElement?.querySelector('output');
    if (output && output.textContent !== stage.valueText && document.activeElement !== slider) {
      output.textContent = stage.valueText;
    }
  });
  view.settings.forEach((setting, index) => {
    const control = /** @type {HTMLInputElement | HTMLSelectElement | null} */ (
      container.querySelector(`[data-setting="${index}"]`));
    if (!control) return;
    const disabled = !(enabled && settable.settings) || setting.value === null;
    if (control.disabled !== disabled) control.disabled = disabled;
    if (control instanceof HTMLInputElement && control.type === 'checkbox') {
      const checked = setting.value === true;
      if (control.checked !== checked) control.checked = checked;
    } else if (!handlers.typing(control)) {
      const text = settingText(setting);
      if (control.value !== text) control.value = text;
    }
  });
}
