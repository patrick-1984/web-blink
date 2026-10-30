"""Schema editions and the reader compatibility rule.

Every bundled schema carries ``x-blink-schema: {"version": "MAJOR.MINOR.PATCH", "released": "YYYY-MM-DD"}``
(also listed in ``schemas/index.json``). A document may declare the edition it was written for with the same
key. Newer readers accept older documents; a higher MINOR (additive fields only) is accepted; a different
MAJOR is refused with a clear message, because removing or renaming a field requires a new MAJOR.
"""
import re

SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
DATE = re.compile(r'[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])')
URN_MAJOR = re.compile(r'urn:web-blink:schema:[a-z-]+:([0-9]+)')


def parse(version):
    """'1.2.3' -> (1, 2, 3); anything else -> None."""
    match = SEMVER.fullmatch(version) if isinstance(version, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def edition(schema):
    """The (version, released) a schema document declares about itself."""
    meta = schema.get('x-blink-schema') if isinstance(schema, dict) else None
    if not isinstance(meta, dict) or parse(meta.get('version')) is None or not (
            isinstance(meta.get('released'), str) and DATE.fullmatch(meta['released'])):
        raise ValueError('Schema document lacks a valid x-blink-schema version and release date')
    return meta['version'], meta['released']


def bump_minor(version):
    major, minor, _ = parse(version) or (0, 0, 0)
    return f'{major}.{minor + 1}.0'


def compatibility_problem(document, schema, label='document'):
    """None when this reader may read the document; otherwise a human-readable refusal.

    Looks at every way a document can name its edition: the declared ``x-blink-schema.version``,
    an integer ``schemaVersion`` (MAJOR) and a ``$schema`` URN suffix (MAJOR).
    """
    if not isinstance(document, dict):
        return None
    reader, _ = edition(schema)
    major = parse(reader)[0]
    declared = []
    meta = document.get('x-blink-schema')
    if meta is not None:
        version = meta.get('version') if isinstance(meta, dict) else None
        if parse(version) is None:
            return f'{label} has an invalid x-blink-schema.version; use MAJOR.MINOR.PATCH, e.g. "{reader}"'
        declared.append((parse(version)[0], version))
    number = document.get('schemaVersion')
    if isinstance(number, int) and not isinstance(number, bool):
        declared.append((number, f'schemaVersion {number}'))
    urn = URN_MAJOR.fullmatch(document.get('$schema')) if isinstance(document.get('$schema'), str) else None
    if urn:
        declared.append((int(urn.group(1)), document['$schema']))
    for found, text in declared:
        if found != major:
            newer = 'newer' if found > major else 'older, retired'
            return (f'{label} declares schema {text} (MAJOR {found}), a {newer} edition; this Web Blink reads '
                    f'MAJOR {major} (up to {reader}). A different MAJOR edition is a breaking change: '
                    f'{"update Web Blink" if found > major else "convert the document"} to read it.')
    return None
