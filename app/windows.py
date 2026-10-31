"""Окна съёмки, настроек и управления обучением."""
import json
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox, QProgressBar, QDialog,
    QDialogButtonBox, QCheckBox, QSpinBox, QPlainTextEdit)
from config import BASE
from app.services import CameraService, CaptureWorker, new_session
from app.viewers import ImageView, frame_pixmap


class SettingsDialog(QDialog):
    """Настройки сохраняются отдельно от исходников, пароль не попадает в архив."""
    def __init__(self, settings, parent=None):
        super().__init__(parent);self.setWindowTitle('Камера и папка проверок');self.resize(650,400)
        layout=QVBoxLayout(self);form=QFormLayout();layout.addLayout(form);self.fields={}
        for key,title in [('ip','IP камеры'),('port','Порт RTSP'),('username','Пользователь'),('password','Пароль'),('stream','Поток'),('checks_root','Корневая папка проверок'),('calibration','Калибровка, необязательный JSON')]:
            edit=QLineEdit(str(settings.get(key,'')));self.fields[key]=edit;form.addRow(title,edit)
            if key=='password':edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember=QCheckBox('Сохранить пароль на этом компьютере (локальный файл настроек)');self.remember.setChecked(settings.get('remember_password',False));layout.addWidget(self.remember)
        info=QLabel('Съёмка: 15 свежих кадров, интервал 1 секунда.\nЦифровой зум меняет только просмотр, исходный кадр сохраняется полностью.');info.setWordWrap(True);layout.addWidget(info)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)

    def values(self):
        values={key:edit.text().strip() for key,edit in self.fields.items()}
        values['port']=int(values['port'])
        if not values['ip'] or not 1<=values['port']<=65535:raise ValueError('Проверьте IP и порт камеры.')
        if not Path(values['checks_root']).is_absolute():raise ValueError('Нужен абсолютный путь папки проверок.')
        values['remember_password']=self.remember.isChecked()
        return values


class InspectionWindow(QMainWindow):
    """Живой поток, ввод номера заказа и сохранение серии до анализа."""
    def __init__(self, owner):
        super().__init__(owner);self.owner=owner;self.setWindowTitle('Проверка изделия в цеху');self.resize(1300,900)
        self.camera=CameraService(owner.settings);self.worker=None;self.worker_active=False;self.last_sequence=-1
        root=QWidget();self.setCentralWidget(root);layout=QVBoxLayout(root);row=QHBoxLayout();layout.addLayout(row)
        self.order=QLineEdit();self.order.setPlaceholderText('№ заказа');self.product=QLineEdit();self.product.setPlaceholderText('№ изделия')
        row.addWidget(QLabel('Заказ'));row.addWidget(self.order);row.addWidget(QLabel('Изделие'));row.addWidget(self.product)
        self.capture=QPushButton('Начать съёмку — 15 кадров');self.capture.clicked.connect(self.start_capture);row.addWidget(self.capture)
        self.cancel=QPushButton('Прервать съёмку');self.cancel.clicked.connect(self.cancel_capture);self.cancel.setEnabled(False);row.addWidget(self.cancel)
        self.progress=QProgressBar();self.progress.setRange(0,15);layout.addWidget(self.progress)
        self.view=ImageView();layout.addWidget(self.view,1)
        controls=QHBoxLayout();layout.addLayout(controls)
        for title,callback in [('Приблизить',lambda:self.view.scale(1.2,1.2)),('Отдалить',lambda:self.view.scale(1/1.2,1/1.2)),('Вся сцена',self.fit)]:
            button=QPushButton(title);button.clicked.connect(callback);controls.addWidget(button)
        self.status=QLabel('Подключение…');layout.addWidget(self.status)
        self.model_status=QLabel('Модель: '+(Path(owner.settings['model']).name if owner.settings.get('model') else 'не выбрана; разрешено сохранение без анализа'));layout.addWidget(self.model_status)
        self.camera.start();self.timer=QTimer(self);self.timer.timeout.connect(self.refresh);self.timer.start(100)

    def fit(self):
        self.view.fitInView(self.view.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)

    def refresh(self):
        frame,sequence,received=self.camera.snapshot();self.status.setText(self.camera.status)
        self.model_status.setText('Модель: '+(Path(self.owner.settings['model']).name if self.owner.settings.get('model') else 'не выбрана; разрешено сохранение без анализа'))
        if frame is None or sequence==self.last_sequence:return
        old_transform=self.view.transform();old_center=self.view.mapToScene(self.view.viewport().rect().center())
        self.view.set_pixmap(frame_pixmap(frame))
        if self.last_sequence>=0:
            self.view.setTransform(old_transform);self.view.centerOn(old_center)
        self.last_sequence=sequence

    def start_capture(self):
        if self.owner.busy:
            QMessageBox.warning(self,'Занято',f'Сейчас выполняется: {self.owner.busy}');return
        try:
            frame,_,received=self.camera.snapshot()
            import time
            if frame is None or time.monotonic()-received>1.5:raise ValueError('Нет свежего кадра камеры.')
            folder,session=new_session(self.owner.settings['checks_root'],self.order.text(),self.product.text())
        except Exception as error:QMessageBox.warning(self,'Съёмка',str(error));return
        self.owner.set_busy('Съёмка');self.capture.setEnabled(False);self.order.setEnabled(False);self.product.setEnabled(False);self.cancel.setEnabled(True);self.progress.setValue(0)
        self.worker=CaptureWorker(self.camera,folder,session,self);self.worker_active=True
        self.worker.progress.connect(self.progress.setValue);self.worker.completed.connect(self.capture_finished);self.worker.start()

    def cancel_capture(self):
        if self.worker:self.worker.cancel_event.set()

    def capture_finished(self,folder):
        self.worker_active=False
        self.owner.set_busy('');self.capture.setEnabled(True);self.cancel.setEnabled(False);self.order.setEnabled(True);self.product.setEnabled(True)
        session=json.loads((Path(folder)/'session.json').read_text(encoding='utf-8'))
        if not session['shots']:
            QMessageBox.warning(self,'Съёмка','Не сохранено ни одного кадра. '+str(session.get('error','')));return
        if session['capture_status']!='complete':
            answer=QMessageBox.question(self,'Незавершённая серия',f"Сохранено {len(session['shots'])}/15. {session.get('error','')}\nПроверить сохранённые кадры?\n«Нет» — вернуться к камере и повторить съёмку.")
            if answer!=QMessageBox.StandardButton.Yes:return
        self.owner.analyze_session(folder)

    def closeEvent(self,event):
        if self.worker_active:
            QMessageBox.information(self,'Съёмка','Сначала прервите съёмку и дождитесь сохранения состояния.');event.ignore();return
        self.timer.stop();self.camera.stop();event.accept()


class TrainingWindow(QMainWindow):
    """Управляет подготовкой данных и отдельным процессом обучения."""
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.resize(1100,850);self.setWindowTitle('Датасет, обучение и оценка модели')
        root=QWidget();self.setCentralWidget(root);layout=QVBoxLayout(root);form=QFormLayout();layout.addLayout(form)
        self.fields={}
        for key,title,default in [('images','Проверенные изображения',''),('labels','Проверенные TXT',''),('dataset','Новая папка подготовленного датасета',str(BASE/'dataset')),('data','Файл data.yaml',str(BASE/'dataset/data.yaml')),('model','Начальные веса / дообучение',str(BASE/'models'/'yolo26n-seg.pt')),('weights','Веса для оценки','')]:
            row=QHBoxLayout();edit=QLineEdit(default);self.fields[key]=edit;row.addWidget(edit);button=QPushButton('Выбрать…');button.clicked.connect(lambda _,k=key:self.pick(k));row.addWidget(button);form.addRow(title,row)
        parameters=QHBoxLayout();layout.addLayout(parameters);self.params={}
        for key,title,value,minimum,maximum in [('epochs','Эпохи',150,1,2000),('patience','Остановка',30,0,1000),('imgsz','Размер',960,64,2560),('batch','Пакет',2,1,32)]:
            parameters.addWidget(QLabel(title));spin=QSpinBox();spin.setRange(minimum,maximum);spin.setValue(value);parameters.addWidget(spin);self.params[key]=spin
        row=QHBoxLayout();layout.addLayout(row)
        for title,callback in [('1. Подготовить выборки',self.prepare),('2. Обучить на GPU',self.train),('3. Оценить на val',self.evaluate),('Выбрать рабочую модель',owner.choose_model),('Остановить задачу',owner.stop_task)]:
            button=QPushButton(title);button.clicked.connect(callback);row.addWidget(button)
        note=QLabel('Нужна разметка по 13 классам. Обучение использует отдельные маски продукции и дефектов. Новая модель не заменяет рабочую автоматически.');note.setWordWrap(True);layout.addWidget(note)
        self.progress=QProgressBar();self.progress.setRange(0,100);layout.addWidget(self.progress)
        self.log=QPlainTextEdit();self.log.setReadOnly(True);self.log.setMaximumBlockCount(5000);layout.addWidget(self.log,1)

    def pick(self,key):
        path=QFileDialog.getOpenFileName(self,'Выберите файл')[0] if key in ('data','weights','model') else QFileDialog.getExistingDirectory(self,'Выберите папку')
        if path:self.fields[key].setText(path)

    def prepare(self):
        fields={k:e.text().strip() for k,e in self.fields.items()}
        if not all(fields[k] for k in ('images','labels','dataset')):
            QMessageBox.warning(self,'Данные','Выберите изображения, TXT и новую папку датасета.');return
        self.owner.start_task('Подготовка данных',['prepare','--images',fields['images'],'--labels',fields['labels'],'--output',fields['dataset']],lambda ok:self.fields['data'].setText(str(Path(fields['dataset'])/'data.yaml')) if ok else None)

    def train(self):
        args=['train','--data',self.fields['data'].text(),'--model',self.fields['model'].text(),'--device','0']
        for key,spin in self.params.items():args.extend(['--'+key,str(spin.value())])
        self.owner.start_task('Обучение',args)

    def evaluate(self):
        if not self.fields['weights'].text():QMessageBox.warning(self,'Оценка','Выберите обученные веса best.pt.');return
        self.owner.start_task('Оценка',['evaluate','--data',self.fields['data'].text(),'--weights',self.fields['weights'].text(),'--split','val','--device','0','--imgsz',str(self.params['imgsz'].value())])
