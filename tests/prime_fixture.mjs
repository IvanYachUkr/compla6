/** Offline transport only; integration tests use Prime's real SDK and controller. */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const PRIME_ROOT = '/opt/prime-agent-0.9.3/node_modules/prime-agent';
export const ROOT_MODEL = 'compression-fixture/root';
export const CHILD_MODEL = 'compression-fixture/worker';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const python = [
  'import json,sys',
  'from pathlib import Path',
  'from unittest.mock import patch',
  'sys.path[:0]=[str(Path(sys.argv[1])/"src"),str(Path(sys.argv[1])/"tests")]',
  'from test_controller import EvidenceEngine',
  'from compression_lab.controller import Controller',
  'e=EvidenceEngine(sys.argv[2]); operation=sys.argv[3]; args=json.load(sys.stdin)',
  'if sys.argv[4]=="evidence": e.add()',
  'try:',
  '    with patch("compression_lab.policy.bridge.verify_candidate", return_value=({}, {})):',
  '        value=Controller.configure(e,args).state() if operation=="configure" else getattr(Controller(e),operation)(**args)',
  '    print(json.dumps({"ok":True,"value":value}))',
  'except Exception as error:',
  '    print(json.dumps({"ok":False,"error":str(error)}))',
].join('\n');

export async function fixture(t, Adapter, { protocol = {}, evidence = false, customTools = [] } = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'prime-controller-'));
  const profile = join(directory, 'profile'); mkdirSync(profile, { mode: 0o700 });
  const adapters = [];
  t.after(async () => {
    for (const adapter of adapters.reverse()) await adapter.close();
    rmSync(directory, { recursive: true, force: true });
  });
  const calls = [], providerCalls = [];
  const rpc = async (operation, args = {}) => {
    calls.push({ operation, args: structuredClone(args) });
    const result = spawnSync('python3', ['-c', python, root, directory, operation, evidence ? 'evidence' : 'empty'],
      { input: JSON.stringify(args), encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    const response = JSON.parse(result.stdout);
    if (!response.ok) throw new Error(response.error);
    return response.value;
  };
  await rpc('configure', { schema_version: 1, objective: 'qualification',
    deadline_epoch: Date.now() / 1000 + 120, max_children: 1, max_depth: 1,
    models: { root: [{ model: ROOT_MODEL, effort: 'medium' }],
      worker: [{ model: CHILD_MODEL, effort: 'high' }] }, max_feedback_attempts: 1, ...protocol });
  const sdk = await import(pathToFileURL(join(PRIME_ROOT, 'dist/index.js')));
  const { AssistantMessageEventStream } = await import(pathToFileURL(join(PRIME_ROOT, '../@earendil-works/pi-ai/dist/utils/event-stream.js')));
  let nextGate, nextTool;
  const control = {
    holdNext() {
      let entered, release;
      const gate = { entered: new Promise(resolve => { entered = resolve; }),
        wait: new Promise(resolve => { release = resolve; }), release };
      gate.enter = entered; nextGate = gate; return gate;
    },
    toolNext(name) { nextTool = name; },
  };
  const streamSimple = (model, context, options) => {
    const stream = new AssistantMessageEventStream();
    const gate = nextGate, tool = nextTool; nextGate = undefined; nextTool = undefined;
    const call = { model: model.provider + '/' + model.id, effort: options.reasoning, aborted: false };
    providerCalls.push(call);
    let settled = false;
    const finish = aborted => {
      if (settled) return; settled = true; call.aborted = aborted;
      const usage = aborted ? { input: 0, cacheRead: 0, cacheWrite: 0, output: 0, totalTokens: 0 } :
        { input: 7, cacheRead: 3, cacheWrite: 2, output: 5, totalTokens: 17 };
      const message = { role: 'assistant', content: tool && !aborted ?
        [{ type: 'toolCall', id: 'fixture-tool-' + providerCalls.length, name: tool, arguments: {} }] :
        [{ type: 'text', text: 'offline fixture reply' }],
        api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
        stopReason: aborted ? 'aborted' : tool ? 'toolUse' : 'stop',
        usage: { ...usage, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      stream.push(aborted ? { type: 'error', reason: 'aborted', error: message } :
        { type: 'done', reason: message.stopReason, message }); stream.end();
    };
    if (gate) {
      gate.enter();
      options.signal?.addEventListener('abort', () => finish(true), { once: true });
      if (options.signal?.aborted) finish(true);
      else void gate.wait.then(() => finish(false));
    } else queueMicrotask(() => finish(false));
    return stream;
  };
  const createServices = async options => {
    const authStorage = sdk.AuthStorage.inMemory({ 'compression-fixture': { type: 'api_key', key: 'offline-fixture' } });
    const modelRegistry = sdk.ModelRegistry.inMemory(authStorage);
    modelRegistry.registerProvider('compression-fixture', { api: 'compression-fixture-api',
      baseUrl: 'https://no-network.invalid', apiKey: 'offline-fixture', streamSimple,
      models: ['root', 'worker', 'forbidden'].map(id => ({ id, name: id, reasoning: true, input: ['text'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100000, maxTokens: 1000 })) });
    const settingsManager = sdk.SettingsManager.inMemory({ telemetry: { enabled: false },
      autoRefine: { enabled: false }, retry: { enabled: false }, mcpServers: {} });
    return sdk.createAgentSessionServices({ ...options, authStorage, modelRegistry, settingsManager,
      resourceLoaderOptions: { ...options.resourceLoaderOptions, noSkills: true,
        noContextFiles: true, noPromptTemplates: true, noThemes: true } });
  };
  const options = { rpc, stateDir: join(directory, 'host'), cwd: directory, agentDir: profile,
    createServices, sessionOptions: { customTools, tools: ['ipython', ...customTools.map(tool => tool.name)] } };
  const open = async () => { const adapter = await Adapter.open(options); adapters.push(adapter); return adapter; };
  const adapter = await open();
  return { adapter, open, rpc, calls, providerCalls, control, sdk, options, directory };
}

export async function eventually(predicate, timeout = 10000) {
  const until = Date.now() + timeout;
  while (!(await predicate())) {
    assert.ok(Date.now() < until, 'condition did not become true');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}
