"""Small, deterministic query hints; no rewriting or external NLP calls."""

import re

RECENCY_TERMS = ('최근', '요즘', '현재', '근황', '최신')
STOPWORDS = {
    *RECENCY_TERMS, '뭐', '뭐야', '알려줘', '알려주세요', '활동', '소식',
    '관련', '어떻게', '했어', '찍었어', '주요', '이슈', '대한', '대해',
    '좀', '어떤', '있어', '있나요', '해줘', '뉴스',
}
# Longer particles first. Keep at least two characters to avoid stripping names
# such as 아이브, 파묘, 샤이니, 싸이, 현아 indiscriminately.
PARTICLES = ('에서는', '에게는', '으로는', '에서', '에게', '한테', '에대한',
             '으로', '이랑', '하고', '처럼', '까지', '부터', '에는',
             '은', '는', '을', '를', '의', '에', '와', '과', '도')
TOPIC_TERMS = {'영화', '드라마', '음악', '공연', '광고', '앨범', '콘서트'}


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
    # named work, e.g. "최근 영화 파묘 관련 소식".
    if '영화' in words and any(word not in TOPIC_TERMS for word in words):
        words.remove('영화')
    return words


def wants_recent(question: str) -> bool:
    return any(term in question for term in RECENCY_TERMS)
