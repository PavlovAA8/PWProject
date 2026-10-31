"""Проверка и копирование полигональных данных без изменения исходников."""
import collections
import csv
import hashlib
import json
import math
import random
import re
import shutil
from pathlib import Path
from PIL import Image
from config import BASE, NAMES, IMAGE_EXTENSIONS


def read_groups(path):
    """Загружает связь имени файла и физической группы изделий."""
    if not path:
        return {}
    result = {}
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'filename', 'group_id'}.issubset(reader.fieldnames or []):
            raise ValueError('В CSV нужны столбцы filename,group_id.')
        for row in reader:
            name, group = row['filename'].strip(), row['group_id'].strip()
            if not name or not group or name in result:
                raise ValueError('Пустая или повторная запись в CSV групп.')
            result[name] = group
    return result


def read_polygons(path):
    """Читает и проверяет полигоны YOLO Segmentation."""

    polygons = []

    # Каждая строка содержит класс и нормализованные координаты вершин.
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line.strip():
            continue

        try:
            fields = line.split()
            class_id = int(fields[0])
            coordinates = list(map(float, fields[1:]))
        except (ValueError, IndexError) as error:
            raise ValueError(
                f"{path.name}, строка {line_number}: ошибка чтения."
            ) from error

        # Для полигона необходимы минимум три вершины.
        if class_id not in range(len(NAMES)):
            raise ValueError(
                f"{path.name}, строка {line_number}: неизвестный класс."
            )

        if len(coordinates) < 6 or len(coordinates) % 2 != 0:
            raise ValueError(
                f"{path.name}, строка {line_number}: "
                "ожидается полигон, а не рамка."
            )

        # Координаты YOLO должны быть конечными числами от 0 до 1.
        if not all(
            math.isfinite(value) and 0 <= value <= 1
            for value in coordinates
        ):
            raise ValueError(
                f"{path.name}, строка {line_number}: "
                "недопустимые координаты."
            )

        points = list(zip(coordinates[::2], coordinates[1::2]))

        # Проверяем, что полигон имеет ненулевую площадь.
        doubled_area = abs(
            sum(
                points[i][0] * points[(i + 1) % len(points)][1]
                - points[(i + 1) % len(points)][0] * points[i][1]
                for i in range(len(points))
            )
        )

        if len(set(points)) < 3 or doubled_area <= 1e-12:
            raise ValueError(
                f"{path.name}, строка {line_number}: вырожденный полигон."
            )

        polygons.append((class_id, coordinates))

    return polygons


def get_group_id(stem):
    """Объединяет фотографии до и после по номеру изделия."""

    # Например, ART_before_1 и ART_after_1 получают группу ART_1.
    return re.sub(r"_(before|after)_", "_", stem, flags=re.IGNORECASE)


def prepare(args):
    """Создаёт отдельный датасет, сохраняя исходные полигоны."""

    images_dir = Path(args.images)
    labels_dir = Path(args.labels)
    output_dir = Path(args.output).resolve()
    # Служебную таблицу ищем сами; неполный старый CSV не блокирует подготовку.
    group_path = Path(args.groups) if getattr(args, 'groups', None) else labels_dir/'groups.csv'
    explicit_groups = read_groups(group_path) if group_path.is_file() else {}
    inferred_images = []
    # Восстанавливаем известные связи из журнала экспорта, если CSV их не содержит.
    manifest_path = labels_dir/'manifest.json'
    if manifest_path.is_file():
        previous_records = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        for record in previous_records.values():
            if record.get('output') and record.get('group_id'):
                explicit_groups.setdefault(record['output']+'.png', record['group_id'])

    # Не перезаписываем уже подготовленный датасет и его разбиение.
    if output_dir.exists():
        raise ValueError(
            "Папка dataset уже существует. "
            "Перед новой подготовкой переименуйте её."
        )

    if not images_dir.is_dir() or not labels_dir.is_dir():
        raise ValueError("Проверьте пути к изображениям и разметке.")

    # Проверяем соответствие номеров классов текущему проекту.
    classes = json.loads(
        (labels_dir / "classes.json").read_text(encoding="utf-8-sig")
    )

    if [item["name"] for item in classes] != NAMES:
        raise ValueError(
            "Неверные названия или порядок 13 классов. Сверьте classes.json с config.py."
        )

    records = []
    seen_stems = set()

    # Проверяем изображения и аннотации до создания копии данных.
    for image_path in sorted(images_dir.iterdir()):
        if image_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        if image_path.stem in seen_stems:
            raise ValueError(
                f"Повторяющееся имя изображения: {image_path.stem}"
            )

        seen_stems.add(image_path.stem)
        label_path = labels_dir / f"{image_path.stem}.txt"

        if not label_path.exists():
            raise ValueError(f"Нет аннотации: {label_path}")

        polygons = read_polygons(label_path)

        # Хеш пикселей помогает обнаружить одинаковые изображения.
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            digest = hashlib.sha256(
                str(image.size).encode() + image.tobytes()
            ).hexdigest()

        if image_path.name not in explicit_groups:
            inferred_images.append(image_path.name)
        records.append(
            {
                "image": image_path,
                "label": label_path,
                "polygons": polygons,
                "group": explicit_groups.get(image_path.name, get_group_id(image_path.stem)),
                "hash": digest,
            }
        )

    # Неизвестные связи не выдумываем: такие снимки получают отдельную группу.
    # Совпадающие пиксели и стандартные имена до/после по-прежнему связываются.
    # Связываем группы точных дубликатов, чтобы избежать утечки данных.
    parents = {record["group"]: record["group"] for record in records}

    def find_group(group):
        while parents[group] != group:
            parents[group] = parents[parents[group]]
            group = parents[group]
        return group

    hashes = {}

    for record in records:
        previous = hashes.get(record["hash"])

        if previous is not None:
            if sorted(record["polygons"]) != sorted(previous["polygons"]):
                raise ValueError(
                    "Одинаковые изображения имеют разную разметку: "
                    f"{previous['image'].name} и {record['image'].name}"
                )

            parents[find_group(record["group"])] = find_group(
                previous["group"]
            )

        hashes[record["hash"]] = record

    # Разделяем целые группы, сохраняя пары до/после вместе.
    groups = sorted(
        {find_group(record["group"]) for record in records}
    )

    if len(groups) < 5:
        raise ValueError("Нужно минимум пять независимых групп.")

    random.Random(args.seed).shuffle(groups)

    val_count = max(1, round(len(groups) * 0.15))
    test_count = max(1, round(len(groups) * 0.15))

    split_by_group = {}

    for index, group in enumerate(groups):
        if index < val_count:
            split = "val"
        elif index < val_count + test_count:
            split = "test"
        else:
            split = "train"

        split_by_group[group] = split

    manifest = {
        "task": "segment",
        "seed": args.seed,
        "grouping_verified_by_user": bool(getattr(args, "groups", None)) and not inferred_images,
        "automatically_assigned_images": inferred_images,
        "names": NAMES,
        "files": [],
        "splits": {},
    }

    # Копируем исходные файлы без преобразования полигонов в рамки.
    for split in ("train", "val", "test"):
        (output_dir / "images" / split).mkdir(parents=True)
        (output_dir / "labels" / split).mkdir(parents=True)

    for record in records:
        group = find_group(record["group"])
        split = split_by_group[group]

        shutil.copy2(
            record["image"],
            output_dir / "images" / split / record["image"].name,
        )

        shutil.copy2(
            record["label"],
            output_dir / "labels" / split / record["label"].name,
        )

        manifest["files"].append(
            {
                "image": record["image"].name,
                "group": group,
                "split": split,
                "sha256": record["hash"],
            }
        )

    # Сохраняем статистику отдельно по каждой части датасета.
    for split in ("train", "val", "test"):
        selected = [
            record
            for record in records
            if split_by_group[find_group(record["group"])] == split
        ]

        counts = collections.Counter(
            class_id
            for record in selected
            for class_id, _ in record["polygons"]
        )

        manifest["splits"][split] = {
            "images": len(selected),
            "groups": len(
                {find_group(record["group"]) for record in selected}
            ),
            "objects": {
                NAMES[class_id]: counts[class_id]
                for class_id in range(len(NAMES))
            },
        }

    # Создаём конфигурацию датасета для Ultralytics.
    yaml_text = (
        f"path: {json.dumps(output_dir.as_posix(), ensure_ascii=False)}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n"
        + "".join(
            f"  {class_id}: {name}\n"
            for class_id, name in enumerate(NAMES)
        )
    )

    (output_dir / "data.yaml").write_text(
        yaml_text, encoding="utf-8"
    )

    (output_dir / "split_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Полная служебная таблица сохраняется в копии датасета, исходный CSV не меняется.
    with (output_dir/'groups_resolved.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['filename', 'group_id'])
        writer.writerows((r['image'].name, find_group(r['group'])) for r in records)
    print(json.dumps(manifest["splits"], ensure_ascii=False, indent=2))
    print("Полигоны сохранены без изменений.")
    if inferred_images:
        print(f'Автоматически назначены служебные группы для {len(inferred_images)} фото. Связи разных ракурсов с произвольными именами не подтверждены; итоговое качество проверяйте на новых изделиях.')

    # Предупреждаем о классах, отсутствующих в обучающей выборке.
    for name, count in manifest["splits"]["train"]["objects"].items():
        if count == 0:
            print(f"Внимание: в обучении нет примеров класса «{name}».")
