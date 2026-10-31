"""Команды подготовки данных, обучения, проверки и локального предсказания."""
import argparse
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from config import BASE, NAMES, IMAGE_EXTENSIONS
from data import prepare
from reporting import load_policy, extract_objects, build_report, save_json, save_overlay


def file_hash(path):
    """Связывает модель, пороги и результаты калибровки по содержимому весов."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_device(device):
    """Не допускает незаметного перехода с видеокарты на процессор."""
    import torch
    if device != 'cpu' and not torch.cuda.is_available():
        raise RuntimeError('CUDA недоступна. Проверьте PyTorch или укажите --device cpu.')


def load_model(path, custom=False):
    """Загружает только локальные веса нужной задачи и схемы классов."""
    from ultralytics import YOLO
    if not Path(path).is_file():
        raise FileNotFoundError(f'Нет локальных весов: {path}')
    model = YOLO(str(path))
    if model.task != 'segment':
        raise ValueError('Нужны веса сегментации, а не детекции.')
    if custom and [model.names[i] for i in range(len(model.names))] != NAMES:
        raise ValueError('Эти веса не обучены на согласованных 13 классах.')
    return model


def doctor(args):
    """Показывает фактическое окружение и выполняет пробное вычисление на GPU."""
    import torch
    information = {'python': sys.executable, 'python_version': sys.version,
                   'torch': torch.__version__, 'cuda_runtime': torch.version.cuda,
                   'cuda_available': torch.cuda.is_available(),
                   'ultralytics': importlib.metadata.version('ultralytics')}
    if torch.cuda.is_available():
        information['gpu'] = torch.cuda.get_device_name(0)
        information['gpu_test'] = float((torch.ones(4, device='cuda') * 2).sum().item())
    print(json.dumps(information, ensure_ascii=False, indent=2))


def train(args):
    """Запускает новый эксперимент и сохраняет конфигурацию окружения."""
    check_device(args.device)
    data = Path(args.data).resolve()
    if not data.is_file():
        raise FileNotFoundError('Сначала выполните prepare; файл data.yaml не найден.')
    # Конфигурация датасета должна принадлежать именно текущей схеме.
    import yaml
    info = yaml.safe_load(data.read_text(encoding='utf-8'))
    names = info.get('names', {})
    if isinstance(names, dict):
        names = [names.get(i) for i in range(len(names))]
    if names != NAMES:
        raise ValueError('В data.yaml указаны другие классы.')
    model = load_model(args.model)
    # Независимые маски сохраняют вложенные области продукции и дефектов.
    model.train(data=str(data), device=args.device, epochs=args.epochs,
                imgsz=args.imgsz, batch=args.batch, workers=0, seed=args.seed,
                deterministic=True, project=str(BASE/'runs'), name='segment_13_train',
                patience=args.patience, overlap_mask=False, mask_ratio=args.mask_ratio,
                max_det=1000, fliplr=0, flipud=0, hsv_h=0, hsv_s=0, hsv_v=.1,
                mosaic=0, mixup=0, copy_paste=0, scale=.1, translate=.05,
                degrees=0, shear=0, perspective=0, close_mosaic=0)
    # Результаты разных запусков не перезаписываются Ultralytics.
    import torch
    save_json({'python': sys.version, 'torch': torch.__version__,
               'ultralytics': importlib.metadata.version('ultralytics'),
               'initial_weights_sha256': file_hash(args.model),
               'data_yaml_sha256': file_hash(data),
               'arguments': vars(args)}, Path(model.trainer.save_dir)/'environment.json')


def evaluate(args):
    """Оценивает маски; итог решения по фото проверяется отдельной командой audit."""
    check_device(args.device)
    model = load_model(args.weights, custom=True)
    metrics = model.val(data=str(Path(args.data).resolve()), split=args.split,
                        device=args.device, imgsz=args.imgsz, batch=1, workers=0,
                        max_det=1000, overlap_mask=False, mask_ratio=1,
                        project=str(BASE/'runs'), name=f'segment_13_{args.split}')
    report = {name: {'evaluated': False} for name in NAMES}
    for index, class_id in enumerate(metrics.seg.ap_class_index):
        report[NAMES[int(class_id)]] = {'evaluated': True,
            'mask_precision': float(metrics.seg.p[index]), 'mask_recall': float(metrics.seg.r[index]),
            'mask_mAP50': float(metrics.seg.ap50[index]), 'mask_mAP50_95': float(metrics.seg.ap[index])}
    save_json(report, Path(metrics.save_dir)/'metrics_by_class.json')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def apply_calibration(report, calibration):
    """Применяет калибровку риска по всему снимку, не подменяя оценки изделий."""
    import numpy as np
    calibrated = []
    for item in report['image_defects']:
        curve = calibration.get('classes', {}).get(str(item['class_id']))
        if item['applicable'] and curve:
            item['probability_percent'] = round(float(np.interp(item['score'], curve['x'], curve['y']))*100, 2)
            calibrated.append(item['class_id'])
    report['probabilities_calibrated'] = bool(calibrated)
    report['calibrated_class_ids'] = calibrated
    report['calibration_scope'] = 'image' if calibrated else None


def predict(args):
    """Обрабатывает файл или папку без сетевых обращений и без интерфейса."""
    check_device(args.device)
    model = load_model(args.weights, custom=True)
    source = Path(args.source)
    paths = [source] if source.is_file() else sorted(p for p in source.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS)
    if not paths:
        raise ValueError('Нет фотографий для проверки.')
    output = Path(args.output)
    if output.exists():
        raise ValueError('Папка результата существует; укажите новую --output.')
    policy = load_policy(args.policy)
    calibration = None
    if args.calibration:
        calibration = json.loads(Path(args.calibration).read_text(encoding='utf-8'))
        if calibration['weights_sha256'] != file_hash(args.weights) or calibration['policy'] != policy or calibration['imgsz'] != args.imgsz:
            raise ValueError('Калибровка получена для других весов, порогов или размера входа.')
    # Потоковая обработка не хранит все маски фотографий в оперативной памяти.
    digest = file_hash(args.weights)
    summary = []
    for index, path in enumerate(paths, 1):
        result = model.predict(source=str(path), conf=policy['candidate_threshold'],
                               imgsz=args.imgsz, device=args.device, max_det=1000,
                               retina_masks=False, save=False, verbose=False)[0]
        report = build_report(extract_objects(result), policy)
        report.update(image=str(path.resolve()), weights_sha256=digest, policy=policy, imgsz=args.imgsz)
        if calibration:
            apply_calibration(report, calibration)
        stem = f'{index:05d}_{path.stem}'
        save_json(report, output/(stem+'.json'))
        # Визуализация сохраняет только области, прошедшие порог решения.
        if args.visualize:
            save_overlay(result.orig_img, [r for r in report['all_regions'] if 5 <= r['class_id'] <= 10], policy, output/(stem+'.jpg'))
        summary.append({'image': str(path), 'status': report['status'], 'report': stem+'.json'})
        print(f'{path.name}: {report["status"]}; изделий: {len(report["products"])}')
    save_json({'images': summary}, output/'summary.json')


def main():
    """Определяет параметры всех команд проекта."""
    parser = argparse.ArgumentParser(description='Даймонд Трейд: 13 классов, YOLO Segmentation')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    p = commands.add_parser('prepare')
    p.add_argument('--images', required=True); p.add_argument('--labels', required=True)
    p.add_argument('--groups'); p.add_argument('--allow-ungrouped', action='store_true')
    p.add_argument('--output', default=str(BASE/'dataset')); p.add_argument('--seed', type=int, default=42)
    p = commands.add_parser('train')
    p.add_argument('--model', default=str(BASE/'models/yolo26n-seg.pt'))
    p.add_argument('--data', default=str(BASE/'dataset/data.yaml'))
    p.add_argument('--epochs', type=int, default=150); p.add_argument('--patience', type=int, default=30)
    p.add_argument('--batch', type=int, default=2); p.add_argument('--imgsz', type=int, default=960)
    p.add_argument('--mask-ratio', type=int, choices=[1, 2, 4], default=1)
    p.add_argument('--device', default='0'); p.add_argument('--seed', type=int, default=42)
    p = commands.add_parser('evaluate')
    p.add_argument('--weights', required=True); p.add_argument('--data', default=str(BASE/'dataset/data.yaml'))
    p.add_argument('--split', choices=['val', 'test'], default='val')
    p.add_argument('--imgsz', type=int, default=960); p.add_argument('--device', default='0')
    p = commands.add_parser('predict')
    p.add_argument('--weights', required=True); p.add_argument('--source', required=True)
    p.add_argument('--output', required=True); p.add_argument('--policy'); p.add_argument('--calibration')
    p.add_argument('--visualize', action='store_true'); p.add_argument('--imgsz', type=int, default=960)
    p.add_argument('--device', default='0')
    # Калибровку и аудит импортируем только для соответствующих команд.
    for name in ('calibrate', 'audit'):
        p = commands.add_parser(name)
        p.add_argument('--reports', required=True); p.add_argument('--labels', required=True)
        p.add_argument('--output', required=True)
        if name == 'calibrate':
            p.add_argument('--min-samples', type=int, default=20)
    args = parser.parse_args()
    if args.command in ('calibrate', 'audit'):
        from validation import calibrate, audit
        {'calibrate': calibrate, 'audit': audit}[args.command](args)
    else:
        {'prepare': prepare, 'train': train, 'evaluate': evaluate, 'predict': predict, 'doctor': doctor}[args.command](args)


# Защита точки входа необходима для запуска обучения в Windows.
if __name__ == '__main__':
    main()
