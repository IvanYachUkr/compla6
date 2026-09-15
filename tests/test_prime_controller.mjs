import test from 'node:test';
import assert from 'node:assert/strict';
import { appendFileSync, readFileSync, truncateSync } from 'node:fs';
import { PrimeControllerAdapter, sourcePreflight } from '../tools/prime_controller.mjs';
import { fixture, eventually, ROOT_MODEL, CHILD_MODEL } from './prime_fixture.mjs';

test('installed native runtime is available without a universal provider token ceiling', () => {
  assert.equal(sourcePreflight().version, '0.9.3');
  assert.equal(sourcePreflight().production_supported, true);
});

test('attempt-only protocol has no implicit deadline and native feedback exhausts its own limit',
  { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter, { protocol: {
    schema_version: 2, deadline_epoch: null, max_feedback_attempts: null, max_failure_continuations: 1,
  } });
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'initial research' });
  await f.adapter.sync();
  assert.equal((await f.rpc('state')).lifecycle, 'open');
  assert.equal((await f.rpc('pending_deliveries')).length, 1);
  await f.adapter.deliverPending();
  await parent.waitForHeadlessIdle(); await f.adapter.sync();
  await eventually(async () => (await f.rpc('state')).lifecycle === 'closed');
  const state = await f.rpc('state');
  assert.equal(state.failure_continuations, 1);
  assert.equal(state.completion.stop_reason, 'attempt_limit');
  assert.equal(f.providerCalls.length, 2);
});

test('native root and child retain tools and report their own cached usage once', { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'root task', name: 'controller' });
  assert.ok(parent.getActiveToolNames().includes('ipython'));
  assert.equal(parent.rlmMaxDepth, 1);
  const handle = await parent.runRlmChild('child task', { name: 'worker', model: CHILD_MODEL, thinking: 'high' });
  await parent.waitForRlmQuiescence(); await f.adapter.sync();
  const childRecord = Object.values(f.adapter.journal.data.nodes).find(node => node.parent_id === 'root');
  const child = f.adapter.sessions.get(childRecord.node_id);
  assert.equal(child.sessionId, childRecord.native_session_id);
  assert.equal(childRecord.native_child_id, handle.rlm_child_id);
  assert.equal(child.thinkingLevel, 'high');
  const state = await f.rpc('state');
  for (const node of Object.values(state.nodes)) {
    const n = f.providerCalls.filter(call => call.model === node.model && !call.aborted).length;
    assert.equal(node.usage.input_tokens, n * 12);
    assert.equal(node.usage.output_tokens, n * 5);
  }
  const usage = (await f.rpc('brief')).usage;
  assert.equal(usage.total_tokens, f.providerCalls.length * 17);
  await f.adapter.sync();
  assert.deepEqual((await f.rpc('brief')).usage, usage);
  assert.equal(state.nodes[childRecord.node_id].status, 'completed');
});

test('native admission rejects wrong model, wrong effort, excess and nested children before dispatch',
  { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'parent' });
  for (const [name, model, thinking] of [
    ['bad-model', 'compression-fixture/forbidden', 'high'], ['bad-effort', CHILD_MODEL, 'low'],
  ]) {
    const handle = await parent.runRlmChild('denied', { name, model, thinking });
    assert.ok(handle.rlm_child_id); // Native handle precedes detached runtime startup.
    await parent.waitForRlmQuiescence(); await f.adapter.sync();
  }
  assert.equal(f.providerCalls.some(call => call.model !== ROOT_MODEL), false);
  const first = await parent.runRlmChild('allowed', { name: 'worker', model: CHILD_MODEL, thinking: 'high' });
  await parent.waitForRlmQuiescence(); await f.adapter.sync();
  const child = parent.getRlmChildSession(first.rlm_child_id);
  const n = f.providerCalls.filter(call => call.model === CHILD_MODEL).length;
  await parent.runRlmChild('excess', { name: 'second', model: CHILD_MODEL, thinking: 'high' });
  await parent.waitForRlmQuiescence(); await f.adapter.sync();
  assert.equal(f.providerCalls.filter(call => call.model === CHILD_MODEL).length, n);
  await assert.rejects(child.runRlmChild('nested', { name: 'nested', model: CHILD_MODEL, thinking: 'high' }), /depth limit/);
  assert.equal(Object.keys((await f.rpc('state')).nodes).length, 2);
});

test('restoration preserves native IDs and child effort and observes a retained follow-up turn',
  { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'parent', name: 'controller' });
  const handle = await parent.runRlmChild('first task', { name: 'worker', model: CHILD_MODEL, thinking: 'high' });
  await parent.waitForRlmQuiescence(); await f.adapter.sync();
  const saved = Object.values(f.adapter.journal.data.nodes);
  const before = f.providerCalls.length; await f.adapter.close();
  const restored = await f.open(); await restored.restore(); await restored.sync();
  assert.equal(f.providerCalls.length, before);
  for (const node of saved) {
    assert.equal(restored.sessions.get(node.node_id).sessionId, node.native_session_id);
    assert.equal(restored.sessions.get(node.node_id).thinkingLevel, node.effort);
  }
  const childNode = saved.find(node => node.parent_id === 'root');
  const child = restored.sessions.get(childNode.node_id);
  assert.equal(restored.sessions.get('root').getRlmChildSession(handle.rlm_child_id), child);
  const gate = f.control.holdNext();
  const receipt = await restored.messageController('root').sendAgentMessage({ target: 'worker', message: 'second task' });
  await gate.entered; await restored.sync();
  assert.equal((await f.rpc('state')).nodes[childNode.node_id].status, 'running');
  assert.equal(receipt.target.sessionId, childNode.native_session_id);
  gate.release(); await child.waitForHeadlessIdle(); await restored.sync();
  assert.equal((await f.rpc('state')).nodes[childNode.node_id].status, 'completed');
  assert.equal(readFileSync(restored.journal.path, 'utf8').includes('second task'), false);
});

test('saved effort drift is rejected before a provider call', { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium' });
  parent.sessionManager.appendThinkingLevelChange('low'); parent.sessionManager.flushNow();
  await f.adapter.close(); const restored = await f.open();
  await assert.rejects(restored.restore(), /restored_effort_mismatch/);
  assert.equal(f.providerCalls.length, 0);
});

test('ambiguous child launch is not recreated after recovery', { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'parent' });
  const runtime = f.adapter.runtimes.get('root');
  const nativeFactory = runtime.createRlmSubagentRuntime.bind(runtime);
  runtime.createRlmSubagentRuntime = async () => { throw new Error('startup unavailable'); };
  await parent.runRlmChild('one task', { name: 'worker', model: CHILD_MODEL, thinking: 'high' });
  await parent.waitForRlmQuiescence(); await f.adapter.sync();
  runtime.createRlmSubagentRuntime = nativeFactory; await f.adapter.close();
  const restored = await f.open(); await restored.restore();
  await restored.sessions.get('root').runRlmChild('one task', { name: 'worker', model: CHILD_MODEL, thinking: 'high' });
  await restored.sessions.get('root').waitForRlmQuiescence(); await restored.sync();
  assert.equal(f.providerCalls.some(call => call.model === CHILD_MODEL), false);
  assert.equal(Object.keys((await f.rpc('state')).nodes).length, 2);
});

test('queued feedback waits for persistence and acceptance-before-ACK recovers without resend',
  { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'parent' });
  await f.adapter.sync();
  const pending = (await f.rpc('pending_deliveries'))[0]; assert.ok(pending);
  const gate = f.control.holdNext();
  const busy = parent.prompt('busy turn'); await gate.entered;
  const deliver = f.adapter.deliverPending(); deliver.catch(() => {});
  await eventually(() => Boolean(f.adapter.journal.data.deliveries[pending.id]));
  assert.equal((await f.rpc('unacknowledged_deliveries')).length, 1);
  const nativeId = f.adapter.journal.data.deliveries[pending.id].native_id;
  assert.equal(f.adapter.nativeProof('root', nativeId), false);
  const realRpc = f.adapter.rpc;
  f.adapter.rpc = async (operation, args) => {
    if (operation === 'ack_delivery') throw new Error('ack transport unavailable');
    return realRpc(operation, args);
  };
  gate.release(); await busy; await assert.rejects(deliver, /ack transport unavailable/);
  await parent.waitForHeadlessIdle(); await f.adapter.sync();
  assert.equal(f.adapter.nativeProof('root', nativeId), true);
  const file = parent.sessionFile, bytes = readFileSync(file).length;
  appendFileSync(file, '{"type":"message","incomplete":');
  assert.equal(f.adapter.nativeProof('root', nativeId), true);
  truncateSync(file, bytes);
  const before = f.providerCalls.length; await f.adapter.close();
  const restored = await f.open(); await restored.restore();
  assert.equal((await f.rpc('unacknowledged_deliveries')).length, 0);
  assert.equal(f.providerCalls.length, before);
  assert.equal(restored.sessions.get('root').sessionManager.getEntries()
    .filter(entry => entry.type === 'custom_message' && entry.details?.id === nativeId).length, 1);
});

test('deadline aborts an in-flight call and drains without requiring a usage receipt',
  { timeout: 30000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter, { protocol: { deadline_epoch: Date.now() / 1000 + 5 } });
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium' });
  const gate = f.control.holdNext(); const busy = parent.prompt('wait'); await gate.entered;
  await eventually(() => f.providerCalls[0].aborted, 10000);
  await busy.catch(() => {}); await f.adapter.poll();
  const state = await f.rpc('state');
  assert.equal(state.lifecycle, 'closed');
  assert.equal(state.completion.stop_reason, 'deadline');
  assert.equal(state.nodes.root.status, 'stopped');
  assert.equal((await f.rpc('brief')).usage.unreported_nodes.includes('root'), true);
});

test('native context methods stay native and effort drift stops the runtime', { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium', prompt: 'context for compaction' });
  assert.equal(parent.compact, f.sdk.AgentSession.prototype.compact);
  assert.equal(parent.refine, f.sdk.AgentSession.prototype.refine);
  assert.equal(parent.settingsManager.getAutoRefineSettings().enabled, false);
  const before = f.providerCalls.length;
  await assert.rejects(parent.refine(), /skipped by extension/);
  assert.equal(f.providerCalls.length, before);
  parent.settingsManager.applyOverrides({ compaction: { keepRecentTokens: 1, reserveTokens: 128 } });
  await parent.prompt('more context for compaction');
  await parent.compact('keep the task context'); await f.adapter.sync();
  assert.ok(f.providerCalls.length > before);
  assert.ok(f.providerCalls.every(call => call.model === ROOT_MODEL && call.effort === 'medium'));
  assert.equal((await f.rpc('brief')).usage.total_tokens, f.providerCalls.length * 17);
  parent.setThinkingLevel('low');
  await eventually(async () => (await f.rpc('state')).lifecycle !== 'open');
  await f.adapter.poll();
  assert.equal((await f.rpc('state')).completion.stop_reason, 'infrastructure_blocked');
  assert.equal(f.providerCalls.some(call => call.effort === 'low'), false);
});

test('finish inside a native root tool drains its final response without abort or self-deadlock',
  { timeout: 60000 }, async t => {
  let rpc, finishEntered, releaseFinish;
  const entered = new Promise(resolve => { finishEntered = resolve; });
  const release = new Promise(resolve => { releaseFinish = resolve; });
  const finishTool = { name: 'fixture_finish', label: 'Finish fixture', description: 'Complete the fixture.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    execute: async () => {
      const receipt = await rpc('finish', { request_id: 'native-finish', candidate_digest: 'candidate-a',
        result_id: 'r-agent', outcome: 'success' });
      assert.equal(receipt.accepted, true); finishEntered(); await release;
      return { content: [{ type: 'text', text: 'finish accepted' }], details: {} };
    } };
  const f = await fixture(t, PrimeControllerAdapter, { evidence: true, customTools: [finishTool] }); rpc = f.rpc;
  const parent = await f.adapter.start({ model: ROOT_MODEL, effort: 'medium' });
  f.control.toolNext('fixture_finish');
  const turn = parent.prompt('finish through the tool'); await entered;
  const drain = f.adapter.poll(); releaseFinish();
  await turn; await drain;
  assert.equal((await f.rpc('state')).completion.stop_reason, 'explicit_finish');
  assert.equal(f.providerCalls.length, 2);
  assert.equal(f.providerCalls.some(call => call.aborted), false);
  assert.equal((await f.rpc('brief')).usage.total_tokens, 34);
});

test('kernel writer lease rejects a concurrent adapter and releases on close', { timeout: 30000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  await assert.rejects(PrimeControllerAdapter.open(f.options), /adapter_already_running/);
  await f.adapter.close(); const second = await f.open(); assert.ok(second.journal);
});

test('a failed observation retries the identical event after restoration', { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  await f.adapter.start({ model: ROOT_MODEL, effort: 'medium' });
  const realRpc = f.adapter.rpc;
  let failed;
  f.adapter.rpc = async (operation, args) => {
    if (operation === 'observe') { failed = structuredClone(args); throw new Error('observe unavailable'); }
    return realRpc(operation, args);
  };
  f.adapter.journal.data.nodes.root.started = true;
  await assert.rejects(f.adapter.observe('root', 'idle'), /observe unavailable/);
  assert.equal((await f.rpc('state')).nodes.root.status, 'admitted');
  assert.equal(f.adapter.journal.data.nodes.root.observation_confirmed, false);
  await f.adapter.close();
  const restored = await f.open(); await restored.restore();
  const replayed = f.calls.filter(call => call.operation === 'observe' && call.args.event_id === failed.event_id);
  assert.ok(replayed.length >= 1);
  assert.deepEqual(replayed[0].args, failed);
  assert.equal((await f.rpc('state')).nodes.root.status, 'idle');
  assert.equal(restored.journal.data.nodes.root.observation_confirmed, true);
});

test('a durable native session binds on recovery if the initial binding RPC failed', { timeout: 60000 }, async t => {
  const f = await fixture(t, PrimeControllerAdapter);
  const realRpc = f.adapter.rpc;
  f.adapter.rpc = async (operation, args) => {
    if (operation === 'bind_session') throw new Error('bind unavailable');
    return realRpc(operation, args);
  };
  await assert.rejects(f.adapter.start({ model: ROOT_MODEL, effort: 'medium' }), /bind unavailable/);
  const saved = f.adapter.journal.data.nodes.root;
  assert.ok(saved.native_session_id);
  assert.equal((await f.rpc('state')).nodes.root.native_session_id, null);
  await f.adapter.close();
  const restored = await f.open(); await restored.restore();
  assert.equal(restored.sessions.get('root').sessionId, saved.native_session_id);
  assert.equal((await f.rpc('state')).nodes.root.native_session_id, saved.native_session_id);
  assert.equal(f.providerCalls.length, 0);
});
