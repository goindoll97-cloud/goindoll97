# Windows 데스크톱 버전 (PySide6)

기존 Streamlit 웹 버전(app.py, start.bat)과 별개로 제공되는 Windows 네이티브 창 버전입니다.
Streamlit 서버, 브라우저 또는 localhost 주소 없이 실행합니다.

## 구현된 기능

- CSV/XLSX/XLS 인벤토리 파일 선택
- 승인 규제 데이터 기반 인체등유해성·사고대비·제한·금지물질 판정
- PubChem 자동 구조조회 ON/OFF
- 로컬 AI 자동검토 ON/OFF: Ollama와 설정 모델이 설치되면 체크박스를 켜고 끌 수 있습니다. 없으면 체크박스가 보이지만 OFF로 비활성화되며 규칙 기반 판정은 작동합니다.
- 국가법령정보 API 최신성 확인 ON/OFF
- 작업 중 중앙 로딩 창과 진행 메시지
- 판정 결과·법률근거·신뢰 게이트·검색기록·포괄규제범위·AI 감사기록 탭
- Excel 자동 보존 및 다른 이름으로 저장, AI 오류·미처리 사례 재검토

규제 자동확정·보류 조건과 AI 보조 역할은 기존 판정 엔진을 그대로 사용합니다. 로컬 AI는 규제 판정 자동확정 권한을 가지지 않습니다.

## 개발용으로 실행

Windows PowerShell에서 새로운 클론 폴더를 권장합니다. 기존 미추적 파일이 있는 폴더에서 무리하게 git pull하지 마세요.

~~~powershell
git clone https://github.com/goindoll97-cloud/goindoll97.git D:\chemical_regul_desktop
cd D:\chemical_regul_desktop
conda create -n regdesktop python=3.12 -y
conda activate regdesktop
python -m pip install -r requirements-desktop.txt
python desktop_smoke_test.py
python desktop_app.py
~~~

Conda가 없다면 Python 3.12 환경에서 pip install 이하를 실행하면 됩니다.

## Windows EXE 만들기

개발 환경이 준비된 Windows 컴퓨터에서 build_desktop.bat을 더블클릭하거나 다음 명령을 실행합니다.

~~~powershell
python -m PyInstaller --noconfirm --clean desktop.spec
~~~

성공하면 다음 경로에 EXE가 생성됩니다.

~~~
dist\ChemicalRegulatoryDesktop\ChemicalRegulatoryDesktop.exe
~~~

**중요: EXE만 복사하지 말고 dist\ChemicalRegulatoryDesktop\ 폴더 전체를 복사해야 합니다.**
다른 Windows 컴퓨터에서는 이 폴더에서 EXE를 더블클릭하면 됩니다. PyInstaller는 Python 런타임과 패키지를 폴더에 포함합니다. Windows EXE는 Windows에서 빌드해야 합니다.

GitHub 저장소의 Actions > Build Windows desktop application > Run workflow를 사용해 Windows 빌드를 실행할 수도 있습니다. 성공하면 실행 폴더 ZIP이 ChemicalRegulatoryDesktop-Windows 아티팩트로 제공됩니다. Actions 사용 가능 여부와 실행 결과는 GitHub에서 확인해야 합니다.

## 데이터와 설정

- 승인 규제 마스터: 빌드 시 engine/data/를 읽기 전용으로 포함합니다.
- 결과·캐시·법규 API 임시파일: 사용자별 %LOCALAPPDATA%\ChemicalRegulatoryDesktop\ 아래에 생성합니다. EXE가 들어 있는 폴더에는 저장하지 않습니다.
- 로컬 AI: 별도 설치된 Ollama의 http://127.0.0.1:11434 만 지원합니다. 기본 모델은 qwen3.5:4b이며, OLLAMA_MODEL 환경변수로 바꿀 수 있습니다.
- 법규 API: 별도 .env 파일에 LAW_OC=...를 지정하거나 환경변수를 설정해야 실시간 최신성 확인이 가능합니다. 이 값은 배포본/공개 저장소에 포함하지 마세요.
- PubChem: 인터넷 연결이 필요합니다. 데모 데이터를 이용할 때는 PubChem OFF로도 규칙 기반 판정이 가능합니다.
- 판정의 한계: 공식 고시 최신성을 확인하지 못하면 해당 상태를 표시하며, 결과를 최신 법규 확인 또는 법률 검토를 대신하는 것으로 해석하면 안 됩니다. 허가물질은 판정 범위 밖입니다.

## 발표용 체크

1. Windows에서 EXE 폴더를 준비합니다.
2. ChemicalRegulatoryDesktop.exe를 열고 로컬 AI가 OFF인지 확인합니다. AI가 없더라도 기본 판정은 가능합니다.
3. 프로그램에서 [예시파일 선택] 버튼을 누르면 포함된 예시 인벤토리 6건을 바로 불러올 수 있습니다. 별도의 테스트 파일이 없어도 됩니다.
4. 인터넷 상태에 따라 PubChem과 실시간 법규 조회를 결정합니다. 법규 API 인증키가 없으면 실시간 확인은 꺼진 상태가 기본입니다.
5. 판정 실행, 결과 표, Excel 저장 기능을 실제 발표용 PC에서 미리 테스트합니다.
6. 발표 PC에서 Ollama를 사용할 수 없으면 AI 재검토는 시연하지 않고 ON/OFF 기능과 연결 상태를 설명합니다.

## 자동 테스트 / 검증 범위

python desktop_smoke_test.py는 Ollama 및 공식 법규 API 없이 예시 인벤토리 6건에 대해 규칙 기반 판정과 Excel 보고서 생성을 확인합니다.

GitHub Actions에서는 이 테스트 후 PyInstaller 빌드를 수행하도록 설정되어 있습니다.
본 코드와 설정을 GitHub에 추가한 사실만으로 발표용 PC에서의 EXE 작동이 검증된 것은 아닙니다. Windows 빌드 로그와 실물 PC 실행으로 최종 확인해야 합니다.
