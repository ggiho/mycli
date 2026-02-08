# mycli 개발 가이드

## 개요

이 저장소는 [dbcli/mycli](https://github.com/dbcli/mycli)를 기반으로 다음을 개선한 버전입니다:

- 코드 아키텍처 리팩토링 (MyCli God Class 분리)
- 자동완성 기능 강화 (CTE, Window Function, JSON Path)
- 보안 개선 (SQL Injection 방지, 비밀번호 마스킹)
- 성능 최적화 (커넥션 재사용, 메타데이터 쿼리 병합)
- UX 개선 (진행 표시, 에러 메시지 개선)

## 설치

### 요구사항

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (권장) 또는 pip

### uv로 개발 환경 설정

```bash
# 저장소 클론
git clone https://github.com/ggiho/mycli.git
cd mycli

# 가상환경 생성 및 개발 모드 설치
uv venv
uv pip install -e ".[ssh,llm]"

# 활성화
source .venv/bin/activate

# 실행
mycli -h localhost -u root
```

### 선택적 의존성

```bash
# SSH 터널링 지원
uv pip install -e ".[ssh]"

# LLM 기능 지원 (AI SQL 생성)
uv pip install -e ".[llm]"

# 전체 설치
uv pip install -e ".[all]"
```

## 사용법

### 기본 사용

```bash
# 로컬 MySQL 접속
mycli -u root

# 원격 접속
mycli -h hostname -u username -p password database

# DSN 형식
mycli mysql://user:password@host:port/database
```

### 새로 추가된 기능

#### 1. JSON Path 자동완성

JSON 컬럼 작업 시 자동완성 지원:

```sql
SELECT data->'$.<Tab>        -- JSON 경로 제안
SELECT JSON_EXTRACT(data, '$.<Tab>  -- JSON 함수에서도 작동
```

제안되는 패턴:
- `'$'` - JSON root
- `'$.'` - object property
- `'$[*]'` - all array elements
- `'$[0]'` - first array element
- `'$.key'` - property access
- `'$**.key'` - recursive descent

#### 2. CTE (WITH절) 자동완성

```sql
WITH user_stats AS (
    SELECT user_id, COUNT(*) as cnt FROM orders GROUP BY user_id
)
SELECT * FROM user_<Tab>  -- user_stats 제안됨
```

#### 3. Window Function 자동완성

```sql
SELECT name,
       ROW_NUMBER() OVER (<Tab>  -- PARTITION BY, ORDER BY 등 제안
FROM employees
```

#### 4. 컬럼 타입 힌트

자동완성 시 컬럼 타입 표시:

```
id          int
name        varchar(100)
created_at  datetime
data        json
```

#### 5. LLM SQL 생성 (옵션)

```sql
\llm 최근 7일간 가장 많이 주문한 고객 5명을 찾아줘
```

위험한 SQL 생성 시 경고 표시:
```
⚠️  Warning: The generated SQL contains 'DELETE'. Please review carefully before executing.
```

## 프로젝트 구조

```
mycli/
├── main.py              # CLI 엔트리포인트
├── output_mixin.py      # 출력/포맷팅 메서드
├── connection_mixin.py  # DB 연결 관리
├── cliloop_mixin.py     # 대화형 루프/명령 처리
├── constants.py         # 공유 상수
├── query_utils.py       # 쿼리 유틸리티
├── sqlexecute.py        # SQL 실행 엔진
├── sqlcompleter.py      # 자동완성 엔진
├── completion_refresher.py  # 메타데이터 새로고침
└── packages/
    ├── completion_engine.py  # 완성 컨텍스트 분석
    ├── sqlparse_config.py    # sqlparse 설정
    └── special/
        ├── iocommands.py     # I/O 명령어
        └── llm.py            # LLM 통합
```

## 개발

### 코드 수정 후 테스트

개발 모드(`-e`)로 설치했으므로 코드 수정 시 바로 반영됩니다:

```bash
# 코드 수정 후 바로 실행
mycli -u root

# 또는 모듈로 실행
python -m mycli.main -u root
```

### 테스트 실행

```bash
# pytest 설치
uv pip install pytest pytest-cov

# 테스트 실행
pytest test/

# 커버리지 포함
pytest --cov=mycli test/
```

### 문법 검사

```bash
python -c "import ast; ast.parse(open('mycli/main.py').read())"
```

## 개선 사항 목록

### 완료됨

#### 보안
- [x] SQL Injection 방지 (parameterized queries)
- [x] 비밀번호 로깅 마스킹
- [x] LLM 생성 위험 SQL 경고

#### 성능
- [x] 메타데이터 새로고침용 커넥션 재사용
- [x] 메타데이터 쿼리 병합 (views JOIN, all_columns 통합)
- [x] 핫 루프 regex 모듈 레벨 이동

#### 기능
- [x] CTE (WITH절) 자동완성
- [x] Window Function 자동완성
- [x] JSON Path 자동완성
- [x] 컬럼 타입 힌트 표시

#### 코드 품질
- [x] MyCli God Class → 3개 mixin으로 분리
- [x] cli() 함수 426→212줄 축소
- [x] IOState 클래스로 전역 변수 캡슐화
- [x] sqlparse 설정 중앙화
- [x] bare except → DEBUG 로깅
- [x] assert isinstance → 런타임 체크

#### UX
- [x] 메타데이터 리프레시 진행 표시
- [x] 재접속 재귀호출 스택오버플로우 수정
- [x] SSH import 실패 시 명확한 에러 메시지

### 미완료 (대규모 작업)

- [ ] 플러그인/확장 시스템
- [ ] 듀얼 파서 통합
- [ ] 테스트 커버리지 확대
- [ ] 쿼리 실행 계획 시각화
- [ ] 커넥션 풀링

## 라이선스

BSD-3-Clause (원본 mycli와 동일)
