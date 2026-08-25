"""Bound and redact Git transport diagnostics before anyone records them.

A remote chooses its own error strings, so these bytes are attacker-influenced.
Recorded as they arrive they can carry terminal control sequences into a log
viewer, grow without limit, decode to nothing, or repeat back material that came
from *this* side: a URL with credentials in its userinfo, a token echoed in a
message, a prompt naming a helper.

Stability is the other half.  If the same failure renders differently from run
to run, nothing downstream can compare two diagnostics, deduplicate them, or
assert on them, and an operator reading two reports cannot tell whether the
underlying condition changed.  So there is no clock, no environment, no
randomness and no ordering dependence here: identical bytes give identical text.

**Redaction here is the second line of defence, and it cannot be the first.**
Matching credential shapes means matching a pattern set someone has to keep
current, and such a set is never complete -- a format nobody anticipated passes
straight through.  Presenting this as *making output safe* would be a promise
this module cannot keep, and a caller who believed it would stop maintaining the
control that actually works.

That control is
:data:`~council_tools.git_process_contract.BASE_CHILD_ENVIRONMENT`, which builds
the child environment from scratch and sets ``GIT_TERMINAL_PROMPT=0``,
``GIT_ASKPASS=""`` and ``GCM_INTERACTIVE=never`` so a credential is never
available to the child to leak.  This module reduces the damage when something
gets through anyway; it does not license relaxing that policy.

Every redaction **replaces** rather than deletes, so a reader can see that
something was withheld instead of reading a sentence with a hole in it.
"""

from __future__ import annotations

import re

#: Bounds.  Chosen to keep one failure readable in a log line budget while
#: leaving no way for a remote to decide how much it writes into a record.
MAX_LINES = 40
MAX_LINE_LENGTH = 512
MAX_TOTAL_LENGTH = 8_192

#: Truncation is always visible: a silently shortened diagnostic reads as a
#: complete one, which is how a reader concludes there was nothing more to see.
LINE_TRUNCATION_MARKER = "[truncated]"
LINES_OMITTED_MARKER = "[{count} further line(s) omitted]"
TOTAL_TRUNCATION_MARKER = "[output truncated]"
REDACTION_MARKER = "[redacted]"

#: The line separator is the one control character kept, because the output is
#: line-structured.  Everything else -- including carriage return, which Git's
#: progress output uses to rewrite a line -- is removed.
_LINE_SEPARATOR = "\n"

#: Userinfo in a URL: the ``user:secret@`` between scheme and host.  This is the
#: shape most likely to carry a real credential, because it is written by the
#: side that holds one rather than by the remote.
#: The scheme repetition is **bounded**, and that bound is the difference
#: between linear and quadratic time on attacker-controlled input.  With ``*``
#: the engine consumes the whole line at every start position looking for
#: ``://`` and backtracks all the way out again: measured at 8.8s for a single
#: 160,000-character line, growing with the square, so a remote could spend
#: minutes of CPU by sending one long alphanumeric line to the module whose job
#: is to make its output safe.  No real URL scheme is anywhere near this long.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31}://)[^/\s@]*@")

#: Recognised credential shapes.  This list is deliberately presented as
#: incomplete; see the module docstring.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub tokens: classic, fine-grained, and the OAuth/app/refresh variants.
    # Deliberately NOT anchored on a word boundary.  A leading ``\b`` fails when
    # the token is concatenated to other word characters -- "...abcghp_XXXX" has
    # no boundary before the ``g`` -- and a credential run together with
    # surrounding text is exactly the case where a reader would not spot it.
    # These prefixes are distinctive enough not to need one, and over-redacting
    # is the safe direction here.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    # An Authorization header value.  Consumes the rest of the LINE, not the
    # next whitespace-delimited token: "Authorization: Bearer <token>" would
    # otherwise redact the word "Bearer" and leave the token beside it.  Over-
    # redacting a header is safe because the whole value is the credential.
    re.compile(r"(?i)\bauthorization\s*:.*"),
    re.compile(r"(?i)\b(?:bearer|basic|token)\s+[A-Za-z0-9+/=._\-]{8,}"),
    # AWS access key ids and generic long secrets in an assignment.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"
    ),
    # A PEM private key header gives away the whole block that follows.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _strip_control_characters(text: str) -> str:
    """Keep the line separator; drop every other control character.

    Written as a keep-list rather than a drop-list: a drop-list would have to
    enumerate every control character, and the ones it missed are exactly the
    ones an attacker would reach for.
    """

    return "".join(
        character
        for character in text
        if character == _LINE_SEPARATOR or not _is_control(character)
    )


def _is_control(character: str) -> bool:
    code = ord(character)
    # C0, DEL, and C1.  Unicode category Cc, spelled out so the rule is visible.
    return code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F


def _redact(text: str) -> str:
    text = _URL_USERINFO.sub(lambda match: match.group("scheme") + REDACTION_MARKER + "@", text)
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(REDACTION_MARKER, text)
    return text


def redact_transport_diagnostics(raw: bytes) -> str:
    """Return bounded, stable text for ``raw``.

    Never raises on content: an arbitrary byte sequence from a remote must
    produce a diagnostic, not an error that replaces one.
    """

    if type(raw) is not bytes:
        raise TypeError("transport diagnostics must be bytes")

    # ``replace`` rather than ``strict``: undecodable bytes are a thing a remote
    # can send, and failing on them would let it suppress its own diagnostic.
    text = raw.decode("utf-8", errors="replace")
    text = _strip_control_characters(text)

    lines = text.split(_LINE_SEPARATOR)
    # A trailing separator produces one empty final element that is not a line.
    if lines and lines[-1] == "":
        lines = lines[:-1]

    # The line-count bound is applied BEFORE redaction, so the pattern set never
    # runs over text that is going to be discarded anyway.  It drops whole
    # lines, so it cannot cut a credential in half and leave a prefix behind.
    omitted = 0
    if len(lines) > MAX_LINES:
        omitted = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES]

    # Redaction runs BEFORE per-line truncation, so a credential in the part of
    # a line that survives is replaced rather than merely cut short.
    lines = [_redact(line) for line in lines]

    bounded = []
    for line in lines:
        if len(line) > MAX_LINE_LENGTH:
            keep = MAX_LINE_LENGTH - len(LINE_TRUNCATION_MARKER)
            line = line[:keep] + LINE_TRUNCATION_MARKER
        bounded.append(line)
    if omitted:
        bounded.append(LINES_OMITTED_MARKER.format(count=omitted))

    result = _LINE_SEPARATOR.join(bounded)
    if len(result) > MAX_TOTAL_LENGTH:
        keep = MAX_TOTAL_LENGTH - len(TOTAL_TRUNCATION_MARKER)
        result = result[:keep] + TOTAL_TRUNCATION_MARKER
    return result


__all__ = [
    "LINES_OMITTED_MARKER",
    "LINE_TRUNCATION_MARKER",
    "MAX_LINES",
    "MAX_LINE_LENGTH",
    "MAX_TOTAL_LENGTH",
    "REDACTION_MARKER",
    "TOTAL_TRUNCATION_MARKER",
    "redact_transport_diagnostics",
]
