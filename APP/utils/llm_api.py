"""Project-local API registry and read-only model discovery."""
from __future__ import annotations

import copy
import json
import socket
import ssl
import time
import uuid
import threading
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl
from urllib.request import Request, HTTPRedirectHandler, build_opener

from utils.config_map import pdf2zh_config_map, pdf2zh_next_config_map, pdf2zh_next_service_aliases
from utils.desktop_translation import SERVICES

ENGINES = ('pdf2zh_next', 'pdf2zh')
FREE_SERVICES = {'bing', 'google', 'siliconflowfree', 'argos'}
LOCAL_SERVICES = {'ollama', 'xinference', 'claudecode'}
EMPTY = {'', 'null', 'none', 'your_api_key', 'your-api-key', 'your-api-key-here'}
_REGISTRY_LOCK = threading.RLock()


def clean_value(value):
    value = str(value or '').strip()
    return '' if value.lower() in EMPTY else value


def canonical_service(service):
    service = str(service or '').strip()
    reverse = {value: key for key, value in pdf2zh_next_service_aliases.items()}
    return 'ModelScope' if service.lower() == 'modelscope' else reverse.get(service, service)


def load_catalog():
    import app_paths
    catalog = json.loads(app_paths.resource_path('llm_catalog.json').read_text(encoding='utf-8'))
    templates = {canonical_service(key): value for key, value in catalog['providers'].items()}
    for service in SERVICES:
        template = templates.setdefault(service, {'name': service, 'models': [], 'urls': []})
        template['engines'] = supported_engines(service)
    # Addresses, not model recommendations. Saved entries are never rewritten.
    defaults = {'gemini': 'https://generativelanguage.googleapis.com/v1beta',
                'groq': 'https://api.groq.com/openai/v1', 'grok': 'https://api.x.ai/v1',
                'qwen-mt': 'https://dashscope.aliyuncs.com/compatible-mode/v1'}
    for service, url in defaults.items():
        if not templates[service].get('urls'):
            templates[service]['urls'] = [url]
    catalog['providers'] = templates
    return catalog


def supported_engines(service):
    service = canonical_service(service)
    engines = []
    if service in pdf2zh_config_map or service in FREE_SERVICES - {'siliconflowfree'}:
        engines.append('pdf2zh')
    mapped = pdf2zh_next_service_aliases.get(service, service)
    if mapped in pdf2zh_next_config_map or service in {'bing', 'google'}:
        engines.append('pdf2zh_next')
    return engines


def field_map(service, engine):
    if engine == 'pdf2zh':
        return pdf2zh_config_map.get(service, {})
    return pdf2zh_next_config_map.get(pdf2zh_next_service_aliases.get(service, service), {})


def requires_model(service, engine):
    return 'model' in field_map(service, engine) and service != 'tencent'


def configured(entry, engine):
    if engine not in entry.get('engines', supported_engines(entry.get('service'))):
        return False
    service = entry.get('service')
    if service not in SERVICES:
        return False
    fields = field_map(service, engine)
    if 'apiKey' in fields and not clean_value(entry.get('apiKey')):
        return False
    if 'model' in fields and not clean_value(entry.get('model')):
        return False
    if service in {'openailiked', 'ollama', 'xinference', 'azure-openai', 'azure', 'dify', 'anythingllm', 'deeplx'} and not clean_value(entry.get('apiUrl')):
        return False
    if 'apiUrl' in fields and service != 'claudecode' and clean_value(entry.get('apiUrl')):
        try:
            address = urlsplit(entry['apiUrl'])
            if address.scheme not in {'http', 'https'} or not address.netloc or address.username or address.password:
                return False
        except ValueError:
            return False
    return True


def normalize_entry(raw):
    if not isinstance(raw, dict):
        raise ValueError('API 配置应为 JSON 对象')
    service = canonical_service(raw.get('service'))
    if service not in SERVICES or not supported_engines(service):
        raise ValueError('不支持该供应商类型')
    extra = raw.get('extraData', {})
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise ValueError('API 附加参数应为 JSON 对象')
    engines = raw.get('engines', supported_engines(service))
    if not isinstance(engines, list):
        raise ValueError('API 适用内核应为列表')
    key = str(raw.get('key') or uuid.uuid4())
    if len(key) > 160:
        raise ValueError('API 标识过长')
    active = raw.get('activate', True)
    active = active is True or str(active).lower() in {'true', '1'}
    models = raw.get('models') or []
    if not isinstance(models, list):
        raise ValueError('模型列表应为数组')
    return {'key': key, 'name': clean_value(raw.get('name')) or service, 'service': service,
            'apiKey': clean_value(raw.get('apiKey')), 'apiUrl': clean_value(raw.get('apiUrl')),
            'model': clean_value(raw.get('model')), 'extraData': copy.deepcopy(extra), 'activate': active,
            'engines': [engine for engine in ENGINES if engine in engines and engine in supported_engines(service)],
            'models': list(dict.fromkeys(str(model).strip() for model in models if isinstance(model, str) and model.strip()))[:5000]}


class ApiRegistry:
    def __init__(self, path, catalog=None):
        self.path = Path(path)
        self.catalog = catalog or load_catalog()
        self.entries = []
        self.selected = {}

    def load_or_migrate(self, settings, config_profiles):
        with _REGISTRY_LOCK:
            return self._load_or_migrate(settings, config_profiles)

    def _load_or_migrate(self, settings, config_profiles):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), list):
                raise ValueError('LLM API 配置文件格式无效，原文件未修改')
            self.entries = [normalize_entry(entry) for entry in data['entries']]
            if len({entry['key'] for entry in self.entries}) != len(self.entries):
                raise ValueError('LLM API 配置包含重复标识，原文件未修改')
            selected = data.get('selected', {})
            if not isinstance(selected, dict):
                raise ValueError('LLM API 选择信息格式无效，原文件未修改')
            self.selected = {engine: str(value) for engine, value in selected.items() if engine in ENGINES}
            return self
        from utils.desktop_translation import load_options
        options = load_options(settings)
        profiles = copy.deepcopy(config_profiles)
        stored = settings.get('translation_apis', {})
        if isinstance(stored, dict):
            for identity, saved in stored.items():
                if not isinstance(saved, dict):
                    continue
                merged = copy.deepcopy(profiles.get(identity, {}))
                for field in ('apiKey', 'apiUrl', 'model'):
                    if clean_value(saved.get(field)):
                        merged[field] = saved[field]
                extra = saved.get('extraData')
                if isinstance(extra, dict):
                    merged['extraData'] = {**merged.get('extraData', {}), **extra}
                profiles[identity] = merged
        for identity, profile in profiles.items():
            if not isinstance(profile, dict) or ':' not in identity:
                continue
            engine, service = identity.split(':', 1)
            service = canonical_service(service)
            if engine not in supported_engines(service):
                continue
            meaningful = bool(clean_value(profile.get('apiKey'))) or (service in LOCAL_SERVICES and bool(clean_value(profile.get('model'))))
            chosen = canonical_service(options['service' if engine == 'pdf2zh' else 'next_service']) == service
            if not meaningful and not (chosen and service in FREE_SERVICES):
                continue
            entry = normalize_entry({**profile, 'service': service, 'engines': [engine],
                                     'name': self.catalog['providers'][service].get('name', service) + (' · 旧版' if engine == 'pdf2zh' else ''), 'activate': True})
            self.entries.append(entry)
            if chosen:
                self.selected[engine] = entry['key']
        # A fresh standalone installation retains only its selected free default.
        for engine in ENGINES:
            service = canonical_service(options['service' if engine == 'pdf2zh' else 'next_service'])
            if engine not in self.selected and service in FREE_SERVICES and engine in supported_engines(service):
                entry = normalize_entry({'service': service, 'name': self.catalog['providers'][service].get('name', service), 'engines': [engine]})
                self.entries.append(entry); self.selected[engine] = entry['key']
        self.save()
        return self

    def save(self):
        with _REGISTRY_LOCK:
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'entries': self.entries, 'selected': self.selected}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def _commit(self, entries, selected):
        previous = self.entries, self.selected
        self.entries, self.selected = entries, selected
        try:
            self.save()
        except Exception:
            self.entries, self.selected = previous
            raise

    def get(self, key):
        return next((entry for entry in self.entries if entry['key'] == key), None)

    def choices(self, engine):
        return [entry for entry in self.entries if entry['activate'] and configured(entry, engine)]

    def upsert(self, raw, *, require_complete=True):
        entry = normalize_entry(raw)
        if require_complete and not any(configured(entry, engine) for engine in entry['engines']):
            raise ValueError('请补全 API Key、接口地址和模型等必填项后保存')
        entries = copy.deepcopy(self.entries)
        previous = self.get(entry['key'])
        if previous:
            entries[self.entries.index(previous)] = entry
        else:
            entries.append(entry)
        self._commit(entries, copy.deepcopy(self.selected))
        return entry

    def delete(self, key):
        self._commit([entry for entry in self.entries if entry['key'] != key],
                     {engine: value for engine, value in self.selected.items() if value != key})

    def select(self, engine, key):
        if key and key not in {entry['key'] for entry in self.choices(engine)}:
            raise ValueError('只能选择已配置且已启用的供应商')
        self._commit(copy.deepcopy(self.entries), {**self.selected, engine: key})

    def selected_entry(self, engine):
        entry = self.get(self.selected.get(engine))
        return entry if entry and entry in self.choices(engine) else None

    def import_data(self, data):
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, dict):
            data = data.get('entries', data.get('llmApis', data.get('apis', [data])))
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, list):
            raise ValueError('文件应包含 API 配置数组')
        entries = [normalize_entry(item) for item in data]
        if not entries:
            raise ValueError('没有可导入的 API 配置')
        if len({entry['key'] for entry in entries}) != len(entries):
            raise ValueError('导入文件含重复 API 标识')
        merged = copy.deepcopy(self.entries)
        for entry in entries:
            previous = next((item for item in merged if item['key'] == entry['key']), None)
            if previous:
                merged[merged.index(previous)] = entry
            else:
                merged.append(entry)
        self._commit(merged, copy.deepcopy(self.selected))
        return len(entries)

    def model_choices(self, entry):
        template = self.catalog['providers'].get(entry['service'], {})
        return list(dict.fromkeys(value for value in [entry.get('model'), *entry.get('models', []), *template.get('models', [])] if value))


class ModelDiscoveryError(ValueError):
    pass


def model_request_spec(entry, catalog=None):
    catalog = catalog or load_catalog()
    service = canonical_service(entry.get('service'))
    if service in FREE_SERVICES or service in {'deepl', 'deeplx', 'tencent', 'azure', 'dify', 'anythingllm', 'claudecode'}:
        raise ModelDiscoveryError('此服务没有通用模型列表接口，可在 API 配置中保存所需模型')
    template = catalog['providers'].get(service, {})
    base = clean_value(entry.get('apiUrl')) or next(iter(template.get('urls', [])), '')
    key = clean_value(entry.get('apiKey'))
    if service not in {'ollama', 'xinference', 'openailiked'} and not key:
        raise ModelDiscoveryError('获取模型列表需要 API Key')
    parts = urlsplit(base)
    if parts.scheme not in {'http', 'https'} or not parts.netloc or parts.username or parts.password:
        raise ModelDiscoveryError('请输入有效的 HTTP 或 HTTPS 接口地址')
    path = parts.path.rstrip('/')
    headers = {'Accept': 'application/json'}
    query = list(parse_qsl(parts.query, keep_blank_values=True))
    if service == 'ollama':
        path = path.removesuffix('/api/tags').removesuffix('/api') + '/api/tags'
    elif service == 'gemini':
        path = path.removesuffix('/models') + '/models'
        headers['x-goog-api-key'] = key
        query = [(name, value) for name, value in query if name not in {'key', 'pageToken', 'pageSize'}]
        query.append(('pageSize', '1000'))
    elif service == 'azure-openai':
        headers['api-key'] = key
        if path.endswith(('/openai/v1', '/openai/v1/models')):
            path = path.removesuffix('/models') + '/models'
        else:
            path = path.removesuffix('/openai/models').removesuffix('/openai') + '/openai/models'
            if not any(name == 'api-version' for name, _ in query):
                query.append(('api-version', str(entry.get('extraData', {}).get('azure_openai_api_version') or '2024-10-21')))
    else:
        for suffix in ('/chat/completions', '/completions', '/models'):
            if path.endswith(suffix):
                path = path[:-len(suffix)]; break
        if service == 'xinference' and not path.endswith('/v1'):
            path += '/v1'
        path += '/models'
        if key:
            headers['Authorization'] = 'Bearer ' + key
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), '')), headers


def extract_model_ids(payload):
    candidates = payload
    if isinstance(payload, dict):
        fallback = list(payload.values()) if payload and all(isinstance(value, dict) for value in payload.values()) else []
        candidates = payload.get('data', payload.get('models', payload.get('result', fallback)))
        if isinstance(candidates, dict):
            candidates = candidates.get('data', list(candidates.values()))
    if not isinstance(candidates, list):
        return []
    result = []
    for item in candidates:
        value = item if isinstance(item, str) else next((item.get(name) for name in ('id', 'name', 'model', 'model_name', 'model_uid') if item.get(name)), '') if isinstance(item, dict) else ''
        if isinstance(value, str) and value.strip():
            result.append(value.strip().removeprefix('models/'))
    return list(dict.fromkeys(result))[:5000]


class SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            fp.close()
            raise ModelDiscoveryError('接口跳转到其他主机，请填写最终 API 地址后重试')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def connection_failure(exc):
    """Keep actionable transport details without echoing URLs, keys or proxy passwords."""
    reason = exc.reason if isinstance(exc, URLError) else exc
    code = getattr(reason, 'winerror', None) or getattr(reason, 'errno', None)
    suffix = f'，错误码 {code}' if isinstance(code, int) else ''
    if isinstance(reason, ssl.SSLCertVerificationError):
        verify_code = getattr(reason, 'verify_code', None)
        detail = f'，验证码 {verify_code}' if isinstance(verify_code, int) else ''
        return f'TLS 证书验证失败{detail}；请检查系统时间、证书链或代理证书'
    if isinstance(reason, ssl.SSLError):
        return f'TLS 握手失败{suffix}；请检查 HTTPS 地址和代理协议'
    if isinstance(reason, socket.gaierror):
        return f'DNS 解析失败{suffix}；请检查域名和 DNS/代理设置'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return '连接超时；请检查网络与代理是否可用'
    if isinstance(reason, ConnectionRefusedError):
        return f'连接被拒绝{suffix}；请检查接口或代理端口'
    if isinstance(reason, ConnectionResetError):
        return f'连接被重置{suffix}；请检查接口与代理连接'
    if isinstance(reason, PermissionError):
        return f'系统拒绝此程序访问网络{suffix}；请检查应用的网络访问权限'
    if isinstance(reason, str) and reason.lower().startswith('unknown url type: https'):
        return '此 EXE 的 HTTPS 支持加载失败；请检查发行包中的 SSL 组件是否完整'
    return f'{type(reason).__name__}{suffix}；请检查地址、代理和网络连接'


def discover_models(entry, catalog=None, timeout=20):
    url, headers = model_request_spec(entry, catalog)
    opener = build_opener(SameOriginRedirects())
    deadline = time.monotonic() + timeout
    models, tokens = [], set()
    for _ in range(20):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelDiscoveryError('模型探测超时，请检查接口地址和网络连接')
        try:
            with opener.open(Request(url, headers=headers, method='GET'), timeout=remaining) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ModelDiscoveryError('模型列表响应过大')
                payload = json.loads(raw.decode('utf-8-sig'))
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise ModelDiscoveryError(f'模型探测返回 HTTP {code}，请检查凭据与模型列表接口') from None
        except (TimeoutError, socket.timeout):
            raise ModelDiscoveryError('模型探测超时，请检查接口地址和网络连接') from None
        except (URLError, HTTPException, OSError) as exc:
            raise ModelDiscoveryError('无法连接模型列表接口：' + connection_failure(exc)) from None
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, ModelDiscoveryError):
                raise
            raise ModelDiscoveryError('模型列表接口返回了无法识别的 JSON') from None
        models.extend(extract_model_ids(payload))
        token = payload.get('nextPageToken') if isinstance(payload, dict) else None
        if not token or canonical_service(entry.get('service')) != 'gemini':
            break
        if str(token) in tokens:
            raise ModelDiscoveryError('模型列表分页标识重复')
        tokens.add(str(token))
        parts = urlsplit(url)
        query = [(name, value) for name, value in parse_qsl(parts.query) if name != 'pageToken']
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode([*query, ('pageToken', str(token))]), ''))
    else:
        raise ModelDiscoveryError('模型列表分页超过上限，请使用更精确的接口')
    models = list(dict.fromkeys(models))[:5000]
    if not models:
        raise ModelDiscoveryError('请求成功，但未识别到可选择的模型；已保存模型保持不变')
    return models
