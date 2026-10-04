"""Pure Classroom roster to student matching module."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import permutations
from typing import Any

from submissions_checker.db.models.enums import ClassroomLinkMethod

# Thresholds
NAME_THRESHOLD = 0.85
MARGIN = 0.10
BULK_CONFIRM = 0.95

# Group prefix regex: e.g. "ІП-43", "ІА-з41"
_GROUP_RE = re.compile(r"^\s*[^\W\d_]{1,3}-?[зzЗZ]?\d{2}[^\W\d_]?\s+")

# Cyrillic to Latin transliteration mapping
_KMU = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "h",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "є": "ie",
    "ж": "zh",
    "з": "z",
    "и": "y",
    "і": "i",
    "ї": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ь": "",
    "ю": "iu",
    "я": "ia",
    "ъ": "",
    "ы": "y",
    "э": "e",
    "ё": "e",
}

# Word-initial transliteration rules
_KMU_START = {
    "є": "ye",
    "ї": "yi",
    "й": "y",
    "ю": "yu",
    "я": "ya",
}


@dataclass(frozen=True)
class Candidate:
    """A student candidate for matching."""

    student_id: int
    full_name: str
    email: str
    group: str | None


@dataclass(frozen=True)
class RosterEntry:
    """A Classroom roster entry."""

    user_id: str
    full_name: str
    email: str | None


@dataclass(frozen=True)
class MatchResult:
    """Result of matching a roster entry to a student."""

    method: ClassroomLinkMethod
    student_id: int | None
    score: float | None
    candidates: list[dict[str, Any]]  # top 3: {"student_id", "full_name", "score"}


def _translit(word: str) -> str:
    """Transliterate a Cyrillic word to Latin."""
    result = []
    for i, ch in enumerate(word):
        if i == 0 and ch in _KMU_START:
            result.append(_KMU_START[ch])
        else:
            result.append(_KMU.get(ch, ch))
    return "".join(result)


def name_tokens(name: str) -> list[str]:
    """Extract name tokens from a name string.

    - Strip group prefix (e.g., "ІП-43 ")
    - Lowercase
    - Remove apostrophes and similar characters
    - Transliterate Cyrillic
    - Split on non-word boundaries
    - Drop tokens containing digits
    """
    # Strip group prefix
    name = _GROUP_RE.sub("", name or "").lower()

    # Remove apostrophes and similar characters
    # Includes: ASCII apostrophe (U+0027), modifier apostrophe (U+02BC),
    # right single quotation (U+2019), and backtick (U+0060)
    apostrophes = chr(0x0027) + chr(0x02BC) + chr(0x2019) + "`"
    for ch in apostrophes:
        name = name.replace(ch, "")

    # Split on non-word boundaries and filter
    tokens = []
    for t in re.split(r"[^\w]+", name):
        if t and not any(c.isdigit() for c in t):
            tokens.append(_translit(t))

    return tokens


def email_tokens(email: str | None) -> list[str]:
    """Extract tokens from email local part.

    - Take part before '@'
    - Lowercase
    - Split on '.', '_', '-'
    - Drop tokens containing digits
    """
    local = (email or "").split("@")[0].lower()
    tokens = []
    for t in re.split(r"[._\-]", local):
        if t and not any(c.isdigit() for c in t):
            tokens.append(t)
    return tokens


def name_sim(a: list[str], b: list[str]) -> float:
    """Calculate name similarity between two token lists.

    - Take first 3 tokens of each
    - If either is empty, return 0.0
    - If either has 1 token, max similarity is halved (one token cannot be certain)
    - Otherwise, try all 2-permutations and find best pair match
    """
    a, b = a[:3], b[:3]
    if not a or not b:
        return 0.0

    if len(a) == 1 or len(b) == 1:
        # One token can never be "certain" — max 0.5
        return max(SequenceMatcher(None, x, y).ratio() for x in a for y in b) * 0.5

    best = 0.0
    for i, j in permutations(range(len(a)), 2):
        for k, m in permutations(range(len(b)), 2):
            s = (
                SequenceMatcher(None, a[i], b[k]).ratio()
                + SequenceMatcher(None, a[j], b[m]).ratio()
            ) / 2
            best = max(best, s)

    return best


def match(entry: RosterEntry, candidates: Sequence[Candidate]) -> MatchResult:
    """Match a roster entry to a student candidate.

    1. First try exact email match (case-insensitive)
    2. If no match, use name and email local part similarity
    3. Check if best score meets threshold and margin from second-best
    4. Return suggestions (top 3 candidates by score)
    """
    email = (entry.email or "").strip().lower()

    # Step 1: Try exact email match
    for c in candidates:
        if email and c.email.strip().lower() == email:
            return MatchResult(ClassroomLinkMethod.EMAIL, c.student_id, 1.0, [])

    # Step 2: Score all candidates using name and email tokens
    nt = name_tokens(entry.full_name)
    et = email_tokens(entry.email)

    scored = []
    for c in candidates:
        name_score = name_sim(nt, name_tokens(c.full_name))
        email_score = name_sim(et, name_tokens(c.full_name)) if len(et) >= 2 else 0.0
        combined_score = max(name_score, email_score)
        scored.append((combined_score, c))

    # Sort by score descending
    scored.sort(key=lambda x: -x[0])

    # Step 3: Check if no candidates
    if not scored:
        return MatchResult(ClassroomLinkMethod.NONE, None, None, [])

    # Prepare candidates list (top 3 with rounded scores)
    top = [
        {"student_id": c.student_id, "full_name": c.full_name, "score": round(s, 2)}
        for s, c in scored[:3]
    ]

    # Step 4: Check threshold and margin
    best = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0

    if best >= NAME_THRESHOLD and best - second >= MARGIN:
        return MatchResult(ClassroomLinkMethod.NAME, scored[0][1].student_id, round(best, 2), top)

    # Return best score but no match
    return MatchResult(ClassroomLinkMethod.NONE, None, round(best, 2), top)
