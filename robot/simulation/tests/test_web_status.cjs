// Run with: node --test robot/simulation/tests/test_web_status.cjs
// Execute the real browser functions with an offline DOM and transport.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing source boundary: ${start}`);
  return source.slice(first, last);
}
const code = [
  section('function syncGoalState(', 'async function loadCatalog('),
  section('function renderScene(', 'function adjacentScene('),
  section('let storySubmitting =', "$('intelligence-form').addEventListener"),
  section('async function pollBrain(', 'function wireSayClip('),
  section('async function pollDemo(', "$('start-house-demo').addEventListener"),
].join('\n');

function fixture(data = {}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', value: '', hidden: false, disabled: false, dataset: {},
      replaceChildren(...children) { this.children = children; },
      addEventListener() {},
      append() {},
      querySelector(selector) { return element(`${id} ${selector}`); },
    });
    return elements.get(id);
  };
  const state = { map_id: 'map-a', intelligence_revision: 3,
    intelligence_goal: 'Find the phone', intelligence_enabled: true };
  const context = vm.createContext({
    $: element, current: state, intelligenceContext: null,
    catalog: { scenes: [{ id: 'house', title: 'The house', description: '', ground_truth: {} }] },
    activeSceneId: null, demoStarting: false, demoStagesSignature: null, previousDemoStatus: null,
    document: { activeElement: null, querySelectorAll: () => [], createElement: () => ({ append() {} }) },
    fetch: async () => ({ ok: true, json: async () => data }),
    pollState: async () => {},
    renderExploration() {}, renderPersonSafety() {},
    notice(text) { element('notice').textContent = text; },
    setPanelError(id, message) { element(id).textContent = message || ''; element(id).hidden = !message; },
  });
  vm.runInContext(code, context);
  return { context, element, state };
}

test('new revision immediately clears the previous completion and reason', () => {
  const { context, element, state } = fixture();
  context.syncGoalState({ ...state, intelligence_revision: 2 });
  element('goal-completion').hidden = false;
  element('goal-completion').textContent = 'Delivered Zach’s message';
  element('agent-reason').textContent = 'Previous goal is done';
  context.syncGoalState(state);
  assert.equal(element('goal-completion').hidden, true);
  assert.equal(element('goal-completion').textContent, '');
  assert.equal(element('agent-reason').textContent, '');
  assert.equal(element('autonomy-status').textContent, 'Waiting for the current goal.');
});

test('map changes invalidate completion even when the revision is reused', () => {
  const { context, element, state } = fixture();
  context.syncGoalState(state);
  element('goal-completion').hidden = false;
  context.syncGoalState({ ...state, map_id: 'map-b' });
  assert.equal(element('goal-completion').hidden, true);
});

test('unchanged state preserves completion and a focused draft is never overwritten', () => {
  const { context, element, state } = fixture();
  context.syncGoalState(state);
  element('goal-completion').hidden = false;
  context.syncGoalState(state);
  assert.equal(element('goal-completion').hidden, false);
  context.document.activeElement = element('intelligence-goal');
  element('intelligence-goal').value = 'Unsubmitted draft';
  context.syncGoalState({ ...state, intelligence_revision: 4, intelligence_goal: 'Other goal' });
  assert.equal(element('intelligence-goal').value, 'Unsubmitted draft');
});

test('old goal response cannot restore an old completion or reason', async () => {
  const { context, element } = fixture({
    context_map_id: 'map-a', agent: { goal: 'Deliver Zach’s message', action: { reason: 'Delivered' } },
    goal_completion: { map_id: 'map-a', revision: 2, goal: 'Deliver Zach’s message', result: 'Done' },
  });
  await context.pollBrain();
  assert.equal(element('goal-completion').hidden, true);
  assert.equal(element('agent-reason').textContent, '');
  assert.equal(element('autonomy-status').textContent, 'Waiting for the current goal.');
});

test('a blocked planner is never labelled ready', async () => {
  const reason = 'The cloud inference allowance cannot cover another call.';
  const { context, element } = fixture({
    context_map_id: 'map-a', inference_blocked: reason, last_error: reason,
    agent: { goal: 'Find the phone', thinking: false, execution: reason },
  });
  await context.pollBrain();
  assert.equal(element('autonomy-status').textContent, 'Planning paused · configuration needs attention');
  assert.equal(element('brain-error').textContent, reason);
});

test('a current planner error does not advertise readiness', async () => {
  const { context, element } = fixture({
    context_map_id: 'map-a', last_error: 'agent unavailable (HTTPStatusError HTTP 503)',
    agent: { goal: 'Find the phone', thinking: false },
  });
  await context.pollBrain();
  assert.equal(element('autonomy-status').textContent, 'Planner unavailable');
});

test('matching current completion is displayed; prior-map failures are not', async () => {
  const { context, element, state } = fixture({
    context_map_id: 'map-a', agent: { goal: 'Find the phone', model: 'test-model' },
    goal_completion: { map_id: 'map-a', revision: 3, goal: 'Find the phone', result: 'Spoken answer completed' },
  });
  await context.pollBrain();
  assert.equal(element('goal-completion').hidden, false);
  assert.match(element('goal-completion').textContent, /Spoken answer completed/);
  context.current = { ...state, map_id: 'map-b' };
  context.fetch = async () => ({ ok: true, json: async () => ({
    context_map_id: 'map-a', inference_limit_reached: true, last_error: 'Previous run failed',
  }) });
  await context.pollBrain();
  assert.equal(element('brain-mode').textContent, 'WAITING FOR SCENE');
  assert.equal(element('brain-error').hidden, true);
});

test('scene-loaded notice never claims a playback state that can become stale', () => {
  const { context, element, state } = fixture();
  context.current = { ...state, scene_id: 'house', running: false };
  context.renderScene();
  assert.equal(element('notice').textContent, 'Loaded: The house.');
  context.current.running = true;
  context.renderScene();
  assert.equal(element('notice').textContent, 'Loaded: The house.');
});

test('old check-in failure remains visible and is labelled as a previous staged run', async () => {
  const { context, element } = fixture({ status: 'failed', error: 'Deadline exceeded', stages: [] });
  await context.pollDemo();
  assert.equal(element('demo-evidence-panel h2').textContent, 'Previous staged check-in run');
  assert.equal(element('demo-run-status').textContent, 'Previous check-in incomplete');
  assert.equal(element('demo-error').hidden, false);
  assert.equal(element('demo-error').textContent, 'Deadline exceeded');
});

test('both story requests require completed speech while keeping their respective endpoints', async () => {
  const { context, element } = fixture();
  const requests = [];
  context.postJSON = async (url, payload) => { requests.push({ url, payload }); };
  element('zach-message').value = 'Please find Janine and remind her to charge her phone.';
  element('janine-reply').value = 'I lost my phone.';
  await context.submitStoryMessage('zach');
  await context.submitStoryMessage('janine');
  assert.deepEqual(requests.map(request => request.url), ['/agent/start', '/control']);
  for (const { payload } of requests) {
    assert.equal(payload.action, 'intelligence');
    assert.equal(payload.enabled, true);
    assert.equal(payload.require_speech, true);
    assert.ok(payload.goal.length <= 500);
  }
});
