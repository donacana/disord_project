"""Small, deterministic query hints; no rewriting or external NLP calls."""

import re
from dataclasses import dataclass

if __package__:
    from .config import DEFAULT_RECENT_DAYS
else:
    from config import DEFAULT_RECENT_DAYS

RECENCY_TERMS = ('최근', '요즘', '현재', '근황', '최신', '이번주', '이번달')
STOPWORDS = {
    *RECENCY_TERMS, '뭐', '뭐야', '알려줘', '알려주세요', '활동', '소식',
    '관련', '어떻게', '했어', '찍었어', '주요', '이슈', '대한', '대해',
    '좀', '어떤', '있어', '있나요', '해줘', '뉴스', '있었어', '있었나요',
    '알려', '주세요', '이번', '주', '달', '추천해줘', '궁금해',
    '누구', '누구야', '누군데', '누구인데', '누구임', '누구냐', '누구인지',
    '무엇', '무엇이야', '뭔데', '소개해줘',
}
# Longer particles first; retain at least two characters in proper-name candidates.
PARTICLES = ('에서는', '에게는', '으로는', '에서', '에게', '한테', '에대한',
             '으로', '이랑', '하고', '처럼', '까지', '부터', '에는',
             '은', '는', '을', '를', '의', '에', '와', '과', '도')
TOPIC_TERMS = {'영화', '드라마', '음악', '공연', '광고', '앨범', '콘서트'}

# Specific requests take precedence over the generic activity intent.
INTENT_TERMS = {
    'definition': ('누구야', '누군데', '누구인데', '누구임', '누구냐', '누구인지',
                   '어떤 그룹이야', '무슨 그룹이야', '어떤 사람이야', '뭐야', '뭔데',
                   '소개해줘', '무엇', '멤버', '소속'),
    'controversy': ('논란', '사건', '소송', '고소', '법원', '징역', '모욕', '재판', '혐의',
                    '판결', '의혹', '갑질', '루머'),
    'advertisement': ('광고', '브랜드', '화보', '앰버서더', '캠페인'),
    'event': ('공연', '콘서트', '팬미팅', '행사', '투어', '페스티벌'),
    'music_release': ('컴백', '앨범', '신곡', '음반', '발매'),
    'movie_release': ('개봉', '개봉작'),
    'drama': ('드라마',),
    'movie': ('영화',),
    'activity': ('활동', '근황', '출연'),
}
ARTICLE_INTENT_TERMS = {
    **INTENT_TERMS,
    'definition': ('그룹', '멤버', '소속', '데뷔', '활동', '아이돌', '가수'),
    'activity': ('활동', '공연', '컴백', '출연', '투어', '앨범', '방송', '팬미팅',
                 '광고', '브랜드', '화보', '행사', '촬영', '무대', '개봉', '신곡'),
    'movie_release': ('개봉', '개봉작', '상영'),
}
INTENT_CATEGORIES = {
    'advertisement': ('celeb',), 'event': ('event', 'music'),
    'music_release': ('music',), 'movie_release': ('movie',),
    'movie': ('movie',), 'drama': ('drama',),
}
GENERAL_TERMS = {
    '연예계', '연예', '문화', '대중문화', '주요', '이슈', '일정', '예정', '정보',
    '아이돌', '가수', '배우', '그룹', '걸그룹', '보이그룹', '작품', '추천', '이야기',
    '방송', '예능', '웹툰', '뮤지컬', '음악', '예술', '트렌드', '가요', '업계',
    '요약', '정리', '모두', '전부', '새로운', '새', '인기', '현황',
    *(term for terms in INTENT_TERMS.values() for term in terms),
}


@dataclass(frozen=True)
class QueryHints:
    keywords: tuple[str, ...]
    entities: tuple[str, ...]
    intent: str | None
    categories: tuple[str, ...]
    strict_category: bool
    recent_days: int | None


def extract_keywords(question: str) -> list[str]:
    words = []
    for token in re.findall(r'[가-힣A-Za-z0-9]+', question.casefold()):
        if token in STOPWORDS:
            continue
        for suffix in PARTICLES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                token = token[:-len(suffix)]
                break
        if len(token) >= 2 and token not in STOPWORDS and token not in words:
            words.append(token)
    # 영화 supplies useful intent for broad queries but adds noise next to a
    # named work.
    if '영화' in words and any(word not in TOPIC_TERMS for word in words):
        words.remove('영화')
    return words


def wants_recent(question: str) -> bool:
    return any(term in re.sub(r'\s+', '', question) for term in RECENCY_TERMS)


def analyze_query(question: str) -> QueryHints:
    compact = re.sub(r'\s+', '', question.casefold())
    keywords = extract_keywords(question)
    intent = next((name for name, terms in INTENT_TERMS.items()
                   if any(term in compact for term in terms)), None)
    categories = INTENT_CATEGORIES.get(intent, ())
    strict = intent in {'movie', 'movie_release', 'music_release', 'drama'}
    if not categories:
        if '배우' in compact:
            categories = ('celeb', 'drama')
        elif any(term in compact for term in ('아이돌', '가수', '걸그룹', '보이그룹')):
            categories = ('music', 'celeb')
        elif '웹툰' in compact:
            categories = ('webtoon',)
        elif '예능' in compact:
            categories = ('show',)
    entities = tuple(word for word in keywords if word not in GENERAL_TERMS)
    if intent == 'definition':
        definition_entity = extract_definition_entity(question)
        if definition_entity:
            entities = (definition_entity,)
    days = 7 if '이번주' in compact else DEFAULT_RECENT_DAYS if wants_recent(question) else None
    return QueryHints(tuple(keywords), entities, intent, categories, strict, days)


def extract_definition_entity(question: str) -> str | None:
    """Extract the subject before a definition expression and remove particles."""
    text = re.sub(r'\s+', ' ', question).strip(' ?.!。！？')
    expression = re.compile(
        r'(?:은|는|이|가|을|를)?\s*'
        r'(?:누구야|누군데|누구인데|누구임|누구냐|뭐야|뭔데|무엇이야|'
        r'어떤 그룹이야|무슨 그룹이야|어떤 사람이야|누구인지 알려줘|소개해줘)'
        r'\s*$',
        re.IGNORECASE,
    )
    match = expression.search(text)
    if not match:
        return None
    entity = text[:match.start()].strip()
    entity = re.sub(r'(은|는|이|가|을|를)\s*$', '', entity).strip()
    return entity or None



def expand_query(question: str, hints: QueryHints | None = None) -> tuple[str, ...]:
    """Return deterministic search variants without another model request."""
    hints = hints or analyze_query(question)
    base = list(hints.entities) or list(hints.keywords)
    if not base:
        return (question,)
    subject = ' '.join(base)
    variants = [subject]
    if hints.intent == 'definition':
        variants.extend((f'{subject} 그룹', f'{subject} 멤버', f'{subject} 활동'))
    elif hints.intent == 'activity':
        variants.extend((f'{subject} 활동', f'{subject} 출연', f'{subject} 컴백'))
    elif hints.intent == 'controversy':
        variants.extend((f'{subject} 논란', f'{subject} 사건', f'{subject} 법적 대응'))
    else:
        variants.append(question)
    return tuple(dict.fromkeys(variants))
