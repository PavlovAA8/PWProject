"""Отчёт по изделиям: связь масок, применимость дефектов и итог снимка."""
import copy
import json
from pathlib import Path
import cv2
import numpy as np
from config import DEFAULT_POLICY, NAMES, PRODUCT_CLASSES, DEFECT_CLASSES, LIGHT_CLASSES, SCHEMA_VERSION


def load_policy(path=None):
    """Загружает и проверяет пороги принятия решений."""
    policy = copy.deepcopy(DEFAULT_POLICY)
    if path:
        updates = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if set(updates) - set(policy):
            raise ValueError('В файле порогов есть неизвестные настройки.')
        policy.update(updates)
    # Все классы дефектов должны иметь собственный порог предупреждения.
    if set(policy['defect_thresholds']) != {str(i) for i in DEFECT_CLASSES}:
        raise ValueError('Нужны пороги для классов 5–10.')
    values = [v for k, v in policy.items() if k != 'defect_thresholds']
    values += list(policy['defect_thresholds'].values())
    if not all(isinstance(v, (int, float)) and 0 < v <= 1 for v in values):
        raise ValueError('Пороги должны быть числами в интервале (0, 1].')
    if policy['candidate_threshold'] > min(policy['product_threshold'], policy['attribute_threshold'], *policy['defect_thresholds'].values()):
        raise ValueError('Порог извлечения кандидатов выше порога решения.')
    return policy


def fraction(part, whole):
    """Возвращает долю первой маски внутри второй."""
    area = int(part.sum())
    return float(np.logical_and(part, whole).sum() / area) if area else 0.0


def iou(first, second):
    """Вычисляет пересечение масок относительно объединения."""
    union = np.logical_or(first, second).sum()
    return float(np.logical_and(first, second).sum() / union) if union else 0.0


def extract_objects(result):
    """Приводит маски YOLO к исходному разрешению фотографии."""
    if result.boxes is None or result.masks is None:
        return []
    objects = []
    masks = result.masks.data.cpu().numpy()
    for index, box in enumerate(result.boxes):
        # Для сопоставления используем общий размер масок сети, экономя память.
        # Полигоны для отчёта остаются в пикселях исходного изображения.
        mask = masks[index] > .5
        if mask.any():
            objects.append({
                'id': index, 'class_id': int(box.cls.item()),
                'confidence': float(box.conf.item()),
                'box': box.xyxy[0].cpu().tolist(),
                'polygon': result.masks.xy[index].tolist(), 'mask': mask,
            })
    return objects


def public_object(obj):
    """Убирает тяжёлый массив маски из сохраняемого JSON."""
    return {'id': obj['id'], 'class_id': obj['class_id'], 'class_name': NAMES[obj['class_id']],
            'confidence': round(obj['confidence'], 6), 'box_xyxy': obj['box'], 'polygon_pixels': obj['polygon']}


def defect_entry(class_id, objects, policy, product_class=None):
    """Учитывает применимость дефекта и отделяет уверенность от вероятности."""
    entry = {'class_id': class_id, 'name': NAMES[class_id], 'applicable': True,
             'probability_percent': None, 'confidence_percent': None,
             'detected': False, 'regions': []}
    if class_id in LIGHT_CLASSES and product_class in (0, 2, 4):
        entry.update(applicable=False, reason='Подсветка выключена.' if product_class != 4 else 'Правило 0% для класса «Другое».')
        if product_class == 4:
            entry['probability_percent'] = 0.0
        return entry
    selected = [o for o in objects if o['class_id'] == class_id]
    score = max((o['confidence'] for o in selected), default=0.0)
    entry.update(confidence_percent=round(100 * score, 2) if selected else None,
                 detected=score >= policy['defect_thresholds'][str(class_id)],
                 regions=[public_object(o) for o in selected],
                 reason='Вероятность не откалибрована.' if selected else 'Область этого класса не найдена; это не означает вероятность 0%.')
    return entry


def build_report(objects, policy=None):
    """Создаёт отдельные отчёты изделий и общий результат фотографии."""
    policy = policy or load_policy()
    products = []
    # Конкурирующие типы одного объекта не должны давать два ответа «Да».
    for obj in sorted(objects, key=lambda o: o['confidence'], reverse=True):
        if obj['class_id'] in PRODUCT_CLASSES and obj['confidence'] >= policy['product_threshold']:
            if not any(iou(obj['mask'], p['mask']) >= policy['duplicate_iou'] for p in products):
                products.append(obj)
    supports = [o for o in objects if o['class_id'] == 12 and o['confidence'] >= policy['attribute_threshold']]
    targets = products + supports
    assignments = {o['id']: [] for o in targets}
    unassigned = []
    # Каждую область связываем с наиболее подходящим изделием или основанием.
    for obj in objects:
        if obj['class_id'] not in DEFECT_CLASSES | {11}:
            continue
        candidates = sorted([(fraction(obj['mask'], t['mask']), t['id']) for t in targets], reverse=True)
        if candidates and candidates[0][0] >= policy['association_threshold']:
            assignments[candidates[0][1]].append(obj)
        else:
            unassigned.append(obj)
    items = []
    for product in products:
        defects = [defect_entry(c, assignments[product['id']], policy, product['class_id']) for c in sorted(DEFECT_CLASSES)]
        items.append({
            'product': public_object(product),
            'types': [{'class_id': c, 'name': NAMES[c], 'present': c == product['class_id']} for c in sorted(PRODUCT_CLASSES)],
            'defects': defects,
            'glare': any(o['class_id'] == 11 and o['confidence'] >= policy['attribute_threshold'] for o in assignments[product['id']]),
            'status': 'Требуется проверка' if any(d['detected'] for d in defects) else 'Дефекты не обнаружены',
        })
    # Основание может быть общим нескольким буквам: его дефекты не теряем.
    support_reports = []
    for support in supports:
        defects = [defect_entry(c, assignments[support['id']], policy) for c in sorted(DEFECT_CLASSES)]
        support_reports.append({'support': public_object(support), 'defects': defects,
                                'needs_inspection': any(d['detected'] for d in defects)})
    orphan_defects = [o for o in unassigned if o['class_id'] in DEFECT_CLASSES and o['confidence'] >= policy['defect_thresholds'][str(o['class_id'])]]
    needs_inspection = bool(orphan_defects) or any(p['status'] == 'Требуется проверка' for p in items) or any(s['needs_inspection'] for s in support_reports)
    status = 'Требуется проверка' if needs_inspection else ('Дефекты не обнаружены' if items else 'Не удалось определить')
    # Нераспознанное изделие нельзя автоматически признать исправным.
    warnings = []
    if not products:
        warnings.append('Не найдено уверенно распознанной продукции.')
    if orphan_defects:
        warnings.append('Есть предполагаемые дефекты без надёжной привязки к изделию.')
    # Глобальные оценки позволяют отдельно калибровать риск по всему снимку.
    image_defects = []
    for c in sorted(DEFECT_CLASSES):
        applicable = c not in LIGHT_CLASSES or any(p['class_id'] in (1, 3) for p in products)
        selected = [o for o in objects if o['class_id'] == c]
        score = max((o['confidence'] for o in selected), default=0.0)
        rule_zero = c in LIGHT_CLASSES and bool(products) and all(p['class_id'] == 4 for p in products)
        image_defects.append({'class_id': c, 'name': NAMES[c], 'applicable': applicable,
                              'score': score, 'probability_percent': 0.0 if rule_zero else None})
    return {'schema_version': SCHEMA_VERSION, 'status': status,
            'probabilities_calibrated': False, 'calibration_scope': None,
            'products': items, 'supports': support_reports, 'image_defects': image_defects,
            'glare': any(o['class_id'] == 11 and o['confidence'] >= policy['attribute_threshold'] for o in objects),
            'support_present': bool(supports), 'warnings': warnings,
            'unassigned_regions': [public_object(o) for o in unassigned],
            'all_regions': [public_object(o) for o in objects]}


def save_json(value, path):
    """Сохраняет русские строки без экранирования и создаёт нужную папку."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def save_overlay(image, regions, policy, path):
    """Рисует полигоны с номерами классов без скачивания шрифтов."""
    canvas = image.copy()
    overlay = image.copy()
    selected = []
    for region in regions:
        c = region['class_id']
        threshold = policy['product_threshold'] if c < 5 else policy['defect_thresholds'][str(c)] if c in DEFECT_CLASSES else policy['attribute_threshold']
        if region['confidence'] < threshold or len(region['polygon_pixels']) < 3:
            continue
        points = np.round(region['polygon_pixels']).astype(np.int32).reshape(-1, 1, 2)
        color = ((c*71+50) % 255, (c*109+100) % 255, (c*41+150) % 255)
        cv2.fillPoly(overlay, [points], color)
        selected.append((region, points, color))
    canvas = cv2.addWeighted(overlay, .25, canvas, .75, 0)
    # Номер класса соответствует русскому справочнику в JSON и README.
    for region, points, color in selected:
        cv2.polylines(canvas, [points], True, color, 2)
        x, y = points[0, 0]
        cv2.putText(canvas, f"{region['class_id']} {region['confidence']:.2f}",
                    (max(0, int(x)), max(15, int(y))), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1, cv2.LINE_AA)
    success, encoded = cv2.imencode('.jpg', canvas)
    if not success:
        raise RuntimeError('Не удалось сохранить визуализацию.')
    encoded.tofile(str(path))
