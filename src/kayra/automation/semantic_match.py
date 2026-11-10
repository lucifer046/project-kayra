# ┌────────────────────────────────────────────────────────────────────────┐
# │                          semantic_match.py                             │
# │      "PR and outreach core team" -> "PR and Outreach Core Team 2026-27" │
# └────────────────────────────────────────────────────────────────────────┘
"""
Matches a loosely spoken name to one of a list of real names — WhatsApp chats and groups
first, and anything else with a name (tabs, files, buttons). BROWSER_CONTROL_PLAN H0.3.

The user asked for this NOT to be plain string matching. The score blends:

    words      order-free overlap of the spoken words with the name's words, allowing a stem
               ("outreach" / "outreaches"), a prefix ("dev" / "developers") or a misheard
               spelling ("rahool" / "rahul"); spoken numbers become digits first
    initials   "ptc" -> "Pune Tech Club"
    meaning    cosine of local embeddings (LM Studio's nomic-embed, when loaded) — "mom" ~
               "Maa", "the coding group" ~ "Dev Club 2026"
    aliases    names the user taught Kayra (exact; learning itself is plan item H2)
    recency    the chat with the latest message wins a tie (the user's rule); usage frequency
               a smaller nudge

and returns a DECISION, not just a ranking: "pick" (one clear winner), "ask" (two close —
the caller asks "PR and Outreach Core Team or PR Outreach 2025?"), or "none".

Names and the embeddings of names never leave this computer: embeddings come from the LOCAL
server only, are cached in memory only, and nothing is written to disk.
"""

import re
import math
import difflib
import threading

_STOP = frozenset("the a an my our of and to in on for with chat chats group groups conversation "
                  "contact please".split())


class Candidate:
    """A name to match against. `recent` = 0 for the most recent, 1 for the next…"""
    __slots__ = ("name", "recent", "uses", "data")

    def __init__(self, name, recent=None, uses=0, data=None):
        self.name, self.recent, self.uses, self.data = str(name or ""), recent, uses, data

    def __repr__(self):
        return f"<Candidate {self.name!r} recent={self.recent}>"


class Match:
    __slots__ = ("decision", "best", "ranked", "confidence")

    def __init__(self, decision, best, ranked, confidence):
        self.decision, self.best, self.ranked, self.confidence = decision, best, ranked, confidence

    def top(self, n=2):
        return [c for _s, c in self.ranked[:n]]

    def __repr__(self):
        return f"<Match {self.decision} {self.best!r} {self.confidence:.2f}>"


def _numbers(text):
    try:
        from kayra.automation.web_macros import spoken_numbers
        return spoken_numbers(text)
    except Exception:
        return str(text or "").lower()


_YEAR = re.compile(r"(?:19|20)\d\d")


def year_ranges(words):
    """['2026', '2027'] -> ['2026', '27']: a year range is one thing however it is said or
    written ("2026-27", "2026 2027", "2026 to 27"), so both sides are brought to one form."""
    out = []
    for word in words:
        if out and _YEAR.fullmatch(out[-1]) and _YEAR.fullmatch(word) and int(word) == int(out[-1]) + 1:
            out.append(word[2:])
        else:
            out.append(word)
    return out


def tokens(text, keep_stop=False):
    words = re.findall(r"[a-z0-9]+", _numbers(str(text or "").lower()))
    return year_ranges(words if keep_stop else [w for w in words if w not in _STOP])


def edition(query, name):
    """True when `name` is what was said plus only numbers: "graphics team" for "Graphics Team
    (2026-27)". Several such names are EDITIONS of one group — the latest one is meant."""
    q, n = tokens(query), tokens(name)
    said = [w for w in q if not w.isdigit()]
    if not said or said != [w for w in n if not w.isdigit()]:
        return False
    return all(w in n for w in q if w.isdigit())


def _word_hit(spoken, name_words):
    """How well one spoken word is present in the name: 1 exact, 0.9 stem/prefix, 0.8 near."""
    best = 0.0
    for word in name_words:
        if spoken == word:
            return 1.0
        if len(spoken) >= 3 and (word.startswith(spoken) or spoken.startswith(word) and len(word) >= 3):
            best = max(best, 0.9)
        elif len(spoken) >= 5 and len(word) >= 5 and spoken[:5] == word[:5]:
            best = max(best, 0.9)
        elif len(spoken) >= 4 and len(word) >= 4 and not spoken.isdigit() and not word.isdigit() and \
                difflib.SequenceMatcher(None, spoken, word).ratio() >= 0.72:
            best = max(best, 0.8)                         # "rahool" / "rahul" = 0.73
    return best


def lexical(query, name):
    """0..1: how much of what was said is in the name, lightly penalising a much longer name."""
    q, n = tokens(query), tokens(name)
    if not q or not n:
        return 0.0
    if " ".join(q) == " ".join(n):
        return 1.0
    hits = [_word_hit(w, n) for w in q]
    coverage = sum(hits) / len(q)
    if coverage == 0:
        initials = "".join(w[0] for w in tokens(name, keep_stop=True))
        if len(q) == 1 and 2 <= len(q[0]) <= 6 and q[0].isalpha() and initials.startswith(q[0]):
            return 0.75                                   # "ptc" -> Pune Tech Club
        return 0.0
    extra = max(0, len(n) - sum(1 for h in hits if h)) / max(len(n), 1)
    phrase = 0.1 if " ".join(q) in " ".join(n) else 0.0
    return min(1.0, coverage * (1.0 - 0.25 * extra) + phrase)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


PICK, ASK, NONE = "pick", "ask", "none"
PICK_AT = 0.55          # below this nothing is opened without asking
MARGIN = 0.08           # a winner must lead the runner-up by this much…
TIE = 0.02              # …unless it is a true tie, which the most recent chat wins


def match(query, candidates, embed=None, aliases=None):
    """
    The decision for `query` among `candidates` (Candidate or str). `embed(list[str]) ->
    list[vector] | None` adds meaning (local only); `aliases` maps a lowercase spoken alias to
    a name the user taught Kayra.
    """
    cands = [c if isinstance(c, Candidate) else Candidate(c, recent=i) for i, c in enumerate(candidates)]
    cands = [c for c in cands if c.name.strip()]
    if not cands or not str(query or "").strip():
        return Match(NONE, None, [], 0.0)
    spoken = " ".join(tokens(query, keep_stop=True))
    taught = (aliases or {}).get(spoken) or (aliases or {}).get(str(query).lower().strip())
    if taught:
        hit = next((c for c in cands if c.name == taught), None)
        if hit is not None:
            return Match(PICK, hit, [(1.0, hit)], 1.0)
    exact = [c for c in cands if " ".join(tokens(c.name)) == " ".join(tokens(query)) and tokens(query)]
    if len(exact) == 1:                                    # the full name, said as it is written
        return Match(PICK, exact[0], [(1.0, exact[0])], 1.0)
    if not exact:
        # The name without its year: "graphics team" for "Graphics Team (2026-27)". With several
        # editions the one with the latest message is meant (the user's rule), never a question.
        editions = sorted((c for c in cands if edition(query, c.name)),
                          key=lambda c: c.recent if c.recent is not None else 1e9)
        if editions:
            return Match(PICK, editions[0], [(0.95, c) for c in editions], 0.95)
    vectors = None
    if embed is not None:
        try:
            vectors = embed([query] + [c.name for c in cands])
        except Exception:
            vectors = None
    ranked = []
    for index, cand in enumerate(cands):
        score = lexical(query, cand.name)
        if vectors and len(vectors) == len(cands) + 1 and vectors[0] and vectors[index + 1]:
            meaning = max(0.0, (_cosine(vectors[0], vectors[index + 1]) - 0.5) / 0.5)   # 0.5..1 -> 0..1
            # Meaning breaks near-ties between names that share the spoken words, and on its own
            # can only lead to a QUESTION (at most 0.5 < PICK_AT): measured, a blend let it turn
            # an honest "which PR chat?" into a confident wrong pick.
            score = score + 0.05 * meaning if score else 0.5 * meaning
        recent = cand.recent if cand.recent is not None else len(cands)
        score += 0.01 / (1 + recent) + 0.005 * math.log1p(max(cand.uses, 0))
        ranked.append((score, cand))
    ranked.sort(key=lambda pair: (-pair[0], pair[1].recent if pair[1].recent is not None else 1e9))
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score >= PICK_AT and (best_score - second >= MARGIN or second < 0.4 or
                                  abs(best_score - second) <= TIE):
        decision = PICK          # a clear winner, or a true tie the most recent chat wins
    elif best_score >= 0.35:
        decision = ASK
    else:
        decision = NONE
    return Match(decision, best if decision != NONE else None, ranked, round(best_score, 3))


# ── Local embeddings (LM Studio), cached in memory ──
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 2000


def local_embedder(timeout=3.0):
    """An `embed` function backed by the LOCAL server's embedding model, or None when there is
    none. Never a cloud model: names stay on this computer."""
    try:
        from kayra.automation import ai
        base = ai._local_base()
        models = ai._http().get(base.rstrip("/") + "/models", timeout=timeout).json().get("data", [])
        model = next((m["id"] for m in models if "embed" in m.get("id", "").lower()), None)
    except Exception:
        return None
    if not model:
        return None
    prefix_q, prefix_d = ("search_query: ", "search_document: ") if "nomic" in model else ("", "")

    def embed(texts):
        wanted = [(prefix_q if i == 0 else prefix_d) + t for i, t in enumerate(texts)]
        with _CACHE_LOCK:
            missing = [t for t in wanted if t not in _CACHE]
        if missing:
            reply = ai._http().post(base.rstrip("/") + "/embeddings", timeout=timeout,
                                    json={"model": model, "input": missing}).json()
            vectors = [row["embedding"] for row in sorted(reply.get("data", []), key=lambda r: r.get("index", 0))]
            with _CACHE_LOCK:
                if len(_CACHE) > _CACHE_MAX:
                    _CACHE.clear()
                for text, vec in zip(missing, vectors):
                    _CACHE[text] = vec
        with _CACHE_LOCK:
            return [_CACHE.get(t) for t in wanted]

    return embed
