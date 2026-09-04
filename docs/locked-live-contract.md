# 잠긴 실전 연결 계약

이 릴리스는 실전 매매를 활성화하지 않는다. `require_live_authorization()`은 항상 `LiveTradingLockedError`를 발생시킨다. CLI, 환경변수, UI, 자격증명 자동 탐색, 활성화 옵션은 없다. 테스트 파일만 guard를 패치하고 가짜 자격증명과 `httpx.MockTransport`를 주입한다. 실제 비공개 API 요청(주문 테스트 포함), 실제 주문·취소, 자금 이동 및 운영 검증은 수행하지 않았다.

## 구현된 경계

`LiveClient`는 공식 `https://api.upbit.com`의 `/v1/accounts`, `/v1/orders/chance`, `/v1/orders`, `/v1/order`만 사용한다. JWT HS512, 고유 nonce, 순서 보존 비이스케이프 쿼리 SHA512, 시장가 매수 KRW 총액 및 시장가 매도 BTC 수량을 구현한다. TLS 검증을 켜고 redirect와 환경 proxy 상속을 끈다. 임의 목적지나 사용자 구성 HTTP client는 받지 않는다. 요청 오류와 자격증명 공급자 오류에 임의 응답 본문이나 비밀값을 넣지 않는다.

`LiveService`는 실제 `StrategyEngine`과 `advance_risk_state`를 호출한다. 계정 잔고·주문 가능 정보·조회된 체결 수량/대금/수수료가 실행 사실이다. 종가, 다음 봉 시가, 고가·저가로 실제 체결을 만들어 기록하지 않는다. 진입 봉의 고가는 체결 이전일 수 있으므로 체결 이후 확정 종가와 유효한 현재 가격 관측만 그 봉의 보유 고점 근거로 사용한다. 진입 ATR와 당시 배수, 실제 평균 체결가, 활성/대기 손절, 포지션 원가와 실현 손익을 보존한다.

## 입력 시각과 금액

- `DecisionInput.row['timestamp']`는 기존 공개 데이터와 같은 **UTC 4시간봉 시작 시각**이다. UTC-aware, 0/4/8/12/16/20시 정각이어야 한다. OHLC는 양의 유한 숫자이고 고저 관계가 맞아야 한다.
- 봉 끝은 시작+4시간이다. `봉 끝 <= RiskObservation.now <= clock()`을 요구한다. 위험 관측 cutoff와 봉 라벨, 실제 체결 시각은 서로 다른 사실이다. 체결 시각을 봉에 맞춰 재작성하지 않는다.
- 보유 봉수는 진입 봉0을 기준으로 완료 봉의 시작 bucket에서 실제 체결이 속한 봉 bucket을 뺀다. 봉 끝/cutoff 때문에1을 추가하지 않는다. 포지션 관측 provenance와 보유 봉수의 단조성은 유지한다.
- 진입 시각은 누적 매수 체결의 실제 최초 시각이며 배열 순서·최초 조회 시점·후속 부분 체결로 바꾸지 않는다. 마지막 체결 시각은 별도로 보존하여 최종 청산에 사용한다. 주문 생성시각이나 추정 시각으로 누락된 체결 시각을 대신하지 않는다.
- 조정된 진입 시각이 봉 끝 이상이면 그 봉은 전부 진입 전이다(봉 구간은 시작 이상·끝 미만). `hold` / `DEFERRED_PRE_ENTRY_CANDLE`로 재시도 가능하게 유보하며 해당 OHLC로 고점·손절·청산·평가자산·위험 이력·완료 cursor를 진행하지 않는다. 기존 포지션/보호 사실을 유지하고 다음 적합한 봉에서 자동 재개한다. 유보 자체로 건강 정지를 걸거나 미확정 주문을 만들지 않으며, 별도 실제 가격 관측 보호는 계속 독립적이다.
- `HealthMonitor`의 기존 10분 지연 판정과 API 연속 성공, 스키마, 시각, 미확정 주문, 원장 일치, 체결 편차 조건을 함께 적용한다. 건강 상태는 실전 원장에 저장한다. paper 원장이나 monitor의 paper persistence 메서드를 사용하지 않는다.
- caller가 넘긴 모의 cash/equity/position/closed_trades는 실전 사실이 아니다. 서비스는 조회된 KRW/BTC 잔고와 실제 체결 원장의 포지션/거래를 사용한다. 최초 equity 및 일간·주간·최고자산 기준을 동일한 실제 KRW 기준으로 초기화한다.
- 숫자 `100` 자체는 금지하지 않는다. 실제 100 KRW는 유효한 잔고지만 거래소 최소 주문액 때문에 주문할 수 없을 수 있다. `normalized100` 또는 paper 원장 provenance는 거부한다.
- 기존 BTC 잔고만으로 전략 소유권이나 진입 ATR를 추측하지 않는다. 미관리 BTC/잔고 불일치가 있으면 새 노출을 막는다. 매도량은 전략 소유량과 거래소 사용 가능량 모두 이하이며 거래소 최소/최대 금액을 재검증한다.

## 영속성과 재시작

SQLite 실전 원장과 `.owner.sqlite` sidecar는 한 쌍으로 운영한다. 같은 정규 경로의 단일 실행자가 sidecar SQLite write transaction을 수명 동안 보유한다. 의도와 상태는 별도 본 원장 transaction으로 **전송 전에 commit**된다. 프로세스가 죽으면 OS가 소유권 잠금을 해제하지만 의도는 남는다. 시간 만료로 여전히 실행 중인 writer를 밀어내지 않는다.

한 번 저장한 identifier의 결과가 시간 초과, 프로세스 종료, 응답 오류 또는 not-found로 불명확하면 identifier로 조회만 계속한다. not-found도 미전송의 증명이 아니므로 새 identifier로 재주문하지 않는다. 부분 매수 잔여는 취소 의도를 먼저 기록하고 취소 후 조회하며, 취소 응답을 체결 또는 취소 확정으로 간주하지 않는다. 누적 체결·대금·수수료 감소 및 주문 식별 모순은 기존 사실을 보존한 채 막는다. 0체결 취소는 포지션을 만들지 않는다.

미확정 주문이 있으면 canonical risk cutoff를 더 진행하지 않는다. 나중에 발견한 실제 청산이 이전 cutoff보다 과거가 되는 문제를 방지하기 위함이다. 조회 후 실제 거래 시각과 caller cutoff가 맞지 않으면 fail-closed하고 다음 올바른 cutoff로 재시도한다. 이미 처리한 봉은 조회/건강 overlay만 갱신하며 같은 진입/임의 청산을 재전송하거나 risk/recovery를 다시 누적하지 않는다. 반환된 반복 봉 판단은 현재 위험 진단이며 재실행 영수증이 아니다.

위험 cutoff가 멈춘 동안에도 유효한 완료 봉의 보유 고점·보유 봉수·활성 보호 상태는 별도로 영속화한다. 포지션의 `completed_bar_at`(마지막 관측 완료 봉의 UTC 시작 시각)과 `completed_bar_fingerprint`(그 입력의 SHA256)는 함께 저장하며, 기존 기록에 둘 다 없으면 `None`으로 읽는다. 더 오래된 봉이나 같은 봉의 변경된 입력은 기존 포지션 사실을 바꾸지 않고 차단한다. 동일 입력 재조회는 위험 cursor를 진행하지 않는다. 주문의 `created_at`은 거래소가 문서화한 초 단위 구간으로 비교하여 같은 초의 마이크로초 정밀도 로컬 의도를 잘못 거부하지 않으며, 실제 trade timestamp의 소수초는 보존한다.

## 보호와 제한

`on_price()`는 caller가 제공한 현재 관측 가격에 공통 활성 손절을 적용한다. trailing 후보는 계산 봉이 끝나 다음 봉이 시작되는 경계에서 활성화한다. 거래소 native stop, 가격 구독, 스케줄러 또는 운영 daemon은 제공하지 않는다. 오프라인 테스트는 그 운영 장치의 신뢰성이나 실제 거래소 체결을 증명하지 않는다.

이 원장은 단일 호스트의 신뢰할 수 있는 로컬 SQLite 파일과 단일 정규 경로를 전제로 한다. 네트워크/동기화 파일시스템의 분산 소유권, 다중 계정 라우팅, 외부 입출금 자동 재분류, 미관리 포지션 자동 인수, journal backup/복구 orchestration, 악의적인 전체 원장 재작성/rollback 방지는 제공하지 않는다. 스키마·시각·risk peak·counter·주문 projection 등의 모순은 거부하지만 암호학적 불변 원장은 아니다. unresolved not-found나 실제 잔고 모순을 임의로 지워 재개하지 않는다. 실전 활성화는 별도 명시적 승인과 코드·운영 검토가 필요하다.

안전 scanner는 정확히 검토된 다섯 live 모듈의 AST digest를 고정하고 나머지 모듈의 직접/간접 경계 import와 기존 동적 우회 패턴을 거부한다. live 디렉터리 전체를 제외하지 않는다. digest는 변경 탐지 장치이며 guard 순서·서명·요청·오류·원장 안전성의 증명 자체는 아니다. 해당 동작은 별도 오프라인 테스트로 검증한다.

## 공식 근거

2026-09-05 확인: [인증](https://docs.upbit.com/kr/reference/auth), [주문 생성](https://docs.upbit.com/kr/reference/new-order), [주문 조회](https://docs.upbit.com/kr/reference/get-order), [취소 접수](https://docs.upbit.com/kr/reference/cancel-order), [잔고](https://docs.upbit.com/kr/reference/get-balance), [주문 가능 정보](https://docs.upbit.com/kr/reference/available-order-information).
