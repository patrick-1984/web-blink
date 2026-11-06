"""Stable, value-free runtime errors."""
import re
import uuid

class BlinkError(Exception):
    def __init__(self, code, message, *, stage=None, field_path=None, http_status=None, dispatched=False):
        self.code = code
        self.message = message
        self.stage = stage
        self.field_path = field_path
        self.http_status = http_status
        self.dispatched = dispatched
        super().__init__(message)


def error_detail(exc, stage, *, dispatched=False):
    """Only trusted codes and field paths; never serialize exception text, values or tracebacks."""
    code = exc.code if isinstance(exc, BlinkError) else 'internal_error'
    code = code if re.fullmatch(r'[a-z_]{1,100}', code) else 'internal_error'
    path = getattr(exc, 'field_path', None)
    path = path if isinstance(path, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,300}', path) else None
    status = getattr(exc, 'http_status', None)
    state = getattr(exc, 'dispatched', dispatched)
    if state is not None and type(state) is not bool: state = None
    if state is None:
        recovery = 'Inspect current status; reconcile the original intent for a write. Do not repeat the mutation.'
    elif state:
        recovery = 'Inspect the returned status and verify the result with a read. Reconcile writes; do not repeat the mutation.'
    else:
        recovery = 'Correct the indicated field or prerequisite, then resume the original action.'
    return {'stage': getattr(exc, 'stage', None) or stage, 'code': code, 'fieldPath': path,
            'correlationId': uuid.uuid4().hex,
            'httpStatus': status if type(status) is int and 100 <= status <= 599 else None,
            'dispatched': state, 'recovery': recovery}


def failure(exc, stage, *, data=None, status=None, summary=None, dispatched=False):
    detail = error_detail(exc, stage, dispatched=dispatched)
    return {'status': status or detail['code'],
            'summary': summary or (exc.message if isinstance(exc, BlinkError) else 'Runtime operation could not complete'),
            'warnings': [], 'data': {**(data or {}), 'error': detail}}
