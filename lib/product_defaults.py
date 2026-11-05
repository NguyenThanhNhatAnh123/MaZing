"""Product choices for a campaign and stable base-product matching after cloning."""
from __future__ import annotations

from lib.mayzing import MayzingError


def options(group: dict, names: dict[str, str], observed_names: dict[str, str] | None = None) -> list[dict]:
    products = {p['id']: p for p in group.get('products') or []}
    result = []
    for index, pid in enumerate(group.get('productIds') or []):
        product = products.get(pid) or {}
        base = str(product.get('baseProductId') or '')
        label = names.get(base) or (observed_names or {}).get(base) or (observed_names or {}).get(pid) or f'Sản phẩm {index + 1}'
        result.append({'id': pid, 'base_product_id': base, 'label': label,
                       'current': pid == group.get('heroProductId')})
    return result


def resolve(group: dict, base_product_id: str) -> str:
    members = set(group.get('productIds') or [])
    matches = [p['id'] for p in group.get('products') or []
               if p.get('id') in members and p.get('baseProductId') == base_product_id]
    if len(matches) != 1:
        raise MayzingError('Không tìm thấy đúng một sản phẩm thuộc loại mặc định đã chọn; kiểm tra campaign, tool không tự chọn mẫu khác.')
    return matches[0]
