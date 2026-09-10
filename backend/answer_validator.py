"""Conservative, local checks over exactly the evidence shown to the model.

Lexical support is not entailment. A separate model pass checks meaning; these
checks also run after that pass and never repair missing facts with model memory.
"""

import re
import unicodedata
from dataclasses import dataclass

INSUFFICIENT_ANSWER = '현재 수집된 자료만으로는 확인하기 어렵습니다.'
CITATION = re.compile(r'\[(\d+)\]')
BRACKET = re.compile(r'\[[^\]\n]*\]')
NUMBER = re.compile(r'(?<!\d)\d+(?:[.,]\d+)*(?:\s*(?:년|개월|월|일|주년|주|시|분|초|명|만|억|회|집|개|팀|곡|위|%))?')
QUOTED = re.compile(r'''["“‘「『《〈]([^"”’」』》〉\n]+)["”’」』》〉]|'([^'\n]+)' '''.strip())
PARTICLES = ('으로부터', '에서는', '에게는', '으로는', '에서', '에게', '으로', '까지',
             '부터', '에는', '이랑', '처럼', '하고', '이며', '이고', '은', '는', '이',
             '가', '을', '를', '의', '에', '와', '과', '도')
PREDICATE_ENDINGS = ('하였습니다', '되었습니다', '했습니다', '됐습니다', '합니다',
                     '됩니다', '되었다', '됐다고', '이라고', '입니다', '습니다',
                     '했다', '한다', '이며', '이고', '발매했다', '컴백했다')
MAX_EVIDENCE_SENTENCE_CHARS = 600
# Grammatical/reporting expressions, not artist/work/brand names.
FUNCTION_WORDS = {
    '최근', '현재', '요즘', '수집된', '검색', '자료', '자료에서', '기사', '기사에서',
    '해당', '관련', '소식', '내용', '확인', '확인할', '확인됩니다', '확인됐습니다',
    '확인했습니다', '확인되는', '있습니다', '있다', '있으며', '있는', '있어', '통해',
    '따르면', '대해', '대한', '위해', '함께', '또한', '그리고', '것으로', '것을',
    '것입니다', '등', '중', '수', '한', '이', '그', '및', '와', '과', '것',
    '진행', '진행한', '진행했습니다', '진행했다고', '진행했다', '진행합니다',
    '했습니다', '합니다', '했다', '한다', '하였습니다', '하였다고', '이라고', '라고',
    '보도했습니다', '보도됐습니다', '보도되었습니다', '소개됐습니다', '소개되었습니다',
    '전했습니다', '밝혔습니다', '밝혔다', '되었습니다', '됐습니다', '됩니다',
    '예정입니다', '예정이라고', '예정이다', '예정', '소식입니다',
}
GLOBAL_ABSTENT = (
    INSUFFICIENT_ANSWER,
    '현재 수집된 자료에서 확인하기 어렵습니다.',
    '현재 수집된 자료에서 관련 정보를 찾지 못했습니다.',
)


@dataclass(frozen=True)
class ValidationResult:
    answer: str
    citation_ids: tuple[int, ...]
    removed_sentences: tuple[str, ...]
    reasons: tuple[str, ...]
    replaced_sentences: tuple[tuple[str, str], ...] = ()


def _normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def sentences(answer: str) -> list[str]:
    """Attach citations following punctuation to that sentence, not the next.

    Newlines are also boundaries. Decimal points do not split a sentence.
    Malformed citations remain in the unit and are rejected by validation.
    """
    units = []
    for line in answer.splitlines():
        line = re.sub(r'^\s*(?:[-*•]\s+|\d+[.)]\s+)', '', line).strip()
        if not line:
            continue
        pattern = r'.+?(?:[.!?。！？](?:[”’"\'])?(?:\s*\[[^\]\n]*\])*(?=\s|$)|$)'
        units.extend(match.group().strip() for match in re.finditer(pattern, line) if match.group().strip())
    return units


def _lexical_tokens(claim: str, evidence: str) -> list[str]:
    tokens = []
    for word in re.findall(r'[가-힣a-zA-Z0-9]+', _normalize(claim)):
        if word in FUNCTION_WORDS or NUMBER.fullmatch(word):
            continue
        # Test the unstripped token first so particle-like endings in names are
        # not removed when the full name already occurs in the article.
        if word in evidence:
            tokens.append(word)
            continue
        for suffix in PARTICLES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 2:
                word = word[:-len(suffix)]
                break
        for suffix in PREDICATE_ENDINGS:
            stem = word[:-len(suffix)] if word.endswith(suffix) else ''
            if len(stem) >= 2 and stem in evidence:
                word = stem
                break
        if word not in FUNCTION_WORDS and word:
            tokens.append(word)
    return tokens


def _supported(claim: str, evidence: str, *, semantic_verified: bool = False) -> bool:
    evidence = _normalize(evidence)
    evidence += ' ' + ' '.join(f'{int(year)}년 {int(month)}월 {int(day)}일'
                              for year, month, day in re.findall(r'\b(\d{4})-(\d{2})-(\d{2})\b', evidence))
    compact = re.sub(r'\s+', '', evidence)
    # A number with its unit must occur together, not just elsewhere as a year
    # or a count. Numeric boundaries prevent 1 from matching 11.
    for match in NUMBER.finditer(_normalize(claim)):
        number = re.sub(r'\s+', '', match.group())
        end_boundary = r'(?!\d)' if number[-1].isdigit() else ''
        if not re.search(r'(?<!\d)' + re.escape(number) + end_boundary, compact):
            return False
    for match in QUOTED.finditer(claim):
        name = match.group(1) or match.group(2)
        if re.sub(r'\s+', '', _normalize(name)) not in compact:
            return False
    if semantic_verified:
        # Semantic review checks subject/action, negation, names and synthesis.
        # Token identity would reject legitimate Korean paraphrases afterwards.
        return bool(claim.strip())
    tokens = _lexical_tokens(claim, evidence)
    # Require every remaining specific token, not an overlap average that could
    # hide an invented album name in a mostly accurate sentence.
    return bool(tokens) and all(token in evidence for token in tokens)


def _is_extract(claim: str, evidence: str) -> bool:
    # Whitespace/quote typography and terminal punctuation may differ, but the
    # original subject, action, numbers and word order must be preserved.
    def canonical(text):
        text = _normalize(text).translate(str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"'}))
        return re.sub(r'\s+', '', text).strip(' .!?。！？')
    claim = canonical(claim)
    return bool(claim) and any(claim == canonical(unit) for unit in sentences(evidence))


def _grounded_extract(claim: str, cited_evidence: list[str]) -> str | None:
    # If a verified paraphrase has all its specific facts in ONE source
    # sentence, return that original sentence rather than synthesizing prose.
    # A bag of matching words spread across the article is not sufficient.
    candidates = []
    for unit in sentences(cited_evidence[0]):
        if len(unit) > MAX_EVIDENCE_SENTENCE_CHARS or '[' in unit or ']' in unit:
            continue
        if _supported(claim, unit) and all(_is_extract(unit, source) for source in cited_evidence[1:]):
            candidates.append(unit)
    return min(candidates, key=len) if candidates else None


def validate_answer(answer: str, evidence: list[str], *, require_extract: bool = False,
                    semantic_verified: bool = False) -> ValidationResult:
    answer = unicodedata.normalize('NFKC', answer).strip()
    units = sentences(answer)
    kept, removed, reasons, replaced = [], [], [], []
    used = set()
    pending_heading = None
    for unit in units:
        if re.fullmatch(r'\*\*[^*\n]+\*\*', unit):
            label = unit.strip('*')
            if label in {'요약', '주요 포인트', '주요 인물/그룹', '핵심 활동', '최근 흐름', '종합'} or any(label in source for source in evidence):
                pending_heading = unit
                continue
        if CITATION.sub('', unit).strip() in GLOBAL_ABSTENT:
            if len(units) == 1:
                return ValidationResult(INSUFFICIENT_ANSWER, (), (), ('global_insufficiency',))
            removed.append(unit)
            reasons.append('global_insufficiency')
            continue
        citations = CITATION.findall(unit)
        brackets = BRACKET.findall(unit)
        residue = CITATION.sub('', unit)
        valid_format = (all(CITATION.fullmatch(bracket) for bracket in brackets)
                        and '[' not in residue and ']' not in residue)
        ids = tuple(dict.fromkeys(int(number) for number in citations))
        reason = None
        if not valid_format or not ids or any(number < 1 or number > len(evidence) for number in ids):
            reason = 'invalid_or_missing_citation'
        else:
            claim = CITATION.sub('', unit).strip()
            # Only a successful semantic review may admit multi-source synthesis.
            if semantic_verified and not require_extract:
                supported = _supported(claim, '\n'.join(evidence[number - 1] for number in ids), semantic_verified=True)
            else:
                supported = all(_supported(claim, evidence[number - 1]) for number in ids)
            if not supported:
                reason = 'unsupported_specific_tokens'
            elif require_extract and not all(_is_extract(claim, evidence[number - 1]) for number in ids):
                original = _grounded_extract(claim, [evidence[number - 1] for number in ids])
                if original is None:
                    reason = 'not_an_evidence_sentence'
                else:
                    grounded = original + ''.join(f'[{number}]' for number in ids)
                    replaced.append((unit, grounded))
                    unit = grounded
        if reason:
            removed.append(unit)
            reasons.append(reason)
        else:
            if require_extract and unit in kept:
                removed.append(unit)
                reasons.append('duplicate_sentence')
                continue
            if pending_heading:
                kept.append(pending_heading)
                pending_heading = None
            kept.append(unit)
            used.update(ids)
    return ValidationResult('\n'.join(kept) if kept else INSUFFICIENT_ANSWER,
                            tuple(sorted(used)), tuple(removed), tuple(reasons), tuple(replaced))


def remap_citations(answer: str, citation_ids: tuple[int, ...]) -> str:
    mapping = {old: new for new, old in enumerate(citation_ids, start=1)}
    return CITATION.sub(lambda match: f'[{mapping[int(match.group(1))]}]', answer)
