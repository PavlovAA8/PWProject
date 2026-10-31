"""Единые настройки классов, обучения и формирования отчёта."""
from pathlib import Path

# Папка проекта определяется автоматически, без привязки к компьютеру.
BASE = Path(__file__).resolve().parent
NAMES = [
    'Объемная вывеска — выключена',
    'Объемная вывеска — включена',
    'Неоновая вывеска — выключена',
    'Неоновая вывеска — включена',
    'Другое',
    'Неравномерность свечения',
    'Негорящие участки',
    'Повреждения поверхности',
    'Деформации',
    'Дефекты сборки',
    'Загрязнения',
    'Блик',
    'Подложка / каркас',
]

# Вспомогательные группы не создают дополнительных классов модели.
PRODUCT_CLASSES = frozenset(range(5))
DEFECT_CLASSES = frozenset(range(5, 11))
LIGHT_CLASSES = frozenset({5, 6})
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}
SCHEMA_VERSION = 'diamond-13-v1'

# Начальные пороги необходимо подобрать на валидации, они не гарантируют качество.
DEFAULT_POLICY = {
    'candidate_threshold': 0.01,
    'product_threshold': 0.35,
    'attribute_threshold': 0.25,
    'defect_thresholds': {str(i): 0.15 for i in DEFECT_CLASSES},
    'association_threshold': 0.5,
    'duplicate_iou': 0.75,
}
