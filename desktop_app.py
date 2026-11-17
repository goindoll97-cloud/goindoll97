"""Windows desktop UI (PySide6) for enterprise chemical regulatory screening.

Run locally: python desktop_app.py
Build on Windows: build_desktop.bat
The original Streamlit app.py remains available without modifications.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QTabWidget, QTableView, QVBoxLayout, QWidget,
)

# Read optional .env next to the executable. Never embed credentials into the EXE.
if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).resolve().parent)

import desktop_backend as backend
from regulatory_api import get_api_credential


STYLE = """
QMainWindow, QWidget {background:#f5f8fa;color:#213a4a;font-size:13px;}
QFrame#hero {background:#183b55;border-radius:14px;}
QFrame#hero QLabel {background:transparent;color:#ffffff;}
QFrame#panel {background:#ffffff;border:1px solid #dbe5eb;border-radius:12px;}
QFrame#panel QLabel, QFrame#panel QCheckBox {background:transparent;}
QPushButton {padding:9px 16px;border-radius:7px;background:#e8f1f6;
             color:#17435a;border:1px solid #c8dce6;font-weight:600;}
QPushButton:hover {background:#d8eaf1;}
QPushButton:disabled {color:#9aa9af;background:#f0f2f4;border-color:#e3e9ec;}
QPushButton#primary {background:#236487;color:white;border:1px solid #236487;}
QPushButton#primary:hover {background:#1b5370;}
QLineEdit {padding:9px;background:white;border:1px solid #cfdee7;border-radius:7px;}
QTabWidget::pane {background:white;border:1px solid #dbe5eb;border-radius:6px;}
QTabBar::tab {padding:10px 14px;background:#e8eff3;border:1px solid #dbe5eb;}
QTabBar::tab:selected {background:white;color:#175976;font-weight:700;}
QTableView {background:white;alternate-background-color:#f7fafc;gridline-color:#e6edf1;
            selection-background-color:#d6edf1;selection-color:#193847;}
QHeaderView::section {background:#eaf2f6;padding:7px;border:1px solid #dce8ef;}
QProgressBar {border:1px solid #d6e4ea;border-radius:6px;text-align:center;
              background:#e9f0f3;min-height:15px;}
QProgressBar::chunk {background:#2a7f76;border-radius:5px;}
QCheckBox {spacing:9px;}
"""


class DataFrameModel(QAbstractTableModel):
    def __init__(self, frame: pd.DataFrame | None = None):
        super().__init__()
        self.frame = frame if frame is not None else pd.DataFrame()

    def rowCount(self, parent=None):
        return 0 if parent and parent.isValid() else len(self.frame)

    def columnCount(self, parent=None):
        return 0 if parent and parent.isValid() else len(self.frame.columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            value = self.frame.iat[index.row(), index.column()]
            if value is None:
                return ""
            if isinstance(value, (float, int)) and not isinstance(value, bool):
                try:
                    if not math.isfinite(float(value)):
                        return ""
                except (ValueError, TypeError, OverflowError):
                    pass
            if isinstance(value, (dict, list, tuple, set)):
                return str(value)
            try:
                if pd.isna(value):
                    return ""
            except (ValueError, TypeError):
                pass
            return str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return str(self.frame.columns[section])
        return str(section + 1)


class Worker(QObject):
    stage = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable, args: tuple):
        super().__init__()
        self.task = task
        self.args = args

    @Slot()
    def run(self):
        try:
            result = self.task(*self.args, self.stage.emit)
            self.succeeded.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())


class LoadingDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("로딩 중")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint |
                            Qt.WindowType.WindowTitleHint)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(27, 28, 27, 27)
        layout.setSpacing(14)
        self.title = QLabel("●  로딩 중...")
        self.title.setStyleSheet("font-size:21px;font-weight:700;color:#18354f;")
        self.description = QLabel("프로그램 데이터를 준비하고 있습니다.")
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color:#567082;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # Native continuously animated busy indicator.
        self.bar.setTextVisible(False)
        layout.addWidget(self.title)
        layout.addWidget(self.description)
        layout.addWidget(self.bar)

    def set_message(self, message: str):
        self.description.setText(message)


class DesktopWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.catalog = None
        self.result = None
        self.thread = None
        self.worker = None
        self.loading = None
        self.current_task = None
        self.catalog_online = None

        self.setWindowTitle("기업 화학물질 규제 판정 시스템")
        self.resize(1350, 850)
        self.setMinimumSize(900, 620)
        self.setStyleSheet(STYLE)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(15)

        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("기업 화학물질 규제 판정 시스템")
        title.setStyleSheet("font-size:25px;font-weight:800;")
        subtitle = QLabel("승인된 규제 규칙을 바탕으로 물질 식별과 국내 화학물질 규제를 확인합니다.")
        subtitle.setStyleSheet("font-size:13px;color:#dceef4;")
        hero_layout.addWidget(title)
        hero_layout.addWidget(subtitle)
        outer.addWidget(hero)

        panel = QFrame()
        panel.setObjectName("panel")
        form = QVBoxLayout(panel)
        form.setContentsMargins(18, 17, 18, 17)
        form.setSpacing(12)

        file_row = QHBoxLayout()
        label = QLabel("인벤토리 파일")
        label.setStyleSheet("font-weight:700;")
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("CSV 또는 Excel 파일을 선택하세요.")
        choose = QPushButton("파일 찾기")
        choose.clicked.connect(self.choose_file)
        file_row.addWidget(label)
        file_row.addWidget(self.file_path, 1)
        file_row.addWidget(choose)
        form.addLayout(file_row)

        option_grid = QGridLayout()
        option_grid.setHorizontalSpacing(22)
        option_grid.setVerticalSpacing(8)
        self.pubchem_toggle = QCheckBox("누락 SMILES를 PubChem에서 자동 수집")
        self.pubchem_toggle.setChecked(True)
        self.ai_toggle = QCheckBox("어려운 보류 사례만 로컬 AI 자동검토 (ON/OFF)")
        self.ai_toggle.setEnabled(False)
        self.detail_toggle = QCheckBox("Excel 결과에 상세 감사자료 포함")
        self.law_toggle = QCheckBox("공식 법규 API 최신성 확인 (인터넷)")
        self.law_toggle.setChecked(bool(get_api_credential()[0]))
        self.law_toggle.toggled.connect(self._api_option_changed)
        option_grid.addWidget(self.pubchem_toggle, 0, 0)
        option_grid.addWidget(self.ai_toggle, 0, 1)
        option_grid.addWidget(self.detail_toggle, 1, 0)
        option_grid.addWidget(self.law_toggle, 1, 1)
        form.addLayout(option_grid)
        self.ai_status = QLabel("로컬 AI 상태: 확인 중 (모델 미설치 시 OFF 상태로 유지)")
        self.ai_status.setStyleSheet("color:#5d7683;")
        self.law_status = QLabel("규제 마스터: 준비 중")
        self.law_status.setStyleSheet("color:#5d7683;")
        form.addWidget(self.ai_status)
        form.addWidget(self.law_status)

        actions = QHBoxLayout()
        self.run_button = QPushButton("규제 판정 실행")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_screen)
        self.export_button = QPushButton("Excel 다른 이름으로 저장")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_excel)
        self.retry_button = QPushButton("오류·미처리 AI 재검토")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.retry_ai)
        self.refresh_button = QPushButton("규제 데이터/AI 상태 새로고침")
        self.refresh_button.clicked.connect(self.load_catalog)
        actions.addWidget(self.run_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.retry_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        form.addLayout(actions)
        outer.addWidget(panel)

        self.info = QLabel("인벤토리를 선택한 후 '규제 판정 실행'을 누르세요.")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("font-weight:600;color:#236487;")
        outer.addWidget(self.info)

        self.tabs = QTabWidget()
        self.tables: dict[str, QTableView] = {}
        for name in (
            "판정 결과", "규제 상세", "법률근거", "신뢰 게이트",
            "카테고리 검색기록", "포괄범위 후보", "AI 검토기록",
        ):
            table = QTableView()
            table.setAlternatingRowColors(True)
            table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            table.setSortingEnabled(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            table.horizontalHeader().setDefaultSectionSize(155)
            table.verticalHeader().setDefaultSectionSize(28)
            table.setWordWrap(False)
            table.setModel(DataFrameModel())
            self.tables[name] = table
            self.tabs.addTab(table, name)
        outer.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        disclaimer = QLabel("※ 최신 법규 미확인·보류 결과는 비규제 확정이 아닙니다. 허가물질은 판정 범위에 포함되지 않습니다.")
        disclaimer.setStyleSheet("color:#9b5a19;font-size:12px;")
        footer.addWidget(disclaimer, 1)
        open_results = QPushButton("결과 폴더 열기")
        open_results.clicked.connect(self.open_results)
        footer.addWidget(open_results)
        outer.addLayout(footer)

        self.setCentralWidget(root)
        self.statusBar().showMessage("규제 데이터를 불러오는 중...")
        self.load_catalog()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "인벤토리 선택", str(Path.home()),
            "인벤토리 (*.csv *.xlsx *.xls);;모든 파일 (*)"
        )
        if path:
            self.file_path.setText(path)

    def _api_option_changed(self, enabled: bool):
        if self.catalog is not None and enabled != self.catalog_online:
            self.law_status.setText("법규 API 설정이 변경되었습니다. [새로고침] 후 판정하세요.")
            self.catalog = None
            self.run_button.setEnabled(False)

    def _start(self, task_name: str, function: Callable, args: tuple):
        if self.thread is not None:
            return
        self.current_task = task_name
        self.run_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.loading = LoadingDialog(self)
        self.loading.show()

        thread = QThread(self)
        worker = Worker(function, args)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stage.connect(self._on_progress)
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread = thread
        self.worker = worker
        thread.start()

    @Slot(str)
    def _on_progress(self, message: str):
        self.statusBar().showMessage(message)
        if self.loading is not None:
            self.loading.set_message(message)

    @Slot(object)
    def _on_success(self, data):
        if self.loading is not None:
            self.loading.close()
            self.loading = None
        if self.current_task == "catalog":
            self.catalog = data
            self.catalog_online = bool(self.law_toggle.isChecked())
            self._set_catalog_status()
        else:
            self.result = data
            self._show_result()
        self.statusBar().showMessage("완료")
        self.refresh_button.setEnabled(True)
        self.run_button.setEnabled(self.catalog is not None)

    @Slot(str)
    def _on_failure(self, tb: str):
        if self.loading is not None:
            self.loading.close()
            self.loading = None
        self.refresh_button.setEnabled(True)
        self.run_button.setEnabled(self.catalog is not None)
        self.retry_button.setEnabled(bool(self.result and self.catalog and self.catalog["ai_status"].get("ready")))
        self.statusBar().showMessage("작업 중 오류 발생")
        QMessageBox.critical(
            self, "실행 오류",
            "실행 중 오류가 발생했습니다. 아래 오류 내용을 확인하세요.\n\n" + tb[-6000:]
        )

    @Slot()
    def _thread_finished(self):
        self.thread = None
        self.worker = None
        self.current_task = None

    def load_catalog(self):
        self.catalog = None
        self.ai_toggle.setChecked(False)
        self.ai_toggle.setEnabled(False)
        self.law_status.setText("규제 마스터를 불러오는 중...")
        self._start("catalog", backend.initialize, (self.law_toggle.isChecked(),))

    def _set_catalog_status(self):
        ai = self.catalog["ai_status"]
        if ai.get("ready"):
            self.ai_status.setText(f"로컬 AI: 사용 가능 ({ai.get('model', '')}) · ON/OFF 선택 가능")
            self.ai_toggle.setEnabled(True)
            self.ai_toggle.setChecked(True)  # Matches the Streamlit default.
        else:
            self.ai_status.setText(
                "로컬 AI: 연결되지 않음 (OFF) · Ollama와 모델 설치 후 새로고침하면 ON 가능"
            )
            self.ai_toggle.setChecked(False)
            self.ai_toggle.setEnabled(False)
        notice = self.catalog["notice"]
        law = self.catalog["api_status"].get("sync_status_ko", "")
        self.law_status.setText(f"승인 규제 마스터: {notice}  |  최신성 상태: {law}")
        self.info.setText("준비 완료. 인벤토리 파일을 선택하고 판정하세요.")
        self.retry_button.setEnabled(bool(self.result and ai.get("ready")))

    def run_screen(self):
        if self.catalog is None:
            QMessageBox.warning(self, "준비 중", "규제 데이터 새로고침을 먼저 완료하세요.")
            return
        source = Path(self.file_path.text().strip())
        if not source.is_file():
            QMessageBox.warning(self, "파일 선택", "실제로 존재하는 CSV 또는 Excel 파일을 선택하세요.")
            return
        if source.suffix.lower() not in {".csv", ".xlsx", ".xls"}:
            QMessageBox.warning(self, "파일 형식", "CSV, XLSX, XLS 파일만 지원합니다.")
            return
        self._start(
            "screen", backend.screen_inventory,
            (self.catalog, str(source), self.pubchem_toggle.isChecked(),
             self.ai_toggle.isChecked(), self.detail_toggle.isChecked())
        )

    def retry_ai(self):
        if not self.result or not self.catalog or not self.catalog["ai_status"].get("ready"):
            return
        self._start("retry", backend.rerun_ai, (self.result,))

    def _show_result(self):
        data = self.result
        frames = {
            "판정 결과": data["plain"],
            "규제 상세": data["summary"],
            "법률근거": data["legal_basis"],
            "신뢰 게이트": data["gate_log"],
            "카테고리 검색기록": data["category_log"],
            "포괄범위 후보": data["no_cas_candidates"],
            "AI 검토기록": data["llm_audit"],
        }
        for name, frame in frames.items():
            self.tables[name].setModel(DataFrameModel(frame))
        self.tabs.setCurrentIndex(0)
        count = len(data["summary"])
        file_name = data["saved"]["file_name"]
        review_count = max(0, len(data["llm_cases"]) - len(data["llm_audit"]))
        self.info.setText(
            f"{count:,}건 판정 완료  |  결과 저장: {file_name}"
            + (f"  |  미처리 AI 사례: {review_count:,}건" if review_count else "")
        )
        self.export_button.setEnabled(True)
        self.retry_button.setEnabled(bool(
            self.catalog["ai_status"].get("ready") and data["llm_cases"]
        ))

    def export_excel(self):
        if not self.result:
            return
        preferred = self.result["saved"]["file_name"]
        target, _ = QFileDialog.getSaveFileName(
            self, "Excel 결과 저장", str(Path.home() / "Desktop" / preferred),
            "Excel Workbook (*.xlsx)"
        )
        if not target:
            return
        if not target.lower().endswith(".xlsx"):
            target += ".xlsx"
        try:
            Path(target).write_bytes(self.result["workbook"])
            QMessageBox.information(self, "저장 완료", "Excel 파일을 저장했습니다.\n" + target)
        except OSError as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))

    def open_results(self):
        backend.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(backend.RESULTS_DIR)))

    def closeEvent(self, event):
        if self.thread is not None:
            QMessageBox.information(
                self, "작업 진행 중",
                "규제 판정 또는 데이터 조회가 진행 중입니다. 완료 후 종료하세요."
            )
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("기업 화학물질 규제 판정 시스템")
    app.setStyle("Fusion")
    app.setFont(QFont("Malgun Gothic", 10))
    window = DesktopWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
