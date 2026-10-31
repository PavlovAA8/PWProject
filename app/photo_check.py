"""Проверка отдельной загруженной фотографии без заказа, камеры и серии."""
import json
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QImageReader,QPixmap
from PySide6.QtWidgets import (QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QSplitter,
    QLabel,QTextBrowser,QPushButton,QFileDialog,QMessageBox)
from config import NAMES
from app.viewers import ImageView


def prepare_photo(source, root):
    """Проверяет декодирование и создаёт отдельную копию, не меняя оригинал."""
    source=Path(source)
    if not source.is_file():raise FileNotFoundError('Файл фотографии не найден.')
    reader=QImageReader(str(source));reader.setAutoTransform(True)
    image=reader.read()
    if image.isNull():raise ValueError('Не удалось прочитать фотографию: '+reader.errorString())
    # Уникальная техническая папка не требует номера заказа или изделия.
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    folder=root/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid4().hex[:10])
    folder.mkdir()
    target=folder/source.name
    shutil.copy2(source,target)
    return folder,target


def report_text(data):
    """Формирует понятный отчёт, отделяя уверенность от вероятности."""
    lines=[data.get('status','Не удалось определить'),'']
    lines.extend([f"Блик: {'Да' if data.get('glare') else 'Нет'}",
                  f"Подложка / каркас: {'Да' if data.get('support_present') else 'Нет'}"])
    for number,item in enumerate(data.get('products',[]),1):
        product=item['product']
        lines.extend(['',f"Изделие {number}: {product['class_name']}",
                      f"Уверенность типа: {product['confidence']*100:.1f}%"])
        for defect in item['defects']:
            if defect.get('probability_percent') is not None:
                value=f"{defect['probability_percent']}% (правило)"
            elif not defect.get('applicable',True):value='Не применимо'
            elif defect.get('confidence_percent') is not None:value=f"уверенность области {defect['confidence_percent']}%"
            else:value='Область не найдена'
            if defect.get('detected'):value+=' — требуется проверка'
            lines.append(f"{defect['name']}: {value}")
    if not data.get('products'):
        lines.extend(['','Тип продукции не определён достаточно уверенно. Это не означает отсутствие брака.'])
    for support in data.get('supports',[]):
        lines.append('\nДефекты подложки / каркаса:')
        found=[d for d in support['defects'] if d.get('detected')]
        lines.extend([f"{d['name']}: уверенность области {d['confidence_percent']}%" for d in found] or ['Предупреждений нет.'])
    orphans=[r for r in data.get('unassigned_regions',[]) if 5<=r['class_id']<=10]
    if orphans:
        lines.append('\nКандидаты без надёжной привязки к изделию:')
        lines.extend(f"{NAMES[r['class_id']]}: уверенность {r['confidence']*100:.1f}%" for r in orphans)
    lines.append('\nВероятности по всему снимку:')
    for defect in data.get('image_defects',[]):
        probability=defect.get('probability_percent')
        value=f'{probability}%' if probability is not None else 'Не откалибровано' if defect.get('applicable') else 'Не применимо'
        lines.append(f"{defect['name']}: {value}")
    lines.extend(['','Проценты уверенности областей не являются вероятностью наличия дефекта.',
                  '«Дефекты не обнаружены» не гарантирует отсутствия повреждений.'])
    return '\n'.join(lines)


class PhotoResultWindow(QMainWindow):
    """Оригинал, выделения и отчёт для одного файла без галереи серий."""
    def __init__(self, report_path, parent=None):
        super().__init__(parent)
        self.report_path=Path(report_path)
        self.data=json.loads(self.report_path.read_text(encoding='utf-8'))
        self.source=Path(self.data['image']);self.folder=self.report_path.parent.parent
        # Экспортированная копия может открываться после переноса в другую папку.
        if not self.source.is_file():self.source=self.folder/self.source.name
        self.syncing=False
        self.setWindowTitle('Проверка фотографии — '+self.source.name);self.resize(1600,950)
        root=QWidget();self.setCentralWidget(root);layout=QVBoxLayout(root)
        self.summary=QLabel(self.data['status']+'  |  '+self.source.name);self.summary.setWordWrap(True)
        self.summary.setStyleSheet('font-size:18px; font-weight:600;');layout.addWidget(self.summary)
        split=QSplitter();layout.addWidget(split,1)
        self.left,self.right=ImageView(),ImageView()
        for title,view in [('Исходная фотография',self.left),('Предполагаемые дефекты',self.right)]:
            panel=QWidget();column=QVBoxLayout(panel);column.addWidget(QLabel(title));column.addWidget(view);split.addWidget(panel)
        self.text=QTextBrowser();self.text.setPlainText(report_text(self.data));split.addWidget(self.text)
        split.setSizes([600,600,400])
        # Учитываем ориентацию JPEG, чтобы сравнивать одинаково повёрнутые снимки.
        reader=QImageReader(str(self.source));reader.setAutoTransform(True)
        image=reader.read()
        if image.isNull():raise ValueError('Не удалось открыть сохранённую копию фотографии.')
        self.left.set_pixmap(QPixmap.fromImage(image))
        annotated=self.report_path.with_suffix('.jpg')
        if annotated.exists():self.right.set_image(annotated)
        else:
            self.right.set_pixmap(QPixmap.fromImage(image))
            self.summary.setText(self.summary.text()+' | Визуализация недоступна; смотрите отчёт')
        self.left.view_changed.connect(lambda:self.sync(self.left,self.right))
        self.right.view_changed.connect(lambda:self.sync(self.right,self.left))
        row=QHBoxLayout();layout.addLayout(row)
        for title,callback in [('Вписать оба снимка',self.fit),('Сохранить результат…',self.export_result),('Проверить другую фотографию',self.another_photo)]:
            button=QPushButton(title);button.clicked.connect(callback);row.addWidget(button)
        self.statusBar().showMessage('Результат сохранён: '+str(self.folder))
        # Подгоняем масштаб после фактического размещения трёх столбцов окна.
        QTimer.singleShot(0,self.fit)

    def sync(self,first,second):
        """Синхронизирует масштаб и положение без рекурсивных сигналов."""
        if self.syncing:return
        self.syncing=True
        second.setTransform(first.transform())
        second.horizontalScrollBar().setValue(first.horizontalScrollBar().value())
        second.verticalScrollBar().setValue(first.verticalScrollBar().value())
        self.syncing=False

    def fit(self):
        """Возвращает общий масштаб сравнения."""
        self.syncing=True
        for view in (self.left,self.right):view.fitInView(view.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)
        self.syncing=False

    def another_photo(self):
        """Открывает следующий файл через главную программу."""
        owner=self.parent()
        if owner and hasattr(owner,'check_photo'):owner.check_photo()

    def export_result(self):
        """Копирует фотографию, выделения и JSON без перезаписи существующих результатов."""
        parent=QFileDialog.getExistingDirectory(self,'Куда сохранить копию результата?')
        if not parent:return
        target=Path(parent)/('проверка_фото_'+self.folder.name)
        try:
            if target.exists():raise ValueError('Такая папка уже существует. Выберите другое место.')
            if self.folder.resolve() in target.resolve().parents:raise ValueError('Нельзя сохранять копию внутрь самой проверки.')
            shutil.copytree(self.folder,target)
            QMessageBox.information(self,'Сохранено',str(target))
        except Exception as error:QMessageBox.warning(self,'Сохранение',str(error))
