import sys, json, os, shutil, copy, math
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import Qt, QRectF, QTimer, QPointF
from PySide6.QtGui import QColor, QPen, QPixmap, QImageReader, QShortcut, QKeySequence, QPolygonF, QPainter, QCursor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QSpinBox, QListWidget, QListWidgetItem,
    QColorDialog, QInputDialog, QMessageBox, QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsPolygonItem,
    QSplitter, QAbstractItemView, QDialog, QDialogButtonBox, QKeySequenceEdit, QCheckBox, QComboBox)

from config import NAMES, BASE
from data import get_group_id
ROOT = BASE / 'workspace' / 'annotation'
ROOT.mkdir(parents=True, exist_ok=True)
COLORS = ['#e74c3c','#e67e22','#f1c40f','#2ecc71','#1abc9c','#3498db','#9b59b6','#e84393','#00cec9','#fdcb6e','#6c5ce7','#81ecec','#b2bec3']
DEFAULT_CLASSES = [{'name': n, 'color': COLORS[i], 'hotkey': str(i+1) if i < 9 else ''} for i,n in enumerate(NAMES)]

# Безопасная запись состояния через временный файл.
def atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)

class ClassDialog(QDialog):
    # Инициализация окна или элемента интерфейса.
    def __init__(self, parent, data):
        super().__init__(parent)
        self.setWindowTitle('Настройка класса')
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Название класса'))
        self.name = QLineEdit(data.get('name', '')); self.name.setReadOnly(True); layout.addWidget(self.name)
        self.color = data.get('color', '#ffcc00')
        self.color_button = QPushButton(); layout.addWidget(self.color_button)
        self.color_button.clicked.connect(self.pick_color); self.update_color()
        layout.addWidget(QLabel('Горячая клавиша: нажмите нужную клавишу или сочетание'))
        self.key = QKeySequenceEdit(QKeySequence(data.get('hotkey', '')))
        layout.addWidget(self.key)
        clear = QPushButton('Убрать горячую клавишу'); clear.clicked.connect(self.key.clear); layout.addWidget(clear)
        self.error = QLabel(); self.error.setStyleSheet('color: #de4545'); layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
    def update_color(self):
        self.color_button.setText('Цвет рамки: ' + self.color)
        self.color_button.setStyleSheet(f'border: 3px solid {self.color}; padding: 8px;')
    def pick_color(self):
        color = QColorDialog.getColor(QColor(self.color), self, 'Цвет рамки')
        if color.isValid(): self.color = color.name(); self.update_color()
    # Проверка введённых параметров перед подтверждением.
    def validate(self):
        if not self.name.text().strip(): self.error.setText('Введите название класса.'); return
        if self.key.keySequence().count() > 1:
            self.error.setText('Используйте одну клавишу или одно сочетание.'); return
        self.accept()
    def data(self):
        return dict(name=self.name.text().strip(), color=self.color,
                    hotkey=self.key.keySequence().toString(QKeySequence.SequenceFormat.PortableText))

# Преобразование прямоугольника в четыре вершины полигона.
def rect_points(x, y, w, h):
    return [[x,y],[x+w,y],[x+w,y+h],[x,y+h]]

# Чтение сохранённой геометрии фигуры.
def as_annotation(value):
    if isinstance(value, dict):
        return {'class_id':int(value['class_id']), 'points':[[float(x),float(y)] for x,y in value['points']]}
    c,x,y,w,h=value
    return {'class_id':int(c),'points':rect_points(x,y,w,h)}

# Проверка количества вершин, площади и корректности контура.
def valid_polygon(points):
    if len(points)<3: return False
    area=abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(points,points[1:]+points[:1]))) / 2
    if area<=1e-12: return False
    def cross(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def on(a,b,c): return abs(cross(a,b,c))<1e-8 and min(a[0],b[0])-1e-8<=c[0]<=max(a[0],b[0])+1e-8 and min(a[1],b[1])-1e-8<=c[1]<=max(a[1],b[1])+1e-8
    for i,a in enumerate(points):
        b=points[(i+1)%len(points)]
        if (a[0]-b[0])**2+(a[1]-b[1])**2<1e-8: return False
        for j in range(i+1,len(points)):
            if j==i+1 or (i==0 and j==len(points)-1): continue
            c,d=points[j],points[(j+1)%len(points)]
            if cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0: return False
            if on(a,b,c) or on(a,b,d) or on(c,d,a) or on(c,d,b): return False
    return True

# Нормализация вершин и запись строки YOLO Segmentation.
def yolo_line(annotation, width, height):
    a=as_annotation(annotation)
    if not valid_polygon(a['points']): raise ValueError('Контур вырожден или имеет самопересечения.')
    if any(not (0<=x<=width and 0<=y<=height) for x,y in a['points']): raise ValueError('Контур выходит за границы фотографии.')
    coords=' '.join(f'{x/width:.8f} {y/height:.8f}' for x,y in a['points'])
    return f'{a["class_id"]} {coords}'

class Polygon(QGraphicsPolygonItem):
    # Инициализация окна или элемента интерфейса.
    def __init__(self, canvas, cid, points):
        super().__init__(); self.canvas=canvas; self.cid=cid
        self.setFlag(self.GraphicsItemFlag.ItemIsSelectable,True); self.setZValue(1)
        self.set_points(points); self.refresh()
    # Обновление геометрии выделенной фигуры.
    def set_points(self,points):
        self.setPolygon(QPolygonF([QPointF(x,y) for x,y in points]))
    # Чтение вершин фигуры.
    def points(self): return [[p.x(),p.y()] for p in self.polygon()]
    # Отрисовка выделения поверх исходной фотографии.
    def paint(self,painter,option,widget=None):
        painter.setPen(self.pen());painter.setBrush(Qt.BrushStyle.NoBrush);painter.drawPolygon(self.polygon())
    # Обновление цвета и подписи фигуры.
    def refresh(self):
        c=self.canvas.owner.classes[self.cid]; pen=QPen(QColor(c['color']),2); pen.setCosmetic(True)
        self.setPen(pen); self.setBrush(Qt.BrushStyle.NoBrush); self.setToolTip(c['name'])

class Canvas(QGraphicsView):
    # Инициализация окна или элемента интерфейса.
    def __init__(self,owner):
        super().__init__();self.owner=owner
        self.setScene(QGraphicsScene(self));self.setBackgroundBrush(QColor('#232830'))
        self.setViewportUpdateMode(self.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(self.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(self.ViewportAnchor.AnchorViewCenter)
        self.image_rect=QRectF(); self.image_item=None; self.image_dpr=1.0
        self.setMouseTracking(True); self.viewport().setMouseTracking(True)
        self.start=None;self.draft=[];self.cursor_point=None;self.panning=False;self.drag=None;self.vertex=None
        # Флаг активной траектории сбрасывается при отпускании мыши.
        self.tracing = False
        self.scene().selectionChanged.connect(owner.selection_changed)
    # Ограничение точки границами исходного изображения.
    def clamp(self,p):
        r=self.image_rect;return QPointF(max(0,min(r.width(),p.x())),max(0,min(r.height(),p.y())))
    def tolerance(self): return 9/max(.01,self.transform().m11())
    # Поиск вершины рядом с курсором для редактирования.
    def near_vertex(self,item,p):
        return next((i for i,(x,y) in enumerate(item.points()) if (p.x()-x)**2+(p.y()-y)**2 <= self.tolerance()**2),None)
    def selected(self): return next((x for x in self.scene().selectedItems() if isinstance(x,Polygon)),None)
    def select(self,item):
        self.scene().blockSignals(True);self.scene().clearSelection()
        if item: item.setSelected(True)
        self.scene().blockSignals(False);self.owner.selection_changed();self.viewport().update()
    def incomplete(self): return self.start is not None or bool(self.draft)
    def update_image_filter(self):
        # При уменьшении сглаживаем изображение, при масштабе 1:1 показываем исходные пиксели.
        if self.image_item is not None:
            mode=Qt.TransformationMode.SmoothTransformation if self.transform().m11()*self.image_dpr < .999 else Qt.TransformationMode.FastTransformation
            self.image_item.setTransformationMode(mode)
    # Изменение масштаба относительно указателя мыши.
    def zoom_at(self,position,factor):
        if self.owner.current is None:return
        if not .02 < self.transform().m11()*factor < 40:return
        before=self.mapToScene(position)
        self.scale(factor,factor)
        after=self.mapToScene(position)
        self.centerOn(self.mapToScene(self.viewport().rect().center())+before-after)
        self.update_image_filter();self.update_scale_text()
    # Обработка колеса мыши для изменения масштаба.
    def wheelEvent(self,event):
        delta=event.angleDelta().y()
        if not delta:return
        self.zoom_at(event.position().toPoint(),1.2**(delta/120))
        event.accept()
    def update_scale_text(self):
        if self.owner.current is not None:
            self.owner.scale_label.setText(f'{self.transform().m11()*self.image_dpr*100:.0f}% · {int(self.image_rect.width())}×{int(self.image_rect.height())} px')
    # Показ исходных пикселей в масштабе 1:1.
    def original_size(self):
        if self.owner.current is None:return
        center=self.mapToScene(self.viewport().rect().center())
        self.resetTransform();self.scale(1/self.image_dpr,1/self.image_dpr);self.centerOn(center)
        self.update_image_filter();self.update_scale_text()
    # Курсор показывает текущий инструмент разметки.
    def update_tool_cursor(self):
        if not self.owner.draw_mode:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor);return
        if not hasattr(self,'tool_cursors'):
            self.tool_cursors={}
            for tool in ('rect','points'):
                pix=QPixmap(40,40);pix.fill(Qt.GlobalColor.transparent)
                painter=QPainter(pix);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                # Белая обводка сохраняет видимость курсора на светлом и тёмном фоне.
                for color,width in [('white',4),('#171717',2)]:
                    painter.setPen(QPen(QColor(color),width));painter.drawLine(1,10,19,10);painter.drawLine(10,1,10,19)
                    if tool=='rect':painter.drawRect(23,21,14,12)
                    else:
                        painter.drawLine(23,33,28,21);painter.drawLine(28,21,37,31);painter.drawLine(37,31,23,33)
                if tool=='points':
                    painter.setPen(QPen(QColor('#171717'),1));painter.setBrush(QColor('white'))
                    for x,y in [(23,33),(28,21),(37,31)]:painter.drawEllipse(QPointF(x,y),2,2)
                painter.end();self.tool_cursors[tool]=QCursor(pix,10,10)
        self.viewport().setCursor(self.tool_cursors[self.owner.tool])
    # Начало рисования, выбора или перемещения фигуры.
    def mousePressEvent(self,event):
        self.setFocus()
        if event.button()==self.owner.toggle_mouse_button():
            self.owner.toggle_tool();event.accept();return
        if event.button()==Qt.MouseButton.MiddleButton:
            self.panning=True;self.pan=event.position();self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor);return
        if event.button()!=Qt.MouseButton.LeftButton or self.owner.current is None: return
        p=self.clamp(self.mapToScene(event.position().toPoint()));self.cursor_point=p
        if self.owner.draw_mode:
            if self.owner.tool=='rect': self.start=p
            else:
                if len(self.draft)>=3 and (p-self.draft[0]).manhattanLength()<=self.tolerance(): self.finish_polygon();return
                if not self.draft or (p-self.draft[-1]).manhattanLength()>1e-5: self.draft.append(p)
                # Движение добавляет вершины только при включённой настройке.
                self.tracing = self.owner.trace_checkbox.isChecked()
            self.select(None);self.viewport().update();return
        selected=self.selected();v=self.near_vertex(selected,p) if selected else None
        if v is not None:
            self.vertex=v
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                self.remove_vertex();return
            item=selected
        else:
            item=next((i for i in self.items(event.position().toPoint()) if isinstance(i,Polygon)),None)
            self.vertex=None
        self.select(item)
        if item:
            self.owner.checkpoint();self.drag=(item,v,p,item.points())
    # Обновление положения фигуры или вершины при движении мыши.
    def mouseMoveEvent(self,event):
        if self.panning:
            d=event.position()-self.pan;self.pan=event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-int(d.y()));return
        p=self.clamp(self.mapToScene(event.position().toPoint()));self.cursor_point=p
        # Рисуем траекторию отдельно от перемещения готовой фигуры.
        if (self.tracing and self.owner.draw_mode and self.owner.tool == 'points'
                and event.buttons() & Qt.MouseButton.LeftButton):
            self.append_trace_point(p)
            self.viewport().update()
            return
        if self.drag:
            item,v,origin,points=self.drag;new=copy.deepcopy(points)
            if v is not None: new[v]=[p.x(),p.y()]
            else:
                dx=max(-min(x for x,y in points),min(p.x()-origin.x(),self.image_rect.width()-max(x for x,y in points)))
                dy=max(-min(y for x,y in points),min(p.y()-origin.y(),self.image_rect.height()-max(y for x,y in points)))
                new=[[x+dx,y+dy] for x,y in points]
            item.set_points(new)
        self.viewport().update()
    # Завершение перетаскивания и запись изменения.
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.MouseButton.MiddleButton: self.panning=False;self.update_tool_cursor();return
        if event.button()!=Qt.MouseButton.LeftButton: return
        # Отпускание не замыкает полигон: его можно продолжить следующим нажатием.
        if self.tracing:
            point = self.clamp(self.mapToScene(event.position().toPoint()))
            self.append_trace_point(point, force=True)
            self.tracing = False
            self.viewport().update()
            return
        p=self.clamp(self.mapToScene(event.position().toPoint()))
        if self.start is not None:
            r=QRectF(self.start,p).normalized();self.start=None
            if r.width()>=2 and r.height()>=2: self.add_polygon(rect_points(r.x(),r.y(),r.width(),r.height()))
        if self.drag:
            item,v,origin,old=self.drag;self.drag=None
            if not valid_polygon(item.points()):
                item.set_points(old);self.owner.statusBar().showMessage('Недопустимый контур: изменение отменено.',5000)
            self.owner.changed()
        self.viewport().update()
    # Замыкание контура или добавление вершины при редактировании.
    def mouseDoubleClickEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton: return
        if self.owner.draw_mode:
            if self.owner.tool=='points': self.finish_polygon()
            return
        item=self.selected()
        if item is None:return
        p=self.clamp(self.mapToScene(event.position().toPoint()));points=item.points();best=None
        for i,(a,b) in enumerate(zip(points,points[1:]+points[:1])):
            dx,dy=b[0]-a[0],b[1]-a[1];length=dx*dx+dy*dy
            t=max(0,min(1,((p.x()-a[0])*dx+(p.y()-a[1])*dy)/length)) if length else 0
            q=[a[0]+t*dx,a[1]+t*dy];distance=(q[0]-p.x())**2+(q[1]-p.y())**2
            if best is None or distance<best[0]: best=(distance,i,q,t)
        if best and best[0]<=self.tolerance()**2 and .001<best[3]<.999:
            self.owner.checkpoint();points.insert(best[1]+1,best[2]);item.set_points(points);self.owner.changed()
        self.drag=None;self.viewport().update()
    # Добавление проверенного полигона на холст.
    def add_polygon(self,points):
        if not valid_polygon(points):
            self.owner.statusBar().showMessage('Нужно минимум 3 вершины и контур без самопересечений.',5000);return False
        self.owner.checkpoint();self.scene().addItem(Polygon(self,self.owner.cid,points));self.owner.changed();return True
    def append_trace_point(self, point, force=False):
        """Добавляет вершину после достаточного перемещения на экране."""
        if not self.draft:
            self.draft.append(QPointF(point))
            return
        # Шаг не зависит от разрешения исходника, но уточняется при увеличении.
        previous = self.draft[-1]
        scale = abs(self.transform().m11())
        distance = math.hypot(point.x()-previous.x(), point.y()-previous.y()) * scale
        minimum = 1.0 if force else float(self.owner.trace_step.value())
        if distance >= minimum:
            self.draft.append(QPointF(point))

    def remove_last_point(self):
        """Удаляет только последнюю вершину незавершённого контура."""
        self.tracing = False
        if not self.draft:
            self.owner.statusBar().showMessage('Нет незавершённого контура. Для готовой фигуры используйте редактирование вершин.', 4000)
            return
        self.draft.pop()
        self.viewport().update()
        self.setFocus()

    # Завершение незамкнутого контура.
    def finish_polygon(self):
        self.tracing = False
        if self.add_polygon([[p.x(),p.y()] for p in self.draft]): self.draft=[];self.viewport().update()
    # Удаление выбранной вершины с проверкой геометрии.
    def remove_vertex(self):
        item=self.selected()
        if item is None or self.vertex is None:return
        points=item.points()
        if len(points)<=3:self.owner.statusBar().showMessage('У полигона должно остаться минимум 3 вершины.',5000);return
        points.pop(self.vertex)
        if not valid_polygon(points):return
        self.owner.checkpoint();item.set_points(points);self.vertex=None;self.owner.changed();self.viewport().update()
    # Отрисовка незавершённого контура и вершин.
    def drawForeground(self,painter,rect):
        super().drawForeground(painter,rect)
        if self.owner.current is None:return
        pen=QPen(QColor(self.owner.classes[self.owner.cid]['color']),2);pen.setCosmetic(True);painter.setPen(pen);painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.start is not None and self.cursor_point is not None:painter.drawRect(QRectF(self.start,self.cursor_point).normalized())
        if self.draft:
            chain=self.draft+([self.cursor_point] if self.cursor_point is not None else [])
            painter.drawPolyline(QPolygonF(chain))
            size=6/max(.01,self.transform().m11());painter.setBrush(QColor('white'))
            for p in self.draft:painter.drawEllipse(p,size/2,size/2)
        item=self.selected()
        if item:
            painter.setPen(item.pen());painter.setBrush(QColor('white'));size=8/max(.01,self.transform().m11())
            for x,y in item.points():painter.drawRect(QRectF(x-size/2,y-size/2,size,size))
    # Отмена незавершённого действия.
    def cancel(self):
        self.tracing = False
        self.start=None;self.draft=[];self.drag=None;self.vertex=None;self.scene().clearSelection();self.viewport().update()
    def boxes(self):return [i for i in self.scene().items() if isinstance(i,Polygon)]
    def data(self):return [{'class_id':b.cid,'points':b.points()} for b in reversed(self.boxes())]
    # Восстановление фигур текущего снимка из сессии.
    def restore(self,data):
        for b in self.boxes():self.scene().removeItem(b)
        for value in data:
            a=as_annotation(value);self.scene().addItem(Polygon(self,a['class_id'],a['points']))
    # Вписывание фотографии в окно без изменения исходника.
    def fit(self):
        if self.owner.current is not None:
            self.fitInView(self.image_rect,Qt.AspectRatioMode.KeepAspectRatio);self.update_image_filter();self.update_scale_text()

class Window(QMainWindow):
    # Инициализация окна или элемента интерфейса.
    def __init__(self):
        super().__init__(); self.setWindowTitle('Даймонд Трейд — ручная разметка, 13 классов'); self.resize(1400,900)
        self.statepath=ROOT/'session.json'; self.classpath=ROOT/'classes.json'
        self.settingspath=ROOT/'settings.json'
        self.settings=json.loads(self.settingspath.read_text(encoding='utf-8')) if self.settingspath.exists() else {'toggle_key':'Tab','toggle_mouse':'XButton1'}
        self.tool='rect'; self.tool_shortcut=None
        self.classes=copy.deepcopy(DEFAULT_CLASSES)
        for n,c in enumerate(self.classes):
            c.setdefault('hotkey', str(n+1) if n < 9 else '')
        self.state={}; self.files=[]; self.index=0; self.current=None; self.cid=0; self.history=[]; self.loading=False; self.draw_mode=True; self.class_shortcuts=[]
        widget=QWidget(); self.setCentralWidget(widget); layout=QVBoxLayout(widget)
        self.paths=[]
        for title in ['Исходные фотографии','Сохранённые фотографии','Файлы labels']:
            row=QHBoxLayout(); row.addWidget(QLabel(title)); edit=QLineEdit(); self.paths.append(edit); row.addWidget(edit)
            button=QPushButton('Выбрать…'); button.clicked.connect(lambda checked=False,e=edit:self.pick(e)); row.addWidget(button); layout.addLayout(row)
        row=QHBoxLayout(); row.addWidget(QLabel('Следующий номер ART_')); self.number=QSpinBox(); self.number.setRange(0,999999999); self.number.setValue(1); row.addWidget(self.number)
        self.button(row,'Начать / продолжить',self.begin); self.button(row,'Вписать в окно',lambda:self.canvas.fit()); self.button(row,'Оригинал 1:1',lambda:self.canvas.original_size()); self.scale_label=QLabel();row.addWidget(self.scale_label); self.progress=QLabel('Выберите папки'); row.addWidget(self.progress); layout.addLayout(row)
        split=QSplitter(); layout.addWidget(split,1); self.canvas=Canvas(self); split.addWidget(self.canvas)
        side=QWidget(); tools=QVBoxLayout(side)
        self.tool_button=QPushButton('Инструмент: Прямоугольник → полигон'); self.tool_button.clicked.connect(self.toggle_tool); tools.addWidget(self.tool_button)
        # Можно сочетать отдельные клики и движение с зажатой левой кнопкой.
        self.trace_checkbox = QCheckBox('Рисовать контур зажатой ЛКМ')
        self.trace_checkbox.setChecked(True)
        tools.addWidget(self.trace_checkbox)
        trace_row = QHBoxLayout()
        trace_row.addWidget(QLabel('Шаг точек на экране:'))
        self.trace_step = QSpinBox()
        self.trace_step.setRange(2, 30)
        self.trace_step.setValue(6)
        self.trace_step.setSuffix(' пикс.')
        self.trace_step.setToolTip('Меньше шаг — больше вершин. Для мелких дефектов увеличьте изображение.')
        trace_row.addWidget(self.trace_step)
        tools.addLayout(trace_row)
        self.button(tools,'Клавиша / кнопка мыши переключателя',self.configure_toggle)
        self.mode_button=QPushButton('Режим: РИСОВАНИЕ (R)'); self.mode_button.clicked.connect(self.toggle_mode); tools.addWidget(self.mode_button)
        tools.addWidget(QLabel('Классы — назначаемые горячие клавиши'))
        self.classlist=QListWidget(); self.classlist.currentRowChanged.connect(self.choose_class); self.classlist.itemClicked.connect(lambda item:self.choose_class(self.classlist.row(item))); tools.addWidget(self.classlist)
        row=QHBoxLayout(); self.button(row,'Цвет / клавиша',self.edit_class); tools.addLayout(row)
        tools.addWidget(QLabel('Выделения текущего фото'))
        self.boxlist=QListWidget(); self.boxlist.currentRowChanged.connect(self.select_box); tools.addWidget(self.boxlist)
        row=QHBoxLayout(); self.button(row,'Удалить',self.delete); self.button(row,'Сменить класс',self.reclass); tools.addLayout(row)
        self.button(tools,'Отменить действие (Ctrl+Z)',self.undo)
        self.button(tools,'Замкнуть контур',self.canvas.finish_polygon)
        # Backspace удаляет точку черновика, Delete по-прежнему удаляет фигуру.
        self.button(tools, 'Удалить последнюю точку (Backspace)', self.canvas.remove_last_point)
        self.last_point_shortcut = QShortcut(QKeySequence('Backspace'), self.canvas)
        self.last_point_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.last_point_shortcut.activated.connect(self.canvas.remove_last_point)

        self.button(tools,'Удалить выбранную вершину',self.canvas.remove_vertex)
        # Старые форматы автоматически не изменяем; новый проект использует полигоны.
        # helptext=QLabel('R: рисование. V: редактирование.\nПрямоугольник создаёт 4 независимые вершины.\nПо точкам: клики, затем клик по первой точке или двойной щелчок.\nРедактирование: тяните вершину или всю фигуру. Двойной щелчок на стороне — добавить вершину. Alt + клик по вершине — удалить.\nКолесо: масштаб. Средняя кнопка: перемещение (если не назначена переключателю).\nEnter: сохранить. Esc: отменить незавершённый контур.'); helptext.setWordWrap(True); tools.addWidget(helptext)
        split.addWidget(side); split.setSizes([1050,350])
        row=QHBoxLayout(); self.button(row,'← Назад',lambda:self.navigate(-1)); self.button(row,'Пропустить →',lambda:self.navigate(1));  self.button(row,'Сохранить и далее (Enter)',self.save_next); layout.addLayout(row)
        self.refresh_classes()
        for key,callback in [('Return',self.save_next),('Enter',self.save_next),('Delete',self.delete),('Ctrl+Z',self.undo),('Escape',self.canvas.cancel),('F',self.canvas.fit),('R',lambda:self.set_mode(True)),('V',lambda:self.set_mode(False))]:
            QShortcut(QKeySequence(key),self,activated=callback)

        self.rebuild_tool_shortcut()
        if self.statepath.exists():
            try:
                self.state=json.loads(self.statepath.read_text(encoding='utf-8'))
                if self.state.get('schema_version',1)<3:
                    backup=ROOT/'session_before_polygons.json'
                    if not backup.exists(): shutil.copy2(self.statepath,backup)
                    for rec in self.state.get('records',{}).values():
                        rec['polygons']=[as_annotation(b) for b in rec.get('boxes',[])]
                    self.state['schema_version']=3
                for e,p in zip(self.paths,self.state.get('paths',[])): e.setText(p)
                self.number.setValue(self.state.get('number',1))
            except (ValueError,OSError): pass
        self.timer=QTimer(self); self.timer.timeout.connect(self.persist); self.timer.start(3000)
    def toggle_mouse_button(self):
        return {'RightButton':Qt.MouseButton.RightButton,'MiddleButton':Qt.MouseButton.MiddleButton,
                'XButton1':Qt.MouseButton.XButton1,'XButton2':Qt.MouseButton.XButton2}.get(self.settings.get('toggle_mouse'),Qt.MouseButton.NoButton)
    def toggle_tool(self):
        if self.canvas.incomplete():
            self.statusBar().showMessage('Сначала завершите контур или отмените его клавишей Esc.',5000);return
        self.tool='points' if self.tool=='rect' else 'rect'
        self.tool_button.setText('Инструмент: Полигон — клики / траектория' if self.tool=='points' else 'Инструмент: Прямоугольник → полигон')
        self.set_mode(True)
    def rebuild_tool_shortcut(self):
        if self.tool_shortcut:self.tool_shortcut.setEnabled(False);self.tool_shortcut.deleteLater()
        key=self.settings.get('toggle_key','')
        self.tool_shortcut=QShortcut(QKeySequence(key),self,activated=self.activate_tool) if key else None
        self.tool_button.setToolTip(f'Клавиша: {key or "не назначена"}; мышь: {self.settings.get("toggle_mouse") or "не назначена"}')
    def activate_tool(self):
        if isinstance(QApplication.focusWidget(),(QLineEdit,QSpinBox,QKeySequenceEdit)):return
        self.toggle_tool()
    def configure_toggle(self):
        dialog=QDialog(self);dialog.setWindowTitle('Переключение инструмента');layout=QVBoxLayout(dialog)
        layout.addWidget(QLabel('Клавиша клавиатуры (можно оставить пустой)'))
        key=QKeySequenceEdit(QKeySequence(self.settings.get('toggle_key','')));layout.addWidget(key)
        clear=QPushButton('Убрать клавишу');clear.clicked.connect(key.clear);layout.addWidget(clear)
        layout.addWidget(QLabel('Кнопка мыши — работает над фотографией'))
        mouse=QComboBox()
        for label,value in [('Не назначена',''),('Правая кнопка','RightButton'),('Средняя кнопка / колесо','MiddleButton'),('Боковая кнопка 1','XButton1'),('Боковая кнопка 2','XButton2')]:mouse.addItem(label,value)
        mouse.setCurrentIndex(max(0,mouse.findData(self.settings.get('toggle_mouse',''))));layout.addWidget(mouse)
        layout.addWidget(QLabel('Левая кнопка занята рисованием. Если назначена средняя, перемещение средней кнопкой отключается.'))
        error=QLabel();error.setStyleSheet('color:#de4545');layout.addWidget(error)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        while dialog.exec()==QDialog.DialogCode.Accepted:
            seq=key.keySequence();value=seq.toString(QKeySequence.SequenceFormat.PortableText)
            if seq.count()>1:error.setText('Укажите одно сочетание клавиш.');continue
            if value and (value in {'Return','Enter','Delete','Backspace','Esc','Ctrl+Z','F','R','V','Backtab','Space'} or any(c.get('hotkey')==value for c in self.classes)):
                error.setText('Эта клавиша уже занята. Выберите другую.');continue
            self.settings.update(toggle_key=value,toggle_mouse=mouse.currentData());atomic_json(self.settingspath,self.settings)
            self.rebuild_tool_shortcut();return
    def convert_labels(self):
        if not self.paths[2].text().strip():
            QMessageBox.warning(self,'Папка labels','Сначала выберите папку labels.');return
        labdir=Path(self.paths[2].text()).expanduser().resolve()
        try:
            planned={}
            for path in sorted(labdir.glob('ART_*.txt')):
                lines=[];legacy=False
                for number,line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
                    if not line.strip():continue
                    values=line.split()
                    try:
                        cid=int(values[0]);coords=list(map(float,values[1:]))
                        if cid<0 or any(not math.isfinite(v) for v in coords):raise ValueError()
                        if len(coords)==4:
                            x,y,w,h=coords
                            if w<=0 or h<=0:raise ValueError()
                            points=rect_points(x-w/2,y-h/2,w,h);legacy=True
                        elif len(coords)>=6 and len(coords)%2==0:points=[coords[i:i+2] for i in range(0,len(coords),2)]
                        else:raise ValueError()
                        if any(not (-1e-7<=v<=1+1e-7) for p in points for v in p):raise ValueError()
                        points=[[max(0,min(1,x)),max(0,min(1,y))] for x,y in points]
                        if not valid_polygon(points):raise ValueError()
                        lines.append(str(cid)+' '+' '.join(f'{v:.8f}' for p in points for v in p))
                    except (ValueError,IndexError):raise ValueError(f'{path.name}, строка {number}: недопустимая разметка. Ничего не изменено.')
                if legacy:planned[path]='\n'.join(lines)+('\n' if lines else '')
            if not planned:
                QMessageBox.information(self,'Преобразование','Старые рамки в ART_*.txt не найдены. Существующие полигоны не изменены.');return
            backup=labdir/('backup_detection_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'));backup.mkdir()
            for path in planned:shutil.copy2(path,backup/path.name)
            try:
                for path,text in planned.items():
                    temp=path.with_suffix('.txt.tmp');temp.write_text(text,encoding='utf-8');os.replace(temp,path)
            except Exception:
                for path in planned:shutil.copy2(backup/path.name,path)
                raise
            atomic_json(labdir/'annotation_format.json',{'format':'yolo_segmentation','version':3})
            QMessageBox.information(self,'Готово',f'Преобразовано файлов: {len(planned)}.\nИсходные TXT сохранены в:\n{backup}\nФотографии и ID классов не менялись.')
        except Exception as error:QMessageBox.critical(self,'Ошибка преобразования',str(error))

    def button(self,layout,title,callback):
        b=QPushButton(title); b.clicked.connect(callback); layout.addWidget(b); return b
    def pick(self,edit):
        p=QFileDialog.getExistingDirectory(self,'Выберите папку',edit.text())
        if p: edit.setText(p)
    def refresh_classes(self):
        self.classlist.blockSignals(True); self.classlist.clear()
        for n,c in enumerate(self.classes):
            i=QListWidgetItem(f'{n}: {c["name"]}  [{c.get("hotkey", "—") or "—"}]'); i.setForeground(QColor(c['color'])); self.classlist.addItem(i)
        self.classlist.blockSignals(False); self.classlist.setCurrentRow(self.cid)
        atomic_json(self.classpath,self.classes)
        self.rebuild_class_shortcuts()
    def set_mode(self, drawing):
        if self.canvas.incomplete():
            self.statusBar().showMessage('Сначала завершите контур или отмените его клавишей Esc.',5000);return
        self.draw_mode=drawing
        self.mode_button.setText('Режим: РИСОВАНИЕ (R)' if drawing else 'Режим: РЕДАКТИРОВАНИЕ (V)')
        self.canvas.cancel()
        self.canvas.update_tool_cursor()
    def toggle_mode(self):
        self.set_mode(not self.draw_mode)
    # Выбор класса для следующего выделения.
    def choose_class(self,row):
        if row>=0:
            if self.canvas.incomplete():
                self.classlist.blockSignals(True);self.classlist.setCurrentRow(self.cid);self.classlist.blockSignals(False)
                self.statusBar().showMessage('Сначала завершите текущий контур или нажмите Esc.',5000);return
            self.cid=row; self.set_mode(True)
    def activate_class(self,row):
        if isinstance(QApplication.focusWidget(), (QLineEdit, QSpinBox, QKeySequenceEdit)): return
        self.classlist.setCurrentRow(row); self.choose_class(row)
    def rebuild_class_shortcuts(self):
        for shortcut in self.class_shortcuts: shortcut.setEnabled(False); shortcut.deleteLater()
        self.class_shortcuts=[]
        for n,c in enumerate(self.classes):
            if c.get('hotkey'):
                self.class_shortcuts.append(QShortcut(QKeySequence(c['hotkey']),self,activated=lambda n=n:self.activate_class(n)))
    # Настройка цвета и клавиши без изменения фиксированного номера класса.
    def configure_class(self, index=None):
        original=self.classes[index] if index is not None else {}
        dialog=ClassDialog(self,original)
        while dialog.exec()==QDialog.DialogCode.Accepted:
            data=dialog.data(); key=data['hotkey']
            reserved={'Return','Enter','Delete','Backspace','Esc','Ctrl+Z','F','R','V','Backtab','Space',self.settings.get('toggle_key','')}
            if key and key in reserved:
                dialog.error.setText('Эта клавиша занята командой приложения. Выберите другую.'); continue
            if key and any(n!=index and c.get('hotkey','')==key for n,c in enumerate(self.classes)):
                dialog.error.setText('Эта клавиша уже назначена другому классу.'); continue
            if any(n!=index and c['name'].casefold()==data['name'].casefold() for n,c in enumerate(self.classes)):
                dialog.error.setText('Такое название класса уже существует.'); continue
            if index is None: return
            else: data['name']=NAMES[index]; self.classes[index]=data
            self.refresh_classes()
            for box in self.canvas.boxes(): box.refresh()
            self.changed(); return
    def add_class(self):
        self.configure_class()
    def edit_class(self):
        self.configure_class(self.cid)
    # Открытие выбранных папок и возобновление разметки.
    def begin(self):
        try:
            paths=[Path(e.text()).expanduser().resolve() for e in self.paths]
            if any(not e.text().strip() for e in self.paths): raise ValueError('Выберите все три папки.')
            if not paths[0].is_dir(): raise ValueError('Папка исходных фото не найдена.')
            if len(set(paths))!=3: raise ValueError('Выберите три разные папки.')
            for p in paths[1:]: p.mkdir(parents=True,exist_ok=True)
            config=[str(p) for p in paths]
            if self.state.get('paths')!=config:
                self.state={'paths':config,'records':{},'index':0}
            self.files=sorted([p for p in paths[0].iterdir() if p.suffix.lower() in {'.jpg','.jpeg','.png','.bmp','.webp','.tif','.tiff'}])
            if not self.files: raise ValueError('В исходной папке нет поддерживаемых изображений.')
            self.index=min(self.state.get('index',0),len(self.files)-1); self.open_current()
        except Exception as e: QMessageBox.warning(self,'Не удалось начать',str(e))
    # Загрузка исходного фото и его сохранённых контуров.
    def open_current(self):
        self.loading=True; self.canvas.cancel(); self.current=self.files[self.index]
        reader=QImageReader(str(self.current)); reader.setAutoTransform(True); im=reader.read()
        self.canvas.scene().clear(); self.canvas.setSceneRect(QRectF())
        if im.isNull():
            self.current=None; self.loading=False; QMessageBox.warning(self,'Ошибка изображения',reader.errorString()); return
        pixmap=QPixmap.fromImage(im);pixmap.setDevicePixelRatio(1.0)
        self.canvas.image_item=self.canvas.scene().addPixmap(pixmap)
        self.canvas.image_dpr=self.canvas.viewport().devicePixelRatioF()
        self.canvas.image_rect=QRectF(0,0,im.width(),im.height())
        margin=max(im.width(),im.height())*100
        self.canvas.setSceneRect(self.canvas.image_rect.adjusted(-margin,-margin,margin,margin))
        self.cid=0;self.classlist.blockSignals(True);self.classlist.setCurrentRow(0);self.classlist.blockSignals(False);self.set_mode(True)
        record=self.state.setdefault('records',{}).get(str(self.current),{})
        self.canvas.restore(record.get('polygons',record.get('boxes',[]))); self.history=[]; self.loading=False; self.changed(); self.canvas.fit()
        self.progress.setText(f'{self.index+1} / {len(self.files)} — {self.current.name}')
    # Сохранение состояния для отмены действия.
    def checkpoint(self):
        if not self.loading: self.history.append(copy.deepcopy(self.canvas.data())); self.history=self.history[-100:]
    # Отмена последнего изменения геометрии.
    def undo(self):
        if self.canvas.draft:
            self.canvas.remove_last_point(); return
        if self.history: self.canvas.restore(self.history.pop()); self.changed()
    # Обновление списка фигур и сохранение сессии.
    def changed(self):
        self.boxlist.blockSignals(True); self.boxlist.clear(); self.listboxes=list(reversed(self.canvas.boxes()))
        for n,b in enumerate(self.listboxes):
            i=QListWidgetItem(f'{n+1}. {self.classes[b.cid]["name"]} — {len(b.points())} вершин'); i.setForeground(QColor(self.classes[b.cid]['color'])); self.boxlist.addItem(i)
        self.boxlist.blockSignals(False); self.persist()
    def selection_changed(self):
        if not hasattr(self,'listboxes'): return
        chosen=self.canvas.scene().selectedItems()
        self.boxlist.blockSignals(True)
        self.boxlist.setCurrentRow(next((n for n,b in enumerate(self.listboxes) if b in chosen),-1)); self.boxlist.blockSignals(False)
    def select_box(self,row):
        if row >= 0: self.set_mode(False)
        self.canvas.scene().blockSignals(True)
        self.canvas.scene().clearSelection()
        if 0<=row<len(self.listboxes): self.listboxes[row].setSelected(True)
        self.canvas.scene().blockSignals(False)
        self.selection_changed(); self.canvas.viewport().update()
    def delete(self):
        boxes=self.canvas.scene().selectedItems()
        if boxes:
            self.checkpoint()
            for b in boxes: self.canvas.scene().removeItem(b)
            self.changed()
    def reclass(self):
        chosen=self.canvas.scene().selectedItems()
        if chosen:
            self.checkpoint()
            for b in chosen: b.cid=self.cid; b.refresh()
            self.changed()
    # Сохранение сессии без изменения исходной фотографии.
    def persist(self):
        if self.loading: return
        if self.current:
            rec=self.state.setdefault('records',{}).setdefault(str(self.current),{})
            rec['polygons']=self.canvas.data(); rec.pop('boxes',None)
        self.state.update(number=self.number.value(),schema_version=3)
        if self.files: self.state['index']=self.index
        atomic_json(self.statepath,self.state)
    # Переход между изображениями с сохранением контуров.
    def navigate(self,direction):
        if not self.files: return
        if self.canvas.incomplete():
            QMessageBox.information(self,'Незавершённый контур','Замкните контур или отмените его клавишей Esc перед переходом.');return
        self.persist(); self.index=max(0,min(len(self.files)-1,self.index+direction)); self.open_current()
    def delete_source_photo(self):
        if self.current is None: return
        photo=self.current
        answer=QMessageBox.question(self,'Удаление исходного фото',
            f'Удалить {photo.name} из исходной папки без помещения в корзину?\nЭкспортированные фото и labels останутся.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes: return
        try: photo.unlink()
        except OSError as error:
            QMessageBox.critical(self,'Не удалось удалить',str(error)); return
        self.current=None; self.canvas.cancel(); self.history.clear()
        record=self.state.get('records',{}).get(str(photo))
        if record is not None: record['source_deleted']=True
        self.files.remove(photo)
        if self.files:
            self.index=min(self.index,len(self.files)-1); self.open_current()
        else:
            self.index=0; self.canvas.scene().clear(); self.canvas.setSceneRect(QRectF()); self.changed()
            self.progress.setText('В исходной папке больше нет фотографий')
        self.persist()

    def automatic_group_id(self):
        """Определяет техническую группу без ручного ввода."""
        # Ранее назначенную группу не меняем при повторном сохранении.
        record = self.state.get('records', {}).get(str(self.current), {})
        if record.get('group_id'):
            return record['group_id']
        # У снимков камеры общий номер заказа и изделия хранится в очереди.
        registry = self.current.parent / 'queue_groups.json'
        if registry.exists():
            groups = json.loads(registry.read_text(encoding='utf-8'))
            if groups.get(self.current.name):
                return groups[self.current.name]
        # Стандартные имена до/после связываются по номеру изделия.
        return get_group_id(self.current.stem)

    # Экспорт проверенной фотографии, полигонов и группы изделия.
    def save_next(self):
        if self.current is None: return
        if self.canvas.incomplete():
            self.statusBar().showMessage('Сначала замкните контур или отмените его клавишей Esc.',5000); return
        if not self.canvas.boxes() and QMessageBox.question(self,'Пустая разметка','Сохранить проверенное фото без объектов и пустой TXT?')!=QMessageBox.StandardButton.Yes: return
        try:
            self.persist(); imgdir,labdir=map(Path,self.state['paths'][1:])
            if (labdir/'classes.json').exists() and [c['name'] for c in json.loads((labdir/'classes.json').read_text(encoding='utf-8-sig'))] != NAMES: raise ValueError('Выберите новую папку labels: существующий список классов отличается.')
            for legacy in labdir.glob('ART_*.txt'):
                if any(len(line.split())==5 for line in legacy.read_text(encoding='utf-8-sig').splitlines() if line.strip()):
                    raise ValueError('В папке labels есть старые рамки. Сначала нажмите «Преобразовать старые TXT в полигоны», чтобы не смешивать форматы.')
            rec=self.state['records'][str(self.current)]; stem=rec.get('output')
            if not stem:
                stem=f'ART_{self.number.value()}'
                if list(imgdir.glob(stem+'.*')) or (labdir/(stem+'.txt')).exists(): raise ValueError(f'{stem} уже существует. Выберите свободный номер.')
            # Экспортируем исходное ориентированное изображение без нарисованных контуров.
            imagepath=imgdir/(stem+'.png'); labelpath=labdir/(stem+'.txt')
            reader=QImageReader(str(self.current)); reader.setAutoTransform(True); image=reader.read()
            if image.isNull(): raise ValueError(reader.errorString())
            data=self.canvas.data(); text='\n'.join(yolo_line(b,image.width(),image.height()) for b in data)
            itmp=imgdir/(stem+'.image.tmp'); ltmp=labdir/(stem+'.label.tmp')
            if not image.save(str(itmp),'PNG'): raise OSError('Не удалось сохранить изображение.')
            ltmp.write_text(text+('\n' if text else ''),encoding='utf-8')
            try:
                os.replace(itmp,imagepath); os.replace(ltmp,labelpath)
            except Exception:
                if not rec.get('output'):
                    imagepath.unlink(missing_ok=True); labelpath.unlink(missing_ok=True)
                itmp.unlink(missing_ok=True); ltmp.unlink(missing_ok=True)
                raise
            atomic_json(labdir/'annotation_format.json',{'format':'yolo_segmentation','version':3})
            atomic_json(labdir/'classes.json',self.classes)
            (labdir/'classes.txt').write_text('\n'.join(c['name'] for c in self.classes)+'\n',encoding='utf-8')
            if not rec.get('output'): self.number.setValue(self.number.value()+1)
            rec['output']=stem; rec['saved']=True; rec['reviewed']=True; rec['group_id']=self.automatic_group_id()
            atomic_json(labdir/'manifest.json',self.state['records']); self.export_groups(); self.persist()
            self.statusBar().showMessage(f'Сохранено: {stem}.png + {stem}.txt',10000)
            if self.index<len(self.files)-1: self.navigate(1)
            else: QMessageBox.information(self,'Готово','Последняя фотография сохранена.')
        except Exception as e: QMessageBox.critical(self,'Ошибка сохранения',str(e))
    # Проверка незавершённого контура перед закрытием окна.
    def closeEvent(self,event):
        if self.canvas.incomplete():
            answer=QMessageBox.question(self,'Незавершённый контур','Отменить незавершённый контур и закрыть программу? Завершённые фигуры сохранятся.',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if answer!=QMessageBox.StandardButton.Yes:event.ignore();return
            self.canvas.cancel()
        self.persist(); event.accept()


# Экспорт групп позволяет сохранить связь ракурсов после переименования снимков.
def export_groups(self):
    """Сохраняет полную таблицу, не теряя ранее экспортированные фотографии."""
    import csv
    if not self.state.get('paths'):
        return
    path = Path(self.state['paths'][2])/'groups.csv'
    groups = {}
    if path.exists():
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                if row.get('filename') and row.get('group_id'):
                    groups[row['filename']] = row['group_id']
    # Новые сведения обновляют только соответствующие имена, не всю таблицу.
    for record in self.state.get('records', {}).values():
        if record.get('saved') and record.get('output'):
            groups[record['output']+'.png'] = record.get('group_id') or record['output']
    temporary = path.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['filename', 'group_id'])
        writer.writerows(sorted(groups.items()))
    os.replace(temporary, path)
Window.export_groups=export_groups
