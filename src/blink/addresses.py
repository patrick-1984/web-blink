"""Resolve declared deployment addresses into an immutable, bounded execution document.

Only destination fields interpret ``blink-address:``. Private values never replace arbitrary text,
and inspection stand-ins are deliberately absent from the returned executable address snapshot.
"""
import copy
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

from .errors import BlinkError

NAME = re.compile(r'[a-z][a-z0-9-]{0,63}')
REFERENCE = re.compile(r'blink-address:([a-z][a-z0-9-]{0,63})')
STANDIN_SUFFIX = '.blink-address.invalid'
PREFIX = re.compile(r"(?:/[A-Za-z0-9._~!$&'()*+,;=:@-]+)*/?")


def _ipv6_host(address):
    """Serialize eight hex groups as WHATWG does, including mapped IPv4.

    Some Python versions render mapped addresses with dotted IPv4 notation.
    Browsers always serialize those final groups in hexadecimal.
    """
    number = int(ipaddress.IPv6Address(address))
    groups = [(number >> shift) & 0xffff for shift in range(112, -1, -16)]
    best_start, best_length, index = -1, 1, 0
    while index < len(groups):
        if groups[index]:
            index += 1
            continue
        end = index
        while end < len(groups) and not groups[end]:
            end += 1
        if end - index > best_length:
            best_start, best_length = index, end - index
        index = end
    encoded = [f'{group:x}' for group in groups]
    if best_start >= 0:
        return ':'.join(encoded[:best_start]) + '::' + ':'.join(encoded[best_start + best_length:])
    return ':'.join(encoded)


def base_url(value, *, allow_http=False, allow_fqdn=False):
    """Canonical exact origin plus a safe optional deployment prefix; never credentials or templates."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(c.isspace() or unicodedata.category(c).startswith('C') for c in value)
            or any(c in value for c in '\\?#%{}')):
        raise BlinkError('invalid_address', 'An address must be an exact HTTPS URL or private FQDN without credentials, query or fragment')
    if '://' not in value:
        if not allow_fqdn or '/' in value:
            raise BlinkError('invalid_address', 'Public address URLs must include https://')
        value = 'https://' + value
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ('https', 'http') or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or port is not None and not 1 <= port <= 65535):
            raise ValueError()
        if ':' in host:
            host = _ipv6_host(host)
            literal = '[' + host + ']'
        else:
            try:
                host = str(ipaddress.IPv4Address(host))
            except ValueError:
                host = host.encode('idna').decode('ascii').lower()
                labels = host.split('.')
                if (len(host) > 253 or host.endswith(STANDIN_SUFFIX) or host == STANDIN_SUFFIX[1:]
                        or host != 'localhost' and len(labels) < 2
                        or labels[-1].isdigit() or re.fullmatch(r'0x[0-9a-f]*', labels[-1])
                        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in labels)):
                    raise ValueError()
            literal = host
        if parsed.scheme == 'http' and not (allow_http and host in ('localhost', '127.0.0.1', '::1')):
            raise ValueError()
        path = parsed.path
        if not PREFIX.fullmatch(path) or any(x in ('.', '..') for x in path.split('/')) or '//' in path:
            raise ValueError()
        # A trailing slash denotes the same deployment prefix; operation paths keep their own slash.
        path = path.rstrip('/')
        default = 443 if parsed.scheme == 'https' else 80
        return parsed.scheme + '://' + literal + (f':{port}' if port and port != default else '') + path
    except (ValueError, UnicodeError):
        raise BlinkError('invalid_address', 'Address requires a concrete valid host, HTTPS (or enabled loopback HTTP), and a safe path prefix') from None


def _origin(value):
    parsed = urlsplit(value)
    return parsed.scheme + '://' + parsed.netloc


def _reference(value, declarations):
    match = REFERENCE.fullmatch(value) if isinstance(value, str) else None
    if match:
        name = match.group(1)
        if name not in declarations:
            raise BlinkError('invalid_manifest', f'Address slot {name} is not declared in x-blink-service.addresses')
        return name
    if isinstance(value, str) and 'blink-address:' in value:
        raise BlinkError('invalid_manifest', 'An address reference must occupy the whole destination field')
    return None


def resolve_addresses(doc, values=None, *, deployment=None, require=True, allow_http=False):
    """Mutate a compiler-owned copy, returning its referenced concrete address snapshot and markers."""
    service = doc['x-blink-service']
    declarations = service.get('addresses') or {}
    if deployment is not None and (not isinstance(deployment, str) or not NAME.fullmatch(deployment)):
        raise BlinkError('invalid_arguments', 'deployment must be a lowercase named local deployment')
    if values is not None and not isinstance(values, dict):
        raise BlinkError('invalid_private', 'Deployment addresses must be a config-name to URL map')
    values = values or {}
    public = {}
    for name, declaration in declarations.items():
        # The public schema checks the closed shape; keep URL validation semantic and independent.
        if 'url' in declaration:
            public[name] = base_url(declaration['url'], allow_http=allow_http)
    resolved, unresolved = {}, set()

    def resolve(value, *, server=False):
        name = _reference(value, declarations)
        if name is None:
            return value, ''
        if name in resolved:
            url = resolved[name]
        elif name in unresolved:
            url = 'https://' + name + STANDIN_SUFFIX
        else:
            declaration = declarations[name]
            if declaration['mode'] == 'fixed':
                url = public[name]
            else:
                key = declaration['config']
                if deployment is None or key not in values:
                    if require:
                        raise BlinkError('private_values_required', f'Address {name} requires an explicit deployment and its private addresses.{key} value; save it with private_set and stage with --deployment')
                    unresolved.add(name)
                    url = 'https://' + name + STANDIN_SUFFIX
                else:
                    url = base_url(values[key], allow_http=allow_http, allow_fqdn=True)
            if name not in unresolved:
                resolved[name] = url
        return _origin(url), urlsplit(url).path if server else ''

    root, root_prefix = resolve(doc['servers'][0]['url'], server=True)
    doc['servers'][0]['url'] = root
    for field in ('requestedOrigins', 'loginOrigins'):
        if field in service:
            result = [resolve(value)[0] for value in service[field]]
            service[field] = list(dict.fromkeys(result)) if declarations else result
    if 'appOrigin' in service:
        service['appOrigin'] = resolve(service['appOrigin'])[0]
    paths = {}
    for path, methods in doc['paths'].items():
        for method, operation in methods.items():
            prefix = root_prefix
            if operation.get('servers'):
                server, prefix = resolve(operation['servers'][0]['url'], server=True)
                operation['servers'][0]['url'] = server
            target = prefix + path
            if prefix and ('//' in target or any(x in ('.', '..') for x in path.split('/'))):
                raise BlinkError('invalid_manifest', 'An address-prefixed operation path must be unambiguous and cannot contain dot segments')
            if method in paths.get(target, {}):
                raise BlinkError('invalid_manifest', 'Deployment prefix expansion collides with another operation path and method')
            paths.setdefault(target, {})[method] = operation
    doc['paths'] = paths
    return {'addresses': resolved, 'deployment': deployment, 'unresolvedAddresses': sorted(unresolved)}


def snapshot_values(document, compiled):
    """Recover only approved config values for a checked parent's overlay, without reading private files."""
    result = {}
    for slot, declaration in document['x-blink-service'].get('addresses', {}).items():
        if declaration['mode'] == 'fixed' or slot not in compiled.get('addresses', {}):
            continue
        key, value = declaration['config'], compiled['addresses'][slot]
        if key in result and result[key] != value:
            raise BlinkError('unsafe_state', 'Approved address snapshot has conflicting local config values')
        result[key] = copy.deepcopy(value)
    return result
