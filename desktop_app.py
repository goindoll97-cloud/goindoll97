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
from PySide6.QtCore import QAbstractTableModel, QObject, Qt, QThread, QUrl, Signal, Slot, QRectF
from PySide6.QtGui import QDesktopServices, QFont, QColor, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QTableView, QVBoxLayout, QWidget,
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
"""


class ToggleSwitch(QCheckBox):
    """An accessible, checkable native Qt ON/OFF slider control."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setAccessibleName(label)
        self.setToolTip(f"{label}: 클릭하여 켜거나 끕니다.")
        self.setFixedSize(78, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setTristate(False)

    def hitButton(self, pos):
        """Treat the entire painted 78px slider as a clickable control.

        QCheckBox's default hit zone is only the hidden native indicator,
        which does not match the width of our custom painted switch.
        """
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        active = self.isChecked()
        enabled = self.isEnabled()
        painter.setBrush(
            QColor("#287e74" if active else "#a8b7c1")
            if enabled else QColor("#d4dce1")
        )
        painter.drawRoundedRect(QRectF(1, 3, 76, 28), 14, 14)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(52 if active else 5, 7, 20, 20))
        painter.setPen(QColor("#ffffff" if enabled else "#77858e"))
        font = QFont("Segoe UI", 9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(5, 3, 43, 28) if active else QRectF(28, 3, 46, 28),
            Qt.AlignmentFlag.AlignCenter,
            "ON" if active else "OFF",
        )
        painter.end()


def toggle_option(label: str, switch: ToggleSwitch) -> QWidget:
    """Pair a full descriptive label with a right-aligned switch."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 5, 0, 5)
    layout.setSpacing(12)
    text = QLabel(label)
    text.setWordWrap(True)
    text.setBuddy(switch)
    layout.addWidget(text, 1)
    layout.addWidget(switch, 0, Qt.AlignmentFlag.AlignRight)
    return container


class DataFrameModel(QAbstractTableModel):
    def __init__(self, frame: pd.DataFrame | None = None, parent=None):
        super().__init__(parent)
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
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(27, 28, 27, 27)
        layout.setSpacing(14)
        self.title = QLabel("로딩 중...")
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
        close_note = QLabel("창을 닫아도 작업은 계속 진행되며, 완료 후 결과가 표시됩니다.")
        close_note.setWordWrap(True)
        close_note.setStyleSheet("color:#567082;font-size:12px;")
        layout.addWidget(close_note)
        self.close_button = QPushButton("창 닫기")
        self.close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

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

        self.setWindowTitle("사업장 화학물질 규제 판정 시스템")
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
        title = QLabel("사업장 화학물질 규제 판정 시스템")
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

        workspace_row = QHBoxLayout()
        workspace_label = QLabel("작업 데이터 저장 폴더")
        workspace_label.setStyleSheet("font-weight:700;")
        self.workspace_path = QLineEdit(str(backend.WORKSPACE_DIR))
        self.workspace_path.setReadOnly(True)
        self.workspace_path.setToolTip("판정 결과·PubChem 캐시·법규 API 임시파일이 모두 이 폴더에 저장됩니다.")
        choose_workspace = QPushButton("저장 폴더 지정")
        choose_workspace.clicked.connect(self.choose_workspace)
        workspace_row.addWidget(workspace_label)
        workspace_row.addWidget(self.workspace_path, 1)
        workspace_row.addWidget(choose_workspace)
        form.addLayout(workspace_row)
        file_row = QHBoxLayout()
        label = QLabel("인벤토리 파일")
        label.setStyleSheet("font-weight:700;")
        self.file_path = QLineEdit()
        self.file_path.setPlaceholderText("CSV 또는 Excel 파일을 선택하세요.")
        choose = QPushButton("파일 찾기")
        choose.clicked.connect(self.choose_file)
        self.example_button = QPushButton("작성예시파일")
        self.example_button.setToolTip(
            "물질명, CAS번호, 함량(%)만 작성하는 Excel 예시 양식을 저장합니다."
        )
        self.example_button.clicked.connect(self.save_entry_example)
        file_row.addWidget(label)
        file_row.addWidget(self.file_path, 1)
        file_row.addWidget(self.example_button)
        file_row.addWidget(choose)
        form.addLayout(file_row)

        option_grid = QGridLayout()
        option_grid.setHorizontalSpacing(36)
        option_grid.setVerticalSpacing(8)
        option_grid.setColumnStretch(0, 1)
        option_grid.setColumnStretch(1, 1)
        self.pubchem_toggle = ToggleSwitch("화학물질 구조정보 자동으로 찾기 (인터넷)")
        self.pubchem_toggle.setToolTip(
            "물질의 구조정보가 없을 때 인터넷(PubChem)에서 자동으로 찾습니다. "
            "인터넷에 연결되지 않으면 입력된 정보만 사용합니다."
        )
        self.pubchem_toggle.setChecked(True)
        self.ai_toggle = ToggleSwitch("어려운 보류 사례만 로컬 AI 자동검토")
        self.ai_toggle.setEnabled(False)
        self.law_toggle = ToggleSwitch("공식 법규 API 최신성 확인 (인터넷)")
        self.law_toggle.setChecked(bool(get_api_credential()[0]))
        self.law_toggle.toggled.connect(self._api_option_changed)
        option_grid.addWidget(toggle_option("화학물질 구조정보 자동으로 찾기 (인터넷)", self.pubchem_toggle), 0, 0)
        option_grid.addWidget(toggle_option("어려운 보류 사례만 로컬 AI 자동검토", self.ai_toggle), 0, 1)
        option_grid.addWidget(toggle_option("공식 법규 API 최신성 확인 (인터넷)", self.law_toggle), 1, 0)
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
        self.refresh_button = QPushButton("법령 데이터/AI 상태 새로고침")
        self.refresh_button.clicked.connect(self.load_catalog)
        actions.addWidget(self.run_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch(1)
        form.addLayout(actions)
        outer.addWidget(panel)

        self.info = QLabel("인벤토리를 선택한 후 '규제 판정 실행'을 누르세요.")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("font-weight:600;color:#236487;")
        outer.addWidget(self.info)

        # The screen shows only the reader-facing summary. Detailed legal,
        # search and trust-gate audits remain available in exported Excel.
        result_heading = QLabel("판정 결과")
        result_heading.setStyleSheet("font-size:16px;font-weight:700;color:#183b55;")
        outer.addWidget(result_heading)
        self.result_table = QTableView()
        self.result_table.setObjectName("judgementResults")
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.result_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.result_table.setSortingEnabled(False)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.result_table.horizontalHeader().setDefaultSectionSize(155)
        self.result_table.verticalHeader().setDefaultSectionSize(28)
        self.result_table.setWordWrap(False)
        self.result_model = DataFrameModel(parent=self.result_table)
        self.result_table.setModel(self.result_model)
        outer.addWidget(self.result_table, 1)

        open_results = QPushButton("결과 폴더 열기")
        open_results.clicked.connect(self.open_results)
        outer.addWidget(open_results, 0, Qt.AlignmentFlag.AlignRight)

        self.setCentralWidget(root)
        self.statusBar().showMessage("법령 데이터를 불러오는 중...")
        self.load_catalog()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "인벤토리 선택", str(Path.home()),
            "인벤토리 (*.csv *.xlsx *.xls);;모든 파일 (*)"
        )
        if path:
            self.file_path.setText(path)

    def save_entry_example(self):
        target, _ = QFileDialog.getSaveFileName(
            self,
            "작성예시파일 저장",
            str(Path.home() / "작성예시파일.xlsx"),
            "Excel 작성 양식 (*.xlsx)",
        )
        if not target:
            return
        if not target.lower().endswith(".xlsx"):
            target += ".xlsx"
        try:
            backend.write_inventory_example(Path(target))
        except Exception as exc:
            QMessageBox.critical(self, "예시파일 저장 실패", str(exc))
            return
        self.info.setText(
            "작성예시파일 저장 완료. 예시 행을 실제 물질 정보로 수정한 뒤 "
            "'파일 찾기'에서 작성한 Excel을 선택하세요."
        )
        QMessageBox.information(
            self,
            "작성예시파일 저장 완료",
            "Excel 예시 양식을 저장했습니다.\n"
            "예시 2개 물질은 실제 정보로 교체하거나 삭제하세요.\n\n"
            f"{target}",
        )

    def choose_workspace(self):
        if self.thread is not None:
            QMessageBox.warning(self, "작업 중", "현재 작업이 끝난 후 저장 위치를 변경하세요.")
            return
        if self.result is not None:
            QMessageBox.warning(
                self, "이미 판정 결과가 있습니다",
                "저장 위치는 분석 전에 지정하세요. 이미 만들어진 결과가 "
                "이전 위치에 남지 않도록 프로그램을 종료한 뒤 다시 열어 변경하세요."
            )
            return
        target = QFileDialog.getExistingDirectory(
            self, "발표용 작업 데이터 저장 폴더 선택",
            str(backend.WORKSPACE_DIR.parent),
        )
        if not target:
            return
        try:
            folder = backend.set_workspace(target)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "폴더 변경 실패", str(exc))
            return
        self.workspace_path.setText(str(folder))
        self.info.setText(
            f"작업 데이터 저장 위치: {folder}  |  기존 파일이 있는 폴더는 삭제 전에 반드시 확인하세요."
        )

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
        self.loading = LoadingDialog(self)
        self.loading.finished.connect(self._loading_window_closed)
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

    @Slot(int)
    def _loading_window_closed(self, _result: int):
        # Dismissing the progress dialog never interrupts the worker thread.
        # The status bar continues showing progress until results are ready.
        if self.sender() is self.loading:
            self.loading = None
            if self.thread is not None:
                self.statusBar().showMessage(
                    "로딩 창을 닫았습니다. 작업은 계속 진행 중이며 완료 시 결과가 표시됩니다."
                )

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
            cleanup_warning = data.get("saved", {}).get("cleanup_warning", "")
            if cleanup_warning:
                QMessageBox.warning(
                    self, "기존 결과파일 정리 필요",
                    "새로운 판정결과는 정상 저장했습니다.\n"
                    "다음 이전 결과파일은 삭제하지 못했습니다.\n\n"
                    + cleanup_warning
                    + "\n\n엑셀 파일이 열려 있다면 닫은 후 다시 판정하세요."
                )
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
        self.law_status.setText("법령 데이터를 불러오는 중...")
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

    def run_screen(self):
        if self.catalog is None:
            QMessageBox.warning(self, "준비 중", "법령 데이터 새로고침을 먼저 완료하세요.")
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
             self.ai_toggle.isChecked())
        )

    def _show_result(self):
        data = self.result
        self.result_model = DataFrameModel(data["plain"], parent=self.result_table)
        self.result_table.setModel(self.result_model)
        count = len(data["summary"])
        file_name = data["saved"]["file_name"]
        removed = data["saved"].get("cleanup_removed", 0)
        self.info.setText(
            f"{count:,}건 판정 완료  |  결과 저장: {file_name}"
            + (f"  |  이전 결과 {removed}개 정리" if removed else "")
        )
        self.export_button.setEnabled(True)

    def export_excel(self):
        if not self.result:
            return
        preferred = self.result["saved"]["file_name"]
        backend.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        target, _ = QFileDialog.getSaveFileName(
            self, "Excel 결과 저장",
            str(backend.RESULTS_DIR / preferred),
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
    app.setApplicationName("사업장 화학물질 규제 판정 시스템")
    app.setStyle("Fusion")
    app.setFont(QFont("Malgun Gothic", 10))
    window = DesktopWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
