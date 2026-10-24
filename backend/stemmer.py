"""Porter-style English stemmer used for lexical passage retrieval.

The previous inline suffix stripper produced inconsistent roots (for example
"escalate" and "escalation" stemmed differently), which silently dropped
relevant passages from retrieval results. This module implements the Porter
snowball algorithm and a small domain normalisation table so that equivalent
word forms collapse onto one root.
"""

from __future__ import annotations

VOWELS = "aeiou"

# Words that the Porter algorithm leaves on slightly different roots but that
# mean the same thing in this domain.
DOMAIN_ROOTS = {
    "cit": "cite",
    "cite": "cite",
    "document": "docu",
    "mainten": "maintain",
    "safeti": "safe",
    "verif": "verifi",
}


def _is_consonant(word: str, index: int) -> bool:
    letter = word[index]
    if letter in VOWELS:
        return False
    if letter == "y":
        return index == 0 or not _is_consonant(word, index - 1)
    return True


def _measure(stem: str) -> int:
    """Return the Porter ``m`` value: the number of vowel-consonant sequences."""
    count = 0
    index = 0
    size = len(stem)
    while index < size and _is_consonant(stem, index):
        index += 1
    while index < size:
        while index < size and not _is_consonant(stem, index):
            index += 1
        if index >= size:
            break
        count += 1
        while index < size and _is_consonant(stem, index):
            index += 1
    return count


def _contains_vowel(stem: str) -> bool:
    return any(not _is_consonant(stem, index) for index in range(len(stem)))


def _double_consonant(word: str) -> bool:
    return (
        len(word) >= 2
        and word[-1] == word[-2]
        and _is_consonant(word, len(word) - 1)
    )


def _cvc(word: str) -> bool:
    if len(word) < 3:
        return False
    if not (
        _is_consonant(word, len(word) - 3)
        and not _is_consonant(word, len(word) - 2)
        and _is_consonant(word, len(word) - 1)
    ):
        return False
    return word[-1] not in "wxy"


def _porter(word: str) -> str:
    stem = word
    if len(stem) <= 2:
        return stem

    # Step 1a - plurals.
    if stem.endswith("sses"):
        stem = stem[:-2]
    elif stem.endswith("ies"):
        stem = stem[:-2]
    elif stem.endswith("ss"):
        pass
    elif stem.endswith("s"):
        stem = stem[:-1]

    # Step 1b - past participles and progressive verbs.
    step_one_b = False
    if stem.endswith("eed"):
        if _measure(stem[:-3]) > 0:
            stem = stem[:-1]
    elif stem.endswith("ed") and _contains_vowel(stem[:-2]):
        stem = stem[:-2]
        step_one_b = True
    elif stem.endswith("ing") and _contains_vowel(stem[:-3]):
        stem = stem[:-3]
        step_one_b = True
    if step_one_b:
        if stem.endswith(("at", "bl", "iz")):
            stem += "e"
        elif _double_consonant(stem) and not stem.endswith(("l", "s", "z")):
            stem = stem[:-1]
        elif _measure(stem) == 1 and _cvc(stem):
            stem += "e"

    # Step 1c - terminal y.
    if stem.endswith("y") and _contains_vowel(stem[:-1]):
        stem = stem[:-1] + "i"

    # Step 2 - derivational suffixes.
    step_two = (
        ("ational", "ate"), ("tional", "tion"), ("enci", "ence"), ("anci", "ance"),
        ("izer", "ize"), ("abli", "able"), ("alli", "al"), ("entli", "ent"),
        ("eli", "e"), ("ousli", "ous"), ("ization", "ize"), ("ation", "ate"),
        ("ator", "ate"), ("alism", "al"), ("iveness", "ive"), ("fulness", "ful"),
        ("ousness", "ous"), ("aliti", "al"), ("iviti", "ive"), ("biliti", "ble"),
        ("logi", "log"),
    )
    for suffix, replacement in step_two:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)] + replacement
            break

    # Step 3 - further derivational suffixes.
    step_three = (
        ("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"),
        ("ical", "ic"), ("ful", ""), ("ness", ""),
    )
    for suffix, replacement in step_three:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)] + replacement
            break

    # Step 4 - stripping large suffixes.
    step_four = (
        "al", "ance", "ence", "er", "ic", "able", "ible", "ant", "ement",
        "ment", "ent", "ou", "ism", "ate", "iti", "ous", "ive", "ize",
    )
    for suffix in step_four:
        if stem.endswith(suffix):
            if suffix == "ion":
                if stem[:-3].endswith(("s", "t")):
                    stem = stem[:-3]
                break
            stem = stem[: -len(suffix)]
            break

    # Step 5a - terminal e.
    if stem.endswith("e"):
        measure = _measure(stem[:-1])
        if measure > 1 or (measure == 1 and not _cvc(stem[:-1])):
            stem = stem[:-1]

    # Step 5b - terminal l.
    if stem.endswith("l") and _double_consonant(stem) and _measure(stem) > 1:
        stem = stem[:-1]
    return stem


def stem(word: str) -> str:
    """Return a stable root for ``word``."""
    if len(word) <= 2:
        return word
    rooted = _porter(word)
    return DOMAIN_ROOTS.get(rooted, rooted)
