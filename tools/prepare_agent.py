#!/usr/bin/env python3
"""Prepare the current instructions and clients in a fresh public workbench."""
import argparse
from pathlib import Path
from compression_lab.engine import ResearcherEngine
from compression_lab.instructions import prompt, configuration
from compression_lab.util import Error, atomic, canonical
import hashlib


def prepare(workspace, protocol=None):
    engine = ResearcherEngine(workspace)
    if protocol is not None:
        from compression_lab.controller import Controller
        Controller.configure(engine, protocol)
    root = Path(__file__).resolve().parents[1]
    out = engine.root/'workbench'
    access = ('\nUse the exposed MCP tools or run `python '+str(out/'lab.py')+
              ' TOOL \'{JSON arguments}\'`. Write candidate sources under `'+str(out/'agent')+'`.\n')
    files = {'INITIAL_PROMPT.md': (prompt(engine)+access).encode(),
             'AGENTS.md': ('# Compression research\n\nRead '+str(out/'INITIAL_PROMPT.md')+
                 '. The sealed run configuration and current user instructions govern this workspace. Use brief for live status.\n').encode()}
    for name in ('lab.py','lab_client.py'):
        files[name] = (root/'tools'/name).read_bytes()
    for name in ('CANDIDATE_ABI.md','AGENT_WORKFLOW.md'):
        files['docs/'+name] = (root/'docs'/name).read_bytes()
    receipt = {'schema_version':1,'configuration_digest':configuration(engine)['configuration_digest'],
               'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()},'model_calls':0}
    files['instruction-receipt.json'] = canonical(receipt)+b'\n'
    for name in files:
        target = out/name
        if target.exists() or target.is_symlink(): raise Error('workbench_file_exists', str(target))
    (out/'agent').mkdir(exist_ok=True)
    for name, data in files.items(): atomic(out/name, data, 0o644)
    return out/'INITIAL_PROMPT.md'


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--workspace',required=True)
    parser.add_argument('--protocol',type=Path,help='Owner completion protocol JSON; configure before generating instructions')
    args=parser.parse_args()
    import json
    print(prepare(args.workspace,json.loads(args.protocol.read_text()) if args.protocol else None))
