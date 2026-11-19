# Conductor 공개판 설계 (작업 계약)

- 상태: 구현 중. 이 문서는 작업자 사이의 계약이다. 바꿀 때는 오케스트레이터가 고친다.
- 대상: 한국어를 쓰고, OpenCodex(`ocx`)로 여러 공급사 모델을 쓰는 Codex 사용자.
- 목표: 남의 컴퓨터에서도 첫 세션부터 Conductor가 켜지고, 요청을 분류해 맞는 스킬과 모델에 맡기고, 회고가 그 사람의 개인 설정을 고쳐 나간다.

## 1. 층

| 층 | 담는 것 | 위치 |
|---|---|---|
| 엔진 | 분류·위험도·검증 하한, 체크포인트·영수증, route 엔진, 회고 절차, 명령 | 이 저장소의 `skills/kp-conductor` |
| 기본 틀 | 경로 범주, 역할 정의 예시, 등급표, 진입 블록 문구 | `skills/kp-conductor/references` |
| 개인 설정 | 그 사람의 경로 지도, 역할 배정, 설정, 모델 메모 | 사용자 컴퓨터의 `<state>/local/` |
| 기록 | 체크포인트, 영수증, 프로필, 회고 보고, 제안 | 사용자 컴퓨터의 `<state>/` |

`<state>`는 `CONDUCTOR_HOME` 환경 변수, 없으면 `~/.codex/kp-conductor`다. 개인 설정과 기록은 이 저장소에 들어오지 않는다.

## 2. 저장소 구조

    kp-conductor/
      README.md  LICENSE  NOTICE.md  DESIGN.md  CHANGELOG.md  RELEASING.md
      skills/kp-conductor/          엔진 스킬 (SKILL.md, VERSION, references/, scripts/conductor.py, scripts/kp_lint.py)
      skills/kp-conductor-onboard/  온보딩 스킬 (SKILL.md, references/)
      skills/korean-protocol/    KP v0.11.1 고정 사본 (LICENSE, NOTICE.md, PINNED.json 포함)
      tools/release.py           버전·릴리스 노트 검사 (RELEASING.md)
      .githooks/pre-push         main push 전 같은 검사
      .github/workflows/         push마다 테스트와 릴리스
      tests/

사용자 설치 위치는 `~/.codex/skills/kp-conductor`, `~/.codex/skills/kp-conductor-onboard`, `~/.codex/skills/korean-protocol`이다. README가 복사 명령을 안내한다.

## 3. 개인 설정 파일 (`<state>/local/`)

| 파일 | 스키마 | 없을 때 |
|---|---|---|
| `routing.json` | 패키지 `references/routing.json`과 같다 | 패키지 `references/routing.json`(기본 틀)을 쓴다 |
| `roles.json` | 패키지 `references/roles.json`과 같다 | 패키지 `references/roles.json`(기본 예시)을 쓴다 |
| `tiers.json` | `references/tiers.json`과 같다. 최상위 키(`models`, `seats` 등) 안의 항목 단위로 패키지 값을 덮어쓴다 | 패키지 값만 쓴다 |
| `settings.json` | 아래 4절 | 기본값을 쓴다 |
| `notes.md` | 사람이 읽는 모델 특성 메모. 회고가 갱신한다 | 없음 |
| `backups/<YYYYmmdd-HHMMSS>/` | 위 파일들의 사본 | 없음 |

## 4. settings.json v1

    {
     "version": 1,
     "orchestrator": "anthropic/claude-opus-5-5",
     "language": "ko",
     "entry": "agents_md",
     "retro": {"apply": "propose"},
     "ocx": "~/.local/bin/ocx",
     "proxy": "http://127.0.0.1:10100",
     "quota": [
      {"key": "openai", "provider": "openai", "window": "weekly", "label": "OpenAI 주간", "models": ["gpt-*"]},
      {"key": "anthropic", "provider": "anthropic", "window": "weekly", "label": "Anthropic 공통", "models": ["anthropic/*"]},
      {"key": "fable", "provider": "anthropic", "window": "Fable", "label": "Fable 전용", "models": ["anthropic/claude-fable-*"], "parent": "anthropic"}
     ],
     "thresholds": {"no_expand": 70, "restrict": 80, "stop": 95, "orchestrator_notice": 85,
                    "builder_one": 70, "builder_stop": 90, "burn_off": 0.7, "burn_down": 1.0},
     "onboarding": {"method": "B", "judge_model": "", "reviewer_model": "", "tiers_version": "", "date": ""}
    }

- 모든 키는 선택이다. 빠진 키는 위 값이 기본값이다. 단 `orchestrator`와 `onboarding`의 기본값은 `null`이다.
- `quota`가 없으면 `ocx provider quota` 출력의 공급사마다 `weekly` 창 하나를 키(공급사 이름)로 만든다. 모델과 공급사의 대응은 이름으로 정한다. `anthropic/`, `xai/`, `google-antigravity/` 같은 접두사가 있으면 그 공급사이고, 접두사가 없으면 `openai`다.
- `models`는 `fnmatch` 패턴이다. 한 모델은 여러 키에 걸릴 수 있다.
- `quota`의 키마다 `thresholds`와 `substitutes`를 선택으로 둔다.
  - `thresholds`는 전역 `thresholds` 가운데 그 키에서 다르게 쓸 값만 적는다. `null`은 그 키에서 해당 기준을 끈다.
  - `substitutes`는 `"모델 강도"` 문자열 목록이다. `p ≥ stop`이면 이 순서로 대체를 안내한다. 대체 모델 자신의 키도 `stop`이면 그 모델은 뺀다.
  - 예: Anthropic 공통은 `{"no_expand": 70, "restrict": null, "stop": 85}`와 `["gpt-6-astra xhigh"]`, OpenAI는 `{"no_expand": null, "restrict": null, "stop": null}`로 소진 속도만 본다.
- `retro.apply`: `propose`(기본)는 회고의 모든 변경을 제안 파일로만 남긴다. `notify`는 retrospective.md의 "알리고 바로 적용" 항목을 적용한다.
  - `off`는 이 컴퓨터의 회고를 끈다. 엔진을 배포받기만 하는 보조 컴퓨터에 쓰며, `cx status`가 `retro: off`를 낸다. `cx retro`는 종료 코드 1로 거절한다.
- `maintainer`(선택): `{"repo": "<엔진 저장소 경로>", "branch": "main", "hosts": [{"name": "<이름>", "ssh": "<SSH 접속 이름>"}]}`. `branch`의 기본값은 `main`이다.
  - `retro.apply=notify`이면 회고가 엔진 변경을 로컬 저장소에 커밋하고 설치·배포한다(retrospective.md "maintainer 모드"). `propose`이면 제안만 남긴다.
  - 회고는 GitHub push를 하지 않고 승인 요청만 남긴다.
- `entry`: `agents_md`(전역 AGENTS.md 블록) 또는 `none`(이름을 불러 쓰는 방식).

## 5. 한도 판단 (일반화)

키마다 사용률 `p`와 소진 속도 `b`(사용률 ÷ 주 경과 비율)를 본다. 사용률은 소수도 받는다(`8.7% resets ...`).

- `p` 또는 `b` 미확인: 이 키 모델의 확대 자리를 끄고 추론 프론티어에 들어가지 않는다. 구현 작업자는 2개까지 띄운다.
- `p ≥ stop`: 이 키 모델을 배정하지 않는다. 같은 역할의 대체 순서로 넘긴다. 키에 `substitutes`가 있으면 쓸 수 있는 대체 모델을 함께 출력한다.
- 아래 기준값은 키마다 `thresholds`로 바꾼 값을 쓴다. 값이 `null`인 기준은 그 키에서 판단하지 않는다.
- `p ≥ restrict`: 이 키 모델은 유형 E 본분석과 R3 검토에만 쓴다.
- `p ≥ no_expand`: 확대 자리를 끄고 추론 프론티어에 들어가지 않는다.
- `b > burn_down`: 확대 자리를 끄고, 사용자가 말한 프론티어도 이 키 모델의 자리를 균형 배정으로 낮춘다. 추론 프론티어에 들어가지 않는다.
- `b ≥ burn_off`: 확대 자리를 끄고 추론 프론티어에 들어가지 않는다.
- 오케스트레이터 모델이 걸린 키의 `p ≥ orchestrator_notice`: 메인 세션도 이 한도를 쓰므로 사용자에게 알린다.
- `cx-build` 모델이 걸린 키의 `p ≥ builder_stop`: 서브에이전트를 띄우지 않고 직접 처리한다. `p ≥ builder_one`: 구현 작업자는 1개, 경쟁 실행은 하지 않는다.
- `expansion`: 키마다 `p < no_expand`이고 `b < burn_off`이면 `on`이다. `parent`가 있으면 부모 키도 `on`이어야 한다. 형식은 현행과 같다: `expansion: openai on; anthropic off; fable off`.

## 6. 명령 (`cx` = `python3 ~/.codex/skills/kp-conductor/scripts/conductor.py`)

현행 명령은 모두 유지한다. 바뀌거나 새로 생기는 것은 아래다. `--home`은 시험용으로 홈 폴더를 바꾼다(기본 `Path.home()`).

| 명령 | 동작 |
|---|---|
| `cx status` | 현행 줄에 더해 `roles:`(역할별 `모델 강도 [한도 키]`), `overlay:`(present/absent), `entry:`(present/absent/outdated), `onboarding:`(방식·판단 모델·날짜, 없으면 `needed`), `retro_apply:`를 출력한다 |
| `cx onboard detect [--json]` | 읽기 전용 탐지. 7절의 JSON |
| `cx onboard propose --routing <file> [--out <file>]` | 탐지 결과, 경로 지도, 등급표로 계획(8절)을 만든다. `--out`이 없으면 표준 출력 |
| `cx onboard apply --plan <file> [--dry-run]` | 계획을 검증하고 개인 설정 백업, 개인 설정 쓰기, 역할 동기화, 진입 블록 설치(`entry`가 `agents_md`일 때)를 한다 |
| `cx entry install`, `remove`, `status` | 전역 AGENTS.md의 표지 블록을 넣거나 빼거나 확인한다. 넣고 빼기 전에 `<state>/local/backups/`에 AGENTS.md 사본을 남긴다 |
| `cx overlay backup`, `restore --id <id>`, `list`, `verify --id <id>` | 개인 설정 백업과 복구. `verify`는 doctor 검사를 돌리고 실패하면 지정한 백업으로 되돌린 뒤 다시 검사하고 종료 코드 1을 낸다(13절) |
| `cx settings show`, `set <key> <value>` | `retro.apply`(`propose`·`notify`·`off`), `orchestrator`, `language`, `entry`만 바꾼다 |
| `cx uninstall [--dry-run]` | 진입 블록 제거, 생성된 역할 파일(첫 줄 `# generated by conductor roles sync`) 제거, config.toml의 `[agents.<역할>]` 등록 제거(백업 후). `<state>`는 지우지 않고 경로만 알린다 |
| `cx config convert-agents [--dry-run]` | config.toml 최상위 `agents = {...}` 한 줄 표를 같은 값의 `[agents]` 표로 바꾼다(14절). 온보딩이 동의를 받은 뒤 실행한다 |
| `cx --version` | 엔진 폴더의 `VERSION`을 출력한다. `doctor` 첫 줄 `engine`도 같은 값이다 |
| `cx skills off\|on\|list [대상] [--dry-run]` | config.toml 끝의 `# conductor:skills:begin v1` ~ `# conductor:skills:end` 구역에 `[[skills.config]]` `enabled = false` 항목을 넣고 뺀다. 대상은 스킬 이름이나 SKILL.md 경로다. 구역 밖은 바꾸지 않는다. 마지막 항목을 빼면 구역도 지운다. `uninstall`도 구역을 지운다 |
| `cx maintainer status\|install\|deploy --host <name> [--dry-run]` | settings의 `maintainer`가 있어야 한다. 저장소가 깨끗하고 브랜치가 설정과 같아야 한다. `install`은 `git archive`로 HEAD 사본을 꺼낸다. 그 사본을 임시 홈에서 테스트한 뒤 `receive`와 같은 설치를 한다. `deploy`는 이 컴퓨터에 설치한 커밋이 HEAD와 같아야 한다. 같은 사본을 원격 `<state>/staging/engine-<시각>`에 rsync로 보낸다. 그다음 원격에서 `receive`를 실행하고 그 경고를 `remote:` 줄로 보여 준다. 결과는 `<state>/engine.json`에 남긴다 |
| `cx maintainer receive --source <dir> [--commit <sha>]` | settings 없이 동작한다. 엔진 폴더를 설치하고 이전 폴더를 `<state>/backups/engine-<시각>`으로 옮긴다. 진입 블록이 `outdated`면 다시 넣는다. 아래 경우에는 이전 폴더와 설치 전 AGENTS.md 원문을 되돌린다. doctor가 실패하거나 중간에 멈춘 경우, 기록 저장이 실패한 경우다. 폴더 하나의 복구가 실패해도 나머지 복구를 계속하고 실패 목록을 출력한다. doctor의 FAIL이 환경 검사뿐이면 엔진을 유지하고 경고한다. 환경 검사는 `ocx`, `proxy`, 프록시 실패에 딸린 `routed models in catalog`다(13절 verify와 같다). 설치 전 진입 블록이 `absent`였으면 `AGENTS.md pointer`도 환경으로 본다. `~/.agents/skills/korean-protocol`이 있으면 KP 사본은 설치하지 않는다. `~/.codex/skills/korean-protocol`의 PINNED.json 버전이 저장소 사본보다 높아도 설치하지 않는다 |
| `cx roles sync` | 개인 설정의 roles.json(없으면 기본 예시)으로 역할 파일을 만든다 |
| `cx skills-audit [--remote] [--json]` | 스킬별 사용 횟수와 판정을 `<state>/audits/skills-<날짜>.json`에 남긴다. 행마다 `installed_commit`을 적는다. `--remote`이면 원본 최신 커밋과 비교해 `update`(`available`·`current`·`미확인`)를 적고 `counts.update_available`을 센다 |
| `cx skills delete <대상> [--yes] [--dry-run]` | 사용자가 승인한 스킬만 지운다. `--yes`가 없으면 계획만 보이고 멈춘다. 스킬 폴더는 `<state>/backups/skills-deleted-<시각>/`으로 옮기고, 링크 설치는 링크만 지운다. Conductor 전용 구역의 끄기 항목도 지우고 `<state>/skills-deleted.jsonl`에 기록한다. 엔진 스킬, `korean-protocol`, Codex 기본·플러그인 스킬, 두 스킬 폴더 밖의 대상은 거절한다. 보호 스킬 안의 하위 폴더도 거절한다. 링크를 지우면 그 끄기 항목을 스킬 폴더 안 원본 경로로 옮긴다. 지울 폴더를 가리키는 다른 링크는 계획에 `linked from`으로 보인다 |
| `cx skills-suggest [--days N] [--json]` | 설치·활성 스킬이 없는 경로(`gaps`)와 `poor`가 2건 이상이며 `good`보다 많은 스킬(`weak`)을 찾는다. 빈 경로에는 미설치 권장 도구를 붙인다. 스킬을 적지 않은 경로는 권장 도구가 있을 때만 보인다. 결과는 `<state>/audits/suggest-<날짜>.json`에 남기며 아무것도 설치하지 않는다 |
| `cx kp status [--remote]`, `cx kp update [--to vX.Y.Z] [--dry-run]` | 포함한 KP 사본(`~/.codex/skills/korean-protocol`, PINNED.json)과 따로 설치한 KP(`~/.agents/skills/korean-protocol`)의 버전을 보인다. `--remote`이면 원본의 `vX.Y.Z` 태그 가운데 가장 높은 것과 비교한다. 설치 기록에 원본 주소가 없으면 공개 KP 저장소를 본다. `update`는 포함한 사본만 바꾼다. 태그를 받아 같은 구성으로 만들고 이전 사본은 `<state>/backups/kp-<시각>/`으로 옮긴다. 따로 설치한 KP는 거절하고 그 저장소의 방법을 안내한다 |
| `cx lineup` | 살아 있는 모델과 등급표(패키지+개인)의 차이 |
| `cx aa-refresh` | 등급표의 `aa_slug`로 받는다. 비교 대상은 `<state>/aa/`의 직전 파일이다 |
| `cx doctor` | 9절 |
| `cx checkpoint ... --judge-calls <n>` | `--fable-calls`는 같은 값의 별칭으로 남긴다. 저장 키는 `judge_calls`이고 읽을 때 `fable_calls`도 받는다 |
| `cx profile set ... --by judge|self` | `fable`은 `judge`의 별칭이다 |

## 7. `onboard detect` JSON

    {
     "host": {"codex_home": "...", "default_model": "...", "agents_md": "...", "entry_block": "present|absent|outdated",
              "other_entry": [{"kind": "skill|hook|agents_md", "name": "...", "path": "...", "evidence": "..."}]},
     "ocx": {"path": "...", "version": "...", "proxy": "reachable|unreachable"},
     "models": ["..."],
     "quota": {}, "burn": {},
     "skills": [{"name": "...", "path": ".../SKILL.md", "description": "...", "origin": "local|native|external"}],
     "kp": {"installed": true, "path": "..."},
     "overlay": {"exists": false, "path": "..."},
     "mcp_servers": ["config.toml의 mcp_servers 이름"],
     "capabilities": [{"capability": "ui-ux", "recommended": "StyleGallery", "installed": [], "substitutes": ["..."],
                       "mcp": ["..."], "routes": ["ui-ux"], "install": {"how": "...", "command": "...", "docs": "..."}}],
     "unknown_models": ["등급표에 없는 살아 있는 모델"]
    }

- 스킬 설명은 SKILL.md frontmatter의 `description`만 읽는다. 본문은 읽지 않는다.
- `capabilities`는 `references/recommended-skills.json`의 팩마다 만든다. `substitutes`는 이름이나 설명이 팩의 `detect` 정규식에 맞는 다른 설치 스킬이다. 후보일 뿐이고, 온보딩이 설명을 근거로 사용자와 확정한다.
- `host.inline_agents`는 config.toml 최상위에 `agents = {...}` 한 줄 표가 있으면 `true`다.
- `other_entry`: `~/.codex/hooks.json`의 SessionStart 항목, 이름이 `using-superpowers`인 스킬, AGENTS.md에서 그 이름을 언급한 줄을 찾는다. 찾지 못했다는 것은 없다는 증명이 아니다.
- 실패한 항목은 `null`로 두고 전체 명령은 성공한다.

## 8. 계획(plan) JSON과 배정 규칙

    {"version": 1, "routing": {}, "roles": {"roles": {}}, "settings": {}, "notes": "", "entry": "agents_md",
     "warnings": ["임시 배정 이유, 등급표 밖 모델 등"]}

배정(`onboard propose`):

1. 역할마다 `tiers.json`의 `seats` 후보를 순서대로 보고, 살아 있는 모델 가운데 `constraints`를 어기지 않는 첫 후보를 고른다.
2. 후보가 없으면 같은 등급 이상의 살아 있는 모델 가운데 제약을 어기지 않는 모델을 고르고 `warnings`에 "임시"로 적는다. 강도는 `seat_kind`와 `default_effort`로 정한다(탐색 low, 구현 high, 검토·판단 xhigh).
3. 등급표 밖 모델은 자동 배정하지 않는다. `warnings`와 `notes`에 적는다.
4. 역할의 sandbox와 job은 기본 예시 roles.json의 값을 쓴다. 허용 스킬은 그 역할이 맡은 경로들의 스킬 합집합에, 기본 예시 역할의 허용 스킬 가운데 설치된 것을 더한다. `korean-protocol`이 설치돼 있으면 모든 역할에 넣는다.
5. `settings.orchestrator`는 `seats.orchestrator`의 첫 살아 있는 후보다.

작성자 모델 구성(Terra, Sol, Astra, Fable, Opus가 모두 살아 있음)을 넣으면 작성자의 현행 배정과 같은 모델·강도가 나와야 한다. 이것을 테스트로 고정한다. 기대값은 `tests/` 안의 고정 자료로 둔다.

## 9. doctor

| 검사 | 실패 시 |
|---|---|
| `ocx` 실행 파일 | FAIL (필수 의존) |
| 프록시 응답 | FAIL |
| routing.json, roles.json 형식 | FAIL |
| 역할 모델이 살아 있는 모델 목록에 있음 | FAIL |
| OpenCodex 서브에이전트 후보 목록 | WARN |
| advisor 설정 | INFO |
| 기본 모델과 `settings.orchestrator` 일치 | WARN |
| `entry`가 `agents_md`인데 블록 없음 | FAIL |
| KP 설치(`language`가 `ko`) | WARN |
| 역할 파일 상태, 경로 스킬 설치·허용 | WARN (현행과 같음) |
| 개인 설정 없음 | WARN "온보딩 전" |
| 웹 검색 사이드카(요청 경로별) | `anthropic`·`disabled`·설정 읽기 실패면 WARN |

## 10. 진입 블록

`skills/kp-conductor/references/entry-block.md` 파일 전체를 `<!-- conductor:begin v1 -->`와 `<!-- conductor:end -->` 사이에 넣는다. `status`의 `outdated`는 블록 내용과 파일 내용의 sha256 비교로 정한다. 블록 밖의 내용은 바꾸지 않는다.

진입 블록에는 KP 버전을 적지 않는다. KP 사본을 바꿔도 진입 블록은 `outdated`가 되지 않는다.

## 11. 회고 안전 장치

- 회고는 `<state>/local/`의 파일만 고친다. 설치된 엔진 파일(SKILL.md, references, scripts)은 고치지 않는다.
  - 예외: `maintainer`와 `retro.apply=notify`가 함께 있으면 엔진 변경을 로컬 저장소에 커밋한다. 설치는 `cx maintainer install`과 `deploy`로 한다. 설치된 폴더를 직접 고치지 않는다.
- 고치기 전에 `cx overlay backup`으로 백업 id를 받고, 고친 뒤 `cx overlay verify --id <id>`를 실행한다. verify가 실패하면 그 백업으로 되돌아간다.
- `retro.apply`가 `propose`이면 모든 변경을 `<state>/proposals/<날짜>.md`에 제안으로만 남긴다.
- `retro.apply`가 `off`이면 그 컴퓨터는 회고하지 않는다.
- 스킬 도태의 기본 처리는 끄기다. 삭제는 사용자가 명시 승인한 때만 `cx skills delete --yes`로 한다. maintainer 모드도 같다.
- 엔진 문제(규칙 빈틈, 코드 결함, 기본 틀 결함)는 `<state>/proposals/upstream-<날짜>.md`에 남긴다. 외부로 보내지 않는다. 공개 저장소 반영은 사람이 고른다.
- 실행 계기는 두 가지다. 하나는 Codex 앱 예약 작업이고, 온보딩이 사용자 동의를 받아 만든다. 다른 하나는 `cx status`의 `retro: due`이고, 그 세션의 요청을 끝낸 뒤 실행한다.

## 12. 개인 정보

`tests/test_public_hygiene.py`가 저장소의 개인 식별 문자열을 검사한다. 영수증, 체크포인트, 프로필, 회고 보고, AA 수치 파일은 저장소에 넣지 않는다.

## 13. 검토 반영 계약 (1차 독립 검토 뒤 추가)

### 파일 쓰기

- 모든 쓰기는 대상과 같은 폴더의 임시 파일에 쓰고 `os.replace`로 바꾼다. 원래 파일의 권한을 유지한다.
- 쓰기 직전에 대상의 해시를 다시 읽어 처음 읽은 값과 비교한다. 다르면 쓰지 않고 "다른 작업이 파일을 바꿨다"로 중단한다.
- 잠금은 CLI 진입점에서 상태를 바꾸는 명령마다 한 번만 잡는다. 대상은 onboard apply, entry install/remove, overlay backup/restore/verify, settings set, roles sync, uninstall, backup이다. `<state>/.lock`에 `fcntl.flock` 배타 잠금을 쓴다. 내부 함수는 잠금을 다시 잡지 않는다. Windows는 지원하지 않는다고 README에 적는다.
- Conductor 소유 경로(`<state>` 아래 모든 경로와 그 상위 구성 요소, `~/.codex/agents`와 생성 역할 파일)에 심볼릭 링크가 있으면 쓰지 않고 중단한다.
- 사용자 파일(`AGENTS.md`, `config.toml`)이 심볼릭 링크이면 링크를 바꾸지 않고 실제 대상 경로를 해석해, 그 폴더에 임시 파일을 만들고 원자적으로 바꾼다. 해시도 실제 대상으로 계산한다. 출력에 실제 대상 경로를 적는다.

### 복구 단위

- 복구는 Conductor 소유 단위만 되돌린다. 단위는 개인 설정 파일, 생성 역할 파일, 표지가 있는 `[agents.<역할>]` 등록, AGENTS.md의 Conductor 블록이다. 사용자 파일 전체를 덮어쓰지 않는다.
- 사용자 파일 전체 복구는 현재 해시가 Conductor가 마지막으로 쓴 해시와 같을 때만 한다. 다르면 "복구하지 않음"으로 보고한다.
- 여러 단위를 바꾸는 명령은 시작 전에 한 백업을 만든다. 실패하면 단위마다 복구를 시도하고, 한 단위가 실패해도 나머지를 계속 복구한다. 복구하지 못한 단위를 모두 출력하고 종료 코드 1로 끝낸다. 작업 기록(journal)은 v1에서 만들지 않는다.
- 백업 폴더는 0700, 백업 파일은 0600 권한이다. `config.toml`에 비밀값이 있을 수 있다고 README에 적는다.

### 역할 등록

- Conductor 소유 등록은 둘 중 하나다. 바로 위 줄에 `# conductor:role` 표지가 있거나, `config_file`이 첫 줄에 생성 표지가 있는 역할 파일을 가리킨다. 두 번째 경우는 소유로 인정하고 표지를 붙이지 않은 채 그대로 둔다(14절).
- 등록하려는 역할 이름이 소유가 아닌 곳에 나오면 쓰기 전에 중단한다. 검사 대상은 `agents` 맥락 전체다: `[agents.<역할>]`, 따옴표나 공백이 들어간 표 이름, `[agents]` 표 안의 키, 최상위 `agents = {...}`, 점 표기 키. 중단할 때는 사용자가 직접 넣을 등록 문구를 출력한다.
- Python 3.11 이상이면 `tomllib`로 원본과 저장 후보를 읽는다. 두 결과에서 Conductor 소유 역할을 뺀 나머지가 같아야 저장한다. 3.9~3.10에서는 위 검사로 의심되면 중단한다.
- `uninstall`은 소유 등록만 지우고, `config_file`이 생성 역할 파일을 가리키는지 확인한다. 역할 파일은 첫 줄이 생성 표지인 것만 지운다.

### 개인 설정 백업과 검증

- `overlay backup`은 개인 설정 파일, 생성 역할 파일, 소유 등록, Conductor 블록을 백업한다. 출력 첫 줄은 `overlay backup: <id>`다.
- `overlay verify`에는 `--id <id>`가 필수다. 검사가 실패하면 그 백업으로 복구하고, 복구 뒤 검사를 다시 돌려 결과를 출력한다. 종료 코드는 1이다.
- 환경 검사만 실패하면 복구하지 않는다. 환경 검사는 `ocx`, `proxy`, 프록시가 닫혀 생긴 `routed models in catalog`다. 이때 "overlay not restored"를 출력하고 종료 코드 2로 끝낸다.
- 단 고른 백업과 현재 개인 설정의 `ocx`나 `proxy` 값이 다르면, 그 검사 실패는 개인 설정 탓으로 보고 복구한다.
- `overlay restore`와 verify의 복구는 먼저 현재 상태를 새 백업으로 남긴다. 출력은 "복구 전 상태 백업: <id>"다. 잘못 복구했으면 이 ID로 다시 되돌린다.
- `settings set`은 새 백업을 만들지 않는다.
- 검사는 tiers.json의 자료형(`models` 객체, `seats` 객체와 후보 쌍, `constraints` 목록)과 roles.json의 값(강도 `minimal|low|medium|high|xhigh|max|ultra`, sandbox `null|read-only|workspace-write|danger-full-access`)을 확인한다. 선언 설정과 생성 역할 파일의 모델·강도가 다르면 실패다.
- `cx backup`은 엔진 폴더와 함께 위 소유 단위, `config.toml` 사본, 전역 `AGENTS.md` 사본을 백업한다. 출력에 보존 범위를 적는다.

### 진입과 개인 정책

- `onboard apply`에서 `entry`가 `none`이면 기존 Conductor 블록을 제거한다. 블록 밖 내용은 보존한다.
- 스킬 금지 목록은 `settings.json`의 `banned_skills`(fnmatch 패턴 목록, 기본 빈 목록)다. 엔진에 특정 도구 금지를 고정하지 않는다. 감사의 `banned` 판정은 이 목록으로만 한다.
- 회고의 `retro.apply`가 `propose`이면 프로필 평가 결과도 `cx profile set`으로 쓰지 않고 제안 파일에 적는다.

## 14. 2차 검토 반영 계약

### config.toml 등록 구역

- Conductor가 새로 추가하는 등록은 config.toml 맨 끝의 표지 구역 안에만 둔다. 구역은 `# conductor:roles:begin v1` 줄로 시작하고 `# conductor:roles:end` 줄로 끝난다. 구역 안에는 `[agents.<역할>]` 표와 `config_file` 줄만 둔다.
- `roles sync`는 구역을 다시 만든다. 지우는 것은 두 표지 줄과 구역 안의 소유 역할 표뿐이다. 새 구역은 파일 끝에 붙인다.
- 다른 도구가 구역 안에 넣은 표는 제자리에 남긴다. 예를 들어 `codex mcp add`는 끝 표지 앞에 `[mcp_servers.<이름>]`을 넣는다.
- 구역 밖은 한 글자도 바꾸지 않는다. 줄바꿈은 파일의 기존 방식(LF 또는 CRLF)을 따른다.
- `uninstall`과 복구도 같은 규칙을 따른다. 구역에 소유 역할이 남지 않으면 표지 줄만 지운다.
- 구역 안에 있다는 것만으로 소유가 되지 않는다. 구역 안 표는 세 조건을 모두 만족할 때만 소유다.
  - 표 모양이 `[agents.<역할>]`과 `config_file` 한 줄뿐이다.
  - `config_file`이 `~/.codex/agents/` 폴더 안을 가리킨다.
  - 그 자리에 생성 표지가 없는 사용자 파일이 없다.
- 조건을 벗어난 구역 안 표는 사용자 것으로 보고 제자리에 남긴다.
- 지울 구간이 서로 겹치면 쓰기 전에 중단한다. 예를 들어 시작 표지가 역할 표 중간에 있으면 겹친다. 안내문은 config.toml을 직접 고치라고 알린다.
- 소유 역할 표에 `config_file` 말고 다른 키가 있으면 사용자가 쓴 내용으로 본다. 그 표를 지우거나 바꿔야 하면 쓰기 전에 중단한다. 안내문은 그 키를 다른 표로 옮기라고 알린다.
- 저장 후보 검사 규칙은 두 Python 경로가 같다. 소유 역할을 뺀 나머지가 같아야 저장한다.
  - 3.11 이상은 `tomllib`로 읽는다.
  - 3.9~3.10은 스캐너로 선언 목록을 읽는다.
  - `[[agents]]` 배열이 있으면 두 경로 모두 충돌로 보고 중단한다.
- 구역 밖에 있지만 `config_file`이 생성 역할 파일을 가리키는 등록은 이전 버전이 만든 소유 등록이다. 그대로 두고, 구역에 같은 역할을 중복으로 넣지 않는다. `uninstall`은 이 등록도 지운다.
- 충돌 판단은 TOML 구조로만 한다. Python 3.11 이상은 `tomllib`로 `agents` 표의 실제 키를 읽는다. 3.9~3.10은 줄 단위 스캐너를 쓴다. 스캐너는 주석, 한 줄 문자열, 여러 줄 문자열(`"""`, `'''`) 안의 내용을 무시한다. 표 머리글, `[agents]` 표 안의 키, 점 표기 키, 최상위 `agents = {...}`만 본다. LF와 CRLF를 모두 처리한다.
- 소유가 아닌 같은 이름의 역할이 있으면 쓰기 전에 중단하고, 사용자가 직접 넣을 등록 문구를 출력한다.
- 최상위 `agents = {...}` 한 줄 표는 TOML 규칙상 뒤에서 확장할 수 없다. `cx config convert-agents`가 같은 값의 `[agents]` 표로 바꾼다.
  - 원래 줄을 지우고 표를 파일 끝(등록 구역이 있으면 그 앞)에 둔다. 줄 끝 주석은 표 바로 위로 옮긴다.
  - 바꾸기 전후 값이 같은지 검사한다. 3.11 이상은 `tomllib`, 3.9~3.10은 스캐너로 비교하고, 다르면 쓰지 않는다.
  - 쓰기 전에 개인 설정 백업에 config.toml 사본을 남기고, 실패하면 되돌린다.

### 실패 명령의 자동 복구

- 상태를 바꾸는 명령이 실패하면, 그 명령이 실제로 쓴 단위만 되돌린다. 아직 쓰지 않은 단위는 건드리지 않는다.
- 되돌리기 전에 단위의 현재 해시를 그 명령이 마지막으로 쓴 해시와 비교한다. 다르면 그 사이 다른 작업이 바꾼 것이므로 보존하고 "복구하지 않음(외부 변경)"으로 보고한다.
- 명시적 `overlay restore`와 `overlay verify --id`의 복구는 사용자가 고른 백업으로 소유 단위를 되돌린다. 이때도 사용자 파일 전체 복구는 13절의 해시 조건을 따른다.
- 복구는 시작 전에 모든 등록의 소유권을 확정하고, 그 판정을 복구 끝까지 유지한다. 생성 파일을 지우기 전에 그 파일을 가리키는 소유 등록을 먼저 처리한다. 끝나면 지운 생성 파일을 가리키는 등록이 남지 않았는지 확인한다.
- 바뀐 것이 없는 단위는 "변경 없음"으로 보고한다. "복구하지 않음"은 외부 변경으로 보존한 경우에만 쓴다.
- Ctrl-C(`KeyboardInterrupt`)로 멈춘 명령도 같은 방식으로 되돌린다.
- `roles sync`는 config.toml 옆에 별도 사본을 만들지 않는다. 사본은 0700 개인 설정 백업 안에만 둔다. config.toml이 dotfiles 폴더로 가는 링크일 때 그 폴더에 비밀값 사본이 쌓이지 않게 한다.
- `entry remove`로 블록을 지운 결과가 빈 파일이면 AGENTS.md를 지운다. 그 파일은 `entry install`이 만든 것이기 때문이다. 링크인 AGENTS.md는 지우지 않고 빈 내용으로 쓴다.
- 잠금은 `<state>` 단위다. `CONDUCTOR_HOME`을 서로 다르게 준 두 실행은 같은 `~/.codex`를 두고 서로 막지 않는다. 일반 설치는 `<state>`가 하나라서 해당하지 않는다.
