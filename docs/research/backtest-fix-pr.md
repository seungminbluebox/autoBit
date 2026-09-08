# PR #2: 실제 업비트 데이터 백테스트 입력·수량 검증 수정

대상: `codex/fix-real-upbit-timestamp` → `main`, 커밋 `f719dae`.

실제 업비트 응답에 UTC 캔들 문자열과 숫자 timestamp가 함께 있으면 시간 열이 충돌했습니다. 공식 UTC 캔들 시간을 선택하도록 수정했습니다. 또 정규화 자산 100에서 발생하는 작은 BTC 부분체결을 잘못 거부하던 수량 비교를 보완하고, 큰 잔고의 실제 불일치를 허용하지 않도록 허용오차 상한을 추가했습니다.

변경 파일은 `src/autobit/cli.py`, `src/autobit/validation/runner.py`, `tests/integration/test_collection_evidence.py`, `tests/integration/test_walk_forward_runner.py`입니다. 매매 규칙과 실전 잠금은 바뀌지 않습니다.

이전 작업 기록: 관련 192 passed, 전체 1465 passed / 70 skipped, 독립 리뷰 지적 없음. 이 수치는 이전 실행 증거이며 이번 실행 결과와 구분합니다. 이번 재검증은 연구 결과 문서에 별도로 기록합니다.

2026-09-08 사용자 승인으로 원격 업로드와 [PR #2 생성](https://github.com/seungminbluebox/autoBit/pull/2)을 완료했습니다. main 병합·OCI 배포는 수행하지 않았습니다. 아래 연구 기록은 [후속 PR #3](https://github.com/seungminbluebox/autoBit/pull/3)에서 별도로 검토합니다.
