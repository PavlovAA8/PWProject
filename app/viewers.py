"""Просмотр оригинала и результата с синхронными масштабом и перемещением."""
import json
import shutil
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QImage, QIcon, QPainter
from PySide6.QtWidgets import (QGraphicsView, QGraphicsScene, QWidget, QVBoxLayout, QHBoxLayout,
    QMainWindow, QSplitter, QTextBrowser, QListWidget, QListWidgetItem, QLabel, QPushButton,
    QFileDialog, QMessageBox, QAbstractItemView)


class ImageView(QGraphicsView):
    """Показывает изображение без изменения сохранённого исходника."""
    view_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setStyleSheet('background: #111827; border: 0;')
        self.horizontalScrollBar().valueChanged.connect(lambda: self.view_changed.emit())
        self.verticalScrollBar().valueChanged.connect(lambda: self.view_changed.emit())

    def set_image(self, path):
        self.set_pixmap(QPixmap(str(path)))

    def set_pixmap(self, pixmap):
        self.scene().clear()
        if not pixmap.isNull():
            self.scene().addPixmap(pixmap)
            self.setSceneRect(self.scene().itemsBoundingRect())
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        self.scale(1.2 if event.angleDelta().y() > 0 else 1/1.2, 1.2 if event.angleDelta().y() > 0 else 1/1.2)
        self.view_changed.emit()


def frame_pixmap(frame):
    """Передаёт независимую копию кадра в Qt без обращения к файлу."""
    import cv2
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return QPixmap.fromImage(QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format.Format_RGB888).copy())


class ResultsWindow(QMainWindow):
    """Показывает серию, выбранный кадр, отчёт и общую сводку."""
    def __init__(self, folder, queue_dir, parent=None):
        super().__init__(parent)
        self.folder, self.queue_dir = Path(folder), Path(queue_dir)
        self.session = json.loads((self.folder/'session.json').read_text(encoding='utf-8'))
        self.syncing = False
        self.setWindowTitle(f"Заказ {self.session['order']} — изделие {self.session['product']} — результаты")
        self.resize(1600, 950)
        root = QWidget(); self.setCentralWidget(root); layout = QVBoxLayout(root)
        self.summary = QLabel(); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        split = QSplitter(); layout.addWidget(split, 1)
        self.left, self.right = ImageView(), ImageView()
        for title, view in [('Исходный снимок', self.left), ('Предполагаемые дефекты', self.right)]:
            panel=QWidget(); stack=QVBoxLayout(panel); stack.addWidget(QLabel(title)); stack.addWidget(view); split.addWidget(panel)
        self.text = QTextBrowser(); split.addWidget(self.text); split.setSizes([600,600,400])
        self.gallery = QListWidget(); self.gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.gallery.setIconSize(QSize(110,75)); self.gallery.setMaximumHeight(125)
        self.gallery.setResizeMode(QListWidget.ResizeMode.Adjust); layout.addWidget(self.gallery)
        buttons=QHBoxLayout(); layout.addLayout(buttons)
        for label, callback in [('Вписать оба снимка', self.fit), ('Передать кадр на разметку', self.queue_image), ('Сохранить копию проверки…', self.export)]:
            button=QPushButton(label); button.clicked.connect(callback); buttons.addWidget(button)
        self.reports = {}
        analysis = self.session.get('analysis_dir')
        if analysis:
            for path in (self.folder/analysis).glob('*.json'):
                data=json.loads(path.read_text(encoding='utf-8'))
                if 'image_defects' in data:
                    self.reports[Path(data['image']).name]=(path,data)
        for shot in self.session['shots']:
            image=self.folder/'originals'/shot['filename']
            data=self.reports.get(shot['filename'], (None,{}))[1]
            tag='!' if data.get('status')=='Требуется проверка' else '?' if data.get('status') in (None,'Не удалось определить') else '✓'
            item=QListWidgetItem(QIcon(str(image)), f"{shot['index']:02d} {tag}"); self.gallery.addItem(item)
        self.gallery.currentRowChanged.connect(self.select)
        self.left.view_changed.connect(lambda:self.sync(self.left,self.right))
        self.right.view_changed.connect(lambda:self.sync(self.right,self.left))
        warnings=sum(d['status']=='Требуется проверка' for _,d in self.reports.values())
        unknown=sum(d['status']=='Не удалось определить' for _,d in self.reports.values())
        overall='Требуется проверка' if warnings else 'Не удалось определить' if unknown else 'Дефекты не обнаружены'
        if not self.reports: overall='Анализ не выполнен'
        elif not warnings and (len(self.reports)<len(self.session['shots']) or self.session.get('analysis_status')!='complete'):
            overall='Не удалось определить: анализ серии не завершён'
        self.summary.setText(f"{overall} | Снято {len(self.session['shots'])}/15 | С предупреждениями: {warnings} | Неопределённых: {unknown} | Серия: {self.session['capture_status']}\nРакурсы связаны; число областей не равно числу уникальных повреждений изделия.")
        if self.session['shots']:self.gallery.setCurrentRow(0)

    def sync(self, first, second):
        # Одинаковые размеры исходника и наложения позволяют синхронизировать координаты.
        if self.syncing:return
        self.syncing=True
        second.setTransform(first.transform())
        second.horizontalScrollBar().setValue(first.horizontalScrollBar().value())
        second.verticalScrollBar().setValue(first.verticalScrollBar().value())
        self.syncing=False

    def fit(self):
        self.syncing=True
        for view in (self.left,self.right):view.fitInView(view.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)
        self.syncing=False

    def select(self,index):
        if index<0:return
        shot=self.session['shots'][index]; image=self.folder/'originals'/shot['filename']
        self.syncing=True; self.left.set_image(image)
        pair=self.reports.get(shot['filename'])
        annotated=pair[0].with_suffix('.jpg') if pair else image
        self.right.set_image(annotated if annotated.exists() else image);self.syncing=False
        if not pair:
            self.text.setPlainText('Анализ не выполнен. Снимок сохранён и доступен для разметки.');return
        data=pair[1]; lines=[data['status'],'',f"Блик: {'Да' if data['glare'] else 'Нет'}",f"Подложка / каркас: {'Да' if data['support_present'] else 'Нет'}"]
        for number,product in enumerate(data['products'],1):
            lines.extend(['',f"Изделие {number}: {product['product']['class_name']}"])
            for defect in product['defects']:
                value='Не применимо' if not defect['applicable'] else 'Область не найдена'
                if defect['probability_percent'] is not None:value=f"{defect['probability_percent']}% (правило)"
                elif defect['confidence_percent'] is not None:value=f"Уверенность области: {defect['confidence_percent']}%"
                lines.append(f"{defect['name']}: {value}")
        for support in data['supports']:
            lines.append('\nПодложка / каркас:')
            lines += [f"{d['name']}: {d['confidence_percent']}% — требуется проверка" for d in support['defects'] if d['detected']]
        lines.extend(['','Вероятности по всему снимку:'])
        for defect in data.get('image_defects',[]):
            value=defect.get('probability_percent')
            lines.append(f"{defect['name']}: {value if value is not None else 'не откалибровано'}"+('%' if value is not None else ''))
        lines.extend(['','Калибровка относится к снимку, а не к отдельным буквам. Уверенность области не равна вероятности дефекта.'])
        self.text.setPlainText('\n'.join(lines))

    def queue_image(self):
        # Снимок копируется в очередь, но автоматически правильной разметкой не становится.
        index=self.gallery.currentRow()
        if index<0:return
        filename=self.session['shots'][index]['filename']
        self.queue_dir.mkdir(parents=True,exist_ok=True)
        target=self.queue_dir/f"{self.session['order']}_{self.session['product']}_серия{self.session['series']}_{filename}"
        if target.exists():
            QMessageBox.information(self,'Очередь','Этот кадр уже находится в очереди.');return
        shutil.copy2(self.folder/'originals'/filename,target)
        # Группа очереди сохраняет связь разных ракурсов одного изделия.
        from app.services import atomic_json
        registry=self.queue_dir/'queue_groups.json'
        groups=json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else {}
        groups[target.name]=f"{self.session['order']}__{self.session['product']}"
        atomic_json(registry,groups)
        QMessageBox.information(self,'Очередь',f'Скопировано для ручной разметки:\n{target}')

    def export(self):
        parent=QFileDialog.getExistingDirectory(self,'Папка для копии проверки')
        if not parent:return
        target=Path(parent)/f"заказ_{self.session['order']}_изделие_{self.session['product']}_серия_{self.session['series']}"
        if target.exists():QMessageBox.warning(self,'Копия','Такая папка уже существует.');return
        try:
            if self.folder.resolve() in target.resolve().parents:raise ValueError('Нельзя копировать проверку внутрь самой себя.')
            shutil.copytree(self.folder,target)
            QMessageBox.information(self,'Копия','Проверка сохранена.')
        except Exception as error:QMessageBox.warning(self,'Копия',str(error))
