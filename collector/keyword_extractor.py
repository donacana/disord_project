"""
기사 제목에서 인물명·작품명을 뽑아 articles.keywords 배열에 넣기 위한 모듈.

한국 연예 기사 제목의 정형화된 패턴을 이용:
  - 작은따옴표 안 = 작품명/곡명    예) '펜트하우스', '이 별로부터'
  - 쉼표 앞 = 주체(인물/그룹)       예) "아이유, 10일 싱글..." → 아이유
  - 큰따옴표 안 = 인용문이므로 제외
  - 가운뎃점·♥ 등으로 나열된 인물은 분리   예) 김풍·이은지

형태소 분석기 없이 규칙만 사용하므로 100% 정확하진 않음.
하이브리드 검색에서 '필터'가 아니라 '가산점' 축으로 쓰는 것이 전제.
"""

import re

JOSA = (
    r"(에서는|으로는|에게는|이라며|라며|이라고|라고|에서|에게|으로"
    r"|부터|까지|처럼|보다|이랑|와의|과의|의|은|는|이|가|을|를|와|과|도|만|로|에)"
)

# 키워드로 부적합한 일반명사·직함
STOPWORDS = {
    "걸그룹", "보이그룹", "아이돌", "배우", "가수", "그룹", "감독", "작가", "멤버", "팬", "팬들",
    "신드롬", "초통령", "국민", "공개", "확정", "출연", "방송", "드라마", "영화", "예능", "웹툰",
    "콘서트", "앨범", "싱글", "무대", "화보", "근황", "소식", "결실", "위기", "전격", "단독", "공식",
    "최초", "최대", "최고", "역대", "오늘", "내일", "이번", "지난", "올해", "작년", "월드투어",
}

SEPARATORS = r"[·X×/♥&+]"


def _clean_token(t: str, strip_josa: bool = True) -> str:
    t = t.strip(" '\"‘’“”·…-~!?.,()[]行")
    if strip_josa:
        t = re.sub(JOSA + r"$", "", t)
    return t.strip()


def _is_valid(t: str) -> bool:
    if not (2 <= len(t) <= 12):
        return False
    if t in STOPWORDS:
        return False
    if re.search(r"[…~!?%]", t):
        return False
    # 서술형 어미로 끝나면 고유명사가 아님
    if re.search(r"(다|요|까|죠|네|군|음|함|것|때|중|말|해|돼|된|한|할|는|기)$", t):
        return False
    # 숫자·단위만 있는 경우
    if re.fullmatch(r"[\d\s년월일시분초회명건억만천%]+", t):
        return False
    return True


def extract_keywords(title: str) -> list[str]:
    """제목에서 키워드 최대 5개 추출"""
    kws = []

    body = re.sub(r'["“][^"”]*["”]', " ", title)      # 큰따옴표 인용문 제거
    body = re.sub(r"\[[^\]]*\]", " ", body).strip()   # [가요소식] 같은 태그 제거

    # 1) 작은따옴표 안 = 작품명/곡명 (조사 제거하지 않음)
    for m in re.findall(r"['‘]([^'’]{2,20})['’]", body):
        m = _clean_token(m, strip_josa=False)
        if _is_valid(m):
            kws.append(m)

    rest = re.sub(r"['‘][^'’]*['’]", " ", body).strip()

    # 2) 쉼표 앞 = 주체
    head = rest.split(",")[0] if "," in rest else rest
    head = re.split(r"[…·]{2,}|\.\.", head)[0]

    for part in re.split(SEPARATORS, head):
        words = part.strip().split()
        # 쉼표가 있으면 쉼표 직전 어절이 주체, 없으면 문장 앞쪽이 주체
        candidates = words[-2:] if "," in rest else words[:2]
        for w in candidates:
            w = _clean_token(w)
            if _is_valid(w):
                kws.append(w)

    # 중복 제거(순서 유지)
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:5]


if __name__ == "__main__":
    samples = [
        "아이유, 10일 싱글 '이 별로부터' 발표…정규 6집",
        "'펜트하우스' 윤종훈, 배우 한은서와 11월 결혼",
        "김풍·이은지, 아이돌과 '한식 수다' 펼친다",
        "르세라핌 홍은채, 파란 하늘 아래 일상샷…9월 컴백",
    ]
    for s in samples:
        print(f"{s[:40]:<42} → {extract_keywords(s)}")
