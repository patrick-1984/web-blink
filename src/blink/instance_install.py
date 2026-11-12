"""Native instance-install boundaries: explicit sibling I/O and value-free preflight."""
import os
from pathlib import Path

from .errors import BlinkError
from .storage import encoded


def validate_install_parameters(params, *, allow_http=None):
    """Validate supplied JSON before preview, permit issuance or daemon startup; never read paths."""
    from .client import MAX_MESSAGE
    try:
        size = len(encoded(params).encode())
    except (TypeError, ValueError, RecursionError):
        raise BlinkError('invalid_arguments', 'Install parameters must be bounded strict JSON') from None
    if size > MAX_MESSAGE - 500:
        raise BlinkError('invalid_arguments', 'Install parameters exceed the native transport limit')
    from .instances import materialize_instance
    return materialize_instance(params['document'], base_document=params.get('baseDocument'),
                                allow_http=(os.environ.get('BLINK_ALLOW_HTTP_LOOPBACK') == '1')
                                if allow_http is None else allow_http)


def sibling_base(document, child_input):
    """Read only a validated thin child's explicitly declared, regular sibling JSON file."""
    from .instances import is_instance, validate_definition
    if not is_instance(document):
        return None
    validate_definition(document)
    if 'artifact' not in document['base']:
        return None
    if (not isinstance(child_input, (str, os.PathLike)) or str(child_input) == '-'
            or str(child_input).lstrip().startswith(('{', '['))):
        raise BlinkError('base_required', 'A thin instance needs its explicitly selected child file and sibling base artifact')
    source = str(child_input)
    source = source[7:] if source.startswith('file://') else source
    from .distribution import regular_file, strict_json
    try:
        path = regular_file(Path(source).expanduser().absolute().parent, document['base']['artifact'])
        with path.open('rb') as handle:
            raw = handle.read(2_000_001)
        return strict_json(raw, limit=2_000_000)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise BlinkError('invalid_base', 'The selected base artifact is unavailable or invalid') from None
