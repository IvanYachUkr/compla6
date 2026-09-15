#!/usr/bin/env node
/** Native Prime SDK adapter; run this host with authority isolated from agent tools.
 * Usage is native-reported cumulative own usage, including cached input. It is
 * observation, never a token reservation or a claim about unreported requests.
 */
import { createHash, randomUUID } from 'node:crypto';
import { spawn, spawnSync } from 'node:child_process';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync,
  renameSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const PRIME_ROOT = '/opt/prime-agent-0.9.3/node_modules/prime-agent';
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const stable = (prefix, value) => prefix + '-' + hash(value);
function check(value, code) { if (!value) throw new Error(code); }
const modelName = model => model && model.provider + '/' + model.id;
function durableFile(path) {
  const fd = openSync(path, 'r'); try { fsyncSync(fd); } finally { closeSync(fd); }
}
function atomicJson(path, value) {
  const temporary = path + '.' + randomUUID() + '.tmp';
  const fd = openSync(temporary, 'wx', 0o600);
  try { writeFileSync(fd, JSON.stringify(value)); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(temporary, path); durableFile(dirname(path));
}

export function sourcePreflight(primeRoot = PRIME_ROOT) {
  const version = JSON.parse(readFileSync(join(primeRoot, 'package.json'))).version;
  check(version === '0.9.3', 'prime_version_mismatch');
  check(existsSync(join(primeRoot, 'dist/index.js')), 'prime_sdk_missing');
  return { name: 'prime-agent', version, production_supported: true,
    usage_scope: 'native_reported_own_usage', token_ceiling: false };
}

/** Injectable host RPC. The default CLI bridge is for a trusted local host. */
export function controllerHost({ workspace, python = 'python3', sourceDir = resolve(dirname(fileURLToPath(import.meta.url)), '../src') }) {
  return async (operation, args = {}) => {
    const call = spawnSync(python, ['-m', 'compression_lab.cli', 'controller-host', '--workspace', resolve(workspace),
      '--operation', operation, '--arguments', '-'], { input: JSON.stringify(args), encoding: 'utf8',
      env: { ...process.env, PYTHONPATH: sourceDir }, maxBuffer: 4 * 1024 * 1024 });
    if (call.error) throw call.error;
    let response;
    try { response = JSON.parse(call.stdout); } catch { throw new Error('invalid_controller_host_response'); }
    check(call.status === 0 && !response.error, 'controller_host:' + JSON.stringify(response.error ?? response.errors));
    return response.metrics;
  };
}

/** Kernel lease is released by EOF if this process crashes; no stale-PID gate. */
class Journal {
  static async open(directory, context) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const writer = spawn('/usr/bin/flock', ['--nonblock', join(directory, 'adapter.lock'),
      '/bin/sh', '-c', 'printf READY; cat >/dev/null'], { stdio: ['pipe', 'pipe', 'ignore'] });
    writer.stdin.on('error', () => {});
    await new Promise((accept, reject) => {
      writer.once('error', reject);
      writer.once('exit', () => reject(new Error('adapter_already_running')));
      writer.stdout.once('data', () => accept());
    });
    const journal = new Journal(); journal.writer = writer; journal.path = join(directory, 'adapter.json');
    try {
      journal.data = existsSync(journal.path) ? JSON.parse(readFileSync(journal.path)) :
        { schema_version: 2, context, nodes: {}, launches: {}, deliveries: {} };
      check(journal.data.schema_version === 2 && JSON.stringify(journal.data.context) === JSON.stringify(context),
        'adapter_context_mismatch');
      journal.save(); return journal;
    } catch (error) { await journal.close(); throw error; }
  }
  save() { check(this.writer.exitCode === null && !this.closed, 'adapter_writer_lost'); atomicJson(this.path, this.data); }
  async close() {
    if (this.closed) return; this.closed = true;
    if (this.writer.exitCode !== null) return;
    await new Promise(resolveExit => { this.writer.once('exit', resolveExit); this.writer.stdin.end(); });
  }
}

export class PrimeControllerAdapter {
  static async open({ rpc, stateDir, cwd, agentDir, primeRoot = PRIME_ROOT, createServices, sessionOptions = {} }) {
    sourcePreflight(primeRoot);
    check(typeof rpc === 'function' && stateDir && cwd && agentDir, 'missing_adapter_inputs');
    check(statSync(cwd).isDirectory() && statSync(agentDir).isDirectory(), 'invalid_native_workspace_or_profile');
    const state = await rpc('state');
    const adapter = new PrimeControllerAdapter({ rpc, stateDir: resolve(stateDir), cwd: resolve(cwd),
      agentDir: resolve(agentDir), primeRoot, createServices, sessionOptions, state });
    adapter.journal = await Journal.open(adapter.stateDir, { run_id: state.run_id, protocol_digest: state.protocol_digest });
    try { await adapter.initialize(); } catch (error) { await adapter.close(); throw error; }
    // Supervision is armed before start(prompt), so the initial turn is bounded too.
    if (state.protocol.deadline_epoch != null) {
    adapter.deadlineTimer = setTimeout(() => {
      void adapter.stop('deadline').catch(error => { adapter.backgroundError = error; });
    }, Math.max(0, state.protocol.deadline_epoch * 1000 - Date.now()));
    adapter.deadlineTimer.unref();
    }
    return adapter;
  }
  constructor(options) {
    Object.assign(this, options); this.sessions = new Map(); this.runtimes = new Map();
    this.pending = new Map(); this.observations = Promise.resolve(); this.proofWaiters = new Map();
    this.subscriptions = [];
  }
  async initialize() {
    this.sdk = await import(pathToFileURL(join(this.primeRoot, 'dist/index.js')));
    this.messages = await import(pathToFileURL(join(this.primeRoot, 'dist/core/agent-messages.js')));
    for (const name of ['createAgentSessionServices', 'createAgentSessionFromServices', 'createAgentSessionRuntime', 'SessionManager']) {
      check(typeof this.sdk[name] === 'function', 'unsupported_sdk_method:' + name);
    }
    this.createServices ??= this.sdk.createAgentSessionServices;
    if (this.state.lifecycle === 'open') await this.rpc('attach_backend', { name: 'prime-agent', version: '0.9.3' });
    else check(this.state.backend?.name === 'prime-agent' && this.state.backend.version === '0.9.3', 'backend_identity_mismatch');
  }
  async admit(nodeId, parentId, model, effort) {
    await this.rpc('guard_research');
    const node = await this.rpc('admit', { request_id: stable('admit', [nodeId, parentId, model, effort]),
      node_id: nodeId, parent_id: parentId, model, effort });
    this.state.nodes[nodeId] = node; return node;
  }
  identity(nodeId, session = this.sessions.get(nodeId)) {
    const expected = this.state.nodes[nodeId];
    check(expected && session && modelName(session.model) === expected.model &&
      session.thinkingLevel === expected.effort, 'native_identity_mismatch');
  }
  verifySaved(manager, node) {
    const saved = manager.buildSessionContext();
    if (saved.model) check(saved.model.provider + '/' + saved.model.modelId === node.model, 'restored_model_mismatch');
    if (manager.getBranch().some(entry => entry.type === 'thinking_level_change')) {
      check(saved.thinkingLevel === node.effort, 'restored_effort_mismatch');
    }
  }
  async start({ nodeId = 'root', model, effort, prompt, name = 'root' }) {
    check(!this.journal.data.nodes[nodeId], 'existing_session_requires_restore');
    const intent = stable('launch', [nodeId, null]);
    check(!this.journal.data.launches[intent], 'ambiguous_launch_not_replayed');
    await this.admit(nodeId, null, model, effort);
    this.journal.data.launches[intent] = { node_id: nodeId, state: 'dispatched', signature: hash([name, model, effort]) };
    this.journal.save();
    const manager = this.sdk.SessionManager.create(this.cwd, join(this.stateDir, 'sessions'));
    const runtime = await this.construct(nodeId, manager, {}, { name });
    runtime.session.setSessionName(name); this.persistNode(nodeId, runtime.session, { name });
    this.journal.data.launches[intent].state = 'bound'; this.journal.save();
    if (prompt !== undefined) { await this.rpc('guard_research'); await runtime.session.prompt(prompt); }
    await this.sync(); return runtime.session;
  }
  persistNode(nodeId, session, details = {}) {
    session.sessionManager.flushNow();
    check(session.sessionFile && existsSync(session.sessionFile), 'native_session_not_durable');
    durableFile(session.sessionFile);
    const node = this.state.nodes[nodeId];
    this.journal.data.nodes[nodeId] = { sequence: 0, ...this.journal.data.nodes[nodeId],
      node_id: nodeId, parent_id: node.parent_id, model: node.model, effort: node.effort,
      native_session_id: session.sessionId, native_file: session.sessionFile, ...details };
    this.journal.save();
  }
  async construct(nodeId, manager, initialOptions = {}, details = {}) {
    this.verifySaved(manager, this.state.nodes[nodeId]);
    const factory = async incoming => {
      const pending = this.pending.get(incoming.sessionOptions?.rlmSessionDir);
      const id = pending?.node_id ?? nodeId;
      const expected = this.state.nodes[id]; check(expected, 'unadmitted_native_child');
      this.verifySaved(incoming.sessionManager, expected);
      const policyHooks = pi => {
        // These public hooks return decisions; throwing in provider hooks is not an admission gate.
        pi.on('session_before_refine', () => ({ skip: true }));
        pi.on('session_before_tree', () => ({ cancel: true }));
        pi.on('session_before_switch', () => ({ cancel: true }));
        pi.on('session_before_fork', () => ({ cancel: true }));
        pi.on('session_before_compact', async () => {
          try { this.identity(id); await this.rpc('guard_research'); return {}; }
          catch { return { cancel: true }; }
        });
      };
      const services = await this.createServices({ cwd: this.cwd, agentDir: this.agentDir,
        telemetryDisabled: true, noBuiltinHerdrReporter: true,
        resourceLoaderOptions: { noExtensions: true, additionalSkillPaths: [join(this.primeRoot, 'skills/agent-message')],
          extensionFactories: [policyHooks] } });
      // Native refinement requests deliberately omit reasoning; suppress that unsupported helper path.
      services.settingsManager.applyOverrides({ autoRefine: { enabled: false } });
      check(services.resourceLoader.getSkills().skills.some(skill => skill.name === 'agent-message'), 'native_agent_message_skill_missing');
      const slash = expected.model.indexOf('/');
      const model = services.modelRegistry.find(expected.model.slice(0, slash), expected.model.slice(slash + 1));
      check(model, 'configured_native_model_unavailable');
      const result = await this.sdk.createAgentSessionFromServices({ ...incoming.sessionOptions, ...this.sessionOptions,
        services, sessionManager: incoming.sessionManager, model, thinkingLevel: expected.effort,
        agentMessageController: this.messageController(id), rlmMaxDepth: this.state.protocol.max_depth,
        prewarmIpythonKernel: false, serializedRefine: true, telemetryDisabled: true });
      check(!result.modelFallbackMessage, 'native_model_fallback_forbidden');
      this.identity(id, result.session);
      this.sessions.set(id, result.session);
      this.persistNode(id, result.session, pending ?? details);
      try {
        await this.rpc('bind_session', { node_id: id, native_session_id: result.session.sessionId,
          model: expected.model, effort: expected.effort });
      } catch (error) {
        this.sessions.delete(id); await result.session.disposeAsync(); throw error;
      }
      this.watch(id, result.session);
      return { ...result, services, diagnostics: services.diagnostics };
    };
    const runtime = await this.sdk.createAgentSessionRuntime(factory, { cwd: this.cwd, agentDir: this.agentDir,
      sessionManager: manager, sessionOptions: { rlmDepth: this.state.nodes[nodeId].depth, ...initialOptions } });
    this.installHost(nodeId, runtime);
    await runtime.session.bindExtensions({});
    return runtime;
  }
  installHost(nodeId, runtime) {
    this.runtimes.set(nodeId, runtime);
    runtime.setSubagentRuntimeHost({
      createRlmSubagentRuntime: async options => {
        this.identity(nodeId); await this.rpc('guard_research');
        const name = options.sessionName;
        const id = stable('node', [nodeId, name]), intent = stable('launch', [nodeId, name]);
        check(!this.journal.data.launches[intent], 'ambiguous_launch_not_replayed');
        await this.admit(id, nodeId, modelName(options.model), options.thinkingLevel);
        this.journal.data.launches[intent] = { node_id: id, state: 'dispatched',
          signature: hash([name, modelName(options.model), options.thinkingLevel, options.prompt]) };
        this.journal.save();
        this.pending.set(options.sessionDir, { node_id: id, native_child_id: options.id, name });
        try {
          const child = await runtime.createRlmSubagentRuntime({ ...options, onSessionPublished: session => {
            const childRuntime = runtime.listSubagentRuntimes().find(candidate => candidate.session === session);
            check(childRuntime, 'native_child_runtime_unavailable');
            this.installHost(id, childRuntime);
            options.onSessionPublished?.(session);
          } });
          this.journal.data.launches[intent].state = 'bound'; this.journal.save(); return child;
        } catch (error) {
          await this.observe(id, 'failed'); throw error;
        } finally { this.pending.delete(options.sessionDir); }
      },
      deleteRlmSubagentRuntime: (id, session) => runtime.deleteRlmSubagentRuntime(id, session),
    });
  }
  watch(nodeId, session) {
    const queue = () => {
      if (this.closed) return;
      this.observations = this.observations.then(() => this.snapshot(nodeId))
        .catch(error => { this.backgroundError = error; });
    };
    this.subscriptions.push(session.sessionManager.onPersist(() => { this.resolveProofs(nodeId); queue(); }));
    this.subscriptions.push(session.subscribe(event => {
      if (event.type === 'agent_start') {
        this.journal.data.nodes[nodeId].started = true; this.journal.save();
      }
      if (['agent_start', 'agent_end', 'session_action_update', 'compaction_end', 'thinking_level_changed'].includes(event.type)) queue();
      if (event.type === 'thinking_level_changed') {
        try { this.identity(nodeId); }
        catch { void this.stop('infrastructure_blocked').catch(error => { this.backgroundError = error; }); }
      }
    }));
  }
  async observe(nodeId, status) {
    let record = this.journal.data.nodes[nodeId];
    if (!record) {
      record = this.journal.data.launches[Object.keys(this.journal.data.launches)
        .find(key => this.journal.data.launches[key].node_id === nodeId)];
    }
    if (record.status === status && record.observation_confirmed) return;
    if (record.status !== status) {
      record.sequence = (record.sequence ?? 0) + 1; record.status = status;
    }
    record.observation_confirmed = false; this.journal.save();
    await this.rpc('observe', { event_id: stable('event', [nodeId, record.sequence]), node_id: nodeId, status, sequence: record.sequence });
    record.observation_confirmed = true; this.journal.save();
  }
  async snapshot(nodeId) {
    const session = this.sessions.get(nodeId); if (!session) return;
    const record = this.journal.data.nodes[nodeId];
    const own = session.getOwnUsageSummary();
    if (own && Number.isSafeInteger(own.inputTokens) && Number.isSafeInteger(own.outputTokens)) {
      const previous = record.usage;
      if (!previous || previous.input_tokens !== own.inputTokens || previous.output_tokens !== own.outputTokens) {
        record.usage = { input_tokens: own.inputTokens, output_tokens: own.outputTokens,
          sequence: (previous?.sequence ?? -1) + 1, confirmed: false }; this.journal.save();
      }
    }
    if (record.usage && !record.usage.confirmed) {
      const { confirmed, ...counts } = record.usage;
      try {
        await this.rpc('record_usage', { node_id: nodeId, native_session_id: record.native_session_id, ...counts });
        record.usage.confirmed = true; this.journal.save(); this.usageError = undefined;
      } catch (error) { this.usageError = error; } // Persisted snapshot retries; accounting never gates research.
    }
    const status = this.stopping ? 'stopped' : session.isSessionActive ? 'running' :
      record.started ? record.parent_id ? 'completed' : 'idle' : record.status ?? 'admitted';
    if (!(this.stopping && session.isSessionActive)) await this.observe(nodeId, status);
  }
  async sync() {
    for (const id of this.sessions.keys()) {
      this.observations = this.observations.then(() => this.snapshot(id))
        .catch(error => { this.backgroundError = error; });
    }
    await this.observations;
    if (this.backgroundError) throw this.backgroundError;
  }
  async restore() {
    const nodes = Object.values(this.journal.data.nodes);
    check(nodes.length, 'no_retained_sessions');
    this.state = await this.rpc('state');
    for (const launch of Object.values(this.journal.data.launches)) {
      if (!this.journal.data.nodes[launch.node_id] && launch.status && !launch.observation_confirmed) {
        await this.observe(launch.node_id, launch.status);
      }
    }
    for (const saved of nodes.sort((a, b) => this.state.nodes[a.node_id].depth - this.state.nodes[b.node_id].depth)) {
      const node = this.state.nodes[saved.node_id];
      check(node && node.model === saved.model && node.effort === saved.effort &&
        (node.native_session_id === null || node.native_session_id === saved.native_session_id), 'restored_identity_mismatch');
      check(existsSync(saved.native_file), 'retained_session_missing');
      const manager = this.sdk.SessionManager.open(saved.native_file);
      check(manager.getSessionId() === saved.native_session_id, 'restored_native_session_mismatch');
      await this.construct(saved.node_id, manager, { rlmDepth: node.depth,
        rlmParentNodeId: saved.native_child_id, rlmSessionDir: saved.native_child_id ? dirname(saved.native_file) : undefined,
        semanticParentSessionId: this.journal.data.nodes[saved.parent_id]?.native_session_id }, saved);
      const parent = this.sessions.get(saved.parent_id);
      if (parent) parent.registerRlmChildSession(saved.native_child_id, this.sessions.get(saved.node_id));
    }
    await this.reconcileDeliveries(); await this.sync(); return this.sessions;
  }
  catalog() {
    return Object.values(this.journal.data.nodes).map(node => ({ id: node.native_session_id, name: node.name,
      depth: this.state.nodes[node.node_id].depth, status: this.sessions.get(node.node_id)?.isSessionActive ? 'running' : 'idle',
      sessionPath: node.native_file, parentSessionId: this.journal.data.nodes[node.parent_id]?.native_session_id,
      parentSessionPath: this.journal.data.nodes[node.parent_id]?.native_file }));
  }
  messageController(nodeId) {
    return {
      roster: async () => { const rows = this.catalog(); return this.messages.buildAgentFamilyRoster(
        rows.find(row => row.id === this.sessions.get(nodeId)?.sessionId), rows); },
      listAgents: async () => {
        const agents = Object.values(this.journal.data.nodes).map(node => ({ activeSessionId: node.native_session_id,
          sessionId: node.native_session_id, sessionPath: node.native_file, sessionName: node.name,
          runtimeKind: node.parent_id ? 'subagent' : 'top-level', parentActiveSessionId: this.journal.data.nodes[node.parent_id]?.native_session_id,
          parentSessionId: this.journal.data.nodes[node.parent_id]?.native_session_id, rlmChildId: node.native_child_id,
          sessionDir: dirname(node.native_file), isStreaming: this.sessions.get(node.node_id)?.isStreaming ?? false }));
        return { current: agents.find(row => row.sessionId === this.sessions.get(nodeId)?.sessionId), agents };
      },
      sendAgentMessage: async ({ target, message }) => {
        const source = this.journal.data.nodes[nodeId];
        const recipient = Object.values(this.journal.data.nodes).find(node => [node.name, node.node_id, node.native_session_id].includes(target));
        check(recipient, 'unknown_message_target');
        const rows = this.catalog();
        const relation = this.messages.assertAgentFamilyReach(rows.find(row => row.id === recipient.native_session_id),
          rows.find(row => row.id === source.native_session_id));
        const submission = await this.acceptNative(recipient.node_id, 'agentmsg_' + randomUUID(), message, source, relation);
        // Native queued receipts must return promptly even when the recipient is busy.
        submission.committed.catch(() => {});
        return submission.receipt;
      },
    };
  }
  nativeProof(nodeId, nativeId) {
    const file = this.journal.data.nodes[nodeId]?.native_file;
    if (!file || !existsSync(file)) return false;
    // A crashed append can leave one incomplete tail; earlier committed lines remain proof.
    const lines = readFileSync(file, 'utf8').split('\n');
    let accepted = false;
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i]) continue;
      let entry;
      try { entry = JSON.parse(lines[i]); }
      catch (error) { if (i === lines.length - 1) break; throw error; }
      accepted ||= entry.type === 'custom_message' && entry.customType === 'agent_message' && entry.details?.id === nativeId;
    }
    return accepted;
  }
  resolveProofs(nodeId) {
    for (const [id, waiter] of this.proofWaiters) {
      if (waiter.nodeId === nodeId && this.nativeProof(nodeId, id)) {
        durableFile(this.journal.data.nodes[nodeId].native_file);
        this.proofWaiters.delete(id); waiter.resolve();
      }
    }
  }
  async acceptNative(nodeId, nativeId, message, source, relationship) {
    await this.rpc('guard_research');
    const session = this.sessions.get(nodeId); this.identity(nodeId, session);
    const payload = { id: nativeId, source: 'agent_message', message,
      from: source ? { sessionId: source.native_session_id, activeSessionId: source.native_session_id, sessionName: source.name } : undefined,
      fromRelationship: relationship, target: { sessionId: session.sessionId, activeSessionId: session.sessionId, sessionName: session.sessionName } };
    const custom = this.messages.createAgentSessionMessage(payload);
    const committed = new Promise((resolveProof, reject) => { this.proofWaiters.set(nativeId, { nodeId, resolve: resolveProof, reject }); });
    committed.catch(() => {});
    const delivery = session.waitForAgentMessagePromptDelivery(nativeId);
    delivery.catch(error => { this.proofWaiters.get(nativeId)?.reject(error); this.proofWaiters.delete(nativeId); });
    let queued = false;
    try {
      await session.acceptAgentMessagePrompt(custom.content, { expandPromptTemplates: false, streamingBehavior: 'steer', queueIfBusy: true,
        customMessage: custom, admissionCommitted: () => this.identity(nodeId, session),
        preflightResult: (success, wasQueued) => { check(success, 'native_delivery_rejected'); queued = wasQueued; } });
      this.resolveProofs(nodeId);
      return { receipt: this.messages.createAgentSessionMessageReceipt(payload, queued ? 'queued' : 'delivered'), committed };
    } catch (error) { this.proofWaiters.get(nativeId)?.reject(error); this.proofWaiters.delete(nativeId); throw error; }
  }
  async reconcileDeliveries() {
    for (const pending of await this.rpc('unacknowledged_deliveries')) {
      const nativeId = this.journal.data.deliveries[pending.id]?.native_id ?? 'agentmsg_' + hash(pending.id);
      if (!this.nativeProof(pending.target, nativeId)) continue;
      durableFile(this.journal.data.nodes[pending.target].native_file);
      const receipt = stable('accepted', [this.sessions.get(pending.target).sessionId, nativeId]);
      this.journal.data.deliveries[pending.id] = { target: pending.target, native_id: nativeId, state: 'accepted', native_receipt: receipt };
      this.journal.save();
      await this.rpc('ack_delivery', { message_id: pending.id, native_receipt: receipt });
      this.journal.data.deliveries[pending.id].state = 'acked'; this.journal.save();
    }
  }
  async deliverPending() {
    await this.reconcileDeliveries();
    for (const pending of await this.rpc('pending_deliveries')) {
      const message = await this.rpc('claim_delivery', { message_id: pending.id });
      const nativeId = 'agentmsg_' + hash(pending.id);
      this.journal.data.deliveries[pending.id] = { target: pending.target, native_id: nativeId, state: 'dispatched' }; this.journal.save();
      const submission = await this.acceptNative(pending.target, nativeId, JSON.stringify(message.message));
      await submission.committed; await this.reconcileDeliveries();
    }
  }
  async drain(abort) {
    if (abort && !this.stopping) {
      this.stopping = true;
      this.abortTask = Promise.all([...this.sessions.values()].map(session => session.abort()));
      for (const waiter of this.proofWaiters.values()) waiter.reject(new Error('native_delivery_stopped'));
      this.proofWaiters.clear();
    }
    this.drainTask ??= (async () => {
      await this.abortTask;
      for (const session of this.sessions.values()) {
        await session.waitForHeadlessIdle();
        try { await session.waitForRlmQuiescence(); }
        catch (error) { if (!this.stopping) throw error; await session.waitForHeadlessIdle(); }
      }
      await this.sync(); await this.reconcileDeliveries(); return this.rpc('reconcile');
    })();
    return this.drainTask;
  }
  async stop(reason) {
    if (this.stopTask) return this.stopTask;
    this.stopTask = (async () => { await this.rpc('stop', { reason }); return this.drain(true); })();
    return this.stopTask;
  }
  async poll() {
    await this.sync(); await this.rpc('reconcile');
    const state = await this.rpc('state');
    if (state.lifecycle === 'open') await this.deliverPending();
    else {
      // finish() may run inside the root's own MCP tool: let its final response complete.
      const explicitFinish = state.lifecycle === 'closed' && state.completion?.stop_reason === 'explicit_finish';
      await this.drain(!explicitFinish);
    }
    return this.rpc('reconcile');
  }
  async close() {
    if (this.closed) return;
    clearTimeout(this.deadlineTimer);
    for (const session of this.sessions.values()) await session.abort();
    await this.observations;
    this.closed = true;
    for (const unsubscribe of this.subscriptions) unsubscribe();
    for (const waiter of this.proofWaiters.values()) waiter.reject(new Error('adapter_closed'));
    this.proofWaiters.clear();
    try { for (const runtime of [...this.runtimes.values()].reverse()) await runtime.dispose(); }
    finally { await this.journal?.close(); }
  }
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  const flags = {};
  for (let i = 0; i < args.length; i += 2) { check(args[i]?.startsWith('--') && args[i + 1], 'invalid_cli_flags'); flags[args[i].slice(2)] = args[i + 1]; }
  if (command === 'preflight') { console.log(JSON.stringify(sourcePreflight(flags['prime-root']), null, 2)); return; }
  check(command === 'run' && flags.workspace && flags['state-dir'] && flags['agent-dir'], 'usage: run --workspace PATH --state-dir PATH --agent-dir PATH --model PROVIDER/MODEL --effort LEVEL --prompt-file PATH; use --resume true to restore');
  const adapter = await PrimeControllerAdapter.open({ rpc: controllerHost({ workspace: flags.workspace, python: flags.python }),
    stateDir: flags['state-dir'], cwd: flags.cwd ?? flags.workspace, agentDir: flags['agent-dir'], primeRoot: flags['prime-root'] });
  const shutdown = () => { void adapter.stop('operator_stop').catch(error => { adapter.backgroundError = error; }); };
  process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);
  let pollTask;
  try {
    const startup = flags.resume === 'true' ? adapter.restore() :
      adapter.start({ model: flags.model, effort: flags.effort, name: flags.name ?? 'controller',
        prompt: flags['prompt-file'] ? readFileSync(flags['prompt-file'], 'utf8') : undefined });
    // Poll while the first prompt is in flight, and leave deadline supervision independent.
    pollTask = (async () => {
      while (!adapter.closed) {
        await new Promise(resolveTick => setTimeout(resolveTick, 250));
        if (adapter.closed) break;
        const state = await adapter.poll();
        if (state.lifecycle === 'closed') break;
      }
    })();
    pollTask.catch(() => {});
    await Promise.all([startup, pollTask]);
    console.log(JSON.stringify(await adapter.rpc('brief')));
  } finally {
    process.off('SIGINT', shutdown); process.off('SIGTERM', shutdown); await adapter.close();
    await pollTask?.catch(() => {});
  }
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(JSON.stringify({ status: 'failed', error: error.message })); process.exitCode = 2; });
}
