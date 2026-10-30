import os
import re
import threading
from typing import Any, Iterable, List, Mapping, Optional, Set

# A run of key-safe characters (letters, digits and -_. only, so never any
# whitespace or URL/path punctuation) at least this long looks like an api key.
MIN_KEY_LENGTH = 24

# Values picked up because their own name looks like a credential are taken in
# full (they may contain punctuation the token regex can't span), but only when
# at least this long, so trivial values ("", "0", "true") can't blank out
# ordinary words in the conversation.
MIN_VALUE_LENGTH = 8

REDACTED = "<redacted>"

_KEY_LIKE_RE = re.compile(r"[A-Za-z0-9\-_.]{24,}")

# A value that is nothing but an unexpanded reference into the environment.
# Those are not secrets themselves, so they must never be registered.
_ENV_REF_RE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")

# Name words that mark a value as a credential, and take precedence over
# BENIGN_NAME_WORDS. "pwd" is deliberately absent: PWD holds the working dir,
# the rest are matched as substrings instead (see SECRET_NAME_SUBSTRINGS).
SECRET_NAME_WORDS = {
    "apikey",
    "credential",
    "credentials",
    "key",
    "keys",
    "passwd",
    "password",
    "secret",
    "secrets",
    "token",
    "tokens",
}

# Fragments that mark a name as credential-named wherever they appear in it, so
# names whose credential word is not separated by punctuation ("MYTOKEN").
SECRET_NAME_SUBSTRINGS = ("token", "passwd", "password")

# Name words that mark a value as an identifier (paths, models, session ids,
# hostnames). These are never scanned for key-like tokens: config/env entries
# named this way legitimately hold long strings that are not secrets, e.g.
# "claude-3-5-sonnet-20241022" or "debuginfod.fedoraproject.org".
BENIGN_NAME_WORDS = {
    "base",
    "bin",
    "branch",
    "cwd",
    "dir",
    "dirs",
    "directory",
    "domain",
    "editor",
    "encoding",
    "exe",
    "file",
    "files",
    "format",
    "guid",
    "home",
    "host",
    "hosts",
    "hostname",
    "id",
    "lang",
    "language",
    "locale",
    "model",
    "models",
    "name",
    "pager",
    "path",
    "paths",
    "pid",
    "profile",
    "release",
    "repo",
    "root",
    "session",
    "shell",
    "term",
    "terminal",
    "theme",
    "tz",
    "url",
    "urls",
    "user",
    "uuid",
    "version",
}

_api_keys: List[str] = []
_matcher: Optional[re.Pattern] = None
_lock = threading.RLock()


def detect_api_keys(
    merged_config: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Collect api-key-like strings from a merged config and the environment.

    Every value is classified by the name it is stored under:

      - credential-named (contains "key", "token", "password", "secret", ...):
        the whole value is taken as a candidate, since such values may contain
        punctuation that a key-like token regex cannot span.
      - identifier-named (see BENIGN_NAME_WORDS): skipped, because these
        legitimately hold long non-secret strings.
      - anything else: scanned for key-like tokens, i.e. runs of
        ``MIN_KEY_LENGTH``+ characters of ``[A-Za-z0-9-_.]``.

    Config values additionally have ``$VAR``/``${VAR}`` expanded against
    ``environ`` first, since config files usually reference keys that live in
    the environment.

    Args:
        merged_config: The fully merged config dict (see
            config_utils.read_and_merge_all_configs).
        environ: Environment mapping to read, defaults to os.environ.

    Returns:
        Sorted, de-duplicated list of candidate secret strings.
    """
    if environ is None:
        environ = os.environ

    found: Set[str] = set()

    _scan_entries(environ, found, expand=False)
    _scan_entries(merged_config, found, expand=True)

    return sorted(found)


def register_api_keys(keys: Iterable[str]) -> List[str]:
    """Add candidate secrets to the process-wide redaction list.

    Registration is additive, so later calls (an oauth flow, a config reload)
    only widen the set. Values shorter than ``MIN_VALUE_LENGTH`` are ignored
    for the same reason as in detect_api_keys.

    Returns:
        The full registered list, longest first.
    """
    global _api_keys, _matcher

    with _lock:
        merged = set(_api_keys)

        for key in keys:
            if isinstance(key, str) and len(key) >= MIN_VALUE_LENGTH:
                merged.add(key)

        # Longest first so a key nested inside a longer one still wins.
        _api_keys = sorted(merged, key=lambda item: (-len(item), item))
        _matcher = None

        return list(_api_keys)


def reset_api_keys() -> None:
    """Drop all registered secrets (used by tests and config reloads)."""
    global _api_keys, _matcher

    with _lock:
        _api_keys = []
        _matcher = None


def get_api_keys() -> List[str]:
    """Return the registered secrets, longest first."""
    with _lock:
        return list(_api_keys)


def redact_api_keys(value: Any) -> Any:
    """Replace every registered secret in ``value`` with ``<redacted>``.

    Walks dicts, lists and tuples so nested content blocks and tool-call
    arguments are covered. Returns the input untouched when nothing is
    registered, and only rebuilds a container whose contents actually changed.
    """
    if not _api_keys:
        return value

    matcher = _get_matcher()

    if isinstance(value, str):
        return matcher.sub(REDACTED, value)

    if isinstance(value, Mapping):
        return _redact_mapping(value)
        return {key: redact_api_keys(item) for key, item in value.items()}

    if isinstance(value, list):
        return _redact_seq(value, list)

    if isinstance(value, tuple):
        return _redact_seq(value, tuple)

    return value


def _scan_entries(mapping: Any, found: Set[str], expand: bool) -> None:
    """Classify and scan every entry of a nested config/env mapping."""
    if not isinstance(mapping, Mapping):
        return

    for name, value in mapping.items():
        _scan_entry(name, value, found, expand, forced=False)


def _scan_entry(name: Any, value: Any, found: Set[str], expand: bool, forced: bool) -> None:
    """Scan one value under ``name``, recursing into containers.

    ``forced`` marks values inherited from a credential-named parent, so a
    mapping under e.g. ``api-keys:`` is still read as secrets.
    """
    if isinstance(value, Mapping):
        credential_named = forced or _is_secret_name(name)

        for sub_name, sub_value in value.items():
            _scan_entry(sub_name, sub_value, found, expand, credential_named)

        return

    if isinstance(value, (list, tuple, set)):
        for item in value:
            _scan_entry(name, item, found, expand, forced)

        return

    if not isinstance(value, str) or not value:
        return

    text = os.path.expandvars(value) if expand else value

    if forced or _is_secret_name(name):
        _register_secret_value(text, found)
        return

    if _is_benign_name(name):
        return

    found.update(_KEY_LIKE_RE.findall(text))


def _register_secret_value(value: str, found: Set[str]) -> None:
    """Take a credential-named value in full, plus any key-like tokens in it."""
    stripped = value.strip()

    if len(stripped) < MIN_VALUE_LENGTH or _ENV_REF_RE.match(stripped):
        return

    found.add(stripped)
    found.update(_KEY_LIKE_RE.findall(stripped))


def _is_secret_name(name: Any) -> bool:
    """True if the name itself says this value is a credential."""
    lowered = str(name).lower()

    if "key" in lowered or any(word in lowered for word in SECRET_NAME_SUBSTRINGS):
        return True

    return bool(_name_words(name) & SECRET_NAME_WORDS)


def _is_benign_name(name: Any) -> bool:
    """True if the name says this value is an identifier, not a secret."""
    return bool(_name_words(name) & BENIGN_NAME_WORDS)


def _name_words(name: Any) -> Set[str]:
    """Split ``name`` into lowercased words, handling -/. and camelCase."""
    words = set()

    for chunk in re.split(r"[^A-Za-z0-9]+", str(name)):
        if not chunk:
            continue

        words.add(chunk.lower())
        words.update(w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", chunk))

    return words


def _redact_mapping(mapping: Mapping) -> Mapping:
    """Apply redaction to every value, keeping the mapping when unchanged."""
    items = {}
    changed = False

    for key, item in mapping.items():
        new_item = redact_api_keys(item)
        changed = changed or new_item is not item
        items[key] = new_item

    return items if changed else mapping


def _redact_seq(seq: list, ctor) -> Any:
    """Apply ``redact_api_keys`` to ``seq``, keeping it when nothing changed."""
    items = []
    changed = False

    for item in seq:
        new_item = redact_api_keys(item)
        changed = changed or new_item is not item
        items.append(new_item)

    return ctor(items) if changed else seq


def _get_matcher() -> re.Pattern:
    """Build (and cache) the alternation matcher for the registered secrets."""
    global _matcher

    with _lock:
        if _matcher is None:
            pattern = "|".join(re.escape(key) for key in _api_keys)
            _matcher = re.compile(pattern)

        return _matcher
