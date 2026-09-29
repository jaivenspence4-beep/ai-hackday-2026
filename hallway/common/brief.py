"""Synthetic HANDOFF evidence checks; not a de-identification certification."""
import hashlib
import json
import re
import unicodedata
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field


class Patient(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pseudo_id: str


class QuotedItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str
    quote: str


class FollowUp(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    text: str
    quote: str
    status: Literal['pending','unresolved'] = 'pending'
    owner: str | None = None
    owner_message_id: str | None = None
    request_message_id: str | None = None


class Brief(BaseModel):
    model_config = ConfigDict(extra='forbid')
    patient: Patient
    meds: list[QuotedItem] = Field(default_factory=list)
    allergies: list[QuotedItem] = Field(default_factory=list)
    pending_results: list[QuotedItem] = Field(default_factory=list)
    findings: list[QuotedItem] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(default_factory=list)


def normalize(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_brief(brief: Brief, recording: dict, enrichment: list[dict]) -> list[str]:
    transcript=normalize(recording['transcript'])
    reasons=[]
    if not recording.get('pseudo_id') or brief.patient.pseudo_id != recording['pseudo_id']:
        reasons.append('patient pseudo_id differs from authenticated Desk assignment')
    follow_ids=[item.id for item in brief.follow_ups]
    if len(set(follow_ids)) != len(follow_ids) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',key) for key in follow_ids):
        reasons.append('follow-up IDs must be unique short alphanumeric labels')
    for group in ('meds','allergies','pending_results','findings','follow_ups'):
        for i,item in enumerate(getattr(brief,group),1):
            if not normalize(item.quote) or normalize(item.quote) not in transcript:
                reasons.append(f'{group} #{i}: quote not found verbatim in transcript')
            if group=='follow_ups' and item.status=='pending' and not item.owner:
                reasons.append(f'follow_ups #{i}: named human-confirmed owner required')
            if group=='follow_ups' and item.status=='unresolved' and item.owner:
                reasons.append(f'follow_ups #{i}: unresolved item cannot have an owner')
    for i,fact in enumerate(enrichment,1):
        url=urlparse(str(fact.get('url','')))
        if url.scheme not in ('http','https') or not url.netloc:
            reasons.append(f'enrichment #{i}: source URL required')
    reasons += identifier_violations({'brief':brief.model_dump(),'enrichment':enrichment},recording)
    return reasons


# Conservative demonstration checks, not a de-identification certification.
LABELS = re.compile(r"(?:patient(?: name)?|full name|name|dob|date of birth|born|mrn|medical record(?: number)?|phone|telephone|address)\s*[:=]\s*([^\n;]+)", re.I)
MONTH_DOB = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:\d{1,2}|[a-z-]+)(?:,)?\s+(?:\d{4}|(?:nineteen|twenty|two thousand)\s+[a-z -]+)", re.I)
DOB = re.compile(r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b")
PHONE = re.compile(r"(?<!\d)(?:\+?1[-. ]?)?(?:\(\d{3}\)|\d{3})[-. ]?\d{3}[-. ]?\d{4}(?!\d)")
MRN = re.compile(r"\b(?:MRN|medical record(?: number)?)\s*(?:is|[:#=])?\s*((?=[A-Z0-9-]*\d)[A-Z0-9-]{4,})\b", re.I)
ADDRESS = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9 .]+?\s+(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd)\b(?:[^\n;]*)", re.I)


def extract_identifiers(transcript: str) -> list[str]:
    values=[]
    # Bounded labels and ordinary spoken synthetic-fixture forms. Avoid swallowing
    # an entire clinical sentence as the supposed patient name.
    name_patterns=(r"(?i:patient(?: name)?|full name|name)\s*(?::|=|is)\s*([A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?(?:\s+[A-Z][a-z]+){1,3})",
                   r"\b(?i:This is)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})(?=,|\.|\s+date of birth)")
    name_patterns += (r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}),\s*(?:date of birth|DOB|medical record(?: number)?|MRN)\b',)
    for pattern in name_patterns:
        for match in re.finditer(pattern,transcript):
            name=match.group(1).strip()
            values.append(name)
            values.extend(word for word in name.split() if len(word)>=3)
    # DOB phrases may be spoken entirely as words; preserve exact spoken value.
    for match in re.finditer(r"(?:DOB|date of birth|born)\s*(?::|=|is)?\s*([^.;\n]+)",transcript,re.I):
        values.append(match.group(1).strip())
    for match in re.finditer(r"(?:phone|telephone|address)\s*[:=]\s*([^;\n]+)",transcript,re.I):
        values.append(match.group(1).strip(' .'))
    spoken_digit=r'(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b'
    spoken_pattern=rf'(?:MRN|medical record(?: number)?|phone(?: number)?|telephone(?: number)?|(?:his|her|their|contact) number|number)\s*(?:is|:|=)?\s*({spoken_digit}(?:[ ,;-]+{spoken_digit}){{3,}})'
    spoken_values=[m.group(1).strip() for m in re.finditer(spoken_pattern,transcript,re.I)]
    values += spoken_values
    digits={'zero':'0','oh':'0','one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9'}
    # Equivalent digit renderings remain identifiers, even when a model reformats
    # a spoken MRN/phone instead of quoting the original digit words.
    values += [''.join(digits[word] for word in re.findall(r'[a-z]+',value.casefold())) for value in spoken_values]
    values += DOB.findall(transcript) + PHONE.findall(transcript)
    values += [m.group(1) for m in MRN.finditer(transcript)]
    values += [m.group(0).strip() for m in ADDRESS.finditer(transcript)]
    return sorted({v for v in values if v})


def identifier_violations(payload: dict, recording: dict) -> list[str]:
    # Serialize the entire object: identifiers in quotes, URLs, keys or nested
    # metadata are just as disallowed as identifiers in the clinical text.
    serialized = normalize(json.dumps(payload, ensure_ascii=False))
    identifiers = extract_identifiers(recording['transcript'])
    reasons = []
    for value in identifiers:
        canonical=lambda text: re.sub(r'[,;-]+',' ',normalize(text))
        if normalize(value) in serialized or ' '.join(canonical(value).split()) in ' '.join(canonical(serialized).split()):
            reasons.append('Direct identifier from transcript present in outbound JSON')
            break
    if DOB.search(serialized) or MONTH_DOB.search(serialized) or PHONE.search(serialized) or MRN.search(serialized) or ADDRESS.search(serialized):
        reasons.append('DOB/MRN/phone/address pattern present in outbound JSON')
    return reasons


IDENTIFIER_FIELDS = frozenset({'patient_name','dob','mrn','phone','address'})


def identifier_fields(transcript: str) -> list[str]:
    """Detected field categories only; no claim of complete identifier detection."""
    fields=set()
    name=r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}"
    if re.search(rf"(?i:patient(?: name)?|full name|name|This is)\s*(?::|=|is)?\s*{name}",transcript) or re.search(rf"{name},\s*(?i:date of birth|DOB|medical record|MRN)",transcript):
        fields.add('patient_name')
    if DOB.search(transcript) or re.search(r'(?:DOB|date of birth|born)\s*(?::|=|is)?\s*[^.;\n]+',transcript,re.I):
        fields.add('dob')
    digit=r'(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b'
    spoken=rf'{digit}(?:[ ,;-]+{digit}){{3,}}'
    if MRN.search(transcript) or re.search(rf'(?:MRN|medical record(?: number)?)\s*(?:is|:|=)?\s*{spoken}',transcript,re.I):
        fields.add('mrn')
    if PHONE.search(transcript) or re.search(rf'(?:phone(?: number)?|telephone(?: number)?|(?:his|her|their|contact) number)\s*(?:is|:|=)?\s*{spoken}',transcript,re.I):
        fields.add('phone')
    if ADDRESS.search(transcript) or re.search(r'address\s*[:=]\s*[^;\n]+',transcript,re.I):
        fields.add('address')
    return sorted(fields)
