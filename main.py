"""Точка входа единого приложения Даймонд Трейд."""
import os

# Параметры задаются до импорта OpenCV, чтобы адрес с паролем не попадал в журнал.
os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS','rtsp_transport;tcp')
os.environ.setdefault('OPENCV_FFMPEG_LOGLEVEL','-8')
os.environ.setdefault('OPENCV_LOG_LEVEL','SILENT')
import json
import re
import sys
from pathlib import Path
from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QPushButton,
    QLabel,QFileDialog,QMessageBox,QDialog,QListWidget,QListWidgetItem,QDialogButtonBox)
from config import BASE
from app.services import atomic_json
from app.windows import SettingsDialog,InspectionWindow,TrainingWindow
from app.viewers import ResultsWindow


class MainWindow(QMainWindow):
    """Объединяет проверку в цеху, разметку, обучение и историю."""
    def __init__(self):
        super().__init__()
        # Явно загружаем системный шрифт с кириллицей, в том числе для проверки без экрана.
        font_path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'segoeui.ttf'
        if font_path.exists():
            font_id=QFontDatabase.addApplicationFont(str(font_path))
            families=QFontDatabase.applicationFontFamilies(font_id)
            if families:QApplication.instance().setFont(QFont(families[0],10))
        self.setWindowTitle('Даймонд Трейд — контроль качества');self.resize(1000,720)
        self.workspace=BASE/'workspace';self.workspace.mkdir(exist_ok=True)
        self.settings_path=self.workspace/'settings.json'
        self.settings={'ip':'192.168.1.64','port':554,'username':'admin','password':'','stream':'101',
                       'remember_password':False,'checks_root':str(BASE.parent/'Проверки'),'model':'','calibration':''}
        if self.settings_path.exists():
            try:self.settings.update(json.loads(self.settings_path.read_text(encoding='utf-8')))
            except Exception:pass
        self.busy='';self.process=None;self.children=[];self.inspection=None;self.training=None;self.callback=None
        root=QWidget();self.setCentralWidget(root);layout=QVBoxLayout(root);layout.setSpacing(18)
        title=QLabel('ДАЙМОНД ТРЕЙД');title.setStyleSheet('font-size:30px; font-weight:700;');layout.addWidget(title)
        layout.addWidget(QLabel('Контроль готовой продукции • 13 классов • локальная обработка'))
        self.check_button=QPushButton('Проверить изделие в цеху');self.check_button.setMinimumHeight(125);self.check_button.setStyleSheet('background:#2563eb;color:white;font-size:24px;border-radius:12px;');self.check_button.clicked.connect(self.open_inspection);layout.addWidget(self.check_button)
        # Отдельный снимок проверяется без камеры, заказа и серии.
        self.photo_button=QPushButton('Проверить фотографию из файла')
        self.photo_button.setMinimumHeight(65)
        self.photo_button.clicked.connect(self.check_photo)
        layout.addWidget(self.photo_button)
        row=QHBoxLayout();layout.addLayout(row)
        for title,callback in [('Разметить фотографии',self.open_labeler),('Обучить и оценить модель',self.open_training)]:
            button=QPushButton(title);button.setMinimumHeight(95);button.clicked.connect(callback);row.addWidget(button)
        row=QHBoxLayout();layout.addLayout(row)
        for title,callback in [('История проверок',self.history),('Настройки камеры',self.configure),('Выбрать рабочую модель',self.choose_model)]:
            button=QPushButton(title);button.clicked.connect(callback);row.addWidget(button)
        self.state=QLabel();self.state.setWordWrap(True);layout.addWidget(self.state)
        note=QLabel('Без обученной модели разрешена съёмка. Проценты YOLO — уверенность области, а не проверенная вероятность брака.');note.setWordWrap(True);layout.addWidget(note)
        layout.addStretch();self.refresh_status()
        self.setStyleSheet(self.styleSheet()+' QPushButton { padding:12px; } QLabel { font-size:14px; }')

    def persist_settings(self):
        data=dict(self.settings)
        if not data.get('remember_password'):data['password']=''
        atomic_json(self.settings_path,data)

    def refresh_status(self):
        model=self.settings.get('model')
        self.state.setText(f"Рабочая модель: {Path(model).name if model else 'не выбрана'}\nКамера: {self.settings['ip']}, поток {self.settings['stream']}\nПроверки: {self.settings['checks_root']}\nЗадача: {self.busy or 'нет активных задач'}")

    def set_busy(self,value):
        self.busy=value;self.check_button.setEnabled(not bool(value));self.photo_button.setEnabled(not bool(value));self.refresh_status()

    def configure(self):
        if self.busy:QMessageBox.information(self,'Занято','Настройки доступны после завершения задачи.');return
        if self.inspection and self.inspection.isVisible():QMessageBox.information(self,'Камера','Закройте окно камеры перед изменением подключения.');return
        dialog=SettingsDialog(self.settings,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            try:self.settings.update(dialog.values());self.persist_settings();self.refresh_status()
            except Exception as error:QMessageBox.warning(self,'Настройки',str(error))

    def open_inspection(self):
        if self.busy:QMessageBox.information(self,'Занято',self.busy);return
        if not self.settings.get('password'):
            self.configure()
            if not self.settings.get('password'):return
        if self.inspection and self.inspection.isVisible():self.inspection.raise_();return
        self.inspection=InspectionWindow(self);self.inspection.show()

    def open_labeler(self):
        # Новый рабочий реестр не затрагивает сессии старого разметчика.
        from app.labeler import Window
        for child in self.children:
            if isinstance(child,Window) and child.isVisible():child.raise_();return
        window=Window()
        if not window.state.get('paths'):
            queue=BASE/'workspace'/'annotation_queue';queue.mkdir(parents=True,exist_ok=True)
            for field,path in zip(window.paths,[queue,BASE/'workspace'/'training_data'/'images',BASE/'workspace'/'training_data'/'labels']):field.setText(str(path))
        self.children.append(window);window.show()

    def open_training(self):
        if not self.training:self.training=TrainingWindow(self)
        self.training.show();self.training.raise_()

    def choose_model(self):
        if self.busy:QMessageBox.information(self,'Занято',self.busy);return
        path=QFileDialog.getOpenFileName(self,'Выберите обученную модель по 13 классам',str(BASE/'runs'),'Веса (*.pt)')[0]
        if not path:return
        # Проверка весов выполняется в отдельном процессе без подвисания интерфейса.
        def done(ok):
            if ok:self.settings['model']=path;self.persist_settings();self.refresh_status();QMessageBox.information(self,'Модель','Рабочая модель выбрана и сохранена.')
        code='from pipeline import load_model; import sys; load_model(sys.argv[1],custom=True); print("MODEL_OK")'
        self.start_task('Проверка модели',['-c',code,path],done,python_arguments=True)

    def start_task(self,title,args,callback=None,python_arguments=False):
        if self.busy:QMessageBox.warning(self,'Занято',f'Уже выполняется: {self.busy}');return False
        self.set_busy(title);self.callback=callback
        if not self.training:self.training=TrainingWindow(self)
        self.training.log.appendPlainText('\n=== '+title+' ===');self.training.progress.setValue(0)
        self.process=QProcess(self);self.process.setWorkingDirectory(str(BASE));env=QProcessEnvironment.systemEnvironment();env.insert('PYTHONUTF8','1');env.insert('PYTHONUNBUFFERED','1');self.process.setProcessEnvironment(env)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output);self.process.finished.connect(self.task_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start(sys.executable,args if python_arguments else [str(BASE/'pipeline.py')]+args)
        return True

    def read_output(self):
        if not self.process:return
        text=bytes(self.process.readAllStandardOutput()).decode('utf-8',errors='replace')
        self.training.log.appendPlainText(text)
        matches=re.findall(r'\b(\d+)/(\d+)\s+\S*G',text)
        if matches:
            epoch,total=map(int,matches[-1]);self.training.progress.setValue(round(100*epoch/max(1,total)))

    def process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            callback=self.callback;self.callback=None;self.set_busy('')
            QMessageBox.warning(self,'Запуск','Не удалось запустить Python рабочего окружения.')
            if callback:callback(False)

    def task_finished(self,code,status):
        self.read_output();callback=self.callback;self.callback=None;self.set_busy('')
        ok=code==0 and status==QProcess.ExitStatus.NormalExit
        self.training.log.appendPlainText('Готово.' if ok else 'Задача остановлена или завершилась ошибкой. Смотрите журнал.')
        if ok:self.training.progress.setValue(100)
        if callback:callback(ok)
        elif not ok:self.training.show();QMessageBox.warning(self,'Задача','Ошибка или остановка. Подробности в журнале обучения.')
        elif self.training.isVisible():QMessageBox.information(self,'Задача','Выполнено. Рабочая модель не заменяется автоматически.')

    def stop_task(self):
        if self.process and self.process.state()!=QProcess.ProcessState.NotRunning:
            if QMessageBox.question(self,'Остановка','Остановить текущую задачу? Незавершённая эпоха не сохранится.')==QMessageBox.StandardButton.Yes:self.process.kill()

    def analyze_session(self,folder):
        from app.services import atomic_json
        folder=Path(folder);session=json.loads((folder/'session.json').read_text(encoding='utf-8'))
        model=self.settings.get('model')
        if not model or not Path(model).is_file():
            session['analysis_status']='no_model';atomic_json(folder/'session.json',session)
            QMessageBox.information(self,'Снимки сохранены','Анализ не выполнен — рабочая модель не выбрана.');self.show_results(folder);return
        analysis='analysis_001';number=1
        while (folder/analysis).exists():number+=1;analysis=f'analysis_{number:03d}'
        session.update(analysis_status='running',model_path=model,analysis_dir=analysis);atomic_json(folder/'session.json',session)
        def done(ok):
            session['analysis_status']='complete' if ok else 'failed';atomic_json(folder/'session.json',session)
            self.show_results(folder)
            if not ok:QMessageBox.warning(self,'Анализ','Снимки сохранены, но анализ завершился ошибкой. Проверьте журнал.')
        arguments=['predict','--weights',model,'--source',str(folder/'originals'),'--output',str(folder/analysis),'--visualize','--device','0']
        if self.settings.get('calibration'):arguments.extend(['--calibration',self.settings['calibration']])
        self.start_task('Анализ серии',arguments,done)

    def check_photo(self):
        """Проверяет один загруженный файл независимо от заказов и камеры."""
        if self.busy:
            QMessageBox.information(self, 'Занято', self.busy)
            return
        model = self.settings.get('model')
        if not model or not Path(model).is_file():
            QMessageBox.information(self, 'Модель не выбрана', 'Сначала нажмите «Выбрать рабочую модель» и укажите обученные best.pt по 13 классам.')
            return
        source, _ = QFileDialog.getOpenFileName(
            self, 'Выберите фотографию изделия', '',
            'Фотографии (*.png *.jpg *.jpeg *.bmp *.webp)'
        )
        if not source:
            return
        from app.photo_check import prepare_photo, PhotoResultWindow
        try:
            folder, copied_source = prepare_photo(source, self.workspace/'photo_checks')
        except Exception as error:
            QMessageBox.warning(self, 'Фотография', str(error))
            return
        output = folder/'analysis'
        metadata = {'kind': 'single_photo', 'source_original': str(Path(source).resolve()),
                    'source_copy': copied_source.name, 'model_path': str(model), 'status': 'running'}
        atomic_json(folder/'check.json', metadata)

        def finished(ok):
            metadata['status'] = 'complete' if ok else 'failed'
            atomic_json(folder/'check.json', metadata)
            if not ok:
                self.training.show()
                QMessageBox.warning(self, 'Проверка фотографии', 'Анализ завершился ошибкой. Подробности в журнале. Исходный файл не изменён.')
                return
            try:
                reports = [p for p in output.glob('*.json') if p.name != 'summary.json']
                if len(reports) != 1:
                    raise ValueError('Не найден единственный отчёт проверки фотографии.')
                window = PhotoResultWindow(reports[0], self)
                self.children.append(window)
                window.show()
            except Exception as error:
                QMessageBox.warning(self, 'Результат фотографии', str(error))

        arguments = ['predict', '--weights', str(model), '--source', str(copied_source),
                     '--output', str(output), '--visualize', '--device', '0']
        if self.settings.get('calibration'):
            arguments.extend(['--calibration', self.settings['calibration']])
        if not self.start_task('Проверка фотографии', arguments, finished):
            metadata['status'] = 'not_started'
            atomic_json(folder/'check.json', metadata)

    def show_results(self,folder):
        try:
            window=ResultsWindow(folder,BASE/'workspace'/'annotation_queue',self);self.children.append(window);window.show()
        except Exception as error:QMessageBox.warning(self,'Результаты',str(error))

    def history(self):
        dialog=QDialog(self);dialog.setWindowTitle('История проверок');dialog.resize(1000,650);layout=QVBoxLayout(dialog);listing=QListWidget();layout.addWidget(listing)
        root=Path(self.settings['checks_root'])
        for path in sorted(root.glob('*/изделие_*/серия_*/session.json'),reverse=True):
            try:
                s=json.loads(path.read_text(encoding='utf-8'));item=QListWidgetItem(f"Заказ {s['order']} | изделие {s['product']} | серия {s['series']} | кадров {len(s['shots'])}/15 | {s['analysis_status']} | {s['created_at']}")
                item.setData(Qt.ItemDataRole.UserRole,str(path.parent));listing.addItem(item)
            except Exception:continue
        def open_item(item):
            if item:dialog.accept();self.show_results(item.data(Qt.ItemDataRole.UserRole))
        def analyze_item():
            item=listing.currentItem()
            if item:dialog.accept();self.analyze_session(item.data(Qt.ItemDataRole.UserRole))
        listing.itemDoubleClicked.connect(open_item)
        row=QHBoxLayout();layout.addLayout(row)
        open_button=QPushButton('Открыть');open_button.clicked.connect(lambda:open_item(listing.currentItem()));row.addWidget(open_button)
        analyze=QPushButton('Анализировать сохранённую серию');analyze.clicked.connect(analyze_item);row.addWidget(analyze)
        dialog.exec()

    def closeEvent(self,event):
        if self.busy:
            QMessageBox.information(self,'Есть активная задача','Сначала завершите или остановите съёмку / обработку / обучение.');event.ignore();return
        for child in self.children:
            if child.isVisible() and not child.close():event.ignore();return
        if self.inspection:self.inspection.close()
        if self.training:self.training.close()
        self.persist_settings();event.accept()


def main():
    """Создаёт одно приложение Qt для всех окон и модулей."""
    application=QApplication(sys.argv);application.setApplicationName('Diamond Trade Quality')
    window=MainWindow();window.show();sys.exit(application.exec())


# Запуск разметчика отдельно не создаёт вторую копию приложения Qt.
if __name__=='__main__':main()
