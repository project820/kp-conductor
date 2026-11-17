# 버전과 릴리스 규칙

KP Conductor는 `main`에 push할 때마다 새 버전을 하나 낸다.
버전마다 사용자가 읽을 릴리스 노트를 붙인다.

## 버전 번호

버전은 `MAJOR.MINOR.PATCH` 형식이다.
형식은 [Semantic Versioning 2.0.0](https://semver.org/lang/ko/)을 따른다.
Git 태그와 GitHub 릴리스 이름은 앞에 `v`를 붙인다(`v0.2.0`).

첫 공개판(커밋 `7959609`)이 `v0.1.0`이다.
1.0.0 전까지는 알파 단계다.

| 바뀐 내용 | 올리는 자리 | 예 |
|---|---|---|
| 새 기능, 명령 추가, 기존 동작 변경 | MINOR | 0.2.0 → 0.3.0 |
| 설정 형식, 명령 이름, 설치 위치의 호환성 변경 | MINOR | 0.2.0 → 0.3.0 |
| 오류 수정, 문서·테스트 수정, 등급표·권장 목록 자료 갱신 | PATCH | 0.2.0 → 0.2.1 |
| 포함한 Korean Protocol 사본 교체 | 바뀐 KP 내용에 따라 MINOR 또는 PATCH | 0.2.0 → 0.3.0 |
| 정식판 선언 | MAJOR | 0.9.0 → 1.0.0 |

MINOR를 올리면 PATCH는 0으로 되돌린다.
1.0.0은 설정 형식과 명령을 고정할 때 저장소 관리자가 정한다.
1.0.0 뒤의 호환성 변경은 MAJOR를 올린다.

포함한 Korean Protocol은 자체 버전을 따로 가진다([PINNED.json](skills/korean-protocol/PINNED.json)).
KP 사본을 바꾸면 Conductor 버전도 올린다.

KP 사본은 KP 저장소의 릴리스 태그에서 만든다. 손으로 고치지 않는다.

```sh
python3 tools/sync_kp.py --repo <KP 저장소 경로나 주소> --tag vX.Y.Z --dry-run
python3 tools/sync_kp.py --repo <KP 저장소 경로나 주소> --tag vX.Y.Z
```

이미 설치한 사용자는 `cx kp status --remote`로 새 KP를 확인하고 `cx kp update`로 바꾼다.

이전 사본은 git 기록에 남는다. KP 버전 문자열은 아래 파일에도 있다. 함께 고친다.

- `README.md`의 라이선스 절
- `NOTICE.md`
- `DESIGN.md`의 저장소 구조
- `skills/kp-conductor/scripts/kp_lint.py`의 `BASIS`

진입 블록(`references/entry-block.md`)이 바뀌면 기존 설치는 `entry: outdated`가 된다.
이때는 릴리스 노트에 `### 호환성 변경` 절을 두고 `cx entry install` 재실행을 적는다.

진입 블록에는 KP 버전을 적지 않는다. KP를 바꿔도 진입 블록은 그대로다.

## 버전을 적는 곳

| 파일 | 내용 |
|---|---|
| [skills/kp-conductor/VERSION](skills/kp-conductor/VERSION) | 현재 버전 한 줄이다. 버전의 유일한 원본이다. |
| [CHANGELOG.md](CHANGELOG.md) | 버전별 릴리스 노트다. 새 버전을 맨 위에 쓴다. |

`VERSION` 파일은 엔진 폴더와 함께 설치된다.
설치한 버전은 `cx --version`으로 확인한다.
`cx doctor`의 `engine` 줄에도 나온다.

## push 절차

1. `skills/kp-conductor/VERSION`의 버전을 올린다.
2. `CHANGELOG.md` 맨 위에 새 버전 절을 쓴다.
3. 커밋한다.
4. `python3 tools/release.py check`를 실행한다. 검사는 커밋된 파일을 읽는다.
5. `main`에 push한다.

아직 push하지 않은 버전 절이 맨 위에 있으면 버전을 다시 올리지 않는다.
그 절에 항목을 덧붙이고 날짜를 push하는 날로 고친다.

push 뒤에는 GitHub Actions가 테스트와 버전 검사를 실행한다.
둘 다 통과하면 태그 `vX.Y.Z`와 GitHub 릴리스를 만든다.
릴리스 본문은 `CHANGELOG.md`의 해당 절이다.

## 릴리스 노트 쓰는 법

절 제목은 `## [X.Y.Z] - YYYY-MM-DD` 형식이다.
날짜는 push하는 날의 한국 날짜다.
본문에는 아래 소제목 가운데 해당하는 것만 쓴다.

| 소제목 | 쓰는 내용 |
|---|---|
| `### 추가` | 새 기능과 명령 |
| `### 변경` | 기존 동작이 바뀐 점 |
| `### 수정` | 고친 오류 |
| `### 제거` | 없어진 기능과 파일 |
| `### 호환성 변경` | 이전 판 사용자가 할 일과 옮기는 방법 |

- 항목 하나에 사용자가 겪는 결과 하나를 쓴다. 함수 이름보다 명령과 동작을 적는다.
- Korean Protocol의 문서체("한다")를 따른다.
- 개인 경로, 계정, 호스트 이름을 쓰지 않는다.
- 절마다 `- ` 항목이 하나 이상 있어야 한다.

## 자동 검사

| 위치 | 실행 시점 | 하는 일 |
|---|---|---|
| `tools/release.py check` | 커밋 뒤 push 전에 직접 실행 | 아래 규칙을 검사한다. |
| `.githooks/pre-push` | `main` push 직전 | 같은 규칙을 원격 태그 기준으로 검사한다. |
| `.github/workflows/release.yml` | push와 PR | 테스트와 검사를 실행한다. push이면 릴리스를 만든다. |

### 검사 규칙

- `VERSION`은 `MAJOR.MINOR.PATCH` 한 줄이다.
- `CHANGELOG.md` 맨 위 절의 버전은 `VERSION`과 같다.
- 절은 최신 버전부터 내림차순이다. 날짜는 거꾸로 가지 않는다.
- 같은 버전 태그가 다른 커밋에 있으면 실패한다. 버전을 올리지 않은 push다.
- 버전은 이 커밋 이전의 가장 최근 릴리스보다 커야 한다.
- 나중 커밋의 릴리스가 먼저 나와도 앞선 커밋의 릴리스는 막지 않는다.

검사는 커밋된 파일만 읽는다.
커밋하지 않은 수정은 결과에 영향을 주지 않는다.

pre-push 훅은 저장소를 복제할 때마다 한 번 켠다.

```sh
git config core.hooksPath .githooks
```

## 실패했을 때

Actions가 실패하면 릴리스를 만들지 않는다.
push한 커밋은 `main`에 그대로 남는다.

네트워크 오류 같은 일시 실패는 Actions 화면에서 같은 실행을 다시 돌린다.
이미 릴리스된 커밋이면 다시 만들지 않고 통과한다.

태그가 생기지 않은 버전은 같은 번호로 다시 push해도 된다.
고친 내용은 그 버전의 절에 덧붙인다.

한 번 공개한 태그는 옮기거나 지우지 않는다.
잘못 공개한 버전은 다음 PATCH 버전에서 고친다.

## 외부 기여

PR도 같은 규칙을 따른다.
PR에 버전 올림과 릴리스 노트를 함께 넣는다.
병합하면 `main` push와 같이 릴리스가 생긴다.

PR을 연 뒤 다른 릴리스가 먼저 나가면 PR의 버전이 낡는다.
그대로 병합하면 검사가 실패하고 릴리스가 생기지 않는다.
병합 전에 관리자가 버전과 날짜를 새로 맞춘다.
