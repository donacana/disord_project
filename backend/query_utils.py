"""Small, deterministic query hints; no rewriting or external NLP calls."""

import re
from dataclasses import dataclass

if __package__:
    from .config import DEFAULT_RECENT_DAYS
else:
    from config import DEFAULT_RECENT_DAYS

RECENCY_TERMS = ('최근', '요즘', '현재', '근황', '최신', '이번주', '이번달')
ALLOWED_INTENTS = frozenset({
    'definition', 'activity', 'controversy', 'comeback', 'movie', 'drama',
    'show', 'event', 'trend', 'trend_ranking', 'general',
})
ALLOWED_TIME_RANGES = frozenset({'recent', 'today', 'week', 'month', 'year', 'all', 'unknown'})
ENTITY_ALIASES = {
    '스키즈': '스트레이 키즈',
    '방탄': 'BTS',
    '방탄소년단': 'BTS',
    '블핑': 'BLACKPINK',
}
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
                   '어떤 그룹이야', '무슨 그룹이야', '어떤 사람이야', '뭐하는 그룹이야',
                   '뭐하는 애들이야', '뭐야', '뭔데',
                   '소개해줘', '무엇', '멤버', '소속'),
    'controversy': ('논란', '사건', '소송', '고소', '법원', '징역', '모욕', '재판', '혐의',
                    '판결', '의혹', '갑질', '루머'),
    'advertisement': ('광고', '브랜드', '화보', '앰버서더', '캠페인'),
    'event': ('공연', '콘서트', '팬미팅', '행사', '투어', '페스티벌'),
    'music_release': ('컴백', '앨범', '신곡', '음반', '발매'),
    'movie_release': ('개봉', '개봉작'),
    'drama': ('드라마',),
    'movie': ('영화',),
    'activity': ('활동', '근황', '출연', '뭐함', '뭐해', '뭐하고'),
    'comeback': ('컴백', '앨범', '신곡', '음반', '발매'),
    'trend_ranking': ('누가 유명', '누가 핫', '많이 언급', '뜨는 아이돌', '화제 인물'),
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
    search_queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryAnalysis:
    original_question: str
    normalized_question: str
    entity: str | None
    intent: str
    time_range: str
    keywords: tuple[str, ...]
    search_queries: tuple[str, ...]
    confidence: float
    target_type: str = 'entertainer'


def target_type(question: str) -> str:
    if any(term in question for term in ('아이돌', '그룹')):
        return 'idol_or_group'
    if any(term in question for term in ('배우', '연기자')):
        return 'actor'
    return 'entertainer'


def is_ranking_question(question: str) -> bool:
    compact = re.sub(r'\s+', '', question)
    population = any(term in compact for term in ('누가', '누구', '어떤아이돌', '아이돌', '배우', '그룹', '연예인', '화제인물'))
    ranking = any(term in compact for term in ('유명', '핫', '뜨는', '화제', '많이언급', '활동이많', '활동많'))
    return population and ranking


def _canonical_intent(intent: str | None) -> str:
    intent = (intent or '').strip().casefold()
    if intent == 'music_release':
        return 'comeback'
    return intent if intent in ALLOWED_INTENTS else 'general'


def _time_range(question: str) -> str:
    compact = re.sub(r'\s+', '', question.casefold())
    if '오늘' in compact:
        return 'today'
    if '이번주' in compact or '이번주' in compact:
        return 'week'
    if '이번달' in compact:
        return 'month'
    if '올해' in compact:
        return 'year'
    if any(term in compact for term in RECENCY_TERMS):
        return 'recent'
    return 'unknown'


def _alias(value: str | None) -> str | None:
    if not value:
        return None
    return ENTITY_ALIASES.get(value.casefold(), value)


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
                   if any(re.sub(r'\s+', '', term.casefold()) in compact for term in terms)), None)
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
    entities = tuple(_alias(word) or word for word in keywords if word not in GENERAL_TERMS)
    if intent == 'definition':
        definition_entity = extract_definition_entity(question)
        if definition_entity:
            entities = (definition_entity,)
    days = 7 if '이번주' in compact else DEFAULT_RECENT_DAYS if wants_recent(question) else None
    return QueryHints(tuple(keywords), entities, intent, categories, strict, days)


def rule_query_analysis(question: str) -> QueryAnalysis:
    hints = analyze_query(question)
    compact = re.sub(r'\s+', '', question.casefold())
    generic_prefixes = tuple(term.casefold() for terms in INTENT_TERMS.values() for term in terms)
    question_words = {'누가', '누구', '무슨', '어떤', '뭐', '뭔', '무엇', '유명해', '핫함'}
    entity_candidates = [word for word in hints.entities
                         if word.casefold() not in question_words
                         and not any(word.casefold().startswith(prefix) for prefix in generic_prefixes)]
    entity = entity_candidates[0] if entity_candidates else None
    entity = _alias(entity)
    if '무슨일' in compact or '무슨일이' in compact or '무슨 일' in question:
        intent = 'controversy'
    elif is_ranking_question(question):
        intent = 'trend_ranking'
    elif hints.intent == 'music_release':
        intent = 'comeback'
    elif hints.intent in {'definition', 'activity', 'controversy', 'movie', 'drama', 'event',
                          'trend_ranking'}:
        intent = hints.intent
    elif any(term in compact for term in ('아이돌컴백', '최근컴백', '컴백한')):
        intent = 'comeback'
    elif '예능' in compact or '방송' in compact:
        intent = 'show'
    elif '트렌드' in compact or '유행' in compact:
        intent = 'trend'
    else:
        intent = 'general'
    if entity:
        normalized_subject = entity
    else:
        normalized_subject = ' '.join(hints.keywords)
    normalized = normalized_subject
    if _canonical_intent(intent) == 'activity':
        normalized += ' 최근 활동'
    elif _canonical_intent(intent) == 'definition':
        normalized += ' 그룹 멤버 활동'
    elif _canonical_intent(intent) == 'controversy':
        normalized += ' 논란 사건 법적 대응'
    elif _canonical_intent(intent) == 'comeback':
        normalized += ' 컴백 앨범 신곡'
    elif normalized_subject:
        normalized += ' ' + ' '.join(hints.keywords[1:])
    confidence = 0.9 if entity and intent != 'general' else 0.55 if entity else 0.4
    if any(term in compact for term in ('뭐함', '뭐해', '있음', '누군데')):
        confidence -= 0.15
    analysis_intent = _canonical_intent(intent)
    if analysis_intent == 'trend_ranking':
        entity = None
    fallback = QueryAnalysis(question, normalized.strip(), entity, analysis_intent,
                             _time_range(question), tuple(hints.keywords), (),
                             max(0.0, confidence))
    return QueryAnalysis(fallback.original_question, fallback.normalized_question,
                         fallback.entity, fallback.intent, fallback.time_range,
                         fallback.keywords, _fallback_search_queries(fallback),
                         fallback.confidence, target_type(question))


def _fallback_search_queries(analysis: QueryAnalysis) -> tuple[str, ...]:
    """Build bounded search queries only when LLM understanding falls back."""
    subject = analysis.entity or ' '.join(analysis.keywords)
    if not subject:
        return () if analysis.intent == 'trend_ranking' else (analysis.original_question,)
    return tuple(dict.fromkeys(expand_query(analysis.original_question, analysis)))[:6]


def should_use_llm(analysis: QueryAnalysis) -> bool:
    question = analysis.original_question.casefold()
    compact = re.sub(r'\s+', '', question)
    has_alias = any(alias in compact for alias in ENTITY_ALIASES)
    colloquial = any(term in compact for term in ('뭐함', '뭐해', '있음', '누군데', '핫함', '애들이야'))
    complex_question = any(mark in question for mark in ('그리고', '또', '?', ' 또는 ')) and question.count('?') > 1
    return (not analysis.entity or analysis.intent == 'general' or analysis.confidence < 0.75
            or has_alias or colloquial or complex_question)


def merge_llm_analysis(rule: QueryAnalysis, payload: dict) -> QueryAnalysis:
    if 'entity' not in payload:
        entity = rule.entity
    else:
        raw_entity = payload.get('entity')
        entity = _alias(raw_entity.strip()) if isinstance(raw_entity, str) and raw_entity.strip() else None
    raw_intent = payload.get('intent')
    intent = rule.intent if not raw_intent else _canonical_intent(raw_intent)
    if raw_intent is None and rule.intent != 'general':
        intent = rule.intent
    time_range = payload.get('time_range')
    if time_range not in ALLOWED_TIME_RANGES:
        time_range = rule.time_range if time_range is None else 'unknown'
    keywords = payload.get('keywords')
    if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
        keywords = list(rule.keywords)
    keywords = tuple(dict.fromkeys(item.strip() for item in keywords if item.strip()))
    raw_queries = payload.get('search_queries')
    if not isinstance(raw_queries, list) or not all(isinstance(item, str) for item in raw_queries):
        raw_queries = list(rule.search_queries)
    search_queries = tuple(dict.fromkeys(item.strip() for item in raw_queries if item.strip()))[:6]
    if not search_queries and intent != 'trend_ranking':
        search_queries = _fallback_search_queries(rule)
    normalized = payload.get('normalized_question')
    if not isinstance(normalized, str) or not normalized.strip():
        normalized = rule.normalized_question
    try:
        confidence = max(0.0, min(1.0, float(payload.get('confidence', rule.confidence))))
    except (TypeError, ValueError):
        confidence = rule.confidence
    target = payload.get('target_type', rule.target_type)
    if target not in {'idol_or_group', 'actor', 'entertainer', 'work', 'general'}:
        target = rule.target_type
    return QueryAnalysis(rule.original_question, normalized.strip(), entity, intent, time_range,
                         keywords, search_queries, confidence, target)


def analysis_to_hints(analysis: QueryAnalysis) -> QueryHints:
    legacy_intent = 'music_release' if analysis.intent == 'comeback' else analysis.intent
    keywords = tuple(dict.fromkeys(([analysis.entity] if analysis.entity else []) + list(analysis.keywords)))
    entities = (analysis.entity,) if analysis.entity else ()
    categories = INTENT_CATEGORIES.get(legacy_intent, ())
    if not categories and analysis.intent == 'show':
        categories = ('show',)
    recent_days = {'today': 1, 'week': 7, 'month': 30, 'recent': DEFAULT_RECENT_DAYS,
                   'year': 365}.get(analysis.time_range)
    return QueryHints(keywords, entities, legacy_intent if legacy_intent != 'general' else None,
                      categories, legacy_intent in {'movie', 'movie_release', 'drama'},
                      recent_days, analysis.search_queries)


def extract_definition_entity(question: str) -> str | None:
    """Extract the subject before a definition expression and remove particles."""
    text = re.sub(r'\s+', ' ', question).strip(' ?.!。！？')
    expression = re.compile(
        r'(?:은|는|이|가|을|를)?\s*'
        r'(?:누구야|누군데|누구인데|누구임|누구냐|뭐야|뭔데|무엇이야|'
            r'어떤 그룹이야|무슨 그룹이야|어떤 사람이야|뭐하는 그룹이야|뭐하는 애들이야|'
            r'누구인지 알려줘|소개해줘)'
        r'\s*$',
        re.IGNORECASE,
    )
    match = expression.search(text)
    if not match:
        return None
    entity = text[:match.start()].strip()
    entity = re.sub(r'(은|는|이|가|을|를)\s*$', '', entity).strip()
    return entity or None



def expand_query(question: str, hints: QueryHints | QueryAnalysis | None = None) -> tuple[str, ...]:
    """Return deterministic search variants without another model request."""
    if isinstance(hints, QueryAnalysis):
        subject = hints.entity or ' '.join(hints.keywords)
        if hints.intent == 'definition':
            return tuple(dict.fromkeys((subject, f'{subject} 그룹', f'{subject} 멤버',
                                        f'{subject} 활동', f'{subject} 데뷔')))
        if hints.intent == 'activity':
            return tuple(dict.fromkeys((subject, f'{subject} 최근 활동', f'{subject} 컴백',
                                        f'{subject} 앨범', f'{subject} 공연', f'{subject} 방송')))
        if hints.intent == 'controversy':
            return tuple(dict.fromkeys((subject, f'{subject} 논란', f'{subject} 사건',
                                        f'{subject} 법적 대응')))
        if hints.intent == 'comeback':
            return tuple(dict.fromkeys((subject, f'{subject} 컴백', f'{subject} 앨범', f'{subject} 신곡')))
        return tuple(dict.fromkeys((subject, hints.normalized_question)))
    hints = hints or analyze_query(question)
    base = list(hints.entities) or list(hints.keywords)
    if not base:
        return (question,)
    subject = ' '.join(base)
    variants = [subject]
    if hints.intent == 'definition':
        variants.extend((f'{subject} 그룹', f'{subject} 멤버', f'{subject} 활동', f'{subject} 데뷔'))
    elif hints.intent == 'activity':
        variants.extend((f'{subject} 활동', f'{subject} 출연', f'{subject} 컴백'))
    elif hints.intent == 'controversy':
        variants.extend((f'{subject} 논란', f'{subject} 사건', f'{subject} 법적 대응'))
    else:
        variants.append(question)
    return tuple(dict.fromkeys(variants))
