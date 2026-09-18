"""Small public-workbench client for Prime's native MCP bridge; no model calls."""
import json
import os
import stat
from pathlib import Path


def unpack(value):
    """Accept native bridge JSON text and official SDK 1/2 response objects."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    if isinstance(value, str):
        return json.loads(value)
    if not isinstance(value, dict):
        raise TypeError('Expected a JSON object, JSON text or official MCP response')
    structured = value.get('structuredContent')
    if structured is None:
        structured = value.get('structured_content')
    if structured is not None:
        return structured
    if 'content' in value and 'status' not in value:
        text = '\n'.join(x.get('text', '') for x in value['content'] if x.get('type') == 'text')
        if value.get('isError') or value.get('is_error'):
            return {'status': 'tool_error', 'error': text}
        return json.loads(text)
    return value


def brief(value, depth=0):
    """Bound presentation; full parsed evidence remains available in client.last."""
    if isinstance(value, str):
        return value if len(value) <= 1600 else value[:1600] + ' [truncated; full value in lab.last]'
    if isinstance(value, list):
        items = [brief(x, depth + 1) for x in value[:12]]
        return items if len(value) <= 12 else {'items': items, 'remaining_items': len(value)-12}
    if isinstance(value, dict):
        keys = list(value)
        # Baseline responses repeat the same full rows in four views.
        if 'rows' in value and 'pareto_frontier' in value:
            keys = [k for k in keys if k not in ('pareto_frontier', 'best_size', 'best_eligible')]
            keys += ['best_eligible_result_id']
            value = {**value, 'best_eligible_result_id': (value.get('best_eligible') or {}).get('result_id')}
        result = {k: brief(value[k], depth + 1) for k in keys[:24]}
        if len(keys) > 24:
            result['additional_keys_in_lab_last'] = keys[24:]
        return result
    return value


def prepare_candidate(workspace, candidate):
    """Make only owned, regular public candidate files readable by the evaluator."""
    workspace = Path(workspace).absolute()
    candidate = Path(candidate)
    candidate = candidate if candidate.is_absolute() else workspace / candidate
    relative = candidate.relative_to(workspace / 'workbench' / 'agent')
    if '..' in relative.parts:
        raise ValueError('Candidate must stay within workbench/agent')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in candidate.parts[1:]:
            if part in ('.', '..'):
                raise ValueError('Noncanonical candidate path')
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd); fd = child
        count = 0
        def walk(directory):
            nonlocal count
            info = os.fstat(directory)
            if info.st_uid != os.geteuid():
                raise PermissionError('Candidate directories must belong to the agent')
            os.fchmod(directory, 0o755)
            for name in sorted(os.listdir(directory)):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, flags, dir_fd=directory)
                    try: walk(child)
                    finally: os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    count += 1
                    if count > 20000:
                        raise ValueError('Too many candidate files')
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        checked = os.fstat(child)
                        if not stat.S_ISREG(checked.st_mode) or checked.st_nlink != 1 or checked.st_uid != os.geteuid():
                            raise PermissionError('Candidate must contain owned regular files without hardlinks')
                        os.fchmod(child, 0o755 if checked.st_mode & 0o111 else 0o644)
                    finally: os.close(child)
                else:
                    raise ValueError('Symlinks and special files are not public candidate inputs')
        walk(fd)
        return str(candidate)
    finally:
        os.close(fd)


class LabClient:
    """Pass Prime's pre-imported `mcp` bridge, then await lab.call(tool, **args)."""
    def __init__(self, mcp, workspace=None, server='compression-lab'):
        self.mcp = mcp
        self.workspace = Path(workspace or Path.cwd()).absolute()
        self.server = server
        self.last = None

    def __getattr__(self, name):
        """Forward public tool names; the connected server defines their schemas."""
        if name.startswith('_'):
            raise AttributeError(name)
        async def invoke(**arguments):
            return await self.call(name, **arguments)
        return invoke

    async def call(self, tool, **arguments):
        if tool == 'register':
            arguments['candidate_path'] = prepare_candidate(self.workspace, arguments['candidate_path'])
        self.last = unpack(await self.mcp.call_tool(self.server, tool, arguments))
        return self.last if tool in ('brief', 'manifest_template') else brief(self.last)
