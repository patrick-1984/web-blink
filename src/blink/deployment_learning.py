"""Map observed deployment routes back to the shared address contract."""
import re
from urllib.parse import urlsplit

from .errors import BlinkError


def captured_destination(record, origin, path, *, concrete_path=None):
    """Return a relative path and logical server, without publishing private hosts.

    Only the immutable approved address map is consulted. Matching a path prefix
    requires a segment boundary; equally specific slots require human mapping.
    """
    concrete_path = concrete_path if concrete_path is not None else path
    instance = record['compiled'].get('instance')
    target_slot = instance['target']['address'] if instance is not None else None
    matches = []
    for name, value in record['compiled'].get('addresses', {}).items():
        if target_slot is not None and name != target_slot:
            continue
        parsed = urlsplit(value)
        address_origin = parsed.scheme + '://' + parsed.netloc
        prefix = parsed.path.rstrip('/')
        if origin != address_origin:
            continue
        if prefix and not concrete_path.startswith(prefix + '/'):
            continue
        matches.append((len(prefix), name, prefix))
    if matches:
        longest = max(item[0] for item in matches)
        selected = [item for item in matches if item[0] == longest]
        if len(selected) == 1:
            _, name, prefix = selected[0]
            relative = path
            if prefix:
                # Captures template IDs in every segment, including deployment
                # prefixes such as /123 or /deadbeef. Match the concrete path,
                # then remove those prefix segments from its template.
                prefix_parts = prefix.split('/')[1:]
                template_parts = path.split('/')[1:]
                if len(template_parts) > len(prefix_parts) and all(
                        chosen == actual or re.fullmatch(r'\{[A-Za-z][A-Za-z0-9_]*\}', chosen)
                        for chosen, actual in zip(template_parts, prefix_parts)):
                    relative = '/' + '/'.join(template_parts[len(prefix_parts):])
                else:
                    relative = ''
            if relative.startswith('/'):
                return relative, 'blink-address:' + name
    if instance is not None:
        raise BlinkError('instance_scope_violation',
                         'Learned instance operations must stay on the approved target address and path prefix',
                         stage='learn_propose')
    raise BlinkError(
        'deployment_mapping_required',
        'Map this endpoint to one unambiguous approved address slot using a reviewed '
        'full-document patch. Keep its path relative to that address base path.',
        stage='learn_propose')


def compiler_inputs(record):
    """The parent's resolved values, never the current private deployment file."""
    if not record or not record['document']['x-blink-service'].get('addresses'):
        return {}
    from .addresses import snapshot_values
    return {'addresses': snapshot_values(record['document'], record['compiled']),
            'deployment': record['compiled'].get('deployment')}


def non_host_variable_uses(operations):
    """Match the compiler's executable parameter/top-level body markers, not arbitrary JSON text."""
    used = {}
    for operation in operations:
        for parameter in operation.get('parameters', []):
            name = parameter.get('x-blink-variable')
            location = parameter.get('in')
            if isinstance(name, str) and location in ('path', 'query', 'header'):
                used.setdefault(name, set()).add(location)
        for media in operation.get('requestBody', {}).get('content', {}).values():
            schema = media.get('schema')
            if not isinstance(schema, dict):
                continue
            for shape in (schema.get('properties') or {}).values():
                name = shape.get('x-blink-variable') if isinstance(shape, dict) else None
                if isinstance(name, str):
                    used.setdefault(name, set()).add('body')
    return used
