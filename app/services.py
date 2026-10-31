"""Подключение камеры и безопасное сохранение серий в отдельных сеансах."""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
import cv2
from PySide6.QtCore import QThread, Signal


def atomic_json(path, value):
    """Сохраняет состояние целиком, заменяя файл только после успешной записи."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def identifier(value):
    """Номер не должен выходить за пределы папки проверок или нарушать имена Windows."""
    value = value.strip()
    if not value or len(value) > 60 or not re.fullmatch(r'[\w-]+', value, flags=re.UNICODE):
        raise ValueError('Номер: только буквы, цифры, дефис и подчёркивание; до 60 символов.')
    if value.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[1-9]', value.upper()):
        raise ValueError('Этот номер зарезервирован Windows.')
    return value


def new_session(root, order, product):
    """Создаёт новую серию, не перезаписывая повторную проверку изделия."""
    order, product = identifier(order), identifier(product)
    parent = Path(root).resolve()/order/('изделие_'+product)
    parent.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        folder = parent/f'серия_{number:04d}'
        try:
            folder.mkdir()
            break
        except FileExistsError:
            number += 1
    (folder/'originals').mkdir()
    session = {'order': order, 'product': product, 'series': number,
               'created_at': datetime.now().astimezone().isoformat(),
               'capture_status': 'created', 'analysis_status': 'not_started',
               'expected_shots': 15, 'shots': [], 'error': None}
    atomic_json(folder/'session.json', session)
    return folder, session


class CameraService:
    """Читает поток в фоне; в памяти хранится только последний кадр."""
    def __init__(self, settings):
        self.settings = dict(settings)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.frame = None
        self.sequence = 0
        self.received_at = 0.0
        self.status = 'Подключение к камере…'
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def snapshot(self):
        # Копирование защищает сохранение кадра от следующего обновления потока.
        with self.lock:
            return (None if self.frame is None else self.frame.copy(), self.sequence, self.received_at)

    def stop(self):
        self.stop_event.set()

    def _read(self):
        s = self.settings
        host = s['ip']
        url = f"rtsp://{quote(s['username'], safe='')}:{quote(s['password'], safe='')}@{host}:{s['port']}/Streaming/Channels/{s['stream']}"
        while not self.stop_event.is_set():
            cap = cv2.VideoCapture()
            try:
                opened = cap.open(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])
                if opened:
                    self.status = 'Камера подключена'
                    while not self.stop_event.is_set():
                        ok, frame = cap.read()
                        if not ok:
                            break
                        with self.lock:
                            self.frame = frame
                            self.sequence += 1
                            self.received_at = time.monotonic()
            except Exception:
                # Адрес потока с паролем не попадает в сообщения приложения.
                pass
            finally:
                cap.release()
                with self.lock:
                    self.frame = None
            self.status = 'Нет связи. Переподключение…'
            self.stop_event.wait(2)


class CaptureWorker(QThread):
    """Сохраняет 15 новых кадров с интервалом в секунду вне потока интерфейса."""
    progress = Signal(int)
    completed = Signal(str)

    def __init__(self, camera, folder, session, parent=None):
        super().__init__(parent)
        self.camera, self.folder, self.session = camera, Path(folder), session
        self.cancel_event = threading.Event()

    def run(self):
        s = self.session
        s['capture_status'] = 'capturing'
        started = time.monotonic()
        previous_sequence = -1
        try:
            for index in range(15):
                # Время отсчитывается от начала серии, а не от завершения записи PNG.
                if self.cancel_event.wait(max(0, started+index-time.monotonic())):
                    raise RuntimeError('Съёмка прервана пользователем.')
                frame, sequence, received = self.camera.snapshot()
                if frame is None or sequence == previous_sequence or time.monotonic()-received > 1.5:
                    raise RuntimeError('Нет нового свежего кадра. Серия не завершена.')
                previous_sequence = sequence
                filename = f"{s['order']}_{s['product']}_{index+1:02d}.png"
                ok, buffer = cv2.imencode('.png', frame)
                if not ok:
                    raise RuntimeError('Не удалось закодировать снимок.')
                path = self.folder/'originals'/filename
                buffer.tofile(str(path))
                s['shots'].append({'index': index+1, 'filename': filename, 'captured_at': datetime.now().astimezone().isoformat(),
                                   'width': frame.shape[1], 'height': frame.shape[0], 'sequence': sequence})
                atomic_json(self.folder/'session.json', s)
                self.progress.emit(index+1)
            s['capture_status'] = 'complete'
        except Exception as error:
            s['capture_status'] = 'incomplete'
            s['error'] = str(error)
        finally:
            atomic_json(self.folder/'session.json', s)
            self.completed.emit(str(self.folder))
