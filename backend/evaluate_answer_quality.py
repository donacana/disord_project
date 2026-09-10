"""Run the requested questions against the configured real DB/OpenAI services.

Usage: python -m backend.evaluate_answer_quality --output backend/answer_quality_results.json
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import diagnostics, rag

QUESTIONS = [
    '요즘 어떤 아이돌이 유명해?',
    '요즘 누가 유명해?',
    '최근 활동이 많은 아이돌 알려줘',
    '스트레이 키즈 요즘 뭐해?',
    '장원영 최근 활동 알려줘',
    '최근 화제인 배우 알려줘',
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='backend/answer_quality_results.json')
    parser.add_argument('--question', action='append')
    args = parser.parse_args()
    results = []
    for question in args.question or QUESTIONS:
        started = time.perf_counter()
        print(f'RUN {question}', flush=True)
        try:
            response = rag.answer_question(question, 5)
            result = {**diagnostics.snapshot(), 'sources': response.model_dump(mode='json')['sources']}
        except Exception as error:
            result = {**diagnostics.snapshot(), 'error_type': type(error).__name__,
                      'error': str(error)}
        result['executed_at'] = datetime.now(timezone.utc).isoformat()
        result['latency_seconds'] = round(time.perf_counter() - started, 2)
        results.append(result)
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(json.dumps({key: result.get(key) for key in (
            'intent', 'target_type', 'route', 'articles_scanned', 'raw_entities', 'eligible_entities',
            'top_entities', 'candidate_docs', 'final_docs', 'insufficient_reason', 'error', 'latency_seconds')},
            ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
