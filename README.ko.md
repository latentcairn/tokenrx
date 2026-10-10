# TokenRx · 토큰 처방전

[English](README.md) | 한국어

Claude Code와 Codex의 사용 기록을 읽어 **토큰과 사용 한도가 어디로 샜는지** 진단하고, 설정을 고쳐 주는 도구예요.

![예시 진단서(가짜 데이터)](docs/sample-report.png)

- **PC 안에서만 동작해요.** 인터넷에 아무것도 보내지 않고, 통계도 수집하지 않아요.
- **대화 내용은 읽지 않아요.** 토큰 수와 기록 정보(모델, 시각, 도구 사용 여부)만 써요.
- **설치할 게 없어요.** 파이썬 3.10 이상만 있으면 돼요.

## 시작하기

```
git clone <이 저장소 주소>
cd tokenrx
python tokenrx.py
```

진단 결과가 터미널에 나오고, `output/report.html`에 진단서가 저장돼요.
최근 며칠만 보려면 `python tokenrx.py --days 14`처럼 실행하세요.

## 무엇을 진단하나요

| 도구 | 진단 항목 |
|---|---|
| Claude Code | 캐시가 깨져서 다시 낸 비용(자리비움, 대화 중 모델 변경), 대화 시작 고정비, 서브에이전트 비용, 너무 길어진 대화, 출력 비중, 모델별 사용량, 사용 속도 |
| Codex | 주간·5시간 한도 예보, 생각 수준별 한도 소모, 서브에이전트 한도 소모, 캐시 적중률, 추론 비중, 한도를 많이 쓴 대화 |

- **Claude:** 금액은 API 정가로 환산한 값이에요. 구독 요금제의 실제 청구액과는 다르고, 한도를 얼마나 썼는지 비교하는 잣대로 보면 돼요.
- **Codex:** 기록에 실제 한도 사용률이 남아서 %p(주간 한도 사용률이 오른 양)로 보여 줘요.

## 처방

```
python tokenrx.py rx                 # 처방 목록과 적용 상태
python tokenrx.py apply <처방 이름>   # 백업 → 미리 보기 → 동의 → 적용
python tokenrx.py undo               # 가장 최근 처방 되돌리기
python tokenrx.py backups            # 백업 목록
```

| 이름 | 하는 일 |
|---|---|
| `claude-idle-guard` | 긴 대화를 캐시가 만료된 뒤에 이어가려 하면 한 번 멈추고 예상 비용을 알려 줘요. 같은 메시지를 10분 안에 다시 보내면 그대로 진행돼요. |
| `claude-limit-recorder` | 상태 표시줄에 5시간·주간 한도 사용률과 캐시 남은 시간을 보여 주고, 한도 기록을 남겨 예보에 써요. |
| `claude-subagent-sonnet` | 서브에이전트 기본 모델을 Sonnet으로 바꿔요. |
| `claude-default-opus` | 기본 모델을 Opus로 바꿔요. |
| `codex-effort-high` | Codex 기본 생각 수준을 high로 바꿔요. |

- **바꾸는 파일:** `~/.claude/settings.json`, `~/.claude/tokenrx/`, `~/.codex/config.toml`이에요.
- **백업:** 원본은 `backups/`에 저장되고, `undo`로 그대로 되돌릴 수 있어요.

## 가짜 데이터로 시험하기

```
python examples/make_sample.py sample-data
python tokenrx.py --claude-home sample-data/claude --codex-home sample-data/codex
```

## 알아 둘 점

- **기록 형식이 바뀔 수 있어요.** Claude Code 기록 형식은 공식 문서가 없고 버전마다 바뀔 수 있어요. 모르는 줄은 건너뛰어요.
- **Codex는 처음 한 번만 오래 걸려요.** 기록이 수 GB면 처음에 1분 정도 걸릴 수 있어요. 그다음부터는 `cache/`에 정리해 둔 결과로 몇 초면 끝나요.
- **한도 기록기는 조건이 있어요.** Claude 구독(Pro·Max)에서, 상태 표시줄이 보이는 화면에서만 기록돼요.
- **화면은 아직 한국어만 지원해요.**

## 라이선스

MIT © latentcairn
