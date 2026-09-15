#!/usr/bin/env python3
"""Launch the local evaluator service only in its commissioned runtime."""
import argparse
import os
from pathlib import Path

from compression_lab import runner
from compression_lab.mcp_server import serve
from compression_lab.util import load

parser = argparse.ArgumentParser()
parser.add_argument('--workspace', type=Path, required=True)
parser.add_argument('--port', type=int, default=8766)
args = parser.parse_args()
workspace = args.workspace.absolute()
os.chdir(workspace)
runner.require_fingerprint(load(workspace / 'runtime.json'))
serve(workspace, http_port=args.port, bearer_token_env='COMPRESSION_LAB_TOKEN')
