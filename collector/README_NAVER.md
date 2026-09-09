# NAVER 2026 수집기

프로젝트 루트에서 실행:

```powershell
python collector/sources_naver.py
```

기존 `collector/.env`의 DATABASE_URL, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET을 사용합니다.
이미 설정된 프로세스 환경변수가 .env보다 우선합니다. RSS 수집기와 별도로 실행하며 임베딩을 생성하지 않습니다.

- 첨부 코드의 검색어 44개를 사용합니다.
- 기본 display=100, sort=date, start=1,101,...,901로 검색어당 최대 1000개 후보를 조회합니다.
- 기본 최대 NAVER 검색 호출 수는 44 × 10 = 440회입니다. 본문 다운로드는 별도 HTTP 요청입니다.
- 날짜 기준은 첨부 코드와 같은 UTC입니다. 2026-01-01 00:00 UTC 이상, 2027-01-01 00:00 UTC 미만이면서 실행 시작 시각 이하인 기사만 처리합니다. 미래 기사와 날짜 파싱 실패는 제외합니다.
- pubDate는 NAVER 제공 시각이며, 원문 발행 시각과 항상 동일하다고 보장되지 않습니다.
- 빈 페이지, 마지막 페이지, 날짜를 모두 파싱한 페이지 전체가 2026년 이전이면 조기 종료합니다. 날짜 미상만으로는 과거 구간이라고 판단하지 않습니다.
- 실패한 API 호출도 집계합니다. HTTP 401/403/429는 전체 실행을 중단하고 오류 종료 코드 1을 반환합니다. 자동 반복 재시도는 하지 않습니다.
- 검색어별 호출 수·페이지 수·저장 수·중복·날짜 오류·본문 실패·최신/최과거 조회 시각·종료 사유를 표준 출력으로 기록합니다. 계정 전체 quota 사용량을 조회하거나 영구 체크포인트를 저장하는 기능은 없습니다.
- 실행 전체의 URL/본문 중복 집합을 공유합니다. URL DB 중복을 본문 다운로드 전에 확인합니다. 실패한 저장/본문 추출은 성공으로 캐시하지 않습니다.
- DB 연결은 autocommit=True이며 INSERT는 기사별 transaction으로 확정합니다. 한 기사 실패로 이전 저장분이나 저장 카운터가 롤백되지 않습니다.
- URL 중복은 기존 ON CONFLICT(url_hash)로 처리합니다. 본문 해시는 사전 검사이며 DB UNIQUE 제약이 없는 경우 동시 프로세스 간 본문 중복을 완전히 보장하지 않습니다. DB schema는 변경하지 않았습니다.
- 기존 `fetch_naver_news(query, category, display=100)` 진입점을 유지합니다.

NAVER 검색 결과 창 밖의 과거 기사는 이 방식으로 수집할 수 없으므로 2026년 전체 기사 수집을 보장하지 않습니다.
[공식 NAVER API HUB 뉴스 문서](https://api.ncloud-docs.com/docs/naver-api-hub-search-news)는 display 1~100, start 1~1000, 검색 API 일일 한도 25,000회를 안내합니다. 공유되는 다른 호출과 계정 상태는 별도로 확인해야 합니다.

검증:

```powershell
python -m py_compile collector/sources_naver.py collector/test_sources_naver.py
python -m unittest collector.test_sources_naver
```

모의 테스트 10개로 페이지 경계·연도/미래 필터·호출 실패·중복·재시도·기사별 트랜잭션을 검증했습니다. 이번 수정 작업에서 실제 대량 수집, NAVER 인증 호출, DB INSERT는 실행하지 않았습니다.
