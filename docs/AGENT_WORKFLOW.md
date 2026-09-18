# File-interface researcher workflow

Start with `brief`: `run_configuration` resolves the sealed dataset card and `search_contract` gives its instructions. Follow that objective, implementation policy, timing and completion method. For the native RAM interface, use [NATIVE_STRINGS.md](NATIVE_STRINGS.md).

Inspect the data, implement a focused experiment, run development checks, then fully evaluate and export useful results. When the run requires a hypothesis, call `record_hypothesis(statement, expected_benefit, falsifier)` before substantive implementation and put its `experiment_id` under `hypothesis` in `candidate.json`. The receipt proves recording before registration, not before source editing.

`manifest_template(name)` supplies a C++ v2 manifest; implement the interfaces in [CANDIDATE_ABI.md](CANDIDATE_ABI.md). Work under `workbench/agent`. Understand the full data path, obey the configured thread and memory limits, optimize measured bottlenecks and preserve independent decoding and validity checks.

The prepared HTTP helper accepts the advertised MCP names and JSON arguments. Replace the placeholders below with IDs returned by the preceding calls:

```sh
python lab.py manifest_template '{"name":"my-codec"}'
python lab.py register '{"candidate_path":"workbench/agent/my-codec"}'
python lab.py evaluate '{"candidate_digest":"CANDIDATE_DIGEST","depth":"quick"}'
python lab.py status '{"job_id":"JOB_ID"}'
```

Save each job ID and poll until terminal. Read `feedback(result_id)` before the next experiment; use `compare(result_ids=[reference, candidate], diagnostics=True)` for paired full results. Serialize measurements and retry temporary benchmark-lease contention. The Python bridge client exposes the same keyword arguments; `lab.last` retains complete evidence when result presentation is shortened.

Screens and quick runs guide search. Final claims require full evaluation, every configured gate and all seven trials. Use the primary size policy consistently and preserve strict deployment totals. Report each selected mode's size, encoding-stage times, decoding speed and memory with its result ID and export.

For `offline-plus-online-v1`, declare the reproducible offline role described in the ABI, including required dataset-dependent compilation. Report offline, online and paired combined time. Every required preparation step belongs in its declared stage; existing runs retain their sealed timing policy.

Continue useful improvements within the commissioned allowances, reserving time for validation and delivery. Follow the completion method in `brief`: export selected results, write `workbench/agent/RESULT.md` and `CHECKPOINT.md` with IDs and paths, then submit `finish` and inspect its receipt when a controller is present, or report to the supervisor. Preserve failed attempts and explain unmet objectives. Model, effort, delegation and time limits come from the commissioned campaign.
