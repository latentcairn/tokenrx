# TokenRx · 토큰 처방전

[English](README.md) | 한국어

Claude Code와 Codex의 사용 기록을 읽어 **토큰과 사용 한도가 어디로 샜는지** 진단하고, 설정을 고쳐 주는 도구예요.

![예시 진단서(가짜 데이터)](docs/sample-report.png)

- **PC 안에서만 동작해요.** 인터넷에 아무것도 보내지 않고, 통계도 수집하지 않아요.
- **진단서는 대화 내용을 읽지 않아요.** 토큰 수와 기록 정보(모델, 시각, 도구 사용 여부)만 써요. 예외는 선택 처방인 자리비움 경고 하나예요. 비싼 이어가기를 멈출 때, 새 대화가 싸게 이어받도록 최근 요청, 마지막 응답, 수정한 파일을 짧은 인수인계 메모로 PC에 저장해요. 이 메모는 PC 밖으로 나가지 않아요.
- **설치할 게 없어요.** 파이썬 3.10 이상만 있으면 돼요.

## 시작하기

```
git clone <이 저장소 주소>
cd tokenrx
python tokenrx.py
```

진단 결과가 터미널에 나오고, `output/report.html`에 진단서가 저장돼요.
최근 며칠만 보려면 `python tokenrx.py --days 14`처럼 실행하세요.
화면 언어는 PC 설정을 따라 한국어나 영어로 정해져요. 직접 고르려면 `--lang ko`나 `--lang en`을 붙이거나 `TOKENRX_LANG` 환경 변수를 쓰세요.

## 무엇을 진단하나요

| 도구 | 진단 항목 |
|---|---|
| Claude Code | 캐시가 깨져서 다시 낸 비용(자리비움, 대화 중 모델 변경), 대화 시작 고정비, 서브에이전트 비용, 너무 길어진 대화, 도구 실패, 출력 비중, 모델별 사용량, 사용 속도 |
| Codex | 주간·5시간 한도 예보, 생각 수준별 한도 소모, 서브에이전트 한도 소모, 캐시 적중률, 추론 비중, 한도를 많이 쓴 대화, 오류(실패하거나 멈춘 작업, 원인별 명령·MCP 실패) |

- **Claude:** 금액은 API 정가로 환산한 값이에요. 구독 요금제의 실제 청구액과는 다르고, 한도를 얼마나 썼는지 비교하는 잣대로 보면 돼요.
- **Codex:** 기록에 실제 한도 사용률이 남아서 %p(주간 한도 사용률이 오른 양)로 보여 줘요.
- **처방을 켰다면 아꼈을 양:** 처방이 있는 진단마다, 지난 기록에 그 처방이 있었다면 얼마나 아꼈을지 보여 줘요. 안내대로 했다고 가정한 최대치이고, 서로 겹치는 부분이 있어서 더하면 안 돼요.
- **효과 확인:** 처방을 적용하면, 적용 전 30일과 적용 뒤 14일 동안 그 처방이 겨냥한 낭비의 비중을 비교해 보여 줘요.
- **오류는 대부분 모델보다 환경 문제예요.** 시험한 Codex 기록에서 서버·모델 오류로 끝난 작업은 1%도 안 됐어요. 대신 AI가 실행한 명령의 약 8%가 실패했고(셸 문법, 없는 경로, 빌드 실패), 실패 직후의 요청이 그 비용이에요.

## 처방

```
python tokenrx.py rx                 # 처방 목록과 적용 상태
python tokenrx.py apply <처방 이름>   # 백업 → 미리 보기 → 동의 → 적용
python tokenrx.py undo               # 가장 최근 처방 되돌리기
python tokenrx.py backups            # 백업 목록
```

| 이름 | 하는 일 |
|---|---|
| `claude-idle-guard` | 긴 대화를 캐시가 만료된 뒤에 이어가려 하면 한 번 멈추고 예상 비용을 알려 줘요. 짧은 인수인계 메모도 남겨요. 같은 폴더에서 새 대화를 열고 "이어서 해줘"라고만 하면, 새 대화가 메모 위치를 전달받아 대화 전체를 다시 저장하는 대신 몇천 토큰만 읽고 이어받아요. 그래도 이어가려면 같은 메시지를 10분 안에 다시 보내면 돼요. |
| `claude-limit-recorder` | 상태 표시줄에 5시간·주간 한도 사용률과 캐시 남은 시간을 보여 주고, 긴 대화의 캐시가 곧 만료되면 `/compact`를 권해요. 한도 기록을 남겨 예보에 써요. 터미널에서 실행한 `claude`에서만 동작해요(Claude 데스크톱 앱은 상태 표시줄을 실행하지 않아요). |
| `claude-autocompact` | 자동 압축 기준(`autoCompactWindow`)을 40만 토큰으로 낮춰요. 100만 토큰 모델은 기본으로 약 97만 토큰이 돼서야 압축해서, 긴 대화는 질문 한 번에 수십만 토큰을 읽어요. 압축하면 오래된 세부 내용은 요약으로 줄어들어요. |
| `claude-subagent-sonnet` | 서브에이전트 기본 모델을 Sonnet으로 바꿔요. |
| `claude-default-opus` | 기본 모델을 Opus로 바꿔요. |
| `codex-effort-high` | Codex 기본 생각 수준을 high로 바꿔요. |
| `codex-shell-notes` | Codex가 실제로 쓰는 셸(기록에서 확인)의 사용 요령 몇 줄을 `~/.codex/AGENTS.md`에 넣어서, 셸 문법 오류로 실패하는 일을 줄여요. 요청마다 약 85토큰이 붙지만 대부분 캐시로 싸게 읽어요. |

- **바꾸는 파일:** `~/.claude/settings.json`, `~/.claude/tokenrx/`, `~/.codex/config.toml`, `~/.codex/AGENTS.md`예요.
- **백업:** 원본은 `backups/`에 저장되고, `undo`로 그대로 되돌릴 수 있어요.

## 데스크톱 대화 정리

```
python tokenrx.py sessions           # Claude 데스크톱 앱 대화 중 보관하거나 이름을 바꿀 것과 그 이유 (아무것도 바꾸지 않아요)
```

크고 식은 대화를 실수로 다시 열면 대화 전체를 다시 저장해서 한도를 크게 써요. 이 목록은 자리비움 경고가 새 대화로 넘긴 대화, 같은 폴더의 새 대화가 이어받은 큰 대화, 짧은 시험 대화, 2주 넘게 안 쓴 대화, 내용을 알 수 없는 제목을 알려 줘요. TokenRx mod를 설치했다면 데스크톱 앱에서 "대화 정리해줘"라고 하세요(`/tokenrx:tidy`). Claude가 같은 목록을 보여 주고, 고른 대화만 이름을 바꾸고 보관해요. 보관한 대화는 언제든 다시 꺼낼 수 있고, 지우지 않아요.

## 가짜 데이터로 시험하기

```
python examples/make_sample.py sample-data ko
python tokenrx.py --claude-home sample-data/claude --codex-home sample-data/codex
```

## 테스트

```
python -m unittest discover -s tests                 # 기록 해석, 추정, 훅, 처방
python tests/e2e_claude.py <claude 실행 파일 경로>      # 실제 Claude Code에서 자리비움 경고 시험
```

종단 테스트는 임시 대화와 임시 설정 파일로 Claude Code를 실행해서, 경고가 모델을 부르지 않고 막는지 확인하고 흔적을 지워요.

## 검증

`eval/`은 처방이 Claude Code의 작업 품질을 떨어뜨리지 않고 토큰을 아끼는지 비교하는 A/B 평가 도구예요. 아직 공개한 결과는 없어요.

| 실험 | 비교 | 재는 것 |
|---|---|---|
| `autocompact` | 자동 압축 기준 1M(control)과 400K(treatment) | 첫 메시지로 정확한 요구 사항 6개를 줘요. 그다음 약 56만 토큰 분량의 코드와 문서를 리뷰하고, 기능을 구현하고, 테스트를 돌려요. 요구 사항마다 숨은 채점으로 확인해요. |
| `handoff` | 같은 대화를 이어가기와, 자리비움 경고의 인수인계 메모로 새 대화 열기 | 약 23만 토큰짜리 대화에서 중간중간 결정을 내리고, 후속 작업이 그 결정에 따라야 해요. 두 방식은 같은 상태에서 출발해요. |

```
python eval/run.py --exp autocompact --model claude-haiku-5-5 --trials 10 --estimate   # 예상 토큰과 비용(아무것도 실행하지 않음)
python eval/run.py --exp handoff --trials 2 --out eval-out/dry --dry-run               # 가짜 claude로 전체 흐름 확인
python eval/run.py --claude <claude 실행 파일 경로> --preflight                          # 아주 짧은 실제 요청 두 개로 설정 확인
python eval/run.py --claude <claude 실행 파일 경로> --exp autocompact --model claude-haiku-5-5 --trials 10 --out eval-out/ac-haiku
```

- **한도를 써요.** 실제 실행은 구독 한도를 써요. 먼저 `--estimate`로 비용을 보고, 모델은 `claude-haiku-5-5`처럼 전체 이름으로 적으세요. `--budget`을 주면 쓴 금액이 그만큼에 닿았을 때 새 시행을 시작하지 않아요. 같은 `--out`으로 다시 실행하면 시행이 더해져요.
- **시행마다 따로 돌아요.** 새 임시 폴더에서 `--setting-sources project`, 시행별 `--settings`, MCP 서버 없이 실행해요. 모델은 Read, Glob, Grep, Edit, Write와 파이썬 명령만 쓸 수 있어요. 끝나면 대화 기록을 `<out>/transcripts/`로 옮기고, `claude purge`로 Claude Code가 그 폴더에 대해 남긴 것을 지운 뒤, 폴더를 지워요. 내 사용 기록 진단에는 섞이지 않아요.
- **채점 방법.** 숨은 채점은 대화가 끝난 뒤에만 넣어요. 보이는 테스트와 마지막 기억 퀴즈도 채점해요. 결과에는 합격률(Fisher 정확 검정, 짝 비교 McNemar 검정), 종류별 평균 토큰, 평균 비용과 차이의 부트스트랩 95% 구간, 시행당 압축 횟수, 첫 요청의 크기가 나와요. `handoff`는 캐시가 만료됐다면 이어가기에 얼마가 더 들었을지도 보여 줘요.
- 결과는 `<out>/trials.jsonl`과 `<out>/summary.json`에 써요.

## 알아 둘 점

- **기록 형식이 바뀔 수 있어요.** Claude Code 기록 형식은 공식 문서가 없고 버전마다 바뀔 수 있어요. 모르는 줄은 건너뛰어요.
- **Codex는 처음 한 번만 오래 걸려요.** 기록이 수 GB면 처음에 1분 정도 걸릴 수 있어요. 그다음부터는 `cache/`에 정리해 둔 결과로 몇 초면 끝나요.
- **한도 기록기는 조건이 있어요.** Claude 구독(Pro·Max)에서, 상태 표시줄이 보이는 화면에서만 기록돼요.

## 라이선스

MIT © latentcairn
