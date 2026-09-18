// WebAssembly implementation of the existing strings-v1 decoder driver ABI.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const [library, operation, inputPath, outputPath, capacityText, idsPath] = process.argv.slice(2);
assert(['decode', 'rows'].includes(operation));
const capacity = Number(capacityText);
assert(Number.isSafeInteger(capacity) && capacity >= 0 && capacity <= 512 * 1024 ** 2);
const now = () => process.hrtime.bigint();
const seconds = (a, b) => Number(b - a) / 1e9;
const compileStart = now();
const module = new WebAssembly.Module(fs.readFileSync(library));
const compileEnd = now();
assert.deepEqual(WebAssembly.Module.imports(module), [
  { module: 'env', name: 'emscripten_notify_memory_growth', kind: 'function' },
]);
const instance = new WebAssembly.Instance(module, {
  env: { emscripten_notify_memory_growth() {} },
});
instance.exports._initialize();
const instantiateEnd = now();
const api = instance.exports;
const heap = () => new Uint8Array(api.memory.buffer);
const input = fs.readFileSync(inputPath);
const ids = operation === 'rows' ? fs.readFileSync(idsPath) : Buffer.alloc(0);
assert.equal(ids.length % 8, 0);
const count = ids.length / 8;
const archive = api.malloc(Math.max(1, input.length));
const output = api.malloc(capacity + 64);
const idBuffer = api.malloc(Math.max(8, ids.length));
const offsetBuffer = api.malloc((count + 1) * 8);
assert(archive && output && idBuffer && offsetBuffer);
heap().set(input, archive); heap().set(ids, idBuffer);
heap().fill(0xa5, output, output + capacity + 64);
const call = state => operation === 'decode'
  ? api.lab_decode(state, output, capacity)
  : api.lab_rows(state, idBuffer, count, output, capacity, offsetBuffer);
const a = now(), state = api.lab_open(archive, input.length), b = now();
assert(state);
const c = now(), length = call(state), d = now();
assert(length >= 0n && length <= BigInt(capacity));
const guard = () => assert(heap().subarray(output + capacity, output + capacity + 64).every(x => x === 0xa5));
guard();
fs.writeFileSync(outputPath, Buffer.from(api.memory.buffer, output, Number(length)));
if (operation === 'rows') fs.writeFileSync(outputPath + '.offsets', Buffer.from(api.memory.buffer, offsetBuffer, (count + 1) * 8));
heap().fill(0xa5, output, output + capacity + 64);
const e = now(), warmLength = call(state), f = now();
assert.equal(warmLength, length); guard();
assert(fs.readFileSync(outputPath).equals(Buffer.from(api.memory.buffer, output, Number(length))));
if (operation === 'rows') assert(fs.readFileSync(outputPath + '.offsets').equals(Buffer.from(api.memory.buffer, offsetBuffer, (count + 1) * 8)));
const g = now(); api.lab_close(state); const h = now();
fs.writeFileSync(outputPath + '.runtime.json', JSON.stringify({
  compile_seconds: seconds(compileStart, compileEnd),
  instantiate_seconds: seconds(compileEnd, instantiateEnd),
  runtime: process.version, runtime_flags: process.execArgv,
  memory_bytes: api.memory.buffer.byteLength,
}));
console.log(JSON.stringify({setup_seconds: seconds(a, b), operation_seconds: seconds(c, d),
  warm_seconds: seconds(e, f), cleanup_seconds: seconds(g, h), output_bytes: Number(length)}));
