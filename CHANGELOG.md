# 변경 기록

KP Conductor의 버전별 릴리스 노트다.
규칙은 [RELEASING.md](RELEASING.md)를 따른다.

## [0.2.0] - 2026-11-17

### 추가

- 버전 규칙(RELEASING.md)과 변경 기록(CHANGELOG.md)을 추가했다.
- README에 요청 하나가 처리되는 사용 흐름 도식을 추가했다.
- `main`에 push하면 GitHub Actions가 테스트와 버전 검사를 실행한다. 둘 다 통과하면 태그와 GitHub 릴리스가 생긴다.
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
