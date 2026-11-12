# ┌────────────────────────────────────────────────────────────────────────┐
# │                      automation/form_profile.py                        │
# │        Your own details, for "fill this form with my details"          │
# └────────────────────────────────────────────────────────────────────────┘
"""
The user's form profile (BROWSER_CONTROL_PLAN Part 6, D3; decision D2 answered 2026-11-11:
name, email, phone, postal address, college and roll number, date of birth and gender).

Rules:
  * There is NO key for a password, a card, a CVV, an OTP, a PIN or a government id, and
    `set_value` refuses a value that looks like a card number. The page side refuses those
    fields too (`pageForm` in the extension).
  * Stored in `data/profile.json`, mode 0600, written atomically. Read and matched on this
    computer only: a value is never sent to a model, never logged (the action carries it as
    the redacted `text` parameter), and never kept in a pending confirmation — the approved fill
    names field numbers and KEYS; the values are read again when the user says yes.
  * Matching a form field to a key is local and deterministic: the field's `autocomplete`
    token first, then its label words. A field about SOMEONE ELSE (father's name, emergency
    contact, nominee, company) is never filled.
  * The profile is filled in by voice ("remember my phone number is …"); nobody edits a file.
"""

import os
import re
import json
import datetime

from kayra.core.paths import data_path

PROFILE_FILE = "profile.json"

# key -> (what Kayra calls it, the words a person uses for it when setting it)
FIELDS = {
    "name": ("name", ("name", "full name")),
    "first_name": ("first name", ("first name", "given name")),
    "last_name": ("last name", ("last name", "surname", "family name")),
    "email": ("email", ("email", "email address", "e mail", "mail id", "email id")),
    "phone": ("phone number", ("phone", "phone number", "mobile", "mobile number", "number",
                               "contact number", "whatsapp number", "cell number")),
    "address": ("address", ("address", "street address", "home address", "postal address")),
    "address2": ("second address line", ("address line 2", "second address line", "landmark",
                                         "apartment")),
    "city": ("city", ("city", "town")),
    "state": ("state", ("state",)),
    "pin": ("PIN code", ("pin", "pin code", "pincode", "postal code", "zip", "zip code")),
    "country": ("country", ("country",)),
    "college": ("college", ("college", "institute", "university", "institution", "college name")),
    "course": ("course", ("course", "programme", "program", "branch", "degree", "department")),
    "roll_number": ("roll number", ("roll number", "roll no", "roll", "student id",
                                    "enrollment number", "enrolment number", "registration number")),
    "dob": ("date of birth", ("date of birth", "dob", "birthday", "birth date")),
    "gender": ("gender", ("gender", "sex")),
}

# autocomplete="…" tokens (HTML standard) -> key
_AUTOCOMPLETE = {"name": "name", "given-name": "first_name", "family-name": "last_name",
                 "email": "email", "tel": "phone", "tel-national": "phone", "mobile": "phone",
                 "street-address": "address", "address-line1": "address", "address-line2": "address2",
                 "address-level2": "city", "address-level1": "state", "postal-code": "pin",
                 "country": "country", "country-name": "country", "bday": "dob", "sex": "gender"}

# A field about another person or an account, never filled with the user's own details.
_SOMEONE_ELSE = re.compile(
    r"\b(father|mother|parent|guardian|spouse|husband|wife|nominee|emergency|reference|referee|"
    r"referr\w*|company|organi[sz]ation|employer|user ?name|nick ?name|team|project|card ?holder|"
    r"sibling|brother|sister|friend|manager|supervisor|recipient|receiver|beneficiary)\b")

# label words -> key, in order: the specific shapes before the general ones.
_LABELS = (
    ("first_name", r"\bfirst name\b|\bgiven name\b|\bfname\b|\bforename\b|\bfirstname\b"),
    ("last_name", r"\blast name\b|\bsurname\b|\bfamily name\b|\blname\b|\blastname\b"),
    ("email", r"\be ?mail\b|\bemail ?id\b"),
    ("dob", r"\bdate of birth\b|\bdob\b|\bbirth ?date\b|\bbirthday\b|\bborn on\b"),
    ("phone", r"\bphone\b|\bmobile\b|\bcontact (?:number|no)\b|\bwhatsapp (?:number|no)\b|\btelephone\b|"
              r"\bcell(?: number)?\b|\btel\b"),
    ("gender", r"\bgender\b|\bsex\b"),
    ("roll_number", r"\broll\b|\benrol+ment\b|\bstudent (?:id|number|no)\b|\bregistration (?:number|no)\b|"
                    r"\breg no\b|\badmission (?:number|no)\b"),
    ("pin", r"\bpin ?code\b|\bpincode\b|\bzip\b|\bpostal code\b|\bpost ?code\b"),
    ("address2", r"\baddress (?:line )?2\b|\bapartment\b|\bsuite\b|\blandmark\b"),
    ("address", r"\baddress\b|\bstreet\b"),
    ("city", r"\bcity\b|\btown\b|\bdistrict\b"),
    ("state", r"\bstate\b|\bprovince\b"),
    ("country", r"\bcountry\b"),
    ("college", r"\bcollege\b|\binstitute\b|\binstitution\b|\buniversity\b|\bschool\b"),
    ("course", r"\bcourse\b|\bprogram(?:me)?\b|\bbranch\b|\bdegree\b|\bdepartment\b|\bmajor\b|\bstream\b"),
    ("name", r"\bfull name\b|\byour name\b|\bname\b"),
)

_CARD = re.compile(r"(?:\d[ -]?){13,19}")
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_ORDINAL_WORDS = {w: i for i, w in enumerate((
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
    "eighteenth", "nineteenth", "twentieth"), 1)}


def _path():
    return data_path(PROFILE_FILE)


def load():
    """The saved profile ({key: value}), {} when there is none or it cannot be read."""
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in data.items() if k in FIELDS and str(v).strip()} if isinstance(data, dict) else {}


def save(profile):
    """Atomic write, 0600 (it holds the user's own address and phone number)."""
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({k: profile[k] for k in FIELDS if profile.get(k)}, handle, indent=1, ensure_ascii=False)
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def spoken(key):
    return FIELDS[key][0] if key in FIELDS else key.replace("_", " ")


def key_for(words):
    """'phone number' -> 'phone', 'roll no' -> 'roll_number'; None when it is not a profile field."""
    w = " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(words or "").lower()).split())
    w = re.sub(r"^(?:my|the)\s+", "", w)
    for key, (_, said) in FIELDS.items():
        if w in said:
            return key
    return None


def parse_date(text):
    """'4 may 2003', 'may 4th 2003', '2003-05-04', '04/05/2003' (day first) -> '2003-05-04', or ''."""
    from kayra.automation.web_macros import spoken_numbers
    t = " ".join(str(text or "").lower().replace(",", " ").split())
    t = re.sub(r"\b(twenty|thirty)[ -](first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b",
               lambda m: str({"twenty": 20, "thirty": 30}[m.group(1)] + _ORDINAL_WORDS[m.group(2)]), t)
    t = re.sub(r"\b(" + "|".join(_ORDINAL_WORDS) + r")\b", lambda m: str(_ORDINAL_WORDS[m.group(1)]), t)
    t = re.sub(r"\bthirtieth\b", "30", t)
    t = spoken_numbers(t)
    t = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", t).replace(" of ", " ")
    t = re.sub(r"^(?:the|on) ", "", t).strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4})", t)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.fullmatch(r"(\d{1,2}) ([a-z]+) (\d{4})", t) or re.fullmatch(r"([a-z]+) (\d{1,2}) (\d{4})", t)
            if not m:
                return ""
            a, b, y = m.group(1), m.group(2), int(m.group(3))
            d, month = (int(a), b) if a.isdigit() else (int(b), a)
            mo = _MONTHS.get(month)
            if not mo:
                return ""
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return ""


def normalize(key, value):
    """The value as stored, or '' when it is not a valid value for that key."""
    raw = " ".join(str(value or "").split()).strip(" .")
    if not raw or key not in FIELDS:
        return ""
    if key == "email":
        from kayra.automation.common import spoken_email
        return spoken_email(raw)
    if key in ("phone", "pin"):
        from kayra.automation.web_macros import spoken_numbers
        digits = re.sub(r"[^\d+]", "", spoken_numbers(raw).replace("plus ", "+"))
        need = (7, 15) if key == "phone" else (4, 10)
        return digits if need[0] <= len(digits.lstrip("+")) <= need[1] else ""
    if key == "dob":
        return parse_date(raw)
    if key == "gender":
        low = raw.lower()
        return {"male": "Male", "man": "Male", "m": "Male", "female": "Female", "woman": "Female",
                "f": "Female"}.get(low, raw[:1].upper() + raw[1:])
    if _CARD.search(raw):
        return ""                       # never store anything shaped like a card number
    return raw[:200]


def set_value(key, value):
    """Stores one value; returns (ok, sentence)."""
    stored = normalize(key, value)
    if not stored:
        return False, f"That doesn't look like a {spoken(key)}, so I didn't save it."
    profile = load()
    profile[key] = stored
    if key == "name" and " " in stored:
        first, last = stored.split(" ", 1)
        profile["first_name"], profile["last_name"] = first, last
    save(profile)
    return True, f"Saved your {spoken(key)} for forms."


def forget(key=None):
    """Removes one key, or everything when key is None. Returns how many values went."""
    profile = load()
    if key is None:
        count = len(profile)
        try:
            os.remove(_path())
        except OSError:
            pass
        return count
    if key not in profile:
        return 0
    profile.pop(key)
    save(profile)
    return 1


def _norm(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def key_of_field(field):
    """The profile key a listed form field asks for, or None."""
    if field.get("sensitive") or field.get("type") in ("checkbox", "file", "password"):
        return None
    hints = _norm(field.get("hints") or field.get("label"))
    if _SOMEONE_ELSE.search(hints):
        return None
    tokens = str(field.get("autocomplete") or "").lower().split()
    for token in reversed(tokens):
        if token in _AUTOCOMPLETE:
            return _AUTOCOMPLETE[token]
    if field.get("type") == "email":
        return "email"
    if field.get("type") == "tel":
        return "phone"
    if field.get("type") == "date" and re.search(r"\bbirth|\bdob\b", hints):
        return "dob"
    label = _norm(field.get("label")) or hints
    for text in (label, hints):
        for key, pattern in _LABELS:
            if re.search(pattern, text):
                return key
    return None


def _complete(profile):
    """Fills the derived names both ways: first + last from a full name and back."""
    p = dict(profile)
    if p.get("name") and " " in p["name"]:
        first, last = p["name"].split(" ", 1)
        p.setdefault("first_name", first)
        p.setdefault("last_name", last)
    if not p.get("name") and p.get("first_name"):
        p["name"] = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
    return p


def value_for(field, key, profile):
    """What goes into that field: the date in the field's own format, the rest as stored."""
    value = _complete(profile).get(key, "")
    if not value:
        return ""
    if key == "dob":
        try:
            day = datetime.date.fromisoformat(value)
        except ValueError:
            return ""
        if field.get("type") == "date":
            return day.isoformat()
        hints = str(field.get("hints") or "").lower()
        if "mm/dd" in hints or "mm-dd" in hints:
            return day.strftime("%m/%d/%Y")
        if "yyyy-mm-dd" in hints or "yyyy/mm/dd" in hints:
            return day.isoformat()
        return day.strftime("%d/%m/%Y")
    if key == "phone" and value.startswith("+") and re.search(r"\b10 digit|without (?:the )?country code",
                                                             str(field.get("hints") or "").lower()):
        return value[-10:]
    return value


def plan(fields, profile):
    """[(field, key, value)] for every EMPTY field the profile can fill, in page order; plus the
    keys the page asks for that the profile does not have."""
    full = _complete(profile)
    todo, missing, used = [], [], set()
    for field in fields:
        key = key_of_field(field)
        if not key:
            continue
        if field.get("filled"):
            used.add(key)
            continue
        value = value_for(field, key, full)
        if value:
            todo.append((field, key, value))
        elif key not in missing:
            missing.append(key)
    # A form with separate first / last fields does not also need the full name, and the
    # other way round: drop what the profile simply does not hold in that shape.
    missing = [k for k in missing if not (k == "name" and full.get("first_name"))]
    return todo, missing


def describe(profile):
    """'name, email and phone number' — what Kayra has, never the values."""
    keys = [spoken(k) for k in FIELDS if profile.get(k) and k not in ("first_name", "last_name")
            or (k in ("first_name", "last_name") and profile.get(k) and not profile.get("name"))]
    if not keys:
        return ""
    return keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + " and " + keys[-1]
