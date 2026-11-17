# 제3자 저작물 고지

**이 저장소에는 MIT인 Korean Protocol v0.11.1을 포함한다.**
Conductor 자체의 이용 허락은 루트 [LICENSE](LICENSE)를 따른다.

## 포함한 Korean Protocol

| 항목 | 값 |
|---|---|
| 원본 | https://github.com/project820/korean-protocol |
| 버전 | `v0.11.1` |
| 고정 커밋 | `564c68f76e961e00e20cc2261b502f53cf0112b4` |
| 라이선스 | MIT |
| 저작권 | Copyright (c) 2026 Korean Protocol contributors |
| 포함 위치 | `skills/korean-protocol/` |

고정 정보는 [PINNED.json](skills/korean-protocol/PINNED.json)에 기록한다.
저작권 표시와 MIT 허가문은 [KP LICENSE](skills/korean-protocol/LICENSE)에 보존한다.

KP의 외부 자료 권리는 [KP NOTICE](skills/korean-protocol/NOTICE.md)를 따른다.

## 수정한 KP 코드

`skills/kp-conductor/scripts/kp_lint.py`는 KP 저장소 코드를 수정한 파일이다.
원본은 KP 커밋 `7810679`의 다음 경로에 있다.

```text
evaluation/kp-lean-pilot-20261113/candidate/scripts/kp_lint.py
```

Conductor 명령에서 쓰도록 수정했다.
문체 검사 기준은 Korean Protocol v0.11.1을 따른다.

이 파생 파일에도 KP의 MIT 저작권과 허가문을 적용한다.
KP의 전체 MIT 허가문은 위 KP LICENSE에 포함한다.

## 수치 자료와 권장 설치 대상

Artificial Analysis의 수치 원자료는 이 저장소에 포함하지 않는다.
등급표에는 모델 분류와 출처 식별 정보를 둔다.

필요한 원자료는 사용자가 `cx aa-refresh`로 받는다.
외부 자료의 이용 조건은 해당 원본을 따른다.

[recommended-skills.json](skills/kp-conductor/references/recommended-skills.json)은 원본 저장소 안내 목록이다.
권장 스킬의 파일은 이 저장소에 포함하지 않는다.

사용자는 각 원본에서 스킬을 받는다.

unslop의 원본은 https://github.com/heydamir/unslop 이다.
원본 저자는 Damir Sokolovsky다.

원본의 라이선스는 CC BY 4.0이다.

StyleGallery의 원본은 https://github.com/changeroa/StyleGallery 이다.
원본 저장소의 라이선스는 코드가 MIT, 문서가 CC BY 4.0이다(LICENSE, LICENSE-DOCS).
권장 설치본은 npm `stylegallery@0.1.19`이고, gitHead는 `0bf928f6269b1cb6cf24f04e0bf28a8ce2bbc0ff`다.
