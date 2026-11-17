# 변경 기록

KP Conductor의 버전별 릴리스 노트다.
규칙은 [RELEASING.md](RELEASING.md)를 따른다.

## [0.3.0] - 2026-11-17

### 추가

- 주간 회고가 스킬 감사와 부족한 스킬 제안, KP 새 버전 확인을 실행한다. 회고 절차서에 세 단계를 넣었다.
- `cx skills-audit --remote`가 원본 최신 커밋과 설치된 커밋을 비교한다. 다르면 갱신 후보(`update: available`)로 표시한다.
- `cx skills delete <대상> --yes`를 추가했다. 사용자가 승인한 스킬만 지운다. `--yes`가 없으면 계획만 보이고 멈춘다.
- 복사로 설치한 스킬은 지울 때 `<state>/backups/`에 복구용 사본을 남긴다. 링크로 설치한 스킬은 링크만 지운다.
- `cx skills-suggest`를 추가했다. 설치된 스킬이 없는 경로와 성과가 낮은 스킬을 찾아 권장 도구를 붙인다. 설치는 하지 않는다.
- `cx kp status --remote`와 `cx kp update`를 추가했다. 포함한 KP 사본을 KP 릴리스 태그에서 다시 만들어 바꾼다. 이전 사본은 백업한다.
- 저장소 관리자용 `tools/sync_kp.py`를 추가했다. KP 릴리스 태그에서 `skills/korean-protocol/`을 다시 만든다.
- `retro.apply=off`를 추가했다. 엔진을 배포받기만 하는 보조 컴퓨터의 회고를 끈다. 이때 `cx retro`는 실행을 거절한다.
- 경로 정의에 제외어(`exclude`)를 추가했다. 제외어가 요청에 있으면 그 경로를 고르지 않는다.

### 변경

- 도태한 스킬의 기본 처리는 끄기다. 삭제는 사용자가 명시 승인한 때만 한다. maintainer 모드도 같다.
- README 필요 조건에 OpenCodex 저장소 링크를 넣었다.
- 진입 블록에서 KP 버전 숫자를 뺐다. KP를 바꿔도 진입 블록은 그대로다.
- `cx maintainer install`과 `deploy`가 `cx kp update`로 올린 더 새로운 KP를 덮어쓰지 않는다.

### 수정

- 예약 회고가 스킬 감사를 건너뛸 수 있던 문제를 고쳤다. 온보딩이 만든 예약은 회고 절차서만 읽는데, 절차서에 감사 단계가 없었다.
- "플로우차트" 요청이 `차트` 키워드 때문에 데이터 분석 경로로 가던 문제를 고쳤다. 데이터 경로에 흐름도 계열 단어 3개를 제외어로 넣었다.

### 호환성 변경

- 진입 블록 문구가 바뀌어 `cx status`가 `entry: outdated`를 보인다. `cx entry install`을 다시 실행한다.
- 개인 경로 지도(`<state>/local/routing.json`)는 엔진 갱신으로 바뀌지 않는다. 데이터 경로에 같은 제외어를 직접 넣는다.

## [0.2.0] - 2026-11-17

### 추가

- 버전 규칙(RELEASING.md)과 변경 기록(CHANGELOG.md)을 추가했다.
- README에 요청 하나가 처리되는 사용 흐름 도식을 추가했다.
- `main`에 push하면 GitHub Actions가 테스트와 버전 검사를 실행한다. 둘 다 통과하면 태그와 GitHub 릴리스가 생긴다.
  테스트는 Ubuntu(Python 3.9·3.12)와 macOS에서 실행한다.
- `main` push 전에 버전과 릴리스 노트를 검사하는 pre-push 훅을 추가했다.
- `cx --version`으로 설치한 엔진 버전을 확인한다. `cx doctor`도 `engine` 줄에 버전을 보여준다.
- `cx skills off|on|list`로 스킬을 끄고 켠다. `config.toml`의 Conductor 전용 구역만 고친다.
- 엔진 관리자용 `cx maintainer status|install|deploy`를 추가했다.
- `cx maintainer install`은 HEAD 커밋의 파일만 꺼내 테스트한 뒤 이 컴퓨터에 설치한다.
- `cx maintainer deploy`는 같은 파일을 등록한 원격 컴퓨터에 설치한다.
- 설치 전 엔진 폴더는 백업한다. 설치 뒤 검사가 실패하면 이전 엔진으로 되돌린다.
- `settings.json`에 `maintainer` 항목을 추가했다. 저장소 경로와 브랜치, 배포 대상을 적는다.
- `maintainer`가 있고 `retro.apply=notify`이면 회고가 엔진 변경을 로컬 저장소에 커밋하고 설치·배포한다.
- 같은 조건에서 회고가 도태하거나 금지된 스킬을 `cx skills off`로 끈다. GitHub push는 회고가 하지 않는다.

### 변경

- `cx uninstall`이 `cx skills off`로 끈 스킬을 다시 켠다.
- 진입 블록이 평소에는 KP 전문을 읽지 않게 했다. 기본 규칙은 블록 안에 있다.
  시범 요청에서 요청마다 KP 전문을 읽어 토큰이 늘던 원인이었다.

### 수정

- KP를 `~/.agents/skills`에만 설치한 환경의 경로 오류를 고쳤다. 진입 블록과 엔진 문서가 없는 경로를 가리켰다.
  이제 `~/.codex/skills`에 KP가 없으면 `~/.agents/skills`의 KP를 쓴다.

### 호환성 변경

- 진입 블록 문구가 바뀌어 `cx status`가 `entry: outdated`를 보인다. `cx entry install`을 다시 실행한다.

## [0.1.0] - 2026-11-17

첫 공개판이다.

### 추가

- 진입 스킬 `kp-conductor`를 공개했다. 요청을 분류해 처리 방식과 맡길 역할을 정한다.
- 온보딩 스킬 `kp-conductor-onboard`를 공개했다. 환경을 탐지해 경로 지도와 역할 배정을 만든다.
- 온보딩은 사용자 승인을 받은 뒤에만 설정을 쓴다.
- 주간 회고가 작업 기록으로 개인 설정 변경을 제안한다. `retro.apply=notify`이면 일부를 바로 적용한다.
- 공급사별 한도와 소진 속도에 따라 위임을 줄인다(`settings.quota`).
- Korean Protocol v0.11.1 고정 사본과 `kp-lint` 검사를 포함했다.
- 권장 도구 목록을 추가했다. 같은 일을 하는 도구가 이미 있으면 그 도구를 쓴다.
- 개인 설정을 백업하고 복구한다.
- 진입 블록을 설치하고 제거한다.
- `cx uninstall`로 Conductor 등록을 제거한다.
