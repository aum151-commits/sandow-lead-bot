"""Bounded, untrusted attribution for private lead records only."""
import json
import re
from urllib.parse import urlsplit, unquote

FIELDS = ('attr_version lead_id client_id first_source first_medium first_campaign first_term first_landing first_at last_source last_medium last_campaign last_term last_landing last_at source_status').split()

def is_test(data, phone=''):
    flag = str(data.get('test', '')).strip().lower()
    digits = re.sub(r'\D', '', str(phone))[-10:]
    return flag not in ('', '0', 'false', 'no', 'off', 'none') or digits.startswith(('900000', '000'))

def sanitize_attribution(data):
    forbidden = [str(data.get(k, '')).strip().lower() for k in ('name', 'email', 'phone')]
    def safe(value, limit=80, term=False, status=False):
        if not isinstance(value, (str, int, float)):
            return 'unknown'
        text = str(value)[:512].strip()
        if any(x and len(x) > 2 and x in text.lower() for x in forbidden) or '@' in text or re.search(r'\d[\d ()+.-]{8,}\d', text):
            return 'unknown'
        pattern = r'[a-zA-Z0-9_;=.\-]*' if status else (r'[a-zA-Zа-яА-ЯёЁ0-9 _.,+\-]*' if term else r'[a-zA-Zа-яА-ЯёЁ0-9 _.:/T+\-]*')
        return text[:limit] if re.fullmatch(pattern, text) and text else 'unknown'
    def landing(value):
        if not isinstance(value, str): return 'unknown'
        try:
            u = urlsplit(value[:1024] if '://' in value else 'https://' + value[:1024])
            if u.scheme not in ('http', 'https') or u.username or u.password or not u.hostname: return 'unknown'
            return safe(u.hostname + unquote(u.path), 160)
        except ValueError: return 'unknown'
    result = {}
    for key in FIELDS:
        value = data.get('sndw_' + key, '')
        if key.endswith('_landing'): value = landing(value)
        elif key.endswith('_at'):
            value = str(value)[:32]
            value = value if re.fullmatch(r'\d{4}-\d\d-\d\dT[\d:.+Z-]{5,20}', value) else 'unknown'
        elif key == 'client_id':
            value = str(value)[:64]
            value = value if re.fullmatch(r'(?:\d{5,30}|\d{1,20}\.\d{1,20})', value) else 'unknown'
        else: value = safe(value, term=key.endswith('_term'), status=key == 'source_status')
        result[key] = value
    path = data.get('sndw_path', '[]')
    try: path = json.loads(path[:4096]) if isinstance(path, str) else path
    except (ValueError, TypeError): path = []
    result['path'] = []
    for item in (path[:8] if isinstance(path, list) else []):
        if not isinstance(item, dict): continue
        url = landing(item.get('url', item.get('path', item.get('page', ''))))
        at = str(item.get('at', item.get('t', '')))[:32]
        if url != 'unknown': result['path'].append({'url': url, 'at': at if re.fullmatch(r'\d{4}-\d\d-\d\dT[\d:.+Z-]{5,20}', at) else 'unknown'})
    return result

def attribution_text(a):
    def show(value): return 'неизвестно' if value == 'unknown' else value
    lines = ['Источник заявки (со слов браузера):']
    for prefix, label in [('first', 'Первый переход'), ('last', 'Последний переход')]:
        lines.append(f"{label}: {show(a[prefix+'_source'])} / {show(a[prefix+'_medium'])}; кампания: {show(a[prefix+'_campaign'])}; страница: {show(a[prefix+'_landing'])}; время: {show(a[prefix+'_at'])}")
        if a.get(prefix + '_term', 'unknown') != 'unknown':
            lines.append('Фраза/метка запроса: ' + a[prefix + '_term'])
    lines.append('Путь: ' + (' → '.join(x['url'] + ' (' + show(x['at']) + ')' for x in a['path']) or 'неизвестно'))
    lines.append(f"Код заявки: {show(a['lead_id'])}; код браузера: {show(a['client_id'])}; версия: {show(a['attr_version'])}; определение источника: {show(a['source_status'])}")
    return '\n'.join(lines)
