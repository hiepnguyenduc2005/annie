// Offline regression checks against the real TypeScript methods. Constructors
// that connect to services are never run; all transports, clocks and DOM nodes
// below are in-memory fakes.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const appText = readFileSync(new URL('../src/ui/app.ts', import.meta.url), 'utf8');
const source = ts.createSourceFile('app.ts', appText, ts.ScriptTarget.Latest, true);
const appClass = source.statements.find(n => ts.isClassDeclaration(n) && n.name?.text === 'App');
const constructor = appClass.members.find(ts.isConstructorDeclaration);
const lifecycleStatements = constructor.body.statements.filter(n => {
  const text = n.getText(source);
  return /^(document|window)\.addEventListener\('(visibilitychange|blur|pagehide|beforeunload)'/.test(text);
}).map(n => n.getText(source)).join('\n');

function compile(text, globals = {}) {
  const output = ts.transpileModule(text, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const context = { exports: {}, ...globals };
  vm.runInNewContext(output, context, { timeout: 1000 });
  return context.exports;
}

class FakeElement {
  constructor() {
    this.handlers = new Map(); this.children = []; this.style = {};
    this.classList = { toggle() {} };
  }
  addEventListener(name, callback) {
    const callbacks = this.handlers.get(name) || [];
    callbacks.push(callback); this.handlers.set(name, callbacks);
  }
  fire(name, event = {}) { for (const fn of this.handlers.get(name) || []) fn(event); }
  appendChild(child) { this.children.push(child); }
  getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 200 }; }
}

function fixture(family = 'Go2') {
  const events = []; const timers = new Map(); const callbacks = []; let nextTimer = 0;
  const document = new FakeElement(); document.visibilityState = 'visible';
  let focused = true; document.hasFocus = () => focused;
  document.createElement = () => new FakeElement();
  const window = new FakeElement();
  const cloudApi = { connectFamily: family, ensureFreshToken: async () => true };
  const globals = {
    document, window, cloudApi,
    isG1Family: f => f === 'G1' || f === 'R1',
    RTC_TOPIC: { WIRELESS_CONTROLLER: 'rt/wirelesscontroller', SPORT_MOD: 'rt/api/sport/request' },
    SPORT_CMD: { StopMove: 1003, Damp: 1001 },
    setInterval: fn => { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearInterval: id => { events.push(['cancel', id]); timers.delete(id); },
    btBackend: () => ({ subscribe: (name, fn) => {
      callbacks.push(fn); return () => events.push(['unsubscribe']);
    } }),
  };
  const { App } = compile(appClass.getText(source), globals);
  const app = Object.create(App.prototype);
  Object.assign(app, {
    currentScreen: 'control', joystickTimer: null, motionInputGeneration: 0,
    joystickReleaseTicks: 0, joystickState: { lx: 0, ly: 0, rx: 0, ry: 0 },
    activeSourceId: null, relayUnsub: null, emergencyStopped: false,
    settingsState: {}, settingsPage: null, settingsDrawer: null,
    leftJoystickWrap: new FakeElement(), rightJoystickWrap: new FakeElement(),
    refreshInputSources() { events.push(['refresh']); },
    notifyEstopBlocked() { events.push(['blocked']); },
    gamepadManager: { currentState: { lx: 0, ly: .1, rx: 0, ry: 0, keys: 0 },
      destroy() { events.push(['gamepad-destroy']); } },
    dataHandler: {
      publish: (topic, data) => events.push(['publish', topic, { ...data }]),
      publishRequest: (topic, api, parameter, options) => events.push(['request', api, options]),
      destroy: () => events.push(['destroy']),
    },
    publishRequestLogged(topic, api, parameter, options) {
      return this.dataHandler.publishRequest(topic, api, parameter, options);
    },
  });
  const lifecycle = compile(`export function install() { let lastHiddenAt = 0; ${lifecycleStatements} }`, globals);
  lifecycle.install.call(app);
  return { app, events, timers, callbacks, document, window, globals,
    focus: value => { focused = value; },
    tick: () => { for (const fn of [...timers.values()]) fn(); },
  };
}

const publishes = f => f.events.filter(e => e[0] === 'publish');
const requests = f => f.events.filter(e => e[0] === 'request');
const neutral = data => Object.values(data).every(value => value === 0);

for (const event of ['blur', 'pagehide', 'beforeunload', 'hidden']) {
  test(`${event} cancels input before neutral and priority StopMove; queued input cannot resume`, () => {
    const f = fixture(); f.app.joystickState.ly = .1; f.app.startJoystickLoop();
    f.tick(); assert.equal(publishes(f).at(-1)[2].ly, .1);
    const queued = [...f.timers.values()][0]; f.events.length = 0;
    if (event === 'hidden') { f.document.visibilityState = 'hidden'; f.document.fire('visibilitychange'); }
    else { f.focus(false); f.window.fire(event); }
    assert.equal(f.events[0][0], 'cancel');
    assert.ok(neutral(publishes(f).at(-1)[2]));
    assert.equal(requests(f).at(-1)[1], 1003);
    assert.equal(requests(f).at(-1)[2].priority, true);
    const count = publishes(f).length; queued(); f.tick();
    f.focus(true); f.document.visibilityState = 'visible'; f.window.fire('focus');
    f.document.fire('visibilitychange'); f.tick();
    assert.equal(publishes(f).length, count);
    assert.equal(f.timers.size, 0);
    assert.equal(f.app.activeSourceId, null);
  });
}

test('fresh manual input can start after stop, with exactly one producer', () => {
  const f = fixture(); f.app.startJoystickLoop(); f.app.stopJoystickLoop();
  f.app.joystickState.ly = .1; f.app.startJoystickLoop(); f.app.startJoystickLoop();
  assert.equal(f.timers.size, 1); f.tick(); assert.equal(publishes(f).at(-1)[2].ly, .1);
});

test('BLE stick release reaches transport and a queued old source is invalid after stop', () => {
  const f = fixture(); f.app.setActiveInputSource('bt:test');
  const relay = f.callbacks.at(-1);
  relay({ lx: 0, ly: .1, rx: 0, ry: 0, buttons: {} });
  assert.equal(publishes(f).at(-1)[2].ly, .1);
  relay({ lx: 0, ly: 0, rx: 0, ry: 0, buttons: {} });
  assert.ok(neutral(publishes(f).at(-1)[2]));
  f.app.stopJoystickLoop(); const count = publishes(f).length;
  relay({ lx: 0, ly: .1, rx: 0, ry: 0, buttons: {} });
  assert.equal(publishes(f).length, count);
  assert.ok(f.events.some(e => e[0] === 'unsubscribe'));
});

test('held gamepad is deselected on blur and does not auto-resume on focus', () => {
  const f = fixture(); f.app.setActiveInputSource('gamepad:0'); f.tick();
  assert.equal(publishes(f).at(-1)[2].ly, .1);
  f.focus(false); f.window.fire('blur'); const count = publishes(f).length;
  f.focus(true); f.window.fire('focus'); f.tick();
  assert.equal(publishes(f).length, count); assert.equal(f.app.activeSourceId, null);
});

test('transport failure does not prevent producer cancellation or priority stop attempt', () => {
  const f = fixture(); f.app.startJoystickLoop();
  f.app.dataHandler.publish = () => { throw Error('closed'); };
  assert.doesNotThrow(() => f.app.stopJoystickLoop());
  assert.equal(f.timers.size, 0); assert.equal(requests(f).at(-1)[1], 1003);
  f.app.dataHandler.publishRequest = () => { throw Error('closed'); };
  assert.doesNotThrow(() => f.app.stopJoystickLoop());
});

test('red emergency stop still sends priority Damp and release never restarts motion', () => {
  const f = fixture(); f.app.startJoystickLoop(); f.app.sendStop(true);
  assert.equal(requests(f).at(-1)[1], 1001); assert.equal(requests(f).at(-1)[2].priority, true);
  assert.equal(f.app.emergencyStopped, true); assert.equal(f.timers.size, 0);
  f.app.joystickState.ly = .1; f.app.startJoystickLoop(); assert.equal(f.timers.size, 0);
  f.app.sendStop(false); assert.equal(f.timers.size, 0);
});

test('G1 lifecycle stop never receives Go2 StopMove API', () => {
  const f = fixture('G1'); f.app.stopJoystickLoop();
  assert.equal(requests(f).length, 0); assert.ok(neutral(publishes(f).at(-1)[2]));
});

test('disconnect neutralizes and requests StopMove before destroying transport', () => {
  const f = fixture();
  Object.assign(f.app, {
    webrtc: { close: () => f.events.push(['close']) },
    stopBgNoise() {}, stopTeachHeartbeat() {}, clearEstopToast() {},
    showLandingScreen() { f.events.push(['landing']); },
    audioPending: new Map(), armPending: new Map(), mcfSeedPending: new Map(),
    errorStore: { clear() {} },
  });
  f.app.joystickState.ly = .1; f.app.startJoystickLoop();
  const queued = [...f.timers.values()][0];
  f.app.disconnect();
  const stop = f.events.findIndex(e => e[0] === 'request' && e[1] === 1003);
  const destroy = f.events.findIndex(e => e[0] === 'destroy');
  const close = f.events.findIndex(e => e[0] === 'close');
  assert.ok(stop >= 0 && stop < destroy && destroy < close);
  assert.ok(neutral(publishes(f).at(-1)[2]));
  const count = publishes(f).length; queued();
  assert.equal(publishes(f).length, count);
  assert.equal(f.app.dataHandler, null); assert.equal(f.app.webrtc, null);
});

test('stopping the App releases its pointer so a later mousemove cannot restart driving', () => {
  const f = fixture();
  const { Joystick } = compile(readFileSync(new URL('../src/ui/components/joystick.ts', import.meta.url), 'utf8'), f.globals);
  const parent = new FakeElement();
  f.app.leftJoystick = new Joystick(parent, value => {
    f.app.joystickState.lx = value.x; f.app.joystickState.ly = value.y;
    if (value.x || value.y) f.app.startJoystickLoop();
  });
  parent.children[0].fire('mousedown', { preventDefault() {}, clientX: 100, clientY: 50 });
  f.tick(); assert.ok(publishes(f).at(-1)[2].ly > 0);
  f.app.stopJoystickLoop(); const count = publishes(f).length;
  f.window.fire('mousemove', { clientX: 100, clientY: 0 }); f.tick();
  assert.equal(publishes(f).length, count); assert.equal(f.timers.size, 0);
});

for (const event of ['blur', 'hidden', 'disabled', 'pagehide']) {
  test(`onscreen held pointer resets on ${event}; stale mousemove cannot re-arm`, () => {
    const f = fixture(); const values = [];
    const { Joystick } = compile(readFileSync(new URL('../src/ui/components/joystick.ts', import.meta.url), 'utf8'), f.globals);
    const parent = new FakeElement(); const joystick = new Joystick(parent, value => values.push(value));
    parent.children[0].fire('mousedown', { preventDefault() {}, clientX: 100, clientY: 50 });
    assert.ok(values.at(-1).y > 0);
    if (event === 'hidden') { f.document.visibilityState = 'hidden'; f.document.fire('visibilitychange'); }
    else if (event === 'disabled') joystick.setDisabled(true);
    else f.window.fire(event);
    assert.equal(values.at(-1).x, 0); assert.equal(values.at(-1).y, 0);
    const count = values.length; f.window.fire('mousemove', { clientX: 100, clientY: 0 });
    assert.equal(values.length, count);
  });
}
